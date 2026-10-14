"""Read an Exportify CSV (https://exportify.net) into library tracks.

Exportify exports a Spotify playlist (or Liked Songs) as CSV, including
Spotify's audio features (tempo, key, energy, valence...) and artist genres.
Uploads come from the public, so everything is validated and capped, and
personal columns are dropped on purpose: "Added By" (the uploader's Spotify
user ID) and "Added At" (when they saved the song) are never read.

Both the current Exportify layout and the older one are accepted; columns are
matched by name, not position.
"""

from __future__ import annotations

import csv
import io
import re

MAX_ROWS = 20_000
MAX_TEXT = 300          # longest name/title kept, in characters
MAX_GENRES = 15
MAX_GENRE_LEN = 60

_TRACK_ID = re.compile(r"(?:spotify:track:|open\.spotify\.com/track/)([A-Za-z0-9]{22})")
_BARE_ID = re.compile(r"^[A-Za-z0-9]{22}$")
_ARTIST_ID = re.compile(r"(?:spotify:artist:|open\.spotify\.com/artist/)([A-Za-z0-9]{22})")
_ISRC = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")

# Each field -> the column names Exportify has used for it.
COLUMNS = {
    "uri": ["Track URI", "Spotify URI"],
    "id": ["Track ID", "Spotify ID"],
    "name": ["Track Name"],
    "artists": ["Artist Name(s)", "Artist Names"],
    "artist_uris": ["Artist URI(s)", "Artist URIs"],
    "album": ["Album Name"],
    "release_date": ["Release Date", "Album Release Date"],
    "duration_ms": ["Duration (ms)", "Track Duration (ms)"],
    "explicit": ["Explicit"],
    "popularity": ["Popularity"],
    "isrc": ["ISRC"],
    "genres": ["Genres", "Artist Genres"],
    "label": ["Record Label", "Label"],
    "danceability": ["Danceability"],
    "energy": ["Energy"],
    "key": ["Key"],
    "loudness": ["Loudness"],
    "mode": ["Mode"],
    "speechiness": ["Speechiness"],
    "acousticness": ["Acousticness"],
    "instrumentalness": ["Instrumentalness"],
    "liveness": ["Liveness"],
    "valence": ["Valence"],
    "tempo": ["Tempo"],
    "time_signature": ["Time Signature"],
}
UNIT_FEATURES = ("danceability", "energy", "speechiness", "acousticness", "instrumentalness", "liveness", "valence")


class ExportifyError(ValueError):
    """The file can't be used at all (not a CSV, no track column...)."""


def _text(value, limit=MAX_TEXT) -> str:
    # No control characters or line breaks: names end up in M3U lines and headers.
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value or "").strip()
    return re.sub(r"\s{2,}", " ", value)[:limit]


def _number(value, lo=None, hi=None):
    try:
        x = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        return None
    return x


def _track_id(row: dict) -> str | None:
    for raw in (row.get("uri"), row.get("id")):
        raw = (raw or "").strip()
        m = _TRACK_ID.search(raw)
        if m:
            return m.group(1)
        if _BARE_ID.match(raw):
            return raw
    return None


def _artists(row: dict) -> list[dict]:
    names_raw = _text(row.get("artists"), 1000)
    uris = [m.group(1) for m in _ARTIST_ID.finditer(row.get("artist_uris") or "")]
    if ";" in names_raw:
        names = [n.strip() for n in names_raw.split(";")]
    elif len(uris) > 1 and len(names_raw.split(", ")) == len(uris):
        # Older Exportify: comma-separated names, safe to split when the
        # artist URI column confirms how many there are.
        names = [n.strip() for n in names_raw.split(", ")]
    else:
        names = [names_raw]
    names = [n[:MAX_TEXT] for n in names if n][:20]
    artists = []
    for i, name in enumerate(names):
        aid = uris[i] if i < len(uris) else None
        artists.append({"id": aid, "name": name, "uri": f"spotify:artist:{aid}" if aid else None})
    return artists


def _features(row: dict) -> dict | None:
    f = {}
    for name in UNIT_FEATURES:
        f[name] = _number(row.get(name), 0, 1)
    f["tempo"] = _number(row.get("tempo"), 1, 400)
    f["loudness"] = _number(row.get("loudness"), -80, 10)
    key = _number(row.get("key"), -1, 11)
    mode = _number(row.get("mode"), 0, 1)
    f["key"] = int(key) if key is not None and key >= 0 and key == int(key) else None
    f["mode"] = int(mode) if mode is not None and mode == int(mode) else None
    ts = _number(row.get("time_signature"), 1, 12)
    f["time_signature"] = int(ts) if ts else None
    return f if any(v is not None for v in f.values()) else None


def _genres(row: dict) -> list[str]:
    out = []
    for g in (row.get("genres") or "").split(","):
        g = _text(g, MAX_GENRE_LEN).lower()
        if g and g not in out:
            out.append(g)
    return out[:MAX_GENRES]


def parse(data: bytes) -> tuple[list[dict], dict]:
    """Exportify CSV bytes -> (tracks, report). Raises ExportifyError if unusable."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")  # never fails; odd characters beat rejecting the file
    if "\x00" in text[:4096]:
        raise ExportifyError("That doesn't look like a CSV file.")
    csv.field_size_limit(1_000_000)
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    lookup = {h.lower(): h for h in header}
    mapping = {field: next((lookup[n.lower()] for n in names if n.lower() in lookup), None)
               for field, names in COLUMNS.items()}
    if not (mapping["uri"] or mapping["id"]) or not mapping["name"]:
        raise ExportifyError(
            "This doesn't look like an Exportify export: it needs a 'Track URI' and a 'Track Name' column. "
            "Export a playlist or your Liked Songs at exportify.net and upload the .csv it gives you.")

    report = {"rows": 0, "valid": 0, "skipped_local": 0, "skipped_invalid": 0, "duplicates_in_file": 0,
              "with_audio_features": 0, "with_genres": 0, "notes": []}
    if not any(mapping[f] for f in ("tempo", "energy", "key")):
        report["notes"].append("This export has no audio-feature columns (tempo, key, energy); "
                               "those will come from enrichment instead.")
    if not mapping["genres"]:
        report["notes"].append("This export has no Genres column.")

    tracks, seen = [], set()
    for raw in reader:
        report["rows"] += 1
        if report["rows"] > MAX_ROWS:
            report["notes"].append(f"Only the first {MAX_ROWS:,} rows were read.")
            report["rows"] -= 1
            break
        row = {field: raw.get(col) for field, col in mapping.items() if col}
        uri = (row.get("uri") or "").strip()
        if uri.startswith("spotify:local:"):
            report["skipped_local"] += 1
            continue
        track_id = _track_id(row)
        name = _text(row.get("name"))
        if not track_id or not name:
            report["skipped_invalid"] += 1
            continue
        if track_id in seen:
            report["duplicates_in_file"] += 1
            continue
        seen.add(track_id)

        isrc = _text(row.get("isrc"), 12).upper()
        release = _text(row.get("release_date"), 10)
        duration = _number(row.get("duration_ms"), 1000, 3 * 60 * 60 * 1000)
        popularity = _number(row.get("popularity"), 0, 100)
        features = _features(row)
        genres = _genres(row)
        tracks.append({
            "track_id": track_id,
            "track_uri": f"spotify:track:{track_id}",
            "track_name": name,
            "artists": _artists(row),
            "album": {"name": _text(row.get("album")) or None,
                      "release_date": release if _DATE.match(release) else None},
            "duration_ms": int(duration) if duration else None,
            "explicit": (row.get("explicit") or "").strip().lower() == "true",
            "isrc": isrc if _ISRC.match(isrc) else None,
            "popularity": int(popularity) if popularity is not None else None,
            "label": _text(row.get("label")) or None,
            # Always built from the ID, never taken from the file.
            "spotify_url": f"https://open.spotify.com/track/{track_id}",
            "is_local": False,
            "csv_features": features,
            "csv_genres": genres,
        })
        report["with_audio_features"] += bool(features)
        report["with_genres"] += bool(genres)
    report["valid"] = len(tracks)
    if not tracks:
        raise ExportifyError("No usable songs were found in that file.")
    return tracks, report
