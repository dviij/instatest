"""Tests for status interpretation (mocked HTTP) and the pre-filter run loop."""

from collections.abc import Callable

import httpx
import pytest

from conftest import LOGIN_HTML, NOT_FOUND_HTML, FakeTime, mock_prefilter, profile_html
from handle_finder.db import HandleDB, Status
from handle_finder.prefilter import DEFAULT_HEADERS, PROFILE_URL, StopReason, interpret_response, run_prefilter
from handle_finder.throttle import Throttle


def check(handler: Callable[[httpx.Request], httpx.Response], name: str = "abc"):
    with mock_prefilter(handler) as pf:
        return pf.check(name)


# --- Status interpretation -----------------------------------------------------------


def test_200_with_profile_is_taken() -> None:
    result = check(lambda r: httpx.Response(200, html=profile_html("abc")))
    assert result.status is Status.TAKEN
    assert not result.is_failure


def test_200_with_only_og_profile_type_is_taken() -> None:
    body = '<html><head><meta content="profile" property="og:type"></head></html>'
    assert check(lambda r: httpx.Response(200, html=body)).status is Status.TAKEN


def test_404_is_possibly_available_never_confirmed() -> None:
    result = check(lambda r: httpx.Response(404, html=NOT_FOUND_HTML))
    assert result.status is Status.POSSIBLY_AVAILABLE
    assert "banned" in result.notes  # the caveat is recorded


def test_200_not_found_page_is_possibly_available() -> None:
    assert check(lambda r: httpx.Response(200, html=NOT_FOUND_HTML)).status is Status.POSSIBLY_AVAILABLE


@pytest.mark.parametrize("code", [429, 403])
def test_429_and_403_are_rate_limited(code: int) -> None:
    result = check(lambda r: httpx.Response(code))
    assert result.status is Status.RATE_LIMITED
    assert result.is_failure


def test_login_redirect_is_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/abc/":
            return httpx.Response(302, headers={"Location": "https://www.instagram.com/accounts/login/?next=/abc/"})
        return httpx.Response(200, html=LOGIN_HTML)

    result = check(handler)
    assert result.status is Status.RATE_LIMITED
    assert "login" in result.notes


def test_challenge_redirect_is_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/abc/":
            return httpx.Response(302, headers={"Location": "/challenge/?next=/abc/"})
        return httpx.Response(200, html="<html>challenge</html>")

    assert check(handler).status is Status.RATE_LIMITED


def test_please_wait_page_is_rate_limited() -> None:
    body = "<html><body>Please wait a few minutes before you try again.</body></html>"
    assert check(lambda r: httpx.Response(200, html=body)).status is Status.RATE_LIMITED


def test_inline_login_wall_is_rate_limited() -> None:
    assert check(lambda r: httpx.Response(200, html=LOGIN_HTML)).status is Status.RATE_LIMITED


def test_ambiguous_200_is_unknown_not_available() -> None:
    result = check(lambda r: httpx.Response(200, html="<html><head><title>Instagram</title></head></html>"))
    assert result.status is Status.UNKNOWN
    assert not result.is_failure


def test_profile_for_a_different_user_is_not_taken() -> None:
    # A page mentioning another account must not mark "abc" as taken.
    body = "<html><head><title>Other (@other) &bull; Instagram</title></head></html>"
    assert check(lambda r: httpx.Response(200, html=body), name="abc").status is Status.UNKNOWN


@pytest.mark.parametrize("code", [500, 502, 503])
def test_server_errors_are_unknown(code: int) -> None:
    assert check(lambda r: httpx.Response(code)).status is Status.UNKNOWN


def test_unexpected_status_is_unknown() -> None:
    assert check(lambda r: httpx.Response(418)).status is Status.UNKNOWN


def test_network_error_is_unknown_and_a_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = check(handler)
    assert result.status is Status.UNKNOWN
    assert result.network_error and result.is_failure


def test_request_uses_profile_url_browser_user_agent_and_no_cookies() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404)

    check(handler, name="zeal")
    assert str(seen[0].url) == PROFILE_URL.format(name="zeal")
    assert seen[0].headers["user-agent"] == DEFAULT_HEADERS["User-Agent"]
    assert "Mozilla/5.0" in seen[0].headers["user-agent"]
    assert "cookie" not in seen[0].headers  # logged out: no session cookie


def test_interpret_response_is_pure() -> None:
    request = httpx.Request("GET", PROFILE_URL.format(name="abc"))
    assert interpret_response("abc", httpx.Response(404, request=request)).status is Status.POSSIBLY_AVAILABLE


# --- Run loop --------------------------------------------------------------------------


def route(table: dict[str, int | list[int]]) -> Callable[[httpx.Request], httpx.Response]:
    """Build a handler: name -> status code (or a list consumed one per request)."""
    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.strip("/")
        spec = table[name]
        if isinstance(spec, list):
            code = spec[min(calls.get(name, 0), len(spec) - 1)]
            calls[name] = calls.get(name, 0) + 1
        else:
            code = spec
        return httpx.Response(code, html=profile_html(name) if code == 200 else "")

    return handler


def test_run_saves_every_result_with_jittered_delays(
    db: HandleDB, make_throttle: Callable[..., Throttle], fake_time: FakeTime
) -> None:
    handler = route({"aaa": 200, "bbb": 404, "ccc": 200})
    with mock_prefilter(handler) as pf:
        outcome = run_prefilter(["aaa", "bbb", "ccc"], prefilter=pf, db=db, throttle=make_throttle())
    assert outcome.stop_reason is StopReason.COMPLETED
    assert db.get("aaa").status is Status.TAKEN
    assert db.get("bbb").status is Status.POSSIBLY_AVAILABLE
    assert db.get("ccc").status is Status.TAKEN
    assert all(db.get(n).checked_at for n in ("aaa", "bbb", "ccc"))
    assert outcome.stats.checked == 3 and outcome.stats.possibly_available == 1
    # Two waits between three requests, each in the 3-6s window (slept in 1s slices).
    assert 6.0 <= sum(fake_time.sleeps) <= 12.0
    assert db.checks_today() == 3


def test_three_consecutive_rate_limits_stop_the_run(
    db: HandleDB, make_throttle: Callable[..., Throttle], fake_time: FakeTime
) -> None:
    handler = route({"aaa": 429, "bbb": 404})
    with mock_prefilter(handler) as pf:
        outcome = run_prefilter(["aaa", "bbb"], prefilter=pf, db=db, throttle=make_throttle(backoff_base=60))
    assert outcome.stop_reason is StopReason.BLOCKED
    assert "Wait" in outcome.message
    assert outcome.stats.requests == 3  # retried "aaa" twice, then stopped
    assert db.get("bbb") is None  # never reached
    assert db.get("aaa").status is Status.RATE_LIMITED
    # Backoffs of ~60s and ~120s (plus up to 10% jitter) on top of normal delays.
    assert 60 + 120 <= sum(fake_time.sleeps) <= (60 + 120) * 1.1 + 12


def test_rate_limit_then_success_retries_same_name(db: HandleDB, make_throttle: Callable[..., Throttle]) -> None:
    handler = route({"aaa": [429, 404], "bbb": 200})
    throttle = make_throttle()
    with mock_prefilter(handler) as pf:
        outcome = run_prefilter(["aaa", "bbb"], prefilter=pf, db=db, throttle=throttle)
    assert outcome.stop_reason is StopReason.COMPLETED
    assert db.get("aaa").status is Status.POSSIBLY_AVAILABLE
    assert throttle.consecutive_failures == 0
    assert outcome.stats.blocked == 1 and outcome.stats.checked == 2


def test_daily_cap_is_enforced_across_runs(db: HandleDB, make_throttle: Callable[..., Throttle]) -> None:
    names = ["aaa", "bbb", "ccc", "ddd"]
    handler = route({n: 404 for n in names})
    with mock_prefilter(handler) as pf:
        first = run_prefilter(names, prefilter=pf, db=db, throttle=make_throttle(daily_cap=2))
        second = run_prefilter(names[2:], prefilter=pf, db=db, throttle=make_throttle(daily_cap=2))
    assert first.stop_reason is StopReason.DAILY_CAP
    assert first.stats.requests == 2
    assert second.stop_reason is StopReason.DAILY_CAP
    assert second.stats.requests == 0
    assert db.get("ccc") is None


def test_ctrl_c_keeps_results_already_saved(db: HandleDB, make_throttle: Callable[..., Throttle]) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return httpx.Response(404)

    with mock_prefilter(handler) as pf:
        outcome = run_prefilter(["aaa", "bbb", "ccc"], prefilter=pf, db=db, throttle=make_throttle())
    assert outcome.stop_reason is StopReason.USER_STOPPED
    assert db.get("aaa").status is Status.POSSIBLY_AVAILABLE
    assert db.get("bbb").status is Status.POSSIBLY_AVAILABLE
    assert db.get("ccc") is None


def test_rate_limit_never_erases_a_conclusive_result(db: HandleDB, make_throttle: Callable[..., Throttle]) -> None:
    db.record_result("aaa", Status.POSSIBLY_AVAILABLE, notes="404 earlier")
    with mock_prefilter(route({"aaa": [429, 429, 429]})) as pf:
        run_prefilter(["aaa"], prefilter=pf, db=db, throttle=make_throttle())
    record = db.get("aaa")
    assert record.status is Status.POSSIBLY_AVAILABLE
    assert "RATE_LIMITED" in (record.notes or "")


def test_too_many_unknown_responses_stop_the_run(db: HandleDB, make_throttle: Callable[..., Throttle]) -> None:
    names = [f"a{i:02d}" for i in range(20)]
    with mock_prefilter(route({n: 418 for n in names})) as pf:
        outcome = run_prefilter(names, prefilter=pf, db=db, throttle=make_throttle(), max_consecutive_unknown=5)
    assert outcome.stop_reason is StopReason.TOO_MANY_UNKNOWN
    assert outcome.stats.requests == 5
