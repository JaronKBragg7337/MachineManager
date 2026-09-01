"""Bounded local verification handler for durable Machine Manager tasks."""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable

from .dispatcher import DispatchOutcome
from .scheduler import WorkItem
from .supervisor import utc_now


_TEST_COUNT = re.compile(r"\bRan\s+(\d+)\s+tests?\b", re.IGNORECASE)
_SAFE_TASK_ID = re.compile(r"[^A-Za-z0-9_.-]+")


class VerificationRetryableError(RuntimeError):
    """A verification attempt could not run and may be retried safely."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class RepositoryVerificationHandler:
    """Run the fixed repository unittest command without accepting shell input."""

    def __init__(
        self,
        root: Path,
        *,
        artifact_dir: Path,
        timeout_s: float = 120.0,
        runner: Runner = subprocess.run,
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError("verification root must be an existing directory")
        try:
            self.timeout_s = float(timeout_s)
        except (TypeError, ValueError) as error:
            raise ValueError("verification timeout_s must be a number") from error
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("verification timeout_s must be positive")
        self.artifact_dir = Path(artifact_dir)
        self.runner = runner

    @staticmethod
    def _tests_run(output: str) -> int:
        match = _TEST_COUNT.search(output)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _artifact_name(task_id: str) -> str:
        safe = _SAFE_TASK_ID.sub("_", task_id).strip("._")[:80]
        return safe or "verification-task"

    def __call__(self, item: WorkItem) -> DispatchOutcome:
        if item.kind != "verification":
            raise ValueError("verification handler received an unsupported task kind")
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"]
        started = time.monotonic()
        try:
            result = self.runner(
                command,
                cwd=str(self.root),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as error:
            raise VerificationRetryableError("verification timed out") from error
        except OSError as error:
            raise VerificationRetryableError("verification process could not start") from error

        duration_s = round(max(0.0, time.monotonic() - started), 3)
        stdout = str(result.stdout or "")
        stderr = str(result.stderr or "")
        tests_run = self._tests_run(f"{stdout}\n{stderr}")
        passed = result.returncode == 0 and tests_run > 0
        metrics: dict[str, int | float | bool] = {
            "checks_run": 1,
            "tests_run": tests_run,
            "passed": passed,
            "duration_s": duration_s,
        }
        artifact = {
            "version": 1,
            "task_id": item.task_id,
            "objective_id": item.objective_id,
            "completed_at": utc_now(),
            "check": "python -m unittest discover -s tests -q",
            "return_code": int(result.returncode),
            "tests_run": tests_run,
            "passed": passed,
            "duration_s": duration_s,
        }
        try:
            _atomic_json_write(
                self.artifact_dir / f"{self._artifact_name(item.task_id)}.json",
                artifact,
            )
        except OSError as error:
            raise VerificationRetryableError("verification evidence could not be saved") from error
        return DispatchOutcome(
            status="COMPLETE" if passed else "FAILED",
            metrics=metrics,
            public_message=(
                f"Repository verification {'passed' if passed else 'failed'} "
                f"{tests_run} test(s) in {duration_s:.1f}s."
            ),
        )
