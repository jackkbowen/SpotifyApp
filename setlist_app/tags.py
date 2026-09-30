"""Cleaning and merging crowdsourced Last.fm tags into per-track mood/genre tags.

The raw tags from Last.fm are cached untouched; everything here runs when
library_enriched.json is rebuilt, so the rules can be tuned without
refetching anything.

There is deliberately no list of allowed genres or moods: whatever survives
the junk filter becomes vocabulary, so the app reflects this library.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict

# Last.fm reports each tag's weight on a track/artist as 0-100 relative to its
# top tag. Below this it's usually one or two people's personal label.
MIN_TAG_COUNT = 5
# Artist tags describe an artist's whole catalogue, so they count for less
# than tags on the track itself.
ARTIST_TAG_FACTOR = 0.5
# Use artist tags when a track has fewer usable tags than this.
MIN_TRACK_TAGS = 3
MAX_TAGS_PER_TRACK = 12

# Non-descriptive tags: opinions, listening habits, personal bookkeeping.
_JUNK_EXACT = {
    "seen live", "favorite", "favorites", "favourite", "favourites", "fav", "favs",
    "awesome", "amazing", "love", "loved", "love it", "love at first listen", "best",
    "cool", "good", "great", "nice", "perfect", "epic", "wow", "genius", "masterpiece",
    "spotify", "youtube", "soundcloud", "albums i own", "owned", "wishlist",
    "check out", "to listen", "listen", "under 2000 listeners", "all", "music", "song",
    "songs", "tracks", "track", "other", "misc", "unknown", "none", "test",
    "male vocalists", "female vocalists", "male vocalist", "female vocalist",
    "vocal", "vocals", "singer", "singer songwriter", "cover", "covers",
    "single", "radio", "billboard", "hits", "hit", "top 40", "new", "2000s hits",
}
_JUNK_PATTERN = re.compile(
    r"favou?rite|\bfavs?\b|seen live|\bmy\b|\bi\b|\bme\b|\bmine\b|\bi'?m\b|to buy|"
    r"check out|\bown(ed)?\b|\blisten(ed|ing)?\b|\bstars?\b|\d+ ?/ ?10|\bspotify\b|"
    r"\bplaylist\b|\bfuck|\bshit\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"^(19|20)\d\d$")

# Abbreviations that tokenising alone can't merge with their long form.
_ALIASES = {
    "dnb": "drum and bass",
    "d and b": "drum and bass",
    "drum n bass": "drum and bass",
    "drum and bass": "drum and bass",
    "rnb": "r and b",
    "edm": "edm",
    "idm": "idm",
}


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return folded if folded.strip() else text


def display_form(raw: str) -> str:
    """'Future-Bass' -> 'future bass', 'Drum & Bass' -> 'drum and bass'."""
    tag = _fold(raw).lower().strip()
    tag = tag.replace("&", " and ").replace("'n'", " and ").replace(" 'n ", " and ")
    tag = re.sub(r"[_\-/]+", " ", tag)
    tag = re.sub(r"[^\w\s']", "", tag)
    tag = re.sub(r"\s+", " ", tag).strip()
    return _ALIASES.get(tag, tag)


def tag_key(display: str) -> str:
    """Spacing-insensitive identity: 'future bass' == 'futurebass'."""
    return display.replace(" ", "").replace("'", "")


def _names_key(text: str | None) -> str:
    return tag_key(display_form(text or ""))


def is_junk(display: str, context_keys: set[str]) -> bool:
    if len(display) < 2 or len(display) > 30 or len(display.split()) > 4:
        return True
    if display in _JUNK_EXACT or _JUNK_PATTERN.search(display) or _YEAR.match(display):
        return True
    if display.replace(" ", "").isdigit():
        return True
    key = tag_key(display)
    # The artist's own name, the track title, or album name used as a tag.
    # Exact match only: an artist called "Bass" mustn't wipe out "bass house".
    return key in context_keys


class TagVocabulary:
    """Chooses one display spelling per tag across the whole library, so
    'futurebass' and 'future-bass' both show up as 'future bass'."""

    def __init__(self, raw_tag_names):
        spellings: dict[str, Counter] = defaultdict(Counter)
        for raw in raw_tag_names:
            display = display_form(raw)
            spellings[tag_key(display)][display] += 1
        self.best = {
            key: max(counter.items(), key=lambda kv: (kv[0].count(" "), kv[1], kv[0]))[0]
            for key, counter in spellings.items()
        }

    def canonical(self, raw: str) -> tuple[str, str]:
        display = display_form(raw)
        key = tag_key(display)
        return key, self.best.get(key, display)


def clean_tags(raw_tags, vocab: TagVocabulary, context_keys: set[str], factor: float = 1.0) -> dict[str, dict]:
    """[{'name', 'count'}] from Last.fm -> {key: {'tag', 'weight'}} with junk removed
    and variants merged (keeping the highest weight)."""
    cleaned: dict[str, dict] = {}
    for raw in raw_tags or []:
        try:
            count = int(raw.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count < MIN_TAG_COUNT or not raw.get("name"):
            continue
        key, display = vocab.canonical(raw["name"])
        if not key or is_junk(display, context_keys):
            continue
        weight = round(count * factor)
        if weight > cleaned.get(key, {}).get("weight", -1):
            cleaned[key] = {"tag": display, "weight": weight}
    return cleaned


def context_keys_for(track: dict) -> set[str]:
    names = [a.get("name") for a in track.get("artists") or []]
    names += [track.get("track_name"), (track.get("album") or {}).get("name")]
    return {k for k in (_names_key(n) for n in names) if k}


def merge_track_tags(track: dict, track_raw, artist_raws: list, vocab: TagVocabulary) -> list[dict]:
    """Final per-track tag list, strongest first: [{'tag', 'weight', 'source'}].

    Track-level tags are used as-is; artist tags are only mixed in (at half
    weight) when the track has fewer than MIN_TRACK_TAGS usable tags, which is
    common for new or niche releases.
    """
    context = context_keys_for(track)
    merged = {k: {**v, "source": "track"} for k, v in clean_tags(track_raw, vocab, context).items()}
    if len(merged) < MIN_TRACK_TAGS:
        for artist_raw in artist_raws:
            for key, value in clean_tags(artist_raw, vocab, context, ARTIST_TAG_FACTOR).items():
                if key not in merged or value["weight"] > merged[key]["weight"]:
                    merged[key] = {**value, "source": "artist"}
    ordered = sorted(merged.values(), key=lambda t: (-t["weight"], t["tag"]))
    return ordered[:MAX_TAGS_PER_TRACK]
