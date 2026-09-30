#!/usr/bin/env python3
"""Export your Spotify Liked Songs library to local CSV and JSON files.

Usage:
    python export_liked_songs.py                 # export to ./output/
    python export_liked_songs.py --output-dir D  # export somewhere else
    python export_liked_songs.py --reauth        # discard cached login and sign in again

See README.md for setup (Spotify Developer app, .env, dependencies).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import spotipy
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from spotipy.cache_handler import CacheFileHandler
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import SpotifyOAuth
from urllib3.util.retry import Retry

PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"
TOKEN_CACHE_PATH = PROJECT_DIR / ".spotify_token_cache"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"
CSV_FILENAME = "liked_songs.csv"
JSON_FILENAME = "liked_songs.json"

# Read-only access to the user's saved tracks. Nothing broader.
SCOPE = "user-library-read"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"

PAGE_SIZE = 50  # maximum `limit` accepted by GET /me/tracks
RATE_LIMIT_MAX_ATTEMPTS = 6
# Spotify occasionally answers a 429 with a Retry-After of several hours
# (usually after heavy use in Development Mode). Rather than silently hang,
# give up and tell the user when the requested wait is longer than this.
RATE_LIMIT_MAX_WAIT_SECONDS = 15 * 60

# Track/album keys that are mapped to explicit CSV columns (or deliberately
# dropped). Any *other* scalar field Spotify returns is passed through to the
# CSV automatically, so the export follows the live response shape instead of
# a hardcoded schema. E.g. `popularity` was removed in the Feb 2026 API
# migration: if it ever reappears it becomes a column, and while it is absent
# no empty column is written.
TRACK_KEYS_MAPPED = {
    "id", "uri", "name", "artists", "album", "duration_ms", "explicit",
    "external_ids", "external_urls", "disc_number", "track_number", "is_local",
    "href", "type",
}
ALBUM_KEYS_MAPPED = {
    "id", "uri", "name", "artists", "album_type", "release_date",
    "release_date_precision", "external_urls", "href", "type", "images",
}

CSV_BASE_COLUMNS = [
    "added_at",
    "track_name",
    "artists",
    "album_name",
    "album_artists",
    "album_release_date",
    "album_release_date_precision",
    "album_type",
    "duration_ms",
    "duration",
    "explicit",
    "isrc",
    "disc_number",
    "track_number",
    "is_local",
    "track_id",
    "track_uri",
    "spotify_url",
    "artist_ids",
    "album_id",
]


# spotipy logs every HTTP error before raising it; this script reports errors
# itself with clearer, actionable messages, so keep its logger quiet.
logging.getLogger("spotipy").setLevel(logging.CRITICAL)


class ExportError(Exception):
    """An error with a message that is meant to be shown to the user as-is."""


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #

def _read_setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    # Treat the untouched .env.example placeholders as "not set".
    return "" if value.startswith("your-") else value


def build_client(reauth: bool) -> spotipy.Spotify:
    load_dotenv(ENV_PATH)
    client_id = _read_setting("SPOTIFY_CLIENT_ID")
    client_secret = _read_setting("SPOTIFY_CLIENT_SECRET")
    redirect_uri = _read_setting("SPOTIFY_REDIRECT_URI") or DEFAULT_REDIRECT_URI

    missing = [
        name
        for name, value in (
            ("SPOTIFY_CLIENT_ID", client_id),
            ("SPOTIFY_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise ExportError(
            f"Missing {', '.join(missing)}. Copy .env.example to .env "
            f"({ENV_PATH}) and fill in the values from your app in the "
            "Spotify Developer Dashboard. See README.md."
        )

    if reauth and TOKEN_CACHE_PATH.exists():
        TOKEN_CACHE_PATH.unlink()
        print("Discarded cached Spotify login.")

    if not TOKEN_CACHE_PATH.exists():
        print(
            "No saved Spotify login found. Your browser will open so you can "
            "log in and approve read-only access to your Liked Songs.\n"
            f"(If it doesn't, copy the URL printed below into a browser. "
            f"Redirect URI in use: {redirect_uri})"
        )

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPE,
        # spotipy refreshes the access token automatically using the cached
        # refresh token, so you only log in through the browser once.
        cache_handler=CacheFileHandler(cache_path=str(TOKEN_CACHE_PATH)),
        open_browser=True,
    )
    return spotipy.Spotify(
        auth_manager=auth_manager,
        requests_session=_build_http_session(),
        requests_timeout=20,
    )


def _build_http_session() -> requests.Session:
    """HTTP session that retries transient 5xx errors but leaves 429s to us.

    spotipy's default session retries 429s inside urllib3, which sleeps for
    whatever Retry-After Spotify sends (possibly hours) with no output, and
    then reports a failure without the Retry-After value. Handling 429s in
    `call_with_backoff` lets us show progress and cap the wait.
    """
    retry = Retry(
        total=3,
        connect=3,
        read=False,
        status=3,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        backoff_factor=1.0,
        respect_retry_after_header=False,
        # After the last 5xx retry, return the response so spotipy raises a
        # SpotifyException with the real status instead of a generic error.
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def _retry_after_seconds(error: SpotifyException, attempt: int) -> float:
    header = (error.headers or {}).get("Retry-After")
    try:
        return max(float(header), 1.0)
    except (TypeError, ValueError):
        # No usable header: exponential backoff (2, 4, 8, ... seconds).
        return float(2 ** attempt)


def call_with_backoff(func, *args, **kwargs):
    """Call a spotipy method, waiting and retrying when rate limited (HTTP 429)."""
    for attempt in range(1, RATE_LIMIT_MAX_ATTEMPTS + 1):
        try:
            return func(*args, **kwargs)
        except SpotifyException as error:
            if error.http_status != 429:
                raise
            if attempt == RATE_LIMIT_MAX_ATTEMPTS:
                raise ExportError(
                    f"Still rate limited by Spotify after {attempt} attempts. "
                    "Wait a few minutes and run the export again."
                ) from error
            wait = _retry_after_seconds(error, attempt)
            if wait > RATE_LIMIT_MAX_WAIT_SECONDS:
                raise ExportError(
                    f"Spotify is rate limiting this app and asked us to wait "
                    f"{wait / 60:.0f} minutes. Try again later."
                ) from error
            wait += random.uniform(0, 1)  # jitter
            print(
                f"\nRate limited by Spotify; waiting {wait:.0f}s before retrying "
                f"(attempt {attempt}/{RATE_LIMIT_MAX_ATTEMPTS})...",
                flush=True,
            )
            time.sleep(wait)
    raise AssertionError("unreachable")


def fetch_liked_songs(sp: spotipy.Spotify) -> tuple[list[dict], int]:
    """Page through GET /me/tracks until every saved track has been retrieved.

    Returns (saved-track items exactly as Spotify sent them, number skipped).
    """
    items: list[dict] = []
    seen_uris: set[str] = set()
    skipped = 0
    offset = 0
    expected_total = None
    interactive = sys.stdout.isatty()

    while True:
        page = call_with_backoff(
            sp.current_user_saved_tracks, limit=PAGE_SIZE, offset=offset
        )
        if page is None:
            raise ExportError("Spotify returned an empty response for GET /me/tracks.")
        if expected_total is None:
            expected_total = page.get("total") or 0
            if expected_total:
                print(f"Spotify reports ~{expected_total} liked songs.")

        page_items = page.get("items") or []
        for item in page_items:
            track = item.get("track")
            if not track:
                # Can happen for tracks that are no longer available at all.
                skipped += 1
                continue
            uri = track.get("uri")
            # If you like/unlike a song mid-export, offsets shift and a track
            # can show up on two pages; keep only the first occurrence.
            if uri and uri in seen_uris:
                continue
            if uri:
                seen_uris.add(uri)
            items.append(item)

        offset += len(page_items)
        if page_items:
            progress = f"Fetched {len(items)} of ~{expected_total} tracks..."
            print(f"\r{progress}" if interactive else progress, end="" if interactive else "\n", flush=True)

        if not page_items or not page.get("next"):
            break

    if interactive and offset:
        print()
    return items, skipped


# --------------------------------------------------------------------------- #
# Shaping the data
# --------------------------------------------------------------------------- #

def _artist_list(artists: list[dict] | None) -> list[dict]:
    return [
        {"id": a.get("id"), "name": a.get("name"), "uri": a.get("uri")}
        for a in (artists or [])
    ]


def to_record(item: dict) -> dict:
    """Normalized, nested record for the JSON export."""
    track = item["track"]
    album = track.get("album") or {}
    return {
        "added_at": item.get("added_at"),
        "track_id": track.get("id"),  # None for local files
        "track_uri": track.get("uri"),
        "track_name": track.get("name"),
        "artists": _artist_list(track.get("artists")),
        "album": {
            "id": album.get("id"),
            "uri": album.get("uri"),
            "name": album.get("name"),
            "album_type": album.get("album_type"),
            "release_date": album.get("release_date"),
            "release_date_precision": album.get("release_date_precision"),
            "artists": _artist_list(album.get("artists")),
        },
        "duration_ms": track.get("duration_ms"),
        "explicit": track.get("explicit"),
        # external_ids was removed in the Feb 2026 migration and restored in
        # March 2026, so treat it as optional.
        "isrc": (track.get("external_ids") or {}).get("isrc"),
        "disc_number": track.get("disc_number"),
        "track_number": track.get("track_number"),
        "is_local": track.get("is_local"),
        "spotify_url": (track.get("external_urls") or {}).get("spotify"),
        # NOTE: Audio features (BPM/tempo, key, mode, energy, danceability...)
        # are intentionally NOT fetched. Spotify's GET /audio-features and
        # /audio-analysis endpoints are restricted to extended-quota apps, and
        # this tool is designed for a Development Mode app. A future version
        # can enrich these records from another source (e.g. analysing your
        # own audio files with librosa/Essentia, or a third-party BPM/key
        # database), joining on `isrc` (most portable) or `track_id`. Add
        # those fields here and to the CSV columns once that source exists.
        #
        # Everything Spotify returned for this saved track, untouched, so
        # later tools can use fields that are not mapped above.
        "spotify_raw": item,
    }


def _is_scalar(value) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _format_duration(ms) -> str:
    if not isinstance(ms, int):
        return ""
    minutes, seconds = divmod(round(ms / 1000), 60)
    return f"{minutes}:{seconds:02d}"


def to_csv_row(record: dict) -> dict:
    track = record["spotify_raw"]["track"]
    album_raw = track.get("album") or {}
    album = record["album"]
    row = {
        "added_at": record["added_at"],
        "track_name": record["track_name"],
        "artists": "; ".join(a["name"] or "" for a in record["artists"]),
        "album_name": album["name"],
        "album_artists": "; ".join(a["name"] or "" for a in album["artists"]),
        "album_release_date": album["release_date"],
        "album_release_date_precision": album["release_date_precision"],
        "album_type": album["album_type"],
        "duration_ms": record["duration_ms"],
        "duration": _format_duration(record["duration_ms"]),
        "explicit": record["explicit"],
        "isrc": record["isrc"],
        "disc_number": record["disc_number"],
        "track_number": record["track_number"],
        "is_local": record["is_local"],
        "track_id": record["track_id"],
        "track_uri": record["track_uri"],
        "spotify_url": record["spotify_url"],
        "artist_ids": "; ".join(a["id"] or "" for a in record["artists"]),
        "album_id": album["id"],
    }
    for key, value in track.items():
        if key not in TRACK_KEYS_MAPPED and _is_scalar(value):
            row[key] = value
    for key, value in album_raw.items():
        if key not in ALBUM_KEYS_MAPPED and _is_scalar(value):
            row[f"album_{key}"] = value
    return row


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def write_csv(path: Path, records: list[dict]) -> None:
    rows = [to_csv_row(r) for r in records]
    extra_columns = sorted({key for row in rows for key in row} - set(CSV_BASE_COLUMNS))
    # utf-8-sig adds a BOM so Excel detects UTF-8 (accents, CJK, emoji).
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_BASE_COLUMNS + extra_columns, restval="")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, records: list[dict], exported_at: str) -> None:
    payload = {
        "exported_at": exported_at,
        "source": "Spotify Web API GET /me/tracks",
        "track_count": len(records),
        "tracks": records,
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _parse_added_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Error reporting
# --------------------------------------------------------------------------- #

def describe_oauth_error(error: SpotifyOauthError) -> str:
    code = getattr(error, "error", None)
    if code == "invalid_client":
        return (
            "Spotify rejected the client credentials. Check SPOTIFY_CLIENT_ID "
            "and SPOTIFY_CLIENT_SECRET in .env against the Developer Dashboard."
        )
    if code == "invalid_grant":
        return (
            "Your saved Spotify login has expired or was revoked. Run again "
            "with --reauth to log in fresh."
        )
    if code == "access_denied":
        return "Access was not granted in the browser. Run again and click 'Agree'."
    return (
        f"Spotify authentication failed: {error}. Run again with --reauth to "
        "log in fresh; if it keeps failing, check the values in .env."
    )


def describe_api_error(error: SpotifyException) -> str:
    if error.http_status == 401:
        return (
            "Spotify says the access token is invalid or expired. Run again "
            "with --reauth to log in fresh."
        )
    if error.http_status == 403:
        return (
            "Spotify refused access (HTTP 403). For a Development Mode app, "
            "the Spotify account you logged in with must be listed under "
            "User Management in the Developer Dashboard, and the app owner "
            "needs an active Premium subscription. If you logged in with the "
            "wrong account, run again with --reauth.\n"
            f"Details: {error.msg}"
        )
    return f"Spotify API error (HTTP {error.http_status}): {error.msg}"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export your Spotify Liked Songs to CSV and JSON."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to write {CSV_FILENAME} and {JSON_FILENAME} "
        f"(default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="Delete the cached Spotify login and sign in again.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        sp = build_client(args.reauth)
        print("Fetching Liked Songs from Spotify...")
        items, skipped = fetch_liked_songs(sp)
    except ExportError as error:
        print(f"\nError: {error}", file=sys.stderr)
        return 1
    except SpotifyOauthError as error:
        print(f"\nError: {describe_oauth_error(error)}", file=sys.stderr)
        return 1
    except SpotifyException as error:
        print(f"\nError: {describe_api_error(error)}", file=sys.stderr)
        return 1
    except requests.exceptions.RequestException as error:
        print(
            f"\nError: could not reach Spotify ({error.__class__.__name__}). "
            "Check your internet connection and try again.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Nothing was written.", file=sys.stderr)
        return 130

    if not items:
        if skipped:
            print(f"All {skipped} liked songs Spotify returned are unavailable; nothing to export.")
        else:
            print("Your Liked Songs library is empty; nothing to export.")
        # Exit without writing files so an earlier export isn't replaced by an empty one.
        return 0

    # Same order as the Spotify app: most recently liked first.
    records = [to_record(item) for item in items]
    exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / CSV_FILENAME
    json_path = output_dir / JSON_FILENAME
    write_csv(csv_path, records)
    write_json(json_path, records, exported_at)

    added = [d for d in (_parse_added_at(r["added_at"]) for r in records) if d]
    print()
    print("Export complete")
    print(f"  Tracks exported:  {len(records)}")
    if skipped:
        print(f"  Skipped:          {skipped} (unavailable on Spotify)")
    if added:
        print(f"  Oldest liked:     {min(added):%Y-%m-%d}")
        print(f"  Newest liked:     {max(added):%Y-%m-%d}")
    print(f"  CSV:              {csv_path}")
    print(f"  JSON:             {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
