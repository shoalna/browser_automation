"""
Scenario -> code: compile a natural-language scenario into ``browser_automation``
code, run it, validate the result, and return the data.

A :class:`Scenario` reads a plain-language description of a web task, asks Claude
to generate a fluent chain built on the ``*_agent`` methods, executes it, and
checks the outcome. Generated code is an *internal, cached* intermediate keyed by
a hash of the scenario, so resubmitting the same scenario skips the LLM and just
re-runs the stored code against the live site (fresh data every time).

The surface is symmetric with :class:`~browser_automation.browser.Browser`::

    from browser_automation import Scenario, Parse

    result = (
        Scenario(
            "log into {url}, then read the account balance",
            parse=Parse(url="https://bank.example", username="me", password=SECRET),
            help=True,
        )
        .run()
    )
    print(result["balance"])

A thin CLI wraps the same core::

    python -m browser_automation.codegen "read the top headline from {url}" -p url=https://news.ycombinator.com

Design rationale lives in the project memory note ``feature-scenario-codegen``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import threading
from typing import Any, Protocol

import anthropic

from ._anthropic import DEFAULT_MODEL, build_client, structured_json_text
from ._jsoncache import AtomicJsonCache
from .result import Result

logger = logging.getLogger("browser_automation")

# A {placeholder} token: a Python-identifier name in single braces. Doubled
# braces ({{ }}) are literal and never treated as placeholders.
_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")

# Parse keys whose values look like secrets — used only to sharpen the inline
# warning below (see _warn_inline_secrets).
_SECRET_KEY_RE = re.compile(
    r"(pass(word|wd)?|pwd|secret|token|api[_-]?key|credential|auth)", re.I
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ScenarioError(RuntimeError):
    """Base class for scenario-compilation failures."""


class MissingParamsError(ScenarioError):
    """A ``{placeholder}`` in the scenario has no value in :class:`Parse`."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        joined = ", ".join(repr(m) for m in missing)
        super().__init__(
            f"scenario references placeholder(s) with no value in Parse: {joined}"
        )


class CodeValidationError(ScenarioError):
    """Generated code failed the AST whitelist (used disallowed constructs)."""


class ScenarioResolutionError(ScenarioError):
    """The scenario could not be turned into a validated, working implementation."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"could not realize scenario: {reason}")


# ---------------------------------------------------------------------------
# Parameters and prompting
# ---------------------------------------------------------------------------

class Parse:
    """Declarative bag of values for a scenario's ``{placeholder}`` tokens.

    Every placeholder in the scenario must have a value here; a missing one
    raises :class:`MissingParamsError` (values are never prompted for). Secrets
    belong here as placeholders so they stay out of the scenario text, the cache
    key, and the generated code.
    """

    def __init__(self, **values: Any) -> None:
        self._values = dict(values)

    def values(self) -> dict[str, Any]:
        return dict(self._values)

    def validate(self, required: set[str]) -> None:
        """Raise if any *required* placeholder is unfilled; warn on extras."""
        missing = required - self._values.keys()
        if missing:
            raise MissingParamsError(sorted(missing))
        extra = self._values.keys() - required
        if extra:
            logger.warning(
                "Parse has value(s) not used by any placeholder: %s",
                ", ".join(sorted(extra)),
            )

    def __repr__(self) -> str:  # keep secret values out of logs/reprs
        return f"Parse(keys={sorted(self._values)})"


class Prompter(Protocol):
    """Asks the user a clarifying question and returns their answer.

    Used only for clarifications the tool cannot supply itself — missing
    structural detail (start URL, success signal) and escalation when
    self-correction is exhausted. Routine input *values* come from :class:`Parse`,
    never from here. The default is :class:`ConsolePrompter`; a GUI injects its
    own implementation.
    """

    def ask(self, question: str) -> str: ...


class ConsolePrompter:
    """Terminal :class:`Prompter` backed by ``input()``."""

    def ask(self, question: str) -> str:
        return input(f"{question}\n> ").strip()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class ScenarioCache(AtomicJsonCache):
    """JSON cache of generated code, keyed by scenario hash.

    Value is ``{"code": str, "extractions": [str], "scenario": str}``. The
    scenario text is stored purely for human diffability. Only code that has
    produced a validated run is ever written here.
    """

    def get(self, key: str) -> dict | None:
        self._load()
        assert self._data is not None
        return self._data.get(key)

    def set(self, key: str, code: str, extractions: list[str], scenario: str) -> None:
        self._load()
        assert self._data is not None
        self._data[key] = {
            "code": code,
            "extractions": extractions,
            "scenario": scenario,
        }
        self._flush()


# ---------------------------------------------------------------------------
# Placeholder + hashing helpers
# ---------------------------------------------------------------------------

def extract_placeholders(text: str) -> set[str]:
    """Return the set of ``{placeholder}`` names in *text* (``{{`` is literal)."""
    return set(_PLACEHOLDER_RE.findall(text))


def scenario_key(text: str) -> str:
    """Stable cache key: sha256 of the whitespace-normalized scenario.

    Placeholders are left intact, so the key is independent of the runtime
    values that fill them.
    """
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _warn_inline_secrets(text: str, parse: Parse) -> None:
    """Warn when a secret-looking value appears literally in the scenario.

    Placeholders are the sanctioned path for secrets; this is the surviving
    guard for a user who pastes a raw value inline instead. Only warns on
    :class:`Parse` values whose key looks secret-ish and that occur verbatim in
    the scenario (precise — no false positives from free text).
    """
    for name, value in parse.values().items():
        if not _SECRET_KEY_RE.search(name):
            continue
        svalue = str(value)
        if svalue and svalue in text:
            logger.warning(
                "value for %r appears inline in the scenario text — use a "
                "{%s} placeholder so the secret stays out of the cache and "
                "generated code",
                name, name,
            )


# ---------------------------------------------------------------------------
# AST whitelist for generated code
# ---------------------------------------------------------------------------

# Node types permitted in the generated function: a single returned chain built
# from the injected params/browser plus literals. No assignments, loops,
# lambdas, comprehensions, or dict/set literals — "library API + literals +
# params only".
_ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return,
    ast.Expr, ast.Call, ast.Attribute, ast.Name, ast.Constant,
    ast.Subscript, ast.keyword, ast.Load, ast.List, ast.Tuple,
)


def validate_code(code: str) -> ast.Module:
    """Parse *code* and enforce the whitelist, or raise :class:`CodeValidationError`.

    The generated artifact must be a single ``def scenario(params, browser):``
    that builds and returns a ``browser`` chain. Imports, other function/class
    definitions, loops, lambdas, comprehensions, assignments, dunder access, and
    references to any name other than ``params``/``browser`` are rejected — a
    cheap deterministic guard against a hallucinating model, standing in for a
    full sandbox.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise CodeValidationError(f"generated code is not valid Python: {e}") from e

    body = tree.body
    if len(body) != 1 or not isinstance(body[0], ast.FunctionDef):
        raise CodeValidationError("generated code must define exactly one function")
    func = body[0]
    if func.name != "scenario":
        raise CodeValidationError(f"function must be named 'scenario', got {func.name!r}")
    argnames = [a.arg for a in func.args.args]
    if argnames != ["params", "browser"]:
        raise CodeValidationError(
            f"function signature must be scenario(params, browser), got {argnames}"
        )

    allowed_names = {"params", "browser"}
    for node in ast.walk(func):
        if isinstance(node, ast.arg):
            continue
        if not isinstance(node, _ALLOWED_NODES):
            raise CodeValidationError(
                f"disallowed syntax in generated code: {type(node).__name__}"
            )
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise CodeValidationError(f"disallowed dunder access: {node.attr!r}")
        if isinstance(node, ast.Name) and node.id not in allowed_names:
            raise CodeValidationError(f"disallowed reference to name {node.id!r}")

    return tree


def compile_scenario(code: str):
    """Validate then compile *code*, returning the ``scenario`` callable.

    Executed in a namespace with no builtins so the linear chain cannot reach
    ``open``/``eval``/``__import__`` even if the whitelist were bypassed.
    """
    validate_code(code)
    namespace: dict = {"__builtins__": {}}
    exec(compile(code, "<scenario>", "exec"), namespace)  # noqa: S102 - guarded above
    return namespace["scenario"]


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

_COMPLETENESS_SYSTEM = """\
You check whether a web-automation scenario contains enough information to be
turned into code. Required ingredients:
  1. A start URL (where the task begins).
  2. A success signal — either something concrete to extract, OR, for a
     side-effect task, an explicit confirmation of success stated by the user.

Input VALUES may appear as {placeholder} tokens; they are supplied separately, so
never ask for their values. Ask only about missing STRUCTURAL pieces (a start URL
or a success signal). Return complete=true when both are present; otherwise list
one short question per missing piece."""

_COMPLETENESS_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["complete", "questions"],
    "additionalProperties": False,
}

_GENERATE_SYSTEM = """\
You convert a natural-language web-automation scenario into Python that uses the
`browser_automation` fluent API. Emit EXACTLY one function:

    def scenario(params, browser):
        return (
            browser
            .goto("https://...")
            .type_agent("the email field", params["username"])
            .click_agent("the log in button")
            .extract_agent("the account balance", name="balance")
            .run()
        )

Hard rules:
- A SINGLE returned chain. Do NOT import anything, construct Browser(), define
  other functions, assign variables, or use loops, lambdas, comprehensions, or
  conditionals.
- Refer to every page element with a *_agent method and a natural-language
  description, never a raw CSS/XPath selector:
    .click_agent("desc"), .type_agent("desc", value), .hover_agent("desc"),
    .select_agent("desc", value), .extract_agent("desc", name="x"),
    .extract_all_agent("desc", name="x")
- {placeholder} tokens in the scenario are RUNTIME PARAMETERS. Reference them as
  params["placeholder"] — never inline their literal values.
- Any non-constant value the flow needs must come from params["..."].
- Give every extraction a unique name= and list all of them in `extractions`.
- The chain MUST end in .run().
- For a side-effect task, confirm success by adding an .extract_agent(...) that
  reads the page's confirmation text, and include its name in `extractions`.

Return the function source in `code` and the list of extraction names in
`extractions`."""

_GENERATE_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "extractions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["code", "extractions"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Scenario facade
# ---------------------------------------------------------------------------

class Scenario:
    """Compile a natural-language scenario into library code, run it, return data.

    Symmetric with :class:`~browser_automation.browser.Browser`: build one and
    call :meth:`run`, which returns a :class:`~browser_automation.result.Result`.
    """

    def __init__(
        self,
        text: str,
        *,
        parse: Parse | None = None,
        help: bool = False,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        code_cache_file: str = ".browser_automation_scenario_cache.json",
        agent_cache_file: str = ".browser_automation_agent_cache.json",
        prompter: Prompter | None = None,
        max_retries: int = 3,
        timeout: float = 120.0,
        headless: bool = True,
        agent_wait: float = 0,
        verbose: bool = False,
    ) -> None:
        self._text = text
        self._parse = parse or Parse()
        self._help = help
        self._model = model
        self._api_key = api_key
        self._cache = ScenarioCache(code_cache_file)
        self._agent_cache_file = agent_cache_file
        self._prompter = prompter or ConsolePrompter()
        self._max_retries = max_retries
        self._timeout = timeout
        self._headless = headless
        self._agent_wait = agent_wait
        self._client: anthropic.Anthropic | None = None

        if verbose:
            logging.getLogger("browser_automation").setLevel(logging.DEBUG)

        # Placeholders are known up front; fail fast before any LLM work.
        self._required = extract_placeholders(text)
        self._parse.validate(self._required)
        _warn_inline_secrets(text, self._parse)

    # -- public API ---------------------------------------------------------

    def run(self) -> Result:
        """Realize the scenario and return its :class:`Result`.

        On a cache hit, the stored code is re-executed against the live site for
        fresh data; if it no longer validates, it is regenerated. On a miss (with
        ``help=True``), the scenario is completed, generated, executed, and
        refined until it validates.
        """
        code, extractions = self._obtain_code()
        params = self._parse.values()

        if code is not None:
            try:
                return self._execute(code, extractions, params)
            except ScenarioResolutionError:
                logger.debug("cached scenario code stale — regenerating")
                if not self._help:
                    raise

        # Miss (or stale hit with help=True): generate from scratch.
        return self._generate_run_refine(params)

    def to_code(self) -> str:
        """Return the generated source without running it (generating if needed).

        A read-only escape hatch: it never writes the run cache, since the code
        it returns has not been executed or validated. Only :meth:`run` caches,
        and only code that produced a validated run.
        """
        code, _ = self._obtain_code()
        if code is not None:
            return code
        if not self._help:
            raise ScenarioResolutionError(
                "no cached code and help=False — run with help=True to generate"
            )
        text = self._complete_scenario()
        gen = self._llm_generate(text, feedback=None)
        validate_code(gen["code"])
        return gen["code"]

    # -- cache / completeness ----------------------------------------------

    def _obtain_code(self) -> tuple[str | None, list[str]]:
        entry = self._cache.get(scenario_key(self._text))
        if entry is not None:
            return entry["code"], entry.get("extractions", [])
        return None, []

    def _complete_scenario(self) -> str:
        """Run the pre-flight completeness check, folding in Prompter answers."""
        if not self._help:
            # No LLM permitted; use the scenario as written.
            return self._text
        check = self._call(
            _COMPLETENESS_SYSTEM,
            f"Scenario:\n\n{self._text}",
            _COMPLETENESS_SCHEMA,
        )
        if check.get("complete", True):
            return self._text
        additions = []
        for question in check.get("questions", []):
            answer = self._prompter.ask(question)
            additions.append(f"- {question} {answer}")
        if not additions:
            return self._text
        return self._text + "\n\nAdditional detail:\n" + "\n".join(additions)

    # -- generate / execute / refine ---------------------------------------

    def _generate_run_refine(self, params: dict) -> Result:
        if not self._help:
            raise ScenarioResolutionError(
                "no cached code and help=False — run with help=True to generate"
            )
        text = self._complete_scenario()
        feedback: str | None = None
        for attempt in range(self._max_retries + 1):
            gen = self._llm_generate(text, feedback)
            code, extractions = gen["code"], gen["extractions"]
            try:
                result = self._execute(code, extractions, params)
            except ScenarioResolutionError as e:
                feedback = e.reason
                logger.debug("scenario attempt %d failed: %s", attempt + 1, feedback)
                continue
            # Success — cache the code that produced a validated run.
            self._cache.set(scenario_key(self._text), code, extractions, self._text)
            return result

        # Self-correction exhausted — escalate to the user once.
        clarification = self._prompter.ask(
            "I couldn't get this scenario working automatically. "
            f"Last problem: {feedback}\nAny clarifying detail?"
        )
        if clarification.strip():
            text = text + f"\n\nAdditional detail:\n- {clarification}"
            gen = self._llm_generate(text, feedback)
            try:
                result = self._execute(gen["code"], gen["extractions"], params)
            except ScenarioResolutionError as e:
                raise ScenarioResolutionError(
                    f"failed after clarification: {e.reason}"
                ) from e
            self._cache.set(
                scenario_key(self._text), gen["code"], gen["extractions"], self._text
            )
            return result
        raise ScenarioResolutionError(feedback or "unknown failure")

    def _execute(self, code: str, extractions: list[str], params: dict) -> Result:
        """Compile, run under a timeout, and mechanically validate the outcome.

        Raises :class:`ScenarioResolutionError` (with a specific reason for the
        refine loop) on any failure: bad code, exception, timeout, soft errors,
        or a missing/empty declared extraction.
        """
        try:
            fn = compile_scenario(code)
        except CodeValidationError as e:
            raise ScenarioResolutionError(str(e)) from e

        result = self._run_with_timeout(fn, params)

        problems = self._validate_result(result, extractions)
        if problems:
            raise ScenarioResolutionError("; ".join(problems))
        return result

    def _run_with_timeout(self, fn, params: dict) -> Result:
        """Run *fn* in a worker thread bounded by a wall-clock timeout.

        The ``Browser`` is both constructed and driven inside the worker so the
        thread-affine sync Playwright API is only ever touched from one thread.
        A timed-out worker is abandoned (daemon) rather than killed — an accepted
        v1 limitation, as full process isolation is out of scope.
        """
        box: dict[str, Any] = {}

        def worker() -> None:
            try:
                browser = self._make_browser()
                box["result"] = fn(params, browser)
            except Exception as e:  # surfaced as a resolution failure below
                box["error"] = e

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(self._timeout)
        if t.is_alive():
            raise ScenarioResolutionError(
                f"execution exceeded {self._timeout:.0f}s timeout"
            )
        if "error" in box:
            raise ScenarioResolutionError(
                f"execution raised {type(box['error']).__name__}: {box['error']}"
            )
        return box["result"]

    @staticmethod
    def _validate_result(result: Result, extractions: list[str]) -> list[str]:
        """Mechanical validation: no soft errors, every extraction present+non-empty."""
        problems: list[str] = []
        problems.extend(result.errors)
        for name in extractions:
            if name not in result:
                problems.append(f"expected extraction {name!r} was not produced")
                continue
            value = result[name]
            if value is None or value == "" or value == []:
                problems.append(f"extraction {name!r} was empty")
        return problems

    def _make_browser(self):
        from .browser import Browser

        return Browser(
            headless=self._headless,
            help=self._help,
            agent_model=self._model,
            agent_cache_file=self._agent_cache_file,
            agent_api_key=self._api_key,
            agent_wait=self._agent_wait,
        )

    # -- LLM plumbing -------------------------------------------------------

    def _llm_generate(self, text: str, feedback: str | None) -> dict:
        user = f"Scenario:\n\n{text}"
        if self._required:
            user += "\n\nPlaceholders available as params: " + ", ".join(
                sorted(self._required)
            )
        if feedback:
            user += (
                "\n\nThe previous attempt failed with: "
                f"{feedback}\nFix the code accordingly."
            )
        return self._call(_GENERATE_SYSTEM, user, _GENERATE_SCHEMA)

    def _ensure_client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = build_client(self._api_key)
        return self._client

    def _call(self, system: str, user: str, schema: dict) -> dict:
        text = structured_json_text(
            self._ensure_client(), self._model, system,
            [{"role": "user", "content": user}], schema, max_tokens=2048,
        )
        if text is None:
            raise ScenarioResolutionError("model returned no content")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ScenarioResolutionError("model returned invalid JSON") from e


# ---------------------------------------------------------------------------
# CLI: python -m browser_automation.codegen
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m browser_automation.codegen",
        description="Compile a natural-language scenario into browser_automation "
        "code, run it, and print the extracted data as JSON.",
    )
    parser.add_argument(
        "scenario", nargs="?",
        help="scenario text (use {name} placeholders); read from stdin if omitted",
    )
    parser.add_argument(
        "-p", "--param", action="append", default=[], metavar="KEY=VALUE",
        help="a placeholder value; repeatable",
    )
    parser.add_argument(
        "--no-help", action="store_true",
        help="forbid live LLM calls (cache-only)",
    )
    parser.add_argument("--verbose", action="store_true", help="DEBUG logging")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    text = args.scenario if args.scenario is not None else sys.stdin.read()
    if not text.strip():
        parser.error("no scenario provided")

    values: dict[str, str] = {}
    for item in args.param:
        if "=" not in item:
            parser.error(f"--param must be KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        values[key] = value

    try:
        result = Scenario(
            text, parse=Parse(**values), help=not args.no_help, verbose=args.verbose,
        ).run()
    except ScenarioError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(json.dumps(result.data, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
