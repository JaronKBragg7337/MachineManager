from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class ScenarioArtifactTests(unittest.TestCase):
    def test_stalled_worker_artifacts_record_a_sanitized_pass(self) -> None:
        trace = json.loads(
            (REPO_ROOT / "scenarios" / "stalled-worker" / "trace-001.json").read_text(
                encoding="utf-8"
            )
        )
        evaluation = json.loads(
            (REPO_ROOT / "evaluations" / "stalled-worker-manager-001.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(trace["result"], "PASS")
        self.assertTrue(trace["observations"]["worker_present_after_failure"])
        self.assertFalse(trace["observations"]["heartbeat_fresh_after_failure"])
        self.assertEqual(trace["observations"]["supervisor_state_after_failure"], "STALLED")
        self.assertEqual(trace["resulting_state"], "RUNNING")
        self.assertEqual(evaluation["result"], "PASS")
        self.assertEqual(evaluation["ceo_intervention_count"], 0)
        self.assertEqual(evaluation["recovery"]["confirmed_state"], "RUNNING")
        self.assertNotIn("pid", json.dumps(trace).lower())
        self.assertNotIn("pid", json.dumps(evaluation).lower())

    def test_malformed_agent_response_artifacts_record_a_sanitized_pass(self) -> None:
        trace = json.loads(
            (
                REPO_ROOT
                / "scenarios"
                / "malformed-agent-response"
                / "trace-001.json"
            ).read_text(encoding="utf-8")
        )
        evaluation = json.loads(
            (
                REPO_ROOT
                / "evaluations"
                / "malformed-agent-response-manager-001.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(trace["result"], "PASS")
        self.assertEqual(trace["observations"]["fallback_cases"], 5)
        self.assertEqual(trace["observations"]["fallback_action"], "continue")
        self.assertTrue(trace["observations"]["supervisor_continued"])
        self.assertEqual(trace["recovery"]["confirmed_agent_state"], "READY")
        self.assertEqual(evaluation["result"], "PASS")
        self.assertEqual(evaluation["ceo_intervention_count"], 0)
        self.assertEqual(evaluation["manager_observation"]["event_outcome"], "fallback")
        self.assertNotIn("pid", json.dumps(trace).lower())
        self.assertNotIn("pid", json.dumps(evaluation).lower())

    def test_repeated_failure_artifacts_record_a_sanitized_escalation(self) -> None:
        trace = json.loads(
            (
                REPO_ROOT
                / "scenarios"
                / "repeated-failure-escalation"
                / "trace-001.json"
            ).read_text(encoding="utf-8")
        )
        evaluation = json.loads(
            (
                REPO_ROOT
                / "evaluations"
                / "repeated-failure-escalation-manager-001.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(trace["result"], "PASS")
        self.assertEqual(trace["induced_failure"]["failure_observations"], 2)
        self.assertTrue(trace["observations"]["retry_budget_exhausted"])
        self.assertEqual(trace["recovery"]["final_state"], "ESCALATED")
        self.assertTrue(trace["recovery"]["worker_stopped_after_escalation"])
        self.assertEqual(evaluation["result"], "PASS")
        self.assertEqual(evaluation["ceo_intervention_count"], 0)
        self.assertEqual(
            evaluation["manager_observation"]["escalation_outcome"],
            "retry_limit_reached",
        )
        self.assertNotIn("pid", json.dumps(trace).lower())
        self.assertNotIn("pid", json.dumps(evaluation).lower())


if __name__ == "__main__":
    unittest.main()
