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
        ):
            self.assertIn(marker, self.dashboard)

    def test_low_gpu_event_samples_keep_the_raw_value_visible(self) -> None:
        self.assertIn("GPU ACTIVE (raw ", self.dashboard)


if __name__ == "__main__":
    unittest.main()
