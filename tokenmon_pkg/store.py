"""SQLite persistence for normalised usage records.

The database is intentionally small and append-only: every import inserts rows
with INSERT OR IGNORE keyed on a stable record_key hash so re-running a
collection never double counts, and no row is ever updated or deleted.

Schema
------
usage_records
    One row per normalised usage observation.
ingest_runs
    One row per collect invocation.
ingest_sources
    Per tool per run summary: files scanned, records parsed, errors, warnings.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import model as mdl
from .model import METRIC_KEYS, AccountSnapshot, UsageRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tool              TEXT NOT NULL,
    record_key        TEXT NOT NULL UNIQUE,
    ts                TEXT NOT NULL,
    model             TEXT NOT NULL DEFAULT 'unknown',
    provider          TEXT NOT NULL DEFAULT '',
    session_id        TEXT NOT NULL DEFAULT '',
    project           TEXT NOT NULL DEFAULT '',
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    llm_calls         INTEGER NOT NULL DEFAULT 1,
    tool_calls        INTEGER NOT NULL DEFAULT 0,
    is_interaction    INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'ok',
    source_path       TEXT NOT NULL DEFAULT '',
    metadata_json     TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_records_tool_ts    ON usage_records(tool, ts);
CREATE INDEX IF NOT EXISTS idx_records_model_ts   ON usage_records(model, ts);
CREATE INDEX IF NOT EXISTS idx_records_project_ts ON usage_records(project, ts);
CREATE INDEX IF NOT EXISTS idx_records_ts         ON usage_records(ts);
CREATE INDEX IF NOT EXISTS idx_records_session    ON usage_records(session_id);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    duration_ms  INTEGER,
    db_path      TEXT NOT NULL DEFAULT '',
    tools        TEXT NOT NULL DEFAULT '',
    inserted     INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    errors       INTEGER NOT NULL DEFAULT 0,
    warnings     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ingest_sources (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
    tool           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'ok',
    scanned_files  INTEGER NOT NULL DEFAULT 0,
    parsed_records INTEGER NOT NULL DEFAULT 0,
    inserted       INTEGER NOT NULL DEFAULT 0,
    skipped        INTEGER NOT NULL DEFAULT 0,
    errors         INTEGER NOT NULL DEFAULT 0,
    warnings       INTEGER NOT NULL DEFAULT 0,
    message        TEXT NOT NULL DEFAULT '',
    root_paths     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sources_run ON ingest_sources(run_id);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    tool             TEXT NOT NULL,
    record_key       TEXT NOT NULL UNIQUE,
    ts               TEXT NOT NULL,
    kind             TEXT NOT NULL,
    account          TEXT NOT NULL DEFAULT '',
    label            TEXT NOT NULL DEFAULT '',
    currency         TEXT NOT NULL DEFAULT '',
    amount           REAL NOT NULL DEFAULT 0,
    raw_amount       REAL NOT NULL DEFAULT 0,
    used_percent     REAL,
    remaining_percent REAL,
    window_minutes   INTEGER,
    resets_at        TEXT,
    opening_amount   REAL,
    plan             TEXT NOT NULL DEFAULT '',
    source_path      TEXT NOT NULL DEFAULT '',
    metadata_json    TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_accounts_tool_ts ON account_snapshots(tool, ts);
CREATE INDEX IF NOT EXISTS idx_accounts_kind    ON account_snapshots(kind);
"""

ACCOUNT_COLUMNS = (
    "tool",
    "record_key",
    "ts",
    "kind",
    "account",
    "label",
    "currency",
    "amount",
    "raw_amount",
    "used_percent",
    "remaining_percent",
    "window_minutes",
    "resets_at",
    "opening_amount",
    "plan",
    "source_path",
    "metadata_json",
)

RECORD_COLUMNS = (
    "tool",
    "record_key",
    "ts",
    "model",
    "provider",
    "session_id",
    "project",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
    "llm_calls",
    "tool_calls",
    "is_interaction",
    "status",
    "source_path",
    "metadata_json",
)


@dataclass
class SourceReport:
    """Result of scanning a single tool."""

    tool: str
    status: str = "ok"
    scanned_files: int = 0
    parsed_records: int = 0
    inserted: int = 0
    skipped: int = 0
    errors: int = 0
    warnings: int = 0
    message: str = ""
    root_paths: list = field(default_factory=list)
    details: list = field(default_factory=list)

    def warn(self, text: str) -> None:
        self.warnings += 1
        self.details.append("warning: " + text)

    def error(self, text: str) -> None:
        self.errors += 1
        self.details.append("error: " + text)

    def to_row(self, run_id: int) -> tuple:
        return (
            run_id,
            self.tool,
            self.status,
            self.scanned_files,
            self.parsed_records,
            self.inserted,
            self.skipped,
            self.errors,
            self.warnings,
            self.message,
            ";".join(self.root_paths),
        )


@dataclass
class RunReport:
    """Result of one full collect invocation."""

    run_id: int
    started_at: datetime
    tools: list = field(default_factory=list)
    inserted: int = 0
    skipped: int = 0
    errors: int = 0
    warnings: int = 0
    sources: dict = field(default_factory=dict)
    db_path: str = ""

    @property
    def finished(self) -> bool:
        return bool(self.sources)

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "db_path": self.db_path,
            "tools": self.tools,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "errors": self.errors,
            "warnings": self.warnings,
            "sources": {
                name: {
                    "status": rep.status,
                    "scanned_files": rep.scanned_files,
                    "parsed_records": rep.parsed_records,
                    "inserted": rep.inserted,
                    "skipped": rep.skipped,
                    "errors": rep.errors,
                    "warnings": rep.warnings,
                    "message": rep.message,
                    "root_paths": rep.root_paths,
                }
                for name, rep in self.sources.items()
            },
        }


class Store:
    """Thin, dependency free SQLite wrapper."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    @contextmanager
    def transaction(self):
        with self._conn as conn:
            yield conn

    # -- ingestion ---------------------------------------------------------
    def start_run(self, tools):
        cur = self._conn.execute(
            "INSERT INTO ingest_runs (started_at, db_path, tools) VALUES (?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                str(self.db_path),
                ",".join(tools),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id, inserted, skipped, errors, warnings, duration_ms):
        self._conn.execute(
            "UPDATE ingest_runs SET finished_at = ?, duration_ms = ?, inserted = ?, "
            "skipped = ?, errors = ?, warnings = ? WHERE id = ?",
            (
                datetime.now(timezone.utc).isoformat(),
                duration_ms,
                inserted,
                skipped,
                errors,
                warnings,
                run_id,
            ),
        )
        self._conn.commit()

    def save_source(self, run_id, report):
        self._conn.execute(
            "INSERT INTO ingest_sources (run_id, tool, status, scanned_files, "
            "parsed_records, inserted, skipped, errors, warnings, message, root_paths) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            report.to_row(run_id),
        )
        self._conn.commit()

    def insert_many(self, records):
        """Insert records, ignoring duplicates by record_key.

        Returns (inserted, skipped).
        """
        rows = [rec.to_row() for rec in records]
        if not rows:
            return 0, 0
        placeholders = ",".join(["?"] * len(RECORD_COLUMNS))
        sql = (
            "INSERT OR IGNORE INTO usage_records (" + ",".join(RECORD_COLUMNS) + ") "
            "VALUES (" + placeholders + ")"
        )
        with self.transaction() as conn:
            before = conn.total_changes
            conn.executemany(sql, rows)
            inserted = conn.total_changes - before
        return inserted, len(rows) - inserted

    def insert_accounts(self, snapshots) -> tuple:
        """Insert account snapshots, ignoring duplicates by record_key."""
        rows = [s.to_row() for s in snapshots]
        if not rows:
            return 0, 0
        placeholders = ",".join(["?"] * len(ACCOUNT_COLUMNS))
        sql = ("INSERT OR IGNORE INTO account_snapshots ("
               + ",".join(ACCOUNT_COLUMNS) + ") VALUES (" + placeholders + ")")
        with self.transaction() as conn:
            before = conn.total_changes
            conn.executemany(sql, rows)
            inserted = conn.total_changes - before
        return inserted, len(rows) - inserted

    # -- querying ----------------------------------------------------------
    def count(self):
        row = self._conn.execute("SELECT COUNT(*) AS n FROM usage_records").fetchone()
        return int(row["n"]) if row else 0

    def latest_ts(self):
        row = self._conn.execute("SELECT MAX(ts) AS ts FROM usage_records").fetchone()
        return row["ts"] if row else None

    def iter_records(self, tools=None, models=None, projects=None,
                     since=None, until=None, limit=None):
        """Yield stored records as UsageRecord objects."""
        clauses = []
        params = []
        if tools:
            clauses.append("tool IN (%s)" % ",".join("?" * len(tools)))
            params.extend(tools)
        if models:
            clauses.append("model IN (%s)" % ",".join("?" * len(models)))
            params.extend(models)
        if projects:
            clauses.append("project IN (%s)" % ",".join("?" * len(projects)))
            params.extend(projects)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(mdl.to_utc(since).isoformat())
        if until is not None:
            clauses.append("ts <= ?")
            params.append(mdl.to_utc(until).isoformat())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT " + ",".join(RECORD_COLUMNS) + " FROM usage_records" + where + " ORDER BY ts ASC"
        if limit:
            sql += " LIMIT " + str(int(limit))
        for row in self._conn.execute(sql, params):
            yield _row_to_record(row)

    def iter_accounts(self, tools=None, kinds=None, since=None, until=None):
        """Yield stored account snapshots as AccountSnapshot objects."""
        clauses = []
        params = []
        if tools:
            clauses.append("tool IN (%s)" % ",".join("?" * len(tools)))
            params.extend(tools)
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend(kinds)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(mdl.to_utc(since).isoformat())
        if until is not None:
            clauses.append("ts <= ?")
            params.append(mdl.to_utc(until).isoformat())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = ("SELECT " + ",".join(ACCOUNT_COLUMNS)
               + " FROM account_snapshots" + where + " ORDER BY ts ASC")
        for row in self._conn.execute(sql, params):
            yield _row_to_snapshot(row)

    def latest_accounts(self):
        """Most recent snapshot per (tool, kind, account, label)."""
        rows = self._conn.execute(
            "SELECT * FROM account_snapshots ORDER BY ts DESC, id DESC"
        ).fetchall()
        seen = set()
        latest = []
        for row in rows:
            key = (row["tool"], row["kind"], row["account"], row["label"])
            if key in seen:
                continue
            seen.add(key)
            latest.append(_row_to_snapshot(row))
        return latest

    def distinct(self, column):
        """Sorted distinct values of a column (used to populate UI filters)."""
        if column not in RECORD_COLUMNS:
            raise ValueError("unsupported column: " + column)
        rows = self._conn.execute(
            "SELECT DISTINCT " + column + " AS v FROM usage_records WHERE " + column
            + " != '' ORDER BY v"
        ).fetchall()
        return [row["v"] for row in rows]

    def last_runs(self, limit=10):
        rows = self._conn.execute(
            "SELECT * FROM ingest_runs ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(row) for row in rows]

    def last_run_sources(self, run_id):
        rows = self._conn.execute(
            "SELECT * FROM ingest_sources WHERE run_id = ? ORDER BY id", (int(run_id),)
        ).fetchall()
        return [dict(row) for row in rows]


def _row_to_record(row):
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    return UsageRecord(
        tool=row["tool"],
        record_key=row["record_key"],
        ts=mdl.to_utc(row["ts"]) or datetime.now(timezone.utc),
        model=row["model"],
        provider=row["provider"],
        session_id=row["session_id"],
        project=row["project"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        reasoning_tokens=row["reasoning_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_write_tokens=row["cache_write_tokens"],
        total_tokens=row["total_tokens"],
        llm_calls=row["llm_calls"],
        tool_calls=row["tool_calls"],
        is_interaction=row["is_interaction"],
        status=row["status"],
        source_path=row["source_path"],
        metadata=metadata,
    )


def _row_to_snapshot(row):
    import json as _json

    try:
        metadata = _json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    return AccountSnapshot(
        tool=row["tool"],
        record_key=row["record_key"],
        ts=mdl.to_utc(row["ts"]) or datetime.now(timezone.utc),
        kind=row["kind"],
        account=row["account"],
        label=row["label"],
        currency=row["currency"],
        amount=row["amount"],
        raw_amount=row["raw_amount"],
        used_percent=row["used_percent"],
        window_minutes=row["window_minutes"],
        resets_at=mdl.to_utc(row["resets_at"]) if row["resets_at"] else None,
        opening_amount=row["opening_amount"],
        plan=row["plan"],
        source_path=row["source_path"],
        metadata=metadata,
    )


def now_ms():
    return int(time.time() * 1000)
