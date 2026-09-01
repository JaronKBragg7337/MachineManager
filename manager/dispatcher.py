"""Explicit, bounded dispatch for durable work items.

The live KeyHunt supervisor does not use this dispatcher. A future research,
build, or delivery worker can register a handler and receive durable work
without making the protected Puzzle #71 assignment mutable by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Callable, Mapping
import uuid

from .scheduler import WorkItem, WorkScheduler
from .supervisor import utc_now


@dataclass(frozen=True)
class DispatchOutcome:
    """The only state changes a registered task handler may request."""

    status: str = "COMPLETE"
    retry_after_s: float = 300.0
    metrics: Mapping[str, int | float | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchResult:
    task_id: str
    kind: str
    status: str
    attempts: int
    metrics: Mapping[str, int | float | bool] = field(default_factory=dict)


DispatchHandler = Callable[[WorkItem], DispatchOutcome]


class WorkDispatcher:
    """Run registered task handlers in a small, auditable batch."""

    _TERMINAL = {"COMPLETE", "FAILED", "ESCALATED"}

    def __init__(
        self,
        scheduler: WorkScheduler,
        handlers: Mapping[str, DispatchHandler] | None = None,
        *,
        defer_delay_s: float = 300.0,
        max_attempts: int = 3,
        actor: str = "queue-dispatcher",
    ) -> None:
        if defer_delay_s <= 0:
            raise ValueError("defer_delay_s must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.scheduler = scheduler
        self.handlers = dict(handlers or {})
        self.defer_delay_s = float(defer_delay_s)
        self.max_attempts = int(max_attempts)
        self.actor = str(actor or "queue-dispatcher")[:80]

    def _record(
        self,
        item: WorkItem,
        *,
        event_type: str,
        new_state: str,
        action: str,
        outcome: str,
        error: str | None = None,
        metrics: Mapping[str, int | float | bool] | None = None,
    ) -> None:
        """Persist a public-safe lifecycle event for one task attempt."""
        event_metrics: dict[str, int | float | bool] = {"attempt": item.attempts}
        event_metrics.update(dict(metrics or {}))
        self.scheduler.store.append_event(
            {
                "timestamp": utc_now(),
                "event_id": f"evt-queue-task-{uuid.uuid4().hex[:12]}",
                "objective_id": item.objective_id,
                "job_id": "task-queue",
                "worker_id": "",
                "actor": self.actor,
                "event_type": event_type,
                "previous_state": "QUEUED" if event_type == "queue_task_claimed" else "RUNNING",
                "new_state": new_state,
                "metrics": event_metrics,
                "action": action,
                "outcome": outcome,
                "artifact_refs": [f"task:{item.task_id}"],
                "error": error,
                "duration": None,
            }
        )

    def dispatch(self, *, limit: int = 10, now: float | None = None) -> list[DispatchResult]:
        """Dispatch due work and return a compact result for each claimed item."""
        current = time.time() if now is None else float(now)
        results: list[DispatchResult] = []
        for item in self.scheduler.claim(limit=limit, now=current):
            self._record(
                item,
                event_type="queue_task_claimed",
                new_state="RUNNING",
                action="claim_queue_task",
                outcome="dispatch_attempt_started",
            )
            handler = self.handlers.get(item.kind)
            if handler is None:
                self.scheduler.defer(item.task_id, retry_at=current + self.defer_delay_s)
                self._record(
                    item,
                    event_type="queue_task_deferred",
                    new_state="QUEUED",
                    action="defer_unhandled_task",
                    outcome="no_handler_registered",
                )
                results.append(
                    DispatchResult(item.task_id, item.kind, "DEFERRED", item.attempts)
                )
                continue

            result_metrics: Mapping[str, int | float | bool] = {}
            try:
                outcome = handler(item)
                status = str(outcome.status).upper()
                result_metrics = dict(outcome.metrics)
                if status not in self._TERMINAL and status != "RETRY":
                    raise ValueError(f"unsupported dispatch outcome: {status}")
                if status == "COMPLETE":
                    self.scheduler.complete(item.task_id)
                    self._record(
                        item,
                        event_type="queue_task_completed",
                        new_state="COMPLETE",
                        action="complete_queue_task",
                        outcome="handler_completed",
                        metrics=result_metrics,
                    )
                elif status == "RETRY":
                    retry_after = max(0.1, float(outcome.retry_after_s))
                    if item.attempts >= self.max_attempts:
                        status = "ESCALATED"
                        self.scheduler.escalate(item.task_id)
                        self._record(
                            item,
                            event_type="queue_task_escalated",
                            new_state="ESCALATED",
                            action="escalate_queue_task",
                            outcome="retry_budget_exhausted",
                        )
                    else:
                        self.scheduler.defer(item.task_id, retry_at=current + retry_after)
                        self._record(
                            item,
                            event_type="queue_task_retry",
                            new_state="QUEUED",
                            action="retry_queue_task",
                            outcome="handler_requested_retry",
                            metrics={**result_metrics, "retry_after_s": retry_after},
                        )
                elif status == "FAILED":
                    self.scheduler.fail(item.task_id)
                    self._record(
                        item,
                        event_type="queue_task_failed",
                        new_state="FAILED",
                        action="fail_queue_task",
                        outcome="handler_reported_failure",
                        metrics=result_metrics,
                    )
                else:
                    self.scheduler.escalate(item.task_id)
                    self._record(
                        item,
                        event_type="queue_task_escalated",
                        new_state="ESCALATED",
                        action="escalate_queue_task",
                        outcome="handler_requested_escalation",
                        metrics=result_metrics,
                    )
            except Exception as error:
                if item.attempts >= self.max_attempts:
                    status = "ESCALATED"
                    self.scheduler.escalate(item.task_id)
                    event_type = "queue_task_escalated"
                    new_state = "ESCALATED"
                    action = "escalate_queue_task"
                    outcome = "handler_error_retry_budget_exhausted"
                else:
                    status = "RETRY"
                    self.scheduler.defer(item.task_id, retry_at=current + self.defer_delay_s)
                    event_type = "queue_task_retry"
                    new_state = "QUEUED"
                    action = "retry_queue_task"
                    outcome = "handler_error_retry_scheduled"
                self._record(
                    item,
                    event_type=event_type,
                    new_state=new_state,
                    action=action,
                    outcome=outcome,
                    error=type(error).__name__,
                )
            results.append(
                DispatchResult(item.task_id, item.kind, status, item.attempts, result_metrics)
            )
        return results
