import unittest
from pathlib import Path


class DashboardContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dashboard = (
            Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
        ).read_text(encoding="utf-8")

    def test_search_evidence_contains_a_source_backed_trend(self) -> None:
        for marker in (
            "function searchProgressSamples()",
            "function renderSearchHistory(samples)",
            "Cumulative candidate checks",
            'role="img"',
            "counters may reset after a worker restart",
            "Run evidence",
            "Started ",
            "Run at snapshot",
            "AI support team",
            "const agentRows = agents.length ? agents.map",
            "AI reviewer is working",
            "Processing the current sanitized snapshot",
            "Assigned focus:",
            "function agentRunRecord(agent)",
            "function agentOutcomeLabel(agent)",
            "Run record",
            "Last result",
            "Deterministic fallback",
            "function workstreamAgent(stream, agents)",
            "workstreamCard(stream, agents)",
            "Run record:</strong>",
            "function renderRecurringSchedules(items)",
            "function gpuEvidenceActive(gpu)",
            "function gpuActivityBasisText(gpu)",
            "function cpuActivityText(system, gpu)",
            "function cpuActivityCard(system, gpu)",
            "function taskKindLabel(value)",
            'revenue: "Revenue discovery"',
            "UNATTENDED SCHEDULE",
            "UNATTENDED WORK CADENCE",
            "manager-owned cadence",
            "const workEvents = recentEvents(500).filter",
            "const researchEvents = recentEvents(500).filter",
            "No separate lifecycle events are retained",
            "latest 32 public task records",
            "kind-balanced window interleaves recent records",
            "function taskEvidenceLabel(item)",
            "function taskEvidenceMetricChips(item)",
            "Latest evidence",
            "function auditReviewPlan(plan)",
            "function auditReviewCount(audits)",
            "planned category tests",
            "Recommended evidence tests:",
        ):
            self.assertIn(marker, self.dashboard)

    def test_low_gpu_event_samples_keep_the_raw_value_visible(self) -> None:
        self.assertIn("GPU ACTIVE (diagnostic raw sample ", self.dashboard)

    def test_low_cpu_samples_explain_active_gpu_work(self) -> None:
        self.assertIn("diagnostic CPU sample ", self.dashboard)
        self.assertIn('missionReading("CPU", cpuActivityText(system, gpu)', self.dashboard)
        self.assertIn('+ cpuActivityCard(system, gpu) +', self.dashboard)
        self.assertNotIn('resourceCard("CPU utilization", numberValue(system.cpu_pct) === null ? "--" : String(Math.round(system.cpu_pct))', self.dashboard)

    def test_search_evidence_labels_transient_low_gpu_samples(self) -> None:
        self.assertIn("function gpuObservationText(gpu)", self.dashboard)
        self.assertIn('<span>Driver observation</span>', self.dashboard)
        self.assertIn('<span>Activity basis</span>', self.dashboard)
        self.assertIn('<span>Host-load basis</span>', self.dashboard)
        self.assertIn("diagnostic driver sample", self.dashboard)

    def test_semantic_resource_states_do_not_render_false_numeric_badges(self) -> None:
        self.assertIn("function resourceMeterLabel(value, percent)", self.dashboard)
        self.assertIn('["ACTIVE", "LOW", "IDLE", "--"].includes(semantic)', self.dashboard)
        self.assertIn("resourceMeterLabel(value, percent)", self.dashboard)

    def test_resumed_mobile_pages_fetch_a_fresh_snapshot(self) -> None:
        self.assertIn('document.addEventListener("visibilitychange"', self.dashboard)
        self.assertIn('if (document.visibilityState === "visible") loadAll(false);', self.dashboard)
        self.assertIn('window.addEventListener("pageshow"', self.dashboard)


if __name__ == "__main__":
    unittest.main()
