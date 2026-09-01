"""Run the general-purpose local Machine Manager runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any, Mapping

from .agents import AgentCoordinator
from .autonomy import OperatingCharter
from .capabilities import CapabilityRegistry
from .dispatcher import WorkDispatcher
from .evidence import EvidenceCoordinator
from .instance_lock import InstanceAlreadyRunning, InstanceLock
from .machine_manager import MachineManager
from .probes import CpuUsageProbe, gpu_resource_ok, keyhunt_progress_probe, nvidia_smi_probe
from .public_upload import GitHubPagesPublisher, PublicUploadError
from .research_worker import OllamaResearchHandler, PublicResearchHandler
from .scheduler import WorkScheduler
from .state_store import StateStore
from .supervisor import WorkerSpec
from .telemetry import TelemetryPublisher, atomic_json_write, utc_now
from .workstreams import WorkstreamRegistry


def load_json_document(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_config(path: Path) -> dict[str, Any]:
    config = load_json_document(path)
    if not isinstance(config, dict):
        raise ValueError("manager config must be a JSON object")
    return config


def host_boot_marker() -> str | None:
    """Return a stable identifier for the current host boot when available."""
    if os.name == "nt":
        try:
            import ctypes

            get_tick_count = ctypes.windll.kernel32.GetTickCount64
            get_tick_count.restype = ctypes.c_ulonglong
            boot_epoch = time.time() - (int(get_tick_count()) / 1000)
            return f"windows:{int((boot_epoch + 30) // 60)}"
        except (AttributeError, OSError):
            return None

    boot_id = Path("/proc/sys/kernel/random/boot_id")
    try:
        value = boot_id.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return f"posix:{value}" if value else None


def update_host_boot_marker(store: StateStore, *, marker: str | None = None) -> bool:
    """Persist the current boot identity and report whether the host rebooted."""
    marker = marker if marker is not None else host_boot_marker()
    if not marker:
        return False
    previous = store.get_meta("host_boot_marker")
    store.set_meta("host_boot_marker", marker)
    return isinstance(previous, str) and previous != marker


def load_env_file(path: Path | None) -> None:
    """Load a simple local-only KEY=VALUE file without printing its values."""
    if path is None or not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not (
            key.startswith("MACHINE_MANAGER_")
            or key.startswith("GITHUB_")
        ):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def resolve_path(value: str | None, *, base: Path) -> Path | None:
    if value is None:
        return None
    path = Path(os.path.expandvars(value))
    return path if path.is_absolute() else base / path


def write_status(path: Path, status: dict[str, Any]) -> None:
    atomic_json_write(path, status)


def load_public_records(dashboard_dir: Path | None) -> tuple[list[Any], list[Any]]:
    """Load existing public records so a live publish does not erase history."""
    if dashboard_dir is None:
        return [], []
    data_dir = dashboard_dir / "data"
    events = load_json_document(data_dir / "events.json") if (data_dir / "events.json").exists() else []
    scenarios_data = (
        load_json_document(data_dir / "scenarios.json")
        if (data_dir / "scenarios.json").exists()
        else []
    )
    if isinstance(events, dict):
        events = events.get("events", [])
    if not isinstance(events, list):
        events = []
    if isinstance(scenarios_data, dict):
        scenarios = scenarios_data.get("scenarios", [])
    else:
        scenarios = scenarios_data
    if not isinstance(scenarios, list):
        scenarios = []
    return events, scenarios


def merge_public_events(
    existing: list[Any],
    current: list[Mapping[str, Any]],
    *,
    limit: int,
) -> list[Mapping[str, Any]]:
    """Keep a bounded chronological public event window without duplicates."""
    merged: list[Mapping[str, Any]] = []
    seen_ids: set[str] = set()
    for candidate in [*existing, *current]:
        if not isinstance(candidate, Mapping):
            continue
        event_id = str(candidate.get("event_id", "")).strip()
        if event_id and event_id in seen_ids:
            continue
        if event_id:
            seen_ids.add(event_id)
        merged.append(candidate)
    return merged[-max(1, int(limit)) :]


def _job_entries(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_jobs = config.get("jobs")
    if raw_jobs is None:
        worker = config.get("worker")
        if not isinstance(worker, Mapping):
            raise ValueError("config.worker must be an object")
        return [
            {
                "job_id": str(config.get("job_id", "job-001")),
                "objective_id": str(config.get("objective_id", "objective-001")),
                "objective": str(config.get("objective", config.get("objective_id", "objective-001"))),
                "max_restarts": int(config.get("max_restarts", 3)),
                "retry_reset_after_s": float(config.get("retry_reset_after_s", 3600)),
                "worker": dict(worker),
            }
        ]
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ValueError("config.jobs must be a non-empty array")
    entries: list[dict[str, Any]] = []
    for raw_job in raw_jobs:
        if not isinstance(raw_job, Mapping):
            raise ValueError("each config.jobs item must be an object")
        worker = raw_job.get("worker")
        if not isinstance(worker, Mapping):
            raise ValueError("each job must contain a worker object")
        job_id = str(raw_job.get("job_id", raw_job.get("id", ""))).strip()
        if not job_id:
            raise ValueError("each job requires an id")
        objective_id = str(raw_job.get("objective_id", job_id))
        entries.append(
            {
                "job_id": job_id,
                "objective_id": objective_id,
                "objective": str(raw_job.get("objective", objective_id)),
                "max_restarts": int(raw_job.get("max_restarts", config.get("max_restarts", 3))),
                "retry_reset_after_s": float(
                    raw_job.get("retry_reset_after_s", config.get("retry_reset_after_s", 3600))
                ),
                "worker": dict(worker),
            }
        )
    return entries


def _worker_spec(worker: Mapping[str, Any], *, config_path: Path) -> WorkerSpec:
    base = config_path.parent
    command = worker.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise ValueError("worker.command must be a non-empty string array")

    resource_probe = None
    resource_ok = None
    if worker.get("resource") == "nvidia-gpu":
        resource_probe = nvidia_smi_probe
        resource_ok = gpu_resource_ok

    stdout_file = resolve_path(worker.get("stdout_file"), base=base)
    progress_probe = None
    if str(worker.get("type", "")).strip().lower() == "keyhunt" and stdout_file is not None:
        progress_probe = lambda stdout_file=stdout_file: keyhunt_progress_probe(stdout_file)

    return WorkerSpec(
        worker_id=str(worker["id"]),
        worker_type=str(worker.get("type", "SpecialistWorker")),
        command=tuple(command),
        cwd=resolve_path(worker.get("cwd"), base=base),
        env=worker.get("env"),
        heartbeat_file=resolve_path(worker.get("heartbeat_file"), base=base),
        progress_file=resolve_path(worker.get("progress_file"), base=base),
        progress_probe=progress_probe,
        heartbeat_max_age_s=float(worker.get("heartbeat_max_age_s", 30)),
        startup_grace_s=float(worker.get("startup_grace_s", 5)),
        resource_probe=resource_probe,
        resource_ok=resource_ok,
        pid_file=resolve_path(worker.get("pid_file"), base=base),
        stdout_file=stdout_file,
        stderr_file=resolve_path(worker.get("stderr_file"), base=base),
    )


def manager_from_config(
    config: Mapping[str, Any],
    *,
    config_path: Path,
    state_store: StateStore | None = None,
    reset_retry_budget: bool = False,
    reset_retry_budget_for: set[str] | None = None,
) -> tuple[MachineManager, str, str]:
    entries = _job_entries(config)
    reset_job_ids = set(reset_retry_budget_for or ())
    manager = MachineManager(actor=str(config.get("actor", "local-manager")))
    primary_objective_id = entries[0]["objective_id"]
    primary_job_id = entries[0]["job_id"]
    for entry in entries:
        prior = state_store.get_job(entry["job_id"]) if state_store else None
        manager.register_job(
            _worker_spec(entry["worker"], config_path=config_path),
            objective_id=entry["objective_id"],
            job_id=entry["job_id"],
            max_restarts=entry["max_restarts"],
            retry_reset_after_s=entry["retry_reset_after_s"],
            initial_attempt=int((prior or {}).get("attempt", 0) or 0),
            initial_restart_count=0
            if reset_retry_budget or entry["job_id"] in reset_job_ids
            else int((prior or {}).get("restart_count", 0) or 0),
        )
    return manager, primary_objective_id, primary_job_id


_OPERATOR_RESUME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")


def _pending_operator_resume(
    config: Mapping[str, Any],
    store: StateStore,
    *,
    job_id: str,
    prior_state: str,
) -> str | None:
    """Return one unconsumed operator acknowledgement for an escalated job.

    This does not mark the request consumed. The caller does that only after it
    has successfully rebuilt the manager with the requested one-job reset.
    """

    raw = config.get("operator_resume")
    if not isinstance(raw, Mapping) or str(prior_state).upper() != "ESCALATED":
        return None
    request_id = str(raw.get("id", "")).strip()
    requested_job_id = str(raw.get("job_id", job_id)).strip()
    if not _OPERATOR_RESUME_ID.fullmatch(request_id) or requested_job_id != job_id:
        return None
    if store.get_meta(f"operator_resume:{request_id}"):
        return None
    return request_id


def _persist_runtime(
    manager: MachineManager,
    agents: AgentCoordinator,
    store: StateStore,
    seen_event_ids: set[str],
) -> None:
    for event in [*manager.events, *agents.events]:
        event_id = str(event.get("event_id", ""))
        if event_id and event_id not in seen_event_ids:
            store.append_event(event)
            seen_event_ids.add(event_id)
    updated = utc_now()
    for job_id, managed_job in manager.jobs.items():
        snapshot = managed_job.supervisor.snapshot()
        snapshot.update(
            {
                "job_id": job_id,
                "objective_id": managed_job.objective_id,
                "updated": updated,
            }
        )
        store.upsert_job(snapshot)
    for agent in agents.snapshot():
        store.upsert_agent(agent)


def _public_state_marker(snapshot: Mapping[str, Any]) -> str:
    """Return the compact state changes that justify an immediate public update.

    Metrics and timestamps intentionally do not participate: normal progress is
    published on the configured cadence, while an operational state transition
    becomes visible as soon as the local publisher has produced a safe record.
    """

    def entries(name: str) -> list[dict[str, str]]:
        source = snapshot.get(name, [])
        if not isinstance(source, list):
            return []
        return [
            {
                "id": str(item.get("id", item.get("worker_id", ""))),
                "state": str(item.get("state", "UNKNOWN")).upper(),
            }
            for item in source
            if isinstance(item, Mapping)
        ]

    return json.dumps(
        {
            "status": str(snapshot.get("status", "UNKNOWN")).upper(),
            "workers": entries("workers"),
            "jobs": entries("jobs"),
            "workstreams": entries("workstreams"),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _print_safe(message: str) -> None:
    print(str(message).encode("ascii", "replace").decode("ascii"), flush=True)


def _append_manager_log(path: Path | None, message: str) -> None:
    """Append local-only runtime diagnostics without exposing them publicly."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"[{utc_now()}] {message.rstrip()}\n")


def _record_noncritical_output_failure(
    manager_log_path: Path | None,
    *,
    output: str,
    error: Exception,
) -> None:
    """Report a local output problem without allowing it to stop supervision."""

    message = f"{output} deferred: {type(error).__name__}"
    _print_safe(message)
    try:
        _append_manager_log(manager_log_path, message)
    except OSError:
        # A blocked diagnostic log must not turn a non-critical output issue
        # into a manager crash.
        pass


def _publish_local_snapshot(
    publisher: TelemetryPublisher,
    snapshot: Mapping[str, Any],
    *,
    events: list[Any],
    scenarios: list[Any],
    manager_log_path: Path | None,
) -> bool:
    """Publish a public-safe snapshot, returning false when it is deferred."""

    try:
        publisher.publish(snapshot, events=events, scenarios=scenarios)
    except Exception as error:
        _record_noncritical_output_failure(
            manager_log_path,
            output="Local telemetry",
            error=error,
        )
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--dashboard-dir", type=Path)
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--lock-file", type=Path)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument(
        "--resume-after-host-boot",
        action="store_true",
        help="reset the bounded worker retry budget once after a confirmed host restart",
    )
    parser.add_argument("--once", action="store_true", help="start and perform one observation")
    args = parser.parse_args()

    config_path = args.config.resolve()
    config = load_config(config_path)
    charter = OperatingCharter.from_mapping(config.get("autonomy"))
    base = config_path.parent
    load_env_file(resolve_path(config.get("env_file"), base=base))
    manager_log_path = (
        args.log_file
        or resolve_path(config.get("manager_log_file"), base=base)
        or resolve_path(config.get("log_file"), base=base)
    )

    status_path = args.status_file or resolve_path(config.get("status_file"), base=base)
    dashboard_dir = args.dashboard_dir or resolve_path(config.get("dashboard_dir"), base=base)
    state_path = (
        args.state_db
        or resolve_path(config.get("state_db"), base=base)
        or (base / "var" / "machine_manager.sqlite3")
    )
    lock_path = (
        args.lock_file
        or resolve_path(config.get("lock_file"), base=base)
        or state_path.with_name("manager.lock")
    )
    store = StateStore(
        state_path,
        event_retention=int(config.get("event_retention", 5000)),
    )
    lock: InstanceLock | None = None
    manager: MachineManager | None = None
    agents: AgentCoordinator | None = None
    scheduler: WorkScheduler | None = None
    queue_dispatcher: WorkDispatcher | None = None
    queue_dispatch_limit = 4
    workstreams: WorkstreamRegistry | None = None
    local_publisher: TelemetryPublisher | None = None
    remote_publisher: GitHubPagesPublisher | None = None
    capabilities: CapabilityRegistry | None = None
    evidence: EvidenceCoordinator | None = None
    public_events: list[Any] = []
    public_scenarios: list[Any] = []
    seen_event_ids: set[str] = set()
    primary_objective_id = ""
    primary_job_id = ""
    objective = ""
    public_event_limit = 500
    last_public_marker: str | None = None
    requested_stop = False
    try:
        try:
            lock = InstanceLock(lock_path).acquire()
        except InstanceAlreadyRunning:
            _print_safe("Machine Manager is already running.")
            return 0

        host_boot_changed = update_host_boot_marker(store)
        configured_primary_job_id = _job_entries(config)[0]["job_id"]
        prior_primary_job = store.get_job(configured_primary_job_id) or {}
        operator_resume_id = _pending_operator_resume(
            config,
            store,
            job_id=configured_primary_job_id,
            prior_state=str(prior_primary_job.get("state", "UNKNOWN")),
        )
        manager, primary_objective_id, primary_job_id = manager_from_config(
            config,
            config_path=config_path,
            state_store=store,
            reset_retry_budget=host_boot_changed or args.resume_after_host_boot,
            reset_retry_budget_for={configured_primary_job_id} if operator_resume_id else None,
        )
        if primary_job_id != configured_primary_job_id:
            raise RuntimeError("configured primary job changed during manager setup")
        if host_boot_changed or args.resume_after_host_boot:
            store.append_event(
                {
                    "timestamp": utc_now(),
                    "event_id": f"evt-host-boot-{time.time_ns()}",
                    "objective_id": primary_objective_id,
                    "job_id": primary_job_id,
                    "worker_id": "",
                    "actor": str(config.get("actor", "local-manager")),
                    "event_type": "retry_budget_reset",
                    "previous_state": str(prior_primary_job.get("state", "UNKNOWN")),
                    "new_state": "QUEUED",
                    "metrics": {
                        "attempt": int(prior_primary_job.get("attempt", 0) or 0),
                        "restart_count": 0,
                        "max_restarts": manager.jobs[primary_job_id].supervisor.max_restarts,
                    },
                    "action": "resume_after_host_boot",
                    "outcome": "retry_budget_reset",
                    "artifact_refs": [],
                    "error": None,
                    "duration": None,
                }
            )
        if operator_resume_id:
            store.set_meta(f"operator_resume:{operator_resume_id}", utc_now())
            store.append_event(
                {
                    "timestamp": utc_now(),
                    "event_id": f"evt-operator-resume-{time.time_ns()}",
                    "objective_id": primary_objective_id,
                    "job_id": primary_job_id,
                    "worker_id": "",
                    "actor": "operator",
                    "event_type": "operator_resume_authorized",
                    "previous_state": str(prior_primary_job.get("state", "UNKNOWN")),
                    "new_state": "QUEUED",
                    "metrics": {
                        "attempt": int(prior_primary_job.get("attempt", 0) or 0),
                        "restart_count": 0,
                        "max_restarts": manager.jobs[primary_job_id].supervisor.max_restarts,
                    },
                    "action": "resume_escalated_job",
                    "outcome": "operator_authorized_one_time_recovery",
                    "artifact_refs": [f"operator-resume:{operator_resume_id}"],
                    "error": None,
                    "duration": None,
                }
            )
        agents_raw = config.get("agents", [])
        if not isinstance(agents_raw, list):
            raise ValueError("config.agents must be an array")
        scheduler = WorkScheduler(store)
        recovered_task_count = scheduler.recover_interrupted()
        if recovered_task_count:
            store.append_event(
                {
                    "timestamp": utc_now(),
                    "event_id": f"evt-queue-recovery-{time.time_ns()}",
                    "objective_id": primary_objective_id,
                    "job_id": "task-queue",
                    "worker_id": "",
                    "actor": str(config.get("actor", "local-manager")),
                    "event_type": "queue_tasks_recovered",
                    "previous_state": "RUNNING",
                    "new_state": "QUEUED",
                    "metrics": {"tasks_requeued": recovered_task_count},
                    "action": "recover_interrupted_tasks",
                    "outcome": "requeued_for_retry",
                    "artifact_refs": [],
                    "error": None,
                    "duration": None,
                }
            )
        raw_dispatch = config.get("queue_dispatch", {})
        if raw_dispatch is None:
            raw_dispatch = {}
        research_handlers: dict[str, Any] = {}
        try:
            if not isinstance(raw_dispatch, Mapping):
                raise ValueError("config.queue_dispatch must be an object")
            dispatch_enabled = bool(raw_dispatch.get("enabled", False))
            queue_dispatch_limit = max(1, min(int(raw_dispatch.get("limit", 4)), 20))
            defer_delay_s = float(raw_dispatch.get("defer_delay_s", 300))
            max_attempts = int(raw_dispatch.get("max_attempts", 3))
            raw_research = config.get("research_worker", {})
            if raw_research is None:
                raw_research = {}
            if not isinstance(raw_research, Mapping):
                raise ValueError("config.research_worker must be an object")
            if bool(raw_research.get("enabled", False)):
                mode = str(raw_research.get("mode", "ollama")).strip().lower()
                artifact_dir = resolve_path(
                    str(raw_research.get("artifact_dir", "var/research")),
                    base=base,
                )
                if artifact_dir is None:
                    raise ValueError("research artifact_dir is required")
                if mode == "evidence_only":
                    research_handlers["research"] = PublicResearchHandler(
                        artifact_dir,
                        max_sources=int(raw_research.get("max_sources", 3)),
                    )
                elif mode == "ollama":
                    research_handlers["research"] = OllamaResearchHandler(
                        artifact_dir,
                        model=str(raw_research.get("model", "")),
                        base_url=str(raw_research.get("base_url", "http://127.0.0.1:11434")),
                        model_timeout_s=float(raw_research.get("model_timeout_s", 30)),
                        source_timeout_s=float(raw_research.get("source_timeout_s", 15)),
                        max_source_bytes=int(raw_research.get("max_source_bytes", 120000)),
                        max_sources=int(raw_research.get("max_sources", 3)),
                    )
                else:
                    raise ValueError("research_worker.mode must be ollama or evidence_only")
            if dispatch_enabled:
                queue_dispatcher = WorkDispatcher(
                    scheduler,
                    research_handlers,
                    defer_delay_s=defer_delay_s,
                    max_attempts=max_attempts,
                    actor=str(config.get("actor", "local-manager")),
                )
        except (TypeError, ValueError) as exc:
            store.append_event(
                {
                    "timestamp": utc_now(),
                    "event_id": f"evt-queue-dispatch-config-{time.time_ns()}",
                    "objective_id": primary_objective_id,
                    "job_id": "task-queue",
                    "worker_id": "",
                    "actor": str(config.get("actor", "local-manager")),
                    "event_type": "queue_dispatch_configuration_deferred",
                    "previous_state": "UNKNOWN",
                    "new_state": "DEFERRED",
                    "metrics": {},
                    "action": "load_queue_dispatch_config",
                    "outcome": "core_supervision_continues",
                    "artifact_refs": [],
                    "error": type(exc).__name__,
                    "duration": None,
                }
            )
            _append_manager_log(manager_log_path, f"Queue dispatch configuration deferred: {type(exc).__name__}")
        agents = AgentCoordinator(agents_raw, scheduler=scheduler)
        try:
            workstreams = WorkstreamRegistry(store, config.get("workstreams", []))
        except (TypeError, ValueError) as exc:
            store.append_event(
                {
                    "timestamp": utc_now(),
                    "event_id": f"evt-workstream-config-{time.time_ns()}",
                    "objective_id": primary_objective_id,
                    "job_id": primary_job_id,
                    "worker_id": "",
                    "actor": str(config.get("actor", "local-manager")),
                    "event_type": "workstream_configuration_deferred",
                    "previous_state": "UNKNOWN",
                    "new_state": "DEFERRED",
                    "metrics": {},
                    "action": "load_workstream_config",
                    "outcome": "core_supervision_continues",
                    "artifact_refs": [],
                    "error": type(exc).__name__,
                    "duration": None,
                }
            )
            _append_manager_log(manager_log_path, f"Workstream configuration deferred: {type(exc).__name__}")
            workstreams = None
        try:
            evidence = EvidenceCoordinator(
                store,
                config.get("evidence"),
                base=base,
                actor=str(config.get("actor", "local-manager")),
                objective_id=primary_objective_id,
                job_id=primary_job_id,
            )
        except (TypeError, ValueError) as exc:
            # Evidence is additive. A malformed optional audit configuration must
            # never take down the protected worker or the core supervisor.
            store.append_event(
                {
                    "timestamp": utc_now(),
                    "event_id": f"evt-evidence-config-{time.time_ns()}",
                    "objective_id": primary_objective_id,
                    "job_id": primary_job_id,
                    "worker_id": "",
                    "actor": str(config.get("actor", "local-manager")),
                    "event_type": "evidence_configuration_deferred",
                    "previous_state": "UNKNOWN",
                    "new_state": "DEFERRED",
                    "metrics": {},
                    "action": "load_evidence_config",
                    "outcome": "core_supervision_continues",
                    "artifact_refs": [],
                    "error": type(exc).__name__,
                    "duration": None,
                }
            )
            _append_manager_log(manager_log_path, f"Evidence configuration deferred: {type(exc).__name__}")
            evidence = None
        public_events, public_scenarios = load_public_records(dashboard_dir)
        local_publisher = TelemetryPublisher(dashboard_dir) if dashboard_dir else None

        upload_config = config.get("public_upload", {})
        if not isinstance(upload_config, Mapping):
            raise ValueError("config.public_upload must be an object")
        remote_publisher = (
            GitHubPagesPublisher.from_mapping(dashboard_dir, upload_config)
            if dashboard_dir and bool(upload_config.get("enabled", False))
            else None
        )
        capabilities = CapabilityRegistry.default(
            github_upload_enabled=bool(remote_publisher and remote_publisher.configured),
            agents_enabled=any(
                bool(item.get("enabled"))
                for item in agents_raw
                if isinstance(item, Mapping)
            ),
            execute_and_report_enabled=charter.mode == "EXECUTE_AND_REPORT",
            transparent_outreach_enabled=charter.allow_outreach and charter.honor_outreach_opt_out,
            developer_tools_enabled=charter.allow_tool_installation,
            gpu_idle_use_enabled=charter.allow_gpu_when_protected_worker_idle,
            evidence_ledger_enabled=bool(evidence and evidence.enabled),
            constraint_audit_enabled=bool(evidence and evidence.targets),
            workstreams_enabled=bool(workstreams and workstreams.specs),
        )
        interval = max(0.1, float(config.get("poll_interval_s", 15)))
        objective = str(config.get("objective", primary_objective_id))
        public_event_limit = max(20, int(config.get("public_event_limit", 500)))
        manager.start_all()
        cpu_probe = CpuUsageProbe()

        while True:
            statuses = manager.tick_all(auto_recover=True)
            snapshot = manager.snapshot(objective=objective)
            snapshot["objective_id"] = primary_objective_id
            snapshot["updated"] = utc_now()
            snapshot["system"] = cpu_probe()
            for status in statuses.values():
                health = status.get("health", {})
                metrics = health.get("metrics", {}) if isinstance(health, Mapping) else {}
                if isinstance(metrics, Mapping) and metrics:
                    snapshot["gpu"] = dict(metrics)
                    if isinstance(health, Mapping) and isinstance(health.get("resource_active"), bool):
                        snapshot["gpu"]["resource_active"] = health["resource_active"]
                    break
            agents.tick(snapshot, events=store.recent_events(limit=20))
            if evidence is not None:
                for event in evidence.tick():
                    store.append_event(event)
            snapshot["agents"] = agents.snapshot()
            snapshot["capabilities"] = capabilities.snapshot()
            snapshot["autonomy"] = charter.public_summary()
            snapshot["worker_profiles"] = evidence.public_profiles() if evidence else []
            snapshot["constraint_audits"] = evidence.public_audits() if evidence else []
            if workstreams is not None:
                stream_snapshot, stream_events = workstreams.sync(
                    agents=snapshot["agents"],
                    audits=snapshot["constraint_audits"],
                    profiles=snapshot["worker_profiles"],
                )
                snapshot["workstreams"] = stream_snapshot
                for event in stream_events:
                    store.append_event(event)
            else:
                snapshot["workstreams"] = []
            if queue_dispatcher is not None:
                try:
                    queue_dispatcher.dispatch(limit=queue_dispatch_limit)
                except Exception as error:
                    _record_noncritical_output_failure(
                        manager_log_path,
                        output="Queue dispatch",
                        error=error,
                    )
            snapshot["queue"] = scheduler.snapshot()
            snapshot["queue_kinds"] = scheduler.kind_snapshot()
            snapshot["queue_activity"] = scheduler.activity_snapshot(limit=20)
            _persist_runtime(manager, agents, store, seen_event_ids)
            public_events = merge_public_events(
                public_events,
                store.recent_events(limit=public_event_limit),
                limit=public_event_limit,
            )

            local_snapshot_ready = True
            if status_path:
                try:
                    write_status(status_path, snapshot)
                except Exception as error:
                    _record_noncritical_output_failure(
                        manager_log_path,
                        output="Local status",
                        error=error,
                    )
            if local_publisher:
                local_snapshot_ready = _publish_local_snapshot(
                    local_publisher,
                    snapshot,
                    events=public_events,
                    scenarios=public_scenarios,
                    manager_log_path=manager_log_path,
                )
            if remote_publisher and local_snapshot_ready:
                try:
                    public_marker = _public_state_marker(snapshot)
                    immediate_public_update = public_marker != last_public_marker
                    last_public_marker = public_marker
                    remote_publisher.maybe_publish(immediate=immediate_public_update)
                except PublicUploadError as exc:
                    _print_safe(f"Public upload deferred: {type(exc).__name__}")
            if status_path and remote_publisher:
                status_copy = dict(snapshot)
                status_copy["public_upload"] = remote_publisher.status()
                try:
                    write_status(status_path, status_copy)
                except Exception as error:
                    _record_noncritical_output_failure(
                        manager_log_path,
                        output="Local status",
                        error=error,
                    )
            if args.once:
                requested_stop = True
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        requested_stop = True
        _print_safe("Machine Manager stopping.")
        try:
            _append_manager_log(manager_log_path, "Machine Manager stopped by KeyboardInterrupt.")
        except OSError:
            pass
    except Exception as exc:
        _print_safe(f"Machine Manager stopped: {type(exc).__name__}")
        try:
            _append_manager_log(manager_log_path, traceback.format_exc())
        except OSError:
            pass
        raise
    finally:
        if manager is not None and agents is not None and capabilities is not None and scheduler is not None:
            if requested_stop:
                manager.cancel_all()
            _persist_runtime(manager, agents, store, seen_event_ids)
            if local_publisher and requested_stop:
                stopped = manager.snapshot(objective=objective)
                stopped["objective_id"] = primary_objective_id
                stopped["updated"] = utc_now()
                stopped["agents"] = agents.snapshot()
                stopped["capabilities"] = capabilities.snapshot()
                stopped["autonomy"] = charter.public_summary()
                stopped["worker_profiles"] = evidence.public_profiles() if evidence else []
                stopped["constraint_audits"] = evidence.public_audits() if evidence else []
                stopped["queue"] = scheduler.snapshot()
                stopped["queue_kinds"] = scheduler.kind_snapshot()
                stopped["queue_activity"] = scheduler.activity_snapshot(limit=20)
                public_events = merge_public_events(
                    public_events,
                    store.recent_events(limit=public_event_limit),
                    limit=public_event_limit,
                )
                _publish_local_snapshot(
                    local_publisher,
                    stopped,
                    events=public_events,
                    scenarios=public_scenarios,
                    manager_log_path=manager_log_path,
                )
        if agents is not None:
            agents.close()
        if evidence is not None:
            evidence.close()
        if lock is not None:
            lock.release()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
