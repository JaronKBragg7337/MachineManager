"""Publish a local manager status file as sanitized dashboard telemetry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .telemetry import TelemetryPublisher


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def snapshot_from_status(
    status: dict[str, Any],
    *,
    objective: str,
    worker_id: str,
    worker_type: str,
    job_id: str,
    objective_id: str,
) -> dict[str, Any]:
    gpu_input = status.get("gpu") if isinstance(status.get("gpu"), dict) else {}
    state = "RUNNING" if status.get("search_active") else "STALLED"
    return {
        "manager_version": "0.2",
        "status": "HEALTHY" if state == "RUNNING" else "STALLED",
        "objective": objective,
        "updated": status.get("timestamp"),
        "workers": [{"id": worker_id, "type": worker_type, "state": state, "owner": "local-manager"}],
        "jobs": [{"id": job_id, "objective_id": objective_id, "state": state}],
        "worker_profiles": status.get("worker_profiles", []),
        "constraint_audits": status.get("constraint_audits", []),
        "gpu": {
            "util_pct": gpu_input.get("util", gpu_input.get("util_pct")),
            "mem_used_mib": gpu_input.get("mem_used", gpu_input.get("mem_used_mib")),
            "mem_total_mib": gpu_input.get("mem_total", gpu_input.get("mem_total_mib")),
            "temp_c": gpu_input.get("temp", gpu_input.get("temp_c")),
            "power_w": gpu_input.get("power", gpu_input.get("power_w")),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--dashboard-dir", type=Path, required=True)
    parser.add_argument("--objective", default="Local worker supervision")
    parser.add_argument("--worker-id", default="worker-001")
    parser.add_argument("--worker-type", default="SpecialistWorker")
    parser.add_argument("--job-id", default="job-001")
    parser.add_argument("--objective-id", default="objective-001")
    args = parser.parse_args()

    status = load_json(args.status_file, {})
    if not isinstance(status, dict):
        raise SystemExit(f"status file must contain a JSON object: {args.status_file}")

    data_dir = args.dashboard_dir / "data"
    old_events = load_json(data_dir / "events.json", [])
    if not isinstance(old_events, list):
        old_events = []
    old_scenarios = load_json(data_dir / "scenarios.json", {})
    if isinstance(old_scenarios, dict):
        old_scenarios = old_scenarios.get("scenarios", [])
    if not isinstance(old_scenarios, list):
        old_scenarios = []

    snapshot = snapshot_from_status(
        status,
        objective=args.objective,
        worker_id=args.worker_id,
        worker_type=args.worker_type,
        job_id=args.job_id,
        objective_id=args.objective_id,
    )
    TelemetryPublisher(args.dashboard_dir).publish(snapshot, events=old_events, scenarios=old_scenarios)
    print(f"Published sanitized telemetry to {args.dashboard_dir / 'data'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
