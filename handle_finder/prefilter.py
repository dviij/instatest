"""Stage 2: the cheap pre-filter.

Requests ``https://www.instagram.com/<name>/`` while logged out, with an ordinary
browser User-Agent, and interprets the answer:

=========================================  ======================
Response                                   Status
=========================================  ======================
200 with profile markers                   TAKEN
404, or 200 "this page isn't available"    POSSIBLY_AVAILABLE
429, 403, login redirect, challenge page   RATE_LIMITED (back off)
anything else, including network errors    UNKNOWN
=========================================  ======================

POSSIBLY_AVAILABLE is only a hint: the name may still be banned, reserved or
belong to a deactivated account. Ambiguous responses are never counted as
available.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType

import httpx

from .db import HandleDB, Status
from .throttle import BlockedError, Throttle, daily_cap_message

PROFILE_URL = "https://www.instagram.com/{name}/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

#: URL path fragments that mean Instagram wants a login or a challenge.
_BLOCK_PATHS = ("/accounts/login", "/challenge", "/checkpoint", "/accounts/suspended")
#: Body text that means "slow down" or "prove you're human".
_BLOCK_TEXT = (
    "please wait a few minutes before you try again",
    "challenge_required",
    "checkpoint_required",
)
#: Body text of Instagram's "not found" page.
_NOT_FOUND_TEXT = (
    "sorry, this page isn't available",
    "the link you followed may be broken",
    "page not found",
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_OG_PROFILE_RE = re.compile(
    r"<meta[^>]+(?:property=\"og:type\"[^>]+content=\"profile\"|content=\"profile\"[^>]+property=\"og:type\")",
    re.IGNORECASE,
)

#: Stop if this many UNKNOWN results arrive in a row; Instagram may have
#: changed its page format, and burning the daily cap would gain nothing.
DEFAULT_MAX_CONSECUTIVE_UNKNOWN = 10


@dataclass(frozen=True)
class CheckResult:
    """Outcome of checking one name."""

    name: str
    status: Status
    notes: str
    http_status: int | None = None
    network_error: bool = False

    @property
    def is_failure(self) -> bool:
        """Should this result trigger backoff (and count toward the safety stop)?"""
        return self.status is Status.RATE_LIMITED or self.network_error


def _normalized_body(response: httpx.Response) -> str:
    """Lowercased, HTML-unescaped body with curly apostrophes made straight."""
    try:
        text = response.text
    except (UnicodeDecodeError, LookupError):
        text = response.content.decode("utf-8", errors="replace")
    return html.unescape(text).replace("’", "'").lower()


def _has_profile_markers(name: str, body: str) -> bool:
    """Does the page look like a real profile for ``name``?"""
    return f"(@{name})" in body or f'"username":"{name}"' in body or bool(_OG_PROFILE_RE.search(body))


def interpret_response(name: str, response: httpx.Response) -> CheckResult:
    """Translate an HTTP response for ``/<name>/`` into a :class:`CheckResult`.

    This is a pure function (no I/O), so the rules are easy to test.
    """
    code = response.status_code

    def result(status: Status, notes: str) -> CheckResult:
        return CheckResult(name=name, status=status, notes=notes, http_status=code)

    if code == 429:
        return result(Status.RATE_LIMITED, "HTTP 429 Too Many Requests")
    if code == 403:
        return result(Status.RATE_LIMITED, "HTTP 403 Forbidden")

    visited = [response.url.path] + [str(r.headers.get("location", "")) for r in response.history]
    if 300 <= code < 400:
        visited.append(str(response.headers.get("location", "")))
    if any(marker in path.lower() for path in visited for marker in _BLOCK_PATHS):
        return result(Status.RATE_LIMITED, "redirected to a login or challenge page")
    if 300 <= code < 400:
        return result(Status.UNKNOWN, f"unexpected redirect (HTTP {code})")

    if code == 404:
        return result(
            Status.POSSIBLY_AVAILABLE,
            "HTTP 404: no profile (may still be banned, reserved or deactivated)",
        )

    if code == 200:
        body = _normalized_body(response)
        if _has_profile_markers(name, body):
            return result(Status.TAKEN, "profile page found")
        if any(marker in body for marker in _BLOCK_TEXT):
            return result(Status.RATE_LIMITED, "challenge or 'please wait' page")
        if any(marker in body for marker in _NOT_FOUND_TEXT):
            return result(
                Status.POSSIBLY_AVAILABLE,
                "page says it isn't available (may still be banned, reserved or deactivated)",
            )
        title_match = _TITLE_RE.search(body)
        title = title_match.group(1).strip() if title_match else ""
        if title.startswith(("login", "log in")):
            return result(Status.RATE_LIMITED, "login wall instead of a profile")
        return result(Status.UNKNOWN, "HTTP 200 but no profile or not-found markers")

    if code >= 500:
        return result(Status.UNKNOWN, f"Instagram server error (HTTP {code})")
    return result(Status.UNKNOWN, f"unexpected response (HTTP {code})")


class PreFilter:
    """Checks names against public profile URLs. Never logs in."""

    def __init__(self, client: httpx.Client | None = None, timeout: float = 15.0) -> None:
        """Create a pre-filter. Pass ``client`` to inject a transport (tests)."""
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=5,
        )

    def check(self, name: str) -> CheckResult:
        """Request the profile URL for ``name`` and interpret the response."""
        url = PROFILE_URL.format(name=name)
        try:
            response = self._client.get(url, headers=DEFAULT_HEADERS)
        except httpx.TooManyRedirects:
            return CheckResult(name, Status.UNKNOWN, "too many redirects (often a login wall)", network_error=True)
        except httpx.TimeoutException:
            return CheckResult(name, Status.UNKNOWN, "request timed out", network_error=True)
        except httpx.HTTPError as exc:
            return CheckResult(name, Status.UNKNOWN, f"network error: {exc.__class__.__name__}", network_error=True)
        return interpret_response(name, response)

    def close(self) -> None:
        """Close the HTTP client if this object created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PreFilter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


# --- Run loop --------------------------------------------------------------------------


class StopReason(StrEnum):
    """Why a run ended."""

    COMPLETED = "completed"
    DAILY_CAP = "daily_cap"
    BLOCKED = "blocked"
    TOO_MANY_UNKNOWN = "too_many_unknown"
    USER_STOPPED = "user_stopped"


@dataclass
class RunStats:
    """Running totals for progress display."""

    checked: int = 0
    taken: int = 0
    possibly_available: int = 0
    confirmed: int = 0
    blocked: int = 0
    unknown: int = 0
    skipped: int = 0
    requests: int = 0

    def add(self, status: Status | None, *, failure: bool = False) -> None:
        """Count one result.

        Failures count as blocked, not as checked. ``None`` (not verified) counts
        as skipped.
        """
        if failure:
            self.blocked += 1
            return
        if status is None:
            self.skipped += 1
            return
        self.checked += 1
        if status is Status.TAKEN:
            self.taken += 1
        elif status is Status.POSSIBLY_AVAILABLE:
            self.possibly_available += 1
        elif status is Status.CONFIRMED_AVAILABLE:
            self.confirmed += 1
        elif status is Status.UNKNOWN:
            self.unknown += 1


@dataclass
class RunOutcome:
    """Summary of a finished (or stopped) run."""

    stats: RunStats
    stop_reason: StopReason
    message: str = ""

    @property
    def should_halt(self) -> bool:
        """Must the pipeline stop here instead of continuing to the next stage?

        Reaching the daily cap doesn't halt: local stages (manual confirmation,
        ranking) can still run, and network stages check the cap themselves.
        """
        return self.stop_reason in (
            StopReason.BLOCKED,
            StopReason.TOO_MANY_UNKNOWN,
            StopReason.USER_STOPPED,
        )


ResultHook = Callable[[CheckResult, RunStats], None]
NameHook = Callable[[str], None]
NoticeHook = Callable[[str], None]

USER_STOPPED_MESSAGE = "Stopped by you (Ctrl+C). Every result so far is saved; rerun the same command to resume."


def too_many_unknown_message(count: int) -> str:
    """Plain-English explanation for the unknown-streak stop."""
    return (
        f"Got {count} ambiguous responses in a row. Instagram may be serving a "
        "different page than expected, or quietly limiting you. Stopped to avoid "
        "wasting checks. Wait a while and try a small batch again."
    )


def run_prefilter(
    names: Iterable[str],
    *,
    prefilter: PreFilter,
    db: HandleDB,
    throttle: Throttle,
    on_start: NameHook | None = None,
    on_result: ResultHook | None = None,
    on_notice: NoticeHook | None = None,
    max_consecutive_unknown: int = DEFAULT_MAX_CONSECUTIVE_UNKNOWN,
) -> RunOutcome:
    """Check ``names`` one by one, saving each result as soon as it arrives.

    Pacing and safety:

    * a jittered delay between every request
    * on a rate-limit or network failure: save it, back off exponentially, retry
      the same name; stop after ``max_consecutive_failures``
    * stop at the daily cap (counted in the database, across runs)
    * stop after ``max_consecutive_unknown`` ambiguous results
    * Ctrl+C ends the run cleanly with :attr:`StopReason.USER_STOPPED`
    """
    stats = RunStats()
    unknown_streak = 0
    cap = throttle.config.daily_cap

    def notice(message: str) -> None:
        if on_notice is not None:
            on_notice(message)

    try:
        for name in names:
            while True:
                if db.checks_today() >= cap:
                    return RunOutcome(stats, StopReason.DAILY_CAP, daily_cap_message(cap))
                if on_start is not None:
                    on_start(name)
                throttle.wait()

                result = prefilter.check(name)
                throttle.mark_request()
                stats.requests += 1
                db.log_check(name, "prefilter")
                db.record_result(name, result.status, notes=result.notes)
                stats.add(result.status, failure=result.is_failure)
                if on_result is not None:
                    on_result(result, stats)

                if result.is_failure:
                    try:
                        delay = throttle.record_failure()
                    except BlockedError as exc:
                        return RunOutcome(stats, StopReason.BLOCKED, str(exc))
                    why = result.notes

                    def countdown(left: float, why: str = why) -> None:
                        notice(f"Instagram pushed back ({why}). Backing off, retry in {left:.0f}s")

                    throttle.backoff(delay, on_tick=countdown)
                    continue  # retry the same name

                if result.status is Status.UNKNOWN:
                    unknown_streak += 1
                    if unknown_streak >= max_consecutive_unknown:
                        return RunOutcome(stats, StopReason.TOO_MANY_UNKNOWN, too_many_unknown_message(unknown_streak))
                else:
                    unknown_streak = 0
                    throttle.record_success()
                break
    except KeyboardInterrupt:
        return RunOutcome(stats, StopReason.USER_STOPPED, USER_STOPPED_MESSAGE)
    return RunOutcome(stats, StopReason.COMPLETED)
