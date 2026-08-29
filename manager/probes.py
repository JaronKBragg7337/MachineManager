"""Small resource probes used by the local manager."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


def nvidia_smi_probe() -> dict[str, Any]:
    """Return compact GPU metrics without exposing command lines or paths."""
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        ).strip()
        values = [value.strip() for value in output.split(",")]
        util = float(values[0])
        mem_used = float(values[1])
        mem_total = float(values[2])
        temp = float(values[3])
        power = float(values[4])
        return {
            "util_pct": util,
            "mem_used_mib": mem_used,
            "mem_total_mib": mem_total,
            "temp_c": temp,
            "power_w": power,
        }
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        return {"probe_error": type(exc).__name__}


def gpu_resource_ok(metrics: dict[str, Any], *, min_util_pct: float = 15, min_power_w: float = 20) -> bool:
    """Treat a GPU as active only when utilization and power are non-idle."""
    try:
        return float(metrics.get("util_pct", 0)) >= min_util_pct and float(metrics.get("power_w", 0)) >= min_power_w
    except (TypeError, ValueError):
        return False


_KEYHUNT_STATUS = re.compile(
    r"GPU\s*:\s*(?P<rate>[0-9][0-9.,]*)\s*Mk/s"
    r".*?C\s*:\s*(?P<coverage>[0-9][0-9.,]*)\s*%"
    r".*?R\s*:\s*(?P<round>[0-9]+)"
    r".*?T\s*:\s*(?P<tested>[0-9][0-9 ,]*)",
    re.IGNORECASE | re.DOTALL,
)
_KEYHUNT_FOUND = re.compile(r"F\s*:\s*(?P<found>[0-9]+)", re.IGNORECASE)


def _numeric(value: str) -> float:
    return float(value.replace(",", "").strip())


def keyhunt_progress_probe(path: Path, *, max_bytes: int = 262_144) -> dict[str, Any]:
    """Extract only aggregate KeyHunt status values from its local output.

    KeyHunt's status line contains both aggregate counters and sensitive
    candidate material elsewhere in the stream. This probe reads a bounded
    tail and returns only the explicitly named numeric aggregates.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            text = handle.read(max_bytes).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return {}

    matches = list(_KEYHUNT_STATUS.finditer(text))
    if not matches:
        return {}
    match = matches[-1]
    try:
        rate_mkey_s = _numeric(match.group("rate"))
        coverage_pct = _numeric(match.group("coverage"))
        batch_number = int(match.group("round"))
        keys_tested = int(match.group("tested").replace(" ", "").replace(",", ""))
    except (TypeError, ValueError):
        return {}

    prefix = text[max(0, match.start() - 160) : match.start()]
    found_matches = list(_KEYHUNT_FOUND.finditer(prefix))
    result: dict[str, Any] = {
        "hashrate_mkey_s": rate_mkey_s,
        "keys_per_second": rate_mkey_s * 1_000_000,
        "coverage_pct": coverage_pct,
        "batch_number": batch_number,
        "keys_tested": keys_tested,
    }
    if found_matches:
        result["matches_found"] = int(found_matches[-1].group("found"))
    return result
