"""Stage 1: candidate generation.

Three modes:

* ``all``: every combination of the chosen charset, shuffled. Large spaces
  (for example 4 characters over ``a-z0-9._``, about 2 million combinations) are
  sampled at random instead of materialised when a ``limit`` is given.
* ``pronounceable``: CVC (3 letters) and CVCV / VCVC / CCVC (4 letters) built
  from consonant and vowel slots that read naturally, so no ``xqzt``-style clusters.
* ``words``: real 3/4-letter English words from the bundled wordlist, plus common
  short first names and brand-style fragments.

Every mode dedupes its output, applies the Instagram rules from :mod:`rules`, and
can exclude names that are already checked (resume support).
"""

from __future__ import annotations

import csv
import itertools
import random
import string
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from importlib import resources
from pathlib import Path

from .rules import is_valid, normalize, violations


class Charset(StrEnum):
    """Character sets for ``--mode all``."""

    LETTERS = "letters"
    ALNUM = "alnum"
    ALNUM_SEP = "alnum_sep"


class Mode(StrEnum):
    """Generation strategies."""

    ALL = "all"
    PRONOUNCEABLE = "pronounceable"
    WORDS = "words"


CHARSET_CHARS: dict[Charset, str] = {
    Charset.LETTERS: string.ascii_lowercase,
    Charset.ALNUM: string.ascii_lowercase + string.digits,
    Charset.ALNUM_SEP: string.ascii_lowercase + string.digits + "._",
}

# --- Pronounceable building blocks -------------------------------------------------

VOWELS = "aeiou"
# Consonants that start a syllable cleanly. No "q" (needs a "u") and no "x".
ONSETS = "bcdfghjklmnprstvwyz"
# Consonants that sit comfortably between two vowels (ca-B-a, mi-L-o).
MEDIALS = "bcdfgjklmnprstvyz"
# Consonants that end a word cleanly (ca-T, bo-X, co-W).
CODAS = "bcdfgklmnprstwxyz"
# Two-consonant onsets that occur at the start of real English words.
ONSET_CLUSTERS: tuple[str, ...] = (
    "bl", "br", "ch", "cl", "cr", "dr", "fl", "fr", "gl", "gr", "kl", "kr", "ph",
    "pl", "pr", "sc", "sh", "sk", "sl", "sm", "sn", "sp", "st", "sw", "th", "tr",
    "tw", "wh",
)  # fmt: skip
# Vowel + glide endings that look odd in English ("kiw", "luy").
_AWKWARD_BIGRAMS = frozenset({"iw", "uw", "iy", "uy"})
# Offensive substrings kept out of pronounceable output. (``all`` mode is
# literally every combination, and the bundled wordlists are curated by hand.)
_OFFENSIVE_SUBSTRINGS = frozenset(
    {"fag", "fuk", "fuc", "nig", "cum", "kkk", "sex", "jiz", "cok", "dik", "tit", "nazi", "rape"}
)

PRONOUNCEABLE_PATTERNS: dict[int, tuple[str, ...]] = {
    3: ("CVC",),
    4: ("CVCV", "VCVC", "CCVC"),
}

# When "all" mode has a limit this many times smaller than the search space, it
# samples random indices instead of building every combination in memory.
_SAMPLE_FACTOR = 20
_MAX_DRAWS_PER_RESULT = 50


# --- Bundled data ------------------------------------------------------------------


@cache
def _load_list(filename: str) -> frozenset[str]:
    """Load a bundled one-name-per-line file, skipping blanks and ``#`` comments."""
    text = (resources.files("handle_finder") / "data" / filename).read_text(encoding="utf-8")
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(normalize(line))
    return frozenset(names)


def english_words() -> frozenset[str]:
    """Common 3/4-letter English words from ``data/words.txt``."""
    return _load_list("words.txt")


def known_names() -> frozenset[str]:
    """Common short first names from ``data/names.txt``."""
    return _load_list("names.txt")


def brand_fragments() -> frozenset[str]:
    """Brand-style fragments from ``data/fragments.txt``."""
    return _load_list("fragments.txt")


# --- Generators ----------------------------------------------------------------------


def _decode(index: int, chars: str, length: int) -> str:
    """Map ``index`` in ``[0, len(chars) ** length)`` to its combination string."""
    base = len(chars)
    out = []
    for _ in range(length):
        index, digit = divmod(index, base)
        out.append(chars[digit])
    return "".join(reversed(out))


def _iter_all(length: int, chars: str, rng: random.Random, limit: int | None) -> Iterator[str]:
    """Yield every combination of ``chars`` in random order.

    With a small ``limit`` relative to the space, random indices are sampled
    without replacement so millions of strings never sit in memory. If sampling
    runs dry (for example because most names are excluded), it falls back to a full
    shuffled enumeration; the caller dedupes.
    """
    total = len(chars) ** length
    if limit is not None and limit * _SAMPLE_FACTOR < total:
        seen: set[int] = set()
        max_draws = max(limit, 1) * _MAX_DRAWS_PER_RESULT
        for _ in range(max_draws):
            index = rng.randrange(total)
            if index in seen:
                continue
            seen.add(index)
            yield _decode(index, chars, length)
    names = ["".join(combo) for combo in itertools.product(chars, repeat=length)]
    rng.shuffle(names)
    yield from names


def pronounceable_names(length: int) -> list[str]:
    """Every pronounceable name of ``length`` (3 or 4), in a stable order."""
    if length not in PRONOUNCEABLE_PATTERNS:
        raise ValueError("Pronounceable mode supports lengths 3 and 4 only.")
    names: list[str] = []
    if length == 3:  # CVC
        names += [o + v + c for o in ONSETS for v in VOWELS for c in CODAS]
    else:
        # CVCV
        names += [o + v1 + m + v2 for o in ONSETS for v1 in VOWELS for m in MEDIALS for v2 in VOWELS]
        # VCVC
        names += [v1 + m + v2 + c for v1 in VOWELS for m in MEDIALS for v2 in VOWELS for c in CODAS]
        # CCVC
        names += [cl + v + c for cl in ONSET_CLUSTERS for v in VOWELS for c in CODAS]
    return [
        n
        for n in names
        if not any(bg in n for bg in _AWKWARD_BIGRAMS) and not any(bad in n for bad in _OFFENSIVE_SUBSTRINGS)
    ]


def word_names(length: int) -> list[str]:
    """Bundled words, names and fragments of exactly ``length``, sorted."""
    pool = english_words() | known_names() | brand_fragments()
    return sorted(n for n in pool if len(n) == length)


def generate(
    length: int,
    charset: Charset = Charset.LETTERS,
    mode: Mode = Mode.ALL,
    limit: int | None = None,
    exclude: Iterable[str] = (),
    seed: int | None = None,
) -> list[str]:
    """Generate up to ``limit`` unique, rule-abiding candidates.

    Args:
        length: Handle length (the CLI allows 3 or 4).
        charset: Characters allowed in ``all`` mode. ``pronounceable`` and
            ``words`` always produce letters only, which fit every charset.
        mode: Generation strategy.
        limit: Maximum number of names to return, or ``None`` for all of them.
        exclude: Names to skip, typically those already checked (resume).
        seed: Optional random seed for reproducible output.

    Returns:
        A shuffled list of lowercase candidate names.
    """
    if length < 1:
        raise ValueError("Length must be at least 1.")
    if limit is not None and limit < 0:
        raise ValueError("Limit can't be negative.")

    rng = random.Random(seed)
    allowed = set(CHARSET_CHARS[charset])
    excluded = set(exclude)

    source: Iterable[str]
    if mode is Mode.ALL:
        source = _iter_all(length, CHARSET_CHARS[charset], rng, limit)
    else:
        pool = pronounceable_names(length) if mode is Mode.PRONOUNCEABLE else word_names(length)
        rng.shuffle(pool)
        source = pool

    result: list[str] = []
    seen: set[str] = set()
    if limit == 0:
        return result
    for raw in source:
        name = normalize(raw)
        if name in seen or name in excluded or len(name) != length or not set(name) <= allowed or not is_valid(name):
            continue
        seen.add(name)
        result.append(name)
        if limit is not None and len(result) >= limit:
            break
    return result


# --- File helpers --------------------------------------------------------------------


@dataclass
class LoadedHandles:
    """Names read from a file, split into usable and rejected entries."""

    valid: list[str] = field(default_factory=list)
    invalid: list[tuple[str, str]] = field(default_factory=list)  # (raw, reason)
    duplicates: int = 0


def save_candidates(names: Iterable[str], path: Path) -> int:
    """Write one name per line to ``path``; return how many were written."""
    names = list(names)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{n}\n" for n in names), encoding="utf-8")
    return len(names)


def _raw_entries(path: Path) -> list[str]:
    """Read raw handle strings from a ``.txt`` (one per line) or ``.csv`` file."""
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return []
            column = "handle" if "handle" in reader.fieldnames else reader.fieldnames[0]
            return [(row.get(column) or "") for row in reader]
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln for ln in lines if not ln.strip().startswith("#")]


def load_handles(path: Path) -> LoadedHandles:
    """Load, normalise, validate and dedupe handles from ``path``.

    Text files hold one name per line (``#`` comments allowed). CSV files use
    the ``handle`` column, or the first column if there is no ``handle`` header.

    Raises:
        FileNotFoundError: if ``path`` doesn't exist.
    """
    loaded = LoadedHandles()
    seen: set[str] = set()
    for raw in _raw_entries(path):
        if not raw.strip():
            continue
        name = normalize(raw)
        problems = violations(name)
        if problems:
            loaded.invalid.append((raw.strip(), "; ".join(problems)))
            continue
        if name in seen:
            loaded.duplicates += 1
            continue
        seen.add(name)
        loaded.valid.append(name)
    return loaded
