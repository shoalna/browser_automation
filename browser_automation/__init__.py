"""
browser_automation
==================

A fluent, ergonomic browser automation library built on Playwright.

Quickstart
----------
::

    from browser_automation import Browser

    result = (
        Browser()
        .goto("https://news.ycombinator.com")
        .extract_all(".athing .titleline > a", name="headlines")
        .run()
    )

    for headline in result["headlines"]:
        print(headline)

See :class:`~browser_automation.browser.Browser` for the full API.
"""

from .browser import Browser
from .result import Result

__all__ = ["Browser", "Result"]
__version__ = "0.2.0"
