"""Mood/genre tag enrichment from Last.fm (run via `enrich_library.py`).

A second, independent pipeline next to the GetSongBPM one: it looks each
track up with Last.fm's track.getTopTags (by artist + title; Last.fm has no
ISRC lookup) and falls back to artist.getTopTags when a track has too few
usable tags, which is common for new or niche electronic releases.

Raw Last.fm responses are cached in data/lastfm_tag_cache.json; cleaning and
merging (setlist_app/tags.py) happens when library_enriched.json is built.

Tag data: Last.fm (https://www.last.fm). Free for non-commercial use; the
app credits Last.fm in its footer as their API terms require.
"""

from __future__ import annotations

import random
import sys
import time

import requests

from enrich_library import EnrichError, TransientError, clean_title, now_iso, save_json
from setlist_app.paths import LASTFM_CACHE_PATH
from setlist_app.tags import MIN_TRACK_TAGS, TagVocabulary, clean_tags, context_keys_for, merge_track_tags

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
# Last.fm doesn't publish a hard limit; ~5 requests/second averaged over a few
# minutes is the commonly cited guideline. Stay below it.
MIN_SECONDS_BETWEEN_REQUESTS = 0.25
REQUEST_TIMEOUT_SECONDS = 20
RETRIES = 4
MAX_CONSECUTIVE_FAILURES = 10
MAX_TAGS_STORED = 30  # per track/artist; keeps the cache small (well under Last.fm's 100MB cap)
SAVE_EVERY = 10

_ERR_NOT_FOUND = 6
_ERR_BAD_KEY = (10, 26)
_ERR_TEMPORARY = (8, 11, 16)
_ERR_RATE_LIMIT = 29


def empty_cache() -> dict:
    return {"tracks": {}, "artists": {}}


def track_key(track: dict) -> str | None:
    """Cache key: primary artist + title without feat./radio-edit noise."""
    artists = [a.get("name") for a in track.get("artists") or [] if a.get("name")]
    title = clean_title(track.get("track_name") or "")
    if not artists or not title:
        return None
    return f"{artists[0].lower()}\t{title.lower()}"


def artist_keys(track: dict) -> list[str]:
    # Primary artist, then the second-billed one for collaborations.
    return [a["name"].lower() for a in (track.get("artists") or [])[:2] if a.get("name")]


class LastFmClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "SpotifySetlistBuilder/1.0 (personal, non-commercial)"
        self.last_request = 0.0

    def _throttle(self) -> None:
        gap = self.last_request + MIN_SECONDS_BETWEEN_REQUESTS - time.time()
        if gap > 0:
            time.sleep(gap)
        self.last_request = time.time()

    def _call(self, method: str, **params) -> dict | None:
        """JSON response, or None when Last.fm doesn't know the track/artist."""
        query = {"method": method, "api_key": self.api_key, "format": "json", "autocorrect": 1, **params}
        problem = "unknown error"
        for attempt in range(1, RETRIES + 1):
            self._throttle()
            try:
                response = self.session.get(LASTFM_API_URL, params=query, timeout=REQUEST_TIMEOUT_SECONDS)
                data = response.json()
            except ValueError:  # before RequestException: requests' JSONDecodeError is both
                problem = f"non-JSON response (HTTP {response.status_code})"
                data = None
            except requests.RequestException as error:
                problem = f"network error ({error.__class__.__name__})"
                data = None
            else:
                error_code = data.get("error") if isinstance(data, dict) else None
                if error_code in _ERR_BAD_KEY:
                    raise EnrichError(
                        f"Last.fm rejected the API key ({data.get('message')}). "
                        "Check LASTFM_API_KEY in .env."
                    )
                if error_code == _ERR_NOT_FOUND:
                    return None
                if error_code == _ERR_RATE_LIMIT or response.status_code == 429:
                    problem = "rate limited"
                elif error_code in _ERR_TEMPORARY or response.status_code >= 500:
                    problem = f"temporarily unavailable ({data.get('message') or response.status_code})"
                elif error_code:
                    return None  # any other per-item error: treat as no data
                else:
                    return data
            if attempt < RETRIES:
                base = 30 if problem == "rate limited" else 5
                wait = min(base * 2 ** (attempt - 1), 300) + random.uniform(0, 2)
                print(f"\nLast.fm: {problem}; retrying in {wait:.0f}s...", flush=True)
                time.sleep(wait)
        if problem == "rate limited":
            raise EnrichError("Last.fm keeps rate limiting this key. Progress is saved; run again later.")
        raise TransientError(problem)

    def _top_tags(self, method: str, **params) -> list[dict] | None:
        data = self._call(method, **params)
        if data is None:
            return None
        tags = (data.get("toptags") or {}).get("tag") or []
        if isinstance(tags, dict):  # a single tag can come back unwrapped
            tags = [tags]
        result = []
        for tag in tags:
            if isinstance(tag, dict) and tag.get("name"):
                try:
                    count = int(tag.get("count") or 0)
                except (TypeError, ValueError):
                    count = 0
                result.append({"name": tag["name"], "count": count})
        return result[:MAX_TAGS_STORED]

    def track_tags(self, artist: str, title: str) -> list[dict] | None:
        return self._top_tags("track.gettoptags", artist=artist, track=title)

    def artist_tags(self, artist: str) -> list[dict] | None:
        return self._top_tags("artist.gettoptags", artist=artist)


def _entry(tags: list[dict] | None) -> dict:
    return {"found": tags is not None, "tags": tags or [], "fetched_at": now_iso()}


def _needs_artist_fallback(track: dict, raw_tags: list[dict]) -> bool:
    vocab = TagVocabulary(t["name"] for t in raw_tags)
    return len(clean_tags(raw_tags, vocab, context_keys_for(track))) < MIN_TRACK_TAGS


def run_tag_lookups(tracks: list[dict], cache: dict, api_key: str, limit: int | None,
                    retry_not_found: bool) -> str | None:
    """Fetch Last.fm tags for every track not cached yet (updates `cache` in place).

    Returns a reason string if the run stopped early, else None.
    """
    representatives: dict[str, dict] = {}
    for track in tracks:
        key = track_key(track)
        if key and key not in representatives:
            representatives[key] = track
    todo = [
        key for key, _ in representatives.items()
        if key not in cache["tracks"] or (retry_not_found and not cache["tracks"][key]["found"])
    ]
    if limit is not None:
        todo = todo[:limit]
    print(f"Last.fm tags: {len(representatives)} unique artist/title pairs, "
          f"{len(representatives) - len(todo)} cached, {len(todo)} to look up.")
    if not todo:
        return None

    client = LastFmClient(api_key)
    interactive = sys.stdout.isatty()
    found = not_found = artist_lookups = errors = consecutive_failures = 0

    try:
        for done, key in enumerate(todo, 1):
            track = representatives[key]
            artist, title = key.split("\t", 1)
            try:
                raw = client.track_tags(artist, title)
                cache["tracks"][key] = _entry(raw)
                if raw is None:
                    not_found += 1
                else:
                    found += 1
                if _needs_artist_fallback(track, raw or []):
                    for name in artist_keys(track):
                        if name not in cache["artists"]:
                            cache["artists"][name] = _entry(client.artist_tags(name))
                            artist_lookups += 1
                        if cache["artists"][name]["tags"]:
                            break  # the primary artist had tags; no need for the second
            except TransientError:
                errors += 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    return (f"{consecutive_failures} Last.fm lookups in a row failed; it may be down. "
                            "Run again later; finished lookups are saved.")
                continue
            consecutive_failures = 0
            if done % SAVE_EVERY == 0:
                save_json(LASTFM_CACHE_PATH, cache)
            line = (f"Tagged {done} / {len(todo)} tracks... ({found} found on Last.fm, "
                    f"{not_found} not found, {artist_lookups} artist lookups"
                    f"{f', {errors} errors' if errors else ''})")
            if interactive:
                print(f"\r{line}", end="", flush=True)
            elif done % 25 == 0 or done == len(todo):
                print(line, flush=True)
    except KeyboardInterrupt:
        return "interrupted (Ctrl+C). Finished lookups are saved; run again to continue."
    except EnrichError as error:
        return str(error)
    finally:
        save_json(LASTFM_CACHE_PATH, cache)
        if interactive:
            print()
    if errors:
        print(f"{errors} Last.fm lookups failed with network/server errors; they'll be retried next run.")
    return None


def tags_for_library(tracks: list[dict], cache: dict) -> list[tuple[str, list[dict]]]:
    """(tag_status, tags) for each track, in order.

    tag_status: "tagged" = has usable tags, "no_tags" = looked up but nothing
    usable survived cleaning (or not on Last.fm), "pending" = not looked up yet.
    """
    all_raw_names = [t["name"] for entry in (*cache["tracks"].values(), *cache["artists"].values())
                     for t in entry["tags"]]
    vocab = TagVocabulary(all_raw_names)
    results = []
    for track in tracks:
        key = track_key(track)
        entry = cache["tracks"].get(key) if key else None
        if entry is None:
            results.append(("pending", []))
            continue
        artist_raws = [cache["artists"][name]["tags"] for name in artist_keys(track) if name in cache["artists"]]
        tags = merge_track_tags(track, entry["tags"], artist_raws, vocab)
        results.append(("tagged" if tags else "no_tags", tags))
    return results
