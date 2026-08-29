"""Bounded local-agent coordination for Machine Manager.

Agents are advisory specialists. They may recommend one of a small set of
actions, but they never receive arbitrary shell access and they do not get to
override the supervisor's retry or safety limits.
"""

from __future__ import annotations

import datetime as dt
import concurrent.futures
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .supervisor import utc_now


ALLOWED_ACTIONS = {
    "continue",
    "restart",
    "escalate",
    "queue_follow_up",
    "pause",
}


def _safe_text(value: Any, *, default: str = "", max_len: int = 160) -> str:
    value = default if value is None else str(value)
    value = value.replace("\r", " ").replace("\n", " ").strip()[:max_len]
    if re.search(r"(?i)(?:[A-Za-z]:[\\/]|/Users/|/home/|\\\\|token\s*[:=]|secret\s*[:=]|password\s*[:=])", value):
        return "[redacted]"
    return value


def _safe_loopback_url(value: Any) -> str:
    """Allow only the local Ollama endpoints used by the manager."""
    candidate = str(value or "").strip().rstrip("/")
    if candidate in {"http://127.0.0.1:11434", "http://localhost:11434"}:
        return candidate
    return "http://127.0.0.1:11434"


@dataclass(frozen=True)
class AgentDecision:
    action: str
    reason: str
    fallback: bool = False


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    role: str
    provider: str = "disabled"
    model: str = ""
    base_url: str = "http://127.0.0.1:11434"
    enabled: bool = False
    interval_s: float = 300.0
    timeout_s: float = 10.0
    keep_gpu_free: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AgentSpec":
        agent_id = str(value.get("id", value.get("agent_id", ""))).strip()
        if not agent_id:
            raise ValueError("agent.id is required")
        interval_s = float(value.get("interval_s", 300))
        timeout_s = float(value.get("timeout_s", 10))
        if interval_s <= 0 or timeout_s <= 0:
            raise ValueError("agent intervals and timeouts must be positive")
        return cls(
            agent_id=agent_id,
            role=_safe_text(value.get("role"), default="specialist", max_len=80),
            provider=_safe_text(value.get("provider"), default="disabled", max_len=40).lower(),
            model=_safe_text(value.get("model"), max_len=80),
            base_url=_safe_loopback_url(value.get("base_url")),
            enabled=bool(value.get("enabled", False)),
            interval_s=interval_s,
            timeout_s=timeout_s,
            keep_gpu_free=bool(value.get("keep_gpu_free", True)),
        )


def parse_agent_response(text: Any) -> AgentDecision:
    """Parse a strict JSON response and safely fall back on malformed output."""
    if not isinstance(text, str) or not text.strip():
        return AgentDecision("continue", "empty agent response; continue under manager control", True)
    cleaned = text.strip()
    fence = chr(96) * 3
    if cleaned.startswith(fence) and cleaned.endswith(fence):
        cleaned = cleaned[3:-3].strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        payload = json.loads(cleaned)
    except (TypeError, json.JSONDecodeError):
        return AgentDecision("continue", "malformed agent response; continue under manager control", True)
    if not isinstance(payload, dict):
        return AgentDecision("continue", "non-object agent response; continue under manager control", True)
    action = str(payload.get("action", "")).strip().lower()
    if action not in ALLOWED_ACTIONS:
        return AgentDecision("continue", "unsupported agent action; continue under manager control", True)
    reason = _safe_text(payload.get("reason"), default="no reason supplied", max_len=160)
    return AgentDecision(action, reason, False)


class LocalOllamaAgent:
    """Call Ollama's local generate endpoint with GPU use disabled by default."""

    def __init__(self, spec: AgentSpec) -> None:
        self.spec = spec

    def _prompt(self, snapshot: Mapping[str, Any], events: Iterable[Mapping[str, Any]]) -> str:
        safe_snapshot = {
            "status": _safe_text(snapshot.get("status"), max_len=30),
            "objective": _safe_text(snapshot.get("objective"), max_len=120),
            "workers": [
                {
                    "id": _safe_text(worker.get("id"), max_len=60),
                    "type": _safe_text(worker.get("type"), max_len=60),
                    "state": _safe_text(worker.get("state"), max_len=30),
                }
                for worker in snapshot.get("workers", [])
                if isinstance(worker, Mapping)
            ],
            "jobs": [
                {
                    "id": _safe_text(job.get("id"), max_len=60),
                    "state": _safe_text(job.get("state"), max_len=30),
                }
                for job in snapshot.get("jobs", [])
                if isinstance(job, Mapping)
            ],
            "gpu": dict(snapshot.get("gpu", {})) if isinstance(snapshot.get("gpu"), Mapping) else {},
            "recent_events": [
                {
                    "type": _safe_text(event.get("event_type", event.get("type")), max_len=60),
                    "state": _safe_text(event.get("new_state", event.get("state")), max_len=30),
                    "outcome": _safe_text(event.get("outcome"), max_len=80),
                }
                for event in list(events)[-8:]
                if isinstance(event, Mapping)
            ],
        }
        return (
            "You are a bounded Machine Manager specialist. Review the sanitized "
            "machine snapshot below. Respond with JSON only, exactly in the form "
            '{"action":"continue|restart|escalate|queue_follow_up|pause","reason":"short reason"}. '
            "Do not request credentials, shell commands, private data, or arbitrary file changes. "
            f"Snapshot: {json.dumps(safe_snapshot, ensure_ascii=True, separators=(',', ':'))}"
        )

    def ask(self, snapshot: Mapping[str, Any], events: Iterable[Mapping[str, Any]]) -> AgentDecision:
        body: dict[str, Any] = {
            "model": self.spec.model,
            "prompt": self._prompt(snapshot, events),
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"temperature": 0.1, "num_predict": 80},
        }
        if self.spec.keep_gpu_free:
            body["options"]["num_gpu"] = 0
        request = urllib.request.Request(
            self.spec.base_url.rstrip("/") + "/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.spec.timeout_s) as response:
            payload = json.loads(response.read(512_000).decode("utf-8"))
        return parse_agent_response(payload.get("response", "") if isinstance(payload, dict) else "")


class AgentCoordinator:
    """Run configured agents without letting a slow model block supervision."""

    def __init__(self, raw_specs: Iterable[Mapping[str, Any]] = ()) -> None:
        self.specs = [AgentSpec.from_mapping(item) for item in raw_specs if isinstance(item, Mapping)]
        self._last_run: dict[str, float] = {}
        self._statuses: dict[str, dict[str, Any]] = {
            spec.agent_id: {
                "id": spec.agent_id,
                "role": spec.role,
                "provider": spec.provider,
                "model": spec.model,
                "state": "DISABLED" if not spec.enabled else "READY",
                "enabled": spec.enabled,
                "last_action": "",
                "last_reason": "",
                "last_duration_s": None,
                "tasks_completed": 0,
                "last_run": None,
                "next_run": None,
            }
            for spec in self.specs
        }
        self.events: list[dict[str, Any]] = []
        self._pending: dict[str, concurrent.futures.Future[AgentDecision]] = {}
        self._pending_snapshots: dict[str, dict[str, Any]] = {}
        self._pending_started: dict[str, float] = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(self.specs)),
            thread_name_prefix="machine-manager-agent",
        )

    @staticmethod
    def _next_run(interval_s: float) -> str:
        return (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=interval_s)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _ask(
        self,
        spec: AgentSpec,
        snapshot: Mapping[str, Any],
        events: Iterable[Mapping[str, Any]],
    ) -> AgentDecision:
        if spec.provider in {"noop", "test"}:
            return AgentDecision("continue", "deterministic agent heartbeat", False)
        if spec.provider == "ollama":
            if not spec.model:
                return AgentDecision("continue", "agent model is not configured", True)
            return LocalOllamaAgent(spec).ask(snapshot, events)
        return AgentDecision("continue", "agent provider is unavailable", True)

    def _record(
        self,
        spec: AgentSpec,
        snapshot: Mapping[str, Any],
        decision: AgentDecision,
        error_name: str | None,
        duration_s: float | None = None,
    ) -> AgentDecision:
        status = self._statuses[spec.agent_id]
        status["state"] = "READY" if error_name is None else "DEGRADED"
        status["last_action"] = decision.action
        status["last_reason"] = decision.reason
        status["last_duration_s"] = duration_s
        status["tasks_completed"] = int(status["tasks_completed"]) + 1
        status["last_run"] = utc_now()
        status["next_run"] = self._next_run(spec.interval_s)
        self.events.append(
            {
                "timestamp": status["last_run"],
                "event_id": f"agent-{spec.agent_id}-{time.time_ns()}",
                "objective_id": str(snapshot.get("objective_id", "agent-coordination")),
                "job_id": "agent-coordinator",
                "worker_id": spec.agent_id,
                "actor": spec.agent_id,
                "event_type": "agent_decision",
                "previous_state": "WORKING",
                "new_state": status["state"],
                "metrics": {
                    "tasks_completed": status["tasks_completed"],
                    "fallback": decision.fallback,
                    "duration_s": duration_s,
                },
                "action": decision.action,
                "outcome": "fallback" if decision.fallback else "recommendation",
                "message": decision.reason,
                "artifact_refs": [],
                "error": error_name,
                "duration": None,
            }
        )
        self.events = self.events[-500:]
        return decision

    def _collect_completed(self, decisions: list[AgentDecision]) -> None:
        for agent_id, future in list(self._pending.items()):
            if not future.done():
                continue
            spec = next(item for item in self.specs if item.agent_id == agent_id)
            snapshot = self._pending_snapshots.get(agent_id, {})
            try:
                decision = future.result()
                error_name = None
            except Exception as exc:
                decision = AgentDecision(
                    "continue",
                    "agent unavailable; continue under manager control",
                    True,
                )
                error_name = type(exc).__name__
            started = self._pending_started.pop(agent_id, None)
            duration_s = None if started is None else round(max(0.0, time.monotonic() - started), 3)
            decisions.append(self._record(spec, snapshot, decision, error_name, duration_s))
            del self._pending[agent_id]
            self._pending_snapshots.pop(agent_id, None)

    def tick(
        self,
        snapshot: Mapping[str, Any],
        events: Iterable[Mapping[str, Any]] = (),
    ) -> list[AgentDecision]:
        now = time.monotonic()
        decisions: list[AgentDecision] = []
        self._collect_completed(decisions)
        event_list = list(events)
        for spec in self.specs:
            status = self._statuses[spec.agent_id]
            if not spec.enabled:
                status["state"] = "DISABLED"
                continue
            if spec.agent_id in self._pending:
                status["state"] = "WORKING"
                continue
            last_run = self._last_run.get(spec.agent_id)
            if last_run is not None and now - last_run < spec.interval_s:
                status["state"] = "WAITING"
                continue

            status["state"] = "WORKING"
            self._last_run[spec.agent_id] = now
            if spec.provider == "ollama":
                future = self._executor.submit(self._ask, spec, dict(snapshot), event_list)
                self._pending[spec.agent_id] = future
                self._pending_snapshots[spec.agent_id] = dict(snapshot)
                self._pending_started[spec.agent_id] = time.monotonic()
                continue
            started = time.monotonic()
            try:
                decision = self._ask(spec, snapshot, event_list)
                error_name = None
            except Exception as exc:
                decision = AgentDecision(
                    "continue",
                    "agent unavailable; continue under manager control",
                    True,
                )
                error_name = type(exc).__name__
            duration_s = round(max(0.0, time.monotonic() - started), 3)
            decisions.append(self._record(spec, snapshot, decision, error_name, duration_s))
        return decisions

    def close(self) -> None:
        for future in self._pending.values():
            future.cancel()
        self._pending.clear()
        self._pending_snapshots.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(self._statuses[spec.agent_id]) for spec in self.specs]
