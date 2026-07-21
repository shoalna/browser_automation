"""
Shared base for the library's small JSON-file caches.

Both the ``*_agent`` XPath cache (:class:`agent.AgentCache`) and the scenario
code cache (:class:`codegen.ScenarioCache`) are lazily-loaded JSON files written
through atomically. This base holds that mechanism; subclasses define their own
key shape via ``get``/``set``.
"""

from __future__ import annotations

import json
import os
import tempfile


class AtomicJsonCache:
    """A JSON dict persisted to *path*, loaded lazily and flushed atomically."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict | None = None  # loaded lazily

    def _load(self) -> None:
        if self._data is not None:
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {}

    def _flush(self) -> None:
        """Write the cache atomically: temp file in the same dir, then os.replace."""
        directory = os.path.dirname(os.path.abspath(self._path)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
