"""Small, dependency-free durable state store for Machine Manager.

The store is deliberately local-only. It keeps operational history and retry
state on the machine so a manager restart does not erase the story of what
happened or reset a bounded retry budget. Public telemetry is produced by the
separate allowlisted publisher.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping


class StateStore:
    """SQLite-backed state, event, agent, and work-queue store."""

    def __init__(self, path: Path, *, event_retention: int = 5000) -> None:
        if event_retention < 100:
            raise ValueError("event_retention must be at least 100")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.event_retention = event_retention
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path),
            timeout=30,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    objective_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    restart_count INTEGER NOT NULL DEFAULT 0,
                    updated TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_timestamp_idx
                    ON events(timestamp, event_id);
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    updated TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    objective_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    scheduled_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_due_idx
                    ON tasks(status, scheduled_at, updated);
                CREATE TABLE IF NOT EXISTS outreach_contacts (
                    channel TEXT NOT NULL,
                    recipient_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    first_contacted_at TEXT,
                    last_contacted_at TEXT,
                    updated TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (channel, recipient_hash)
                );
                """
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))

    @staticmethod
    def _object(value: str) -> dict[str, Any]:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def append_event(self, event: Mapping[str, Any]) -> None:
        event_id = str(event.get("event_id", ""))
        if not event_id:
            raise ValueError("event.event_id is required")
        timestamp = str(event.get("timestamp", ""))
        if not timestamp:
            raise ValueError("event.timestamp is required")
        payload = self._json(dict(event))
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO events
                    (event_id, timestamp, job_id, event_type, state, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    timestamp,
                    str(event.get("job_id", "")),
                    str(event.get("event_type", "event")),
                    str(event.get("new_state", event.get("state", "UNKNOWN"))),
                    payload,
                ),
            )
            self._connection.execute(
                """
                DELETE FROM events
                WHERE rowid NOT IN (
                    SELECT rowid FROM events
                    ORDER BY timestamp DESC, rowid DESC
                    LIMIT ?
                )
                """,
                (self.event_retention,),
            )

    def append_events(self, events: Iterable[Mapping[str, Any]]) -> None:
        for event in events:
            self.append_event(event)

    def recent_events(self, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), self.event_retention))
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload FROM events
                ORDER BY timestamp DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._object(row["payload"]) for row in reversed(rows)]

    def event_count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()
        return int(row["count"]) if row else 0

    def upsert_job(self, snapshot: Mapping[str, Any]) -> None:
        job_id = str(snapshot.get("job_id", snapshot.get("id", "")))
        if not job_id:
            raise ValueError("job snapshot id is required")
        payload = dict(snapshot)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO jobs
                    (job_id, objective_id, state, attempt, restart_count, updated, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    objective_id=excluded.objective_id,
                    state=excluded.state,
                    attempt=excluded.attempt,
                    restart_count=excluded.restart_count,
                    updated=excluded.updated,
                    payload=excluded.payload
                """,
                (
                    job_id,
                    str(snapshot.get("objective_id", "")),
                    str(snapshot.get("state", "UNKNOWN")),
                    int(snapshot.get("attempt", 0) or 0),
                    int(snapshot.get("restart_count", 0) or 0),
                    str(snapshot.get("updated", "")),
                    self._json(payload),
                ),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._object(row["payload"]) if row else None

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM jobs ORDER BY job_id"
            ).fetchall()
        return [self._object(row["payload"]) for row in rows]

    def upsert_agent(self, snapshot: Mapping[str, Any]) -> None:
        agent_id = str(snapshot.get("id", ""))
        if not agent_id:
            raise ValueError("agent snapshot id is required")
        updated = str(snapshot.get("last_run", snapshot.get("updated", "")))
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agents (agent_id, state, updated, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    state=excluded.state,
                    updated=excluded.updated,
                    payload=excluded.payload
                """,
                (
                    agent_id,
                    str(snapshot.get("state", "UNKNOWN")),
                    updated,
                    self._json(dict(snapshot)),
                ),
            )

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM agents ORDER BY agent_id"
            ).fetchall()
        return [self._object(row["payload"]) for row in rows]

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(key), self._json(value)),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM meta WHERE key = ?",
                (str(key),),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def upsert_outreach_contact(
        self,
        *,
        channel: str,
        recipient_hash: str,
        state: str,
        first_contacted_at: str | None,
        last_contacted_at: str | None,
        updated: str,
    ) -> None:
        """Store only a recipient hash so outreach state never becomes public data."""
        payload = {
            "channel": str(channel),
            "recipient_hash": str(recipient_hash),
            "state": str(state),
            "first_contacted_at": first_contacted_at,
            "last_contacted_at": last_contacted_at,
            "updated": str(updated),
        }
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO outreach_contacts
                    (channel, recipient_hash, state, first_contacted_at, last_contacted_at, updated, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, recipient_hash) DO UPDATE SET
                    state=excluded.state,
                    first_contacted_at=excluded.first_contacted_at,
                    last_contacted_at=excluded.last_contacted_at,
                    updated=excluded.updated,
                    payload=excluded.payload
                """,
                (
                    payload["channel"],
                    payload["recipient_hash"],
                    payload["state"],
                    payload["first_contacted_at"],
                    payload["last_contacted_at"],
                    payload["updated"],
                    self._json(payload),
                ),
            )

    def get_outreach_contact(self, *, channel: str, recipient_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT payload FROM outreach_contacts
                WHERE channel = ? AND recipient_hash = ?
                """,
                (str(channel), str(recipient_hash)),
            ).fetchone()
        return self._object(row["payload"]) if row else None

    def enqueue_task(
        self,
        *,
        task_id: str,
        kind: str,
        objective_id: str,
        payload: Mapping[str, Any] | None = None,
        scheduled_at: float | None = None,
    ) -> None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO tasks
                    (task_id, kind, objective_id, status, scheduled_at, attempts, updated, payload)
                VALUES (?, ?, ?, 'QUEUED', ?, 0, ?, ?)
                ON CONFLICT(task_id) DO NOTHING
                """,
                (
                    task_id,
                    kind,
                    objective_id,
                    now if scheduled_at is None else float(scheduled_at),
                    now,
                    self._json(dict(payload or {})),
                ),
            )

    def claim_due_tasks(self, *, limit: int = 10, now: float | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        now = time.time() if now is None else float(now)
        claimed: list[dict[str, Any]] = []
        with self._lock, self._connection:
            rows = self._connection.execute(
                """
                SELECT task_id, kind, objective_id, attempts, payload
                FROM tasks
                WHERE status = 'QUEUED' AND scheduled_at <= ?
                ORDER BY scheduled_at, task_id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            for row in rows:
                self._connection.execute(
                    """
                    UPDATE tasks
                    SET status='RUNNING', attempts=attempts+1, updated=?
                    WHERE task_id=? AND status='QUEUED'
                    """,
                    (now, row["task_id"]),
                )
                item = {
                    "task_id": row["task_id"],
                    "kind": row["kind"],
                    "objective_id": row["objective_id"],
                    "attempts": int(row["attempts"]) + 1,
                    "payload": self._object(row["payload"]),
                }
                claimed.append(item)
        return claimed

    def finish_task(
        self,
        task_id: str,
        *,
        status: str = "COMPLETE",
        scheduled_at: float | None = None,
    ) -> None:
        allowed = {"COMPLETE", "FAILED", "ESCALATED", "CANCELLED", "QUEUED"}
        if status not in allowed:
            raise ValueError(f"unsupported task status: {status}")
        now = time.time()
        with self._lock, self._connection:
            if scheduled_at is None:
                self._connection.execute(
                    "UPDATE tasks SET status=?, updated=? WHERE task_id=?",
                    (status, now, task_id),
                )
            else:
                self._connection.execute(
                    "UPDATE tasks SET status=?, scheduled_at=?, updated=? WHERE task_id=?",
                    (status, float(scheduled_at), now, task_id),
                )

    def task_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}
