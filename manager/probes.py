"""Small resource probes used by the local manager."""

from __future__ import annotations

import subprocess
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
