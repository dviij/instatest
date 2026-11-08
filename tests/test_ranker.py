"""Tests for scoring and ranking."""

from pathlib import Path

import pytest

from handle_finder.db import HandleRecord, Status
from handle_finder.ranker import (
    RANKED_COLUMNS,
    ScoredHandle,
    lookalike_score,
    pattern_of,
    pronounceability,
    rank,
    read_results_csv,
    score_handle,
    score_records,
    write_csv,
)


@pytest.mark.parametrize("name", ["cat", "book", "a1_b", "l1l1", "o0o0", "xqzt", "___", "zz.z", "luna"])
def test_scores_are_between_0_and_100(name: str) -> None:
    score = score_handle(name)
    assert 0 <= score.total <= 100
    assert score.total == sum(score.parts.values())
    assert score.reason


def test_perfect_three_letter_word() -> None:
    score = score_handle("cat")
    assert score.total == 100
    assert "English word" in score.reason


def test_shorter_scores_higher() -> None:
    assert score_handle("cat").total > score_handle("book").total


def test_real_word_beats_made_up_pronounceable() -> None:
    assert score_handle("book").total > score_handle("bazu").total


def test_known_name_scores_higher_than_random() -> None:
    luna = score_handle("luna")
    assert "known name" in luna.reason
    assert luna.total > score_handle("lupa").total


def test_pronounceable_beats_unpronounceable() -> None:
    assert score_handle("bazu").total > score_handle("xqzt").total
    assert pronounceability("xqzt")[0] == 0  # no vowels
    assert pronounceability("bazu")[0] == 20


def test_letters_only_beats_digits_and_separators() -> None:
    letters = score_handle("bazu").total
    digits = score_handle("baz2").total
    separators = score_handle("ba_z").total
    assert letters > digits > separators


def test_lookalikes_are_penalised() -> None:
    clean, issues = lookalike_score("bazu")
    assert clean == 10 and issues == []
    assert lookalike_score("o0o0")[0] < 5
    assert lookalike_score("l1l1")[0] < 5
    assert lookalike_score("corn")[0] < 10  # 'rn' looks like 'm'
    assert score_handle("bozo").total > score_handle("b0z0").total


def test_y_is_treated_as_vowel_between_consonants() -> None:
    assert pattern_of("gym") == "CVC"
    assert pattern_of("yak") == "CVC"
    assert pattern_of("boy") == "CVC"
    assert pattern_of("a_1") == "VXX"


def _item(handle: str, status: Status, score: int) -> ScoredHandle:
    return ScoredHandle(handle=handle, status=status.value, score=score, reason="r")


def test_rank_keeps_only_survivors_sorted_with_top() -> None:
    items = [
        _item("aaa", Status.TAKEN, 99),
        _item("bbb", Status.POSSIBLY_AVAILABLE, 50),
        _item("ccc", Status.CONFIRMED_AVAILABLE, 70),
        _item("ddd", Status.UNKNOWN, 90),
        _item("eee", Status.RATE_LIMITED, 90),
        _item("fff", Status.POSSIBLY_AVAILABLE, 70),
    ]
    ranked = rank(items)
    assert [i.handle for i in ranked] == ["ccc", "fff", "bbb"]  # confirmed wins the tie
    assert [i.handle for i in rank(items, top=1)] == ["ccc"]


def test_csv_roundtrip_rescores(tmp_path: Path) -> None:
    records = [
        HandleRecord("cat", Status.POSSIBLY_AVAILABLE, "2026-01-01T00:00:00+00:00", notes="404"),
        HandleRecord("xqzt", Status.TAKEN, "2026-01-01T00:00:00+00:00"),
    ]
    path = tmp_path / "results.csv"
    write_csv(score_records(records), path, with_notes=True)
    loaded = read_results_csv(path)
    assert [i.handle for i in loaded] == ["cat", "xqzt"]
    assert loaded[0].score == 100 and loaded[0].notes == "404"


def test_ranked_csv_has_exact_columns(tmp_path: Path) -> None:
    path = tmp_path / "ranked.csv"
    write_csv([_item("cat", Status.POSSIBLY_AVAILABLE, 100)], path)
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == RANKED_COLUMNS == ["handle", "status", "score", "reason", "checked_at"]


def test_read_results_csv_requires_columns(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("name,foo\nabc,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing column"):
        read_results_csv(path)
