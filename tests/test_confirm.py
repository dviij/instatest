"""Tests for the confirmation stage (no real browser needed)."""

from collections.abc import Callable

import pytest
from rich.console import Console

from handle_finder.confirm import (
    ConfirmChecker,
    ConfirmResult,
    ManualChecker,
    classify_signup_state,
    run_confirmation,
)
from handle_finder.db import HandleDB, Status
from handle_finder.prefilter import StopReason
from handle_finder.throttle import Throttle


@pytest.mark.parametrize(
    ("text", "accepted", "error", "expected"),
    [
        ("This username isn't available. Please try another.", False, True, Status.TAKEN),
        ("This username isn’t available.", False, False, Status.TAKEN),  # curly apostrophe
        ("A user with that username already exists.", False, False, Status.TAKEN),
        ("Sign up to see photos", True, False, Status.CONFIRMED_AVAILABLE),
        ("Sign up to see photos", False, True, Status.TAKEN),
        ("Please wait a few minutes before you try again.", False, False, Status.RATE_LIMITED),
        ("Sign up to see photos", False, False, None),
        ("Sign up to see photos", True, True, None),  # contradictory signals: don't guess
    ],
)
def test_classify_signup_state(text: str, accepted: bool, error: bool, expected: Status | None) -> None:
    status, notes = classify_signup_state(text, accepted_icon=accepted, error_icon=error)
    assert status is expected
    assert notes


def test_manual_checker_prints_shortlist_and_leaves_status() -> None:
    console = Console(record=True, width=120)
    checker = ManualChecker(console)
    checker.announce(["zeal", "milo"])
    output = console.export_text()
    assert "@zeal" in output and "@milo" in output
    assert "emailsignup" in output
    assert checker.confirm("zeal").status is None
    assert checker.uses_network is False


def test_manual_checker_records_answers_when_asked(db: HandleDB, make_throttle: Callable[..., Throttle]) -> None:
    answers = iter(["y", "n", "s"])
    checker = ManualChecker(Console(file=None, quiet=True), ask=True, ask_fn=lambda _: next(answers))
    for name in ("aaa", "bbb", "ccc"):
        db.record_result(name, Status.POSSIBLY_AVAILABLE)
    outcome = run_confirmation(["aaa", "bbb", "ccc"], checker=checker, db=db, throttle=make_throttle())
    assert outcome.stop_reason is StopReason.COMPLETED
    assert db.get("aaa").status is Status.CONFIRMED_AVAILABLE
    assert db.get("bbb").status is Status.TAKEN
    assert db.get("ccc").status is Status.POSSIBLY_AVAILABLE  # skipped: unchanged
    assert db.checks_today() == 0  # manual checks don't count toward the cap


def test_manual_quit_stops_cleanly(db: HandleDB, make_throttle: Callable[..., Throttle]) -> None:
    checker = ManualChecker(Console(quiet=True), ask=True, ask_fn=lambda _: "q")
    outcome = run_confirmation(["aaa"], checker=checker, db=db, throttle=make_throttle())
    assert outcome.stop_reason is StopReason.USER_STOPPED


class ScriptedChecker(ConfirmChecker):
    """A network checker that replays scripted statuses."""

    def __init__(self, statuses: list[Status | None]) -> None:
        self.statuses = list(statuses)
        self.calls: list[str] = []

    def confirm(self, name: str) -> ConfirmResult:
        self.calls.append(name)
        return ConfirmResult(name, self.statuses.pop(0), "scripted")


def test_browser_style_checker_is_throttled_and_capped(db: HandleDB, make_throttle: Callable[..., Throttle]) -> None:
    for name in ("aaa", "bbb", "ccc"):
        db.record_result(name, Status.POSSIBLY_AVAILABLE)
    checker = ScriptedChecker([Status.CONFIRMED_AVAILABLE, Status.TAKEN, Status.CONFIRMED_AVAILABLE])
    outcome = run_confirmation(["aaa", "bbb", "ccc"], checker=checker, db=db, throttle=make_throttle(daily_cap=2))
    assert outcome.stop_reason is StopReason.DAILY_CAP
    assert checker.calls == ["aaa", "bbb"]
    assert db.get("aaa").status is Status.CONFIRMED_AVAILABLE
    assert db.get("bbb").status is Status.TAKEN
    assert db.checks_today() == 2


def test_confirmation_stops_after_repeated_blocks(db: HandleDB, make_throttle: Callable[..., Throttle]) -> None:
    db.record_result("aaa", Status.POSSIBLY_AVAILABLE)
    checker = ScriptedChecker([Status.RATE_LIMITED] * 3)
    outcome = run_confirmation(["aaa"], checker=checker, db=db, throttle=make_throttle())
    assert outcome.stop_reason is StopReason.BLOCKED
    assert len(checker.calls) == 3
    assert db.get("aaa").status is Status.POSSIBLY_AVAILABLE  # a block doesn't erase the hint


def test_unclear_confirmation_keeps_possibly_available(db: HandleDB, make_throttle: Callable[..., Throttle]) -> None:
    db.record_result("aaa", Status.POSSIBLY_AVAILABLE)
    checker = ScriptedChecker([Status.UNKNOWN])
    run_confirmation(["aaa"], checker=checker, db=db, throttle=make_throttle())
    assert db.get("aaa").status is Status.POSSIBLY_AVAILABLE
