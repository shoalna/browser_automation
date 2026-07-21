"""
Browser — the primary entry point for building automation workflows.

Usage
-----
Construct a ``Browser``, chain action methods, then call ``.run()`` to execute::

    from browser_automation import Browser

    result = (
        Browser()
        .goto("https://news.ycombinator.com")
        .extract_all(".athing .titleline > a", name="headlines")
        .run()
    )

    for headline in result["headlines"]:
        print(headline)

Login and persist session::

    # First run — log in and save state
    Browser(headless=False, state_file="session.json")
        .goto("https://example.com/login")
        .type("#email", "user@example.com")
        .type("#password", "secret")
        .click("[type=submit]")
        .wait()
        .run()

    # Subsequent runs — session is restored automatically
    result = (
        Browser(state_file="session.json")
        .goto("https://example.com/dashboard")
        .extract("h1", name="greeting")
        .run()
    )

Escape hatch — access the raw Playwright page::

    browser = Browser()
    browser.goto("https://example.com")
    raw_page = browser.page   # playwright Page object
    raw_page.pdf(path="output.pdf")
    browser.run()
"""

from __future__ import annotations

import datetime
import logging
import subprocess
import sys
from typing import Callable

from playwright.sync_api import (
    Browser as PlaywrightBrowser,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .actions import (
    action_click,
    action_each,
    action_extract,
    action_extract_all,
    action_goto,
    action_hover,
    action_repeat,
    action_repeat_until,
    action_if_exists,
    action_screenshot,
    action_scroll,
    action_select,
    action_type,
    action_wait,
)
from ._anthropic import DEFAULT_MODEL
from .result import Result

logger = logging.getLogger("browser_automation")

# Sentinel so we can distinguish "user passed None" from "user passed nothing"
_MISSING = object()


class Browser:
    """Fluent browser automation workflow builder.

    Construct once, chain action methods, call ``.run()`` to execute and get
    a :class:`~browser_automation.result.Result`.

    Args:
        browser: Browser engine — ``"chromium"`` (default), ``"firefox"``,
            or ``"webkit"``.
        headless: Run without a visible window. Set ``False`` for debugging.
        viewport: ``(width, height)`` tuple in pixels. Defaults to
            ``(1280, 720)``.
        user_agent: Override the browser's default user agent string.
        http_credentials: Username and password for HTTP Basic Authentication,
            as a ``(username, password)`` tuple. Applied to all requests made
            by this browser session.
        state_file: Path to a JSON file for persisting cookies and
            ``localStorage``. Loaded on start; saved (or created) after
            ``.run()`` completes.
        screenshot_on_failure: Save a timestamped screenshot whenever an
            action raises an exception.
        keep_open_on_error: Keep the browser window open when an error occurs
            instead of closing it automatically. Useful for debugging —
            inspect the page state at the point of failure. Has no effect
            in headless mode.
        record_video_dir: Directory to save a video recording of the whole
            session (Playwright records the browser context). The ``.webm`` file
            is written when ``.run()`` finishes and its path is logged. Works in
            both headless and headed mode.
        record_video_size: ``(width, height)`` of the recorded video. Defaults
            to the viewport size when omitted.
        help: Enable "help mode" — permit live LLM calls to resolve the
            natural-language descriptions passed to the ``*_agent`` methods
            (:meth:`click_agent`, :meth:`type_agent`, …). The XPath cache is
            consulted regardless of this flag; ``help`` only governs whether a
            *cache miss* may call the LLM. With ``help=False`` (default) a cache
            miss raises :class:`~browser_automation.agent.AgentResolutionError`.
        agent_model: Claude model used for ``*_agent`` resolution. Defaults to
            ``"claude-sonnet-4-6"``; override (e.g. ``"claude-opus-4-8"``) for
            harder pages.
        agent_cache_file: Path to the JSON file storing resolved XPaths, keyed
            by ``(url, method, description)``. Written through after each new
            resolution so the LLM is paid at most once per element.
        agent_api_key: Explicit Anthropic API key. If omitted, the SDK reads
            ``ANTHROPIC_API_KEY`` from the environment.
        agent_wait: Seconds to pause before an ``*_agent`` step resolves via the
            LLM, letting the page settle so the model sees the finished DOM.
            Applied **only** on a live resolution (``help=True`` + cache miss) —
            never on a cache hit — so the first/recording run is robust without
            manual ``wait()`` calls and cached runs stay fast. Cached steps
            instead wait briefly for their stored XPath to appear before use.
        verbose: Set to ``True`` to emit DEBUG-level log records on every
            action. Equivalent to calling
            ``logging.getLogger("browser_automation").setLevel(logging.DEBUG)``.

    Examples:
        Minimal usage::

            from browser_automation import Browser

            result = Browser().goto("https://example.com").extract("h1", name="title").run()
            print(result["title"])  # "Example Domain"

        HTTP Basic Auth::

            result = (
                Browser(http_credentials=("alice", "secret"))
                .goto("https://httpbin.org/basic-auth/alice/secret")
                .extract("pre", name="body")
                .run()
            )
            print(result["body"])  # {"authenticated": true, "user": "alice"}

        Full constructor::

            browser = Browser(
                browser="firefox",
                headless=False,
                viewport=(1920, 1080),
                user_agent="Mozilla/5.0 (compatible; MyBot/1.0)",
                http_credentials=("user", "pass"),
                state_file="session.json",
                screenshot_on_failure=True,
                keep_open_on_error=True,
                verbose=True,
            )
    """

    def __init__(
        self,
        *,
        browser: str = "chromium",
        headless: bool = True,
        viewport: tuple[int, int] = (1280, 720),
        user_agent: str | None = None,
        http_credentials: tuple[str, str] | None = None,
        state_file: str | None = None,
        screenshot_on_failure: bool = False,
        keep_open_on_error: bool = False,
        record_video_dir: str | None = None,
        record_video_size: tuple[int, int] | None = None,
        help: bool = False,
        agent_model: str = DEFAULT_MODEL,
        agent_cache_file: str = ".browser_automation_agent_cache.json",
        agent_api_key: str | None = None,
        agent_wait: float = 0,
        verbose: bool = False,
    ) -> None:
        self._browser_type = browser
        self._headless = headless
        self._viewport = {"width": viewport[0], "height": viewport[1]}
        self._user_agent = user_agent
        self._http_credentials = http_credentials
        self._keep_open_on_error = keep_open_on_error
        self._state_file = state_file
        self._screenshot_on_failure = screenshot_on_failure
        self._record_video_dir = record_video_dir
        self._record_video_size = (
            {"width": record_video_size[0], "height": record_video_size[1]}
            if record_video_size
            else None
        )

        # Agent (LLM XPath resolution) config. The resolver is built lazily on
        # first *_agent step so non-agent workflows pay nothing.
        self._help = help
        self._agent_model = agent_model
        self._agent_cache_file = agent_cache_file
        self._agent_api_key = agent_api_key
        self._agent_wait = agent_wait
        self._resolver = None

        if verbose:
            logging.getLogger("browser_automation").setLevel(logging.DEBUG)

        # Deferred Playwright objects — created lazily on first use
        self._playwright: Playwright | None = None
        self._pw_browser: PlaywrightBrowser | None = None
        self._page: Page | None = None

        # Frame mode state — when _active_frame is set, locator-based actions
        # target that frame instead of the main page. _frame_stack supports
        # nested enter_frame() calls.
        self._active_frame = None
        self._frame_stack: list = []

        # Workflow state
        self._steps: list[tuple[Callable, tuple, dict]] = []
        self._store: dict = {}
        self._errors: list[str] = []

    # ------------------------------------------------------------------
    # Internal Playwright lifecycle
    # ------------------------------------------------------------------

    def _ensure_browser_installed(self) -> None:
        """Auto-install the browser binary if it is not already present."""
        from playwright._impl._driver import compute_driver_executable
        driver = compute_driver_executable()  # returns (node_bin, cli_js) tuple
        result = subprocess.run(
            [*driver, "install", self._browser_type],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to install {self._browser_type} browser.\n{result.stderr}"
            )

    def _ensure_page(self) -> Page:
        """Return the active page — must be called inside the run() context."""
        if self._page is None:
            raise RuntimeError("No active page. This should only be called inside run().")
        return self._page

    def _ensure_target(self):
        """Return the current action target — the active frame if in frame mode,
        otherwise the main page.

        Locator-based actions (click, type, extract, …) use this so they follow
        ``enter_frame()`` / ``exit_frame()``. Page-level actions (keyboard,
        screenshot, pause, goto) deliberately use :meth:`_ensure_page` instead so
        they always operate on the real page.
        """
        if self._active_frame is not None:
            return self._active_frame
        return self._ensure_page()

    def _start(self, playwright: Playwright) -> None:
        """Start the browser and create the page inside the playwright context."""
        browser_launcher = getattr(playwright, self._browser_type)

        try:
            self._pw_browser = browser_launcher.launch(headless=self._headless)
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):
                logger.info(
                    "Browser binary for %r not found — installing automatically...",
                    self._browser_type,
                )
                print(
                    f"[browser_automation] Installing {self._browser_type} browser "
                    f"(first-time setup, this may take a minute)...",
                    flush=True,
                )
                self._ensure_browser_installed()
                logger.info("Browser installed. Launching...")
                self._pw_browser = browser_launcher.launch(headless=self._headless)
            else:
                raise

        context_kwargs: dict = {"viewport": self._viewport}
        if self._user_agent:
            context_kwargs["user_agent"] = self._user_agent
        if self._http_credentials:
            username, password = self._http_credentials
            context_kwargs["http_credentials"] = {"username": username, "password": password}
            logger.debug("HTTP Basic Auth configured for user %r", username)
        if self._state_file:
            import os
            if os.path.exists(self._state_file):
                context_kwargs["storage_state"] = self._state_file
                logger.debug("loaded session state from %s", self._state_file)
        if self._record_video_dir:
            context_kwargs["record_video_dir"] = self._record_video_dir
            context_kwargs["record_video_size"] = self._record_video_size or self._viewport
            logger.debug("recording video to %s", self._record_video_dir)

        context = self._pw_browser.new_context(**context_kwargs)
        self._page = context.new_page()

    def _stop(self) -> None:
        """Save session state and close page, context, and browser."""
        try:
            if self._page:
                if self._state_file:
                    self._page.context.storage_state(path=self._state_file)
                    logger.debug("saved session state to %s", self._state_file)
                # The video file is finalized on context.close(); capture its
                # path beforehand so we can report it.
                video_path = None
                if self._record_video_dir and self._page.video:
                    try:
                        video_path = self._page.video.path()
                    except Exception:
                        pass
                context = self._page.context
                self._page.close()
                context.close()
                if video_path:
                    logger.info("video saved to %s", video_path)
                    print(f"[browser_automation] video saved to {video_path}", flush=True)
        except Exception as exc:
            logger.debug("error closing page/context: %s", exc)
        try:
            if self._pw_browser:
                self._pw_browser.close()
        except Exception as exc:
            logger.debug("error closing browser: %s", exc)
        finally:
            self._page = None
            self._pw_browser = None

    def _handle_failure(self, exc: Exception, description: str) -> str:
        """Take a screenshot (if configured) and return an error message."""
        msg = f"{description}: {exc}"
        if self._screenshot_on_failure and self._page:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"failure_{ts}.png"
            try:
                self._page.screenshot(path=fname)
                logger.warning("screenshot saved to %s", fname)
            except Exception:
                pass
        return msg

    def _run_step(self, fn: Callable, args: tuple, kwargs: dict, description: str, optional: bool) -> None:
        try:
            fn(*args, **kwargs)
        except (PlaywrightTimeoutError, Exception) as exc:
            msg = self._handle_failure(exc, description)
            if optional:
                logger.warning("optional step failed — %s", msg)
                self._errors.append(msg)
            else:
                logger.error("step failed — %s", msg)
                raise

    # ------------------------------------------------------------------
    # Agent (LLM XPath resolution) internals
    # ------------------------------------------------------------------

    def _ensure_resolver(self):
        """Build the agent resolver lazily on first use."""
        if self._resolver is None:
            from .agent import AgentCache, AgentResolver
            self._resolver = AgentResolver(
                model=self._agent_model,
                cache=AgentCache(self._agent_cache_file),
                api_key=self._agent_api_key,
                wait=self._agent_wait,
            )
        return self._resolver

    def _resolve_agent(self, method: str, description: str, *, multi: bool) -> str:
        """Resolve *description* to an XPath against the active target.

        The cache is keyed by the real page URL (even in frame mode); the DOM
        snapshot and validation use the active target, so frames resolve
        correctly.
        """
        target = self._ensure_target()
        url = self._ensure_page().url
        return self._ensure_resolver().resolve(
            target, url, method, description, multi=multi, help=self._help
        )

    # ------------------------------------------------------------------
    # Escape hatch
    # ------------------------------------------------------------------

    @property
    def page(self) -> Page:
        """The underlying Playwright :class:`Page` object.

        Use this to access any Playwright feature not exposed by the fluent
        API. When using the escape hatch outside of the fluent chain, call
        ``_start()`` / ``_stop()`` manually or use ``sync_playwright()``
        directly.

        Examples:
            ::

                # inside fluent chain — page is active during run()
                browser = Browser()
                browser.goto("https://example.com")
                browser.page   # only valid inside a step lambda or after goto()

                # escape hatch — manage lifecycle manually
                from playwright.sync_api import sync_playwright
                browser = Browser(headless=False)
                with sync_playwright() as pw:
                    browser._start(pw)
                    browser.page.goto("https://example.com")
                    browser.page.pdf(path="output.pdf")
                    browser._stop()
        """
        if self._page is None:
            raise RuntimeError(
                "No active page. Use the fluent chain and call .run(), "
                "or manage the lifecycle manually with _start()/_stop()."
            )
        return self._page

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def goto(self, url: str) -> "Browser":
        """Navigate to *url*.

        Args:
            url: The full URL to navigate to.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                Browser().goto("https://example.com").run()
        """
        self._steps.append((
            lambda u: action_goto(self._ensure_page(), u),
            (url,), {},
        ))
        return self

    # ------------------------------------------------------------------
    # Interactions
    # ------------------------------------------------------------------

    def click(self, selector: str, *, optional: bool = False) -> "Browser":
        """Click the first element matching *selector*.

        Args:
            selector: Any valid Playwright selector (CSS, XPath, ``text=``,
                ``role=``, etc.).
            optional: If ``True``, a failure is recorded in
                :attr:`Result.errors` instead of raising.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Required click — raises if not found
                Browser().goto(url).click("#submit").run()

                # Optional click — skips silently if banner absent
                Browser().goto(url).click("#cookie-accept", optional=True).run()
        """
        def step():
            action_click(self._ensure_target(), selector)

        self._steps.append((self._run_step, (step, (), {}, f"click({selector!r})", optional), {}))
        return self

    def type(self, selector: str, text: str, *, optional: bool = False) -> "Browser":
        """Clear *selector* and type *text* into it.

        Args:
            selector: Input element selector.
            text: Text to enter.
            optional: Record failure instead of raising.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                Browser().goto(url).type("#search", "playwright").click("#go").run()
        """
        def step():
            action_type(self._ensure_target(), selector, text)

        self._steps.append((self._run_step, (step, (), {}, f"type({selector!r})", optional), {}))
        return self

    def press_key(self, key: str) -> "Browser":
        """Press a keyboard key on the currently focused element.

        Accepts any key name supported by Playwright (e.g. ``"Enter"``,
        ``"Tab"``, ``"Escape"``, ``"ArrowDown"``).

        Args:
            key: Key name to press.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Submit a search form by pressing Enter
                Browser()
                    .goto("https://www.google.com")
                    .type('input[name="q"]', "playwright python")
                    .press_key("Enter")
                    .wait()
                    .run()
        """
        self._steps.append((
            lambda: self._ensure_page().keyboard.press(key),
            (), {},
        ))
        return self

    def select(self, selector: str, value: str, *, optional: bool = False) -> "Browser":
        """Select option *value* in the ``<select>`` matching *selector*.

        Args:
            selector: The ``<select>`` element selector.
            value: The option to select — matched against its ``value``
                attribute first, then its visible label. If no option matches,
                a ``ValueError`` lists the available options.
            optional: Record failure instead of raising.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                Browser().goto(url).select("#country", "US").run()           # by value
                Browser().goto(url).select("#country", "United States").run()  # by label
        """
        def step():
            action_select(self._ensure_target(), selector, value)

        self._steps.append((self._run_step, (step, (), {}, f"select({selector!r}, {value!r})", optional), {}))
        return self

    def hover(self, selector: str, *, optional: bool = False) -> "Browser":
        """Hover the mouse over the first element matching *selector*.

        Args:
            selector: Element to hover over.
            optional: Record failure instead of raising.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                Browser().goto(url).hover("#menu").click("#submenu-item").run()
        """
        def step():
            action_hover(self._ensure_target(), selector)

        self._steps.append((self._run_step, (step, (), {}, f"hover({selector!r})", optional), {}))
        return self

    # ------------------------------------------------------------------
    # Scroll
    # ------------------------------------------------------------------

    def scroll(self, selector: str | None = None, *, y: int | None = None) -> "Browser":
        """Scroll the page.

        Behaviour depends on the arguments supplied:

        - ``scroll()``            — scroll to the absolute bottom of the page
        - ``scroll("#target")``   — scroll the element into the viewport
        - ``scroll(y=500)``       — scroll down by 500 pixels

        Args:
            selector: CSS/XPath selector of element to scroll into view.
            y: Number of pixels to scroll down.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Trigger infinite-scroll content loading
                Browser().goto(url).scroll().wait().scroll().run()

                # Bring a sticky footer into view
                Browser().goto(url).scroll("#footer").run()

                # Partial scroll
                Browser().goto(url).scroll(y=800).run()
        """
        self._steps.append((
            lambda: action_scroll(self._ensure_target(), selector, y),
            (), {},
        ))
        return self

    # ------------------------------------------------------------------
    # Debug
    # ------------------------------------------------------------------

    def pause(self) -> "Browser":
        """Pause execution and open the Playwright Inspector for interactive debugging.

        Halts the workflow at this point in the chain and opens a visual
        browser inspector where you can:

        - Inspect the current DOM and page state
        - Step through subsequent actions one-by-one
        - Run locator queries interactively
        - Click **Resume** to continue the workflow

        Only meaningful when ``headless=False``. In headless mode, pausing is
        skipped with a warning so that automated runs are not blocked.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Inspect the page after login before continuing
                result = (
                    Browser(headless=False)
                    .goto("https://example.com/login")
                    .type("#email", "user@example.com")
                    .type("#password", "secret")
                    .click("[type=submit]")
                    .pause()          # <-- opens inspector here; click Resume to continue
                    .extract("h1", name="greeting")
                    .run()
                )

                # Pause at multiple checkpoints
                Browser(headless=False)
                    .goto(url)
                    .pause()          # inspect initial page
                    .click("#next")
                    .pause()          # inspect after click
                    .run()
        """
        def step():
            if self._headless:
                logger.warning("pause() called in headless mode — skipping")
                return
            logger.debug("pausing — open Playwright Inspector to resume")
            self._ensure_page().pause()

        self._steps.append((step, (), {}))
        return self

    # ------------------------------------------------------------------
    # Wait
    # ------------------------------------------------------------------

    def wait(self, selector: str | None = None, *, seconds: float | None = None) -> "Browser":
        """Wait for a condition before proceeding.

        - ``wait(selector)``      — wait until the element is visible
        - ``wait(seconds=N)``     — pause for N seconds
        - ``wait()``              — wait for network to go idle (good after
          form submissions or SPA navigation)

        Args:
            selector: Selector to wait for.
            seconds: Fixed number of seconds to pause.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Wait for search results to appear
                Browser().goto(url).click("#search").wait("#results").run()

                # Fixed pause for an animation to complete
                Browser().goto(url).click("#animate").wait(seconds=1).run()

                # Wait for all network activity to settle
                Browser().goto(url).click("[type=submit]").wait().run()
        """
        self._steps.append((
            lambda: action_wait(self._ensure_target(), selector, seconds),
            (), {},
        ))
        return self

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    def extract(
        self,
        selector: str,
        *,
        name: str,
        attr: str | None = None,
        optional: bool = False,
    ) -> "Browser":
        """Extract a single value and store it in the result.

        By default extracts inner text. Use *attr* for any HTML attribute.

        Args:
            selector: Selector for the element to read.
            name: Key used to retrieve the value from
                :class:`~browser_automation.result.Result`.
            attr: Attribute to extract instead of text (e.g. ``"href"``,
                ``"src"``, ``"data-id"``).
            optional: Record failure instead of raising.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                result = (
                    Browser()
                    .goto("https://example.com")
                    .extract("h1", name="title")
                    .extract("a", name="link", attr="href")
                    .run()
                )
                print(result["title"])  # "Example Domain"
                print(result["link"])   # "https://www.iana.org/domains/example"
        """
        def step():
            action_extract(self._ensure_target(), selector, name, attr, self._store)

        self._steps.append((self._run_step, (step, (), {}, f"extract({selector!r}, name={name!r})", optional), {}))
        return self

    def extract_all(
        self,
        selector: str,
        *,
        name: str,
        attr: str | None = None,
    ) -> "Browser":
        """Extract all matching elements and store them as a list.

        Args:
            selector: Selector that may match multiple elements.
            name: Key in the result for the extracted list.
            attr: Attribute to extract per element instead of text.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                result = (
                    Browser()
                    .goto("https://news.ycombinator.com")
                    .extract_all(".athing .titleline > a", name="headlines")
                    .run()
                )
                for headline in result["headlines"]:
                    print(headline)

                # Extract all hrefs
                result = (
                    Browser()
                    .goto("https://example.com")
                    .extract_all("a", name="links", attr="href")
                    .run()
                )
        """
        self._steps.append((
            lambda: action_extract_all(self._ensure_target(), selector, name, attr, self._store),
            (), {},
        ))
        return self

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    def screenshot(self, filename: str | None = None) -> "Browser":
        """Save a screenshot of the current page.

        Args:
            filename: Output file path. If omitted, saves to
                ``screenshot_YYYYMMDD_HHMMSS.png`` in the current directory.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Auto-named checkpoint
                Browser().goto(url).screenshot().click("#next").screenshot().run()

                # Named file
                Browser().goto(url).screenshot("before_login.png").run()
        """
        def step():
            fname = filename
            if fname is None:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                fname = f"screenshot_{ts}.png"
            action_screenshot(self._ensure_page(), fname)

        self._steps.append((step, (), {}))
        return self

    def save_html(self, filename: str | None = None) -> "Browser":
        """Save the full HTML of the current page to a file.

        Captures the live DOM at this point in the chain — includes dynamically
        created content that would not appear in a static ``curl`` of the URL.

        Args:
            filename: Output file path. If omitted, saves to
                ``page_YYYYMMDD_HHMMSS.html`` in the current directory.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Auto-named dump — useful for debugging dynamic content
                Browser().goto(url).wait().save_html().run()

                # Named file
                Browser().goto(url).click("#tab2").wait().save_html("tab2.html").run()

                # Checkpoint before and after an action
                Browser()
                    .goto(url)
                    .save_html("before.html")
                    .click("#load-more")
                    .wait()
                    .save_html("after.html")
                    .run()
        """
        def step():
            fname = filename
            if fname is None:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                fname = f"page_{ts}.html"
            html = self._ensure_target().content()
            with open(fname, "w", encoding="utf-8") as f:
                f.write(html)
            logger.debug("saved HTML to %s (%d bytes)", fname, len(html))

        self._steps.append((step, (), {}))
        return self

    # ------------------------------------------------------------------
    # Conditional / arbitrary mid-workflow logic
    # ------------------------------------------------------------------

    def do(self, fn: Callable[[dict, "Browser"], None]) -> "Browser":
        """Execute arbitrary logic mid-workflow with access to extracted data.

        Solves the if/else problem: ``extract()`` values are stored internally
        and only returned after ``run()``. ``do()`` lets you read those values
        and branch on them **before** the session ends.

        *fn* receives ``(store, browser)`` where ``store`` is the dict of all
        values extracted so far, and ``browser`` is this ``Browser`` instance
        so the full fluent API is available.

        Steps added inside *fn* (e.g. ``b.click(...)``) are captured and
        executed immediately in the same browser session.

        Args:
            fn: Callable that receives ``(store, browser)`` and performs
                conditional actions.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Click different buttons based on extracted value
                result = (
                    Browser()
                    .goto("https://example.com")
                    .extract("#status", name="status")
                    .do(lambda store, b:
                        b.click("#activate") if store["status"] == "inactive"
                        else b.click("#deactivate")
                    )
                    .run()
                )

                # Check a checkbox only if it is unchecked
                Browser()
                    .goto("https://example.com")
                    .extract("#terms", name="checked", attr="checked", optional=True)
                    .do(lambda store, b:
                        b.click("#terms") if not store.get("checked") else None
                    )
                    .run()

                # Multi-step branch
                def handle(store, b):
                    if store["role"] == "admin":
                        b.click("#admin-panel").wait()
                    else:
                        b.click("#user-panel").wait()

                Browser()
                    .goto("https://example.com")
                    .extract(".role-badge", name="role")
                    .do(handle)
                    .extract("h1", name="heading")
                    .run()
        """
        def step():
            steps_before = len(self._steps)
            fn(self._store, self)
            branch_steps = self._steps[steps_before:]
            self._steps = self._steps[:steps_before]
            for branch_fn, args, kwargs in branch_steps:
                branch_fn(*args, **kwargs)

        self._steps.append((step, (), {}))
        return self

    # ------------------------------------------------------------------
    # Iframe
    # ------------------------------------------------------------------

    def frame(self, selector: str) -> "Page":
        """Return the raw Playwright ``Frame`` object for an iframe.

        This is an escape hatch for direct iframe access — the returned frame
        is a Playwright object, not a fluent ``Browser``.

        Note: call this after the page has loaded (i.e. after using
        ``browser.page.goto()`` directly, or inside a deferred step via
        ``if_exists`` / ``each``).

        Args:
            selector: CSS selector of the ``<iframe>`` element.

        Returns:
            A Playwright ``Frame`` object.

        Examples:
            ::

                browser = Browser(headless=False)
                browser.page.goto("https://example.com")
                browser.page.wait_for_load_state("networkidle")

                frame = browser.frame("iframe#content")
                print(frame.content())
                print(frame.locator("h1").inner_text())

                with open("iframe.html", "w") as f:
                    f.write(frame.content())

                browser.run()
        """
        target = self._ensure_target()
        element = target.locator(selector).first.element_handle()
        return element.content_frame()

    def enter_frame(self, selector: str, *, optional: bool = False) -> "Browser":
        """Enter "frame mode": scope all subsequent actions to an iframe.

        Resolves the iframe **once** and keeps it active until the matching
        :meth:`exit_frame`. Every locator-based action that follows (``click``,
        ``type``, ``extract``, ``wait(selector)``, ``each``, …) operates on the
        iframe's document with no per-action frame re-resolution.

        Page-level actions — ``press_key``, ``screenshot``, ``pause``, ``goto``
        — always target the real page, even in frame mode.

        Calls nest: ``enter_frame`` pushes onto an internal stack, so you can
        enter a frame within a frame and ``exit_frame`` unwinds one level at a
        time. Always pair each ``enter_frame`` with an ``exit_frame``; frame
        mode is reset automatically at the end of :meth:`run`.

        Note: the resolved frame reference is held for the lifetime of frame
        mode. A navigation that destroys and recreates the iframe invalidates
        it — exit and re-enter after such a navigation.

        Args:
            selector: CSS selector of the ``<iframe>`` element.
            optional: If ``True`` and the iframe is not found, record a soft
                failure in :attr:`Result.errors` instead of raising. Frame mode
                is *not* entered, but a balancing ``exit_frame`` is still safe.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Several interactions, one frame resolution
                result = (
                    Browser()
                    .goto("https://example.com")
                    .wait("iframe#editor")
                    .enter_frame("iframe#editor")
                    .type("#title", "Hello")
                    .click("#bold")
                    .type("#body", "world")
                    .extract("h1", name="heading")
                    .exit_frame()
                    .extract("title", name="page_title")
                    .run()
                )

                # Nested iframes
                Browser()
                    .goto(url)
                    .enter_frame("iframe#outer")
                    .enter_frame("iframe#inner")
                    .click("#deep-button")
                    .exit_frame()   # back to outer
                    .exit_frame()   # back to page
                    .run()
        """
        def step():
            target = self._ensure_target()
            # Push the current context first so exit_frame() always balances,
            # even if resolution fails under optional=True.
            self._frame_stack.append(self._active_frame)
            try:
                element = target.locator(selector).first.element_handle()
                frame = element.content_frame()
            except Exception as exc:
                msg = f"enter_frame({selector!r}): {exc}"
                if optional:
                    logger.warning("optional enter_frame failed — %s", msg)
                    self._errors.append(msg)
                    return  # leave _active_frame unchanged; stack stays balanced
                self._frame_stack.pop()
                raise
            logger.debug("enter_frame %s", selector)
            self._active_frame = frame

        self._steps.append((step, (), {}))
        return self

    def exit_frame(self) -> "Browser":
        """Exit the current frame, restoring the parent context.

        Pops one level off the frame stack established by :meth:`enter_frame` —
        returning to the outer iframe (if nested) or the main page. Calling
        ``exit_frame`` when not in frame mode logs a warning and is a no-op.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                Browser().goto(url).enter_frame("#f").click("#x").exit_frame().run()
        """
        def step():
            if not self._frame_stack:
                logger.warning("exit_frame() called outside frame mode — ignoring")
                return
            self._active_frame = self._frame_stack.pop()
            logger.debug("exit_frame -> %s", "page" if self._active_frame is None else "frame")

        self._steps.append((step, (), {}))
        return self

    def within_frame(
        self,
        selector: str,
        fn: Callable[["Browser"], None],
        *,
        optional: bool = False,
    ) -> "Browser":
        """Run *fn* with all actions scoped to an iframe.

        Convenience wrapper around :meth:`enter_frame` / :meth:`exit_frame`:
        enters the frame, lets *fn* queue actions against it, then exits. Prefer
        ``enter_frame`` / ``exit_frame`` directly for long sequences or when the
        scoping does not map cleanly onto a single callback.

        Args:
            selector: CSS selector of the ``<iframe>`` element.
            fn: Callable receiving this ``Browser`` instance. All actions
                inside operate on the iframe.
            optional: Record failure instead of raising if the iframe is
                not found.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Extract content from an iframe
                result = (
                    Browser()
                    .goto("https://example.com")
                    .wait("iframe#content")
                    .within_frame("iframe#content", lambda b: (
                        b.extract("h1", name="title")
                         .extract_all(".item", name="items")
                    ))
                    .run()
                )
                print(result["title"])
                print(result["items"])

                # Click and type inside an iframe
                Browser()
                    .goto("https://example.com")
                    .within_frame("iframe#login-frame", lambda b: (
                        b.type("#username", "user")
                         .type("#password", "pass")
                         .click("button[type=submit]")
                    ))
                    .wait()
                    .run()

                # Save iframe HTML
                Browser()
                    .goto("https://example.com")
                    .within_frame("iframe#content", lambda b: b.save_html("iframe.html"))
                    .run()
        """
        self.enter_frame(selector, optional=optional)
        fn(self)
        self.exit_frame()
        return self

    # ------------------------------------------------------------------
    # Control flow
    # ------------------------------------------------------------------

    def if_exists(self, selector: str, fn: Callable[["Browser"], None]) -> "Browser":
        """Execute *fn* only if *selector* is present in the DOM.

        *fn* receives this ``Browser`` instance so the full fluent API is
        available inside the block. Actions taken inside *fn* run immediately
        (not deferred); they share the same result store and error list.

        Args:
            selector: Selector to probe. Checked with a 3-second timeout.
            fn: Callable that receives ``browser`` and performs actions.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Accept a cookie banner only if it appears
                result = (
                    Browser()
                    .goto("https://example.com")
                    .if_exists("#cookie-banner", lambda b: b.click("#accept-all"))
                    .extract("h1", name="title")
                    .run()
                )

                # Nested actions
                Browser().goto(url).if_exists(
                    "#login-required",
                    lambda b: (
                        b.type("#email", "user@example.com")
                        .type("#password", "secret")
                        .click("[type=submit]")
                        .wait()
                    )
                ).run()
        """
        self._steps.append((
            lambda: action_if_exists(self._ensure_target(), selector, fn, self),
            (), {},
        ))
        return self

    def each(self, selector: str, fn: Callable[["Browser", object], None]) -> "Browser":
        """Run *fn* for every element matching *selector*.

        *fn* receives ``(browser, element_locator)`` — the second argument is a
        Playwright ``Locator`` scoped to that individual element.

        Args:
            selector: Selector that may match multiple elements.
            fn: Callable that receives ``(browser, locator)`` and performs
                actions on each element.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Click every "expand" button in a list
                Browser().goto(url).each(".item .expand", lambda b, el: el.click()).run()

                # Extract text from each row using the raw locator
                rows = []
                Browser().goto(url).each(
                    "table tr",
                    lambda b, el: rows.append(el.inner_text())
                ).run()
        """
        self._steps.append((
            lambda: action_each(self._ensure_target(), selector, fn, self),
            (), {},
        ))
        return self

    def repeat(self, n: int, fn: Callable[["Browser"], None]) -> "Browser":
        """Run *fn* exactly *n* times.

        Args:
            n: Number of iterations.
            fn: Callable that receives this ``Browser`` instance.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Click "Load more" 5 times then scrape all results
                result = (
                    Browser()
                    .goto("https://example.com/feed")
                    .repeat(5, lambda b: b.click("#load-more").wait("#spinner", optional=True).wait())
                    .extract_all(".post-title", name="titles")
                    .run()
                )
        """
        self._steps.append((
            lambda: action_repeat(fn, n, self),
            (), {},
        ))
        return self

    def repeat_until(
        self,
        selector: str,
        fn: Callable[["Browser"], None],
        *,
        max_iterations: int = 100,
    ) -> "Browser":
        """Run *fn* repeatedly until *selector* appears in the DOM.

        Checks for *selector* before each iteration. Stops as soon as the
        element is detected. *max_iterations* prevents infinite loops.

        Args:
            selector: Condition selector — loop stops when this is found.
            fn: Callable that receives this ``Browser`` instance and advances
                the workflow one step (e.g. click "Next").
            max_iterations: Safety cap on loop iterations (default 100).

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                # Paginate until the "Disabled" next-button appears
                result = (
                    Browser()
                    .goto("https://example.com/results")
                    .repeat_until(
                        "#next-page[disabled]",
                        lambda b: b.click("#next-page").wait(),
                    )
                    .extract_all(".result-title", name="titles")
                    .run()
                )
        """
        self._steps.append((
            lambda: action_repeat_until(self._ensure_target(), selector, fn, self, max_iterations),
            (), {},
        ))
        return self

    # ------------------------------------------------------------------
    # Agent — LLM-resolved actions (describe the element in natural language)
    # ------------------------------------------------------------------

    def click_agent(self, description: str, *, optional: bool = False) -> "Browser":
        """Click the element matching a natural-language *description*.

        Resolves *description* to an XPath via the cache (or the LLM, when
        ``help=True``) at run time, then clicks it. See :meth:`click`.

        Args:
            description: Natural-language description of the element — usually the
                visible text (``"Add to cart"``) but it can be richer
                (``"the cart icon in the top-right header"``).
            optional: If ``True``, a failure (including resolution failure) is
                recorded in :attr:`Result.errors` instead of raising.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                Browser(help=True).goto(url).click_agent("Log in").run()
        """
        def step():
            xpath = self._resolve_agent("click", description, multi=False)
            action_click(self._ensure_target(), f"xpath={xpath}")

        self._steps.append((self._run_step, (step, (), {}, f"click_agent({description!r})", optional), {}))
        return self

    def type_agent(self, description: str, text: str, *, optional: bool = False) -> "Browser":
        """Type *text* into the element matching *description*.

        See :meth:`type`. The element is resolved by description at run time.

        Examples:
            ::

                Browser(help=True).goto(url).type_agent("the search box", "playwright").run()
        """
        def step():
            xpath = self._resolve_agent("type", description, multi=False)
            action_type(self._ensure_target(), f"xpath={xpath}", text)

        self._steps.append((self._run_step, (step, (), {}, f"type_agent({description!r})", optional), {}))
        return self

    def hover_agent(self, description: str, *, optional: bool = False) -> "Browser":
        """Hover over the element matching *description*. See :meth:`hover`."""
        def step():
            xpath = self._resolve_agent("hover", description, multi=False)
            action_hover(self._ensure_target(), f"xpath={xpath}")

        self._steps.append((self._run_step, (step, (), {}, f"hover_agent({description!r})", optional), {}))
        return self

    def select_agent(self, description: str, value: str, *, optional: bool = False) -> "Browser":
        """Select option *value* in the ``<select>`` matching *description*.

        See :meth:`select`. The ``<select>`` is resolved by description, and
        *value* matches the option's value attribute or visible label — pass the
        on-screen option text (e.g. ``"有休"``) and it just works.
        """
        def step():
            xpath = self._resolve_agent("select", description, multi=False)
            action_select(self._ensure_target(), f"xpath={xpath}", value)

        self._steps.append((self._run_step, (step, (), {}, f"select_agent({description!r}, {value!r})", optional), {}))
        return self

    def extract_agent(
        self,
        description: str,
        *,
        name: str,
        attr: str | None = None,
        optional: bool = False,
    ) -> "Browser":
        """Extract a single value from the element matching *description*.

        See :meth:`extract`. Resolution is single-target (the description must
        identify exactly one element).

        Examples:
            ::

                result = (
                    Browser(help=True)
                    .goto(url)
                    .extract_agent("the page heading", name="title")
                    .run()
                )
        """
        def step():
            xpath = self._resolve_agent("extract", description, multi=False)
            action_extract(self._ensure_target(), f"xpath={xpath}", name, attr, self._store)

        self._steps.append((self._run_step, (step, (), {}, f"extract_agent({description!r}, name={name!r})", optional), {}))
        return self

    def extract_all_agent(
        self,
        description: str,
        *,
        name: str,
        attr: str | None = None,
    ) -> "Browser":
        """Extract all values matching a *description* of a collection.

        See :meth:`extract_all`. Resolution is multi-target — the description
        names a set of elements (e.g. ``"the product titles"``) and the XPath is
        allowed to match many.

        Examples:
            ::

                result = (
                    Browser(help=True)
                    .goto("https://news.ycombinator.com")
                    .extract_all_agent("the story headline links", name="headlines")
                    .run()
                )
        """
        def step():
            xpath = self._resolve_agent("extract_all", description, multi=True)
            action_extract_all(self._ensure_target(), f"xpath={xpath}", name, attr, self._store)

        self._steps.append((step, (), {}))
        return self

    # ------------------------------------------------------------------
    # Terminal
    # ------------------------------------------------------------------

    def run(self) -> Result:
        """Execute all queued actions and return the result.

        Opens the browser, runs every step in order, closes the browser, and
        returns a :class:`~browser_automation.result.Result` containing all
        extracted data and any soft failures.

        Returns:
            :class:`~browser_automation.result.Result`

        Raises:
            Any exception from a required (non-optional) step.

        Examples:
            ::

                result = Browser().goto("https://example.com").extract("h1", name="h1").run()

                if result.ok:
                    print(result["h1"])
                else:
                    print("Soft failures:", result.errors)
        """
        with sync_playwright() as playwright:
            self._start(playwright)
            # Reset frame mode in case a previous run left it dirty.
            self._active_frame = None
            self._frame_stack.clear()
            # Snapshot and clear before iterating — do() appends to self._steps
            # during execution; iterating the live list would cause those steps
            # to run a second time.
            steps_snapshot = list(self._steps)
            self._steps.clear()
            try:
                for step_tuple in steps_snapshot:
                    fn, args, kwargs = step_tuple
                    fn(*args, **kwargs)
            except Exception:
                if self._keep_open_on_error and not self._headless:
                    logger.warning(
                        "keep_open_on_error=True — browser left open for inspection. "
                        "Close it manually when done."
                    )
                    input("Browser paused on error. Press Enter to close it...")
                raise
            finally:
                self._stop()
                self._steps.clear()
                self._active_frame = None
                self._frame_stack.clear()

        result = Result(data=self._store, errors=self._errors)
        self._store = {}
        self._errors = []
        return result
