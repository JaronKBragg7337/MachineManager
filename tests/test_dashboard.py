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
            "function renderRecurringSchedules(items)",
            "function gpuEvidenceActive(gpu)",
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
            "kind-balanced window",
        ):
            self.assertIn(marker, self.dashboard)

    def test_low_gpu_event_samples_keep_the_raw_value_visible(self) -> None:
        self.assertIn("GPU ACTIVE (raw ", self.dashboard)

    def test_low_cpu_samples_explain_active_gpu_work(self) -> None:
        self.assertIn("Raw CPU sample ", self.dashboard)
        self.assertIn('missionReading("CPU", cpuActivityText(system, gpu)', self.dashboard)
        self.assertIn('+ cpuActivityCard(system, gpu) +', self.dashboard)
        self.assertNotIn('resourceCard("CPU utilization", numberValue(system.cpu_pct) === null ? "--" : String(Math.round(system.cpu_pct))', self.dashboard)


if __name__ == "__main__":
    unittest.main()
