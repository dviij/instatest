"""Tests for candidate generation."""

import re
from pathlib import Path

import pytest

from handle_finder.generator import (
    CODAS,
    ONSET_CLUSTERS,
    ONSETS,
    VOWELS,
    Charset,
    Mode,
    brand_fragments,
    english_words,
    generate,
    known_names,
    load_handles,
    pronounceable_names,
    save_candidates,
)
from handle_finder.rules import is_valid


def test_all_letters_length3_is_complete_unique_and_shuffled() -> None:
    names = generate(3, Charset.LETTERS, Mode.ALL, seed=1)
    assert len(names) == 26**3
    assert len(set(names)) == len(names)
    assert names != sorted(names)  # shuffled
    assert all(len(n) == 3 and n.isalpha() for n in names)


def test_all_alnum_sep_applies_instagram_rules() -> None:
    names = set(generate(3, Charset.ALNUM_SEP, Mode.ALL, seed=2))
    assert all(is_valid(n) for n in names)
    assert not any(n.startswith(".") or n.endswith(".") or ".." in n for n in names)
    assert {"a.b", "_a_", "a_1", "abc", "123"} <= names
    assert ".ab" not in names and "ab." not in names
    # 38^3 total minus names with a leading or trailing period.
    assert 26**3 < len(names) < 38**3


def test_charset_restricts_characters() -> None:
    letters = generate(3, Charset.LETTERS, Mode.ALL, limit=500, seed=3)
    alnum = generate(3, Charset.ALNUM, Mode.ALL, limit=500, seed=3)
    assert all(re.fullmatch(r"[a-z]{3}", n) for n in letters)
    assert all(re.fullmatch(r"[a-z0-9]{3}", n) for n in alnum)


def test_large_space_is_sampled_with_limit_and_is_reproducible() -> None:
    first = generate(4, Charset.ALNUM_SEP, Mode.ALL, limit=50, seed=42)
    second = generate(4, Charset.ALNUM_SEP, Mode.ALL, limit=50, seed=42)
    assert first == second
    assert len(first) == 50 == len(set(first))
    assert all(len(n) == 4 and is_valid(n) for n in first)


def test_exclude_skips_already_checked_names() -> None:
    everything = generate(3, Charset.LETTERS, Mode.WORDS)
    excluded = set(everything[:20])
    rest = generate(3, Charset.LETTERS, Mode.WORDS, exclude=excluded)
    assert excluded.isdisjoint(rest)
    assert len(rest) == len(everything) - 20


def test_sampling_falls_back_when_most_names_are_excluded() -> None:
    # Exclude all but five 4-letter names; sampling would run dry, so the
    # generator must fall back to a full scan and still find the survivors.
    keep = {"abcd", "wxyz", "mnop", "qrst", "efgh"}
    import itertools
    import string

    exclude = {"".join(p) for p in itertools.product(string.ascii_lowercase, repeat=4)} - keep
    names = generate(4, Charset.LETTERS, Mode.ALL, limit=5, exclude=exclude, seed=0)
    assert set(names) == keep


def test_limit_zero_returns_nothing() -> None:
    assert generate(3, Charset.LETTERS, Mode.ALL, limit=0) == []


def _matches_pattern(name: str) -> bool:
    if len(name) == 3:
        return name[0] in ONSETS and name[1] in VOWELS and name[2] in CODAS
    cvcv = name[0] in ONSETS and name[1] in VOWELS and name[2] not in VOWELS and name[3] in VOWELS
    vcvc = name[0] in VOWELS and name[1] not in VOWELS and name[2] in VOWELS and name[3] in CODAS
    ccvc = name[:2] in ONSET_CLUSTERS and name[2] in VOWELS and name[3] in CODAS
    return cvcv or vcvc or ccvc


@pytest.mark.parametrize("length", [3, 4])
def test_pronounceable_follows_patterns(length: int) -> None:
    names = generate(length, mode=Mode.PRONOUNCEABLE, seed=5)
    assert names, "expected some pronounceable names"
    assert len(names) == len(set(names))
    for name in names:
        assert len(name) == length
        assert _matches_pattern(name), name
        assert not re.search(r"[^aeiou]{3}", name), name  # no 3-consonant clusters
        assert name[0] not in "qx"


def test_pronounceable_excludes_unpronounceable_clusters() -> None:
    names = set(pronounceable_names(4))
    for bad in ("xqzt", "bcdf", "qqqq", "tsrk"):
        assert bad not in names
    for good in ("mila", "oval", "trek", "luna"):
        assert good in names


def test_pronounceable_rejects_unsupported_length() -> None:
    with pytest.raises(ValueError):
        generate(5, mode=Mode.PRONOUNCEABLE)


@pytest.mark.parametrize("length", [3, 4])
def test_words_mode_uses_bundled_lists(length: int) -> None:
    pool = english_words() | known_names() | brand_fragments()
    names = generate(length, mode=Mode.WORDS)
    assert names
    assert all(n in pool and len(n) == length for n in names)


def test_words_mode_includes_known_examples() -> None:
    assert "cat" in generate(3, mode=Mode.WORDS)
    assert "book" in generate(4, mode=Mode.WORDS)
    assert "luna" in generate(4, mode=Mode.WORDS)  # a name


def test_bundled_lists_are_clean() -> None:
    for name in english_words() | known_names() | brand_fragments():
        assert re.fullmatch(r"[a-z]{3,4}", name), name
        assert is_valid(name)


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "candidates.txt"
    save_candidates(["abc", "def"], path)
    assert load_handles(path).valid == ["abc", "def"]


def test_load_handles_validates_dedupes_and_skips_comments(tmp_path: Path) -> None:
    path = tmp_path / "mixed.txt"
    path.write_text("# comment\nABC\n@abc\n.bad\na..b\n\nok_1\n", encoding="utf-8")
    loaded = load_handles(path)
    assert loaded.valid == ["abc", "ok_1"]
    assert loaded.duplicates == 1
    assert [raw for raw, _ in loaded.invalid] == [".bad", "a..b"]


def test_load_handles_reads_csv_handle_column(tmp_path: Path) -> None:
    path = tmp_path / "shortlist.csv"
    path.write_text("handle,status,score\nzeal,POSSIBLY_AVAILABLE,90\nmilo,TAKEN,80\n", encoding="utf-8")
    assert load_handles(path).valid == ["zeal", "milo"]
