"""
Offline checks for the deterministic core of browser_automation.codegen.

Exercises the parts that need no Anthropic key or browser: placeholder scanning,
Parse validation, scenario hashing, the AST whitelist, and the cache round-trip.
A scripted Prompter stands in for the console so the escalation path is testable.

Run: python scripts/test_codegen.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_automation.codegen import (  # noqa: E402
    CodeValidationError,
    MissingParamsError,
    Parse,
    ScenarioCache,
    compile_scenario,
    extract_placeholders,
    scenario_key,
    validate_code,
)

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}")


def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


# --- placeholders ----------------------------------------------------------
print("placeholders:")
check("simple", extract_placeholders("log into {url} as {user}") == {"url", "user"})
check("none", extract_placeholders("just text") == set())
check("doubled braces are literal", extract_placeholders("a {{literal}} b {x}") == {"x"})
check("dedupes", extract_placeholders("{x} then {x}") == {"x"})

# --- Parse -----------------------------------------------------------------
print("Parse:")
p = Parse(url="u", user="me")
check("validate passes when covered", p.validate({"url", "user"}) is None)
check("missing raises", raises(MissingParamsError, lambda: Parse(url="u").validate({"url", "pw"})))
check("extra only warns", Parse(url="u", extra="x").validate({"url"}) is None)
check("repr hides values", "me" not in repr(Parse(password="me")))

# --- hashing ---------------------------------------------------------------
print("hashing:")
check("whitespace-insensitive", scenario_key("a  b\n c") == scenario_key("a b c"))
check("placeholder-stable", scenario_key("hi {x}") == scenario_key("hi {x}"))
check("different text differs", scenario_key("a") != scenario_key("b"))

# --- AST whitelist ---------------------------------------------------------
print("AST whitelist:")
good = (
    "def scenario(params, browser):\n"
    "    return (browser.goto('https://x').type_agent('field', params['user'])"
    ".extract_agent('title', name='t').run())\n"
)
check("valid chain accepted", validate_code(good) is not None)
check("import rejected", raises(CodeValidationError, lambda: validate_code(
    "def scenario(params, browser):\n    import os\n    return browser.run()\n")))
check("wrong name rejected", raises(CodeValidationError, lambda: validate_code(
    "def helper(params, browser):\n    return browser.run()\n")))
check("wrong signature rejected", raises(CodeValidationError, lambda: validate_code(
    "def scenario(browser):\n    return browser.run()\n")))
check("foreign name rejected", raises(CodeValidationError, lambda: validate_code(
    "def scenario(params, browser):\n    return os.system('x')\n")))
check("dunder rejected", raises(CodeValidationError, lambda: validate_code(
    "def scenario(params, browser):\n    return browser.__class__\n")))
check("loop rejected", raises(CodeValidationError, lambda: validate_code(
    "def scenario(params, browser):\n    for i in []:\n        pass\n    return browser.run()\n")))
check("assignment rejected", raises(CodeValidationError, lambda: validate_code(
    "def scenario(params, browser):\n    b = browser\n    return b.run()\n")))
check("dict literal rejected", raises(CodeValidationError, lambda: validate_code(
    "def scenario(params, browser):\n    return browser.go({'a': 1}).run()\n")))

# --- compile + exec (no builtins) ------------------------------------------
print("compile/exec:")


class FakeBrowser:
    def goto(self, url):
        self.url = url
        return self

    def run(self):
        return {"ok": True}


fn = compile_scenario(
    "def scenario(params, browser):\n    return browser.goto(params['u']).run()\n")
check("compiled fn runs", fn({"u": "https://x"}, FakeBrowser()) == {"ok": True})
check("open() unavailable in exec ns", raises(Exception, lambda: compile_scenario(
    "def scenario(params, browser):\n    return open('/etc/passwd')\n")({}, FakeBrowser())))

# --- cache round-trip ------------------------------------------------------
print("cache:")
with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "cache.json")
    c = ScenarioCache(path)
    check("miss returns None", c.get("k") is None)
    c.set("k", "code-src", ["t"], "the scenario")
    check("hit returns entry", c.get("k")["code"] == "code-src")
    check("persisted to disk", ScenarioCache(path).get("k")["extractions"] == ["t"])

# --- inline-secret warning -------------------------------------------------
print("inline-secret warning:")
import logging  # noqa: E402
from browser_automation.codegen import _warn_inline_secrets  # noqa: E402


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def warnings_for(text, parse):
    cap = _Capture()
    log = logging.getLogger("browser_automation")
    log.addHandler(cap)
    try:
        _warn_inline_secrets(text, parse)
    finally:
        log.removeHandler(cap)
    return cap.records


check("warns on inlined secret value", len(
    warnings_for("log in with hunter2", Parse(password="hunter2"))) == 1)
check("silent when secret is a placeholder", len(
    warnings_for("log in with {password}", Parse(password="hunter2"))) == 0)
check("non-secret key ignored", len(
    warnings_for("search for kittens", Parse(query="kittens"))) == 0)

# --- AgentCache still works after sharing AtomicJsonCache ------------------
print("AgentCache (shared base):")
from browser_automation.agent import AgentCache  # noqa: E402
with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "agent.json")
    ac = AgentCache(path)
    check("agent miss", ac.get("u", "click", "the button") is None)
    ac.set("u", "click", "the button", "//button")
    check("agent hit", ac.get("u", "click", "the button") == "//button")
    check("agent persisted", AgentCache(path).get("u", "click", "the button") == "//button")

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
