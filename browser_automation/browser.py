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

        if verbose:
            logging.getLogger("browser_automation").setLevel(logging.DEBUG)

        # Deferred Playwright objects — created lazily on first use
        self._playwright: Playwright | None = None
        self._pw_browser: PlaywrightBrowser | None = None
        self._page: Page | None = None

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
        """Lazily start Playwright and return the active page."""
        if self._page is not None:
            return self._page

        self._playwright = sync_playwright().start()
        browser_launcher = getattr(self._playwright, self._browser_type)

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

        context = self._pw_browser.new_context(**context_kwargs)
        self._page = context.new_page()
        return self._page

    def _teardown(self) -> None:
        """Save session state (if configured) and close Playwright."""
        try:
            if self._page:
                if self._state_file:
                    self._page.context.storage_state(path=self._state_file)
                    logger.debug("saved session state to %s", self._state_file)
                context = self._page.context
                self._page.close()
                context.close()
        except Exception as exc:
            logger.debug("error closing page/context: %s", exc)

        try:
            if self._pw_browser:
                self._pw_browser.close()
        except Exception as exc:
            logger.debug("error closing browser: %s", exc)

        try:
            if self._playwright:
                self._playwright.stop()
        except Exception as exc:
            logger.debug("error stopping playwright: %s", exc)

        self._page = None
        self._pw_browser = None
        self._playwright = None

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
    # Escape hatch
    # ------------------------------------------------------------------

    @property
    def page(self) -> Page:
        """The underlying Playwright :class:`Page` object.

        Use this to access any Playwright feature not exposed by the fluent
        API.

        Examples:
            ::

                browser = Browser()
                browser.goto("https://example.com")
                browser.page.pdf(path="output.pdf")
                browser.run()
        """
        return self._ensure_page()

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
            action_click(self._ensure_page(), selector)

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
            action_type(self._ensure_page(), selector, text)

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
            value: The ``value`` attribute of the ``<option>`` to select.
            optional: Record failure instead of raising.

        Returns:
            ``self`` for chaining.

        Examples:
            ::

                Browser().goto(url).select("#country", "US").run()
        """
        def step():
            action_select(self._ensure_page(), selector, value)

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
            action_hover(self._ensure_page(), selector)

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
            lambda: action_scroll(self._ensure_page(), selector, y),
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
            lambda: action_wait(self._ensure_page(), selector, seconds),
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
            action_extract(self._ensure_page(), selector, name, attr, self._store)

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
            lambda: action_extract_all(self._ensure_page(), selector, name, attr, self._store),
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
            html = self._ensure_page().content()
            with open(fname, "w", encoding="utf-8") as f:
                f.write(html)
            logger.debug("saved HTML to %s (%d bytes)", fname, len(html))

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
        page = self._ensure_page()
        element = page.locator(selector).first.element_handle()
        return element.content_frame()

    def within_frame(
        self,
        selector: str,
        fn: Callable[["Browser"], None],
        *,
        optional: bool = False,
    ) -> "Browser":
        """Run *fn* with all actions scoped to an iframe.

        Temporarily redirects every action inside *fn* to operate on the
        iframe's document instead of the main page. The iframe context is
        restored after *fn* returns.

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
        def step():
            page = self._ensure_page()
            try:
                element = page.locator(selector).first.element_handle()
                frame = element.content_frame()
            except Exception as exc:
                msg = f"within_frame({selector!r}): {exc}"
                if optional:
                    logger.warning("optional within_frame failed — %s", msg)
                    self._errors.append(msg)
                    return
                raise

            # fn(self) adds deferred steps to self._steps — capture them,
            # remove from the main queue, then execute immediately inside
            # the frame context before restoring the original page.
            steps_before = len(self._steps)
            fn(self)
            frame_steps = self._steps[steps_before:]
            self._steps = self._steps[:steps_before]

            original_page = self._page
            self._page = frame  # type: ignore[assignment]
            try:
                for fn_step, args, kwargs in frame_steps:
                    fn_step(*args, **kwargs)
            finally:
                self._page = original_page

        self._steps.append((step, (), {}))
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
            lambda: action_if_exists(self._ensure_page(), selector, fn, self),
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
            lambda: action_each(self._ensure_page(), selector, fn, self),
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
            lambda: action_repeat_until(self._ensure_page(), selector, fn, self, max_iterations),
            (), {},
        ))
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
        try:
            for step_tuple in self._steps:
                fn, args, kwargs = step_tuple
                fn(*args, **kwargs)
        except Exception:
            if self._keep_open_on_error and not self._headless:
                logger.warning(
                    "keep_open_on_error=True — browser left open for inspection. "
                    "Close it manually when done."
                )
                input("Browser paused on error. Press Enter to close it...")
            self._teardown()
            raise
        else:
            self._teardown()
        finally:
            self._steps.clear()

        result = Result(data=self._store, errors=self._errors)
        self._store = {}
        self._errors = []
        return result
