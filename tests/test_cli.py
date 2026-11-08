"""End-to-end CLI tests. Instagram is mocked; nothing touches the network."""

import csv
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import handle_finder.cli as cli
from conftest import mock_prefilter, profile_html
from handle_finder.db import HandleDB, Status

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)


def install_instagram(monkeypatch: pytest.MonkeyPatch, decide: Callable[[str], int]) -> list[str]:
    """Route the CLI's PreFilter to a fake Instagram; return the list of requested names."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.strip("/")
        requested.append(name)
        code = decide(name)
        return httpx.Response(code, html=profile_html(name) if code == 200 else "")

    monkeypatch.setattr(cli, "PreFilter", lambda: mock_prefilter(handler))
    return requested


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_generate_writes_file_and_db(tmp_path: Path) -> None:
    out, db = tmp_path / "c.txt", tmp_path / "h.db"
    result = runner.invoke(
        cli.app, ["generate", "-l", "3", "-m", "words", "-n", "7", "--seed", "1", "-o", str(out), "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    assert "Terms of Use" in result.output  # notice printed at startup
    names = out.read_text().split()
    assert len(names) == 7
    with HandleDB(db) as store:
        assert all(store.get(n).status is Status.UNCHECKED for n in names)


def test_run_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requested = install_instagram(monkeypatch, lambda n: 404 if n[0] in "abcdefgh" else 200)
    paths = {k: tmp_path / f"{k}" for k in ("candidates.txt", "results.csv", "ranked.csv", "h.db")}
    args = [
        "run", "-l", "3", "-m", "words", "-n", "12", "--seed", "3", "--confirm", "none",
        "--candidates", str(paths["candidates.txt"]), "--results", str(paths["results.csv"]),
        "--ranked", str(paths["ranked.csv"]), "--db", str(paths["h.db"]),
    ]  # fmt: skip
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert len(requested) == 12
    results = read_rows(paths["results.csv"])
    assert {r["status"] for r in results} <= {"TAKEN", "POSSIBLY_AVAILABLE"}
    ranked = read_rows(paths["ranked.csv"])
    assert list(ranked[0].keys()) == ["handle", "status", "score", "reason", "checked_at"]
    assert all(r["status"] == "POSSIBLY_AVAILABLE" for r in ranked)
    assert [int(r["score"]) for r in ranked] == sorted((int(r["score"]) for r in ranked), reverse=True)
    assert "nothing here is guaranteed" in result.output

    # Second run resumes: already-checked names are not generated or requested again.
    requested.clear()
    again = runner.invoke(cli.app, args)
    assert again.exit_code == 0, again.output
    assert set(requested).isdisjoint({r["handle"] for r in results})


def test_check_skips_invalid_and_already_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requested = install_instagram(monkeypatch, lambda n: 404)
    inp, out, db = tmp_path / "in.txt", tmp_path / "out.csv", tmp_path / "h.db"
    inp.write_text("zeal\n.bad\na..b\nmilo\n", encoding="utf-8")
    first = runner.invoke(cli.app, ["check", "-i", str(inp), "-o", str(out), "--db", str(db)])
    assert first.exit_code == 0, first.output
    assert requested == ["zeal", "milo"]  # invalid names never hit the network
    assert "break Instagram's rules" in first.output

    requested.clear()
    second = runner.invoke(cli.app, ["check", "-i", str(inp), "-o", str(out), "--db", str(db)])
    assert second.exit_code == 0
    assert requested == []
    assert "already checked" in second.output


def test_check_stops_when_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requested = install_instagram(monkeypatch, lambda n: 429)
    inp = tmp_path / "in.txt"
    inp.write_text("zeal\nmilo\nluna\n", encoding="utf-8")
    result = runner.invoke(
        cli.app, ["check", "-i", str(inp), "-o", str(tmp_path / "o.csv"), "--db", str(tmp_path / "h.db")]
    )
    assert result.exit_code == cli.EXIT_BLOCKED
    assert len(requested) == 3  # three tries on the first name, then stop
    flat = " ".join(result.output.replace("│", " ").split())  # ignore panel line wrapping
    assert "Wait a few hours" in flat
    assert (tmp_path / "o.csv").exists()  # partial results still exported


def test_check_missing_input_has_friendly_error(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["check", "-i", str(tmp_path / "nope.txt"), "--db", str(tmp_path / "h.db")])
    assert result.exit_code == 1
    assert "Couldn't find" in result.output
    assert "generate" in result.output


def test_unsafe_delay_is_rejected(tmp_path: Path) -> None:
    inp = tmp_path / "in.txt"
    inp.write_text("zeal\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["check", "-i", str(inp), "--delay-min", "0.1", "--db", str(tmp_path / "h.db")])
    assert result.exit_code != 0


def test_rank_command(tmp_path: Path) -> None:
    inp, out = tmp_path / "results.csv", tmp_path / "ranked.csv"
    inp.write_text(
        "handle,status\ncat,POSSIBLY_AVAILABLE\nxqzt,POSSIBLY_AVAILABLE\nbook,TAKEN\nluna,CONFIRMED_AVAILABLE\n",
        encoding="utf-8",
    )
    result = runner.invoke(cli.app, ["rank", "-i", str(inp), "--top", "2", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert [r["handle"] for r in read_rows(out)] == ["cat", "luna"]


def test_recheck_detects_newly_taken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "h.db"
    with HandleDB(db) as store:
        store.record_result("zeal", Status.CONFIRMED_AVAILABLE)
        store.record_result("milo", Status.POSSIBLY_AVAILABLE)
    shortlist = tmp_path / "ranked.csv"
    shortlist.write_text("handle,status\nzeal,CONFIRMED_AVAILABLE\nmilo,POSSIBLY_AVAILABLE\n", encoding="utf-8")
    install_instagram(monkeypatch, lambda n: 200 if n == "milo" else 404)

    result = runner.invoke(cli.app, ["recheck", "-i", str(shortlist), "--confirm", "none", "--db", str(db)])
    assert result.exit_code == 0, result.output
    rows = {r["handle"]: r["status"] for r in read_rows(tmp_path / "ranked_rechecked.csv")}
    assert rows == {"zeal": "CONFIRMED_AVAILABLE", "milo": "TAKEN"}
    assert "1 of 2 still look available" in result.output
