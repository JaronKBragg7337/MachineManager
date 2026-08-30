"""Capability registry exposed by the general-purpose manager runtime."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    capability_id: str
    description: str
    enabled: bool = True


class CapabilityRegistry:
    """Keep the manager's available powers explicit and inspectable."""

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, capability_id: str, description: str, *, enabled: bool = True) -> None:
        capability_id = str(capability_id).strip()
        if not capability_id:
            raise ValueError("capability_id cannot be empty")
        if capability_id in self._capabilities:
            raise ValueError(f"capability already registered: {capability_id}")
        self._capabilities[capability_id] = Capability(
            capability_id=capability_id,
            description=str(description).strip(),
            enabled=bool(enabled),
        )

    def set_enabled(self, capability_id: str, enabled: bool) -> None:
        current = self._capabilities[capability_id]
        self._capabilities[capability_id] = Capability(
            capability_id=current.capability_id,
            description=current.description,
            enabled=bool(enabled),
        )

    def snapshot(self) -> list[dict[str, str | bool]]:
        return [
            {
                "id": item.capability_id,
                "description": item.description,
                "enabled": item.enabled,
            }
            for item in sorted(self._capabilities.values(), key=lambda item: item.capability_id)
        ]

    @classmethod
    def default(
        cls,
        *,
        github_upload_enabled: bool = False,
        agents_enabled: bool = False,
        execute_and_report_enabled: bool = False,
        transparent_outreach_enabled: bool = False,
        developer_tools_enabled: bool = False,
        gpu_idle_use_enabled: bool = False,
        evidence_ledger_enabled: bool = False,
        constraint_audit_enabled: bool = False,
    ) -> "CapabilityRegistry":
        registry = cls()
        registry.register("process-supervision", "Start, observe, recover, and stop bounded worker processes.")
        registry.register("gpu-monitoring", "Read external GPU activity signals for GPU-bound workers.")
        registry.register("persistent-state", "Store local job state, event history, retry counts, and queued work.")
        registry.register("synthetic-workers", "Run deterministic workers for safe reliability evaluation.")
        registry.register("local-ollama-agents", "Ask configured local models for bounded recommendations.", enabled=agents_enabled)
        registry.register("public-telemetry", "Publish an allowlisted, sanitized dashboard snapshot.")
        registry.register("github-pages-upload", "Upload sanitized dashboard files when a dedicated token is configured.", enabled=github_upload_enabled)
        registry.register("execute-and-report-charter", "Pursue authorized work through research, build, verification, publication, and follow-up.", enabled=execute_and_report_enabled)
        registry.register("transparent-outreach", "Prepare AI-disclosed first contact and permanently suppress opted-out recipients.", enabled=transparent_outreach_enabled)
        registry.register("developer-tooling", "Install and use normal developer tooling when a capable worker needs it.", enabled=developer_tools_enabled)
        registry.register("idle-gpu-work", "Use available GPU capacity for declared work when the protected worker is idle.", enabled=gpu_idle_use_enabled)
        registry.register("capability-evidence-ledger", "Record tested and observed worker capabilities by model/runtime version.", enabled=evidence_ledger_enabled)
        registry.register("constraint-audit", "Find candidate approval and policy language for evidence-based review without changing source automatically.", enabled=constraint_audit_enabled)
        return registry
