"""Shared test fixtures."""

import random
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from handle_finder.db import HandleDB
from handle_finder.prefilter import PreFilter
from handle_finder.throttle import Throttle, ThrottleConfig


class FakeTime:
    """A clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def make_throttle(fake_time: FakeTime) -> Callable[..., Throttle]:
    def factory(**overrides: object) -> Throttle:
        config = ThrottleConfig(**overrides)  # type: ignore[arg-type]
        return Throttle(config, sleep=fake_time.sleep, clock=fake_time.clock, rng=random.Random(0))

    return factory


@pytest.fixture
def db(tmp_path: Path) -> Iterator[HandleDB]:
    store = HandleDB(tmp_path / "test.db")
    yield store
    store.close()


Handler = Callable[[httpx.Request], httpx.Response]


def mock_prefilter(handler: Handler) -> PreFilter:
    """A PreFilter whose HTTP traffic goes to ``handler`` instead of the network."""
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True, max_redirects=5)
    return PreFilter(client=client)


def profile_html(name: str) -> str:
    return (
        f"<html><head><title>Some Person (@{name}) &bull; Instagram photos and videos</title>"
        '<meta property="og:type" content="profile" />'
        f'<meta property="og:title" content="Some Person (@{name}) &bull; Instagram photos and videos" />'
        "</head><body></body></html>"
    )


NOT_FOUND_HTML = (
    "<html><head><title>Page not found &bull; Instagram</title></head>"
    "<body><h2>Sorry, this page isn&#39;t available.</h2>"
    "<p>The link you followed may be broken, or the page may have been removed.</p></body></html>"
)
LOGIN_HTML = "<html><head><title>Login &bull; Instagram</title></head><body><form id='loginForm'></form></body></html>"
