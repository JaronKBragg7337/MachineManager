"""Job registry and orchestration layer for specialist workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .supervisor import JobState, WorkerSpec, WorkerSupervisor


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

    def register_job(
        self,
        spec: WorkerSpec,
        *,
        objective_id: str,
        job_id: str,
        max_restarts: int = 3,
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
            ),
        )
        self.jobs[job_id] = job
        return job

    def start_job(self, job_id: str) -> bool:
        return self.jobs[job_id].supervisor.start()

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

    @property
    def events(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
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
                }
            )
            jobs.append(
                {
                    "id": job.job_id,
                    "objective_id": job.objective_id,
                    "state": job.supervisor.state.value,
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
