from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from manager.state_store import StateStore
from manager.telemetry import TelemetryPublisher
from manager.workstreams import WorkstreamRegistry


class WorkstreamRegistryTests(unittest.TestCase):
    def test_dynamic_sources_publish_observed_state_without_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with StateStore(root / "state.sqlite3") as store:
                registry = WorkstreamRegistry(
                    store,
                    [
                    {
                        "id": "audit-lane",
                        "objective_id": "governance",
                        "title": "Constraint audit",
                        "lane": "Evidence",
                        "owner": "evidence-worker",
                        "summary": "Review candidates without changing files.",
                        "next_action": "Run one bounded evidence test.",
                        "source": {"kind": "constraint_audit", "id": "workspace-audit"},
                    },
                    {
                        "id": "agent-lane",
                        "objective_id": "operations",
                        "title": "Health specialist",
                        "lane": "Runtime",
                        "owner": "local-agent",
                        "summary": "Check the sanitized runtime picture.",
                        "next_action": "Report the next bounded recommendation.",
                        "source": {"kind": "agent", "id": "health-agent"},
                    },
                    {
                        "id": "complete-lane",
                        "objective_id": "delivery",
                        "title": "Published milestone",
                        "lane": "Delivery",
                        "owner": "codex",
                        "summary": "A completed bounded mission.",
                        "next_action": "Use the result as evidence for the next task.",
                        "state": "COMPLETE",
                    },
                    ],
                )
                audits = [
                {
                    "id": "workspace-audit",
                    "state": "NEEDS_EVIDENCE_REVIEW",
                    "candidate_count": 7,
                    "files_scanned": 22,
                    "more_pending": True,
                    "path": r"C:\\not-for-publication",
                }
                ]
                agents = [{"id": "health-agent", "state": "WORKING", "tasks_completed": 3}]

                records, events = registry.sync(agents=agents, audits=audits)
                by_id = {record["id"]: record for record in records}
                self.assertEqual(by_id["audit-lane"]["state"], "REVIEW")
                self.assertEqual(by_id["audit-lane"]["metrics"], {"candidate_count": 7, "files_scanned": 22, "more_pending": True})
                self.assertEqual(by_id["agent-lane"]["state"], "RUNNING")
                self.assertEqual(by_id["agent-lane"]["metrics"], {"tasks_completed": 3})
                self.assertEqual(by_id["complete-lane"]["state"], "COMPLETE")
                self.assertEqual(len(events), 3)
                self.assertNotIn("path", by_id["audit-lane"])

                records, events = registry.sync(
                    agents=agents,
                    audits=[{"id": "workspace-audit", "state": "NO_CANDIDATES", "candidate_count": 0, "files_scanned": 22}],
                )
                refreshed = {record["id"]: record for record in records}
                self.assertEqual(refreshed["audit-lane"]["state"], "WAITING")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event_type"], "workstream_state_changed")

    def test_public_telemetry_allowlists_workstream_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publisher = TelemetryPublisher(root / "dashboard")
            publisher.publish(
                {
                    "status": "HEALTHY",
                    "updated": "2026-08-30T00:00:00Z",
                    "workstreams": [
                        {
                            "id": "safe-lane",
                            "objective_id": "safe-objective",
                            "title": "Safe title",
                            "lane": "Research",
                            "owner": "safe-worker",
                            "state": "RUNNING",
                            "summary": r"C:\\private\\should-not-appear",
                            "next_action": "Run an evidence test.",
                            "source_kind": "constraint_audit",
                            "source_id": "not-public",
                            "metrics": {"candidate_count": 4, "secret_metric": 999},
                            "private": "discard",
                        }
                    ],
                }
            )
            latest = json.loads((root / "dashboard" / "data" / "latest.json").read_text(encoding="utf-8"))
            stream = latest["workstreams"][0]
            self.assertEqual(stream["summary"], "[local-path]")
            self.assertEqual(stream["metrics"], {"candidate_count": 4})
            self.assertNotIn("private", stream)
            self.assertNotIn("source_id", stream)


if __name__ == "__main__":
    unittest.main()
