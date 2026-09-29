"""SQLite store: events, change details, baselines, error signatures, ledger, audit, LLM cache.

Events are plain dicts (JSON-friendly) with these keys::

    id, event_type, service, environment, occurred_at (ISO-8601 UTC), end_at,
    version_from, version_to, significance (1-5), source ('logs' | 'simulated'),
    narrative, payload (dict), related_event_id, outcome_label

``source`` is 'logs' for ANOMALY / ERROR_SIGNATURE (derived from real LogHub logs) and
'simulated' for everything else.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_record (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    service TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'prod',
    occurred_at TEXT NOT NULL,
    occurred_epoch INTEGER NOT NULL,
    end_at TEXT,
    version_from TEXT,
    version_to TEXT,
    significance INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL CHECK (source IN ('logs', 'simulated')),
    narrative TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    related_event_id TEXT,
    outcome_label TEXT
);
CREATE INDEX IF NOT EXISTS ix_event_svc_time ON event_record(service, occurred_epoch);
CREATE TABLE IF NOT EXISTS change_detail (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES event_record(id),
    file TEXT, line_start INTEGER, line_end INTEGER, lines_changed INTEGER, symbol TEXT,
    param_key TEXT, param_old TEXT, param_new TEXT, param_reason TEXT, infra_resource TEXT,
    diff TEXT
);
CREATE INDEX IF NOT EXISTS ix_cd_event ON change_detail(event_id);
CREATE TABLE IF NOT EXISTS baseline (
    service TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'prod',
    stats TEXT NOT NULL,
    refreshed_at TEXT NOT NULL,
    UNIQUE (service, environment)
);
CREATE TABLE IF NOT EXISTS error_signature (
    id TEXT PRIMARY KEY,
    anomaly_id TEXT,
    service TEXT NOT NULL,
    template TEXT NOT NULL,
    count INTEGER NOT NULL,
    first_seen TEXT, last_seen TEXT,
    exemplars TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS ledger (
    session_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    ids TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, line_no)
);
CREATE TABLE IF NOT EXISTS audit_turn (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, turn INTEGER, mode TEXT, stage TEXT,
    question TEXT, context_ids TEXT, anchored_ids TEXT, answer TEXT, ledger TEXT,
    cache_stats TEXT, prefill_ms REAL, decode_ms_per_token REAL, rebuild TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_index (
    path TEXT PRIMARY KEY,
    sha TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);
"""

EVENT_COLUMNS = ("id", "event_type", "service", "environment", "occurred_at", "occurred_epoch", "end_at",
                 "version_from", "version_to", "significance", "source", "narrative", "payload",
                 "related_event_id", "outcome_label")
CD_COLUMNS = ("id", "event_id", "file", "line_start", "line_end", "lines_changed", "symbol", "param_key",
              "param_old", "param_new", "param_reason", "infra_resource", "diff")


def now_iso() -> str:
    """Current UTC time, ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def to_epoch(iso: str) -> int:
    """ISO-8601 -> epoch seconds."""
    return int(datetime.fromisoformat(iso).timestamp())


class SqliteStore:
    """Thin typed wrapper around one SQLite database (``:memory:`` for tests)."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._lock = threading.RLock()

    def _migrate(self) -> None:
        """Add columns introduced after a database was created (keeps old var/ files usable)."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(change_detail)")}
        if "diff" not in cols:
            self.conn.execute("ALTER TABLE change_detail ADD COLUMN diff TEXT")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ events
    def add_events(self, events: Iterable[dict[str, Any]]) -> int:
        """Insert or replace many events; returns the count."""
        rows = []
        for e in events:
            row = {c: e.get(c) for c in EVENT_COLUMNS}
            row["environment"] = row["environment"] or "prod"
            row["occurred_epoch"] = to_epoch(e["occurred_at"])
            row["significance"] = int(e.get("significance") or 1)
            row["narrative"] = e.get("narrative") or ""
            row["payload"] = json.dumps(e.get("payload") or {}, default=str)
            rows.append(tuple(row[c] for c in EVENT_COLUMNS))
        with self._lock, self.conn:
            self.conn.executemany(
                f"INSERT OR REPLACE INTO event_record ({','.join(EVENT_COLUMNS)}) "
                f"VALUES ({','.join('?' * len(EVENT_COLUMNS))})", rows)
        return len(rows)

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["payload"] = json.loads(d["payload"] or "{}")
        return d

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM event_record WHERE id = ?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def get_events(self, services: Sequence[str] | None = None, types: Sequence[str] | None = None,
                   start: str | None = None, end: str | None = None, source: str | None = None,
                   ids: Sequence[str] | None = None, environment: str | None = None,
                   min_significance: int | None = None) -> list[dict[str, Any]]:
        """Filtered events, oldest first."""
        where, args = [], []
        for col, vals in (("service", services), ("event_type", types), ("id", ids)):
            if vals is not None:
                vals = list(vals)
                if not vals:
                    return []
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                args.extend(vals)
        if start:
            where.append("occurred_epoch >= ?")
            args.append(to_epoch(start))
        if end:
            where.append("occurred_epoch <= ?")
            args.append(to_epoch(end))
        if source:
            where.append("source = ?")
            args.append(source)
        if environment:
            where.append("environment = ?")
            args.append(environment)
        if min_significance is not None:
            where.append("significance >= ?")
            args.append(min_significance)
        sql = "SELECT * FROM event_record" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY occurred_epoch, id"
        return [self._event(r) for r in self.conn.execute(sql, args)]

    def set_outcome(self, event_id: str, label: str | None) -> None:
        """Write an outcome label (root_cause / ruled_out) on an event."""
        with self._lock, self.conn:
            self.conn.execute("UPDATE event_record SET outcome_label = ? WHERE id = ?", (label, event_id))

    def count_events(self, by: str) -> dict[str, int]:
        """Counts grouped by one of event_type / source / service."""
        if by not in ("event_type", "source", "service"):
            raise ValueError(by)
        return {r[0]: r[1] for r in self.conn.execute(f"SELECT {by}, COUNT(*) FROM event_record GROUP BY {by} ORDER BY {by}")}

    # ------------------------------------------------------------------ change details
    def add_change_details(self, rows: Iterable[dict[str, Any]]) -> int:
        data = [tuple(r.get(c) for c in CD_COLUMNS) for r in rows]
        with self._lock, self.conn:
            self.conn.executemany(
                f"INSERT OR REPLACE INTO change_detail ({','.join(CD_COLUMNS)}) VALUES ({','.join('?' * len(CD_COLUMNS))})",
                data)
        return len(data)

    def get_change_detail(self, change_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM change_detail WHERE id = ?", (change_id,)).fetchone()
        return dict(row) if row else None

    def get_change_details(self, event_ids: Sequence[str]) -> list[dict[str, Any]]:
        ids = list(event_ids)
        if not ids:
            return []
        sql = f"SELECT * FROM change_detail WHERE event_id IN ({','.join('?' * len(ids))}) ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql, ids)]

    # ------------------------------------------------------------------ baselines
    def upsert_baseline(self, service: str, stats: dict[str, Any], environment: str = "prod") -> None:
        """Insert or update in place: exactly one row per (service, environment)."""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO baseline (service, environment, stats, refreshed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(service, environment) DO UPDATE SET stats = excluded.stats, refreshed_at = excluded.refreshed_at",
                (service, environment, json.dumps(stats, default=str), now_iso()))

    def get_baseline(self, service: str, environment: str = "prod") -> dict[str, Any] | None:
        row = self.conn.execute("SELECT stats, refreshed_at FROM baseline WHERE service = ? AND environment = ?",
                                (service, environment)).fetchone()
        if not row:
            return None
        return {**json.loads(row["stats"]), "refreshed_at": row["refreshed_at"]}

    def baseline_count(self, service: str | None = None) -> int:
        if service:
            return self.conn.execute("SELECT COUNT(*) FROM baseline WHERE service = ?", (service,)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM baseline").fetchone()[0]

    # ------------------------------------------------------------------ error signatures
    def add_error_signatures(self, rows: Iterable[dict[str, Any]]) -> int:
        data = [(r["id"], r.get("anomaly_id"), r["service"], r["template"], int(r["count"]), r.get("first_seen"),
                 r.get("last_seen"), json.dumps(r.get("exemplars") or [])) for r in rows]
        with self._lock, self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO error_signature VALUES (?,?,?,?,?,?,?,?)", data)
        return len(data)

    # ------------------------------------------------------------------ ledger / audit
    def save_ledger(self, session_id: str, lines: Sequence[dict[str, Any]]) -> None:
        """Replace the stored ledger of a session."""
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM ledger WHERE session_id = ?", (session_id,))
            self.conn.executemany(
                "INSERT INTO ledger VALUES (?,?,?,?,?,?)",
                [(session_id, i, ln["kind"], ln["text"], json.dumps(ln.get("ids", [])), now_iso())
                 for i, ln in enumerate(lines)])

    def get_ledger(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT kind, text, ids FROM ledger WHERE session_id = ? ORDER BY line_no",
                                 (session_id,))
        return [{"kind": r["kind"], "text": r["text"], "ids": json.loads(r["ids"])} for r in rows]

    def log_turn(self, session_id: str, turn: int, **fields: Any) -> int:
        """Append one audit row; dict/list fields are stored as JSON."""
        cols = ("mode", "stage", "question", "context_ids", "anchored_ids", "answer", "ledger", "cache_stats",
                "prefill_ms", "decode_ms_per_token", "rebuild")
        vals = [json.dumps(fields.get(c), default=str) if isinstance(fields.get(c), (dict, list, tuple)) else fields.get(c)
                for c in cols]
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"INSERT INTO audit_turn (session_id, turn, {','.join(cols)}, created_at) VALUES (?,?,{','.join('?' * len(cols))},?)",
                (session_id, turn, *vals, now_iso()))
        return int(cur.lastrowid)

    def get_turns(self, session_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM audit_turn WHERE session_id = ? ORDER BY id",
                                                   (session_id,))]

    # ------------------------------------------------------------------ caches
    def cache_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM llm_cache WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def cache_put(self, key: str, value: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?,?)", (key, value, now_iso()))

    def doc_sha(self, path: str) -> str | None:
        row = self.conn.execute("SELECT sha FROM doc_index WHERE path = ?", (path,)).fetchone()
        return row[0] if row else None

    def doc_paths(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT path FROM doc_index ORDER BY path")]

    def forget_doc(self, path: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM doc_index WHERE path = ?", (path,))

    def set_doc_sha(self, path: str, sha: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO doc_index VALUES (?,?,?)", (path, sha, now_iso()))

    def reset_events(self) -> None:
        """Delete events, change details and error signatures (a full re-ingest)."""
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM change_detail")
            self.conn.execute("DELETE FROM error_signature")
            self.conn.execute("DELETE FROM event_record")
