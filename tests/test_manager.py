from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import replace
import unittest
from unittest.mock import patch
from pathlib import Path

from manager.supervisor import JobState, WorkerSpec, WorkerSupervisor
from manager.telemetry import TelemetryPublisher
from manager.run import load_public_records, merge_public_events
from manager.agents import AgentCoordinator, parse_agent_response
from manager.public_upload import GitHubPagesPublisher, PublicUploadError
from manager.state_store import StateStore


REPO_ROOT = Path(__file__).resolve().parents[1]


def synthetic_spec(directory: Path, mode: str, *, max_age: float = 0.12) -> WorkerSpec:
    heartbeat = directory / f"{mode}.heartbeat.json"
    command = (
        sys.executable,
        "-m",
        "manager.synthetic_worker",
        "--mode",
        mode,
        "--heartbeat-file",
        str(heartbeat),
        "--interval",
        "0.03",
    )
    return WorkerSpec(
        worker_id=f"synthetic-{mode}",
        worker_type="SyntheticWorker",
        command=command,
        cwd=REPO_ROOT,
        heartbeat_file=heartbeat,
        heartbeat_max_age_s=max_age,
        startup_grace_s=0,
    )


class SupervisorTests(unittest.TestCase):
    def test_healthy_worker_requires_multiple_signals(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            supervisor = WorkerSupervisor(
                synthetic_spec(directory, "run"),
                objective_id="synthetic-healthy",
                job_id="job-healthy",
            )
            try:
                self.assertTrue(supervisor.start())
                deadline = time.time() + 3
                health = supervisor.observe()
                while not health.healthy and time.time() < deadline:
                    time.sleep(0.05)
                    health = supervisor.observe()
                self.assertTrue(health.healthy, health.as_dict())
                self.assertEqual(supervisor.state, JobState.RUNNING)
            finally:
                supervisor.cancel()

    def test_live_but_stalled_worker_is_not_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            supervisor = WorkerSupervisor(
                synthetic_spec(directory, "stall", max_age=0.08),
                objective_id="synthetic-stall",
                job_id="job-stall",
                max_restarts=0,
            )
            try:
                self.assertTrue(supervisor.start())
                heartbeat = directory / "stall.heartbeat.json"
                deadline = time.time() + 3
                while not heartbeat.exists() and time.time() < deadline:
                    time.sleep(0.03)
                self.assertTrue(heartbeat.exists())
                time.sleep(0.15)
                health = supervisor.observe()
                self.assertTrue(health.process_alive)
                self.assertFalse(health.heartbeat_fresh)
                self.assertFalse(health.healthy)
                self.assertEqual(supervisor.state, JobState.STALLED)
            finally:
                supervisor.cancel()

    def test_process_and_heartbeat_are_not_enough_without_resource_signal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            spec = synthetic_spec(directory, "run")
            spec = replace(
                spec,
                resource_probe=lambda: {"util_pct": 0, "power_w": 3},
                resource_ok=lambda metrics: float(metrics.get("util_pct", 0)) >= 15,
            )
            supervisor = WorkerSupervisor(
                spec,
                objective_id="synthetic-resource",
                job_id="job-resource",
                max_restarts=0,
            )
            try:
                self.assertTrue(supervisor.start())
                heartbeat = directory / "run.heartbeat.json"
                deadline = time.time() + 3
                while not heartbeat.exists() and time.time() < deadline:
                    time.sleep(0.03)
                health = supervisor.observe()
                self.assertTrue(health.process_alive)
                self.assertTrue(health.heartbeat_fresh)
                self.assertFalse(health.resource_active)
                self.assertFalse(health.healthy)
                self.assertEqual(supervisor.state, JobState.STALLED)
            finally:
                supervisor.cancel()

    def test_repeated_crash_escalates_after_retry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            supervisor = WorkerSupervisor(
                synthetic_spec(directory, "crash"),
                objective_id="synthetic-crash",
                job_id="job-crash",
                max_restarts=1,
            )
            try:
                self.assertTrue(supervisor.start())
                time.sleep(0.15)
                self.assertFalse(supervisor.observe().healthy)
                self.assertTrue(supervisor.recover())
                time.sleep(0.15)
                self.assertFalse(supervisor.observe().healthy)
                self.assertFalse(supervisor.recover())
                self.assertEqual(supervisor.state, JobState.ESCALATED)
                self.assertEqual(supervisor.restart_count, 1)
            finally:
                supervisor.cancel()

    def test_event_contract_has_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            supervisor = WorkerSupervisor(
                synthetic_spec(directory, "run"),
                objective_id="synthetic-events",
                job_id="job-events",
            )
            try:
                self.assertTrue(supervisor.start())
                time.sleep(0.1)
                supervisor.observe()
                required = {
                    "timestamp",
                    "event_id",
                    "objective_id",
                    "job_id",
                    "worker_id",
                    "actor",
                    "event_type",
                    "previous_state",
                    "new_state",
                    "metrics",
                    "action",
                    "outcome",
                    "artifact_refs",
                    "error",
                    "duration",
                }
                self.assertTrue(required.issubset(supervisor.events[-1]))
            finally:
                supervisor.cancel()

    def test_healthy_worker_can_be_adopted_after_manager_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            spec = replace(
                synthetic_spec(directory, "run"),
                pid_file=directory / "worker.pid.json",
                startup_grace_s=0,
            )
            first = WorkerSupervisor(
                spec,
                objective_id="synthetic-adoption",
                job_id="job-adoption",
            )
            second = WorkerSupervisor(
                spec,
                objective_id="synthetic-adoption",
                job_id="job-adoption",
            )
            try:
                self.assertTrue(first.start())
                deadline = time.time() + 3
                health = first.observe()
                while not health.healthy and time.time() < deadline:
                    time.sleep(0.05)
                    health = first.observe()
                self.assertTrue(health.healthy, health.as_dict())
                self.assertTrue(second.start())
                self.assertTrue(second.observe().healthy)
                self.assertTrue(any(event["event_type"] == "worker_adopted" for event in second.events))
            finally:
                second.cancel()
                first.cancel()


class TelemetryTests(unittest.TestCase):
    def test_state_store_persists_events_jobs_and_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "state.sqlite3"
            event = {
                "timestamp": "2026-08-28T20:00:00Z",
                "event_id": "evt-persisted",
                "job_id": "job-1",
                "event_type": "state_change",
                "new_state": "RUNNING",
            }
            with StateStore(database) as store:
                store.append_event(event)
                store.upsert_job(
                    {
                        "job_id": "job-1",
                        "objective_id": "obj-1",
                        "state": "RUNNING",
                        "attempt": 2,
                        "restart_count": 1,
                        "updated": event["timestamp"],
                    }
                )
                store.enqueue_task(
                    task_id="task-1",
                    kind="research",
                    objective_id="obj-1",
                    payload={"safe": True},
                )
                claimed = store.claim_due_tasks()
                self.assertEqual(claimed[0]["task_id"], "task-1")
                retry_at = time.time() + 60
                store.finish_task("task-1", status="QUEUED", scheduled_at=retry_at)
                self.assertEqual(store.claim_due_tasks(now=time.time()), [])
                store.finish_task("task-1")
            with StateStore(database) as reopened:
                self.assertEqual(reopened.event_count(), 1)
                self.assertEqual(reopened.recent_events()[0]["event_id"], "evt-persisted")
                self.assertEqual(reopened.get_job("job-1")["restart_count"], 1)
                self.assertEqual(reopened.task_counts()["COMPLETE"], 1)

    def test_agent_response_falls_back_safely(self) -> None:
        self.assertTrue(parse_agent_response("").fallback)
        self.assertTrue(parse_agent_response("not json").fallback)
        decision = parse_agent_response('{"action":"continue","reason":"healthy"}')
        self.assertEqual(decision.action, "continue")
        self.assertFalse(decision.fallback)

    def test_noop_agent_records_bounded_work(self) -> None:
        coordinator = AgentCoordinator(
            [
                {
                    "id": "test-agent",
                    "role": "test",
                    "provider": "test",
                    "enabled": True,
                    "interval_s": 60,
                }
            ]
        )
        with patch("manager.agents.time.monotonic", return_value=1.0):
            decisions = coordinator.tick(
                {
                    "status": "HEALTHY",
                    "objective": "synthetic",
                    "workers": [],
                    "jobs": [],
                }
            )
        self.assertEqual(decisions[0].action, "continue")
        self.assertEqual(coordinator.snapshot()[0]["tasks_completed"], 1)
        self.assertEqual(coordinator.events[0]["event_type"], "agent_decision")

    def test_public_upload_rejects_pid_before_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dashboard = Path(raw) / "dashboard"
            data = dashboard / "data"
            data.mkdir(parents=True)
            (data / "latest.json").write_text(
                '{"status":"HEALTHY","pid":1234}',
                encoding="utf-8",
            )
            (data / "events.json").write_text("[]", encoding="utf-8")
            (data / "scenarios.json").write_text("[]", encoding="utf-8")
            publisher = GitHubPagesPublisher(
                dashboard,
                owner="owner",
                repository="repo",
            )
            with self.assertRaises(PublicUploadError):
                publisher.publish()

    def test_public_upload_builds_one_attributed_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dashboard = Path(raw) / "dashboard"
            data = dashboard / "data"
            data.mkdir(parents=True)
            (data / "latest.json").write_text(
                '{"status":"HEALTHY","updated":"2026-08-29T00:00:00Z"}',
                encoding="utf-8",
            )
            (data / "events.json").write_text("[]", encoding="utf-8")
            (data / "scenarios.json").write_text("[]", encoding="utf-8")
            publisher = GitHubPagesPublisher(
                dashboard,
                owner="owner",
                repository="repo",
            )
            responses = iter(
                [
                    {"object": {"sha": "base-commit"}},
                    {"tree": {"sha": "base-tree"}},
                    {"sha": "blob-1"},
                    {"sha": "blob-2"},
                    {"sha": "blob-3"},
                    {"sha": "new-tree"},
                    {"sha": "new-commit"},
                    {"ref": "refs/heads/main"},
                ]
            )
            with patch.dict(os.environ, {"MACHINE_MANAGER_GITHUB_TOKEN": "test-only"}):
                with patch.object(publisher, "_request", side_effect=lambda *args, **kwargs: next(responses)) as request:
                    self.assertTrue(publisher.publish(force=True))
                    calls = request.call_args_list
            commit_payload = calls[-2].args[2]
            self.assertIn("Co-Authored-By: Codex <noreply@openai.com>", commit_payload["message"])

    def test_runner_reads_array_and_object_public_records(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dashboard = Path(raw) / "dashboard"
            data_dir = dashboard / "data"
            data_dir.mkdir(parents=True)
            (data_dir / "events.json").write_text("[]", encoding="utf-8")
            (data_dir / "scenarios.json").write_text(
                json.dumps({"scenarios": [{"id": "scenario-1"}]}), encoding="utf-8"
            )
            events, scenarios = load_public_records(dashboard)
            self.assertEqual(events, [])
            self.assertEqual(scenarios, [{"id": "scenario-1"}])

    def test_runner_bounds_and_deduplicates_public_events(self) -> None:
        existing = [{"event_id": "old-1"}, {"event_id": "old-2"}]
        current = [
            {"event_id": "old-2"},
            {"event_id": "new-1"},
            {"event_id": "new-2"},
        ]
        merged = merge_public_events(existing, current, limit=2)
        self.assertEqual([event["event_id"] for event in merged], ["new-1", "new-2"])

    def test_publisher_allowlists_public_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dashboard = Path(raw) / "dashboard"
            TelemetryPublisher(dashboard).publish(
                {
                    "manager_version": "0.2",
                    "status": "HEALTHY",
                    "objective": "synthetic reliability test",
                    "workers": [{"id": "w-1", "type": "SyntheticWorker", "state": "RUNNING", "pid": 1234}],
                    "jobs": [{"id": "job-1", "objective_id": "obj-1", "state": "RUNNING", "command": "do not publish"}],
                    "gpu": {"util_pct": 80, "power_w": 70, "private_key": "do not publish"},
                    "updated": "2026-08-28T20:00:00Z",
                },
                events=[
                    {
                        "timestamp": "2026-08-28T20:00:00Z",
                        "event_id": "evt-1",
                        "event_type": "state_change",
                        "new_state": "RUNNING",
                        "action": "start",
                        "error": "C:\\Users\\lilli\\secret-token.txt",
                        "metrics": {"util_pct": 80, "pid": 9999},
                    }
                ],
            )
            latest = json.loads((dashboard / "data/latest.json").read_text(encoding="utf-8"))
            events = json.loads((dashboard / "data/events.json").read_text(encoding="utf-8"))
            encoded = (dashboard / "data/latest.json").read_text(encoding="utf-8")
            self.assertNotIn("private_key", encoded)
            self.assertNotIn("command", encoded)
            self.assertNotIn("pid", encoded)
            self.assertNotIn("C:\\", json.dumps(events))
            self.assertNotIn("pid", json.dumps(events))
            self.assertFalse(events[0]["error"] is False)
            self.assertEqual(latest["workers"][0]["state"], "RUNNING")

    def test_publisher_preserves_legacy_event_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dashboard = Path(raw) / "dashboard"
            TelemetryPublisher(dashboard).publish(
                {"status": "HEALTHY", "workers": [], "jobs": [], "gpu": {}},
                events=[
                    {
                        "ts": "2026-08-28T20:00:00Z",
                        "event_id": "legacy-1",
                        "actor": "reference",
                        "type": "recovery",
                        "state": "RECOVERED",
                        "message": "worker recovered",
                    }
                ],
            )
            events = json.loads((dashboard / "data/events.json").read_text(encoding="utf-8"))
            self.assertEqual(events[0]["type"], "recovery")
            self.assertEqual(events[0]["state"], "RECOVERED")
            self.assertEqual(events[0]["message"], "worker recovered")


if __name__ == "__main__":
    unittest.main()
