"""Atomic, allowlisted publisher for the public dashboard data files."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(value: Any, *, default: str = "", max_len: int = 160) -> str:
    value = default if value is None else str(value)
    value = value.replace("\r", " ").replace("\n", " ").strip()[:max_len]
    if re.search(r"(?i)(?:[A-Za-z]:[\\/]|/Users/|/home/|\\\\|token\s*[:=]|secret\s*[:=]|password\s*[:=])", value):
        return "[redacted]"
    return value


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


PUBLIC_METRIC_KEYS = {
    "attempt",
    "restart_count",
    "max_restarts",
    "util_pct",
    "mem_used_mib",
    "mem_total_mib",
    "temp_c",
    "power_w",
    "process_alive",
    "heartbeat_fresh",
    "resource_active",
    "healthy",
    "detection_time_s",
    "recovery_time_s",
    "tasks_completed",
    "fallback",
}
PUBLIC_QUEUE_KEYS = {
    "QUEUED",
    "RUNNING",
    "COMPLETE",
    "FAILED",
    "ESCALATED",
    "CANCELLED",
}


def _safe_metrics(metrics: Mapping[str, Any] | None) -> dict[str, int | float | bool]:
    safe: dict[str, int | float | bool] = {}
    for key, value in (metrics or {}).items():
        clean_key = _text(key, max_len=40)
        if clean_key not in PUBLIC_METRIC_KEYS:
            continue
        if isinstance(value, bool):
            safe[clean_key] = value
        else:
            number = _number(value)
            if number is not None:
                safe[clean_key] = number
    return safe


def _safe_queue(queue: Mapping[str, Any] | None) -> dict[str, int]:
    safe: dict[str, int] = {}
    for key, value in (queue or {}).items():
        clean_key = _text(key, max_len=20).upper()
        if clean_key not in PUBLIC_QUEUE_KEYS:
            continue
        number = _number(value)
        if number is not None:
            safe[clean_key] = max(0, int(number))
    return safe


class TelemetryPublisher:
    """Write only the dashboard's compact, public-safe JSON contract."""

    def __init__(self, dashboard_dir: Path) -> None:
        self.dashboard_dir = Path(dashboard_dir)
        self.data_dir = self.dashboard_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _atomic_json(self, name: str, value: Any) -> None:
        destination = self.data_dir / name
        fd, temporary_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, indent=2, ensure_ascii=True)
                handle.write("\n")
            os.replace(temporary_name, destination)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _workers(self, workers: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": _text(worker.get("id", worker.get("worker_id"))),
                "type": _text(worker.get("type", worker.get("worker_type"))),
                "state": _text(worker.get("state"), default="UNKNOWN").upper(),
                "owner": _text(worker.get("owner"), default="local-manager"),
            }
            for worker in workers
        ]

    def _jobs(self, jobs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": _text(job.get("id", "")),
                "objective_id": _text(job.get("objective_id", "")),
                "state": _text(job.get("state"), default="UNKNOWN").upper(),
            }
            for job in jobs
        ]

    def _agents(self, agents: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": _text(agent.get("id", "")),
                "role": _text(agent.get("role"), default="specialist", max_len=80),
                "provider": _text(agent.get("provider"), default="unknown", max_len=40),
                "model": _text(agent.get("model"), max_len=80),
                "state": _text(agent.get("state"), default="UNKNOWN", max_len=30).upper(),
                "enabled": bool(agent.get("enabled", False)),
                "last_action": _text(agent.get("last_action"), max_len=40),
                "tasks_completed": _number(agent.get("tasks_completed")) or 0,
                "last_run": _text(agent.get("last_run"), max_len=40),
                "next_run": _text(agent.get("next_run"), max_len=40),
            }
            for agent in agents
        ]

    def _capabilities(self, capabilities: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": _text(item.get("id", ""), max_len=60),
                "description": _text(item.get("description"), max_len=140),
                "enabled": bool(item.get("enabled", False)),
            }
            for item in capabilities
        ]

    def _events(self, events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        public: list[dict[str, Any]] = []
        for event in events:
            action = _text(event.get("action"), max_len=80)
            outcome = _text(event.get("outcome"), max_len=100)
            event_type = _text(event.get("event_type", event.get("type")), default="event", max_len=60)
            state = _text(event.get("new_state", event.get("state")), default="UNKNOWN", max_len=30).upper()
            message = _text(event.get("message"), max_len=160)
            if not message:
                message = _text(" · ".join(part for part in (action, outcome) if part), default=event_type)
            public.append(
                {
                    "ts": _text(event.get("timestamp", event.get("ts")), default=utc_now(), max_len=40),
                    "event_id": _text(event.get("event_id"), max_len=40),
                    "actor": _text(event.get("actor"), default="system", max_len=60),
                    "type": event_type,
                    "state": state,
                    "message": message,
                    "metrics": _safe_metrics(event.get("metrics")),
                    "error": bool(event.get("error")),
                }
            )
        return public

    def _scenarios(self, scenarios: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": _text(scenario.get("id", "")),
                "actor": _text(scenario.get("actor"), default="local-manager"),
                "result": _text(scenario.get("result"), default="UNKNOWN").upper(),
                "score": _number(scenario.get("score")),
                "recovery_s": _number(scenario.get("recovery_s")),
            }
            for scenario in scenarios
        ]

    def publish(
        self,
        snapshot: Mapping[str, Any],
        *,
        events: Iterable[Mapping[str, Any]] = (),
        scenarios: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        gpu_input = snapshot.get("gpu") if isinstance(snapshot.get("gpu"), Mapping) else {}
        gpu: dict[str, int | float] = {}
        for source, target in (
            ("util_pct", "util_pct"),
            ("mem_used_mib", "mem_used_mib"),
            ("mem_total_mib", "mem_total_mib"),
            ("temp_c", "temp_c"),
            ("power_w", "power_w"),
        ):
            number = _number(gpu_input.get(source))
            if number is not None:
                gpu[target] = number

        latest = {
            "manager_version": _text(snapshot.get("manager_version"), default="0.2", max_len=30),
            "status": _text(snapshot.get("status"), default="UNKNOWN", max_len=30).upper(),
            "objective": _text(snapshot.get("objective"), max_len=120),
            "objective_id": _text(snapshot.get("objective_id"), max_len=80),
            "workers": self._workers(snapshot.get("workers", [])),
            "jobs": self._jobs(snapshot.get("jobs", [])),
            "agents": self._agents(snapshot.get("agents", [])),
            "capabilities": self._capabilities(snapshot.get("capabilities", [])),
            "queue": _safe_queue(snapshot.get("queue")),
            "gpu": gpu,
            "notes": "Sanitized public telemetry only. No secrets or raw logs.",
            "updated": _text(snapshot.get("updated"), default=utc_now(), max_len=40),
        }
        self._atomic_json("latest.json", latest)
        self._atomic_json("events.json", self._events(events))
        self._atomic_json("scenarios.json", {"updated": latest["updated"], "scenarios": self._scenarios(scenarios)})
