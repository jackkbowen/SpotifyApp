"""Playlist snapshots and CSV/M3U export (storage lives in store.py)."""

from __future__ import annotations

import csv
import io
import re

GETSONGBPM_BACKLINK = "https://getsongbpm.com"

_FORMULA_START = re.compile(r"^[=+\-@\t\r]")


class InvalidSetlist(ValueError):
    pass


def _cell(value):
    """Song names come from public uploads: stop a crafted name like
    '=HYPERLINK(...)' running as a formula when the CSV opens in a spreadsheet."""
    if isinstance(value, str) and _FORMULA_START.match(value):
        return "'" + value
    return value


def _snapshot(track_ids: list[str], tracks_by_id: dict[str, dict], previous: list[dict]) -> list[dict]:
    """Store enough about each track to still show/export it if it is later
    missing from the library."""
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
            **{k: _cell(v) for k, v in row.items()},
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
