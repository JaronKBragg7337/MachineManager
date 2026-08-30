"""Truthful, public-safe visibility for bounded Machine Manager work streams.

Work streams are not a second process supervisor.  They describe real, bounded
work already being performed by a configured worker, evidence audit, or an
explicitly registered static mission.  The registry keeps their public summary
separate from local-only implementation details and never fabricates progress.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .state_store import StateStore
from .supervisor import utc_now


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:[A-Za-z]:[\\/]|/Users/|/home/|\\\\|token\s*[:=]|secret\s*[:=]|password\s*[:=])"
)

WORKSTREAM_STATES = {
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
SOURCE_KINDS = {"static", "agent", "constraint_audit", "worker_profile"}
PUBLIC_METRIC_KEYS = {"candidate_count", "files_scanned", "more_pending", "tasks_completed", "capability_count"}


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError(f"{field} must be a compact identifier")
    return text


def _safe_text(value: Any, *, default: str = "", max_len: int = 180) -> str:
    text = default if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()[:max_len]
    return "[redacted]" if _SENSITIVE_TEXT.search(text) else text


def _state(value: Any, *, default: str = "UNKNOWN") -> str:
    candidate = str(value or default).strip().upper()
    return candidate if candidate in WORKSTREAM_STATES else default


def _safe_metrics(value: Mapping[str, Any] | None) -> dict[str, int | bool]:
    safe: dict[str, int | bool] = {}
    for key, item in (value or {}).items():
        if key not in PUBLIC_METRIC_KEYS:
            continue
        if isinstance(item, bool):
            safe[key] = item
        elif isinstance(item, (int, float)):
            safe[key] = max(0, int(item))
    return safe


@dataclass(frozen=True)
class WorkstreamSpec:
    """A configured, displayable work stream with an optional live source."""

    stream_id: str
    objective_id: str
    title: str
    lane: str
    owner: str
    summary: str
    next_action: str
    state: str
    source_kind: str = "static"
    source_id: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkstreamSpec":
        stream_id = _identifier(value.get("id", value.get("stream_id")), "workstream.id")
        source = value.get("source", {})
        if source is None:
            source = {}
        if not isinstance(source, Mapping):
            raise ValueError(f"workstream {stream_id}.source must be an object")
        source_kind = _safe_text(source.get("kind"), default="static", max_len=40).lower()
        if source_kind not in SOURCE_KINDS:
            raise ValueError(f"workstream {stream_id} has an unsupported source kind")
        source_id = ""
        if source_kind != "static":
            source_id = _identifier(source.get("id"), f"workstream {stream_id}.source.id")
        return cls(
            stream_id=stream_id,
            objective_id=_identifier(value.get("objective_id", stream_id), f"workstream {stream_id}.objective_id"),
            title=_safe_text(value.get("title"), default=stream_id, max_len=120) or stream_id,
            lane=_safe_text(value.get("lane"), default="General", max_len=80) or "General",
            owner=_safe_text(value.get("owner"), default="local-manager", max_len=80) or "local-manager",
            summary=_safe_text(value.get("summary"), max_len=220),
            next_action=_safe_text(value.get("next_action"), max_len=180),
            state=_state(value.get("state"), default="QUEUED"),
            source_kind=source_kind,
            source_id=source_id,
        )


class WorkstreamRegistry:
    """Synchronize configured work streams with safe, observed runtime signals."""

    def __init__(self, store: StateStore, raw_specs: Iterable[Mapping[str, Any]] = ()) -> None:
        self.store = store
        self.specs = tuple(WorkstreamSpec.from_mapping(item) for item in raw_specs if isinstance(item, Mapping))
        stream_ids = [spec.stream_id for spec in self.specs]
        if len(stream_ids) != len(set(stream_ids)):
            raise ValueError("workstreams contains duplicate ids")

    @staticmethod
    def _indexed(items: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
        return {
            str(item.get("id", "")): item
            for item in items
            if isinstance(item, Mapping) and str(item.get("id", ""))
        }

    @staticmethod
    def _agent_state(value: Any) -> str:
        source_state = str(value or "").upper()
        if source_state == "WORKING":
            return "RUNNING"
        if source_state in {"READY", "WAITING"}:
            return "WAITING"
        if source_state == "DEGRADED":
            return "DEGRADED"
        return "UNKNOWN"

    @staticmethod
    def _audit_state(value: Any) -> str:
        source_state = str(value or "").upper()
        return {
            "RUNNING": "RUNNING",
            "NEEDS_EVIDENCE_REVIEW": "REVIEW",
            "NO_CANDIDATES": "WAITING",
            "FAILED": "DEGRADED",
        }.get(source_state, "UNKNOWN")

    @staticmethod
    def _profile_state(value: Any) -> str:
        source_state = str(value or "").upper()
        if source_state == "RETEST_REQUIRED":
            return "REVIEW"
        if source_state == "READY":
            return "WAITING"
        return "UNKNOWN"

    def _resolve(
        self,
        spec: WorkstreamSpec,
        *,
        agents: Mapping[str, Mapping[str, Any]],
        audits: Mapping[str, Mapping[str, Any]],
        profiles: Mapping[str, Mapping[str, Any]],
    ) -> tuple[str, dict[str, int | bool]]:
        if spec.source_kind == "static":
            return spec.state, {}
        if spec.source_kind == "agent":
            agent = agents.get(spec.source_id)
            if agent is None:
                return "UNKNOWN", {}
            return self._agent_state(agent.get("state")), _safe_metrics(
                {"tasks_completed": agent.get("tasks_completed", 0)}
            )
        if spec.source_kind == "constraint_audit":
            audit = audits.get(spec.source_id)
            if audit is None:
                return "WAITING", {}
            return self._audit_state(audit.get("state")), _safe_metrics(
                {
                    "candidate_count": audit.get("candidate_count", 0),
                    "files_scanned": audit.get("files_scanned", 0),
                    "more_pending": audit.get("more_pending", False),
                }
            )
        profile = profiles.get(spec.source_id)
        if profile is None:
            return "UNKNOWN", {}
        capability_count = profile.get("capabilities", [])
        return self._profile_state(profile.get("state")), _safe_metrics(
            {"capability_count": len(capability_count) if isinstance(capability_count, list) else 0}
        )

    @staticmethod
    def _fingerprint(record: Mapping[str, Any]) -> str:
        stable = {key: value for key, value in record.items() if key not in {"updated", "fingerprint"}}
        return json.dumps(stable, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def _event(self, spec: WorkstreamSpec, *, event_type: str, state: str, outcome: str, metrics: Mapping[str, int | bool]) -> dict[str, Any]:
        return {
            "timestamp": utc_now(),
            "event_id": f"evt-workstream-{spec.stream_id}-{time.time_ns()}",
            "objective_id": spec.objective_id,
            "job_id": f"workstream-{spec.stream_id}",
            "worker_id": spec.owner,
            "actor": spec.owner,
            "event_type": event_type,
            "previous_state": "UNKNOWN",
            "new_state": state,
            "metrics": dict(metrics),
            "action": "synchronize_workstream",
            "outcome": outcome,
            "artifact_refs": [f"workstream:{spec.stream_id}"],
            "error": None,
            "duration": None,
        }

    def sync(
        self,
        *,
        agents: Iterable[Mapping[str, Any]] = (),
        audits: Iterable[Mapping[str, Any]] = (),
        profiles: Iterable[Mapping[str, Any]] = (),
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Record source-derived stream state and return changed events only."""
        indexed_agents = self._indexed(agents)
        indexed_audits = self._indexed(audits)
        indexed_profiles = self._indexed(profiles)
        records: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        for spec in self.specs:
            state, metrics = self._resolve(
                spec,
                agents=indexed_agents,
                audits=indexed_audits,
                profiles=indexed_profiles,
            )
            proposed = {
                "id": spec.stream_id,
                "objective_id": spec.objective_id,
                "title": spec.title,
                "lane": spec.lane,
                "owner": spec.owner,
                "state": state,
                "summary": spec.summary,
                "next_action": spec.next_action,
                "source_kind": spec.source_kind,
                "source_id": spec.source_id,
                "metrics": metrics,
            }
            fingerprint = self._fingerprint(proposed)
            prior = self.store.get_workstream(spec.stream_id)
            if prior and str(prior.get("fingerprint", "")) == fingerprint:
                records.append(prior)
                continue
            proposed["fingerprint"] = fingerprint
            proposed["updated"] = utc_now()
            self.store.upsert_workstream(proposed)
            records.append(proposed)
            if prior is None:
                events.append(
                    self._event(
                        spec,
                        event_type="workstream_registered",
                        state=state,
                        outcome="registered",
                        metrics=metrics,
                    )
                )
            elif str(prior.get("state", "UNKNOWN")) != state:
                events.append(
                    self._event(
                        spec,
                        event_type="workstream_state_changed",
                        state=state,
                        outcome="state_synchronized",
                        metrics=metrics,
                    )
                )
        return records, events

    def public_snapshot(self) -> list[dict[str, Any]]:
        """Return only configured, safe records in configured display order."""
        stored = {str(item.get("id", "")): item for item in self.store.list_workstreams()}
        return [stored[spec.stream_id] for spec in self.specs if spec.stream_id in stored]
