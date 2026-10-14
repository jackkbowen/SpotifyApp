"""Loading the enriched library and describing what's still being filled in."""

from __future__ import annotations

import json
from pathlib import Path

from .paths import ENRICHED_LIBRARY_PATH


class LibraryMissingError(Exception):
    pass


_cache: dict = {"mtime": None, "data": None}


def load_enriched_library(path: Path = ENRICHED_LIBRARY_PATH) -> dict:
    """Parsed library_enriched.json, re-read only when the file changes."""
    if not path.exists():
        raise LibraryMissingError("No songs in the library yet.")
    mtime = path.stat().st_mtime
    if _cache["mtime"] != mtime:
        with path.open(encoding="utf-8") as f:
            _cache["data"] = json.load(f)
        _cache["mtime"] = mtime
    return _cache["data"]


def staleness_warnings(library: dict) -> list[str]:
    """Plain-language notes for visitors about data still on its way."""
    counts = [
        (library.get("tag_status_counts") or {}).get("pending", 0),
        (library.get("genre_status_counts") or {}).get("pending", 0),
        (library.get("status_counts") or {}).get("pending", 0),
    ]
    waiting = max(counts) if counts else 0
    if not waiting:
        return []
    return [f"{waiting} recently added songs are still being enriched (BPM matches, tags and genres). "
            "They're already usable, and the rest fills in automatically in the background."]
