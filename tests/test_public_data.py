from __future__ import annotations

import json
import re
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DATA = REPO_ROOT / "dashboard" / "data"
PRIVATE_KEY = re.compile(
    r"(?i)(?:pid|private[_ ]?key|secret|token|password|seed|api[_ ]?key)"
)
PRIVATE_PATH = re.compile(r"(?i)(?:[A-Za-z]:[\\/]|/Users/|/home/|\\\\)")


class PublicDataContractTests(unittest.TestCase):
    def _load(self, name: str) -> Any:
        path = PUBLIC_DATA / name
        self.assertTrue(path.is_file(), name)
        self.assertLessEqual(path.stat().st_size, 512_000, name)
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def _assert_public_safe(self, value: Any, location: str = "root") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_name = str(key)
                self.assertIsNone(
                    PRIVATE_KEY.search(key_name),
                    f"private field at {location}.{key_name}",
                )
                self._assert_public_safe(child, f"{location}.{key_name}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._assert_public_safe(child, f"{location}[{index}]")
        elif isinstance(value, str):
            self.assertIsNone(
                PRIVATE_PATH.search(value),
                f"private path at {location}",
            )

    def test_public_files_have_expected_shapes_and_safe_metrics(self) -> None:
        latest = self._load("latest.json")
        events = self._load("events.json")
        scenarios_document = self._load("scenarios.json")

        self.assertIsInstance(latest, dict)
        self.assertIn(latest.get("status"), {"HEALTHY", "DEGRADED", "ESCALATED", "UNKNOWN"})
        updated = latest.get("updated")
        self.assertIsInstance(updated, str)
        datetime.fromisoformat(updated.replace("Z", "+00:00"))

        self.assertIsInstance(latest.get("workers"), list)
        self.assertIsInstance(latest.get("jobs"), list)
        self.assertIsInstance(latest.get("agents"), list)
        self.assertIsInstance(latest.get("gpu"), dict)
        self.assertIsInstance(latest.get("system"), dict)

        gpu = latest["gpu"]
        for key in ("util_pct", "mem_used_mib", "mem_total_mib", "temp_c", "power_w"):
            if key in gpu:
                self.assertIsInstance(gpu[key], (int, float))
                self.assertGreaterEqual(gpu[key], 0, key)
        if "util_pct" in gpu:
            self.assertLessEqual(gpu["util_pct"], 100, "gpu.util_pct")
        if "resource_active" in gpu:
            self.assertIsInstance(gpu["resource_active"], bool)

        system = latest["system"]
        if "cpu_pct" in system:
            self.assertIsInstance(system["cpu_pct"], (int, float))
            self.assertGreaterEqual(system["cpu_pct"], 0, "system.cpu_pct")
            self.assertLessEqual(system["cpu_pct"], 100, "system.cpu_pct")

        self.assertIsInstance(events, list)
        self.assertIsInstance(scenarios_document, dict)
        self.assertIsInstance(scenarios_document.get("scenarios"), list)

        self._assert_public_safe(latest)
        self._assert_public_safe(events)
        self._assert_public_safe(scenarios_document)


if __name__ == "__main__":
    unittest.main()
