"""SQLite storage for the shared library and for playlists.

  tracks     every song anyone has imported (one row per Spotify track ID).
             Only song information is kept: no uploader identity, no personal
             "added at" dates. `first_added` is when it joined this library.
  imports    one row per upload, counts only (for the import page's totals).
  playlists  anonymous playlists. Whoever creates one gets a random edit key;
             only its SHA-256 hash is stored, and every change must present
             the key. Anyone with the playlist's ID can view it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import paths

MAX_PLAYLIST_TRACKS = 500
MAX_NAME = 120

_lock = threading.Lock()  # serialises writes inside this process

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    track_id     TEXT PRIMARY KEY,
    data         TEXT NOT NULL,
    first_added  TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    import_count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS imports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    rows       INTEGER NOT NULL,
    valid      INTEGER NOT NULL,
    new_tracks INTEGER NOT NULL,
    updated    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS playlists (
    id         TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    name       TEXT NOT NULL,
    tracks     TEXT NOT NULL,
    mood_tags  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class PlaylistNotFound(Exception):
    pass


class Forbidden(Exception):
    """Missing or wrong edit key."""


class InvalidPlaylist(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or paths.DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def _db():
    """A connection that commits on success, rolls back on error, and always closes."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Shared library
# --------------------------------------------------------------------------- #

def _merge(old: dict, new: dict) -> dict:
    """Keep what we had, fill gaps from the new upload. Uploads can't erase data."""
    merged = dict(old)
    for key, value in new.items():
        if value in (None, "", [], {}):
            continue
        if key in ("csv_genres",):
            merged[key] = list(dict.fromkeys((old.get(key) or []) + value))[:15]
        elif key in ("album", "csv_features") and isinstance(old.get(key), dict):
            merged[key] = {**value, **{k: v for k, v in old[key].items() if v is not None}}
        elif old.get(key) in (None, "", [], {}):
            merged[key] = value
    return merged


def import_tracks(tracks: list[dict]) -> dict:
    """Add/merge parsed tracks into the library. Returns {new, updated, unchanged}."""
    stamp = now_iso()
    counts = {"new": 0, "updated": 0, "unchanged": 0}
    with _lock, _db() as conn:
        existing = {}
        ids = [t["track_id"] for t in tracks]
        for i in range(0, len(ids), 500):  # stay well under SQLite's parameter limit
            chunk = ids[i:i + 500]
            existing.update({row["track_id"]: row["data"] for row in conn.execute(
                f"SELECT track_id, data FROM tracks WHERE track_id IN ({','.join('?' * len(chunk))})", chunk)})
        for t in tracks:
            if t["track_id"] not in existing:
                conn.execute("INSERT INTO tracks (track_id, data, first_added, updated_at) VALUES (?, ?, ?, ?)",
                             (t["track_id"], json.dumps(t, ensure_ascii=False), stamp, stamp))
                counts["new"] += 1
                continue
            old = json.loads(existing[t["track_id"]])
            merged = _merge(old, t)
            if merged != old:
                conn.execute("UPDATE tracks SET data = ?, updated_at = ?, import_count = import_count + 1 WHERE track_id = ?",
                             (json.dumps(merged, ensure_ascii=False), stamp, t["track_id"]))
                counts["updated"] += 1
            else:
                conn.execute("UPDATE tracks SET import_count = import_count + 1 WHERE track_id = ?", (t["track_id"],))
                counts["unchanged"] += 1
        if counts["new"] or counts["updated"]:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('library_changed_at', ?)", (stamp,))
    return counts


def record_import(report: dict, counts: dict) -> None:
    with _lock, _db() as conn:
        conn.execute("INSERT INTO imports (created_at, rows, valid, new_tracks, updated) VALUES (?, ?, ?, ?, ?)",
                     (now_iso(), report["rows"], report["valid"], counts["new"], counts["updated"]))


def library_counts() -> dict:
    with _db() as conn:
        tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        imports = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
        changed = conn.execute("SELECT value FROM meta WHERE key = 'library_changed_at'").fetchone()
    return {"tracks": tracks, "imports": imports, "changed_at": changed[0] if changed else None}


def write_pipeline_source(path: Path | None = None) -> int:
    """Write the whole library in the export format the enrichment pipeline
    reads (as output/liked_songs.json used to be). Returns the track count."""
    path = path or paths.LIBRARY_SOURCE_PATH
    with _db() as conn:
        rows = conn.execute("SELECT data, first_added FROM tracks ORDER BY first_added DESC, track_id").fetchall()
        changed = conn.execute("SELECT value FROM meta WHERE key = 'library_changed_at'").fetchone()
    tracks = []
    for row in rows:
        t = json.loads(row["data"])
        t["added_at"] = row["first_added"]  # when it joined this shared library
        tracks.append(t)
    payload = {
        "exported_at": changed[0] if changed else now_iso(),
        "source": "Shared library (Exportify imports)",
        "track_count": len(tracks),
        "tracks": tracks,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return len(tracks)


# --------------------------------------------------------------------------- #
# Playlists
# --------------------------------------------------------------------------- #

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row_to_playlist(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "tracks": json.loads(row["tracks"]),
        "mood_tags": json.loads(row["mood_tags"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _validate(name: str, entries: list[dict], mood_tags: list[str]) -> tuple[str, list[str]]:
    name = (name or "").strip()[:MAX_NAME]
    if not name:
        raise InvalidPlaylist("A playlist needs a name.")
    if len(entries) > MAX_PLAYLIST_TRACKS:
        raise InvalidPlaylist(f"A playlist can have at most {MAX_PLAYLIST_TRACKS} tracks.")
    tags = [str(t)[:60] for t in mood_tags][:10]
    return name, tags


def get_playlist(playlist_id: str) -> dict:
    with _db() as conn:
        row = conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    if not row:
        raise PlaylistNotFound(playlist_id)
    return _row_to_playlist(row)


def get_summaries(ids: list[str]) -> list[dict]:
    ids = [i for i in ids if isinstance(i, str)][:200]
    if not ids:
        return []
    with _db() as conn:
        rows = conn.execute(f"SELECT id, name, tracks, updated_at FROM playlists WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return sorted(({"id": r["id"], "name": r["name"], "track_count": len(json.loads(r["tracks"])), "updated_at": r["updated_at"]}
                   for r in rows), key=lambda s: s["updated_at"], reverse=True)


def create_playlist(name: str, entries: list[dict], mood_tags: list[str], created_at: str | None = None) -> tuple[dict, str]:
    """Returns (playlist, edit_key). The key is shown once and never stored."""
    name, tags = _validate(name, entries, mood_tags)
    token = secrets.token_urlsafe(24)
    playlist_id = secrets.token_urlsafe(9)
    stamp = now_iso()
    with _lock, _db() as conn:
        conn.execute("INSERT INTO playlists VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (playlist_id, _hash(token), name, json.dumps(entries, ensure_ascii=False),
                      json.dumps(tags, ensure_ascii=False), created_at or stamp, stamp))
    return get_playlist(playlist_id), token


def _check(conn, playlist_id: str, token: str | None):
    row = conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    if not row:
        raise PlaylistNotFound(playlist_id)
    if not token or not hmac.compare_digest(row["token_hash"], _hash(token)):
        raise Forbidden()
    return row


def update_playlist(playlist_id: str, token: str | None, name: str, entries: list[dict], mood_tags: list[str]) -> dict:
    name, tags = _validate(name, entries, mood_tags)
    with _lock, _db() as conn:
        _check(conn, playlist_id, token)
        conn.execute("UPDATE playlists SET name = ?, tracks = ?, mood_tags = ?, updated_at = ? WHERE id = ?",
                     (name, json.dumps(entries, ensure_ascii=False), json.dumps(tags, ensure_ascii=False), now_iso(), playlist_id))
    return get_playlist(playlist_id)


def delete_playlist(playlist_id: str, token: str | None) -> None:
    with _lock, _db() as conn:
        _check(conn, playlist_id, token)
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))


def migrate_setlists_json() -> list[str]:
    """One-time: move pre-SQLite setlists into the database. Each gets an edit
    link, written to data/migrated_playlist_links.txt. Returns those lines."""
    src = paths.SETLISTS_PATH
    if not src.exists():
        return []
    with _db() as conn:
        done = conn.execute("SELECT value FROM meta WHERE key = 'setlists_migrated'").fetchone()
    if done:
        return []
    data = json.loads(src.read_text(encoding="utf-8"))
    lines = []
    for old in (data.get("setlists") or {}).values():
        entries = [{k: v for k, v in e.items()} for e in old.get("tracks", [])][:MAX_PLAYLIST_TRACKS]
        playlist, token = create_playlist(old.get("name") or "Untitled", entries, old.get("mood_tags") or [], old.get("created_at"))
        lines.append(f"{playlist['name']}: /?playlist={playlist['id']}&key={token}")
    with _lock, _db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('setlists_migrated', ?)", (now_iso(),))
    if lines:
        paths.MIGRATED_LINKS_PATH.write_text(
            "Edit links for your playlists from before the shared library (keep these private).\n"
            "Open each one on http://127.0.0.1:8765 to keep editing it in that browser.\n\n" + "\n".join(lines) + "\n",
            encoding="utf-8")
    return lines
