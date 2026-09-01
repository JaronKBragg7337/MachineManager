"""Job registry and orchestration layer for specialist workers."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any
import uuid

from .supervisor import JobState, WorkerSpec, WorkerSupervisor, utc_now


@dataclass
class ManagedJob:
    job_id: str
    objective_id: str
    supervisor: WorkerSupervisor


class MachineManager:
    """Register jobs, tick health, recover workers, and expose snapshots."""

    version = "0.2"

    def __init__(self, *, actor: str = "local-manager") -> None:
        self.actor = actor
        self.jobs: dict[str, ManagedJob] = {}
        self._events: list[dict[str, Any]] = []

    def register_job(
        self,
        spec: WorkerSpec,
        *,
        objective_id: str,
        job_id: str,
        max_restarts: int = 3,
        initial_attempt: int = 0,
        initial_restart_count: int = 0,
        retry_reset_after_s: float = 3600.0,
    ) -> ManagedJob:
        if job_id in self.jobs:
            raise ValueError(f"job already registered: {job_id}")
        job = ManagedJob(
            job_id=job_id,
            objective_id=objective_id,
            supervisor=WorkerSupervisor(
                spec,
                objective_id=objective_id,
                job_id=job_id,
                actor=self.actor,
                max_restarts=max_restarts,
                initial_attempt=initial_attempt,
                initial_restart_count=initial_restart_count,
                retry_reset_after_s=retry_reset_after_s,
            ),
        )
        self.jobs[job_id] = job
        return job

    @property
    def job_ids(self) -> list[str]:
        return list(self.jobs)

    def start_job(self, job_id: str) -> bool:
        return self.jobs[job_id].supervisor.start()

    def start_all(self) -> dict[str, bool]:
        return {job_id: self.start_job(job_id) for job_id in self.jobs}

    def queue_objective_change(
        self,
        job_id: str,
        *,
        scheduler: Any,
        new_objective_id: str,
        task_id: str | None = None,
    ) -> str:
        """Queue a new objective without mutating an active worker assignment.

        A running worker must finish or be explicitly handed off before a new
        objective can reuse its slot. Keeping the current job unchanged makes
        the boundary visible and gives the scheduler a durable next action.
        """
        if job_id not in self.jobs:
            raise KeyError(f"unknown job: {job_id}")
        requested_objective_id = str(new_objective_id).strip()
        if not requested_objective_id or len(requested_objective_id) > 120:
            raise ValueError("new_objective_id must be a non-empty compact value")

        job = self.jobs[job_id]
        if requested_objective_id == job.objective_id:
            raise ValueError("new objective must differ from the active objective")

        queued_task_id = scheduler.enqueue(
            kind="objective_change",
            objective_id=requested_objective_id,
            payload={
                "job_id": job_id,
                "from_objective_id": job.objective_id,
            },
            task_id=task_id,
        )
        state = job.supervisor.state.value
        self._events.append(
            {
                "timestamp": utc_now(),
                "event_id": f"evt-objective-change-{uuid.uuid4().hex[:12]}",
                "objective_id": job.objective_id,
                "job_id": job_id,
                "worker_id": job.supervisor.spec.worker_id,
                "actor": self.actor,
                "event_type": "objective_change_queued",
                "previous_state": state,
                "new_state": state,
                "metrics": {"active_job_preserved": True},
                "action": "queue_objective_change",
                "outcome": "active_job_preserved",
                "artifact_refs": [f"task:{queued_task_id}"],
                "error": None,
                "duration": None,
            }
        )
        self._events = self._events[-500:]
        return queued_task_id

    def tick_job(self, job_id: str, *, auto_recover: bool = True) -> dict[str, Any]:
        supervisor = self.jobs[job_id].supervisor
        health = supervisor.observe()
        if (
            auto_recover
            and not health.healthy
            and not supervisor.in_startup_grace
            and supervisor.state in {JobState.FAILED, JobState.STALLED}
        ):
            supervisor.recover()
        return supervisor.snapshot()

    def tick_all(self, *, auto_recover: bool = True) -> dict[str, dict[str, Any]]:
        return {job_id: self.tick_job(job_id, auto_recover=auto_recover) for job_id in self.jobs}

    def complete_job(self, job_id: str) -> None:
        self.jobs[job_id].supervisor.mark_complete()

    def cancel_job(self, job_id: str) -> None:
        self.jobs[job_id].supervisor.cancel()

    def cancel_all(self) -> None:
        for job_id in self.jobs:
            self.cancel_job(job_id)

    @property
    def events(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = list(self._events)
        for job in self.jobs.values():
            result.extend(job.supervisor.events)
        return sorted(result, key=lambda event: event["timestamp"])

    def snapshot(self, *, objective: str = "") -> dict[str, Any]:
        workers = []
        jobs = []
        for job in self.jobs.values():
            worker = job.supervisor.snapshot()
            workers.append(
                {
                    "id": worker["worker_id"],
                    "type": worker["worker_type"],
                    "state": worker["state"],
                    "owner": self.actor,
                    "pid": worker["pid"],
                    "progress": worker.get("progress", {}),
                }
            )
            jobs.append(
                {
                    "id": job.job_id,
                    "objective_id": job.objective_id,
                    "state": job.supervisor.state.value,
                    "progress": worker.get("progress", {}),
                }
            )

        states = {job.supervisor.state for job in self.jobs.values()}
        if not self.jobs:
            status = "IDLE"
        elif states <= {JobState.RUNNING}:
            status = "HEALTHY"
        elif states <= {JobState.QUEUED, JobState.STARTING, JobState.VERIFYING}:
            status = "STARTING"
        elif JobState.ESCALATED in states:
            status = "ESCALATED"
        elif JobState.STALLED in states:
            status = "STALLED"
        else:
            status = "DEGRADED"

        return {
            "manager_version": self.version,
            "status": status,
            "objective": objective,
            "workers": workers,
            "jobs": jobs,
        }
