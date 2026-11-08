"""Tests for throttling and storage."""

import random
from collections.abc import Callable

import pytest

from conftest import FakeTime
from handle_finder.db import HandleDB, Status, merge_status
from handle_finder.throttle import BlockedError, Throttle, ThrottleConfig

# --- Throttle -------------------------------------------------------------------------


def test_defaults_match_spec() -> None:
    cfg = ThrottleConfig()
    assert (cfg.min_delay, cfg.max_delay) == (3.0, 6.0)
    assert cfg.max_consecutive_failures == 3
    assert cfg.daily_cap == 500


def test_delays_are_jittered_within_bounds() -> None:
    throttle = Throttle(ThrottleConfig(), rng=random.Random(1))
    delays = [throttle.next_delay() for _ in range(200)]
    assert all(3.0 <= d <= 6.0 for d in delays)
    assert len({round(d, 3) for d in delays}) > 50  # actually random


def test_first_request_has_no_wait_then_waits(make_throttle: Callable[..., Throttle], fake_time: FakeTime) -> None:
    throttle = make_throttle()
    assert throttle.wait() == 0.0
    throttle.mark_request()
    slept = throttle.wait()
    assert 3.0 <= slept <= 6.0
    assert sum(fake_time.sleeps) == pytest.approx(slept)


def test_backoff_is_exponential_and_stops_after_three(make_throttle: Callable[..., Throttle]) -> None:
    throttle = make_throttle(backoff_base=10, backoff_max=1000)
    first = throttle.record_failure()
    second = throttle.record_failure()
    assert 10 <= first <= 11
    assert 20 <= second <= 22
    with pytest.raises(BlockedError, match="Wait a few hours"):
        throttle.record_failure()


def test_success_resets_failure_streak(make_throttle: Callable[..., Throttle]) -> None:
    throttle = make_throttle()
    throttle.record_failure()
    throttle.record_failure()
    throttle.record_success()
    throttle.record_failure()  # would raise if the streak hadn't reset
    assert throttle.consecutive_failures == 1


def test_backoff_is_capped(make_throttle: Callable[..., Throttle]) -> None:
    throttle = make_throttle(backoff_base=100, backoff_max=150)
    assert throttle.backoff_delay(5) <= 150 * 1.1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_delay": 0.1},
        {"min_delay": 5, "max_delay": 4},
        {"daily_cap": 0},
        {"max_consecutive_failures": 0},
        {"backoff_base": 0},
    ],
)
def test_unsafe_config_is_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        ThrottleConfig(**kwargs)


# --- Storage --------------------------------------------------------------------------


def test_schema_has_required_columns(db: HandleDB) -> None:
    columns = [row[1] for row in db._conn.execute("PRAGMA table_info(handles)")]
    assert columns == ["name", "status", "checked_at", "source", "notes"]


def test_add_candidates_is_idempotent(db: HandleDB) -> None:
    assert db.add_candidates(["aaa", "bbb"], source="test") == 2
    assert db.add_candidates(["aaa", "ccc"], source="test") == 1
    assert db.get("aaa").status is Status.UNCHECKED


def test_results_persist_across_connections(tmp_path) -> None:
    path = tmp_path / "persist.db"
    with HandleDB(path) as first:
        first.record_result("zeal", Status.POSSIBLY_AVAILABLE, notes="404")
    with HandleDB(path) as second:
        record = second.get("zeal")
        assert record.status is Status.POSSIBLY_AVAILABLE
        assert record.notes == "404"
        assert second.conclusive_names() == {"zeal"}


def test_conclusive_names_exclude_retryable_statuses(db: HandleDB) -> None:
    db.record_result("aaa", Status.TAKEN)
    db.record_result("bbb", Status.UNKNOWN)
    db.record_result("ccc", Status.RATE_LIMITED)
    db.record_result("ddd", Status.CONFIRMED_AVAILABLE)
    assert db.conclusive_names() == {"aaa", "ddd"}


@pytest.mark.parametrize(
    ("previous", "new", "expected"),
    [
        (None, Status.RATE_LIMITED, Status.RATE_LIMITED),
        (Status.POSSIBLY_AVAILABLE, Status.RATE_LIMITED, Status.POSSIBLY_AVAILABLE),
        (Status.TAKEN, Status.UNKNOWN, Status.TAKEN),
        (Status.CONFIRMED_AVAILABLE, Status.POSSIBLY_AVAILABLE, Status.CONFIRMED_AVAILABLE),
        (Status.CONFIRMED_AVAILABLE, Status.TAKEN, Status.TAKEN),
        (Status.POSSIBLY_AVAILABLE, Status.CONFIRMED_AVAILABLE, Status.CONFIRMED_AVAILABLE),
        (Status.UNKNOWN, Status.TAKEN, Status.TAKEN),
        (Status.RATE_LIMITED, Status.POSSIBLY_AVAILABLE, Status.POSSIBLY_AVAILABLE),
    ],
)
def test_merge_status(previous: Status | None, new: Status, expected: Status) -> None:
    assert merge_status(previous, new) is expected
