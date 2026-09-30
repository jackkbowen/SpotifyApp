"""Where every file the enrichment script and the app read or write lives."""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_DIR / ".env"

# Phase 1 output (export_liked_songs.py).
LIKED_SONGS_PATH = PROJECT_DIR / "output" / "liked_songs.json"

# Phase 2 data. Everything here is local, personal, and git-ignored.
# SETLIST_DATA_DIR can point elsewhere (e.g. for testing with a scratch copy).
DATA_DIR = Path(os.environ.get("SETLIST_DATA_DIR") or PROJECT_DIR / "data")
BPM_CACHE_PATH = DATA_DIR / "bpm_cache.json"
UNMATCHED_PATH = DATA_DIR / "unmatched.json"
ENRICHED_LIBRARY_PATH = DATA_DIR / "library_enriched.json"
SETLISTS_PATH = DATA_DIR / "setlists.json"
