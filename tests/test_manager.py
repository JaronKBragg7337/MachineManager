from __future__ import annotations

import json
import os
from types import SimpleNamespace
import sys
import tempfile
import time
from dataclasses import replace
import unittest
from unittest.mock import patch
from pathlib import Path

from manager.supervisor import JobState, WorkerSpec, WorkerSupervisor, _expected_image_name
from manager.telemetry import TelemetryPublisher, TelemetryWriteError
from manager.run import (
    _pending_operator_resume,
    _publish_local_snapshot,
    _public_state_marker,
    load_public_records,
    manager_from_config,
    merge_public_events,
    update_host_boot_marker,
)
from manager.agents import AgentCoordinator, AgentSpec, LocalOllamaAgent, parse_agent_response
from manager.autonomy import FIRST_CONTACT_DISCLOSURE, OperatingCharter, OutreachBlockedError, OutreachRegistry
from manager.capabilities import CapabilityRegistry
from manager.evidence import AuditTarget, ConstraintAuditor, EvidenceCoordinator, WorkerProfile
from manager.public_upload import GitHubPagesPublisher, PublicUploadError
from manager.probes import CpuUsageProbe, gpu_resource_ok, keyhunt_progress_probe
from manager.state_store import StateStore
from manager import DispatchOutcome, MachineManager, WorkDispatcher, WorkScheduler


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


class CapabilityRegistryTests(unittest.TestCase):
    def test_handler_capabilities_reflect_runtime_registration(self) -> None:
        registry = CapabilityRegistry.default(
            queue_dispatch_enabled=True,
            research_enabled=True,
            verification_enabled=True,
            revenue_enabled=False,
        )
        capabilities = {item["id"]: item for item in registry.snapshot()}
        self.assertTrue(capabilities["durable-queue-dispatch"]["enabled"])
        self.assertTrue(capabilities["public-research-workers"]["enabled"])
        self.assertTrue(capabilities["repository-verification"]["enabled"])
        self.assertFalse(capabilities["revenue-discovery"]["enabled"])


class SupervisorTests(unittest.TestCase):
    def test_cpu_usage_probe_reports_delta_without_inventing_a_first_sample(self) -> None:
        probe = CpuUsageProbe()
        probe._previous = None
        with patch.object(probe, "_read_times", return_value=(100, 200)):
            self.assertEqual(probe(), {})
        probe._previous = (100, 1000)
        with patch.object(probe, "_read_times", return_value=(120, 1200)):
            self.assertEqual(probe(), {"cpu_pct": 90.0})

    def test_gpu_resource_probe_tolerates_transient_zero_utilization(self) -> None:
        self.assertTrue(
            gpu_resource_ok(
                {"util_pct": 0, "power_w": 78, "mem_used_mib": 2367}
            )
        )
        self.assertTrue(
            gpu_resource_ok(
                {"util_pct": 13, "power_w": 78, "mem_used_mib": 2367}
            )
        )
        self.assertFalse(
            gpu_resource_ok(
                {"util_pct": 0, "power_w": 35, "mem_used_mib": 2367}
            )
        )
        self.assertFalse(
            gpu_resource_ok(
                {"util_pct": 0, "power_w": 590, "mem_used_mib": 0}
            )
        )

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

    def test_stable_healthy_run_resets_old_retry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            supervisor = WorkerSupervisor(
                synthetic_spec(directory, "run"),
                objective_id="synthetic-stable-reset",
                job_id="job-stable-reset",
                initial_restart_count=2,
                retry_reset_after_s=0.05,
            )
            try:
                self.assertTrue(supervisor.start())
                deadline = time.time() + 3
                health = supervisor.observe()
                while not health.healthy and time.time() < deadline:
                    time.sleep(0.05)
                    health = supervisor.observe()
                self.assertTrue(health.healthy, health.as_dict())
                supervisor._started_monotonic = time.monotonic() - 0.1
                supervisor.observe()
                self.assertEqual(supervisor.restart_count, 0)
                self.assertTrue(
                    any(event["event_type"] == "retry_budget_reset" for event in supervisor.events)
                )
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

    def test_objective_change_queues_without_mutating_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            with StateStore(directory / "state.sqlite3") as store:
                scheduler = WorkScheduler(store)
                manager = MachineManager(actor="test-manager")
                job = manager.register_job(
                    synthetic_spec(directory, "run", max_age=0.5),
                    objective_id="objective-current",
                    job_id="job-objective-change",
                    max_restarts=1,
                )
                try:
                    self.assertTrue(manager.start_job(job.job_id))
                    deadline = time.time() + 3
                    health = job.supervisor.observe()
                    while not health.healthy and time.time() < deadline:
                        time.sleep(0.05)
                        health = job.supervisor.observe()
                    self.assertTrue(health.healthy, health.as_dict())
                    process_before = job.supervisor.process

                    task_id = manager.queue_objective_change(
                        job.job_id,
                        scheduler=scheduler,
                        new_objective_id="objective-next",
                        task_id="task-objective-change",
                    )
                    snapshot = manager.snapshot(objective="Current objective")
                    claimed = scheduler.claim(limit=1)

                    self.assertEqual(task_id, "task-objective-change")
                    self.assertEqual(snapshot["status"], "HEALTHY")
                    self.assertEqual(snapshot["jobs"][0]["objective_id"], "objective-current")
                    self.assertEqual(snapshot["workers"][0]["state"], "RUNNING")
                    self.assertIs(job.supervisor.process, process_before)
                    self.assertEqual(len(claimed), 1)
                    self.assertEqual(claimed[0].kind, "objective_change")
                    self.assertEqual(claimed[0].objective_id, "objective-next")
                    event = [
                        item
                        for item in manager.events
                        if item["event_type"] == "objective_change_queued"
                    ][-1]
                    self.assertEqual(event["outcome"], "active_job_preserved")
                    self.assertEqual(event["new_state"], "RUNNING")
                finally:
                    manager.cancel_all()


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

    def test_constraint_audit_skips_local_secret_and_environment_sources(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "project"
            root.mkdir()
            (root / "README.md").write_text("A normal public project note.\n", encoding="utf-8")
            (root / ".env").write_text("TOKEN=synthetic-placeholder\n", encoding="utf-8")
            secrets = root / "secrets"
            secrets.mkdir()
            (secrets / "local.txt").write_text("Do not expose token=synthetic-placeholder.\n", encoding="utf-8")
            logs = root / "logs"
            logs.mkdir()
            (logs / "manager.log").write_text("Do not expose token=synthetic-placeholder.\n", encoding="utf-8")
            target = AuditTarget.from_mapping(
                {"id": "safe-source-set", "label": "Safe source set", "path": str(root)},
                base=root,
            )

            report = ConstraintAuditor(target).run()

            self.assertEqual(report.files_scanned, 1)
            self.assertEqual(report.findings, ())

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
            self.assertTrue(first.scan_signature)
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

    def test_operator_resume_resets_only_the_acknowledged_escalated_job_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            database = directory / "state.sqlite3"
            config = {
                "jobs": [
                    {
                        "job_id": "job-a",
                        "objective_id": "objective-a",
                        "max_restarts": 3,
                        "worker": {"id": "worker-a", "command": [sys.executable, "-c", "pass"]},
                    },
                    {
                        "job_id": "job-b",
                        "objective_id": "objective-b",
                        "max_restarts": 3,
                        "worker": {"id": "worker-b", "command": [sys.executable, "-c", "pass"]},
                    },
                ],
                "operator_resume": {"id": "resume-test-001", "job_id": "job-a"},
            }
            with StateStore(database) as store:
                for job_id in ("job-a", "job-b"):
                    store.upsert_job(
                        {
                            "job_id": job_id,
                            "objective_id": f"objective-{job_id[-1]}",
                            "state": "ESCALATED",
                            "attempt": 7,
                            "restart_count": 3,
                            "updated": "2026-08-30T00:00:00Z",
                        }
                    )
                request_id = _pending_operator_resume(
                    config,
                    store,
                    job_id="job-a",
                    prior_state="ESCALATED",
                )
                resumed, _, _ = manager_from_config(
                    config,
                    config_path=directory / "manager.json",
                    state_store=store,
                    reset_retry_budget_for={"job-a"},
                )
                store.set_meta(f"operator_resume:{request_id}", "applied")
                consumed = _pending_operator_resume(
                    config,
                    store,
                    job_id="job-a",
                    prior_state="ESCALATED",
                )
            self.assertEqual(request_id, "resume-test-001")
            self.assertEqual(resumed.jobs["job-a"].supervisor.snapshot()["restart_count"], 0)
            self.assertEqual(resumed.jobs["job-b"].supervisor.snapshot()["restart_count"], 3)
            self.assertIsNone(consumed)

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

    def test_scheduler_tracks_and_recovers_interrupted_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with StateStore(Path(raw) / "state.sqlite3") as store:
                scheduler = WorkScheduler(store)
                task_id = scheduler.enqueue(
                    kind="agent_review",
                    objective_id="obj-1",
                    payload={"agent_id": "agent-1"},
                    task_id="task-agent-1",
                )
                self.assertEqual(task_id, "task-agent-1")
                self.assertEqual(scheduler.snapshot(), {"QUEUED": 1})
                self.assertEqual(scheduler.kind_snapshot(), {"agent_review": 1})
                self.assertTrue(scheduler.start(task_id))
                self.assertFalse(scheduler.start(task_id))
                self.assertEqual(scheduler.snapshot(), {"RUNNING": 1})
                self.assertEqual(scheduler.recover_interrupted(), 1)
                self.assertEqual(scheduler.snapshot(), {"QUEUED": 1})
                claimed = scheduler.claim(limit=1)
                self.assertEqual(claimed[0].task_id, task_id)
                self.assertEqual(claimed[0].attempts, 2)
                scheduler.complete(task_id)
                self.assertEqual(scheduler.snapshot(), {"COMPLETE": 1})

    def test_scheduler_activity_excludes_private_task_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with StateStore(Path(raw) / "state.sqlite3") as store:
                scheduler = WorkScheduler(store)
                scheduler.enqueue(
                    kind="research",
                    objective_id="research-objective",
                    payload={"private_note": "not for publication"},
                    task_id="task-research-1",
                )
                activity = scheduler.activity_snapshot(limit=5)
                self.assertEqual(activity[0]["task_id"], "task-research-1")
                self.assertEqual(activity[0]["status"], "QUEUED")
                self.assertNotIn("payload", activity[0])
                self.assertIsInstance(activity[0]["updated_at"], float)

    def test_dispatcher_leaves_coordinator_owned_agent_reviews_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with StateStore(Path(raw) / "state.sqlite3") as store:
                scheduler = WorkScheduler(store)
                scheduler.enqueue(
                    kind="agent_review",
                    objective_id="objective-1",
                    payload={"agent_id": "agent-1"},
                    task_id="task-agent-1",
                    scheduled_at=100,
                )
                dispatcher = WorkDispatcher(
                    scheduler,
                    {},
                    reserved_kinds={"agent_review"},
                )

                self.assertEqual(dispatcher.dispatch(now=100), [])
                self.assertEqual(scheduler.snapshot(), {"QUEUED": 1})
                self.assertEqual(
                    scheduler.find_queued_task(
                        kind="agent_review",
                        payload_key="agent_id",
                        payload_value="agent-1",
                    ),
                    "task-agent-1",
                )

    def test_work_dispatcher_completes_defers_and_escalates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with StateStore(Path(raw) / "state.sqlite3") as store:
                scheduler = WorkScheduler(store)
                scheduler.enqueue(
                    kind="research",
                    objective_id="research-objective",
                    task_id="task-research-1",
                    scheduled_at=100,
                )
                scheduler.enqueue(
                    kind="objective_change",
                    objective_id="next-objective",
                    task_id="task-handoff-1",
                    scheduled_at=100,
                )

                def failing_handler(_item):
                    raise RuntimeError("handler failed")

                dispatcher = WorkDispatcher(
                    scheduler,
                    {"research": lambda _item: DispatchOutcome()},
                    defer_delay_s=10,
                    max_attempts=2,
                )
                first = dispatcher.dispatch(now=100)
                self.assertEqual(
                    [(result.task_id, result.status) for result in first],
                    [("task-handoff-1", "DEFERRED"), ("task-research-1", "COMPLETE")],
                )
                self.assertEqual(scheduler.snapshot(), {"COMPLETE": 1, "QUEUED": 1})

                scheduler.enqueue(
                    kind="build",
                    objective_id="build-objective",
                    task_id="task-build-1",
                    scheduled_at=200,
                )
                dispatcher.handlers["build"] = failing_handler
                dispatcher.handlers["objective_change"] = lambda _item: DispatchOutcome()
                retry = dispatcher.dispatch(now=200)
                build_retry = next(result for result in retry if result.task_id == "task-build-1")
                self.assertEqual(build_retry.status, "RETRY")
                escalated = dispatcher.dispatch(now=211)
                build_escalated = next(result for result in escalated if result.task_id == "task-build-1")
                self.assertEqual(build_escalated.status, "ESCALATED")
                self.assertEqual(scheduler.snapshot()["ESCALATED"], 1)
                event_types = [event["event_type"] for event in store.recent_events(limit=20)]
                self.assertIn("queue_task_claimed", event_types)
                self.assertIn("queue_task_deferred", event_types)
                self.assertIn("queue_task_completed", event_types)
                self.assertIn("queue_task_retry", event_types)
                self.assertIn("queue_task_escalated", event_types)
                self.assertNotIn("handler failed", json.dumps(store.recent_events(limit=20)))

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
                running = coordinator.snapshot()[0]
                self.assertEqual(running["state"], "WORKING")
                self.assertIsNotNone(running["started_at"])
                self.assertEqual(running["elapsed_s"], 0.0)
                time.sleep(0.2)
                decisions = coordinator.tick(snapshot)
            self.assertEqual(decisions[0].action, "continue")
            completed = coordinator.snapshot()[0]
            self.assertLess(completed["last_duration_s"], 0.15)
            self.assertIsNone(completed["started_at"])
            self.assertIsNone(completed["elapsed_s"])
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

    def test_agent_run_uses_durable_task_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with StateStore(Path(raw) / "state.sqlite3") as store:
                scheduler = WorkScheduler(store)
                coordinator = AgentCoordinator(
                    [
                        {
                            "id": "ledger-agent",
                            "role": "test",
                            "provider": "test",
                            "enabled": True,
                            "interval_s": 60,
                        }
                    ],
                    scheduler=scheduler,
                )
                try:
                    with patch("manager.agents.time.monotonic", return_value=1.0):
                        decisions = coordinator.tick(
                            {
                                "status": "HEALTHY",
                                "objective": "synthetic",
                                "objective_id": "synthetic-objective",
                                "workers": [],
                                "jobs": [],
                            }
                        )
                    self.assertEqual(decisions[0].action, "continue")
                    self.assertEqual(scheduler.snapshot(), {"COMPLETE": 1})
                    self.assertEqual(
                        [event["event_type"] for event in coordinator.events],
                        ["agent_task_started", "agent_decision"],
                    )
                    self.assertTrue(coordinator.events[1]["artifact_refs"][0].startswith("task:"))
                finally:
                    coordinator.close()

    def test_agent_coordinator_resumes_its_queued_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with StateStore(Path(raw) / "state.sqlite3") as store:
                scheduler = WorkScheduler(store)
                scheduler.enqueue(
                    kind="agent_review",
                    objective_id="synthetic-objective",
                    payload={"agent_id": "ledger-agent"},
                    task_id="interrupted-agent-review",
                )
                self.assertTrue(scheduler.start("interrupted-agent-review"))
                self.assertEqual(scheduler.recover_interrupted(), 1)
                coordinator = AgentCoordinator(
                    [
                        {
                            "id": "ledger-agent",
                            "role": "test",
                            "provider": "test",
                            "enabled": True,
                            "interval_s": 60,
                        }
                    ],
                    scheduler=scheduler,
                )
                try:
                    with patch("manager.agents.time.monotonic", return_value=1.0):
                        decisions = coordinator.tick(
                            {
                                "status": "HEALTHY",
                                "objective": "synthetic",
                                "objective_id": "synthetic-objective",
                                "workers": [],
                                "jobs": [],
                            }
                        )
                    self.assertEqual(decisions[0].action, "continue")
                    self.assertEqual(scheduler.snapshot(), {"COMPLETE": 1})
                    self.assertEqual(
                        scheduler.activity_snapshot(limit=1)[0]["task_id"],
                        "interrupted-agent-review",
                    )
                    self.assertEqual(
                        scheduler.activity_snapshot(limit=1)[0]["attempts"],
                        2,
                    )
                finally:
                    coordinator.close()

    def test_agent_registry_restores_completed_count_from_durable_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with StateStore(Path(raw) / "state.sqlite3") as store:
                scheduler = WorkScheduler(store)
                for index in range(3):
                    task_id = f"completed-agent-review-{index}"
                    scheduler.enqueue(
                        kind="agent_review",
                        objective_id="synthetic-objective",
                        payload={"agent_id": "ledger-agent"},
                        task_id=task_id,
                    )
                    self.assertTrue(scheduler.start(task_id))
                    scheduler.complete(task_id)

                coordinator = AgentCoordinator(
                    [
                        {
                            "id": "ledger-agent",
                            "role": "test",
                            "provider": "test",
                            "enabled": True,
                            "interval_s": 60,
                        }
                    ],
                    scheduler=scheduler,
                )
                try:
                    self.assertEqual(coordinator.snapshot()[0]["tasks_completed"], 3)
                finally:
                    coordinator.close()

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

    def test_public_upload_rejects_sensitive_fields_and_private_paths_before_network_access(self) -> None:
        unsafe_payloads = {
            "sensitive field": {"status": "HEALTHY", "token": "synthetic-placeholder"},
            "private path": {"status": "HEALTHY", "diagnostic": r"C:\synthetic\private"},
        }
        with tempfile.TemporaryDirectory() as raw:
            dashboard = Path(raw) / "dashboard"
            data = dashboard / "data"
            data.mkdir(parents=True)
            (data / "events.json").write_text("[]", encoding="utf-8")
            (data / "scenarios.json").write_text("[]", encoding="utf-8")
            for label, payload in unsafe_payloads.items():
                with self.subTest(label=label):
                    (data / "latest.json").write_text(json.dumps(payload), encoding="utf-8")
                    publisher = GitHubPagesPublisher(
                        dashboard,
                        owner="owner",
                        repository="repo",
                    )
                    with patch.object(publisher, "_request") as request:
                        with self.assertRaises(PublicUploadError):
                            publisher.publish()
                    request.assert_not_called()

    def test_public_upload_bypasses_cadence_for_a_meaningful_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            publisher = GitHubPagesPublisher(
                Path(raw) / "dashboard",
                owner="owner",
                repository="repo",
            )
            publisher.last_publish_monotonic = 100.0
            with patch.dict(os.environ, {"MACHINE_MANAGER_GITHUB_TOKEN": "test-only"}):
                with patch.object(publisher, "publish", return_value=True) as publish:
                    self.assertFalse(publisher.maybe_publish(now=101.0))
                    publish.assert_not_called()
                    self.assertTrue(publisher.maybe_publish(now=101.0, immediate=True))
                    publish.assert_called_once_with()

    def test_public_state_marker_ignores_progress_but_records_lifecycle_changes(self) -> None:
        baseline = {
            "status": "HEALTHY",
            "workers": [{"id": "worker-1", "state": "RUNNING", "progress": {"keys_tested": 1}}],
            "jobs": [{"id": "job-1", "state": "RUNNING"}],
            "workstreams": [{"id": "lane-1", "state": "RUNNING"}],
            "queue_activity": [
                {
                    "task_id": "task-1",
                    "kind": "research",
                    "status": "COMPLETE",
                    "attempts": 1,
                    "updated_at": 1724875200.0,
                }
            ],
            "updated": "2026-08-30T00:00:00Z",
        }
        progress_only = json.loads(json.dumps(baseline))
        progress_only["workers"][0]["progress"]["keys_tested"] = 2
        progress_only["updated"] = "2026-08-30T00:01:00Z"
        lifecycle_change = json.loads(json.dumps(baseline))
        lifecycle_change["workers"][0]["state"] = "RETRYING"
        queue_progress_only = json.loads(json.dumps(baseline))
        queue_progress_only["queue_activity"][0]["updated_at"] = 1724875260.0
        queue_lifecycle_change = json.loads(json.dumps(baseline))
        queue_lifecycle_change["queue_activity"][0]["status"] = "ESCALATED"

        self.assertEqual(_public_state_marker(baseline), _public_state_marker(progress_only))
        self.assertEqual(_public_state_marker(baseline), _public_state_marker(queue_progress_only))
        self.assertNotEqual(_public_state_marker(baseline), _public_state_marker(lifecycle_change))
        self.assertNotEqual(_public_state_marker(baseline), _public_state_marker(queue_lifecycle_change))

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

    def test_public_upload_mirrors_a_fast_forward_when_only_generated_files_are_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw) / "repo"
            (repository / ".git").mkdir(parents=True)
            publisher = GitHubPagesPublisher(
                repository / "dashboard",
                owner="owner",
                repository="repo",
                local_repo_dir=repository,
            )
            remote_sha = "a" * 40
            local_sha = "b" * 40
            updates: list[list[str]] = []

            def fake_git(command: list[str], **kwargs: object) -> SimpleNamespace:
                action = command[3:]
                if action[:1] == ["symbolic-ref"]:
                    return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
                if action[:1] == ["rev-parse"]:
                    return SimpleNamespace(returncode=0, stdout=local_sha + "\n", stderr="")
                if action[:1] == ["status"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=(
                            " M dashboard/data/latest.json\n"
                            " M dashboard/data/events.json\n"
                            " M dashboard/data/scenarios.json\n"
                        ),
                        stderr="",
                    )
                if action[:1] == ["fetch"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if action[:1] == ["merge-base"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if action[:1] == ["read-tree"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if action[:1] == ["show-ref"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                if action[:1] == ["update-ref"]:
                    updates.append(action)
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                raise AssertionError(action)

            with patch("manager.public_upload.subprocess.run", side_effect=fake_git):
                self.assertEqual(publisher._mirror_local_ref(remote_sha), "synced")
            self.assertEqual(updates, [["update-ref", "refs/heads/main", remote_sha, local_sha]])

    def test_public_upload_defers_local_mirror_for_unrelated_changes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw) / "repo"
            (repository / ".git").mkdir(parents=True)
            publisher = GitHubPagesPublisher(
                repository / "dashboard",
                owner="owner",
                repository="repo",
                local_repo_dir=repository,
            )
            remote_sha = "a" * 40

            def fake_git(command: list[str], **kwargs: object) -> SimpleNamespace:
                action = command[3:]
                if action[:1] == ["symbolic-ref"]:
                    return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
                if action[:1] == ["rev-parse"]:
                    return SimpleNamespace(returncode=0, stdout=("b" * 40) + "\n", stderr="")
                if action[:1] == ["status"]:
                    return SimpleNamespace(returncode=0, stdout=" M README.md\n", stderr="")
                raise AssertionError(action)

            with patch("manager.public_upload.subprocess.run", side_effect=fake_git):
                self.assertEqual(publisher._mirror_local_ref(remote_sha), "deferred")

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
            {"event_id": "old-2", "task_id": "task-research-1"},
            {"event_id": "new-1"},
            {"event_id": "new-2"},
        ]
        merged = merge_public_events(existing, current, limit=2)
        self.assertEqual([event["event_id"] for event in merged], ["new-1", "new-2"])

        replaced = merge_public_events(existing, current, limit=3)
        self.assertEqual(replaced[0]["event_id"], "old-2")
        self.assertEqual(replaced[0]["task_id"], "task-research-1")

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
                    "queue": {"QUEUED": 3, "COMPLETE": 2, "pid": 9999},
                    "queue_kinds": {"agent_review": 2, "objective_change": 1, "private_kind": 99},
                    "queue_activity": [
                        {
                            "task_id": "task-research-1",
                            "kind": "research",
                            "objective_id": "obj-1",
                            "status": "COMPLETE",
                            "attempts": 1,
                            "updated_at": 1724875200.0,
                            "payload": {"private_key": "do not publish"},
                        },
                        {
                            "task_id": "task-private",
                            "kind": "private_kind",
                            "objective_id": "private-objective",
                            "status": "COMPLETE",
                            "attempts": 1,
                            "updated_at": 1724875200.0,
                        },
                    ],
                    "recurring": [
                        {
                            "id": "research-lane",
                            "kind": "research",
                            "objective_id": "research-objective",
                            "enabled": True,
                            "interval_s": 3600,
                            "sequence": 2,
                            "next_at": 1724878800.0,
                            "next_in_s": 3600.0,
                            "last_task_id": "task-research-1",
                            "last_status": "COMPLETE",
                            "payload": {"private_key": "do not publish"},
                        }
                    ],
                    "gpu": {"util_pct": 80, "power_w": 70, "resource_active": True, "private_key": "do not publish"},
                    "system": {"cpu_pct": 8.5, "private_key": "do not publish"},
                    "updated": "2026-08-28T20:00:00Z",
                },
                events=[
                    {
                        "timestamp": "2026-08-28T20:00:00Z",
                        "event_id": "evt-1",
                        "event_type": "state_change",
                        "kind": "research",
                        "objective_id": "research-objective",
                        "task_id": "task-research-1",
                        "new_state": "RUNNING",
                        "action": "start",
                        "artifact_refs": ["task:task-research-1"],
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
            self.assertEqual(latest["queue"], {"QUEUED": 3, "COMPLETE": 2})
            self.assertEqual(latest["queue_kinds"], {"agent_review": 2, "objective_change": 1})
            self.assertEqual(len(latest["queue_activity"]), 1)
            self.assertEqual(latest["queue_activity"][0]["kind"], "research")
            self.assertEqual(latest["queue_activity"][0]["status"], "COMPLETE")
            self.assertNotIn("payload", json.dumps(latest["queue_activity"]))
            self.assertEqual(latest["recurring"][0]["id"], "research-lane")
            self.assertEqual(latest["recurring"][0]["last_status"], "COMPLETE")
            self.assertNotIn("payload", json.dumps(latest["recurring"]))
            self.assertTrue(latest["autonomy"]["developer_tools"])
            self.assertEqual(latest["system"]["cpu_pct"], 8.5)
            self.assertTrue(latest["gpu"]["resource_active"])
            self.assertNotIn("private_note", json.dumps(latest))
            self.assertNotIn("private_path", json.dumps(latest))
            self.assertNotIn("findings", json.dumps(latest))
            self.assertNotIn("C:\\", json.dumps(latest))
            self.assertEqual(latest["worker_profiles"][0]["capabilities"][0]["summary"], "token: [redacted]")
            self.assertEqual(latest["constraint_audits"][0]["categories"], {"approval_gate": 2})
            self.assertTrue(latest["constraint_audits"][0]["more_pending"])
            self.assertNotIn("C:\\", json.dumps(events))
            self.assertNotIn("pid", json.dumps(events))
            self.assertEqual(events[0]["metrics"]["keys_tested"], 99)
            self.assertFalse(events[0]["error"] is False)
            self.assertEqual(events[0]["kind"], "research")
            self.assertEqual(events[0]["objective_id"], "research-objective")
            self.assertEqual(events[0]["task_id"], "task-research-1")
            self.assertEqual(latest["workers"][0]["state"], "RUNNING")

    def test_publisher_retries_a_transient_dashboard_file_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dashboard = Path(raw) / "dashboard"
            publisher = TelemetryPublisher(
                dashboard,
                replace_attempts=2,
                retry_delay_s=0,
            )
            original_replace = os.replace
            calls = 0

            def replace_after_one_lock(source: str, destination: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError(5, "Access is denied", str(destination))
                original_replace(source, destination)

            with patch(
                "manager.telemetry.os.replace",
                side_effect=replace_after_one_lock,
            ):
                publisher.publish({"status": "HEALTHY", "workers": [], "jobs": [], "gpu": {}})

            self.assertEqual(calls, 4)
            self.assertTrue((dashboard / "data" / "latest.json").is_file())

    def test_publisher_reports_a_persistent_dashboard_file_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dashboard = Path(raw) / "dashboard"
            publisher = TelemetryPublisher(
                dashboard,
                replace_attempts=2,
                retry_delay_s=0,
            )
            with patch(
                "manager.telemetry.os.replace",
                side_effect=PermissionError(5, "Access is denied"),
            ):
                with self.assertRaises(TelemetryWriteError):
                    publisher._atomic_json("latest.json", {"status": "HEALTHY"})
            self.assertEqual(list((dashboard / "data").glob("*.tmp")), [])

    def test_runner_defers_a_dashboard_write_failure_without_raising(self) -> None:
        class LockedPublisher:
            def publish(self, *args: object, **kwargs: object) -> None:
                raise PermissionError(5, "Access is denied")

        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "manager.log"
            published = _publish_local_snapshot(
                LockedPublisher(),  # type: ignore[arg-type]
                {"status": "HEALTHY"},
                events=[],
                scenarios=[],
                manager_log_path=log_path,
            )
            self.assertFalse(published)
            diagnostic = log_path.read_text(encoding="utf-8")
            self.assertIn("Local telemetry deferred: PermissionError", diagnostic)
            self.assertNotIn("Access is denied", diagnostic)

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
