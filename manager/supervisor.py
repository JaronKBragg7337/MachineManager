"""Process supervision with multi-signal health and bounded recovery."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


def utc_now() -> str:
    """Return an unambiguous UTC timestamp for event records."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JobState(str, Enum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    COMPLETE = "COMPLETE"
    STALLED = "STALLED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class WorkerSpec:
    """Everything needed to start and verify one specialist worker.

    ``heartbeat_file`` is deliberately a narrow progress contract. A process
    being alive is never sufficient for a healthy result when a heartbeat is
    configured. ``resource_probe`` can add a second external signal such as
    GPU activity for workloads like KeyHunt.
    """

    worker_id: str
    worker_type: str
    command: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    heartbeat_file: Path | None = None
    heartbeat_max_age_s: float = 30.0
    startup_grace_s: float = 5.0
    resource_probe: Callable[[], Mapping[str, Any]] | None = None
    resource_ok: Callable[[Mapping[str, Any]], bool] | None = None
    pid_file: Path | None = None
    stdout_file: Path | None = None
    stderr_file: Path | None = None

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("worker command cannot be empty")
        if self.heartbeat_max_age_s <= 0:
            raise ValueError("heartbeat_max_age_s must be positive")
        if self.startup_grace_s < 0:
            raise ValueError("startup_grace_s cannot be negative")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return result.returncode == 0 and str(pid) in result.stdout
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class _AdoptedProcess:
    """Minimal process handle for a worker that survived a manager restart."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> int | None:
        return None if _pid_alive(self.pid) else 1

    def terminate(self) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                capture_output=True,
                timeout=8,
                check=False,
            )
        else:
            os.kill(self.pid, 15)

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while _pid_alive(self.pid):
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("adopted-process", timeout)
            time.sleep(0.05)
        return 1


@dataclass(frozen=True)
class HealthSignals:
    """A public-safe view of the signals used for one health decision."""

    process_alive: bool
    heartbeat_fresh: bool
    resource_active: bool
    heartbeat_age_s: float | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.process_alive and self.heartbeat_fresh and self.resource_active

    def as_dict(self) -> dict[str, Any]:
        return {
            "process_alive": self.process_alive,
            "heartbeat_fresh": self.heartbeat_fresh,
            "resource_active": self.resource_active,
            "heartbeat_age_s": self.heartbeat_age_s,
            "metrics": dict(self.metrics),
            "healthy": self.healthy,
        }


class WorkerSupervisor:
    """Supervise one worker and recover it a bounded number of times."""

    def __init__(
        self,
        spec: WorkerSpec,
        *,
        objective_id: str,
        job_id: str,
        actor: str = "local-manager",
        max_restarts: int = 3,
        initial_attempt: int = 0,
        initial_restart_count: int = 0,
    ) -> None:
        if max_restarts < 0:
            raise ValueError("max_restarts cannot be negative")
        if initial_attempt < 0 or initial_restart_count < 0:
            raise ValueError("initial counters cannot be negative")
        self.spec = spec
        self.objective_id = objective_id
        self.job_id = job_id
        self.actor = actor
        self.max_restarts = max_restarts
        self.state = JobState.QUEUED
        self.process: Any = None
        self.attempt = initial_attempt
        self.restart_count = initial_restart_count
        self.events: list[dict[str, Any]] = []
        self.last_health: HealthSignals | None = None
        self._started_monotonic: float | None = None
        self._stream_handles: list[Any] = []

    def _emit(
        self,
        *,
        event_type: str,
        previous_state: JobState,
        new_state: JobState,
        metrics: Mapping[str, Any] | None = None,
        action: str | None = None,
        outcome: str | None = None,
        error: str | None = None,
        duration: float | None = None,
    ) -> None:
        self.events.append(
            {
                "timestamp": utc_now(),
                "event_id": f"evt-{uuid.uuid4().hex[:12]}",
                "objective_id": self.objective_id,
                "job_id": self.job_id,
                "worker_id": self.spec.worker_id,
                "actor": self.actor,
                "event_type": event_type,
                "previous_state": previous_state.value,
                "new_state": new_state.value,
                "metrics": dict(metrics or {}),
                "action": action,
                "outcome": outcome,
                "artifact_refs": [],
                "error": error,
                "duration": duration,
            }
        )

    def _transition(
        self,
        new_state: JobState,
        *,
        event_type: str = "state_change",
        metrics: Mapping[str, Any] | None = None,
        action: str | None = None,
        outcome: str | None = None,
        error: str | None = None,
    ) -> None:
        previous = self.state
        self.state = new_state
        duration = None
        if self._started_monotonic is not None and new_state in {
            JobState.COMPLETE,
            JobState.FAILED,
            JobState.STALLED,
            JobState.ESCALATED,
            JobState.CANCELLED,
        }:
            duration = round(time.monotonic() - self._started_monotonic, 3)
        self._emit(
            event_type=event_type,
            previous_state=previous,
            new_state=new_state,
            metrics=metrics,
            action=action,
            outcome=outcome,
            error=error,
            duration=duration,
        )

    def _open_stream(self, path: Path | None) -> Any:
        if path is None:
            return subprocess.DEVNULL
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a", encoding="utf-8")
        self._stream_handles.append(handle)
        return handle

    def _write_pid_file(self, pid: int) -> None:
        path = self.spec.pid_file
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
                json.dump({"pid": int(pid), "worker_id": self.spec.worker_id}, handle)
                handle.write("\n")
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _clear_pid_file(self, pid: int | None = None) -> None:
        path = self.spec.pid_file
        if path is None or not path.exists():
            return
        if pid is not None:
            try:
                payload = json.loads(path.read_text(encoding="ascii"))
                if int(payload.get("pid", -1)) != int(pid):
                    return
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _read_pid_file(self) -> int | None:
        path = self.spec.pid_file
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="ascii"))
            pid = int(payload.get("pid", 0))
            worker_id = str(payload.get("worker_id", ""))
            if pid <= 0 or worker_id != self.spec.worker_id:
                return None
            return pid
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _adopt_existing(self) -> bool:
        pid = self._read_pid_file()
        if pid is None:
            return False
        candidate = _AdoptedProcess(pid)
        if candidate.poll() is not None:
            self._clear_pid_file(pid)
            return False
        self.process = candidate
        health = self._read_health()
        if not health.healthy:
            self._terminate_process()
            self.process = None
            return False
        self.attempt += 1
        self._started_monotonic = time.monotonic()
        self._transition(
            JobState.VERIFYING,
            event_type="worker_adopted",
            metrics={"attempt": self.attempt, "pid": pid},
            action="adopt",
            outcome="survived_manager_restart",
        )
        return True

    def _spawn(self) -> None:
        env = None
        if self.spec.env is not None:
            env = os.environ.copy()
            env.update({str(key): str(value) for key, value in self.spec.env.items()})

        creationflags = 0
        if os.name == "nt":
            # CREATE_NEW_PROCESS_GROUP is valid for a normal child process.
            # Avoid the incompatible CREATE_NEW_CONSOLE | DETACHED_PROCESS
            # combination that caused WinError 87 in the prototype.
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        self.process = subprocess.Popen(
            list(self.spec.command),
            cwd=str(self.spec.cwd) if self.spec.cwd else None,
            env=env,
            stdout=self._open_stream(self.spec.stdout_file),
            stderr=self._open_stream(self.spec.stderr_file),
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        try:
            self._write_pid_file(self.process.pid)
        except Exception:
            self._terminate_process()
            raise
        self.attempt += 1
        self._started_monotonic = time.monotonic()
        self._transition(
            JobState.VERIFYING,
            event_type="worker_started",
            metrics={"attempt": self.attempt, "pid": self.process.pid},
            action="start",
            outcome="spawned",
        )

    def start(self) -> bool:
        """Spawn the worker and enter VERIFYING; health is checked separately."""
        if self.state not in {JobState.QUEUED, JobState.RETRYING}:
            raise RuntimeError(f"cannot start worker from state {self.state.value}")
        self._transition(JobState.STARTING, action="start")
        try:
            if self._adopt_existing():
                return True
            self._spawn()
            return True
        except Exception as exc:  # pragma: no cover - platform-specific errors
            self._transition(JobState.FAILED, action="start", outcome="spawn_failed", error=str(exc))
            self._close_streams()
            return False

    def _close_streams(self) -> None:
        for handle in self._stream_handles:
            try:
                handle.close()
            except Exception:
                pass
        self._stream_handles.clear()

    def _terminate_process(self) -> None:
        process = self.process
        if process is None:
            self._clear_pid_file()
            self._close_streams()
            return
        pid = getattr(process, "pid", None)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        self._clear_pid_file(pid)
        self._close_streams()

    def _read_health(self) -> HealthSignals:
        process_alive = self.process is not None and self.process.poll() is None
        heartbeat_fresh = True
        heartbeat_age_s: float | None = None
        if self.spec.heartbeat_file is not None:
            try:
                heartbeat_age_s = max(0.0, time.time() - self.spec.heartbeat_file.stat().st_mtime)
                heartbeat_fresh = heartbeat_age_s <= self.spec.heartbeat_max_age_s
            except FileNotFoundError:
                heartbeat_fresh = False

        metrics: dict[str, Any] = {}
        resource_active = True
        if self.spec.resource_probe is not None:
            try:
                metrics = dict(self.spec.resource_probe())
                resource_active = (
                    bool(self.spec.resource_ok(metrics))
                    if self.spec.resource_ok is not None
                    else bool(metrics.get("active", False))
                )
            except Exception as exc:
                resource_active = False
                metrics = {"probe_error": type(exc).__name__}

        return HealthSignals(
            process_alive=process_alive,
            heartbeat_fresh=heartbeat_fresh,
            resource_active=resource_active,
            heartbeat_age_s=heartbeat_age_s,
            metrics=metrics,
        )

    @property
    def in_startup_grace(self) -> bool:
        return (
            self._started_monotonic is not None
            and time.monotonic() - self._started_monotonic < self.spec.startup_grace_s
        )

    def observe(self) -> HealthSignals:
        """Evaluate process, progress, and optional resource signals."""
        health = self._read_health()
        self.last_health = health
        if self.state in {JobState.COMPLETE, JobState.CANCELLED, JobState.ESCALATED}:
            return health

        if health.healthy:
            new_state = JobState.RUNNING
            outcome = "healthy"
        elif health.process_alive and self.in_startup_grace:
            new_state = JobState.VERIFYING
            outcome = "startup_grace"
        elif not health.process_alive:
            new_state = JobState.FAILED
            outcome = "worker_exit"
        else:
            new_state = JobState.STALLED
            outcome = "missing_progress_or_resource_signal"

        if new_state != self.state:
            self._transition(new_state, metrics=health.as_dict(), outcome=outcome)
        else:
            self._emit(
                event_type="health_check",
                previous_state=self.state,
                new_state=self.state,
                metrics=health.as_dict(),
                outcome=outcome,
            )
        return health

    def recover(self) -> bool:
        """Restart an unhealthy worker, or escalate after the retry budget."""
        health = self.observe()
        if health.healthy:
            return True
        if self.state not in {JobState.FAILED, JobState.STALLED}:
            return False
        if self.restart_count >= self.max_restarts:
            self._transition(
                JobState.ESCALATED,
                metrics=health.as_dict(),
                action="escalate",
                outcome="retry_limit_reached",
            )
            self._terminate_process()
            return False

        self._terminate_process()
        self.restart_count += 1
        self._transition(
            JobState.RETRYING,
            metrics={"restart_count": self.restart_count, "max_restarts": self.max_restarts},
            action="restart",
            outcome="retrying",
        )
        return self.start()

    def mark_complete(self) -> None:
        self._transition(JobState.COMPLETE, action="complete", outcome="completed")
        self._terminate_process()

    def cancel(self) -> None:
        if self.state not in {JobState.COMPLETE, JobState.CANCELLED}:
            self._transition(JobState.CANCELLED, action="cancel", outcome="cancelled")
        self._terminate_process()

    def snapshot(self) -> dict[str, Any]:
        health = self.last_health or self._read_health()
        return {
            "worker_id": self.spec.worker_id,
            "worker_type": self.spec.worker_type,
            "state": self.state.value,
            "attempt": self.attempt,
            "restart_count": self.restart_count,
            "pid": self.process.pid if self.process is not None and self.process.poll() is None else None,
            "health": health.as_dict(),
        }
