"""Saved setlists (data/setlists.json) and CSV/M3U export."""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .paths import SETLISTS_PATH

GETSONGBPM_BACKLINK = "https://getsongbpm.com"

_lock = threading.Lock()


class SetlistNotFound(Exception):
    pass


class InvalidSetlist(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(path: Path) -> dict:
    if not path.exists():
        return {"setlists": {}}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _snapshot(track_ids: list[str], tracks_by_id: dict[str, dict], previous: list[dict]) -> list[dict]:
    """Store enough about each track to still show/export it if it is later
    removed from Liked Songs (and so gone from the library)."""
    old = {entry["track_id"]: entry for entry in previous}
    entries = []
    for track_id in track_ids:
        track = tracks_by_id.get(track_id)
        if track:
            entries.append({
                "track_id": track_id,
                "isrc": track.get("isrc"),
                "track_name": track.get("track_name"),
                "artists": [a.get("name") for a in track.get("artists") or []],
                "duration_ms": track.get("duration_ms"),
                "spotify_url": track.get("spotify_url"),
                "enrichment": {
                    k: (track.get("enrichment") or {}).get(k)
                    for k in ("bpm", "key", "camelot", "danceability", "acousticness")
                } if track.get("enrichment") else None,
            })
        elif track_id in old:
            entries.append(old[track_id])
        else:
            raise InvalidSetlist(f"Unknown track id: {track_id}")
    return entries


def summary(setlist: dict) -> dict:
    return {
        "id": setlist["id"],
        "name": setlist["name"],
        "track_count": len(setlist["tracks"]),
        "updated_at": setlist["updated_at"],
    }


def list_setlists(path: Path = SETLISTS_PATH) -> list[dict]:
    setlists = _read(path)["setlists"].values()
    return sorted((summary(s) for s in setlists), key=lambda s: s["updated_at"], reverse=True)


def get_setlist(setlist_id: str, path: Path = SETLISTS_PATH) -> dict:
    setlist = _read(path)["setlists"].get(setlist_id)
    if not setlist:
        raise SetlistNotFound(setlist_id)
    return setlist


def save_setlist(name: str, track_ids: list[str], tracks_by_id: dict[str, dict],
                 setlist_id: str | None = None, path: Path = SETLISTS_PATH,
                 mood_tags: list[str] | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise InvalidSetlist("A setlist needs a name.")
    with _lock:
        data = _read(path)
        existing = data["setlists"].get(setlist_id) if setlist_id else None
        if setlist_id and not existing:
            raise SetlistNotFound(setlist_id)
        setlist = existing or {"id": uuid.uuid4().hex[:12], "created_at": _now()}
        setlist.update(
            name=name,
            tracks=_snapshot(track_ids, tracks_by_id, (existing or {}).get("tracks", [])),
            # The mood/vibe tags this set was generated from or filtered by.
            mood_tags=list(mood_tags or []),
            updated_at=_now(),
        )
        data["setlists"][setlist["id"]] = setlist
        _write(path, data)
    return setlist


def record_spotify_export(setlist_id: str, playlist_id: str, url: str | None,
                          path: Path = SETLISTS_PATH) -> dict:
    """Remember which Spotify playlist a setlist was exported to, so the next
    export can update it in place."""
    with _lock:
        data = _read(path)
        setlist = data["setlists"].get(setlist_id)
        if not setlist:
            raise SetlistNotFound(setlist_id)
        setlist.update(spotify_playlist_id=playlist_id, spotify_playlist_url=url,
                       spotify_exported_at=_now())
        _write(path, data)
    return setlist


def delete_setlist(setlist_id: str, path: Path = SETLISTS_PATH) -> None:
    with _lock:
        data = _read(path)
        if data["setlists"].pop(setlist_id, None) is None:
            raise SetlistNotFound(setlist_id)
        _write(path, data)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def _rows(setlist: dict, tracks_by_id: dict[str, dict]) -> list[dict]:
    """Setlist entries merged with the library's current enrichment data."""
    rows = []
    for position, entry in enumerate(setlist["tracks"], 1):
        track = tracks_by_id.get(entry["track_id"]) or {}
        # Current library data when the track is still liked, else the snapshot.
        enrichment = (track.get("enrichment") if track else entry.get("enrichment")) or {}
        artists = [a.get("name") for a in track.get("artists") or []] or entry.get("artists") or []
        rows.append({
            "position": position,
            "track_name": track.get("track_name") or entry.get("track_name"),
            "artists": ", ".join(a for a in artists if a),
            "bpm": enrichment.get("bpm"),
            "key": enrichment.get("key"),
            "camelot": enrichment.get("camelot"),
            "danceability": enrichment.get("danceability"),
            "acousticness": enrichment.get("acousticness"),
            "duration_ms": track.get("duration_ms") or entry.get("duration_ms"),
            "isrc": track.get("isrc") or entry.get("isrc"),
            "spotify_url": track.get("spotify_url") or entry.get("spotify_url"),
            "has_bpm_key_data": bool(enrichment),
        })
    return rows


def _duration(ms) -> str:
    if not ms:
        return ""
    minutes, seconds = divmod(round(ms / 1000), 60)
    return f"{minutes}:{seconds:02d}"


def export_csv(setlist: dict, tracks_by_id: dict[str, dict]) -> str:
    columns = ["position", "track_name", "artists", "bpm", "key", "camelot",
               "danceability", "acousticness", "duration", "isrc", "spotify_url",
               "bpm_key_source"]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in _rows(setlist, tracks_by_id):
        writer.writerow({
            **row,
            "duration": _duration(row["duration_ms"]),
            # GetSongBPM's terms require a backlink wherever their data is shared.
            "bpm_key_source": GETSONGBPM_BACKLINK if row["has_bpm_key_data"] else "",
        })
    # BOM so Excel reads UTF-8 correctly.
    return "﻿" + out.getvalue()


def export_m3u(setlist: dict, tracks_by_id: dict[str, dict]) -> str:
    """Extended M3U. Entries point at Spotify URLs, not audio files, so DJ
    software will list the tracks but can't play them until you relink them
    to local files."""
    lines = [
        "#EXTM3U",
        f"#PLAYLIST:{setlist['name']}",
        f"# BPM/key data: GetSongBPM {GETSONGBPM_BACKLINK}",
    ]
    for row in _rows(setlist, tracks_by_id):
        seconds = round(row["duration_ms"] / 1000) if row["duration_ms"] else -1
        bpm = f"{row['bpm']:g}" if row["bpm"] else "?"
        key = f"{row['camelot']} ({row['key']})" if row["camelot"] else (row["key"] or "?")
        lines.append(f"# {row['position']}. BPM {bpm} | Key {key}")
        lines.append(f"#EXTINF:{seconds},{row['artists']} - {row['track_name']}")
        lines.append(row["spotify_url"] or "")
    return "\n".join(lines) + "\n"
