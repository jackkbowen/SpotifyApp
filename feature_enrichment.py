"""Audio features from ReccoBeats (run via `enrich_library.py --only features`).

ReccoBeats is a free, keyless API that returns Spotify-style audio features
(tempo, energy, valence, danceability, acousticness, ...) and accepts Spotify
track IDs directly, 40 per request, so a whole library takes a few dozen
requests. It's unofficial and its terms aren't published, so raw responses are
cached separately (data/features_cache.json, keyed by Spotify track ID) and
everything derived from them is rebuilt from that cache.

Coverage is partial (about two thirds of this library in testing): tracks it
doesn't know are cached as not found and retried only with --retry-unmatched.
"""

from __future__ import annotations

import math
import random
import re
import sys
import time

import requests

from enrich_library import EnrichError, TransientError, now_iso, save_json
from setlist_app.keys import CAMELOT_TO_KEY, key_to_camelot
from setlist_app.paths import FEATURES_CACHE_PATH

RECCOBEATS_API_URL = "https://api.reccobeats.com/v1/audio-features"
BATCH_SIZE = 40  # the API rejects more than 40 ids per request
MIN_SECONDS_BETWEEN_REQUESTS = 0.5
REQUEST_TIMEOUT_SECONDS = 30
RETRIES = 4
MAX_CONSECUTIVE_FAILED_BATCHES = 3

# Raw field names as ReccoBeats returns them. 0-1 scores, tempo in BPM,
# loudness in dB, key as a pitch class (0 = C) and mode (1 = major).
FEATURE_FIELDS = (
    "tempo", "energy", "valence", "danceability", "acousticness",
    "instrumentalness", "speechiness", "liveness", "loudness", "key", "mode",
)
_PERCENT_FIELDS = ("energy", "valence", "danceability", "acousticness",
                   "instrumentalness", "speechiness", "liveness")
_PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_SPOTIFY_ID_IN_HREF = re.compile(r"/track/([A-Za-z0-9]{22})")


def empty_cache() -> dict:
    return {"results": {}, "not_found": {}}


def normalize_cache(cache: dict) -> dict:
    cache.setdefault("results", {})
    cache.setdefault("not_found", {})
    return cache


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def clean_features(item: dict) -> dict | None:
    """Raw API item -> the fields we cache, or None if it carries no features."""
    values = {field: _number(item.get(field)) for field in FEATURE_FIELDS}
    if all(v is None for v in values.values()):
        return None
    return {"reccobeats_id": item.get("id"), "isrc": item.get("isrc"), **values, "fetched_at": now_iso()}


class ReccoBeatsClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SpotifySetlistBuilder/1.0 (personal, local use)",
            "Accept": "application/json",
        })
        self.last_request = 0.0

    def _throttle(self) -> None:
        gap = self.last_request + MIN_SECONDS_BETWEEN_REQUESTS - time.time()
        if gap > 0:
            time.sleep(gap)
        self.last_request = time.time()

    def audio_features(self, spotify_ids: list[str]) -> list[dict]:
        problem = "unknown error"
        for attempt in range(1, RETRIES + 1):
            self._throttle()
            retry_after = None
            try:
                response = self.session.get(
                    RECCOBEATS_API_URL, params={"ids": ",".join(spotify_ids)},
                    timeout=REQUEST_TIMEOUT_SECONDS)
            except requests.RequestException as error:
                problem = f"network error ({error.__class__.__name__})"
            else:
                if response.status_code == 200:
                    try:
                        content = response.json().get("content")
                    except (ValueError, AttributeError):
                        problem = "non-JSON response"
                    else:
                        return content if isinstance(content, list) else []
                elif response.status_code == 429 or response.status_code >= 500:
                    problem = "rate limited" if response.status_code == 429 else f"HTTP {response.status_code}"
                    retry_after = _number(response.headers.get("Retry-After"))
                else:
                    raise TransientError(f"HTTP {response.status_code}: {response.text[:120]}")
            if attempt < RETRIES:
                wait = retry_after if retry_after else min(5 * 2 ** (attempt - 1), 120)
                wait = min(wait, 300) + random.uniform(0, 2)
                print(f"\nReccoBeats: {problem}; retrying in {wait:.0f}s...", flush=True)
                time.sleep(wait)
        raise TransientError(problem)


def _match_items(content: list[dict], requested: dict[str, dict]) -> dict[str, dict]:
    """Map returned items back to the Spotify IDs we asked for.

    Matches on the Spotify URL ReccoBeats returns; if it answered with a
    different track ID (e.g. a relinked version), falls back to the ISRC when
    that points at exactly one still-unmatched request.
    """
    matched: dict[str, dict] = {}
    leftovers = []
    for item in content:
        found = _SPOTIFY_ID_IN_HREF.search(item.get("href") or "")
        if found and found.group(1) in requested and found.group(1) not in matched:
            matched[found.group(1)] = item
        else:
            leftovers.append(item)
    for item in leftovers:
        candidates = [tid for tid, track in requested.items()
                      if tid not in matched and item.get("isrc") and track.get("isrc") == item["isrc"]]
        if len(candidates) == 1:
            matched[candidates[0]] = item
    return matched


def run_feature_lookups(tracks: list[dict], cache: dict, limit: int | None,
                        retry_not_found: bool) -> str | None:
    """Fetch audio features for every track not cached yet (updates `cache` in place).

    Returns a reason string if the run stopped early, else None.
    """
    normalize_cache(cache)
    unique: dict[str, dict] = {}
    for track in tracks:
        if track.get("track_id") and track["track_id"] not in unique:
            unique[track["track_id"]] = track
    todo = [tid for tid in unique
            if tid not in cache["results"] and (retry_not_found or tid not in cache["not_found"])]
    if limit is not None:
        todo = todo[:limit]
    print(f"ReccoBeats features: {len(unique)} tracks, {len(unique) - len(todo)} cached, "
          f"{len(todo)} to look up (batches of {BATCH_SIZE}).")
    if not todo:
        return None

    client = ReccoBeatsClient()
    interactive = sys.stdout.isatty()
    found = missing = failed_batches = 0
    consecutive_failures = 0
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    try:
        for number, batch in enumerate(batches, 1):
            requested = {tid: unique[tid] for tid in batch}
            try:
                matched = _match_items(client.audio_features(batch), requested)
            except TransientError:
                failed_batches += 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILED_BATCHES:
                    return (f"{consecutive_failures} ReccoBeats requests in a row failed; it may be down "
                            "or have changed. Run again later; finished lookups are saved.")
                continue
            consecutive_failures = 0
            for tid in batch:
                cleaned = clean_features(matched[tid]) if tid in matched else None
                if cleaned:
                    cache["results"][tid] = cleaned
                    cache["not_found"].pop(tid, None)
                    found += 1
                else:
                    cache["not_found"][tid] = {"looked_up_at": now_iso()}
                    missing += 1
            save_json(FEATURES_CACHE_PATH, cache)
            line = (f"Features: batch {number}/{len(batches)} · {found} found, {missing} not on ReccoBeats"
                    f"{f', {failed_batches} batches failed' if failed_batches else ''}")
            print(f"\r{line}" if interactive else line, end="" if interactive else "\n", flush=True)
    except KeyboardInterrupt:
        return "interrupted (Ctrl+C). Finished lookups are saved; run again to continue."
    except EnrichError as error:
        return str(error)
    finally:
        save_json(FEATURES_CACHE_PATH, cache)
        if interactive:
            print()
    if failed_batches:
        print(f"{failed_batches} batches failed with network/server errors; they'll be retried next run.")
    return None


def _percent(value: float | None) -> int | None:
    return None if value is None else max(0, min(100, round(value * 100)))


def to_features(raw: dict) -> dict:
    """Cached raw features -> the per-track `features` object in library_enriched.json.

    0-1 scores become 0-100 integers (matching danceability/acousticness
    elsewhere in the library); key + mode become Camelot.
    """
    tempo = raw.get("tempo")
    camelot = None
    key, mode = raw.get("key"), raw.get("mode")
    if key is not None and mode in (0, 1) and 0 <= key <= 11:  # key -1 means "not detected"
        camelot = key_to_camelot(_PITCH_NAMES[int(key)] + ("" if mode == 1 else "m"))
    features = {
        "source": "reccobeats",
        "tempo": round(tempo, 1) if tempo and tempo > 0 else None,
        "key": CAMELOT_TO_KEY.get(camelot) if camelot else None,
        "camelot": camelot,
        "loudness": raw.get("loudness"),
        "fetched_at": raw.get("fetched_at"),
    }
    features.update({field: _percent(raw.get(field)) for field in _PERCENT_FIELDS})
    return features


def features_for_library(tracks: list[dict], cache: dict) -> list[tuple[str, dict | None]]:
    """(feature_status, features) per track, in order.

    feature_status: "found", "not_found" (ReccoBeats doesn't have it) or
    "pending" (not looked up yet).
    """
    normalize_cache(cache)
    results = []
    for track in tracks:
        tid = track.get("track_id")
        if tid in cache["results"]:
            results.append(("found", to_features(cache["results"][tid])))
        elif tid in cache["not_found"]:
            results.append(("not_found", None))
        else:
            results.append(("pending", None))
    return results
