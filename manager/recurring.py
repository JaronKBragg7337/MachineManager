"""Durable, bounded scheduling for recurring Machine Manager objectives."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
import time
from typing import Any, Iterable, Mapping
import uuid

from .scheduler import WorkScheduler
from .supervisor import utc_now


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PRIVATE_TEXT = re.compile(
    r"(?i)(?:[A-Za-z]:[\\/]|/Users/|/home/|\\\\|token\s*[:=]|secret\s*[:=]|password\s*[:=])"
)


def _config_text(value: Any, *, field: str, max_len: int = 120) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if not text or len(text) > max_len or _PRIVATE_TEXT.search(text):
        raise ValueError(f"recurring task {field} is invalid")
    return text


@dataclass(frozen=True)
class RecurringTaskSpec:
    """One recurring task template whose payload remains local-only."""

    recurring_id: str
    kind: str
    objective_id: str
    interval_s: float
    payload: Mapping[str, Any]
    enabled: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RecurringTaskSpec":
        recurring_id = _config_text(value.get("id"), field="id", max_len=64)
        if not _SAFE_ID.fullmatch(recurring_id):
            raise ValueError("recurring task id contains unsupported characters")
        kind = _config_text(value.get("kind"), field="kind", max_len=60)
        objective_id = _config_text(value.get("objective_id"), field="objective_id", max_len=120)
        try:
            interval_s = float(value.get("interval_s", 0))
        except (TypeError, ValueError) as error:
            raise ValueError("recurring task interval_s must be a number") from error
        if not math.isfinite(interval_s) or interval_s <= 0:
            raise ValueError("recurring task interval_s must be positive")
        payload = value.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ValueError("recurring task payload must be an object")
        try:
            json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ValueError("recurring task payload must be JSON serializable") from error
        return cls(
            recurring_id=recurring_id,
            kind=kind,
            objective_id=objective_id,
            interval_s=interval_s,
            payload=dict(payload),
            enabled=bool(value.get("enabled", True)),
        )


class RecurringTaskSeeder:
    """Schedule at most one due task per template and avoid overlapping runs."""

    _ACTIVE = {"QUEUED", "RUNNING"}

    def __init__(
        self,
        scheduler: WorkScheduler,
        specs: Iterable[RecurringTaskSpec] = (),
        *,
        actor: str = "recurring-scheduler",
    ) -> None:
        self.scheduler = scheduler
        self.specs = list(specs)
        ids = [spec.recurring_id for spec in self.specs]
        if len(ids) != len(set(ids)):
            raise ValueError("recurring task ids must be unique")
        self.actor = _config_text(actor, field="actor", max_len=80)

    @classmethod
    def from_config(
        cls,
        scheduler: WorkScheduler,
        raw_specs: Any,
        *,
        actor: str = "recurring-scheduler",
    ) -> "RecurringTaskSeeder":
        if raw_specs is None:
            raw_specs = []
        if not isinstance(raw_specs, list):
            raise ValueError("config.recurring_tasks must be an array")
        if len(raw_specs) > 20:
            raise ValueError("config.recurring_tasks may contain at most 20 entries")
        specs = [
            RecurringTaskSpec.from_mapping(item)
            for item in raw_specs
            if isinstance(item, Mapping)
        ]
        if len(specs) != len(raw_specs):
            raise ValueError("each recurring task must be an object")
        return cls(scheduler, specs, actor=actor)

    @staticmethod
    def _number(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
            return number if math.isfinite(number) else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _sequence(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def _meta_key(self, spec: RecurringTaskSpec) -> str:
        return f"recurring_task:{spec.recurring_id}"

    def _state(self, spec: RecurringTaskSpec) -> dict[str, Any]:
        value = self.scheduler.store.get_meta(self._meta_key(spec), {})
        return dict(value) if isinstance(value, Mapping) else {}

    def tick(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Enqueue due templates and return lifecycle events for the caller."""
        current = time.time() if now is None else float(now)
        events: list[dict[str, Any]] = []
        for spec in self.specs:
            if not spec.enabled:
                continue
            state = self._state(spec)
            next_at = self._number(state.get("next_at"), 0.0)
            if next_at > current:
                continue

            previous_task_id = str(state.get("task_id", "")).strip()
            sequence = self._sequence(state.get("sequence"))
            if previous_task_id and self.scheduler.store.task_status(previous_task_id) in self._ACTIVE:
                self.scheduler.store.set_meta(
                    self._meta_key(spec),
                    {
                        "version": 1,
                        "task_id": previous_task_id,
                        "sequence": sequence,
                        "next_at": current + min(spec.interval_s, 60.0),
                    },
                )
                continue

            sequence += 1
            task_id = f"recurring-{spec.recurring_id}-{sequence:06d}"
            self.scheduler.enqueue(
                kind=spec.kind,
                objective_id=spec.objective_id,
                payload=spec.payload,
                task_id=task_id,
                scheduled_at=current,
            )
            # The queue insert is idempotent. Persisting the cursor after it
            # means a crash between these operations repeats the same task id
            # rather than creating a second copy of the work.
            self.scheduler.store.set_meta(
                self._meta_key(spec),
                {
                    "version": 1,
                    "task_id": task_id,
                    "sequence": sequence,
                    "next_at": current + spec.interval_s,
                },
            )
            events.append(
                {
                    "timestamp": utc_now(),
                    "event_id": f"evt-recurring-task-{uuid.uuid4().hex[:12]}",
                    "objective_id": spec.objective_id,
                    "job_id": "task-queue",
                    "worker_id": "",
                    "actor": self.actor,
                    "kind": spec.kind,
                    "task_id": task_id,
                    "event_type": "recurring_task_scheduled",
                    "previous_state": "IDLE",
                    "new_state": "QUEUED",
                    "metrics": {},
                    "action": "schedule_recurring_task",
                    "outcome": "task_enqueued",
                    "artifact_refs": [f"task:{task_id}"],
                    "error": None,
                    "duration": None,
                }
            )
        return events
