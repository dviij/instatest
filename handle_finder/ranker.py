"""Scoring and ranking surviving handles (0-100).

Points, out of 100:

=====================  ====  =====================================================
Component              Max   Rule
=====================  ====  =====================================================
Length                 30    3 chars = 30, 4 chars = 20, then -10 per extra char
Word / name            25    English word 25, known name 22, brand fragment 15
Pronounceability       20    vowel/consonant pattern and valid clusters
Characters             15    letters only 15, with digits 7, with . or _ 3
No look-alikes         10    penalties for 0/1, l/1/i and 0/o mixes, rn, vv, cl
=====================  ====  =====================================================
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .db import SURVIVORS, HandleRecord, Status
from .generator import ONSET_CLUSTERS, brand_fragments, english_words, known_names

RANKED_COLUMNS = ["handle", "status", "score", "reason", "checked_at"]
RESULTS_COLUMNS = RANKED_COLUMNS + ["notes"]

VOWELS = frozenset("aeiou")
STRONG_PATTERNS = frozenset({"CVC", "VCV", "CVCV", "VCVC", "CCVC", "CVVC", "CVCC", "CCVV"})
OK_PATTERNS = frozenset({"CVV", "VVC", "VCC", "VCCV", "VVCV", "CVVV", "VCVV"})
CODA_CLUSTERS = frozenset(
    "ck ct ft ld lf lk lm lp lt mp nd ng nk nt pt rb rc rd rf rk rl rm rn rp rs rt "
    "sh sk sp st th ch ll ss ff zz ts ps ks xt".split()
)
CONFUSABLE_GROUPS: tuple[frozenset[str], ...] = (frozenset("l1i"), frozenset("0o"))
AMBIGUOUS_SEQUENCES: dict[str, str] = {"rn": "m", "vv": "w", "cl": "d"}


@dataclass(frozen=True)
class Score:
    """A handle's score with a human-readable reason and the per-part points."""

    total: int
    reason: str
    parts: dict[str, int]


@dataclass(frozen=True)
class ScoredHandle:
    """A handle, its status and score: one CSV row."""

    handle: str
    status: str
    score: int
    reason: str
    checked_at: str = ""
    notes: str = ""


def pattern_of(name: str) -> str:
    """Map ``name`` to a C/V/X pattern (X = digit or separator).

    ``y`` counts as a vowel when it sits between consonants or ends a consonant
    run (g-Y-m, m-Y-th), and as a consonant otherwise (Y-ak, bo-Y).
    """
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch in VOWELS:
            out.append("V")
        elif ch == "y":
            prev_vowel = i > 0 and name[i - 1] in VOWELS
            next_vowel = i + 1 < len(name) and name[i + 1] in VOWELS
            out.append("C" if prev_vowel or next_vowel else "V")
        elif ch.isalpha():
            out.append("C")
        else:
            out.append("X")
    return "".join(out)


def _clusters_ok(name: str, pattern: str) -> bool:
    """Check that leading and trailing consonant pairs occur in English."""
    if pattern.startswith("CC") and name[:2] not in ONSET_CLUSTERS:
        return False
    if pattern.endswith("CC") and name[-2:] not in CODA_CLUSTERS:
        return False
    return True


def pronounceability(name: str, *, is_word: bool = False) -> tuple[int, str]:
    """Score (0-20) how easily ``name`` can be said out loud."""
    if is_word:
        return 20, "pronounceable (real word)"
    pattern = pattern_of(name)
    if "X" in pattern:
        return (4 if "V" in pattern else 0), "not a spoken word (digits/separators)"
    if "V" not in pattern:
        return 0, "no vowels"
    if "CCC" in pattern:
        return 2, f"hard consonant cluster ({pattern})"
    clusters_ok = _clusters_ok(name, pattern)
    if pattern in STRONG_PATTERNS and clusters_ok:
        return 20, f"pronounceable ({pattern})"
    if pattern in STRONG_PATTERNS or (pattern in OK_PATTERNS and clusters_ok):
        return 12, f"mostly pronounceable ({pattern})"
    return 6, f"awkward to say ({pattern})"


def lookalike_score(name: str) -> tuple[int, list[str]]:
    """Score (0-10) for being free of easily confused characters."""
    points = 10
    issues: list[str] = []
    if "0" in name or "1" in name:
        points -= 6
        issues.append("0/1 can be misread as o/l")
    chars = set(name)
    for group in CONFUSABLE_GROUPS:
        present = group & chars
        if len(present) >= 2:
            points -= 4
            issues.append("mixes look-alikes " + "/".join(sorted(present)))
    if "l" in chars and not issues:
        points -= 2
        issues.append("'l' can be mistaken for a capital I")
    for seq, looks_like in AMBIGUOUS_SEQUENCES.items():
        if seq in name:
            points -= 2
            issues.append(f"'{seq}' can look like '{looks_like}'")
    return max(points, 0), issues


def _length_points(name: str) -> int:
    return max(0, 30 - 10 * (len(name) - 3)) if len(name) >= 3 else 30


def score_handle(name: str) -> Score:
    """Score ``name`` from 0 to 100 and explain why."""
    reasons: list[str] = [f"{len(name)} chars"]

    length = _length_points(name)

    if name in english_words():
        meaning, kind = 25, "English word"
    elif name in known_names():
        meaning, kind = 22, "known name"
    elif name in brand_fragments():
        meaning, kind = 15, "brand-style fragment"
    else:
        meaning, kind = 0, ""
    if kind:
        reasons.append(kind)

    pron, pron_reason = pronounceability(name, is_word=bool(kind))
    reasons.append(pron_reason)

    has_digit = any(c.isdigit() for c in name)
    has_sep = any(c in "._" for c in name)
    if has_sep:
        charset, charset_reason = 3, "has . or _"
    elif has_digit:
        charset, charset_reason = 7, "has digits"
    else:
        charset, charset_reason = 15, "letters only"
    reasons.append(charset_reason)

    looks, issues = lookalike_score(name)
    reasons.append("; ".join(issues) if issues else "no look-alikes")

    parts = {"length": length, "meaning": meaning, "pronounceable": pron, "charset": charset, "lookalike": looks}
    total = max(0, min(100, sum(parts.values())))
    return Score(total=total, reason=" · ".join(reasons), parts=parts)


def score_records(records: Iterable[HandleRecord]) -> list[ScoredHandle]:
    """Score every record (any status), keeping input order."""
    out: list[ScoredHandle] = []
    for rec in records:
        s = score_handle(rec.name)
        out.append(
            ScoredHandle(
                handle=rec.name,
                status=rec.status.value,
                score=s.total,
                reason=s.reason,
                checked_at=rec.checked_at or "",
                notes=rec.notes or "",
            )
        )
    return out


def rank(items: Iterable[ScoredHandle], top: int | None = None) -> list[ScoredHandle]:
    """Keep only survivors (POSSIBLY_AVAILABLE / CONFIRMED_AVAILABLE) and sort.

    Sort order: score (high first), then confirmed before possibly available,
    then alphabetically.
    """
    survivors = {s.value for s in SURVIVORS}
    kept = [i for i in items if i.status in survivors]
    kept.sort(key=lambda i: (-i.score, i.status != Status.CONFIRMED_AVAILABLE.value, i.handle))
    return kept[:top] if top is not None else kept


def read_results_csv(path: Path) -> list[ScoredHandle]:
    """Read a results CSV and re-score each row (so wordlist updates apply).

    Only the ``handle`` and ``status`` columns are required.

    Raises:
        FileNotFoundError: if ``path`` doesn't exist.
        ValueError: if required columns are missing.
    """
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or [])
        missing = {"handle", "status"} - fields
        if missing:
            raise ValueError(f"{path} is missing column(s): {', '.join(sorted(missing))}")
        rows = list(reader)
    out: list[ScoredHandle] = []
    for row in rows:
        handle = (row.get("handle") or "").strip().lower()
        if not handle:
            continue
        s = score_handle(handle)
        out.append(
            ScoredHandle(
                handle=handle,
                status=(row.get("status") or "").strip().upper(),
                score=s.total,
                reason=s.reason,
                checked_at=(row.get("checked_at") or "").strip(),
                notes=(row.get("notes") or "").strip(),
            )
        )
    return out


def write_csv(items: Iterable[ScoredHandle], path: Path, *, with_notes: bool = False) -> int:
    """Write rows to ``path``; return the number written.

    Columns are ``handle,status,score,reason,checked_at``, plus ``notes`` when
    ``with_notes`` is true.
    """
    columns = RESULTS_COLUMNS if with_notes else RANKED_COLUMNS
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for item in items:
            row = [item.handle, item.status, item.score, item.reason, item.checked_at]
            if with_notes:
                row.append(item.notes)
            writer.writerow(row)
            count += 1
    return count
