#!/usr/bin/env python3
"""Enrich the exported Liked Songs library with BPM, key, danceability and
acousticness (GetSongBPM) and mood/genre tags (Last.fm, see tag_enrichment.py),
and write data/library_enriched.json.

Usage:
    python enrich_library.py                   # both steps, everything not cached yet
    python enrich_library.py --only tags       # just the Last.fm tag step (or --only bpm)
    python enrich_library.py --limit 25        # try a few first to check match quality
    python enrich_library.py --retry-unmatched # also retry tracks not found before
    python enrich_library.py --no-fetch        # just rebuild library_enriched.json from the caches

A step whose API key isn't in .env is skipped with a note.

Re-run it any time after a new Spotify export: cached ISRCs are never looked
up again, so only newly liked songs cost API requests.

BPM/key data is provided by GetSongBPM (https://getsongbpm.com). Their free
API requires a visible backlink wherever the data is shown; the app's footer
and exports carry it.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from setlist_app.keys import CAMELOT_TO_KEY, key_to_camelot, open_key_to_camelot
from setlist_app.paths import (
    BPM_CACHE_PATH,
    ENRICHED_LIBRARY_PATH,
    ENV_PATH,
    LASTFM_CACHE_PATH,
    LIKED_SONGS_PATH,
    UNMATCHED_PATH,
)

GETSONGBPM_BASE_URL = "https://api.getsong.co"
GETSONGBPM_ATTRIBUTION = "BPM and key data provided by GetSongBPM (https://getsongbpm.com)"

# GetSongBPM allows 3000 requests/hour and blocks the key for an hour if you
# go over. Stay comfortably under it, counting requests from earlier runs too
# (their timestamps are kept in the cache file).
HOURLY_REQUEST_BUDGET = 2800
MIN_SECONDS_BETWEEN_REQUESTS = 0.3
REQUEST_TIMEOUT_SECONDS = 20
TRANSIENT_RETRIES = 4
MAX_CONSECUTIVE_FAILURES = 10
SAVE_EVERY = 10  # lookups between cache writes

# A candidate is accepted only if its base title is at least this similar
# after normalisation (and the artist and version checks pass).
TITLE_MATCH_THRESHOLD = 0.88
ARTIST_MATCH_THRESHOLD = 0.88


class EnrichError(Exception):
    """Fatal problem: message is shown to the user and the run stops."""


class RateLimitedError(EnrichError):
    """GetSongBPM keeps refusing requests; stop and let the user retry later."""


class TransientError(Exception):
    """Lookup failed for reasons unrelated to the track; retry on a later run."""


# --------------------------------------------------------------------------- #
# Small file helpers
# --------------------------------------------------------------------------- #

def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    """Write atomically so an interrupted run never leaves a corrupt file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Library input
# --------------------------------------------------------------------------- #

def load_library(path: Path) -> dict:
    if not path.exists():
        raise EnrichError(
            f"{path} not found. Run export_liked_songs.py first (or pass --input)."
        )
    with path.open(encoding="utf-8") as f:
        library = json.load(f)
    if not isinstance(library.get("tracks"), list):
        raise EnrichError(f"{path} doesn't look like a Liked Songs export (no 'tracks' list).")
    return library


def slim_track(track: dict) -> dict:
    # spotify_raw duplicates everything else and roughly triples the file size.
    return {k: v for k, v in track.items() if k != "spotify_raw"}


def unique_by_isrc(tracks: list[dict]) -> dict[str, dict]:
    """ISRC -> first track with that ISRC. Different releases of the same
    recording share an ISRC, so they share one lookup."""
    by_isrc: dict[str, dict] = {}
    for track in tracks:
        isrc = track.get("isrc")
        if isrc and isrc not in by_isrc:
            by_isrc[isrc] = track
    return by_isrc


# --------------------------------------------------------------------------- #
# Title / artist matching
#
# GetSongBPM has no ISRC lookup, only text search by song and artist. So ISRC
# is our cache key, but the match itself is done on names and then checked
# carefully: a remix or VIP has its own BPM/key, so "Song (X Remix)" must
# never be matched to the original "Song" or to someone else's remix.
# --------------------------------------------------------------------------- #

# Descriptors that label the same recording (safe to drop before searching).
_SAME_RECORDING_RE = re.compile(
    r"\s*(?:[-–—]\s*|[(\[]\s*)"
    r"(?:original(?: radio)? (?:mix|edit|version)|radio (?:edit|version|mix)|"
    r"extended (?:mix|version|edit)|"
    r"explicit|clean|remaster(?:ed)?(?:\s+\d{4})?|\d{4}\s+remaster(?:ed)?|"
    r"(?:feat\.?|ft\.?|featuring|with)\s+[^)\]]*)"
    # ...and it must be the whole bracket / trailing part, so "- Clean Bandit
    # Remix" isn't mistaken for a "- Clean" suffix.
    r"\s*(?:[)\]]|$)",
    re.IGNORECASE,
)
_DESCRIPTOR_RE = re.compile(r"\s*(?:[(\[]([^)\]]*)[)\]]|\s[-–—]\s(.*))\s*$")
# A trailing part that names a different *version* of the song. Anything else
# in brackets, e.g. "(I Love)" or '(From "Euphoria" Soundtrack)', is treated as
# a subtitle and ignored for matching.
_VERSION_WORD_RE = re.compile(
    r"\b(?:remix|rmx|mix|vip|edit|flip|bootleg|rework|refix|remake|live|acoustic|"
    r"version|instrumental|acapella|a cappella|dub|cover|slowed|sped|reprise|demo|"
    r"unplugged|extended)\b",
    re.IGNORECASE,
)


def clean_title(title: str) -> str:
    """Drop feat./radio edit/remaster-style suffixes; keep remix/VIP/etc."""
    previous = None
    while previous != title:
        previous = title
        title = _SAME_RECORDING_RE.sub("", title).strip()
    return title


def split_title(title: str) -> tuple[str, str]:
    """'Song (Knock2 Remix)' -> ('Song', 'Knock2 Remix');
    'Chest Pain (I Love)' -> ('Chest Pain', '')."""
    title = clean_title(title)
    descriptors = []
    while True:
        match = _DESCRIPTOR_RE.search(title)
        if not match or match.start() == 0:
            break
        part = match.group(1) or match.group(2) or ""
        if _VERSION_WORD_RE.search(part):
            descriptors.insert(0, part)
        title = title[: match.start()].strip()
    return title, " ".join(descriptors)


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    # Titles in non-Latin scripts would fold to nothing; keep them as-is.
    text = (folded if folded.strip() else text).lower()
    text = text.replace("&", " and ").replace("$", "s")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


def _tokens(text: str) -> set[str]:
    return set(normalize(text).split()) - {"the", "a", "mix", "version"}


def _version_words(text: str) -> set[str]:
    return {w.lower().replace("rmx", "remix") for w in _VERSION_WORD_RE.findall(text)}


def descriptors_match(ours: str, theirs: str) -> bool:
    ours_tokens, theirs_tokens = _tokens(ours), _tokens(theirs)
    if not ours_tokens and not theirs_tokens:
        return True
    if not ours_tokens or not theirs_tokens:
        return False
    # "Club Mix" vs "Club Mix (VIP)" share most words but are different versions.
    if _version_words(ours) != _version_words(theirs):
        return False
    overlap = len(ours_tokens & theirs_tokens) / len(ours_tokens | theirs_tokens)
    return overlap >= 0.6


_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|\+|/|\bx\b|\band\b|\bvs\.?|\bfeat\.?|\bft\.?|\bwith\b)\s*", re.IGNORECASE)


def artists_match(spotify_artists: list[str], candidate_artist: str) -> bool:
    parts = [p for p in _ARTIST_SPLIT_RE.split(candidate_artist or "") if p.strip()]
    parts.append(candidate_artist or "")
    return any(
        similarity(ours, theirs) >= ARTIST_MATCH_THRESHOLD
        for ours in spotify_artists
        for theirs in parts
    )


def score_candidate(track: dict, candidate: dict) -> float | None:
    """Return a match score, or None if the candidate is not this recording."""
    artist_names = [a["name"] for a in track.get("artists") or [] if a.get("name")]
    if not artists_match(artist_names, candidate.get("artist_name") or ""):
        return None
    our_base, our_desc = split_title(track.get("track_name") or "")
    their_base, their_desc = split_title(candidate.get("title") or "")
    title_score = similarity(our_base, their_base)
    if title_score < TITLE_MATCH_THRESHOLD or not descriptors_match(our_desc, their_desc):
        return None
    return title_score


# --------------------------------------------------------------------------- #
# GetSongBPM client
# --------------------------------------------------------------------------- #

class RateLimiter:
    """Sliding one-hour window, persisted across runs via the cache file."""

    def __init__(self, recent_timestamps: list[float]):
        cutoff = time.time() - 3600
        self.timestamps = deque(t for t in recent_timestamps if t > cutoff)

    def wait(self) -> None:
        now = time.time()
        while self.timestamps and self.timestamps[0] <= now - 3600:
            self.timestamps.popleft()
        if len(self.timestamps) >= HOURLY_REQUEST_BUDGET:
            pause = self.timestamps[0] + 3600 - now + 1
            print(
                f"\nHourly GetSongBPM budget ({HOURLY_REQUEST_BUDGET} requests) used up; "
                f"pausing {pause / 60:.0f} min. Ctrl+C is safe: progress is saved.",
                flush=True,
            )
            time.sleep(pause)
        elif self.timestamps:
            gap = self.timestamps[-1] + MIN_SECONDS_BETWEEN_REQUESTS - time.time()
            if gap > 0:
                time.sleep(gap)
        self.timestamps.append(time.time())


def _normalize_candidate(raw: dict) -> dict:
    """Accept both the current and the older GetSongBPM field names."""
    artist = raw.get("artist") if isinstance(raw.get("artist"), dict) else {}
    return {
        "id": raw.get("id") or raw.get("song_id"),
        "title": raw.get("title") or raw.get("song_title"),
        "uri": raw.get("uri") or raw.get("song_uri"),
        "artist_name": artist.get("name") or raw.get("name") or raw.get("artist_name"),
        "tempo": raw.get("tempo"),
        "key_of": raw.get("key_of"),
        "open_key": raw.get("open_key"),
        "danceability": raw.get("danceability"),
        "acousticness": raw.get("acousticness"),
        "time_sig": raw.get("time_sig"),
    }


def _has_full_details(candidate: dict) -> bool:
    return all(
        candidate.get(field) not in (None, "")
        for field in ("tempo", "key_of", "danceability", "acousticness")
    )


def _to_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _to_percent(value) -> int | None:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def build_enrichment(candidate: dict, query: str, score: float) -> dict:
    camelot = key_to_camelot(candidate.get("key_of")) or open_key_to_camelot(candidate.get("open_key"))
    key = candidate.get("key_of") or (CAMELOT_TO_KEY.get(camelot) if camelot else None)
    bpm = _to_number(candidate.get("tempo"))
    return {
        "source": "getsongbpm",
        "bpm": round(bpm, 1) if bpm else None,
        "key": key,
        "camelot": camelot,
        "open_key": candidate.get("open_key"),
        "danceability": _to_percent(candidate.get("danceability")),
        "acousticness": _to_percent(candidate.get("acousticness")),
        "time_signature": candidate.get("time_sig"),
        "source_id": candidate.get("id"),
        "source_url": candidate.get("uri"),
        "matched_title": candidate.get("title"),
        "matched_artist": candidate.get("artist_name"),
        "match_score": round(score, 3),
        "query": query,
        "looked_up_at": now_iso(),
    }


class GetSongBPMSource:
    name = "getsongbpm"

    def __init__(self, api_key: str, limiter: RateLimiter):
        self.api_key = api_key
        self.limiter = limiter
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "SpotifySetlistBuilder/1.0 (personal, local use)"

    def _get(self, endpoint: str, query_string: str) -> dict:
        # Built by hand: the API expects "song:x artist:y" with literal colons.
        url = f"{GETSONGBPM_BASE_URL}/{endpoint}/?api_key={quote(self.api_key)}&{query_string}"
        for attempt in range(1, TRANSIENT_RETRIES + 1):
            self.limiter.wait()
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            except requests.RequestException as error:
                problem = f"network error ({error.__class__.__name__})"
            else:
                if response.status_code == 401:
                    raise EnrichError(
                        "GetSongBPM rejected the API key (HTTP 401). Check GETSONGBPM_API_KEY "
                        "in .env, and that you confirmed the activation email."
                    )
                if response.status_code == 429 or (
                    response.status_code == 403 and "limit" in response.text.lower()
                ):
                    if attempt == TRANSIENT_RETRIES:
                        raise RateLimitedError(
                            "GetSongBPM is rate limiting this API key (it blocks keys for an "
                            "hour after 3000 requests/hour). Progress is saved; run again later."
                        )
                    problem = "rate limited"
                elif response.status_code >= 500 or response.status_code == 403:
                    problem = f"HTTP {response.status_code}"
                else:
                    try:
                        return response.json()
                    except ValueError:
                        problem = "non-JSON response"
            if attempt < TRANSIENT_RETRIES:
                wait = min(2 ** attempt * 5, 120) + random.uniform(0, 2)
                print(f"\nGetSongBPM: {problem}; retrying in {wait:.0f}s...", flush=True)
                time.sleep(wait)
        raise TransientError(problem)

    def search(self, title: str, artist: str) -> list[dict]:
        lookup = quote(f"song:{title} artist:{artist}", safe=":")
        data = self._get("search", f"type=both&lookup={lookup}")
        results = data.get("search")
        # No hits comes back as {"search": {"error": "no result"}}.
        if not isinstance(results, list):
            return []
        return [_normalize_candidate(r) for r in results if isinstance(r, dict)]

    def song(self, song_id: str) -> dict | None:
        data = self._get("song", f"id={quote(song_id)}")
        song = data.get("song")
        return _normalize_candidate(song) if isinstance(song, dict) else None

    def lookup(self, track: dict) -> dict | None:
        """Best verified match for this track, or None if GetSongBPM lacks it."""
        base, descriptor = split_title(track.get("track_name") or "")
        title = f"{base} {descriptor}".strip()
        artists = [a["name"] for a in track.get("artists") or [] if a.get("name")]
        # Primary artist first; for collaborations the song may be filed under
        # the second-billed artist. Two searches at most per track.
        for artist in artists[:2]:
            query = f"song:{title} artist:{artist}"
            scored = [
                (score, candidate)
                for candidate in self.search(title, artist)
                if (score := score_candidate(track, candidate)) is not None
            ]
            if not scored:
                continue
            score, best = max(scored, key=lambda pair: (pair[0], _has_full_details(pair[1])))
            if not _has_full_details(best) and best.get("id"):
                best = {**best, **{k: v for k, v in (self.song(best["id"]) or {}).items() if v not in (None, "")}}
            return build_enrichment(best, query, score)
        return None


# EXTENSION POINT: additional sources are tried in order for tracks the
# previous ones can't match. A natural fallback is AcousticBrainz: it stopped
# collecting data in 2022, but its existing dataset (bpm, key_key/key_scale,
# danceability) is CC0 public domain and needs no backlink. It is keyed by
# MusicBrainz recording ID (MBID), so an AcousticBrainzSource would map
# ISRC -> MBID via the MusicBrainz API (GET /ws/2/isrc/{isrc}), then read the
# low-level features from the AcousticBrainz data dumps. Implement the same
# `name` + `lookup(track) -> dict | None` interface, return a dict shaped like
# build_enrichment() with "source": "acousticbrainz", and append it below.
def build_sources(api_key: str, limiter: RateLimiter) -> list:
    return [GetSongBPMSource(api_key, limiter)]


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def build_enriched_library(library: dict, cache: dict, unmatched: dict, source_path: Path,
                           lastfm_cache: dict) -> dict:
    import tag_enrichment  # imported here: tag_enrichment itself imports from this module

    tag_results = tag_enrichment.tags_for_library(library["tracks"], lastfm_cache)
    tag_counts = {"tagged": 0, "no_tags": 0, "pending": 0}
    tracks = []
    counts = {"enriched": 0, "unmatched": 0, "pending": 0}
    for track, (tag_status, tags) in zip(library["tracks"], tag_results):
        tag_counts[tag_status] += 1
        slim = slim_track(track)
        isrc = slim.get("isrc")
        if isrc in cache:
            status, enrichment = "enriched", cache[isrc]
        elif isrc in unmatched:
            status, enrichment = "unmatched", None
        else:
            status, enrichment = "pending", None
        counts[status] += 1
        # enrichment_status: "enriched" = data found, "unmatched" = looked up
        # but no source had it, "pending" = not looked up yet (newly liked).
        # tags: cleaned Last.fm tags, strongest first, [{tag, weight 0-100, source}].
        # tag_status: "tagged", "no_tags" (looked up, nothing usable) or "pending".
        tracks.append({**slim, "enrichment_status": status, "enrichment": enrichment,
                       "tag_status": tag_status, "tags": tags})
    return {
        "generated_at": now_iso(),
        "source_file": str(source_path),
        "source_exported_at": library.get("exported_at"),
        "attribution": f"{GETSONGBPM_ATTRIBUTION}; tag data from Last.fm (https://www.last.fm)",
        "track_count": len(tracks),
        "status_counts": counts,
        "tag_status_counts": tag_counts,
        "tracks": tracks,
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", type=Path, default=LIKED_SONGS_PATH,
                        help=f"Liked Songs export to read (default: {LIKED_SONGS_PATH})")
    parser.add_argument("--only", choices=("bpm", "tags"), default=None,
                        help="Run just one step: 'bpm' (GetSongBPM) or 'tags' (Last.fm). Default: both.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Look up at most N uncached tracks per step this run (useful for a first test).")
    parser.add_argument("--retry-unmatched", action="store_true",
                        help="Look up tracks that previously had no match/weren't found again.")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Don't call any API; just rebuild library_enriched.json from the caches.")
    return parser.parse_args(argv)


def _api_key(name: str) -> str | None:
    load_dotenv(ENV_PATH)
    key = os.getenv(name, "").strip()
    return None if not key or key.startswith("your-") else key


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        library = load_library(args.input)
    except EnrichError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    cache_file = load_json(BPM_CACHE_PATH, {"results": {}, "recent_requests": []})
    cache: dict = cache_file.setdefault("results", {})
    unmatched: dict = load_json(UNMATCHED_PATH, {})
    by_isrc = unique_by_isrc(library["tracks"])
    missing_isrc = sum(1 for t in library["tracks"] if not t.get("isrc"))

    todo = [
        isrc for isrc in by_isrc
        if isrc not in cache and (args.retry_unmatched or isrc not in unmatched)
    ]
    if args.limit is not None:
        todo = todo[: args.limit]

    print(f"{len(library['tracks'])} tracks, {len(by_isrc)} unique ISRCs (BPM/key: "
          f"{len(by_isrc) - len(todo)} already cached or known unmatched, {len(todo)} to look up).")
    if missing_isrc:
        print(f"{missing_isrc} tracks have no ISRC and will show as 'no data'.")

    import tag_enrichment

    lastfm_cache = load_json(LASTFM_CACHE_PATH, tag_enrichment.empty_cache())
    stopped_early: str | None = None
    missing_key = False

    # Step 1: BPM/key from GetSongBPM.
    if args.only in (None, "bpm") and todo and not args.no_fetch:
        api_key = _api_key("GETSONGBPM_API_KEY")
        if api_key:
            print("\n== BPM/key (GetSongBPM) ==")
            limiter = RateLimiter(cache_file.get("recent_requests", []))
            sources = build_sources(api_key, limiter)
            stopped_early = run_lookups(todo, by_isrc, sources, cache, unmatched, cache_file, limiter)
        else:
            missing_key = True
            print("Skipping BPM/key: GETSONGBPM_API_KEY is not set in .env (see README.md).",
                  file=sys.stderr)

    # Step 2: mood/genre tags from Last.fm.
    if args.only in (None, "tags") and not args.no_fetch and not stopped_early:
        api_key = _api_key("LASTFM_API_KEY")
        if api_key:
            print("\n== Mood/genre tags (Last.fm) ==")
            stopped_early = tag_enrichment.run_tag_lookups(
                library["tracks"], lastfm_cache, api_key, args.limit, args.retry_unmatched)
        else:
            missing_key = True
            print("Skipping tags: LASTFM_API_KEY is not set in .env (see README.md).", file=sys.stderr)

    enriched = build_enriched_library(library, cache, unmatched, args.input, lastfm_cache)
    save_json(ENRICHED_LIBRARY_PATH, enriched)

    counts = enriched["status_counts"]
    tag_counts = enriched["tag_status_counts"]
    unique_enriched = sum(1 for isrc in by_isrc if isrc in cache)
    print()
    print("Enrichment summary")
    print(f"  Unique tracks with BPM/key: {unique_enriched} / {len(by_isrc)}")
    print(f"  Library tracks (BPM/key):   {counts['enriched']} enriched, "
          f"{counts['unmatched']} no match, {counts['pending']} not looked up yet")
    print(f"  Library tracks (tags):      {tag_counts['tagged']} tagged, "
          f"{tag_counts['no_tags']} no usable tags, {tag_counts['pending']} not looked up yet")
    print(f"  Enriched library:           {ENRICHED_LIBRARY_PATH}")
    print(f"  Caches:                     {BPM_CACHE_PATH.name}, {UNMATCHED_PATH.name}, "
          f"{LASTFM_CACHE_PATH.name} (in {BPM_CACHE_PATH.parent})")
    # Asking for one step explicitly without its key is an error; otherwise a
    # missing key just skips that step.
    if missing_key and args.only:
        return 1
    if stopped_early:
        print(f"\nStopped early: {stopped_early}", file=sys.stderr)
        return 1
    return 0


def run_lookups(todo, by_isrc, sources, cache, unmatched, cache_file, limiter) -> str | None:
    """Look up each ISRC in `todo`, updating cache/unmatched in place.

    Returns a reason string if the run had to stop early, else None.
    """
    interactive = sys.stdout.isatty()
    total_unique = len(by_isrc)
    matched = unmatched_count = errors = consecutive_failures = 0

    def save():
        cache_file["recent_requests"] = list(limiter.timestamps)
        save_json(BPM_CACHE_PATH, cache_file)
        save_json(UNMATCHED_PATH, unmatched)

    try:
        for done, isrc in enumerate(todo, 1):
            track = by_isrc[isrc]
            try:
                result = None
                for source in sources:
                    result = source.lookup(track)
                    if result:
                        break
            except TransientError:
                errors += 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    return (f"{consecutive_failures} lookups in a row failed; GetSongBPM may be "
                            "down. Run again later; finished lookups are saved.")
                continue
            consecutive_failures = 0
            if result:
                cache[isrc] = result
                unmatched.pop(isrc, None)
                matched += 1
            else:
                unmatched[isrc] = {
                    "track_name": track.get("track_name"),
                    "artists": [a.get("name") for a in track.get("artists") or []],
                    "album": (track.get("album") or {}).get("name"),
                    "sources_tried": [s.name for s in sources],
                    "looked_up_at": now_iso(),
                }
                unmatched_count += 1
            if done % SAVE_EVERY == 0:
                save()
            line = (f"Enriched {len(cache)} / {total_unique} unique tracks... "
                    f"(this run: {done}/{len(todo)} looked up, {matched} matched, "
                    f"{unmatched_count} no match{f', {errors} errors' if errors else ''})")
            if interactive:
                print(f"\r{line}", end="", flush=True)
            elif done % 25 == 0 or done == len(todo):  # e.g. output redirected to a log
                print(line, flush=True)
    except KeyboardInterrupt:
        return "interrupted (Ctrl+C). Finished lookups are saved; run again to continue."
    except EnrichError as error:
        return str(error)
    finally:
        save()
        if interactive:
            print()
    if errors:
        print(f"{errors} lookups failed with network/server errors; they'll be retried next run.")
    return None


if __name__ == "__main__":
    sys.exit(main())
