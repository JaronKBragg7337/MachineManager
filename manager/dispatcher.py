"""Explicit, bounded dispatch for durable work items.

The live KeyHunt supervisor does not use this dispatcher. A future research,
build, or delivery worker can register a handler and receive durable work
without making the protected Puzzle #71 assignment mutable by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Mapping

from .scheduler import WorkItem, WorkScheduler


@dataclass(frozen=True)
class DispatchOutcome:
    """The only state changes a registered task handler may request."""

    status: str = "COMPLETE"
    retry_after_s: float = 300.0


@dataclass(frozen=True)
class DispatchResult:
    task_id: str
    kind: str
    status: str
    attempts: int


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
    ) -> None:
        if defer_delay_s <= 0:
            raise ValueError("defer_delay_s must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.scheduler = scheduler
        self.handlers = dict(handlers or {})
        self.defer_delay_s = float(defer_delay_s)
        self.max_attempts = int(max_attempts)

    def dispatch(self, *, limit: int = 10, now: float | None = None) -> list[DispatchResult]:
        """Dispatch due work and return a compact result for each claimed item."""
        current = time.time() if now is None else float(now)
        results: list[DispatchResult] = []
        for item in self.scheduler.claim(limit=limit, now=current):
            handler = self.handlers.get(item.kind)
            if handler is None:
                self.scheduler.defer(item.task_id, retry_at=current + self.defer_delay_s)
                results.append(
                    DispatchResult(item.task_id, item.kind, "DEFERRED", item.attempts)
                )
                continue

            try:
                outcome = handler(item)
                status = str(outcome.status).upper()
                if status not in self._TERMINAL and status != "RETRY":
                    raise ValueError(f"unsupported dispatch outcome: {status}")
                if status == "COMPLETE":
                    self.scheduler.complete(item.task_id)
                elif status == "RETRY":
                    retry_after = max(0.1, float(outcome.retry_after_s))
                    if item.attempts >= self.max_attempts:
                        status = "ESCALATED"
                        self.scheduler.escalate(item.task_id)
                    else:
                        self.scheduler.defer(item.task_id, retry_at=current + retry_after)
                elif status == "FAILED":
                    self.scheduler.fail(item.task_id)
                else:
                    self.scheduler.escalate(item.task_id)
            except Exception:
                if item.attempts >= self.max_attempts:
                    status = "ESCALATED"
                    self.scheduler.escalate(item.task_id)
                else:
                    status = "RETRY"
                    self.scheduler.defer(item.task_id, retry_at=current + self.defer_delay_s)
            results.append(DispatchResult(item.task_id, item.kind, status, item.attempts))
        return results
