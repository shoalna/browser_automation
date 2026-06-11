"""
Result object returned by Browser.run().

Examples:
    Basic access::

        result = Browser().goto("https://example.com").extract("h1", name="title").run()

        result["title"]       # "Example Domain"
        result.data           # {"title": "Example Domain"}
        result.ok             # True
        result.errors         # []

    Checking for soft failures::

        result = (
            Browser()
            .goto("https://example.com")
            .click("#maybe-exists", optional=True)
            .extract("h1", name="title")
            .run()
        )

        if not result.ok:
            for err in result.errors:
                print(f"Soft failure: {err}")
"""

from __future__ import annotations

from typing import Any


class Result:
    """Holds extracted data and error information from a completed workflow."""

    def __init__(self, data: dict[str, Any], errors: list[str]) -> None:
        self._data = data
        self._errors = errors

    @property
    def data(self) -> dict[str, Any]:
        """All named extractions as a plain dict."""
        return self._data

    @property
    def errors(self) -> list[str]:
        """Soft failures collected during the workflow (from optional=True actions)."""
        return self._errors

    @property
    def ok(self) -> bool:
        """True if no soft failures were collected."""
        return len(self._errors) == 0

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __repr__(self) -> str:
        status = "ok" if self.ok else f"{len(self._errors)} error(s)"
        return f"Result(data={list(self._data.keys())}, status={status})"
