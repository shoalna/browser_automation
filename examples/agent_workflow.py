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
- Add a ``wait()`` after anything that navigates or loads content, or the next
  ``*_agent`` step may resolve against a half-rendered page.
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
        )
        .goto(url)
        # --- log in: describe each field by its visible label ----------------
        .type_agent("お客様ID", os.getenv("APP_CUSTOMER_ID", "<customer-id>"))
        .type_agent("ログインID", os.getenv("APP_USER", "<user>"))
        .type_agent("パスワード", os.getenv("APP_PASSWORD", "<password>"))
        .click_agent("入力欄下のログインボタン")
        .wait()                                  # wait out the login navigation
        .wait(seconds=2)                         # let the post-login UI settle
        # --- dismiss a popup if one appears (optional => soft-fail) ----------
        .click_agent("ポップアップしたものを閉じる", optional=True)
        .wait(seconds=1)
        # --- navigate to the leave-request form -----------------------------
        .click_agent("申請承認")
        .wait()
        .click_agent("各種申請")
        .wait()
        .click_agent("新規申請の+")
        .wait(seconds=1)
        .click_agent("申請書提出欄の有給申請")
        .wait(seconds=1)
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
