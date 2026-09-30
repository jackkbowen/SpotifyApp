"""Loading the enriched library and checking it against the latest export."""

from __future__ import annotations

import json
from pathlib import Path

from .paths import ENRICHED_LIBRARY_PATH, LIKED_SONGS_PATH


class LibraryMissingError(Exception):
    pass


_cache: dict = {"mtime": None, "data": None}


def load_enriched_library(path: Path = ENRICHED_LIBRARY_PATH) -> dict:
    """Parsed library_enriched.json, re-read only when the file changes."""
    if not path.exists():
        raise LibraryMissingError(
            f"{path.name} not found. Run `python enrich_library.py` first "
            "(or `python enrich_library.py --no-fetch` to load the library without BPM data)."
        )
    mtime = path.stat().st_mtime
    if _cache["mtime"] != mtime:
        with path.open(encoding="utf-8") as f:
            _cache["data"] = json.load(f)
        _cache["mtime"] = mtime
    return _cache["data"]


def _read_exported_at(path: Path) -> str | None:
    # The export is several MB; exported_at is in the first few lines.
    with path.open(encoding="utf-8") as f:
        head = f.read(4096)
    marker = '"exported_at":'
    start = head.find(marker)
    if start == -1:
        return None
    value = head[start + len(marker):].split(",", 1)[0].strip().strip('"')
    return value or None


def staleness_warnings(library: dict, liked_path: Path = LIKED_SONGS_PATH) -> list[str]:
    warnings = []
    if liked_path.exists():
        latest = _read_exported_at(liked_path)
        used = library.get("source_exported_at")
        # ISO-8601 UTC timestamps from the same exporter compare correctly as strings.
        if latest and used and latest > used:
            warnings.append(
                f"{liked_path.name} (exported {latest[:16].replace('T', ' ')}) is newer than "
                f"the enriched library (built from the {used[:16].replace('T', ' ')} export). "
                "Run `python enrich_library.py` to pick up newly liked songs."
            )
    pending = (library.get("status_counts") or {}).get("pending", 0)
    if pending:
        warnings.append(
            f"{pending} tracks haven't been looked up on GetSongBPM yet. "
            "Run `python enrich_library.py` to enrich them."
        )
    return warnings
