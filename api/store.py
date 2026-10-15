"""What the agent service keeps: the alarm firings Alertmanager sent, and the investigations they
started. A SQLite file (config.AGENT_DB), so that a restart of the service forgets nothing.

A firing is one alert instance (Alertmanager's fingerprint, the labels of one alert) from the
moment it started firing (startsAt). Repeats of the same firing change nothing; a new firing of
an alert that already resolved is a new firing.
"""
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# The states of an investigation. queued -> running -> completed | awaiting_decision |
# not_firing | failed; awaiting_decision -> completed once a person decides (or it lapses);
# no_runbook: recorded, never run (the agent has no runbook for the alert).
QUEUED, RUNNING, COMPLETED, NOT_FIRING, FAILED, NO_RUNBOOK = "queued", "running", "completed", "not_firing", "failed", "no_runbook"
AWAITING_DECISION = "awaiting_decision"
# An investigation still open to new firings of its alert: one incident, one investigation.
OPEN = (QUEUED, RUNNING, AWAITING_DECISION)
# What a decision can be: by a person, or lapsed (every firing behind it resolved first).
APPROVED, REJECTED, LAPSED = "approved", "rejected", "lapsed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    trace_id TEXT,
    root_cause TEXT,
    error TEXT,
    proposal TEXT,
    decision TEXT,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT
);
CREATE TABLE IF NOT EXISTS firings (
    fingerprint TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    alert_name TEXT NOT NULL,
    labels TEXT NOT NULL,
    received_at TEXT NOT NULL,
    resolved_at TEXT,
    investigation_id INTEGER NOT NULL REFERENCES investigations(id),
    PRIMARY KEY (fingerprint, starts_at)
);
"""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path | str, now=utc_now):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # one process, one event loop: the connection is only ever used from it
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        # a file made before decisions existed gets their columns
        present = {row["name"] for row in self._db.execute("PRAGMA table_info(investigations)")}
        with self._db:
            for column in ("proposal", "decision", "decided_by", "decided_at", "decision_note"):
                if column not in present:
                    self._db.execute(f"ALTER TABLE investigations ADD COLUMN {column} TEXT")
        self._now = now

    # ---- firings

    def firing(self, fingerprint: str, starts_at: str) -> dict | None:
        row = self._db.execute("SELECT * FROM firings WHERE fingerprint = ? AND starts_at = ?", (fingerprint, starts_at)).fetchone()
        return _firing(row) if row else None

    def add_firing(self, fingerprint: str, starts_at: str, alert_name: str, labels: dict, investigation_id: int) -> None:
        with self._db:
            self._db.execute("INSERT INTO firings VALUES (?, ?, ?, ?, ?, NULL, ?)",
                             (fingerprint, starts_at, alert_name, json.dumps(labels, sort_keys=True), self._now(), investigation_id))

    def resolve(self, fingerprint: str, starts_at: str, at: str) -> bool:
        """True when the firing was known and is now marked resolved."""
        with self._db:
            changed = self._db.execute("UPDATE firings SET resolved_at = ? WHERE fingerprint = ? AND starts_at = ? AND resolved_at IS NULL",
                                       (at, fingerprint, starts_at)).rowcount
        return changed == 1

    def firings_of(self, investigation_id: int) -> list[dict]:
        rows = self._db.execute("SELECT * FROM firings WHERE investigation_id = ? ORDER BY received_at, fingerprint",
                                (investigation_id,)).fetchall()
        return [_firing(row) for row in rows]

    # ---- investigations

    def create(self, alert_name: str, status: str = QUEUED) -> int:
        with self._db:
            return self._db.execute("INSERT INTO investigations (alert_name, status, created_at) VALUES (?, ?, ?)",
                                    (alert_name, status, self._now())).lastrowid

    def update(self, investigation_id: int, **fields) -> None:
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self._db:
            self._db.execute(f"UPDATE investigations SET {columns} WHERE id = ?", (*fields.values(), investigation_id))  # column names are ours

    def investigation(self, investigation_id: int) -> dict | None:
        row = self._db.execute("SELECT * FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
        return dict(row) if row else None

    def investigations(self, limit: int = 50, status: str | None = None) -> list[dict]:
        if status is None:
            rows = self._db.execute("SELECT * FROM investigations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM investigations WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        return [dict(row) for row in rows]

    def open_for(self, alert_name: str) -> dict | None:
        """The investigation of this alert that is queued, running or waiting for a decision, if any."""
        row = self._db.execute("SELECT * FROM investigations WHERE alert_name = ? AND status IN (?, ?, ?) ORDER BY id LIMIT 1",
                               (alert_name, *OPEN)).fetchone()
        return dict(row) if row else None

    def next_queued(self) -> dict | None:
        row = self._db.execute("SELECT * FROM investigations WHERE status = ? ORDER BY id LIMIT 1", (QUEUED,)).fetchone()
        return dict(row) if row else None

    def requeue_running(self) -> list[int]:
        """After a restart: an investigation that was running when the service stopped is queued again."""
        ids = [row["id"] for row in self._db.execute("SELECT id FROM investigations WHERE status = ?", (RUNNING,))]
        with self._db:
            self._db.execute("UPDATE investigations SET status = ?, started_at = NULL WHERE status = ?", (QUEUED, RUNNING))
        return ids


def _firing(row: sqlite3.Row) -> dict:
    return dict(row) | {"labels": json.loads(row["labels"])}
