"""Tests for Instagram username rules."""

import pytest

from handle_finder.rules import MAX_LENGTH, clean, is_valid, normalize, violations


@pytest.mark.parametrize(
    "name",
    ["abc", "abcd", "a.b", "a_b", "_ab", "ab_", "___", "a1b2", "123", "a.b.c", "a_.b", "x" * MAX_LENGTH],
)
def test_valid_names(name: str) -> None:
    assert is_valid(name)
    assert violations(name) == []


@pytest.mark.parametrize(
    ("name", "expected_fragment"),
    [
        (".ab", "starts with a period"),
        ("ab.", "ends with a period"),
        ("a..b", "two periods in a row"),
        ("ab-c", "characters Instagram doesn't allow"),
        ("ab c", "characters Instagram doesn't allow"),
        ("ABC", "characters Instagram doesn't allow"),  # must be normalised first
        ("abç", "characters Instagram doesn't allow"),
        ("", "is empty"),
        ("x" * (MAX_LENGTH + 1), "longer than"),
    ],
)
def test_invalid_names(name: str, expected_fragment: str) -> None:
    assert not is_valid(name)
    assert any(expected_fragment in problem for problem in violations(name))


def test_multiple_violations_are_all_reported() -> None:
    problems = violations(".a..b-")
    assert len(problems) >= 3


def test_normalize_lowercases_and_strips() -> None:
    assert normalize("  @AbC.d ") == "abc.d"
    assert normalize("ABCD") == "abcd"


def test_clean_returns_none_for_invalid() -> None:
    assert clean(" @ABC ") == "abc"
    assert clean("a..b") is None
    assert clean(".ab") is None
