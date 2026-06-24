"""Example: drive a multi-step web workflow with natural-language ``*_agent`` calls.

Describe each element in plain language instead of writing selectors. Adapted
from a real RPA flow (logging into a timekeeping app and filing a leave request),
with the site-specific values parametrized out.

Set these environment variables before running:

    ANTHROPIC_API_KEY    your Anthropic API key (or pass agent_api_key=)
    APP_URL              the page to start from
    APP_CUSTOMER_ID      login values
    APP_USER
    APP_PASSWORD

Run:

    uv run python examples/agent_workflow.py

Lessons baked into the comments below:

- ``help=True`` permits live LLM resolution on a cache miss; resolved XPaths are
  cached to ``agent_cache_file`` so later runs reuse them with no API call.
- No manual ``wait()`` calls are needed between steps: ``agent_wait`` settles the
  page before each *live* resolution (the first/recording run), and cache hits
  wait briefly for the stored XPath to appear before use. Bump ``agent_wait`` if
  a page is slow to render; drop to an explicit ``.wait("<selector>")`` only when
  you need to gate on something other than the next step's own target.
- Prefer a concrete description ("2026年6月25日") over a relative one ("明日"):
  the model can't reliably compute today's date.
- ``type_agent`` targets the editable field even when you describe it by its
  label text.
- Descriptions can be in any language — they just need to match what's on screen.
"""

from __future__ import annotations

import os
import sys

from browser_automation import Browser


def main() -> int:
    url = os.getenv("APP_URL")
    if not (url and os.getenv("ANTHROPIC_API_KEY")):
        print(
            "Set ANTHROPIC_API_KEY and APP_URL (plus APP_CUSTOMER_ID / APP_USER / "
            "APP_PASSWORD) before running.",
            file=sys.stderr,
        )
        return 2

    result = (
        Browser(
            headless=False,                      # watch it run; True for automation
            help=True,                           # allow live LLM resolution on miss
            agent_cache_file=".agent_cache.json",
            record_video_dir="videos",           # optional: save a .webm of the run
            agent_wait=2,                         # settle before each live resolution
        )
        .goto(url)
        # --- log in: describe each field by its visible label ----------------
        .type_agent("お客様ID", os.getenv("APP_CUSTOMER_ID", "<customer-id>"))
        .type_agent("ログインID", os.getenv("APP_USER", "<user>"))
        .type_agent("パスワード", os.getenv("APP_PASSWORD", "<password>"))
        .click_agent("入力欄下のログインボタン")
        # --- dismiss a popup if one appears (optional => soft-fail) ----------
        .click_agent("ポップアップしたものを閉じる", optional=True)
        # --- navigate to the leave-request form -----------------------------
        .click_agent("申請承認")
        .click_agent("各種申請")
        .click_agent("新規申請の+")
        .click_agent("申請書提出欄の有給申請")
        # --- fill it in ------------------------------------------------------
        .click_agent("2026年6月25日")            # concrete date; relative ("明日") is unreliable
        .type_agent("理由", "休暇申請のテスト")    # resolves to the textarea, not the "理由" label
        # .click_agent("提出する")                # final submit omitted on purpose
        .run()
    )

    print(f"ok={result.ok}  errors={result.errors}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
