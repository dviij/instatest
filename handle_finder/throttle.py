"""Throttling, exponential backoff and the automatic safety stop.

This module only *slows down and stops*. It has no proxy rotation, no IP or
fingerprint spoofing and no captcha handling. When Instagram pushes back, the
tool waits, and if the pushback continues it stops and tells the user to come
back later.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

#: Shortest delay allowed between requests, whatever the user configures.
MIN_DELAY_FLOOR = 1.0

TickCallback = Callable[[float], None]


@dataclass(frozen=True)
class ThrottleConfig:
    """User-tunable pacing and safety limits.

    Attributes:
        min_delay: Minimum seconds between requests (default 3).
        max_delay: Maximum seconds between requests (default 6); the actual
            delay is a random value between the two.
        backoff_base: First backoff after a rate-limit response, in seconds.
            Each later consecutive failure doubles it.
        backoff_max: Upper bound for a single backoff.
        max_consecutive_failures: Stop the run after this many failures in a row.
        daily_cap: Hard limit on requests per day, counted across runs.
    """

    min_delay: float = 3.0
    max_delay: float = 6.0
    backoff_base: float = 60.0
    backoff_max: float = 900.0
    max_consecutive_failures: int = 3
    daily_cap: int = 500

    def __post_init__(self) -> None:
        if self.min_delay < MIN_DELAY_FLOOR:
            raise ValueError(
                f"The minimum delay must be at least {MIN_DELAY_FLOOR:g} second(s) "
                "so the tool doesn't hammer Instagram."
            )
        if self.max_delay < self.min_delay:
            raise ValueError("The maximum delay can't be smaller than the minimum delay.")
        if self.backoff_base <= 0 or self.backoff_max < self.backoff_base:
            raise ValueError("Backoff must be positive, and the maximum backoff must be at least the base.")
        if self.max_consecutive_failures < 1:
            raise ValueError("The consecutive-failure limit must be at least 1.")
        if self.daily_cap < 1:
            raise ValueError("The daily cap must be at least 1.")


class BlockedError(Exception):
    """Instagram kept refusing requests, so the run must stop."""


def blocked_message(failures: int) -> str:
    """Plain-English explanation shown when the safety stop triggers."""
    return (
        f"{failures} requests in a row failed (rate limit, login wall, challenge page "
        "or network error). The tool stopped so it doesn't make things worse. "
        "Wait a few hours, ideally until tomorrow, before running it again. "
        "Everything checked so far is saved, and rerunning the same command picks "
        "up where it left off."
    )


def daily_cap_message(cap: int) -> str:
    """Plain-English explanation shown when the daily cap is reached."""
    return (
        f"Reached the daily cap of {cap} checks. Progress is saved. Run again "
        "tomorrow to continue, or raise --daily-cap if you understand the risk."
    )


class Throttle:
    """Paces requests with random jitter and backs off on failures."""

    def __init__(
        self,
        config: ThrottleConfig | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        """Create a throttle.

        ``sleep``, ``clock`` and ``rng`` are injectable for tests and default to
        :func:`time.sleep`, :func:`time.monotonic` and a fresh RNG.
        """
        self.config = config or ThrottleConfig()
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._rng = rng or random.Random()
        self._last_request_at: float | None = None
        self.consecutive_failures = 0

    def next_delay(self) -> float:
        """A random delay between ``min_delay`` and ``max_delay``."""
        return self._rng.uniform(self.config.min_delay, self.config.max_delay)

    def wait(self, on_tick: TickCallback | None = None) -> float:
        """Sleep until a jittered delay has passed since the previous request.

        Returns the number of seconds slept (0 before the first request).
        """
        if self._last_request_at is None:
            return 0.0
        elapsed = self._clock() - self._last_request_at
        remaining = self.next_delay() - elapsed
        if remaining <= 0:
            return 0.0
        self._sleep_in_steps(remaining, on_tick)
        return remaining

    def mark_request(self) -> None:
        """Note that a request was just sent."""
        self._last_request_at = self._clock()

    def record_success(self) -> None:
        """A conclusive response arrived; reset the failure streak."""
        self.consecutive_failures = 0

    def backoff_delay(self, attempt: int) -> float:
        """Exponential backoff for the ``attempt``-th consecutive failure, plus up to 10% jitter."""
        base = min(self.config.backoff_base * 2 ** (attempt - 1), self.config.backoff_max)
        return base * self._rng.uniform(1.0, 1.1)

    def record_failure(self) -> float:
        """Register a failure and return how long to back off.

        Raises:
            BlockedError: once ``max_consecutive_failures`` is reached.
        """
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.config.max_consecutive_failures:
            raise BlockedError(blocked_message(self.consecutive_failures))
        return self.backoff_delay(self.consecutive_failures)

    def backoff(self, seconds: float, on_tick: TickCallback | None = None) -> None:
        """Sleep for ``seconds``, reporting the remaining time once per second."""
        self._sleep_in_steps(seconds, on_tick)

    def _sleep_in_steps(self, seconds: float, on_tick: TickCallback | None) -> None:
        """Sleep in 1-second slices so Ctrl+C and countdowns stay responsive."""
        remaining = seconds
        while remaining > 0:
            if on_tick is not None:
                on_tick(remaining)
            step = min(1.0, remaining)
            self._sleep(step)
            remaining -= step
