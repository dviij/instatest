"""Command-line interface: ``handle-finder generate | check | rank | run | recheck``."""

import csv
import functools
import sqlite3
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from . import __version__
from .confirm import (
    DEFAULT_PROFILE_DIR,
    BrowserChecker,
    ConfirmChecker,
    ConfirmResult,
    ConfirmSetupError,
    ManualChecker,
    run_confirmation,
)
from .db import HandleDB, Status
from .generator import Charset, LoadedHandles, Mode, generate, load_handles, save_candidates
from .prefilter import CheckResult, PreFilter, RunOutcome, RunStats, StopReason, run_prefilter
from .ranker import ScoredHandle, rank, read_results_csv, score_records, write_csv
from .throttle import Throttle, ThrottleConfig, daily_cap_message

console = Console()
err_console = Console(stderr=True)

TOS_NOTICE = (
    "Automated querying may violate Instagram's Terms of Use. Use small batches, at your own risk.\n"
    "Results are hints, not guarantees: a handle is only registrable if Instagram's signup page accepts it."
)
DISCLAIMER = (
    "[bold]Reminder:[/] nothing here is guaranteed. A handle is only yours once Instagram's "
    "signup page accepts it and you finish registering."
)

EXIT_BLOCKED = 3
EXIT_INTERRUPTED = 130


class ConfirmKind(StrEnum):
    """Which confirmation backend to use after the pre-filter."""

    NONE = "none"
    MANUAL = "manual"
    BROWSER = "browser"


app = typer.Typer(
    name="handle-finder",
    help="Find short Instagram usernames (3-4 characters) that look available.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# --- Shared option types ---------------------------------------------------------------

DbOpt = Annotated[Path, typer.Option("--db", envvar="HANDLE_FINDER_DB", help="SQLite database file.")]
DelayMinOpt = Annotated[float, typer.Option("--delay-min", min=1.0, help="Minimum seconds between requests.")]
DelayMaxOpt = Annotated[float, typer.Option("--delay-max", min=1.0, help="Maximum seconds between requests.")]
DailyCapOpt = Annotated[
    int, typer.Option("--daily-cap", min=1, help="Hard limit on requests per day (counted across runs).")
]
BackoffOpt = Annotated[
    float, typer.Option("--backoff", min=1.0, help="First backoff after a rate limit, in seconds. Doubles each time.")
]
ConfirmOpt = Annotated[
    ConfirmKind,
    typer.Option("--confirm", case_sensitive=False, help="Confirm possibly-available names: none, manual or browser."),
]
AskOpt = Annotated[
    bool, typer.Option("--ask/--no-ask", help="With --confirm manual: ask about each name and save your answers.")
]
ProfileOpt = Annotated[Path, typer.Option("--profile-dir", help="Chromium profile folder for --confirm browser.")]
BrowserPathOpt = Annotated[
    str | None, typer.Option("--browser-path", help="Chromium/Chrome executable to use instead of Playwright's.")
]
LengthOpt = Annotated[int, typer.Option("--length", "-l", min=3, max=4, help="Handle length: 3 or 4.")]
CharsetOpt = Annotated[
    Charset,
    typer.Option("--charset", "-c", case_sensitive=False, help="Characters for --mode all: letters, alnum, alnum_sep."),
]
ModeOpt = Annotated[Mode, typer.Option("--mode", "-m", case_sensitive=False, help="all, pronounceable or words.")]
LimitOpt = Annotated[int, typer.Option("--limit", "-n", min=0, help="Maximum candidates to generate (0 = no limit).")]
SeedOpt = Annotated[int | None, typer.Option("--seed", help="Random seed for reproducible candidates.")]
TopOpt = Annotated[int, typer.Option("--top", "-t", min=1, help="How many top handles to keep.")]

F = TypeVar("F", bound=Callable[..., Any])


# --- Helpers ---------------------------------------------------------------------------


def graceful(func: F) -> F:
    """Turn Ctrl+C into a clean exit with a reassuring message."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except KeyboardInterrupt:
            err_console.print(
                "\n[yellow]Interrupted.[/] Everything checked so far is saved in the database; "
                "rerun the same command to resume."
            )
            raise typer.Exit(EXIT_INTERRUPTED) from None

    return wrapper  # type: ignore[return-value]


def fail(message: str, code: int = 1) -> NoReturn:
    """Print a plain-English error and exit."""
    err_console.print(f"[bold red]Error:[/] {message}")
    raise typer.Exit(code)


def _throttle_config(delay_min: float, delay_max: float, daily_cap: int, backoff: float) -> ThrottleConfig:
    try:
        return ThrottleConfig(
            min_delay=delay_min,
            max_delay=delay_max,
            daily_cap=daily_cap,
            backoff_base=backoff,
            backoff_max=max(900.0, backoff * 8),
        )
    except ValueError as exc:
        fail(str(exc))


def _open_db(path: Path) -> HandleDB:
    try:
        return HandleDB(path)
    except (sqlite3.Error, OSError) as exc:
        fail(f"Couldn't open the database at '{path}': {exc}")


def _load(path: Path, hint: str) -> LoadedHandles:
    """Load handles from ``path``, reporting skipped entries; exit on failure."""
    if not path.exists():
        fail(f"Couldn't find '{path}'. {hint}")
    try:
        loaded = load_handles(path)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        fail(f"Couldn't read '{path}': {exc}")
    if loaded.invalid:
        console.print(
            f"[yellow]Skipping {len(loaded.invalid)} entr(y/ies) that break Instagram's rules "
            "(no network call made):[/]"
        )
        for raw, reason in loaded.invalid[:10]:
            console.print(f"  [dim]{raw!r}[/] {reason}")
        if len(loaded.invalid) > 10:
            console.print(f"  [dim]...and {len(loaded.invalid) - 10} more[/]")
    if loaded.duplicates:
        console.print(f"[dim]Ignored {loaded.duplicates} duplicate(s).[/]")
    if not loaded.valid:
        fail(f"'{path}' has no valid handles to work with.")
    return loaded


def _fmt_duration(seconds: float) -> str:
    minutes = seconds / 60
    if minutes < 1:
        return f"{seconds:.0f} seconds"
    if minutes < 90:
        return f"{minutes:.0f} minute(s)"
    return f"{minutes / 60:.1f} hours"


def _stats_line(stats: RunStats) -> str:
    return (
        f"[green]checked {stats.checked}[/] · [red]taken {stats.taken}[/] · "
        f"[cyan]possibly available {stats.possibly_available}[/] · "
        f"[yellow]blocked {stats.blocked}[/] · [dim]unknown {stats.unknown}[/]"
    )


class _StatsView:
    """Live-updating stats line shown under the progress bar."""

    def __init__(self) -> None:
        self.stats = RunStats()
        self.note = ""

    def __rich__(self) -> Text:
        line = _stats_line(self.stats)
        if self.note:
            line += f"\n[yellow]{self.note}[/]"
        return Text.from_markup(line)


def _rate_limited_printer(interval: float = 15.0) -> Callable[[str], None]:
    """Print notices at most once per ``interval`` seconds (for backoff countdowns)."""
    last: list[float] = []

    def show(message: str) -> None:
        now = time.monotonic()
        if not last or now - last[0] >= interval:
            console.print(f"[yellow]{message}[/]")
            last[:] = [now]

    return show


def _report_outcome(stage: str, outcome: RunOutcome) -> None:
    reason = outcome.stop_reason
    if reason is StopReason.COMPLETED:
        console.print(f"[bold green]{stage} finished.[/] {_stats_line(outcome.stats)}")
    elif reason is StopReason.DAILY_CAP:
        console.print(f"[yellow]{stage} paused:[/] {outcome.message}")
    elif reason is StopReason.BLOCKED:
        err_console.print(Panel(outcome.message, title="Stopped: Instagram is blocking requests", border_style="red"))
    elif reason is StopReason.TOO_MANY_UNKNOWN:
        err_console.print(Panel(outcome.message, title="Stopped: unclear responses", border_style="yellow"))
    else:
        console.print(f"[yellow]{outcome.message}[/]")


def _exit_for(outcome: RunOutcome | None) -> None:
    if outcome is None:
        return
    if outcome.stop_reason in (StopReason.BLOCKED, StopReason.TOO_MANY_UNKNOWN):
        raise typer.Exit(EXIT_BLOCKED)
    if outcome.stop_reason is StopReason.USER_STOPPED:
        raise typer.Exit(EXIT_INTERRUPTED)


# --- Stages ------------------------------------------------------------------------------


def _do_generate(
    store: HandleDB,
    *,
    length: int,
    charset: Charset,
    mode: Mode,
    limit: int,
    seed: int | None,
    output: Path,
) -> list[str]:
    """Generate candidates (skipping already-checked names), save file and DB rows."""
    if mode is not Mode.ALL and charset is not Charset.LETTERS:
        console.print("[dim]Note: --charset only affects --mode all; pronounceable and words produce letters only.[/]")
    try:
        names = generate(length, charset, mode, limit or None, exclude=store.conclusive_names(), seed=seed)
    except ValueError as exc:
        fail(str(exc))
    try:
        save_candidates(names, output)
    except OSError as exc:
        fail(f"Couldn't write '{output}': {exc}")
    store.add_candidates(names, source=f"generate:{mode.value}")
    detail = f"{mode.value}, {length} chars" + (f", {charset.value}" if mode is Mode.ALL else "")
    console.print(
        f"Generated [bold]{len(names)}[/] candidate(s) ({detail}). "
        f"Already-checked names were left out. Saved to {output}."
    )
    if not names:
        console.print(
            "[yellow]No new candidates: everything here is already checked. Try another --mode or --length.[/]"
        )
    return names


def _prefilter_stage(store: HandleDB, names: list[str], throttle: Throttle, *, force: bool) -> RunOutcome:
    """Run the pre-filter on ``names`` with a live progress display."""
    cfg = throttle.config
    done = set() if force else store.conclusive_names()
    todo = [n for n in names if n not in done]
    skipped = len(names) - len(todo)
    if skipped:
        console.print(f"Skipping {skipped} name(s) already checked (use --force to check again).")
    if not todo:
        console.print("Nothing new to check.")
        return RunOutcome(RunStats(), StopReason.COMPLETED)

    used = store.checks_today()
    left = cfg.daily_cap - used
    console.print(f"Daily cap: {used}/{cfg.daily_cap} checks used today.")
    if left <= 0:
        outcome = RunOutcome(RunStats(), StopReason.DAILY_CAP, daily_cap_message(cfg.daily_cap))
        _report_outcome("Pre-filter", outcome)
        return outcome
    planned = min(len(todo), left)
    if planned < len(todo):
        console.print(f"[yellow]Only {planned} of {len(todo)} fit under today's cap; the rest will wait.[/]")
    avg = (cfg.min_delay + cfg.max_delay) / 2
    console.print(
        f"Checking {planned} name(s), one every {cfg.min_delay:g}-{cfg.max_delay:g}s: about "
        f"{_fmt_duration(planned * avg)}. Press Ctrl+C to stop safely."
    )

    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    view = _StatsView()
    task = progress.add_task("Starting", total=planned)

    with PreFilter() as prefilter, Live(Group(progress, view), console=console, refresh_per_second=8) as live:

        def on_start(name: str) -> None:
            progress.update(task, description=f"Checking @{name}")

        def on_result(result: CheckResult, stats: RunStats) -> None:
            view.stats = stats
            view.note = ""
            if not result.is_failure:
                progress.advance(task)
            if result.status is Status.POSSIBLY_AVAILABLE:
                live.console.print(f"  [cyan]possibly available:[/] @{result.name}")

        def on_notice(message: str) -> None:
            view.note = message

        outcome = run_prefilter(
            todo, prefilter=prefilter, db=store, throttle=throttle,
            on_start=on_start, on_result=on_result, on_notice=on_notice,
        )  # fmt: skip
    if not console.is_terminal:
        console.print()  # Live leaves its final frame without a newline when output is piped
    _report_outcome("Pre-filter", outcome)
    return outcome


_CONFIRM_LABELS = {
    Status.CONFIRMED_AVAILABLE: "[bold green]available on signup page[/]",
    Status.TAKEN: "[red]not available[/]",
    Status.UNKNOWN: "[dim]unclear[/]",
    Status.RATE_LIMITED: "[yellow]blocked[/]",
}


def _confirm_stage(
    store: HandleDB,
    names: list[str],
    kind: ConfirmKind,
    throttle: Throttle,
    *,
    ask: bool,
    profile_dir: Path,
    browser_path: str | None,
    include_confirmed: bool = False,
) -> RunOutcome | None:
    """Run confirmation on names that passed the pre-filter. Returns ``None`` if skipped."""
    wanted = {Status.POSSIBLY_AVAILABLE} | ({Status.CONFIRMED_AVAILABLE} if include_confirmed else set())
    candidates = [r.name for r in store.records(names) if r.status in wanted]
    if not candidates:
        console.print("No possibly-available names to confirm.")
        return None
    if kind is ConfirmKind.NONE:
        console.print(
            f"{len(candidates)} name(s) look possibly available. Verify them with "
            "--confirm manual or --confirm browser, or later with `handle-finder recheck`."
        )
        return None

    checker: ConfirmChecker
    if kind is ConfirmKind.MANUAL:
        checker = ManualChecker(console, ask=ask)
    else:
        checker = BrowserChecker(profile_dir, console=console, executable_path=browser_path)

    def on_result(result: ConfirmResult, stats: RunStats) -> None:
        if result.status is None:
            if ask or checker.uses_network:
                console.print(f"  @{result.name}: [dim]skipped[/]")
            return
        console.print(
            f"  @{result.name}: {_CONFIRM_LABELS.get(result.status, result.status.value)} [dim]({result.notes})[/]"
        )

    try:
        with checker:
            outcome = run_confirmation(
                candidates, checker=checker, db=store, throttle=throttle,
                on_result=on_result, on_notice=_rate_limited_printer(),
            )  # fmt: skip
    except ConfirmSetupError as exc:
        err_console.print(f"[bold red]Confirmation couldn't run:[/] {exc}")
        console.print("Pre-filter results are saved. You can confirm later with `handle-finder recheck`.")
        return None
    except Exception as exc:  # Playwright raises its own error types (e.g. window closed)
        if type(exc).__module__.startswith("playwright"):
            err_console.print(f"[bold red]The browser stopped unexpectedly:[/] {exc}")
            console.print("Results so far are saved. Run `handle-finder recheck` to continue.")
            return None
        raise
    if checker.uses_network or ask:
        _report_outcome("Confirmation", outcome)
    return outcome


def _export_results(store: HandleDB, names: list[str], output: Path) -> list[ScoredHandle]:
    rows = score_records(store.records(names))
    try:
        write_csv(rows, output, with_notes=True)
    except OSError as exc:
        fail(f"Couldn't write '{output}': {exc}")
    console.print(f"Saved {len(rows)} result(s) to {output}.")
    return rows


def _rank_and_show(items: list[ScoredHandle], top: int, output: Path) -> None:
    ranked = rank(items, top)
    try:
        write_csv(ranked, output)
    except OSError as exc:
        fail(f"Couldn't write '{output}': {exc}")
    if not ranked:
        console.print("No surviving handles to rank yet (only POSSIBLY_AVAILABLE and CONFIRMED_AVAILABLE are ranked).")
        return
    table = Table(title=f"Top {len(ranked)} handles")
    table.add_column("#", justify="right")
    table.add_column("handle", style="bold")
    table.add_column("status")
    table.add_column("score", justify="right")
    table.add_column("reason", overflow="fold")
    for i, item in enumerate(ranked, 1):
        status_style = "green" if item.status == Status.CONFIRMED_AVAILABLE.value else "cyan"
        table.add_row(str(i), f"@{item.handle}", f"[{status_style}]{item.status}[/]", str(item.score), item.reason)
    console.print(table)
    console.print(f"Saved {len(ranked)} ranked handle(s) to {output}.")


# --- Commands ----------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"handle-finder {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True, help="Show the version and exit.")
    ] = False,
) -> None:
    """Find short Instagram usernames (3-4 characters) that look available."""
    if ctx.resilient_parsing:
        return
    console.print(Panel(TOS_NOTICE, title="[bold yellow]Notice[/]", border_style="yellow", expand=False))


@app.command("generate")
@graceful
def generate_cmd(
    length: LengthOpt = 4,
    charset: CharsetOpt = Charset.LETTERS,
    mode: ModeOpt = Mode.PRONOUNCEABLE,
    limit: LimitOpt = 100,
    output: Annotated[Path, typer.Option("--output", "-o", help="Where to save candidates.")] = Path("candidates.txt"),
    seed: SeedOpt = None,
    db: DbOpt = Path("handles.db"),
) -> None:
    """Stage 1: generate candidate handles and save them to a text file."""
    with _open_db(db) as store:
        _do_generate(store, length=length, charset=charset, mode=mode, limit=limit, seed=seed, output=output)


@app.command("check")
@graceful
def check_cmd(
    input: Annotated[Path, typer.Option("--input", "-i", help="Text file (one handle per line) or CSV.")] = Path(
        "candidates.txt"
    ),
    output: Annotated[Path, typer.Option("--output", "-o", help="Where to save results.")] = Path("results.csv"),
    confirm: ConfirmOpt = ConfirmKind.NONE,
    ask: AskOpt = False,
    force: Annotated[bool, typer.Option("--force", help="Check again even if a name already has a result.")] = False,
    delay_min: DelayMinOpt = 3.0,
    delay_max: DelayMaxOpt = 6.0,
    daily_cap: DailyCapOpt = 500,
    backoff: BackoffOpt = 60.0,
    profile_dir: ProfileOpt = DEFAULT_PROFILE_DIR,
    browser_path: BrowserPathOpt = None,
    db: DbOpt = Path("handles.db"),
) -> None:
    """Stage 2 (+ optional 3): check candidates and save results to CSV."""
    cfg = _throttle_config(delay_min, delay_max, daily_cap, backoff)
    loaded = _load(input, "Run `handle-finder generate` first, or pass --input.")
    outcome: RunOutcome | None
    with _open_db(db) as store:
        store.add_candidates(loaded.valid, source=f"file:{input.name}")
        throttle = Throttle(cfg)
        outcome = _prefilter_stage(store, loaded.valid, throttle, force=force)
        if not outcome.should_halt:
            confirmed = _confirm_stage(
                store, loaded.valid, confirm, throttle, ask=ask, profile_dir=profile_dir, browser_path=browser_path
            )
            if confirmed is not None and confirmed.should_halt:
                outcome = confirmed
        _export_results(store, loaded.valid, output)
    console.print(DISCLAIMER)
    _exit_for(outcome)


@app.command("rank")
@graceful
def rank_cmd(
    input: Annotated[Path, typer.Option("--input", "-i", help="Results CSV from `check`.")] = Path("results.csv"),
    top: TopOpt = 100,
    output: Annotated[Path, typer.Option("--output", "-o", help="Where to save the ranking.")] = Path("ranked.csv"),
) -> None:
    """Score surviving handles (0-100) and export the top ones."""
    if not input.exists():
        fail(f"Couldn't find '{input}'. Run `handle-finder check` first, or pass --input.")
    try:
        items = read_results_csv(input)
    except (ValueError, OSError, UnicodeDecodeError, csv.Error) as exc:
        fail(f"Couldn't read '{input}': {exc}")
    _rank_and_show(items, top, output)
    console.print(DISCLAIMER)


@app.command("run")
@graceful
def run_cmd(
    length: LengthOpt = 4,
    charset: CharsetOpt = Charset.LETTERS,
    mode: ModeOpt = Mode.PRONOUNCEABLE,
    limit: LimitOpt = 100,
    seed: SeedOpt = None,
    top: TopOpt = 100,
    confirm: ConfirmOpt = ConfirmKind.MANUAL,
    ask: AskOpt = False,
    candidates: Annotated[Path, typer.Option("--candidates", help="Where to save candidates.")] = Path(
        "candidates.txt"
    ),
    results: Annotated[Path, typer.Option("--results", help="Where to save check results.")] = Path("results.csv"),
    ranked: Annotated[Path, typer.Option("--ranked", help="Where to save the ranking.")] = Path("ranked.csv"),
    delay_min: DelayMinOpt = 3.0,
    delay_max: DelayMaxOpt = 6.0,
    daily_cap: DailyCapOpt = 500,
    backoff: BackoffOpt = 60.0,
    profile_dir: ProfileOpt = DEFAULT_PROFILE_DIR,
    browser_path: BrowserPathOpt = None,
    db: DbOpt = Path("handles.db"),
) -> None:
    """Run generate, check, confirm and rank end to end."""
    cfg = _throttle_config(delay_min, delay_max, daily_cap, backoff)
    outcome: RunOutcome | None = None
    with _open_db(db) as store:
        console.rule("Stage 1: generate")
        names = _do_generate(
            store, length=length, charset=charset, mode=mode, limit=limit, seed=seed, output=candidates
        )
        if not names:
            return
        console.rule("Stage 2: pre-filter")
        throttle = Throttle(cfg)
        outcome = _prefilter_stage(store, names, throttle, force=False)
        if not outcome.should_halt:
            console.rule("Stage 3: confirm")
            confirmed = _confirm_stage(
                store, names, confirm, throttle, ask=ask, profile_dir=profile_dir, browser_path=browser_path
            )
            if confirmed is not None and confirmed.should_halt:
                outcome = confirmed
        _export_results(store, names, results)
        if not outcome.should_halt:
            console.rule("Rank")
            survivors = store.by_status(Status.POSSIBLY_AVAILABLE, Status.CONFIRMED_AVAILABLE)
            console.print(f"Ranking all {len(survivors)} surviving handle(s) in the database.")
            _rank_and_show(score_records(survivors), top, ranked)
    console.print(DISCLAIMER)
    _exit_for(outcome)


@app.command("recheck")
@graceful
def recheck_cmd(
    input: Annotated[Path, typer.Option("--input", "-i", help="Shortlist CSV (e.g. ranked.csv) or text file.")],
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Where to save results (default: <input>_rechecked.csv).")
    ] = None,
    confirm: ConfirmOpt = ConfirmKind.MANUAL,
    ask: AskOpt = False,
    delay_min: DelayMinOpt = 3.0,
    delay_max: DelayMaxOpt = 6.0,
    daily_cap: DailyCapOpt = 500,
    backoff: BackoffOpt = 60.0,
    profile_dir: ProfileOpt = DEFAULT_PROFILE_DIR,
    browser_path: BrowserPathOpt = None,
    db: DbOpt = Path("handles.db"),
) -> None:
    """Re-verify a shortlist right before you register."""
    cfg = _throttle_config(delay_min, delay_max, daily_cap, backoff)
    loaded = _load(input, "Pass the shortlist CSV produced by `rank` (e.g. ranked.csv).")
    target = output or input.with_name(f"{input.stem}_rechecked.csv")
    outcome: RunOutcome | None
    with _open_db(db) as store:
        store.add_candidates(loaded.valid, source=f"recheck:{input.name}")
        before = {r.name: r.status for r in store.records(loaded.valid)}
        throttle = Throttle(cfg)
        outcome = _prefilter_stage(store, loaded.valid, throttle, force=True)
        if not outcome.should_halt:
            confirmed = _confirm_stage(
                store, loaded.valid, confirm, throttle,
                ask=ask, profile_dir=profile_dir, browser_path=browser_path, include_confirmed=True,
            )  # fmt: skip
            if confirmed is not None and confirmed.should_halt:
                outcome = confirmed
        rows = _export_results(store, loaded.valid, target)

    table = Table(title="Recheck results")
    table.add_column("handle", style="bold")
    table.add_column("before")
    table.add_column("now")
    table.add_column("score", justify="right")
    table.add_column("notes", overflow="fold")
    still = 0
    for row in rows:
        now = Status(row.status)
        if now in (Status.POSSIBLY_AVAILABLE, Status.CONFIRMED_AVAILABLE):
            still += 1
            style = "green" if now is Status.CONFIRMED_AVAILABLE else "cyan"
        elif now is Status.TAKEN:
            style = "red"
        else:
            style = "yellow"
        prev = before.get(row.handle, Status.UNCHECKED)
        table.add_row(f"@{row.handle}", prev.value, f"[{style}]{now.value}[/]", str(row.score), row.notes)
    console.print(table)
    console.print(
        f"[bold]{still}[/] of {len(rows)} still look available. Availability can change at any "
        "moment, so register the ones you want soon."
    )
    console.print(DISCLAIMER)
    _exit_for(outcome)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
