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

from manager.supervisor import JobState, WorkerSpec, WorkerSupervisor, _expected_image_name
from manager.telemetry import TelemetryPublisher
from manager.run import (
    load_public_records,
    manager_from_config,
    merge_public_events,
    update_host_boot_marker,
)
from manager.agents import AgentCoordinator, AgentSpec, LocalOllamaAgent, parse_agent_response
from manager.autonomy import FIRST_CONTACT_DISCLOSURE, OperatingCharter, OutreachBlockedError, OutreachRegistry
from manager.evidence import AuditTarget, ConstraintAuditor, EvidenceCoordinator, WorkerProfile
from manager.public_upload import GitHubPagesPublisher, PublicUploadError
from manager.probes import keyhunt_progress_probe
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
    def test_expected_image_name_resolves_posix_symlink_target(self) -> None:
        with patch("manager.supervisor.os.name", "posix"):
            with patch("manager.supervisor.shutil.which", return_value="/opt/python/bin/python"):
                with patch(
                    "manager.supervisor.os.path.realpath",
                    return_value="/opt/python/bin/python3.12",
                ):
                    self.assertEqual(_expected_image_name(("python",)), "python3.12")

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

    def test_worker_progress_allowlists_aggregate_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            progress = directory / "progress.json"
            progress.write_text(
                json.dumps(
                    {
                        "keys_tested": 123456,
                        "keys_per_second": 789.5,
                        "coverage_pct": 12.5,
                        "private_key": "must never be carried forward",
                    }
                ),
                encoding="utf-8",
            )
            supervisor = WorkerSupervisor(
                replace(synthetic_spec(directory, "run"), progress_file=progress),
                objective_id="synthetic-progress",
                job_id="job-progress",
            )
            try:
                self.assertTrue(supervisor.start())
                deadline = time.time() + 3
                health = supervisor.observe()
                while not health.healthy and time.time() < deadline:
                    time.sleep(0.05)
                    health = supervisor.observe()
                snapshot = supervisor.snapshot()
                self.assertEqual(snapshot["progress"]["kind"], "reported_progress")
                self.assertEqual(snapshot["progress"]["keys_tested"], 123456)
                self.assertEqual(snapshot["progress"]["coverage_pct"], 12.5)
                self.assertNotIn("private_key", snapshot["progress"])
                self.assertEqual(supervisor.events[-1]["metrics"]["keys_tested"], 123456)
            finally:
                supervisor.cancel()

    def test_keyhunt_progress_probe_extracts_only_aggregate_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "keyhunt.stdout"
            output.write_text(
                "F: 0 GPU: 12.5 Mk/s C: 3.25 % R: 8 T: 1 234\n"
                "candidate material is intentionally not parsed\n",
                encoding="utf-8",
            )
            progress = keyhunt_progress_probe(output)
            self.assertEqual(progress["hashrate_mkey_s"], 12.5)
            self.assertEqual(progress["keys_per_second"], 12_500_000)
            self.assertEqual(progress["coverage_pct"], 3.25)
            self.assertEqual(progress["batch_number"], 8)
            self.assertEqual(progress["keys_tested"], 1234)
            self.assertEqual(progress["matches_found"], 0)
            self.assertEqual(set(progress), {"hashrate_mkey_s", "keys_per_second", "coverage_pct", "batch_number", "keys_tested", "matches_found"})

    def test_keyhunt_progress_probe_ignores_truncated_tail_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "keyhunt.stdout"
            output.write_text(
                "F: 0 GPU: 12.5 Mk/s C: 3.25 % R: 8 T: 123456\n"
                "F: 0 GPU: 12.5 Mk/s C: 3.25 % R: 9 T: 9,8",
                encoding="utf-8",
            )
            progress = keyhunt_progress_probe(output)
            self.assertEqual(progress["batch_number"], 8)
            self.assertEqual(progress["keys_tested"], 123456)

    def test_healthy_worker_can_be_adopted_after_manager_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            spec = replace(
                synthetic_spec(directory, "run", max_age=0.5),
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
                second_health = second.observe()
                deadline = time.time() + 3
                while not second_health.healthy and time.time() < deadline:
                    time.sleep(0.05)
                    second_health = second.observe()
                self.assertTrue(second_health.healthy, second_health.as_dict())
                self.assertTrue(any(event["event_type"] == "worker_adopted" for event in second.events))
            finally:
                second.cancel()
                first.cancel()

    def test_stale_pid_record_is_not_adopted_or_terminated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            spec = replace(
                synthetic_spec(directory, "run"),
                pid_file=directory / "worker.pid.json",
            )
            spec.pid_file.write_text(
                json.dumps({"pid": 987654, "worker_id": spec.worker_id}),
                encoding="ascii",
            )
            supervisor = WorkerSupervisor(
                spec,
                objective_id="synthetic-stale-pid",
                job_id="job-stale-pid",
            )
            with patch("manager.supervisor._pid_alive", return_value=True):
                with patch("manager.supervisor._process_image_name", return_value="unrelated.exe"):
                    try:
                        self.assertTrue(supervisor.start())
                        self.assertFalse(
                            any(event["event_type"] == "worker_adopted" for event in supervisor.events)
                        )
                        self.assertTrue(
                            any(event["event_type"] == "worker_started" for event in supervisor.events)
                        )
                    finally:
                        supervisor.cancel()


class TelemetryTests(unittest.TestCase):
    def test_worker_profile_requires_retest_when_model_version_changes(self) -> None:
        profile = WorkerProfile.from_mapping(
            {
                "id": "test-worker",
                "provider": "test",
                "model": "example-model",
                "model_version": "2026.09",
                "verified_model_version": "2026.08",
                "capabilities": [
                    {
                        "id": "repository-build",
                        "status": "TESTED_PASS",
                        "summary": "A bounded build test passed for the older version.",
                    }
                ],
            }
        )
        self.assertTrue(profile.retest_required)
        self.assertEqual(profile.local_record()["state"], "RETEST_REQUIRED")

    def test_constraint_audit_records_candidates_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "project"
            root.mkdir()
            source = root / "README.md"
            original = "Never publish automatically without approval.\nOnly allowed targets are listed below.\nDo not expose token=not-for-publication.\n"
            source.write_text(original, encoding="utf-8")
            target = AuditTarget.from_mapping(
                {"id": "test-project", "label": "Test project", "path": str(root)},
                base=root,
            )
            report = ConstraintAuditor(target).run()
            categories = report.category_counts()
            self.assertGreaterEqual(categories["autonomy_limit"], 1)
            self.assertGreaterEqual(categories["scope_boundary"], 1)
            self.assertGreaterEqual(categories["data_boundary"], 1)
            self.assertTrue(any(item.excerpt == "[redacted]" for item in report.findings))
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_constraint_audit_resumes_after_a_bounded_source_window(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "project"
            root.mkdir()
            for index in range(5):
                (root / f"rule-{index}.md").write_text(
                    "Never publish automatically without approval.\n",
                    encoding="utf-8",
                )
            target = AuditTarget.from_mapping(
                {"id": "windowed-project", "label": "Windowed project", "path": str(root), "max_files": 2},
                base=root,
            )
            auditor = ConstraintAuditor(target)
            first = auditor.run()
            second = auditor.run(start_after=first.next_cursor)
            third = auditor.run(start_after=second.next_cursor)
            first_paths = {item.relative_path for item in first.findings}
            second_paths = {item.relative_path for item in second.findings}
            third_paths = {item.relative_path for item in third.findings}
            self.assertTrue(first.more_pending)
            self.assertTrue(second.more_pending)
            self.assertFalse(third.more_pending)
            self.assertFalse(third.next_cursor)
            self.assertTrue(first_paths.isdisjoint(second_paths))
            self.assertTrue(first_paths.isdisjoint(third_paths))
            self.assertTrue(second_paths.isdisjoint(third_paths))
            self.assertEqual(len(first_paths | second_paths | third_paths), 5)

    def test_evidence_coordinator_persists_profiles_and_retest_events(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "state.sqlite3"
            base_config = {
                "worker_profiles": [
                    {
                        "id": "test-worker",
                        "provider": "test",
                        "model": "example-model",
                        "model_version": "v1",
                        "verified_model_version": "v1",
                        "capabilities": [],
                    }
                ]
            }
            changed_config = {
                "worker_profiles": [
                    {
                        "id": "test-worker",
                        "provider": "test",
                        "model": "example-model",
                        "model_version": "v2",
                        "verified_model_version": "v1",
                        "capabilities": [],
                    }
                ]
            }
            with StateStore(database) as store:
                initial = EvidenceCoordinator(
                    store,
                    base_config,
                    base=root,
                    actor="test-manager",
                    objective_id="objective-1",
                    job_id="job-1",
                )
                try:
                    first_events = initial.tick(now=0)
                finally:
                    initial.close()
                self.assertTrue(any(event["event_type"] == "worker_profile_recorded" for event in first_events))
                changed = EvidenceCoordinator(
                    store,
                    changed_config,
                    base=root,
                    actor="test-manager",
                    objective_id="objective-1",
                    job_id="job-1",
                )
                try:
                    changed_events = changed.tick(now=0)
                    profiles = changed.public_profiles()
                finally:
                    changed.close()
            self.assertTrue(any(event["event_type"] == "worker_profile_retest_due" for event in changed_events))
            self.assertTrue(profiles[0]["retest_required"])

    def test_operating_charter_defaults_to_execute_and_report(self) -> None:
        charter = OperatingCharter.from_mapping()
        summary = charter.public_summary()
        self.assertEqual(summary["mode"], "EXECUTE_AND_REPORT")
        self.assertTrue(summary["public_submissions"])
        self.assertTrue(summary["transparent_outreach"])
        self.assertTrue(summary["procurement_when_funded"])
        self.assertEqual(summary["handoff_style"], "service-required")

    def test_outreach_is_disclosed_once_and_opt_out_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "state.sqlite3"
            with StateStore(database) as store:
                registry = OutreachRegistry(store, OperatingCharter.from_mapping())
                draft = registry.prepare(
                    channel="email",
                    recipient="person@example.com",
                    message="I have a proposal that may help your project.",
                )
                self.assertTrue(draft.first_contact)
                self.assertTrue(draft.content.startswith(FIRST_CONTACT_DISCLOSURE))
                registry.record_sent(draft, timestamp="2026-08-30T00:00:00Z")

                follow_up = registry.prepare(
                    channel="email",
                    recipient="person@example.com",
                    message="Following up with the completed work.",
                )
                self.assertFalse(follow_up.first_contact)
                self.assertEqual(follow_up.content, "Following up with the completed work.")

                registry.record_opt_out(
                    channel="email",
                    recipient="person@example.com",
                    timestamp="2026-08-30T00:01:00Z",
                )
                with self.assertRaises(OutreachBlockedError):
                    registry.prepare(
                        channel="email",
                        recipient="person@example.com",
                        message="This must never be sent.",
                    )
                record = store.get_outreach_contact(
                    channel="email",
                    recipient_hash=registry.recipient_hash("email", "person@example.com"),
                )
                self.assertEqual(record["state"], "DO_NOT_CONTACT")
                self.assertNotIn("person@example.com", json.dumps(record))

    def test_host_boot_marker_only_reports_a_changed_boot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "state.sqlite3"
            with StateStore(database) as store:
                self.assertFalse(update_host_boot_marker(store, marker="windows:100"))
                self.assertFalse(update_host_boot_marker(store, marker="windows:100"))
                self.assertTrue(update_host_boot_marker(store, marker="windows:101"))

    def test_host_boot_resume_keeps_attempt_history_but_clears_retry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            database = directory / "state.sqlite3"
            config = {
                "job_id": "job-recovery",
                "objective_id": "objective-recovery",
                "max_restarts": 3,
                "worker": {
                    "id": "worker-recovery",
                    "type": "SyntheticWorker",
                    "command": [sys.executable, "-c", "pass"],
                },
            }
            with StateStore(database) as store:
                store.upsert_job(
                    {
                        "job_id": "job-recovery",
                        "objective_id": "objective-recovery",
                        "state": "ESCALATED",
                        "attempt": 7,
                        "restart_count": 3,
                        "updated": "2026-08-30T00:00:00Z",
                    }
                )
                preserved, _, _ = manager_from_config(
                    config,
                    config_path=directory / "manager.json",
                    state_store=store,
                )
                resumed, _, _ = manager_from_config(
                    config,
                    config_path=directory / "manager.json",
                    state_store=store,
                    reset_retry_budget=True,
                )
            preserved_snapshot = preserved.jobs["job-recovery"].supervisor.snapshot()
            resumed_snapshot = resumed.jobs["job-recovery"].supervisor.snapshot()
            self.assertEqual(preserved_snapshot["attempt"], 7)
            self.assertEqual(preserved_snapshot["restart_count"], 3)
            self.assertEqual(resumed_snapshot["attempt"], 7)
            self.assertEqual(resumed_snapshot["restart_count"], 0)

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

    def test_ollama_agent_requests_bounded_nonthinking_json(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps({"response": '{"action":"continue","reason":"healthy"}'}).encode("utf-8")

        spec = AgentSpec.from_mapping(
            {
                "id": "test-agent",
                "role": "test",
                "provider": "ollama",
                "model": "test-model",
                "base_url": "http://127.0.0.1:11434",
                "enabled": True,
            }
        )
        agent = LocalOllamaAgent(spec)
        snapshot = {"status": "HEALTHY", "objective": "synthetic", "workers": [], "jobs": []}
        with patch("manager.agents.urllib.request.urlopen", return_value=FakeResponse()) as opener:
            decision = agent.ask(snapshot, [])
        request = opener.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(decision.action, "continue")
        self.assertFalse(decision.fallback)
        self.assertEqual(body["format"], "json")
        self.assertFalse(body["think"])
        self.assertEqual(body["options"]["num_predict"], 80)

    def test_ollama_agent_preserves_allowed_loopback_url(self) -> None:
        spec = AgentSpec.from_mapping(
            {
                "id": "test-agent",
                "provider": "ollama",
                "model": "test-model",
                "base_url": "http://127.0.0.1:11434",
            }
        )
        self.assertEqual(spec.base_url, "http://127.0.0.1:11434")

    def test_async_agent_duration_excludes_manager_poll_wait(self) -> None:
        coordinator = AgentCoordinator(
            [
                {
                    "id": "test-agent",
                    "role": "test",
                    "provider": "ollama",
                    "model": "test-model",
                    "enabled": True,
                    "interval_s": 60,
                }
            ]
        )

        def slow_but_successful_ask(*_args):
            time.sleep(0.03)
            return parse_agent_response('{"action":"continue","reason":"healthy"}')

        snapshot = {"status": "HEALTHY", "objective": "synthetic", "workers": [], "jobs": []}
        try:
            with patch.object(LocalOllamaAgent, "ask", side_effect=slow_but_successful_ask):
                self.assertEqual(coordinator.tick(snapshot), [])
                time.sleep(0.2)
                decisions = coordinator.tick(snapshot)
            self.assertEqual(decisions[0].action, "continue")
            self.assertLess(coordinator.snapshot()[0]["last_duration_s"], 0.15)
        finally:
            coordinator.close()

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
        self.assertEqual(coordinator.snapshot()[0]["last_reason"], "deterministic agent heartbeat")
        self.assertEqual(coordinator.events[0]["message"], "deterministic agent heartbeat")
        self.assertEqual(coordinator.events[0]["metrics"]["duration_s"], 0.0)
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
                    "workers": [{"id": "w-1", "type": "SyntheticWorker", "state": "RUNNING", "pid": 1234, "progress": {"kind": "reported_progress", "sample_count": 4, "keys_tested": 99, "private_key": "do not publish"}}],
                    "jobs": [{"id": "job-1", "objective_id": "obj-1", "state": "RUNNING", "command": "do not publish"}],
                    "agents": [{"id": "agent-1", "provider": "test", "model": "safe", "state": "READY", "last_reason": "healthy", "last_duration_s": 0.4}],
                    "worker_profiles": [{"id": "profile-1", "provider": "test", "model": "safe", "model_version": "v1", "state": "READY", "retest_required": False, "private_path": "C:\\Users\\lilli\\private", "capabilities": [{"id": "safe-capability", "status": "TESTED_PASS", "summary": "token=not-for-publication", "private_note": "do not publish"}]}],
                    "constraint_audits": [{"id": "audit-1", "label": "Safe audit", "state": "NEEDS_EVIDENCE_REVIEW", "files_scanned": 10, "candidate_count": 2, "more_pending": True, "categories": {"approval_gate": 2, "unsafe": 99}, "path": "C:\\Users\\lilli\\private", "findings": [{"excerpt": "do not publish"}]}],
                    "autonomy": {"mode": "EXECUTE_AND_REPORT", "developer_tools": True, "private_note": "do not publish"},
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
                        "metrics": {"util_pct": 80, "keys_tested": 99, "pid": 9999},
                    }
                ],
            )
            latest = json.loads((dashboard / "data/latest.json").read_text(encoding="utf-8"))
            events = json.loads((dashboard / "data/events.json").read_text(encoding="utf-8"))
            encoded = (dashboard / "data/latest.json").read_text(encoding="utf-8")
            self.assertNotIn("private_key", encoded)
            self.assertNotIn("command", encoded)
            self.assertNotIn("pid", encoded)
            self.assertEqual(latest["workers"][0]["progress"]["keys_tested"], 99)
            self.assertNotIn("private_key", json.dumps(latest))
            self.assertEqual(latest["agents"][0]["last_reason"], "healthy")
            self.assertEqual(latest["autonomy"]["mode"], "EXECUTE_AND_REPORT")
            self.assertTrue(latest["autonomy"]["developer_tools"])
            self.assertNotIn("private_note", json.dumps(latest))
            self.assertNotIn("private_path", json.dumps(latest))
            self.assertNotIn("findings", json.dumps(latest))
            self.assertNotIn("C:\\", json.dumps(latest))
            self.assertEqual(latest["worker_profiles"][0]["capabilities"][0]["summary"], "[redacted]")
            self.assertEqual(latest["constraint_audits"][0]["categories"], {"approval_gate": 2})
            self.assertTrue(latest["constraint_audits"][0]["more_pending"])
            self.assertNotIn("C:\\", json.dumps(events))
            self.assertNotIn("pid", json.dumps(events))
            self.assertEqual(events[0]["metrics"]["keys_tested"], 99)
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
