"""
Action execution layer — thin wrappers around Playwright page calls.

All functions accept a Playwright ``Page`` object as their first argument and
operate on it directly. They are called by ``Browser`` and are not part of the
public API, but their signatures are documented for contributors.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from .extractors import extract_all_values, extract_value

if TYPE_CHECKING:
    pass

logger = logging.getLogger("browser_automation")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

def action_goto(page: Page, url: str) -> None:
    """Navigate to a URL and wait for the page to load.

    Examples:
        >>> action_goto(page, "https://example.com")
    """
    logger.debug("goto %s", url)
    page.goto(url, wait_until="domcontentloaded")


# ---------------------------------------------------------------------------
# Interactions
# ---------------------------------------------------------------------------

def action_click(page: Page, selector: str) -> None:
    """Click the first element matching *selector*.

    Examples:
        >>> action_click(page, "#submit-button")
        >>> action_click(page, "text=Login")
    """
    logger.debug("click %s", selector)
    page.locator(selector).first.click()


def action_type(page: Page, selector: str, text: str) -> None:
    """Clear and fill *selector* with *text*.

    Examples:
        >>> action_type(page, "#search", "playwright python")
        >>> action_type(page, "input[name='email']", "user@example.com")
    """
    logger.debug("type %s <- %r", selector, text)
    page.locator(selector).first.fill(text)


def action_select(page: Page, selector: str, value: str) -> None:
    """Select an option in a ``<select>`` element, by value or visible label.

    Tries the option's ``value`` attribute first (a short attempt so the
    fallback is fast), then its visible label — so callers can pass either
    ``"US"`` (value) or ``"United States"`` (label). The label fallback is what
    makes ``select_agent`` work when you describe the option by its on-screen
    text.

    Examples:
        >>> action_select(page, "#country", "US")            # by value
        >>> action_select(page, "select[name='size']", "Large")  # by label
    """
    logger.debug("select %s <- %r", selector, value)
    locator = page.locator(selector).first
    try:
        locator.select_option(value, timeout=3000)
        return
    except PlaywrightTimeoutError:
        pass
    try:
        locator.select_option(label=value, timeout=3000)
        return
    except PlaywrightTimeoutError:
        pass
    # Neither value nor label matched — surface the real options so the caller
    # can see exactly what to pass (instead of an opaque timeout).
    try:
        options = locator.evaluate(
            "el => Array.from(el.options).map(o =>"
            " ({value: o.value, label: (o.label || o.textContent || '').trim()}))"
        )
    except Exception:
        options = []
    available = ", ".join(f"{o['label']!r} (value={o['value']!r})" for o in options) or "<none>"
    raise ValueError(f"select: no option matching {value!r}. Available options: {available}")


def action_hover(page: Page, selector: str) -> None:
    """Hover over the first element matching *selector*.

    Examples:
        >>> action_hover(page, "#dropdown-trigger")
    """
    logger.debug("hover %s", selector)
    page.locator(selector).first.hover()


# ---------------------------------------------------------------------------
# Scroll
# ---------------------------------------------------------------------------

def action_scroll(page: Page, selector: str | None, y: int | None) -> None:
    """Scroll the page.

    Behaviour depends on arguments:

    - ``scroll()``           — scroll to the bottom of the page
    - ``scroll("#target")``  — scroll the element into view
    - ``scroll(y=500)``      — scroll down by *y* pixels

    Examples:
        >>> action_scroll(page, None, None)          # to bottom
        >>> action_scroll(page, "#lazy-section", None)  # to element
        >>> action_scroll(page, None, 800)           # by pixels
    """
    if selector is not None:
        logger.debug("scroll to element %s", selector)
        page.locator(selector).first.scroll_into_view_if_needed()
    elif y is not None:
        logger.debug("scroll y=%d", y)
        page.evaluate(f"window.scrollBy(0, {y})")
    else:
        logger.debug("scroll to bottom")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")


# ---------------------------------------------------------------------------
# Wait
# ---------------------------------------------------------------------------

def action_wait(page: Page, selector: str | None, seconds: float | None) -> None:
    """Wait for a condition.

    Behaviour depends on arguments:

    - ``wait(selector="#el")``   — wait until element is visible
    - ``wait(seconds=2)``        — sleep for a fixed duration
    - ``wait()``                 — wait for network to go idle

    Examples:
        >>> action_wait(page, "#results", None)       # wait for element
        >>> action_wait(page, None, 1.5)              # fixed pause
        >>> action_wait(page, None, None)             # network idle
    """
    if selector is not None:
        logger.debug("wait for selector %s", selector)
        page.locator(selector).first.wait_for(state="visible")
    elif seconds is not None:
        logger.debug("wait %.2fs", seconds)
        page.wait_for_timeout(seconds * 1000)
    else:
        logger.debug("wait for network idle")
        page.wait_for_load_state("networkidle")


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------

def action_extract(
    page: Page,
    selector: str,
    name: str,
    attr: str | None,
    store: dict,
) -> None:
    """Extract a single value and store it under *name*.

    Examples:
        >>> store = {}
        >>> action_extract(page, "h1", "title", None, store)
        >>> store["title"]
        "Example Domain"

        >>> action_extract(page, "a.canonical", "link", "href", store)
        >>> store["link"]
        "https://example.com"
    """
    logger.debug("extract %s -> %r (attr=%s)", selector, name, attr)
    locator = page.locator(selector).first
    store[name] = extract_value(locator, attr)


def action_extract_all(
    page: Page,
    selector: str,
    name: str,
    attr: str | None,
    store: dict,
) -> None:
    """Extract all matching values and store them as a list under *name*.

    Examples:
        >>> store = {}
        >>> action_extract_all(page, "li.result", "results", None, store)
        >>> store["results"]
        ["Result 1", "Result 2", "Result 3"]

        >>> action_extract_all(page, "a", "links", "href", store)
        >>> store["links"]
        ["https://a.com", "https://b.com"]
    """
    logger.debug("extract_all %s -> %r (attr=%s)", selector, name, attr)
    locator = page.locator(selector)
    store[name] = extract_all_values(locator, attr)


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------

def action_screenshot(page: Page, filename: str) -> None:
    """Save a screenshot of the current page to *filename*.

    Examples:
        >>> action_screenshot(page, "screenshot_20240101_120000.png")
    """
    logger.debug("screenshot -> %s", filename)
    page.screenshot(path=filename)


# ---------------------------------------------------------------------------
# Control flow helpers
# ---------------------------------------------------------------------------

def action_if_exists(
    page: Page,
    selector: str,
    fn: Callable,
    browser_instance,
) -> None:
    """Run *fn* only if *selector* is present in the DOM.

    *fn* receives the ``Browser`` instance so the full fluent API is available.

    Examples:
        >>> action_if_exists(page, "#cookie-banner", lambda b: b.click("#accept"), browser)
    """
    logger.debug("if_exists %s", selector)
    try:
        locator = page.locator(selector)
        locator.first.wait_for(state="attached", timeout=3000)
        logger.debug("if_exists %s -> found, running block", selector)
        fn(browser_instance)
    except PlaywrightTimeoutError:
        logger.debug("if_exists %s -> not found, skipping", selector)


def action_each(
    page: Page,
    selector: str,
    fn: Callable,
    browser_instance,
) -> None:
    """Run *fn* for every element matching *selector*.

    *fn* receives ``(browser_instance, element_locator)`` — use the locator as
    a selector string in subsequent calls.

    Examples:
        >>> store = {}
        >>> action_each(
        ...     page, ".product",
        ...     lambda b, el: b.extract(el, name="products"),
        ...     browser
        ... )
    """
    logger.debug("each %s", selector)
    locator = page.locator(selector)
    count = locator.count()
    logger.debug("each %s -> %d elements", selector, count)
    for i in range(count):
        el = locator.nth(i)
        fn(browser_instance, el)


def action_repeat(fn: Callable, n: int, browser_instance) -> None:
    """Run *fn* exactly *n* times.

    Examples:
        >>> action_repeat(lambda b: b.click("#load-more").wait(), 5, browser)
    """
    logger.debug("repeat x%d", n)
    for i in range(n):
        logger.debug("repeat iteration %d/%d", i + 1, n)
        fn(browser_instance)


def action_repeat_until(
    page: Page,
    selector: str,
    fn: Callable,
    browser_instance,
    max_iterations: int = 100,
) -> None:
    """Run *fn* repeatedly until *selector* appears in the DOM.

    *max_iterations* guards against infinite loops (default 100).

    Examples:
        >>> # Keep clicking "Next" until the "last-page" marker appears
        >>> action_repeat_until(
        ...     page, "#last-page",
        ...     lambda b: b.click("#next-page").wait(),
        ...     browser
        ... )
    """
    logger.debug("repeat_until %s appears", selector)
    for i in range(max_iterations):
        try:
            page.locator(selector).first.wait_for(state="attached", timeout=500)
            logger.debug("repeat_until %s -> found after %d iterations", selector, i + 1)
            return
        except PlaywrightTimeoutError:
            pass
        logger.debug("repeat_until iteration %d", i + 1)
        fn(browser_instance)
    logger.warning("repeat_until reached max_iterations=%d without finding %s", max_iterations, selector)
