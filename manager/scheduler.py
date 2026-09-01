"""Persistent queue primitives for future jobs and agent work."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from .state_store import StateStore


@dataclass(frozen=True)
class WorkItem:
    task_id: str
    kind: str
    objective_id: str
    attempts: int
    payload: Mapping[str, Any]


class WorkScheduler:
    """A small durable queue that keeps orchestration separate from workers."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def enqueue(
        self,
        *,
        kind: str,
        objective_id: str,
        payload: Mapping[str, Any] | None = None,
        task_id: str | None = None,
        scheduled_at: float | None = None,
    ) -> str:
        task_id = task_id or f"task-{uuid.uuid4().hex[:12]}"
        self.store.enqueue_task(
            task_id=task_id,
            kind=kind,
            objective_id=objective_id,
            payload=payload,
            scheduled_at=scheduled_at,
        )
        return task_id

    def claim(self, *, limit: int = 10) -> list[WorkItem]:
        return [
            WorkItem(
                task_id=item["task_id"],
                kind=item["kind"],
                objective_id=item["objective_id"],
                attempts=item["attempts"],
                payload=item["payload"],
            )
            for item in self.store.claim_due_tasks(limit=limit)
        ]

    def start(self, task_id: str) -> bool:
        """Start one task that this runtime just enqueued."""
        return self.store.start_task(task_id)

    def recover_interrupted(self) -> int:
        """Return tasks left RUNNING by a prior manager process to the queue."""
        return self.store.requeue_running_tasks()

    def complete(self, task_id: str) -> None:
        self.store.finish_task(task_id, status="COMPLETE")

    def fail(self, task_id: str, *, retry_at: float | None = None) -> None:
        if retry_at is not None:
            self.store.finish_task(task_id, status="QUEUED", scheduled_at=retry_at)
            return
        self.store.finish_task(task_id, status="FAILED")

    def snapshot(self) -> dict[str, int]:
        return self.store.task_counts()

    def kind_snapshot(self) -> dict[str, int]:
        """Return durable task totals grouped by their work kind."""
        return self.store.task_kind_counts()
