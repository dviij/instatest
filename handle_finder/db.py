"""SQLite storage.

Every result is written and committed the moment it arrives, so a crash or Ctrl+C
loses nothing and the next run resumes where the last one stopped.

Tables:

* ``handles(name PRIMARY KEY, status, checked_at, source, notes)``: one row per
  handle with its latest known status.
* ``check_log``: one row per request sent to Instagram, used to enforce the
  daily cap across runs.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType


class Status(StrEnum):
    """Lifecycle of a handle."""

    UNCHECKED = "UNCHECKED"
    TAKEN = "TAKEN"
    POSSIBLY_AVAILABLE = "POSSIBLY_AVAILABLE"
    CONFIRMED_AVAILABLE = "CONFIRMED_AVAILABLE"
    UNKNOWN = "UNKNOWN"
    RATE_LIMITED = "RATE_LIMITED"


#: Statuses that answer the question; resume skips these.
CONCLUSIVE: frozenset[Status] = frozenset({Status.TAKEN, Status.POSSIBLY_AVAILABLE, Status.CONFIRMED_AVAILABLE})
#: Statuses that make it into the ranking.
SURVIVORS: frozenset[Status] = frozenset({Status.POSSIBLY_AVAILABLE, Status.CONFIRMED_AVAILABLE})
#: Statuses that mean "we learned nothing new"; they never overwrite a conclusive one.
_INCONCLUSIVE: frozenset[Status] = frozenset({Status.UNKNOWN, Status.RATE_LIMITED})

SCHEMA = """
CREATE TABLE IF NOT EXISTS handles (
    name       TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'UNCHECKED',
    checked_at TEXT,
    source     TEXT,
    notes      TEXT
);
CREATE TABLE IF NOT EXISTS check_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    kind      TEXT NOT NULL,
    local_day TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_check_log_day ON check_log(local_day);
"""


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def merge_status(previous: Status | None, new: Status) -> Status:
    """Decide which status to store when ``new`` arrives for a known handle.

    * An inconclusive result (UNKNOWN or RATE_LIMITED) never erases a conclusive
      one: a 429 tells us nothing about whether the name is taken.
    * A pre-filter 404 (POSSIBLY_AVAILABLE) doesn't downgrade an earlier
      CONFIRMED_AVAILABLE, because both say the same thing.
    * Everything else, including TAKEN, replaces the old status.
    """
    if previous is None:
        return new
    if new in _INCONCLUSIVE and previous in CONCLUSIVE:
        return previous
    if new is Status.POSSIBLY_AVAILABLE and previous is Status.CONFIRMED_AVAILABLE:
        return previous
    return new


@dataclass(frozen=True)
class HandleRecord:
    """One row of the ``handles`` table."""

    name: str
    status: Status
    checked_at: str | None = None
    source: str | None = None
    notes: str | None = None


class HandleDB:
    """Thin wrapper around the SQLite database. Use it as a context manager."""

    def __init__(self, path: str | Path) -> None:
        """Open (and create if needed) the database at ``path``."""
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        # FULL sync: each commit is on disk before we move to the next request.
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- lifecycle ----------------------------------------------------------------

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()

    def __enter__(self) -> HandleDB:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- writes -------------------------------------------------------------------

    def add_candidates(self, names: Iterable[str], source: str) -> int:
        """Insert names as UNCHECKED if they aren't already stored.

        Returns the number of new rows.
        """
        before = self._conn.total_changes
        self._conn.executemany(
            "INSERT OR IGNORE INTO handles (name, status, source) VALUES (?, ?, ?)",
            ((n, Status.UNCHECKED.value, source) for n in names),
        )
        self._conn.commit()
        return self._conn.total_changes - before

    def record_result(
        self,
        name: str,
        status: Status,
        *,
        notes: str | None = None,
        source: str | None = None,
    ) -> Status:
        """Save a check result immediately and return the status actually stored.

        See :func:`merge_status` for when an existing status is kept. When it is
        kept, only ``notes`` is updated so the latest attempt is still visible.
        """
        previous = self.get(name)
        final = merge_status(previous.status if previous else None, status)
        now = utc_now_iso()
        if previous is None:
            self._conn.execute(
                "INSERT INTO handles (name, status, checked_at, source, notes) VALUES (?, ?, ?, ?, ?)",
                (name, final.value, now, source, notes),
            )
        elif final is status:
            self._conn.execute(
                "UPDATE handles SET status = ?, checked_at = ?, notes = ? WHERE name = ?",
                (final.value, now, notes, name),
            )
        else:
            kept_note = f"kept {final.value}; latest attempt {status.value}: {notes or ''}".rstrip(": ")
            self._conn.execute("UPDATE handles SET notes = ? WHERE name = ?", (kept_note, name))
        self._conn.commit()
        return final

    def log_check(self, name: str, kind: str) -> None:
        """Record that one request was sent to Instagram (for the daily cap)."""
        self._conn.execute(
            "INSERT INTO check_log (name, kind, local_day, at) VALUES (?, ?, ?, ?)",
            (name, kind, date.today().isoformat(), utc_now_iso()),
        )
        self._conn.commit()

    # -- reads --------------------------------------------------------------------

    @staticmethod
    def _to_record(row: sqlite3.Row) -> HandleRecord:
        return HandleRecord(
            name=row["name"],
            status=Status(row["status"]),
            checked_at=row["checked_at"],
            source=row["source"],
            notes=row["notes"],
        )

    def get(self, name: str) -> HandleRecord | None:
        """Return the stored record for ``name``, or ``None``."""
        row = self._conn.execute("SELECT * FROM handles WHERE name = ?", (name,)).fetchone()
        return self._to_record(row) if row else None

    def records(self, names: Iterable[str]) -> list[HandleRecord]:
        """Records for ``names`` in the given order (UNCHECKED if not stored)."""
        return [self.get(n) or HandleRecord(name=n, status=Status.UNCHECKED) for n in names]

    def by_status(self, *statuses: Status) -> list[HandleRecord]:
        """All records whose status is one of ``statuses``, sorted by name."""
        if not statuses:
            return []
        marks = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM handles WHERE status IN ({marks}) ORDER BY name",
            [s.value for s in statuses],
        ).fetchall()
        return [self._to_record(r) for r in rows]

    def conclusive_names(self) -> set[str]:
        """Names that already have a conclusive answer (skipped on resume)."""
        return {r.name for r in self.by_status(*CONCLUSIVE)}

    def checks_today(self) -> int:
        """How many requests were sent to Instagram today (local date)."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM check_log WHERE local_day = ?", (date.today().isoformat(),)
        ).fetchone()
        return int(row[0])

    def counts(self) -> dict[Status, int]:
        """Number of stored handles per status."""
        result = {s: 0 for s in Status}
        for row in self._conn.execute("SELECT status, COUNT(*) AS n FROM handles GROUP BY status"):
            result[Status(row["status"])] = int(row["n"])
        return result
