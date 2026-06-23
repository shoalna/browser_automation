"""
LLM-assisted element resolution for the ``*_agent`` methods.

When *help mode* is on (``Browser(help=True)``), an ``*_agent`` call whose target
isn't already cached sends the cleaned page DOM to Claude and asks for a robust
XPath for the described element. Resolved XPaths are cached to a JSON file keyed
by ``(url, method, description)`` so subsequent runs reuse them without an LLM
call.

This module is internal — users interact with it only through ``Browser``'s
``*_agent`` methods. See the project memory note ``feature-agent-xpath-resolution``
for the full design rationale.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

import anthropic

logger = logging.getLogger("browser_automation")


class AgentResolutionError(RuntimeError):
    """Raised when an ``*_agent`` description cannot be resolved to a usable XPath.

    Raised when:

    - there is no cached XPath and ``help=False`` (live LLM calls disabled),
    - a cached XPath no longer matches and ``help=False`` (cannot re-resolve),
    - the LLM cannot identify the element after one re-ask, or
    - a single-target description resolves to multiple elements even after the
      re-ask (ambiguous — write a more specific description).

    When the failing ``*_agent`` call passed ``optional=True``, this is recorded
    in :attr:`Result.errors` instead of propagating.
    """

    def __init__(self, description: str, reason: str) -> None:
        self.description = description
        self.reason = reason
        super().__init__(f"could not resolve {description!r}: {reason}")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class AgentCache:
    """Nested-dict JSON cache of resolved XPaths, keyed url -> method -> description.

    Loaded lazily on first access and written through (atomically) after every
    new resolution so a workflow that crashes partway keeps what it learned.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict | None = None  # loaded lazily

    def _load(self) -> None:
        if self._data is not None:
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {}

    def get(self, url: str, method: str, description: str) -> str | None:
        self._load()
        assert self._data is not None
        return self._data.get(url, {}).get(method, {}).get(description)

    def set(self, url: str, method: str, description: str, xpath: str) -> None:
        self._load()
        assert self._data is not None
        self._data.setdefault(url, {}).setdefault(method, {})[description] = xpath
        self._flush()

    def _flush(self) -> None:
        """Write the cache atomically: temp file in the same dir, then os.replace."""
        directory = os.path.dirname(os.path.abspath(self._path)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# LLM resolver
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert at writing robust XPath selectors for web automation.

Given the cleaned HTML of a web page and a natural-language description of an
element, return an XPath that locates it.

Rules for a ROBUST XPath:
- Prefer stable handles: @id, @name, @data-testid, @aria-label, or unique
  visible text (via normalize-space()).
- Use relative XPath starting with // — never absolute positional paths like
  /html/body/div[2]/div[3].
- Avoid numeric positional indices unless nothing else can disambiguate.
- Match the user's intent precisely; do not broaden the selector to "make it
  match something".

If the description targets a single element, the XPath MUST match exactly one
element. If you cannot confidently identify the element, set found=false and
explain why in reason."""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "xpath": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["found", "xpath", "reason"],
    "additionalProperties": False,
}

# JS run in the page/frame to produce a cleaned HTML payload for the LLM.
_CLEAN_JS = """
() => {
  const KEEP = new Set([
    'id', 'class', 'name', 'role', 'aria-label', 'aria-labelledby',
    'href', 'type', 'placeholder', 'value', 'title', 'alt', 'for',
    'data-testid', 'data-test', 'data-id'
  ]);
  const root = document.documentElement.cloneNode(true);
  root.querySelectorAll('script,style,noscript,svg,template,link,meta,head')
      .forEach(e => e.remove());
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_COMMENT);
  const comments = [];
  while (walker.nextNode()) comments.push(walker.currentNode);
  comments.forEach(c => c.remove());
  root.querySelectorAll('*').forEach(el => {
    Array.from(el.attributes).forEach(a => {
      if (!KEEP.has(a.name)) el.removeAttribute(a.name);
    });
  });
  return root.outerHTML;
}
"""


class AgentResolver:
    """Resolves natural-language descriptions to XPaths, cache-first, LLM on miss."""

    def __init__(self, model: str, cache: AgentCache, api_key: str | None = None) -> None:
        self._model = model
        self._cache = cache
        self._api_key = api_key
        self._client: anthropic.Anthropic | None = None  # built lazily on first live call

    def _ensure_client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = (
                anthropic.Anthropic(api_key=self._api_key)
                if self._api_key
                else anthropic.Anthropic()
            )
        return self._client

    # -- public entry point -------------------------------------------------

    def resolve(
        self,
        target,
        url: str,
        method: str,
        description: str,
        *,
        multi: bool,
        help: bool,
    ) -> str:
        """Return an XPath for *description*, using the cache then the LLM.

        *target* is the active Playwright page or frame; *url* keys the cache.
        """
        cached = self._cache.get(url, method, description)
        if cached is not None:
            if self._matches(target, cached, multi):
                logger.debug("agent cache hit: %s -> %s", description, cached)
                return cached
            logger.debug("agent cache stale for %r (re-resolving)", description)
            if not help:
                raise AgentResolutionError(
                    description,
                    "cached XPath no longer matches and help=False — "
                    "run with help=True to re-resolve",
                )
        elif not help:
            raise AgentResolutionError(
                description,
                "no cached XPath and help=False — run with help=True to allow "
                "LLM resolution",
            )

        xpath = self._llm_resolve(target, method, description, multi)
        self._cache.set(url, method, description, xpath)
        logger.debug("agent resolved %r -> %s (cached)", description, xpath)
        return xpath

    # -- internals ----------------------------------------------------------

    def _match_count(self, target, xpath: str) -> int:
        try:
            return target.locator(f"xpath={xpath}").count()
        except Exception:
            return -1  # malformed XPath

    def _matches(self, target, xpath: str, multi: bool) -> bool:
        count = self._match_count(target, xpath)
        if multi:
            return count >= 1
        return count == 1

    def _llm_resolve(self, target, method: str, description: str, multi: bool) -> str:
        html = target.evaluate(_CLEAN_JS)
        messages = [{"role": "user", "content": self._user_prompt(description, multi, html)}]

        for attempt in range(2):  # initial attempt + one re-ask
            result = self._call(messages)

            if not result.get("found"):
                reason = result.get("reason") or "model could not identify the element"
            else:
                xpath = result.get("xpath") or ""
                count = self._match_count(target, xpath)
                if count == 1 or (multi and count >= 1):
                    return xpath
                if count <= 0:
                    reason = (
                        f"XPath {xpath!r} matched no elements"
                        if count == 0
                        else f"XPath {xpath!r} is not valid"
                    )
                else:
                    reason = (
                        f"XPath {xpath!r} matched {count} elements but exactly one "
                        "is required — make it uniquely identifying"
                    )

            if attempt == 0:
                messages.append({"role": "assistant", "content": json.dumps(result)})
                messages.append({
                    "role": "user",
                    "content": f"That did not work: {reason}. Provide a corrected XPath.",
                })
            else:
                raise AgentResolutionError(description, reason)

        # Unreachable (loop either returns or raises), but keeps type checkers happy.
        raise AgentResolutionError(description, "resolution failed")

    def _call(self, messages: list[dict]) -> dict:
        client = self._ensure_client()
        resp = client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=messages,
            output_config={"format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            return {"found": False, "xpath": "", "reason": "model returned no content"}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"found": False, "xpath": "", "reason": "model returned invalid JSON"}

    @staticmethod
    def _user_prompt(description: str, multi: bool, html: str) -> str:
        if multi:
            target_line = (
                "Find ALL elements matching this description (this targets a "
                "collection, so the XPath may match multiple elements):"
            )
        else:
            target_line = (
                "Find THE SINGLE element matching this description (the XPath "
                "MUST match exactly one element):"
            )
        return (
            f"{target_line}\n\n"
            f"\"{description}\"\n\n"
            "Here is the cleaned page HTML:\n\n"
            f"{html}"
        )
