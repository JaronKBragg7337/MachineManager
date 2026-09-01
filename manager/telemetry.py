"""Atomic, allowlisted publisher for the public dashboard data files."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping


class TelemetryWriteError(OSError):
    """A public telemetry file could not be safely replaced after retries."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_transient_replace_error(error: OSError) -> bool:
    return isinstance(error, PermissionError) or getattr(error, "winerror", None) in {5, 32}


def atomic_json_write(
    destination: Path,
    value: Any,
    *,
    replace_attempts: int = 5,
    retry_delay_s: float = 0.1,
) -> None:
    """Write JSON without exposing a partial file during replacement.

    Windows can briefly deny a replacement while an indexer, synchronizer, or
    another local reader has the destination open. Retry only those transient
    replacement errors; persistent output failure remains a bounded, typed
    error for the caller to handle without stopping protected work.
    """

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    attempts = max(1, int(replace_attempts))
    delay = max(0.0, float(retry_delay_s))
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
        for attempt in range(attempts):
            try:
                os.replace(temporary_name, destination)
                return
            except OSError as error:
                if not _is_transient_replace_error(error):
                    raise
                if attempt + 1 >= attempts:
                    raise TelemetryWriteError(
                        f"could not replace {destination.name} after {attempts} attempts"
                    ) from error
                if delay:
                    time.sleep(delay * (attempt + 1))
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


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
    "cpu_pct",
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
    "duration_s",
    "heartbeat_age_s",
    "sample_count",
    "worker_uptime_s",
    "progress_reported",
    "keys_tested",
    "keys_per_second",
    "hashrate_mkey_s",
    "coverage_pct",
    "progress_pct",
    "work_units_completed",
    "work_units_total",
    "units_per_second",
    "batch_number",
    "segment_index",
    "segment_total",
    "worker_tick",
    "matches_found",
}
PUBLIC_QUEUE_KEYS = {
    "QUEUED",
    "RUNNING",
    "COMPLETE",
    "FAILED",
    "ESCALATED",
    "CANCELLED",
}
PUBLIC_AUTONOMY_BOOL_KEYS = {
    "account_enrollment",
    "public_submissions",
    "transparent_outreach",
    "outreach_opt_out",
    "paid_work",
    "procurement_when_funded",
    "developer_tools",
    "gpu_idle_use",
    "terms_aware_execution",
}
PUBLIC_AUTONOMY_TEXT_KEYS = {"mode", "handoff_style"}
PUBLIC_PROFILE_STATES = {"READY", "RETEST_REQUIRED", "UNKNOWN"}
PUBLIC_OBSERVATION_STATES = {
    "TESTED_PASS",
    "TESTED_FAIL",
    "OBSERVED",
    "UNKNOWN",
    "UNAVAILABLE",
}
PUBLIC_AUDIT_STATES = {"NEEDS_EVIDENCE_REVIEW", "NO_CANDIDATES", "RUNNING", "FAILED", "UNKNOWN"}
PUBLIC_AUDIT_CATEGORIES = {"approval_gate", "autonomy_limit", "scope_boundary", "data_boundary"}
PUBLIC_WORKSTREAM_STATES = {
    "QUEUED",
    "RUNNING",
    "WAITING",
    "REVIEW",
    "COMPLETE",
    "FAILED",
    "ESCALATED",
    "DEGRADED",
    "UNKNOWN",
}
PUBLIC_WORKSTREAM_SOURCES = {"static", "agent", "constraint_audit", "worker_profile"}
PUBLIC_WORKSTREAM_METRICS = {"candidate_count", "files_scanned", "more_pending", "tasks_completed", "capability_count"}


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


PUBLIC_PROGRESS_NUMBER_KEYS = {
    "sample_count",
    "uptime_s",
    "keys_tested",
    "keys_per_second",
    "hashrate_mkey_s",
    "coverage_pct",
    "progress_pct",
    "work_units_completed",
    "work_units_total",
    "units_per_second",
    "batch_number",
    "segment_index",
    "segment_total",
    "worker_tick",
    "matches_found",
}


def _safe_progress(progress: Mapping[str, Any] | None) -> dict[str, Any]:
    """Allowlist aggregate work evidence without exposing worker internals."""
    if not isinstance(progress, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for key in ("kind", "source", "observed_at"):
        if key in progress:
            safe[key] = _text(progress.get(key), max_len=80)
    for key in ("reported", "active", "healthy"):
        if isinstance(progress.get(key), bool):
            safe[key] = progress[key]
    for key in PUBLIC_PROGRESS_NUMBER_KEYS:
        number = _number(progress.get(key))
        if number is not None:
            safe[key] = number
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


def _safe_autonomy(value: Mapping[str, Any] | None) -> dict[str, str | bool]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, str | bool] = {}
    for key in PUBLIC_AUTONOMY_BOOL_KEYS:
        if isinstance(value.get(key), bool):
            safe[key] = value[key]
    for key in PUBLIC_AUTONOMY_TEXT_KEYS:
        if key in value:
            safe[key] = _text(value[key], max_len=80)
    return safe


class TelemetryPublisher:
    """Write only the dashboard's compact, public-safe JSON contract."""

    def __init__(
        self,
        dashboard_dir: Path,
        *,
        replace_attempts: int = 5,
        retry_delay_s: float = 0.1,
    ) -> None:
        if int(replace_attempts) < 1:
            raise ValueError("replace_attempts must be positive")
        if float(retry_delay_s) < 0:
            raise ValueError("retry_delay_s cannot be negative")
        self.dashboard_dir = Path(dashboard_dir)
        self.data_dir = self.dashboard_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.replace_attempts = int(replace_attempts)
        self.retry_delay_s = float(retry_delay_s)

    def _atomic_json(self, name: str, value: Any) -> None:
        atomic_json_write(
            self.data_dir / name,
            value,
            replace_attempts=self.replace_attempts,
            retry_delay_s=self.retry_delay_s,
        )

    def _workers(self, workers: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": _text(worker.get("id", worker.get("worker_id"))),
                "type": _text(worker.get("type", worker.get("worker_type"))),
                "state": _text(worker.get("state"), default="UNKNOWN").upper(),
                "owner": _text(worker.get("owner"), default="local-manager"),
                "progress": _safe_progress(worker.get("progress")),
            }
            for worker in workers
        ]

    def _jobs(self, jobs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": _text(job.get("id", "")),
                "objective_id": _text(job.get("objective_id", "")),
                "state": _text(job.get("state"), default="UNKNOWN").upper(),
                "progress": _safe_progress(job.get("progress")),
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
                "last_reason": _text(agent.get("last_reason"), max_len=160),
                "last_duration_s": _number(agent.get("last_duration_s")),
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

    def _worker_profiles(self, profiles: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        public: list[dict[str, Any]] = []
        for profile in profiles:
            if len(public) >= 30:
                break
            raw_capabilities = profile.get("capabilities", [])
            capabilities: list[dict[str, Any]] = []
            if isinstance(raw_capabilities, Iterable) and not isinstance(raw_capabilities, (str, bytes, Mapping)):
                for item in raw_capabilities:
                    if not isinstance(item, Mapping) or len(capabilities) >= 30:
                        continue
                    status = _text(item.get("status"), default="UNKNOWN", max_len=30).upper()
                    if status not in PUBLIC_OBSERVATION_STATES:
                        status = "UNKNOWN"
                    capabilities.append(
                        {
                            "id": _text(item.get("id"), max_len=100),
                            "status": status,
                            "summary": _text(item.get("summary"), max_len=180),
                            "observed_at": _text(item.get("observed_at"), max_len=40),
                        }
                    )
            state = _text(profile.get("state"), default="UNKNOWN", max_len=40).upper()
            if state not in PUBLIC_PROFILE_STATES:
                state = "UNKNOWN"
            public.append(
                {
                    "id": _text(profile.get("id"), max_len=100),
                    "provider": _text(profile.get("provider"), max_len=60),
                    "model": _text(profile.get("model"), max_len=100),
                    "model_version": _text(profile.get("model_version"), max_len=100),
                    "state": state,
                    "retest_required": bool(profile.get("retest_required", False)),
                    "last_verified": _text(profile.get("last_verified"), max_len=40),
                    "capabilities": capabilities,
                }
            )
        return public

    def _constraint_audits(self, audits: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        public: list[dict[str, Any]] = []
        for audit in audits:
            if len(public) >= 30:
                break
            raw_categories = audit.get("categories", {})
            categories: dict[str, int] = {}
            if isinstance(raw_categories, Mapping):
                for key, value in raw_categories.items():
                    clean_key = _text(key, max_len=40)
                    number = _number(value)
                    if clean_key in PUBLIC_AUDIT_CATEGORIES and number is not None:
                        categories[clean_key] = max(0, int(number))
            state = _text(audit.get("state"), default="UNKNOWN", max_len=40).upper()
            if state not in PUBLIC_AUDIT_STATES:
                state = "UNKNOWN"
            public.append(
                {
                    "id": _text(audit.get("id"), max_len=100),
                    "label": _text(audit.get("label"), max_len=100),
                    "state": state,
                    "scanned_at": _text(audit.get("scanned_at"), max_len=40),
                    "files_scanned": max(0, int(_number(audit.get("files_scanned")) or 0)),
                    "files_skipped": max(0, int(_number(audit.get("files_skipped")) or 0)),
                    "candidate_count": max(0, int(_number(audit.get("candidate_count")) or 0)),
                    "truncated": bool(audit.get("truncated", False)),
                    "more_pending": bool(audit.get("more_pending", False)),
                    "categories": categories,
                }
            )
        return public

    def _workstreams(self, streams: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        public: list[dict[str, Any]] = []
        for stream in streams:
            if len(public) >= 30 or not isinstance(stream, Mapping):
                continue
            state = _text(stream.get("state"), default="UNKNOWN", max_len=30).upper()
            if state not in PUBLIC_WORKSTREAM_STATES:
                state = "UNKNOWN"
            source_kind = _text(stream.get("source_kind"), default="static", max_len=40).lower()
            if source_kind not in PUBLIC_WORKSTREAM_SOURCES:
                source_kind = "static"
            metrics: dict[str, int | bool] = {}
            raw_metrics = stream.get("metrics", {})
            if isinstance(raw_metrics, Mapping):
                for key, value in raw_metrics.items():
                    if key not in PUBLIC_WORKSTREAM_METRICS:
                        continue
                    if isinstance(value, bool):
                        metrics[key] = value
                    else:
                        number = _number(value)
                        if number is not None:
                            metrics[key] = max(0, int(number))
            public.append(
                {
                    "id": _text(stream.get("id"), max_len=100),
                    "objective_id": _text(stream.get("objective_id"), max_len=100),
                    "title": _text(stream.get("title"), max_len=120),
                    "lane": _text(stream.get("lane"), max_len=80),
                    "owner": _text(stream.get("owner"), max_len=80),
                    "state": state,
                    "summary": _text(stream.get("summary"), max_len=220),
                    "next_action": _text(stream.get("next_action"), max_len=180),
                    "source_kind": source_kind,
                    "metrics": metrics,
                    "updated": _text(stream.get("updated"), max_len=40),
                }
            )
        return public

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
                    "action": action,
                    "outcome": outcome,
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

        system_input = snapshot.get("system") if isinstance(snapshot.get("system"), Mapping) else {}
        system: dict[str, int | float] = {}
        cpu_pct = _number(system_input.get("cpu_pct"))
        if cpu_pct is not None:
            system["cpu_pct"] = max(0.0, min(100.0, cpu_pct))

        safe_workers = self._workers(snapshot.get("workers", []))
        safe_jobs = self._jobs(snapshot.get("jobs", []))
        latest = {
            "manager_version": _text(snapshot.get("manager_version"), default="0.2", max_len=30),
            "status": _text(snapshot.get("status"), default="UNKNOWN", max_len=30).upper(),
            "objective": _text(snapshot.get("objective"), max_len=120),
            "objective_id": _text(snapshot.get("objective_id"), max_len=80),
            "workers": safe_workers,
            "jobs": safe_jobs,
            "agents": self._agents(snapshot.get("agents", [])),
            "capabilities": self._capabilities(snapshot.get("capabilities", [])),
            "worker_profiles": self._worker_profiles(snapshot.get("worker_profiles", [])),
            "constraint_audits": self._constraint_audits(snapshot.get("constraint_audits", [])),
            "workstreams": self._workstreams(snapshot.get("workstreams", [])),
            "autonomy": _safe_autonomy(snapshot.get("autonomy")),
            "queue": _safe_queue(snapshot.get("queue")),
            "gpu": gpu,
            "system": system,
            "progress": safe_workers[0].get("progress", {}) if safe_workers else {},
            "notes": "Sanitized public telemetry only. No secrets or raw logs.",
            "updated": _text(snapshot.get("updated"), default=utc_now(), max_len=40),
        }
        self._atomic_json("latest.json", latest)
        self._atomic_json("events.json", self._events(events))
        self._atomic_json("scenarios.json", {"updated": latest["updated"], "scenarios": self._scenarios(scenarios)})
