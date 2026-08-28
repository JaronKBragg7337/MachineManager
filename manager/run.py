"""Run a configured local Machine Manager job."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .machine_manager import MachineManager
from .probes import gpu_resource_ok, nvidia_smi_probe
from .supervisor import WorkerSpec
from .telemetry import TelemetryPublisher, utc_now


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("manager config must be a JSON object")
    return config


def resolve_path(value: str | None, *, base: Path) -> Path | None:
    if value is None:
        return None
    path = Path(os.path.expandvars(value))
    return path if path.is_absolute() else base / path


def write_status(path: Path, status: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(status, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def manager_from_config(config: dict[str, Any], *, config_path: Path) -> tuple[MachineManager, str, str]:
    base = config_path.parent
    worker = config.get("worker")
    if not isinstance(worker, dict):
        raise ValueError("config.worker must be an object")
    command = worker.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise ValueError("config.worker.command must be a non-empty string array")

    resource_probe = None
    resource_ok = None
    if worker.get("resource") == "nvidia-gpu":
        resource_probe = nvidia_smi_probe
        resource_ok = gpu_resource_ok

    spec = WorkerSpec(
        worker_id=str(worker["id"]),
        worker_type=str(worker.get("type", "SpecialistWorker")),
        command=tuple(command),
        cwd=resolve_path(worker.get("cwd"), base=base),
        env=worker.get("env"),
        heartbeat_file=resolve_path(worker.get("heartbeat_file"), base=base),
        heartbeat_max_age_s=float(worker.get("heartbeat_max_age_s", 30)),
        startup_grace_s=float(worker.get("startup_grace_s", 5)),
        resource_probe=resource_probe,
        resource_ok=resource_ok,
        stdout_file=resolve_path(worker.get("stdout_file"), base=base),
        stderr_file=resolve_path(worker.get("stderr_file"), base=base),
    )
    manager = MachineManager(actor=str(config.get("actor", "local-manager")))
    objective_id = str(config.get("objective_id", "objective-001"))
    job_id = str(config.get("job_id", "job-001"))
    manager.register_job(
        spec,
        objective_id=objective_id,
        job_id=job_id,
        max_restarts=int(config.get("max_restarts", 3)),
    )
    return manager, objective_id, job_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--dashboard-dir", type=Path)
    parser.add_argument("--once", action="store_true", help="start and perform one observation")
    args = parser.parse_args()

    config = load_config(args.config)
    manager, objective_id, job_id = manager_from_config(config, config_path=args.config)
    if not manager.start_job(job_id):
        return 1

    publisher = TelemetryPublisher(args.dashboard_dir) if args.dashboard_dir else None
    interval = max(0.1, float(config.get("poll_interval_s", 15)))
    objective = str(config.get("objective", objective_id))

    try:
        while True:
            statuses = manager.tick_all(auto_recover=True)
            snapshot = manager.snapshot(objective=objective)
            snapshot["updated"] = utc_now()
            health = statuses[job_id].get("health", {})
            metrics = health.get("metrics", {}) if isinstance(health, dict) else {}
            if isinstance(metrics, dict) and metrics:
                snapshot["gpu"] = metrics
            if args.status_file:
                write_status(args.status_file, snapshot)
            if publisher:
                publisher.publish(snapshot, events=manager.events)
            if args.once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        manager.cancel_job(job_id)
    finally:
        if args.once:
            manager.cancel_job(job_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
