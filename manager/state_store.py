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
                CREATE TABLE IF NOT EXISTS worker_profiles (
                    worker_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    updated TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS constraint_audits (
                    target_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    updated TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS constraint_audits_updated_idx
                    ON constraint_audits(updated, target_id);
                CREATE TABLE IF NOT EXISTS workstreams (
                    stream_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    updated TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS workstreams_updated_idx
                    ON workstreams(updated, stream_id);
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

    def upsert_worker_profile(self, profile: Mapping[str, Any]) -> None:
        """Persist a local evidence profile without exposing it to telemetry directly."""
        worker_id = str(profile.get("id", "")).strip()
        if not worker_id:
            raise ValueError("worker profile id is required")
        payload = dict(profile)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO worker_profiles
                    (worker_id, provider, model, fingerprint, state, updated, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    provider=excluded.provider,
                    model=excluded.model,
                    fingerprint=excluded.fingerprint,
                    state=excluded.state,
                    updated=excluded.updated,
                    payload=excluded.payload
                """,
                (
                    worker_id,
                    str(payload.get("provider", "unknown")),
                    str(payload.get("model", "unknown")),
                    str(payload.get("fingerprint", "")),
                    str(payload.get("state", "UNKNOWN")),
                    str(payload.get("last_verified", payload.get("updated", ""))),
                    self._json(payload),
                ),
            )

    def get_worker_profile(self, worker_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM worker_profiles WHERE worker_id = ?",
                (str(worker_id),),
            ).fetchone()
        return self._object(row["payload"]) if row else None

    def list_worker_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM worker_profiles ORDER BY worker_id"
            ).fetchall()
        return [self._object(row["payload"]) for row in rows]

    def upsert_constraint_audit(self, audit: Mapping[str, Any]) -> None:
        """Persist local audit findings; callers publish only a sanitized summary."""
        target_id = str(audit.get("target_id", "")).strip()
        if not target_id:
            raise ValueError("constraint audit target_id is required")
        payload = dict(audit)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO constraint_audits (target_id, state, updated, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    state=excluded.state,
                    updated=excluded.updated,
                    payload=excluded.payload
                """,
                (
                    target_id,
                    str(payload.get("state", "UNKNOWN")),
                    str(payload.get("scanned_at", payload.get("updated", ""))),
                    self._json(payload),
                ),
            )

    def get_constraint_audit(self, target_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM constraint_audits WHERE target_id = ?",
                (str(target_id),),
            ).fetchone()
        return self._object(row["payload"]) if row else None

    def list_constraint_audits(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM constraint_audits ORDER BY target_id"
            ).fetchall()
        return [self._object(row["payload"]) for row in rows]

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

    def claim_due_tasks(
        self,
        *,
        limit: int = 10,
        now: float | None = None,
        exclude_kinds: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        now = time.time() if now is None else float(now)
        exclude_kinds = tuple(
            dict.fromkeys(str(kind).strip() for kind in exclude_kinds if str(kind).strip())
        )
        claimed: list[dict[str, Any]] = []
        with self._lock, self._connection:
            conditions = ["status = 'QUEUED'", "scheduled_at <= ?"]
            parameters: list[Any] = [now]
            if exclude_kinds:
                placeholders = ",".join("?" for _ in exclude_kinds)
                conditions.append(f"kind NOT IN ({placeholders})")
                parameters.extend(exclude_kinds)
            parameters.append(limit)
            rows = self._connection.execute(
                f"""
                SELECT task_id, kind, objective_id, attempts, payload
                FROM tasks
                WHERE {' AND '.join(conditions)}
                ORDER BY scheduled_at, task_id
                LIMIT ?
                """,
                parameters,
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

    def find_queued_task(
        self,
        *,
        kind: str,
        payload_key: str,
        payload_value: Any,
    ) -> str | None:
        """Find the oldest queued task owned by one coordinator.

        This is intentionally a local coordination helper. It reads the task
        payload only to match its owner and never exposes that payload through
        the public activity projection.
        """
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT task_id, payload
                FROM tasks
                WHERE kind = ? AND status = 'QUEUED'
                ORDER BY scheduled_at, task_id
                """,
                (str(kind),),
            ).fetchall()
        expected = str(payload_value)
        for row in rows:
            payload = self._object(row["payload"])
            if isinstance(payload, Mapping) and str(payload.get(payload_key, "")) == expected:
                return str(row["task_id"])
        return None

    def count_tasks_for_owner(
        self,
        *,
        kind: str,
        payload_key: str,
        payload_value: Any,
        status: str = "COMPLETE",
    ) -> int:
        """Count local tasks for one owner without exposing their payloads."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload
                FROM tasks
                WHERE kind = ? AND status = ?
                """,
                (str(kind), str(status)),
            ).fetchall()
        expected = str(payload_value)
        count = 0
        for row in rows:
            payload = self._object(row["payload"])
            if isinstance(payload, Mapping) and str(payload.get(payload_key, "")) == expected:
                count += 1
        return count

    def task_activity(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent task metadata without loading private task payloads.

        Recent rows are interleaved by kind. This keeps a busy specialist from
        hiding every other work lane in a compact public activity snapshot.
        """
        limit = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._connection.execute(
                """
                WITH ranked AS (
                    SELECT task_id, kind, objective_id, status, attempts, updated,
                           ROW_NUMBER() OVER (
                               PARTITION BY kind
                               ORDER BY updated DESC, task_id
                           ) AS kind_rank
                    FROM tasks
                )
                SELECT task_id, kind, objective_id, status, attempts, updated
                FROM ranked
                WHERE kind_rank <= ?
                ORDER BY updated DESC, task_id, kind
                """,
                (limit,),
            ).fetchall()
        buckets: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            buckets.setdefault(str(row["kind"]), []).append(row)
        kind_order = sorted(
            buckets,
            key=lambda kind: (-float(buckets[kind][0]["updated"]), kind),
        )
        positions = {kind: 0 for kind in kind_order}
        balanced: list[sqlite3.Row] = []
        while len(balanced) < limit:
            added = False
            for kind in kind_order:
                position = positions[kind]
                if position >= len(buckets[kind]):
                    continue
                balanced.append(buckets[kind][position])
                positions[kind] = position + 1
                added = True
                if len(balanced) >= limit:
                    break
            if not added:
                break
        rows = balanced
        return [
            {
                "task_id": str(row["task_id"]),
                "kind": str(row["kind"]),
                "objective_id": str(row["objective_id"]),
                "status": str(row["status"]),
                "attempts": int(row["attempts"]),
                "updated_at": float(row["updated"]),
            }
            for row in rows
        ]

    def task_status(self, task_id: str) -> str | None:
        """Return one task status without loading its private payload."""
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM tasks WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
        return None if row is None else str(row["status"])

    def start_task(self, task_id: str, *, now: float | None = None) -> bool:
        """Move one queued task to RUNNING and increment its attempt count."""
        now = time.time() if now is None else float(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE tasks
                SET status='RUNNING', attempts=attempts+1, updated=?
                WHERE task_id=? AND status='QUEUED'
                """,
                (now, str(task_id)),
            )
        return cursor.rowcount == 1

    def requeue_running_tasks(self, *, now: float | None = None) -> int:
        """Requeue tasks left RUNNING by an interrupted manager process."""
        now = time.time() if now is None else float(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE tasks
                SET status='QUEUED', scheduled_at=?, updated=?
                WHERE status='RUNNING'
                """,
                (now, now),
            )
        return cursor.rowcount

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

    def task_kind_counts(self) -> dict[str, int]:
        """Return durable task totals grouped by their declared work kind."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT kind, COUNT(*) AS count FROM tasks GROUP BY kind ORDER BY kind"
            ).fetchall()
        return {str(row["kind"]): int(row["count"]) for row in rows}

    def upsert_workstream(self, snapshot: Mapping[str, Any]) -> None:
        stream_id = str(snapshot.get("id", snapshot.get("stream_id", "")))
        if not stream_id:
            raise ValueError("workstream id is required")
        updated = str(snapshot.get("updated", ""))
        if not updated:
            raise ValueError("workstream updated timestamp is required")
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO workstreams (stream_id, state, updated, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(stream_id) DO UPDATE SET
                    state=excluded.state,
                    updated=excluded.updated,
                    payload=excluded.payload
                """,
                (
                    stream_id,
                    str(snapshot.get("state", "UNKNOWN")),
                    updated,
                    self._json(dict(snapshot)),
                ),
            )

    def get_workstream(self, stream_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM workstreams WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()
        return self._object(row["payload"]) if row else None

    def list_workstreams(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM workstreams ORDER BY updated DESC, stream_id"
            ).fetchall()
        return [self._object(row["payload"]) for row in rows]
