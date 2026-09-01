"""Explicit, bounded dispatch for durable work items.

The live KeyHunt supervisor does not use this dispatcher. A future research,
build, or delivery worker can register a handler and receive durable work
without making the protected Puzzle #71 assignment mutable by accident.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import time
from typing import Callable, Iterable, Mapping
import uuid

from .redaction import redact_text
from .scheduler import WorkItem, WorkScheduler
from .supervisor import utc_now


@dataclass(frozen=True)
class DispatchOutcome:
    """The only state changes a registered task handler may request."""

    status: str = "COMPLETE"
    retry_after_s: float = 300.0
    metrics: Mapping[str, int | float | bool] = field(default_factory=dict)
    public_message: str = ""


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
        reserved_kinds: Iterable[str] = (),
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
        self.reserved_kinds = {
            str(kind).strip() for kind in reserved_kinds if str(kind).strip()
        }

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
        public_message: str = "",
    ) -> None:
        """Persist a public-safe lifecycle event for one task attempt."""
        event_metrics: dict[str, int | float | bool] = {"attempt": item.attempts}
        event_metrics.update(dict(metrics or {}))
        event: dict[str, object] = {
            "timestamp": utc_now(),
            "event_id": f"evt-queue-task-{uuid.uuid4().hex[:12]}",
            "objective_id": item.objective_id,
            "job_id": "task-queue",
            "worker_id": "",
            "actor": self.actor,
            "kind": item.kind,
            "task_id": item.task_id,
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
        clean_message = redact_text(public_message, max_len=220).strip()
        if clean_message:
            event["message"] = clean_message
        self.scheduler.store.append_event(event)

    def _apply_outcome(
        self,
        item: WorkItem,
        outcome: DispatchOutcome,
        *,
        current: float,
    ) -> tuple[str, Mapping[str, int | float | bool]]:
        """Apply one handler result and record its public-safe lifecycle event."""
        status = str(outcome.status).upper()
        result_metrics: Mapping[str, int | float | bool] = dict(outcome.metrics)
        public_message = str(outcome.public_message or "")
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
                public_message=public_message,
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
                    metrics=result_metrics,
                    public_message=public_message,
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
                    public_message=public_message,
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
                public_message=public_message,
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
                public_message=public_message,
            )
        return status, result_metrics

    def _apply_error(
        self,
        item: WorkItem,
        error: Exception,
        *,
        current: float,
    ) -> tuple[str, Mapping[str, int | float | bool]]:
        """Convert a handler exception into a bounded retry or escalation."""
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
        return status, {}

    def dispatch(self, *, limit: int = 10, now: float | None = None) -> list[DispatchResult]:
        """Dispatch due work and return a compact result for each claimed item."""
        current = time.time() if now is None else float(now)
        results: list[DispatchResult] = []
        for item in self.scheduler.claim(
            limit=limit,
            now=current,
            exclude_kinds=tuple(self.reserved_kinds),
        ):
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

            try:
                status, result_metrics = self._apply_outcome(
                    item,
                    handler(item),
                    current=current,
                )
            except Exception as error:
                status, result_metrics = self._apply_error(
                    item,
                    error,
                    current=current,
                )
            results.append(
                DispatchResult(item.task_id, item.kind, status, item.attempts, result_metrics)
            )
        return results

    def close(self) -> None:
        """Close a synchronous dispatcher; retained as a common lifecycle hook."""


class BackgroundWorkDispatcher(WorkDispatcher):
    """Dispatch bounded handlers off the supervision loop.

    A long-running research or build handler must not delay the protected
    worker's health sampling. Claimed tasks remain RUNNING in SQLite while the
    handler executes. A manager shutdown requeues unfinished work so a restart
    can resume it without creating a duplicate task.
    """

    def __init__(
        self,
        scheduler: WorkScheduler,
        handlers: Mapping[str, DispatchHandler] | None = None,
        *,
        defer_delay_s: float = 300.0,
        max_attempts: int = 3,
        actor: str = "queue-dispatcher",
        max_workers: int = 1,
        max_in_flight: int | None = None,
        reserved_kinds: Iterable[str] = (),
    ) -> None:
        super().__init__(
            scheduler,
            handlers,
            defer_delay_s=defer_delay_s,
            max_attempts=max_attempts,
            actor=actor,
            reserved_kinds=reserved_kinds,
        )
        self.max_workers = int(max_workers)
        if self.max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.max_in_flight = self.max_workers if max_in_flight is None else int(max_in_flight)
        if self.max_in_flight < 1 or self.max_in_flight > self.max_workers:
            raise ValueError("max_in_flight must be between 1 and max_workers")
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="machine-manager-queue",
        )
        self._pending: dict[str, tuple[WorkItem, Future[DispatchOutcome]]] = {}
        self._closed = False

    def _collect_completed(self) -> list[DispatchResult]:
        results: list[DispatchResult] = []
        for task_id, (item, future) in list(self._pending.items()):
            if not future.done():
                continue
            del self._pending[task_id]
            try:
                status, metrics = self._apply_outcome(
                    item,
                    future.result(),
                    current=time.time(),
                )
            except Exception as error:
                status, metrics = self._apply_error(
                    item,
                    error,
                    current=time.time(),
                )
            results.append(DispatchResult(item.task_id, item.kind, status, item.attempts, metrics))
        return results

    def dispatch(self, *, limit: int = 10, now: float | None = None) -> list[DispatchResult]:
        """Collect finished handlers, then claim only available background slots."""
        if self._closed:
            return []
        current = time.time() if now is None else float(now)
        results = self._collect_completed()
        available = self.max_in_flight - len(self._pending)
        if available <= 0:
            return results
        for item in self.scheduler.claim(
            limit=min(int(limit), available),
            now=current,
            exclude_kinds=tuple(self.reserved_kinds),
        ):
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
                results.append(DispatchResult(item.task_id, item.kind, "DEFERRED", item.attempts))
                continue
            self._pending[item.task_id] = (item, self._executor.submit(handler, item))
            results.append(DispatchResult(item.task_id, item.kind, "RUNNING", item.attempts))
        return results

    def close(self) -> None:
        """Requeue unfinished handlers and stop accepting new background work."""
        if self._closed:
            return
        self._collect_completed()
        self._closed = True
        retry_at = time.time()
        for task_id, (item, future) in list(self._pending.items()):
            try:
                self.scheduler.defer(task_id, retry_at=retry_at)
                self._record(
                    item,
                    event_type="queue_task_deferred",
                    new_state="QUEUED",
                    action="requeue_background_task",
                    outcome="dispatcher_closed",
                )
            except Exception:
                pass
            future.cancel()
        self._pending.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
