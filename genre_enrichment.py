"""Genres from Deezer and MusicBrainz (run via `enrich_library.py --only genres`).

Both are looked up by ISRC, so they're exact (no name matching), and both are
free and keyless:

  * Deezer gives the album's genre(s): coarse families such as "Electro",
    "Dance", "Rap/Hip Hop". About 98% of this library is found.
  * MusicBrainz gives community-voted, fine-grained genres for the recording
    (falling back to its release group), e.g. "dubstep", "2-step",
    "electro house", "trap". Roughly half the library has these.

Raw responses are cached in data/genre_cache.json; merging, spelling
normalisation and weighting happen when library_enriched.json is rebuilt, so
the rules can change without refetching. Nothing here is a fixed genre list.

MusicBrainz asks for at most one request per second and a meaningful
User-Agent, so the first run over ~1,400 songs takes about an hour (it can be
interrupted and resumed). Optionally set MUSICBRAINZ_CONTACT in .env (an email
or URL) to be identified to them; it's only used for that.
"""

from __future__ import annotations

import os
import random
import sys
import time

import requests
from dotenv import load_dotenv

from enrich_library import EnrichError, TransientError, now_iso, save_json, unique_by_isrc
from setlist_app.paths import ENV_PATH, GENRE_CACHE_PATH
from setlist_app.tags import TagVocabulary

DEEZER_API_URL = "https://api.deezer.com"
MUSICBRAINZ_API_URL = "https://musicbrainz.org/ws/2"
DEEZER_MIN_SECONDS = 0.15       # Deezer allows ~50 requests per 5 seconds
MUSICBRAINZ_MIN_SECONDS = 1.1   # MusicBrainz: at most 1 request per second
REQUEST_TIMEOUT_SECONDS = 25
RETRIES = 4
MAX_CONSECUTIVE_FAILURES = 10
SAVE_EVERY = 10

# Weighting when merging (all weights are 0-100, like the Last.fm tags):
DEEZER_WEIGHT = 50              # coarse family; present for almost everything
RELEASE_GROUP_FACTOR = 0.7      # an album's genres describe a song less than its own
MAX_GENRES_PER_TRACK = 15
MAX_MB_TAGS_STORED = 10

_DEEZER_NO_DATA = 800
_DEEZER_QUOTA = 4


def empty_cache() -> dict:
    return {
        "deezer": {"tracks": {}, "albums": {}},
        "musicbrainz": {"tracks": {}, "release_groups": {}},
    }


def normalize_cache(cache: dict) -> dict:
    cache.setdefault("deezer", {}).setdefault("tracks", {})
    cache["deezer"].setdefault("albums", {})
    cache.setdefault("musicbrainz", {}).setdefault("tracks", {})
    cache["musicbrainz"].setdefault("release_groups", {})
    # MusicBrainz's official genre list ({"fetched_at", "names"}), used by the
    # taxonomy to tell genre words from everything else. None until fetched.
    cache["musicbrainz"].setdefault("genre_list", None)
    return cache


def official_genre_names(cache: dict) -> list[str]:
    """MusicBrainz's official genre names, or [] if the list hasn't been fetched."""
    return list(((cache.get("musicbrainz") or {}).get("genre_list") or {}).get("names") or [])


# --------------------------------------------------------------------------- #
# HTTP clients
# --------------------------------------------------------------------------- #

class _Client:
    name = "?"
    min_gap = 1.0

    def __init__(self, user_agent: str):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self.last_request = 0.0

    def _throttle(self) -> None:
        gap = self.last_request + self.min_gap - time.time()
        if gap > 0:
            time.sleep(gap)
        self.last_request = time.time()

    def _is_rate_limited(self, data) -> bool:
        return False

    def get(self, url: str, params: dict | None = None, as_text: bool = False):
        """Parsed JSON (or the raw text with as_text=True), or None when the
        server says the thing doesn't exist (404)."""
        problem = "unknown error"
        for attempt in range(1, RETRIES + 1):
            self._throttle()
            retry_after = None
            try:
                response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            except requests.RequestException as error:
                problem = f"network error ({error.__class__.__name__})"
            else:
                if response.status_code == 404:
                    return None
                if response.status_code in (429, 503) or response.status_code >= 500:
                    problem = "rate limited" if response.status_code in (429, 503) else f"HTTP {response.status_code}"
                    try:
                        retry_after = float(response.headers.get("Retry-After"))
                    except (TypeError, ValueError):
                        retry_after = None
                elif response.status_code != 200:
                    raise TransientError(f"HTTP {response.status_code}")
                elif as_text:
                    return response.text
                else:
                    try:
                        data = response.json()
                    except ValueError:
                        problem = "non-JSON response"
                    else:
                        if not self._is_rate_limited(data):
                            return data
                        problem = "rate limited"
            if attempt < RETRIES:
                wait = min(retry_after or 5 * 2 ** (attempt - 1), 120) + random.uniform(0, 2)
                print(f"\n{self.name}: {problem}; retrying in {wait:.0f}s...", flush=True)
                time.sleep(wait)
        raise TransientError(f"{self.name}: {problem}")


class DeezerClient(_Client):
    name = "Deezer"
    min_gap = DEEZER_MIN_SECONDS

    def __init__(self):
        super().__init__("SpotifySetlistBuilder/1.0 (personal, local use)")

    def _is_rate_limited(self, data) -> bool:
        error = data.get("error") if isinstance(data, dict) else None
        return isinstance(error, dict) and error.get("code") == _DEEZER_QUOTA

    def track(self, isrc: str) -> dict | None:
        data = self.get(f"{DEEZER_API_URL}/track/isrc:{isrc}")
        if not isinstance(data, dict) or "error" in data or not data.get("id"):
            return None  # "no data" (800) and anything else unusable
        return data

    def album_genres(self, album_id) -> list[str]:
        data = self.get(f"{DEEZER_API_URL}/album/{album_id}")
        genres = ((data or {}).get("genres") or {}).get("data") or []
        return [g["name"] for g in genres if isinstance(g, dict) and g.get("name")]


class MusicBrainzClient(_Client):
    name = "MusicBrainz"
    min_gap = MUSICBRAINZ_MIN_SECONDS

    def __init__(self):
        load_dotenv(ENV_PATH)
        contact = os.getenv("MUSICBRAINZ_CONTACT", "").strip()
        agent = "SpotifySetlistBuilder/1.0" + (f" ( {contact} )" if contact else " (personal, local use)")
        super().__init__(agent)

    def _get(self, path: str, **params):
        return self.get(f"{MUSICBRAINZ_API_URL}/{path}", {**params, "fmt": "json"})

    def recording_ids(self, isrc: str) -> list[str]:
        data = self._get(f"isrc/{isrc}")
        return [r["id"] for r in (data or {}).get("recordings", []) if r.get("id")]

    def recording(self, recording_id: str) -> dict | None:
        # inc values are space separated (requests turns that into "+").
        return self._get(f"recording/{recording_id}", inc="genres tags releases release-groups")

    def release_group(self, release_group_id: str) -> dict | None:
        return self._get(f"release-group/{release_group_id}", inc="genres tags")

    def all_genres(self) -> list[str]:
        """Every official MusicBrainz genre name (one request; text format only)."""
        text = self.get(f"{MUSICBRAINZ_API_URL}/genre/all", {"fmt": "txt"}, as_text=True)
        return sorted({line.strip() for line in (text or "").splitlines() if line.strip()})


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #

def _vote_list(items) -> list[dict]:
    """[{'name', 'count'}] from a MusicBrainz genres/tags list."""
    cleaned = []
    for item in items or []:
        if isinstance(item, dict) and item.get("name"):
            cleaned.append({"name": item["name"], "count": int(item.get("count") or 0)})
    return cleaned


def _pick_release_group(recording: dict) -> str | None:
    """First release group that isn't a compilation (compilations describe
    the compiler, not the song), else the first one."""
    groups = [r["release-group"] for r in recording.get("releases", []) if r.get("release-group", {}).get("id")]
    for group in groups:
        if "Compilation" not in (group.get("secondary-types") or []):
            return group["id"]
    return groups[0]["id"] if groups else None


def _lookup_deezer(client: DeezerClient, cache: dict, isrc: str) -> bool:
    deezer = cache["deezer"]
    track = client.track(isrc)
    if track is None:
        deezer["tracks"][isrc] = {"found": False, "fetched_at": now_iso()}
        return False
    album_id = str((track.get("album") or {}).get("id") or "")
    if album_id and album_id not in deezer["albums"]:
        deezer["albums"][album_id] = client.album_genres(album_id)
    deezer["tracks"][isrc] = {
        "found": True, "album_id": album_id,
        "genres": deezer["albums"].get(album_id, []), "fetched_at": now_iso(),
    }
    return True


def _lookup_musicbrainz(client: MusicBrainzClient, cache: dict, isrc: str) -> bool:
    mb = cache["musicbrainz"]
    recording_ids = client.recording_ids(isrc)
    entry = {"found": bool(recording_ids), "fetched_at": now_iso()}
    # Usually one recording per ISRC. Only if the first has no genres anywhere
    # is a second one worth the extra requests.
    for recording_id in recording_ids[:2]:
        recording = client.recording(recording_id) or {}
        genres, tags = _vote_list(recording.get("genres")), _vote_list(recording.get("tags"))
        group_id = _pick_release_group(recording)
        group_genres: list[dict] = []
        if group_id:
            if group_id not in mb["release_groups"]:
                group = client.release_group(group_id) or {}
                mb["release_groups"][group_id] = {
                    "genres": _vote_list(group.get("genres")),
                    "tags": _vote_list(group.get("tags"))[:MAX_MB_TAGS_STORED],
                }
            group_genres = mb["release_groups"][group_id]["genres"]
        entry.update(recording_id=recording_id, release_group_id=group_id, genres=genres,
                     release_group_genres=group_genres, tags=tags[:MAX_MB_TAGS_STORED])
        if genres or group_genres:
            break
    mb["tracks"][isrc] = entry
    return bool(entry.get("genres") or entry.get("release_group_genres"))


def _run_step(label: str, todo: list[str], lookup, save) -> str | None:
    """Run `lookup(isrc) -> bool` over `todo` with progress, periodic saves and
    a stop after too many consecutive failures. Returns a stop reason or None."""
    interactive = sys.stdout.isatty()
    found = missing = errors = consecutive = 0
    try:
        for done, isrc in enumerate(todo, 1):
            try:
                ok = lookup(isrc)
            except TransientError:
                errors += 1
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    return (f"{consecutive} {label} lookups in a row failed; it may be down. "
                            "Run again later; finished lookups are saved.")
                continue
            consecutive = 0
            found += bool(ok)
            missing += not ok
            if done % SAVE_EVERY == 0:
                save()
            line = (f"{label}: {done}/{len(todo)} · {found} with genres, {missing} without"
                    f"{f', {errors} errors' if errors else ''}")
            if interactive:
                print(f"\r{line}", end="", flush=True)
            elif done % 25 == 0 or done == len(todo):
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
        print(f"{errors} {label} lookups failed with network/server errors; they'll be retried next run.")
    return None


def run_genre_lookups(tracks: list[dict], cache: dict, limit: int | None,
                      retry_not_found: bool) -> str | None:
    """Fetch Deezer then MusicBrainz genres for every unique ISRC not cached yet.

    Returns a reason string if the run stopped early, else None.
    """
    normalize_cache(cache)
    isrcs = list(unique_by_isrc(tracks))

    def save() -> None:
        save_json(GENRE_CACHE_PATH, cache)

    def pending(source: str) -> list[str]:
        known = cache[source]["tracks"]
        todo = [i for i in isrcs if i not in known or (retry_not_found and not _has_genres(source, known[i]))]
        return todo[:limit] if limit is not None else todo

    # MusicBrainz's official genre list: one request, used by the taxonomy to
    # recognise genre words. Failing to get it isn't fatal (the taxonomy falls
    # back to the genres it saw in your library).
    if cache["musicbrainz"].get("genre_list") is None or retry_not_found:
        try:
            names = MusicBrainzClient().all_genres()
        except TransientError as error:
            print(f"Couldn't fetch MusicBrainz's genre list ({error}); will try again next run.")
        else:
            if names:
                cache["musicbrainz"]["genre_list"] = {"fetched_at": now_iso(), "names": names}
                save()
                print(f"MusicBrainz genre list: {len(names)} official genres.")

    for source, label, make_client, lookup in (
        ("deezer", "Deezer", DeezerClient, _lookup_deezer),
        ("musicbrainz", "MusicBrainz", MusicBrainzClient, _lookup_musicbrainz),
    ):
        todo = pending(source)
        print(f"{label} genres: {len(isrcs)} unique ISRCs, {len(isrcs) - len(todo)} cached, {len(todo)} to look up"
              + (f" (about {len(todo) * 2.5 / 60:.0f} min at MusicBrainz's 1 request/second)" if source == "musicbrainz" and todo else "")
              + ".")
        if not todo:
            continue
        client = make_client()
        stopped = _run_step(label, todo, lambda isrc, c=client, f=lookup: f(c, cache, isrc), save)
        if stopped:
            return stopped
    return None


def _has_genres(source: str, entry: dict) -> bool:
    if source == "deezer":
        return bool(entry.get("genres"))
    return bool(entry.get("genres") or entry.get("release_group_genres"))


# --------------------------------------------------------------------------- #
# Merging into the per-track `genres` list
# --------------------------------------------------------------------------- #

def _split_deezer(name: str) -> list[str]:
    # Deezer joins some families with "/", e.g. "Rap/Hip Hop" -> rap, hip hop.
    return [part.strip() for part in name.split("/") if part.strip()]


def _vote_weights(items: list[dict], factor: float = 1.0) -> list[tuple[str, int]]:
    """Genre votes -> 0-100 weights relative to the entity's own most-voted genre."""
    if not items:
        return []
    top = max(max(i["count"], 1) for i in items)
    return [(i["name"], max(10, round(100 * max(i["count"], 1) / top * factor))) for i in items]


def genres_for_library(tracks: list[dict], cache: dict) -> list[tuple[str, list[dict]]]:
    """(genre_status, genres) per track, in order.

    genres: [{"genre", "weight" (0-100), "sources", "by_source"}], strongest
    first. Spelling variants are merged across the whole library (e.g. "r&b" /
    "R and B").
    genre_status: "genres", "no_genres" (looked up, nothing found) or
    "pending" (neither source has been asked yet).
    """
    normalize_cache(cache)
    deezer, mb = cache["deezer"]["tracks"], cache["musicbrainz"]["tracks"]

    raw_names = [part for entry in deezer.values() for name in entry.get("genres", []) for part in _split_deezer(name)]
    for entry in mb.values():
        raw_names += [g["name"] for g in entry.get("genres", []) + entry.get("release_group_genres", [])]
    vocab = TagVocabulary(raw_names)

    results = []
    for track in tracks:
        isrc = track.get("isrc")
        d_entry, m_entry = deezer.get(isrc), mb.get(isrc)
        if d_entry is None and m_entry is None:
            results.append(("pending", []))
            continue
        weighted: list[tuple[str, int, str]] = []
        for name in (d_entry or {}).get("genres", []):
            weighted += [(part, DEEZER_WEIGHT, "deezer") for part in _split_deezer(name)]
        weighted += [(n, w, "musicbrainz") for n, w in _vote_weights((m_entry or {}).get("genres", []))]
        weighted += [(n, w, "musicbrainz:album")
                     for n, w in _vote_weights((m_entry or {}).get("release_group_genres", []), RELEASE_GROUP_FACTOR)]

        merged: dict[str, dict] = {}
        for raw, weight, source in weighted:
            key, display = vocab.canonical(raw)
            if not key:
                continue
            slot = merged.setdefault(key, {"genre": display, "weight": 0, "sources": [], "by_source": {}})
            slot["weight"] = max(slot["weight"], weight)
            if source not in slot["sources"]:
                slot["sources"].append(source)
            # Each source's own weight (album-level MusicBrainz counts as
            # musicbrainz), so the taxonomy can combine sources properly.
            family = source.split(":")[0]
            slot["by_source"][family] = max(slot["by_source"].get(family, 0), weight)
        genres = sorted(merged.values(), key=lambda g: (-g["weight"], g["genre"]))[:MAX_GENRES_PER_TRACK]
        results.append(("genres" if genres else "no_genres", genres))
    return results
