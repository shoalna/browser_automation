"""
Extraction helpers used internally by Browser action methods.

These functions pull text or attribute values from Playwright locator objects
and are not intended to be called directly by users.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Locator


def extract_value(locator: Locator, attr: str | None) -> str:
    """Return inner text or a specific attribute from the first matching element.

    Args:
        locator: A Playwright Locator narrowed to a single element.
        attr: Attribute name to extract (e.g. "href", "src", "data-id").
              If None, returns inner text.

    Returns:
        The extracted string value.

    Examples:
        >>> # inner text (default)
        >>> extract_value(page.locator("h1"), attr=None)
        "Hello World"

        >>> # href attribute
        >>> extract_value(page.locator("a.link"), attr="href")
        "https://example.com"
    """
    if attr is not None:
        return locator.get_attribute(attr) or ""
    return locator.inner_text()


def extract_all_values(locator: Locator, attr: str | None) -> list[Any]:
    """Return a list of values from all matching elements.

    Args:
        locator: A Playwright Locator that may match multiple elements.
        attr: Attribute name to extract. If None, returns inner text for each.

    Returns:
        A list of extracted string values, one per matched element.

    Examples:
        >>> # all link hrefs on the page
        >>> extract_all_values(page.locator("a"), attr="href")
        ["https://example.com", "https://other.com"]

        >>> # text of every list item
        >>> extract_all_values(page.locator("li"), attr=None)
        ["Item 1", "Item 2", "Item 3"]
    """
    count = locator.count()
    results = []
    for i in range(count):
        el = locator.nth(i)
        if attr is not None:
            results.append(el.get_attribute(attr) or "")
        else:
            results.append(el.inner_text())
    return results
