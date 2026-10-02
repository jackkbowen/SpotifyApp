"""One normalised profile per song, built from every genre/tag source, plus a
genre hierarchy and families derived from the library itself.

Inputs are the per-track `tags` (Last.fm) and `genres` (Deezer + MusicBrainz)
already in library_enriched.json. Nothing here calls an API and there is no
hand-written genre list; the only outside vocabulary is MusicBrainz's official
genre list, fetched once by genre_enrichment.py and passed in.

Per track (`profile`):
  styles    genre/style words, merged across sources, strongest first
  vibes     everything else descriptive: places, eras, moods
  families  top-level families (e.g. electronic, hip hop), weights propagated
            up the hierarchy
  primary_style / primary_family

Library-wide (`taxonomy`): the hierarchy ("riddim" is a kind of "dubstep" is a
kind of "bass music"...) with a confidence for every link, the families, and
how every word was classified, so each decision can be inspected.

How things are decided (all thresholds are constants below):

  * Spelling variants merge ("future-bass" / "futurebass" / "Future Bass").
  * Sources combine by noisy-or: two independent 50s make a 75, so agreement
    raises confidence without ever exceeding 100.
  * A word is a *style* if it is an official MusicBrainz genre, or Deezer or
    MusicBrainz used it as a genre for some song, or it ends in a known style
    ("west coast rap", "massive dubstep"). Otherwise it is a *vibe*. Place and
    era words can't be told from genre words statistically (artists from one
    region cluster with one genre), so this is deliberately a vocabulary test.
    Where it gets one wrong ("riddim" isn't an official genre), a hand-edited
    data/taxonomy_overrides.json forces words either way (see load_overrides).
  * Parent links by subsumption: B is a parent of A when most songs that have
    A also have B and B is much more common. Parents are often implied rather
    than tagged (only ~half of dubstep songs say "electronic"), so the bar is
    modest, and near-equal terms ("electronic" / "dance") stay siblings.
  * Families by coverage, most common style first: a style joins an existing
    family if at least half its songs are already in it, otherwise it starts a
    new family. Synonyms ("hip hop" / "rap") therefore collapse into one family
    automatically, and a style can belong to up to two families.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from .tags import display_form, tag_key

PRESENCE_WEIGHT = 20          # a word "counts" on a song from this fused weight up
MIN_SUPPORT = 4               # songs a style needs before a parent can be inferred for it
PARENT_CONFIDENCE = 0.6       # share of A's songs that must also have B for B to be A's parent
PARENT_SIZE_RATIO = 1.5       # ...and B must be at least this much more common (else they're siblings)
MAX_PARENTS = 3
FAMILY_MIN_FRACTION = 0.01    # a style can start a family if on >= 1% of songs...
FAMILY_MIN_TRACKS = 10        # ...and at least this many
FAMILY_COVER = 0.5            # a style belongs to a family if >= half its songs are in it
MAX_FAMILIES_PER_STYLE = 2
FAMILY_NAME_RATIO = 0.6       # an official genre this common as the seed can name the family
MIN_VIBE_SUPPORT = 2          # vibe words seen on a single song are noise
MAX_STYLES = 15
MAX_VIBES = 8

_STYLE, _VIBE = "style", "vibe"


def _source_family(source: str) -> str:
    if source == "deezer":
        return "deezer"
    if source.startswith("musicbrainz"):
        return "musicbrainz"
    return "lastfm"  # Last.fm track- and artist-level tags


def _noisy_or(weights) -> int:
    """Combine independent evidence: 50 + 50 -> 75."""
    remaining = 1.0
    for w in weights:
        remaining *= 1 - min(max(w, 0), 100) / 100
    return round(100 * (1 - remaining))


def _track_words(track: dict) -> dict[str, dict[str, int]]:
    """{word key: {source family: weight}} for one track, spellings merged."""
    words: dict[str, dict[str, int]] = defaultdict(dict)

    def add(word: str, family: str, weight: int) -> None:
        key = tag_key(display_form(word))
        if key:
            words[key][family] = max(words[key].get(family, 0), weight)

    for tag in track.get("tags") or []:
        add(tag["tag"], "lastfm", tag.get("weight", 0))
    for genre in track.get("genres") or []:
        by_source = genre.get("by_source") or {_source_family(s): genre.get("weight", 0) for s in genre.get("sources", [])}
        for family, weight in by_source.items():
            add(genre["genre"], _source_family(family), weight)
    return words


def _best_display(spellings: Counter) -> str:
    # Most common spelling; ties prefer the spaced form ("future bass").
    return max(spellings.items(), key=lambda kv: (kv[1], " " in kv[0], kv[0]))[0]


def _ends_with_style(display: str, style_keys: set[str]) -> bool:
    words = display.split()
    if len(words) < 2:
        return False
    # display_form so spelling aliases apply to the tail too ("rnb" -> "r and b").
    return any(tag_key(display_form(" ".join(words[-n:]))) in style_keys for n in (1, 2, 3) if n < len(words))


def load_overrides(path: Path) -> tuple[dict, str | None]:
    """Read the hand-edited override file: {"styles": [words], "vibes": [words]}.

    "styles" are words the automatic rules called vibes but are really genres;
    "vibes" are the reverse. Case and spelling don't matter; other keys (like a
    "_help" note) are ignored, and a word in both lists counts as a vibe. A
    missing file is fine; an unreadable one returns ({}, message) so the caller
    can warn and carry on without it.
    """
    if not path.exists():
        return {}, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError('the top level must be an object like {"styles": [...], "vibes": [...]}')
        result = {}
        for field in ("styles", "vibes"):
            words = raw.get(field, [])
            if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
                raise ValueError(f'"{field}" must be a list of words')
            result[field] = [w.strip() for w in words if w.strip()]
        return result, None
    except (OSError, ValueError) as error:  # json.JSONDecodeError is a ValueError
        return {}, f"{path.name}: {error}"


def build(tracks: list[dict], official_genres: list[str] | None = None,
          overrides: dict | None = None, overrides_error: str | None = None) -> tuple[dict, list[dict]]:
    """Returns (taxonomy, profiles): the library-wide taxonomy and one profile
    per track, in order. Pure and deterministic."""
    official = {tag_key(display_form(g)) for g in official_genres or []}
    total = len(tracks)
    overrides = overrides or {}
    listed = {field: {tag_key(display_form(w)): w for w in overrides.get(field, []) if tag_key(display_form(w))}
              for field in ("styles", "vibes")}
    force_style, force_vibe = set(listed["styles"]), set(listed["vibes"])

    # ---- 1. fuse every source into one weight per word per track -------------
    spellings: dict[str, Counter] = defaultdict(Counter)
    for track in tracks:
        for tag in track.get("tags") or []:
            spellings[tag_key(display_form(tag["tag"]))][display_form(tag["tag"])] += 1
        for genre in track.get("genres") or []:
            spellings[tag_key(display_form(genre["genre"]))][display_form(genre["genre"])] += 1
    spellings.pop("", None)
    display = {key: _best_display(counter) for key, counter in spellings.items()}

    per_track: list[dict[str, dict]] = []
    for track in tracks:
        fused = {}
        for key, by_source in _track_words(track).items():
            fused[key] = {"weight": _noisy_or(by_source.values()), "sources": sorted(by_source)}
        per_track.append(fused)

    # ---- 2. style or vibe? ------------------------------------------------------
    observed = {tag_key(display_form(g["genre"])) for t in tracks for g in t.get("genres") or []}
    style_keys = {k for k in display if k in official or k in observed or k in force_style}
    # Official genres count as heads even if no song uses them alone, and so do
    # forced styles ("massive riddim" once "riddim" is forced); forced vibes don't.
    known = (official | observed | force_style) - force_vibe
    for key in sorted(display):  # compounds: "west coast rap" ends in the style "rap"
        if key not in style_keys and _ends_with_style(display[key], known | (style_keys - force_vibe)):
            style_keys.add(key)
    style_keys -= force_vibe  # an explicit "this is a vibe" beats everything above

    seen_on = Counter(key for fused in per_track for key in fused)
    vibe_keys = {k for k in display if k not in style_keys and (seen_on[k] >= MIN_VIBE_SUPPORT or k in force_vibe)}

    # ---- 3. hierarchy by subsumption ---------------------------------------------
    present = [{k for k, v in fused.items() if k in style_keys and v["weight"] >= PRESENCE_WEIGHT} for fused in per_track]
    n = Counter(k for s in present for k in s)
    supported = {k for k, c in n.items() if c >= MIN_SUPPORT}
    together: Counter = Counter()
    for s in present:
        keys = sorted(k for k in s if k in supported)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                together[(a, b)] += 1

    candidates: dict[str, dict[str, float]] = defaultdict(dict)  # child -> {parent: confidence}
    for (a, b), both in together.items():
        for child, parent in ((a, b), (b, a)):
            confidence = both / n[child]
            if n[parent] >= PARENT_SIZE_RATIO * n[child] and confidence >= PARENT_CONFIDENCE:
                candidates[child][parent] = confidence

    def ancestors(key: str, seen: set[str] | None = None) -> set[str]:
        seen = set() if seen is None else seen
        for parent in candidates.get(key, {}):
            if parent not in seen:
                seen.add(parent)
                ancestors(parent, seen)
        return seen

    parents: dict[str, dict[str, float]] = {}
    for child, options in candidates.items():
        # Drop a parent when another candidate is already a descendant of it
        # (keep "dubstep", not also "electronic", as riddim's parent).
        direct = {p: c for p, c in options.items()
                  if not any(p in ancestors(other) for other in options if other != p)}
        parents[child] = dict(sorted(direct.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_PARENTS])

    # ---- 4. families by coverage ---------------------------------------------------
    songs_with: dict[str, set[int]] = defaultdict(set)
    for index, s_ in enumerate(present):
        for key in s_:
            songs_with[key].add(index)
    family_min = max(FAMILY_MIN_TRACKS, round(FAMILY_MIN_FRACTION * total))
    seeds: list[dict] = []  # {"seed": key, "members": [keys]}
    for key in sorted((k for k in songs_with if n[k] >= family_min), key=lambda k: (-n[k], k)):
        best = max(((len(songs_with[key] & songs_with[f["seed"]]) / n[key], f) for f in seeds),
                   key=lambda pair: pair[0], default=(0, None))
        if best[1] is not None and best[0] >= FAMILY_COVER:
            best[1]["members"].append(key)
        else:
            seeds.append({"seed": key, "members": [key]})

    def family_name(family: dict) -> str:
        # Prefer an official genre nearly as common as the seed ("electronic"
        # over Deezer's "dance"), else the most common member.
        seed_n = n[family["seed"]]
        official_members = [k for k in family["members"] if k in official and n[k] >= FAMILY_NAME_RATIO * seed_n]
        return display[max(official_members, key=lambda k: (n[k], k))] if official_members else display[family["seed"]]

    names = {f["seed"]: family_name(f) for f in seeds}
    family_songs = {f["seed"]: set().union(*(songs_with[k] for k in f["members"])) for f in seeds}
    families_of: dict[str, list[str]] = {}  # style key -> family seed keys, best first
    for key in songs_with:
        shares = sorted(((len(songs_with[key] & family_songs[f["seed"]]) / n[key], f["seed"]) for f in seeds),
                        key=lambda pair: (-pair[0], pair[1]))
        chosen = [seed for share, seed in shares if share >= FAMILY_COVER][:MAX_FAMILIES_PER_STYLE]
        if key in family_songs and key not in chosen:  # a seed always belongs to its own family
            chosen = [key] + chosen[:MAX_FAMILIES_PER_STYLE - 1]
        if chosen:
            families_of[key] = chosen

    # ---- 5. per-track profiles -------------------------------------------------------
    profiles = []
    for track, fused in zip(tracks, per_track):
        styles = sorted(
            ({"style": display[k], "weight": v["weight"], "sources": v["sources"]}
             for k, v in fused.items() if k in style_keys and v["weight"] > 0),
            key=lambda x: (-x["weight"], x["style"]))[:MAX_STYLES]
        vibes = sorted(
            ({"vibe": display[k], "weight": v["weight"], "sources": v["sources"]}
             for k, v in fused.items() if k in vibe_keys and v["weight"] > 0),
            key=lambda x: (-x["weight"], x["vibe"]))[:MAX_VIBES]
        family_weight: dict[str, int] = {}
        for k, v in fused.items():
            if k in style_keys:
                for family in families_of.get(k, ()):
                    family_weight[family] = max(family_weight.get(family, 0), v["weight"])
        families = sorted(({"family": names[f], "weight": w} for f, w in family_weight.items() if w > 0),
                          key=lambda x: (-x["weight"], x["family"]))
        waiting = track.get("tag_status") == "pending" and track.get("genre_status") == "pending"
        profiles.append({
            "status": "pending" if waiting else ("profiled" if styles or vibes else "no_data"),
            "complete": track.get("tag_status") != "pending" and track.get("genre_status") != "pending",
            "styles": styles,
            "vibes": vibes,
            "families": families,
            "primary_style": styles[0]["style"] if styles else None,
            "primary_family": families[0]["family"] if families else None,
        })

    # ---- 6. the library-wide taxonomy, for inspection and for the UI -------------------
    style_info = {}
    for key in sorted(style_keys, key=lambda k: (-n[k], k)):
        if seen_on[key] == 0:
            continue
        style_info[display[key]] = {
            "tracks": n[key],
            "parents": [{"parent": display[p], "confidence": round(c, 2)} for p, c in parents.get(key, {}).items()],
            "families": [names[f] for f in families_of.get(key, ())],
        }
    taxonomy = {
        "track_count": total,
        "genre_vocabulary": f"MusicBrainz official list ({len(official)} genres) + genres seen in this library"
                            if official else "genres seen in this library only (MusicBrainz genre list not fetched yet)",
        "settings": {
            "presence_weight": PRESENCE_WEIGHT, "min_support": MIN_SUPPORT,
            "parent_confidence": PARENT_CONFIDENCE, "parent_size_ratio": PARENT_SIZE_RATIO,
            "family_min_tracks": family_min, "min_vibe_support": MIN_VIBE_SUPPORT,
        },
        "overrides": {
            "error": overrides_error,
            **{field: {"listed": len(words), "applied": sum(1 for k in words if k in display),
                       "not_in_library": sorted(w for k, w in words.items() if k not in display)}
               for field, words in listed.items()},
        },
        "families": [
            {"family": names[f["seed"]], "tracks": len(family_songs[f["seed"]]), "seed": display[f["seed"]],
             "members": [display[k] for k in sorted(f["members"], key=lambda k: (-n[k], k))[:12]]}
            for f in sorted(seeds, key=lambda f: (-len(family_songs[f["seed"]]), f["seed"]))],
        "styles": style_info,
        "vibes": [{"vibe": display[k], "tracks": seen_on[k]} for k in sorted(vibe_keys, key=lambda k: (-seen_on[k], k))],
    }
    return taxonomy, profiles
