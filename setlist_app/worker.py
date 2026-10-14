"""Keeps the shared library's derived data (library_enriched.json) current.

After an import:
  1. rebuild() runs straight away: the library is written out and rebuilt from
     the caches (no API calls), so new songs appear within seconds using the
     data in their CSV.
  2. The background thread then runs the full enrichment pipeline (GetSongBPM,
     Last.fm, ReccoBeats, Deezer + MusicBrainz) in fetch-only mode and
     rebuilds again when it finishes. That can take a long time for big
     imports (MusicBrainz allows 1 request/second); imports that arrive
     meanwhile are picked up by the next round.

Only one rebuild runs at a time (build lock). Don't run enrich_library.py by
hand while the app is enriching: both would write the same cache files.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback

from . import store
from .paths import LIBRARY_SOURCE_PATH, PROJECT_DIR

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

ERROR_PAUSE_SECONDS = 30


class Worker:
    def __init__(self) -> None:
        self.enabled = False
        self._wake = threading.Event()
        self._dirty = False
        self._build_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state = {"phase": "idle", "enabled": False, "started_at": None, "finished_at": None,
                      "last_error": None, "rounds": 0}

    def start(self, enabled: bool = True) -> None:
        """enabled=False: keep the quick rebuilds, but never call external APIs."""
        self.enabled = self.state["enabled"] = enabled
        if enabled and not self._thread:
            self._thread = threading.Thread(target=self._run, name="enrichment", daemon=True)
            self._thread.start()

    def rebuild(self) -> None:
        """Write the library out and rebuild library_enriched.json from caches."""
        import enrich_library
        with self._build_lock:
            store.write_pipeline_source()
            code = enrich_library.main(["--input", str(LIBRARY_SOURCE_PATH), "--no-fetch"])
            if code not in (0, None):
                raise RuntimeError(f"rebuild failed (exit {code})")

    def notify(self) -> None:
        """New songs arrived: enrich them in the background."""
        if not self.enabled:
            return
        self._dirty = True
        self._wake.set()

    def _run(self) -> None:
        import enrich_library
        while True:
            self._wake.wait()
            self._wake.clear()
            while self._dirty:
                self._dirty = False
                self.state.update(phase="enriching", started_at=store.now_iso(), last_error=None)
                try:
                    with self._build_lock:
                        store.write_pipeline_source()
                    # Fetch only; a stop (e.g. a rate limit) still keeps what was fetched.
                    enrich_library.main(["--input", str(LIBRARY_SOURCE_PATH), "--skip-build"])
                    self.state["phase"] = "rebuilding"
                    self.rebuild()
                except Exception as error:  # keep the thread alive; surface the problem on /api/status
                    self.state["last_error"] = f"{error.__class__.__name__}: {error}"
                    traceback.print_exc()
                    self.state["phase"] = "paused after error"
                    time.sleep(ERROR_PAUSE_SECONDS)   # the next import (or a restart) tries again
                finally:
                    self.state.update(phase="idle", finished_at=store.now_iso())
                    self.state["rounds"] += 1


worker = Worker()
