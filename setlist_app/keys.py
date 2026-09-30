"""Musical key parsing and Camelot wheel notation.

Camelot numbers the 24 keys around a wheel: minor keys are "A", major keys
are "B" (8A = A minor, 8B = C major). Neighbouring numbers are a fifth apart,
and the same number with the other letter is the relative major/minor.
"""

from __future__ import annotations

import re

_NOTE_PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# Conventional spelling for each Camelot key, as DJ software shows them.
CAMELOT_TO_KEY = {
    "1A": "Abm", "2A": "Ebm", "3A": "Bbm", "4A": "Fm", "5A": "Cm", "6A": "Gm",
    "7A": "Dm", "8A": "Am", "9A": "Em", "10A": "Bm", "11A": "F#m", "12A": "C#m",
    "1B": "B", "2B": "F#", "3B": "Db", "4B": "Ab", "5B": "Eb", "6B": "Bb",
    "7B": "F", "8B": "C", "9B": "G", "10B": "D", "11B": "A", "12B": "E",
}

_KEY_RE = re.compile(
    r"^\s*([A-Ga-g])\s*([#♯b♭]?)\s*(m|min|minor|maj|major)?\s*$"
)
_OPEN_KEY_RE = re.compile(r"^\s*(\d{1,2})\s*([mdMD])\s*$")


def key_to_camelot(key: str | None) -> str | None:
    """'Em' -> '9A', 'F#' -> '2B', 'Bbm' -> '3A'. None if unparseable."""
    if not key:
        return None
    match = _KEY_RE.match(key)
    if not match:
        return None
    letter, accidental, quality = match.groups()
    pitch = _NOTE_PITCH[letter.upper()]
    if accidental in ("#", "♯"):
        pitch += 1
    elif accidental in ("b", "♭"):
        pitch -= 1
    pitch %= 12
    minor = bool(quality) and quality.lower().startswith("m") and quality.lower() not in ("maj", "major")
    # Each step round the wheel is a fifth (7 semitones); these offsets pin
    # A minor to 8A and C major to 8B.
    number = (pitch * 7 + (5 if minor else 8)) % 12 or 12
    return f"{number}{'A' if minor else 'B'}"


def open_key_to_camelot(open_key: str | None) -> str | None:
    """Open Key notation (Traktor, used by GetSongBPM) -> Camelot.

    Open Key puts C major at 1d and A minor at 1m, i.e. 7 steps from Camelot.
    """
    if not open_key:
        return None
    match = _OPEN_KEY_RE.match(open_key)
    if not match:
        return None
    number, mode = int(match.group(1)), match.group(2).lower()
    if not 1 <= number <= 12:
        return None
    camelot_number = (number + 6) % 12 + 1
    return f"{camelot_number}{'A' if mode == 'm' else 'B'}"
