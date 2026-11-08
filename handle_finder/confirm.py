"""Stage 3: confirmation of POSSIBLY_AVAILABLE names.

Two implementations of :class:`ConfirmChecker`:

* :class:`BrowserChecker` opens Instagram's signup page in a *visible* Chromium
  window with a persistent profile, types the candidate into the Username field,
  moves focus away (Tab) so Instagram validates it, and reads the result. It
  **never submits the form and never creates an account.** It never presses
  Enter or clicks a button.
* :class:`ManualChecker` prints the shortlist so you can check by hand, and can
  optionally record your answers.

Even CONFIRMED_AVAILABLE is not a guarantee: a name is yours only once
Instagram completes your registration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from .db import HandleDB, Status
from .prefilter import (
    USER_STOPPED_MESSAGE,
    NoticeHook,
    RunOutcome,
    RunStats,
    StopReason,
)
from .throttle import BlockedError, Throttle, daily_cap_message

SIGNUP_URL = "https://www.instagram.com/accounts/emailsignup/"
USERNAME_SELECTOR = 'input[name="username"]'
DEFAULT_PROFILE_DIR = Path.home() / ".handle_finder" / "browser-profile"

_UNAVAILABLE_PHRASES = (
    "username isn't available",
    "username is not available",
    "a user with that username already exists",
    "this username isn't available",
)
_BLOCK_PHRASES = (
    "please wait a few minutes",
    "try again later",
    "we suspect automated behavior",
)

AskFn = Callable[[str], str]


class ConfirmSetupError(RuntimeError):
    """A confirmation backend couldn't start; the message explains why in plain English."""


class StopRequested(Exception):
    """The user asked to stop confirming (for example by answering 'q')."""


@dataclass(frozen=True)
class ConfirmResult:
    """Outcome of confirming one name.

    ``status`` is ``None`` when the name was not verified (skipped, or a
    print-only manual check); the stored status is then left unchanged.
    """

    name: str
    status: Status | None
    notes: str = ""


class ConfirmChecker(ABC):
    """Interface for confirmation backends. Use as a context manager."""

    #: Does each confirmation send a request to Instagram (and so count toward
    #: throttling and the daily cap)?
    uses_network: bool = True

    def start(self) -> None:
        """Acquire resources (open a browser, ...)."""

    def stop(self) -> None:
        """Release resources. Must be safe to call more than once."""

    def announce(self, names: list[str]) -> None:
        """Called once with the full shortlist before checking begins."""

    @abstractmethod
    def confirm(self, name: str) -> ConfirmResult:
        """Check a single name."""

    def __enter__(self) -> ConfirmChecker:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()


def _default_ask(prompt: str) -> str:
    return Prompt.ask(prompt, choices=["y", "n", "s", "q"], default="s")


def _parse_answer(name: str, answer: str, how: str) -> ConfirmResult:
    """Turn a y/n/s/q answer into a result (raises :class:`StopRequested` on q)."""
    answer = answer.strip().lower()[:1]
    if answer == "q":
        raise StopRequested
    if answer == "y":
        return ConfirmResult(name, Status.CONFIRMED_AVAILABLE, f"confirmed available ({how})")
    if answer == "n":
        return ConfirmResult(name, Status.TAKEN, f"not available on signup page ({how})")
    return ConfirmResult(name, None, f"skipped ({how})")


# --- Manual ----------------------------------------------------------------------------


class ManualChecker(ConfirmChecker):
    """Prints the shortlist for checking by hand; optionally records answers."""

    uses_network = False

    def __init__(self, console: Console | None = None, *, ask: bool = False, ask_fn: AskFn | None = None) -> None:
        """Create a manual checker.

        Args:
            console: Where to print the shortlist.
            ask: If true, prompt for each name and store the answer.
            ask_fn: Custom prompt function (for tests); returns y/n/s/q.
        """
        self.console = console or Console()
        self.ask = ask
        self._ask_fn = ask_fn or _default_ask

    def announce(self, names: list[str]) -> None:
        """Print the shortlist with instructions."""
        if not names:
            return
        table = Table(title="Shortlist to verify by hand", show_lines=False)
        table.add_column("#", justify="right")
        table.add_column("handle")
        table.add_column("profile URL (should show 'page isn't available')")
        for i, name in enumerate(names, 1):
            table.add_row(str(i), f"@{name}", f"https://www.instagram.com/{name}/")
        self.console.print(table)
        self.console.print(
            f"To verify: open [link={SIGNUP_URL}]{SIGNUP_URL}[/link], type each handle in the "
            "Username field and click elsewhere. A check mark means available; "
            "\"This username isn't available\" means it's not. Don't submit the form "
            "unless you're actually registering."
        )

    def confirm(self, name: str) -> ConfirmResult:
        """Ask about ``name`` if ``ask`` is set; otherwise leave it unverified."""
        if not self.ask:
            return ConfirmResult(name, None, "not verified (manual list printed)")
        answer = self._ask_fn(f"Is [bold]@{name}[/] available on the signup page? [y]es/[n]o/[s]kip/[q]uit")
        return _parse_answer(name, answer, "manual")


# --- Browser ---------------------------------------------------------------------------


def classify_signup_state(page_text: str, *, accepted_icon: bool, error_icon: bool) -> tuple[Status | None, str]:
    """Interpret what the signup page shows for the typed username.

    Pure function so it can be unit tested without a browser.

    Returns:
        ``(status, notes)`` where status is CONFIRMED_AVAILABLE, TAKEN,
        RATE_LIMITED, or ``None`` when the page gives no clear answer.
    """
    text = page_text.replace("’", "'").lower()
    if any(p in text for p in _BLOCK_PHRASES):
        return Status.RATE_LIMITED, "signup page asked to wait or showed a challenge"
    if any(p in text for p in _UNAVAILABLE_PHRASES):
        return Status.TAKEN, "signup page says the username isn't available (taken, banned or reserved)"
    if error_icon and not accepted_icon:
        return Status.TAKEN, "signup page flagged the username field"
    if accepted_icon and not error_icon:
        return Status.CONFIRMED_AVAILABLE, "signup page accepted the username"
    return None, "couldn't read the signup page's answer"


# Runs in the page: looks only inside the username field's own container (the
# largest ancestor that holds no other input) for accepted/error indicators.
_FIELD_STATE_JS = """
(input) => {
  let box = input;
  while (box.parentElement && box.parentElement.querySelectorAll('input').length <= 1) {
    box = box.parentElement;
  }
  let accepted = false, error = false;
  for (const el of box.querySelectorAll('*')) {
    const hint = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('class') || '')).toLowerCase();
    if (/inputaccepted|username is available|accepted/.test(hint)) accepted = true;
    if (/inputerror|unavailable|not available|isn.t available/.test(hint)) error = true;
  }
  if (input.getAttribute('aria-invalid') === 'true') error = true;
  return { accepted, error };
}
"""


class BrowserChecker(ConfirmChecker):
    """Reads Instagram's signup-form validation in a visible Chromium window.

    Safety guarantees:

    * always visible (``headless=False``) so you can see exactly what happens
    * never submits the form, presses Enter, or clicks any button
    * never logs in; if the profile is logged in, it stops and says so
    * one page load and one validation per name, paced by the shared throttle
    """

    def __init__(
        self,
        profile_dir: Path = DEFAULT_PROFILE_DIR,
        *,
        console: Console | None = None,
        ask_when_unclear: bool = True,
        ask_fn: AskFn | None = None,
        settle_seconds: float = 2.5,
        timeout_seconds: float = 10.0,
        executable_path: str | None = None,
    ) -> None:
        """Configure the checker. The browser opens in :meth:`start`."""
        self.profile_dir = Path(profile_dir).expanduser()
        self.console = console or Console()
        self.ask_when_unclear = ask_when_unclear
        self._ask_fn = ask_fn or _default_ask
        self.settle_seconds = settle_seconds
        self.timeout_seconds = timeout_seconds
        self.executable_path = executable_path
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None

    def start(self) -> None:
        """Launch a persistent, visible Chromium."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ConfirmSetupError(
                "Playwright isn't installed. Run: pip install playwright && playwright install chromium"
            ) from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._pw = sync_playwright().start()
            self._context = self._pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=False,
                no_viewport=True,
                executable_path=self.executable_path,
            )
        except Exception as exc:
            self.stop()
            raise ConfirmSetupError(
                "Couldn't open Chromium. If you haven't yet, run `playwright install chromium`. "
                "A visible browser also needs a desktop session (it won't work over plain SSH). "
                f"Details: {exc}"
            ) from exc
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.console.print(
            f"Opened a browser window (profile: {self.profile_dir}). If a cookie banner "
            "appears, dismiss it yourself. Don't log in with this profile."
        )

    def stop(self) -> None:
        """Close the browser."""
        try:
            if self._context is not None:
                self._context.close()
        finally:
            self._context = None
            if self._pw is not None:
                self._pw.stop()
            self._pw = None

    def _load_signup_page(self) -> None:
        """Open a fresh signup page so no stale validation message carries over."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        page = self._page
        page.goto(SIGNUP_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(USERNAME_SELECTOR, timeout=60_000)
        except PlaywrightTimeout as exc:
            if "emailsignup" not in page.url:
                raise ConfirmSetupError(
                    "The signup page redirected away. The browser profile is probably "
                    "logged in to Instagram. Log out, or use a fresh --profile-dir."
                ) from exc
            raise ConfirmSetupError(
                "Couldn't find the Username field on the signup page within 60 seconds. "
                "If a cookie or other prompt is covering it, dismiss it in the browser window "
                "and run again."
            ) from exc

    def confirm(self, name: str) -> ConfirmResult:
        """Type ``name`` into the signup form's Username field and read the verdict."""
        if self._page is None:
            raise ConfirmSetupError("The browser isn't running. Use BrowserChecker as a context manager.")
        page = self._page
        self._load_signup_page()

        field = page.locator(USERNAME_SELECTOR).first
        field.click()
        field.fill("")
        field.press_sequentially(name, delay=60)
        # Tab moves focus away, which makes Instagram validate the field.
        # NEVER press Enter here: that would submit the form.
        field.press("Tab")
        page.wait_for_timeout(int(self.settle_seconds * 1000))

        status: Status | None = None
        notes = ""
        waited = 0.0
        while waited <= self.timeout_seconds:
            if any(m in page.url for m in ("/challenge", "/accounts/login")):
                return ConfirmResult(name, Status.RATE_LIMITED, "redirected to a login or challenge page")
            state = field.evaluate(_FIELD_STATE_JS)
            status, notes = classify_signup_state(
                page.inner_text("body"),
                accepted_icon=bool(state.get("accepted")),
                error_icon=bool(state.get("error")),
            )
            if status is not None:
                return ConfirmResult(name, status, notes)
            page.wait_for_timeout(500)
            waited += 0.5

        if self.ask_when_unclear:
            answer = self._ask_fn(
                f"Couldn't read the result for [bold]@{name}[/]. Look at the browser window: "
                "is it shown as available? [y]es/[n]o/[s]kip/[q]uit"
            )
            return _parse_answer(name, answer, "browser, read by you")
        return ConfirmResult(name, Status.UNKNOWN, notes)


# --- Run loop --------------------------------------------------------------------------

ConfirmHook = Callable[[ConfirmResult, RunStats], None]


def run_confirmation(
    names: Iterable[str],
    *,
    checker: ConfirmChecker,
    db: HandleDB,
    throttle: Throttle,
    on_result: ConfirmHook | None = None,
    on_notice: NoticeHook | None = None,
) -> RunOutcome:
    """Confirm each name with ``checker``, saving every result immediately.

    Network-based checkers share the pre-filter's throttle, backoff, safety stop
    and daily cap. Results of UNKNOWN or RATE_LIMITED never erase an earlier
    POSSIBLY_AVAILABLE (see :func:`handle_finder.db.merge_status`).
    """
    names = list(names)
    stats = RunStats()
    cap = throttle.config.daily_cap

    def notice(message: str) -> None:
        if on_notice is not None:
            on_notice(message)

    try:
        checker.announce(names)
        for name in names:
            while True:
                if checker.uses_network:
                    if db.checks_today() >= cap:
                        return RunOutcome(stats, StopReason.DAILY_CAP, daily_cap_message(cap))
                    throttle.wait()
                result = checker.confirm(name)
                failure = result.status is Status.RATE_LIMITED
                if checker.uses_network:
                    throttle.mark_request()
                    stats.requests += 1
                    db.log_check(name, "confirm")
                if result.status is not None:
                    db.record_result(name, result.status, notes=result.notes)
                stats.add(result.status, failure=failure)
                if on_result is not None:
                    on_result(result, stats)

                if failure:
                    try:
                        delay = throttle.record_failure()
                    except BlockedError as exc:
                        return RunOutcome(stats, StopReason.BLOCKED, str(exc))
                    throttle.backoff(
                        delay,
                        on_tick=lambda left: notice(f"Signup page pushed back. Retrying in {left:.0f}s"),
                    )
                    continue
                if result.status is not None and result.status is not Status.UNKNOWN:
                    throttle.record_success()
                break
    except StopRequested:
        return RunOutcome(
            stats, StopReason.USER_STOPPED, "Stopped confirming at your request. Answers so far are saved."
        )
    except KeyboardInterrupt:
        return RunOutcome(stats, StopReason.USER_STOPPED, USER_STOPPED_MESSAGE)
    return RunOutcome(stats, StopReason.COMPLETED)
