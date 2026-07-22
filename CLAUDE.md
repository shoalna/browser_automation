# Project Instructions — browser_automation

A fluent, **synchronous** Python wrapper around Playwright for scraping, RPA, and UI
automation. Headline features: `*_agent` methods resolve natural-language element
descriptions to cached XPaths via Claude, and `Scenario` compiles a whole
natural-language scenario into cached library code that runs on top of `*_agent`.
Current version: 0.5.0.

## Tech Stack
- Python ≥3.9, Playwright ≥1.40 (chromium/firefox/webkit)
- Anthropic SDK ≥0.69 (Claude) — used only by `*_agent` methods
- Packaging: setuptools + `pyproject.toml`; env managed with `uv`
- Optional: Flet ≥0.21 (only for the standalone `fake_gui.py` demo)

## Code Style
- `snake_case` files/functions, `PascalCase` classes.
- **Sync only** — never expose `async`/`await` in the public API.
- Fluent methods live on `Browser` (`browser.py`), return `"Browser"` for chaining,
  and queue a step rather than acting immediately; steps execute on `.run()`.
- Action logic is **stateless** in `actions.py` as `action_*(page, ...)` functions
  taking a Playwright `Page`/`Frame`. `Browser` holds state; `actions.py` does not.
- New `*_agent` methods mirror their base method's signature (description as first
  arg) and route the resolved `xpath=...` back through the same `action_*` fn, so
  `optional=` and frame mode keep working unchanged.
- Locator actions target `_ensure_target()` (active frame or page); page-level
  actions (`goto`, `press_key`, `screenshot`, `pause`) use `_ensure_page()`.

## Project Structure
- `browser_automation/browser.py` — `Browser` facade: all fluent methods, step runner, frame stack
- `browser_automation/actions.py` — stateless `action_*` functions
- `browser_automation/extractors.py` — `extract_value` / `extract_all_values` (text vs attr)
- `browser_automation/agent.py` — `AgentResolver` (NL→XPath via Claude) + `AgentCache`
- `browser_automation/codegen.py` — `Scenario` (NL scenario → cached library code) + `Parse`/`Prompter`; CLI `python -m browser_automation.codegen`
- `browser_automation/_anthropic.py`, `_jsoncache.py` — shared LLM client/call + atomic JSON cache base (used by both `agent.py` and `codegen.py`)
- `browser_automation/result.py` — `Result`: dict-like access + `.errors` / `.ok`
- `examples/`, `scripts/` — runnable live examples / smoke + offline tests
- `fake_gui.py` — standalone Flet demo, not part of the package

## Agent (`*_agent`) feature
- `help=True` gates **live LLM calls only**; the cache is always consulted.
- Cache key = `(full_url, method, description)`; human-diffable nested JSON, atomic
  write via `os.replace`. Commit the cache so production runs need no API key.
- Single-target methods require **exactly one** match (raise on ambiguity, even
  after one automatic re-ask); `extract_all_agent` accepts ≥1.
- Requires `ANTHROPIC_API_KEY` (or `agent_api_key=`) when a live call fires; store
  it in `.env` (gitignored) — see `.env.example`.

## Scenario (`codegen.py`) feature
- `Scenario(text, parse=Parse(...), help=True).run() -> Result`; symmetric with `Browser`.
- `{placeholder}` tokens = runtime params supplied by `Parse` (never hashed/baked);
  literal text is baked+hashed. Missing placeholder value → hard error.
- Generated code is an internal cached intermediate (`{scenario_hash: code}`) built
  on `*_agent`; only code from a **validated** run is cached; cache-hit re-executes
  for fresh data and regenerates if it no longer validates.
- Generated code is `exec`'d under an AST whitelist (params/browser + literals only)
  in a no-builtins namespace, inside a worker thread with a wall-clock timeout.
- `help` gates all live LLM calls (completeness check + generation); `Prompter` is
  for clarifications only. Offline tests: `uv run python scripts/test_codegen.py`.

## Build & Run
- Smoke test: `uv run python scripts/smoke.py` (needs `ANTHROPIC_API_KEY`)
- Example: `uv run python examples/agent_workflow.py`
- Build: `python -m build` (dev extra provides `build`/`twine`)
- Browser binaries auto-install on first use.

## Testing
- **No unit test suite.** Verification is via the live smoke scripts in `scripts/`
  and `real_test/`. When changing behavior, exercise it through a smoke script.

## Conventions
- Commits: short imperative, capitalized subject ("Add…", "Fix…", "Enforce…").
- Feature branches (`feat/...`) merged to `main` via PR.
- Bump `__version__` in `__init__.py` and `version` in `pyproject.toml` together.
