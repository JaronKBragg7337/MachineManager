"""Small resource probes used by the local manager."""

from __future__ import annotations

import ctypes
from collections import deque
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable


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


def _history_size(value: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 5


def _history_metrics(values: Iterable[float], *, prefix: str) -> dict[str, int | float]:
    samples = list(values)
    if not samples:
        return {}
    return {
        f"{prefix}_recent_max": round(max(samples), 1),
        f"{prefix}_recent_avg": round(sum(samples) / len(samples), 1),
        f"{prefix}_sample_count": len(samples),
        f"{prefix}_zero_samples": sum(value == 0 for value in samples),
    }


class NvidiaSmiProbe:
    """Add a small bounded utilization history to each GPU observation.

    ``util_pct`` remains the current raw driver sample. The additional fields
    make a transient zero visible as a measurement characteristic instead of
    forcing the dashboard or health decision to infer history from timestamps.
    Probe failures are returned unchanged and never become synthetic zeros.
    """

    def __init__(self, *, history_size: int = 5) -> None:
        self._util_history: deque[float] = deque(maxlen=_history_size(history_size))

    def __call__(self) -> dict[str, Any]:
        metrics = nvidia_smi_probe()
        try:
            util = float(metrics["util_pct"])
        except (KeyError, TypeError, ValueError):
            return metrics
        if not math.isfinite(util):
            return metrics

        self._util_history.append(max(0.0, min(100.0, util)))
        result = dict(metrics)
        result.update(_history_metrics(self._util_history, prefix="util_pct"))
        return result


class CpuUsageProbe:
    """Measure host CPU use from cumulative operating-system counters."""

    def __init__(self, *, history_size: int = 5) -> None:
        self._previous = self._read_times()
        self._usage_history: deque[float] = deque(maxlen=_history_size(history_size))

    def __call__(self) -> dict[str, float]:
        current = self._read_times()
        previous = self._previous
        self._previous = current
        if previous is None or current is None:
            return {}
        previous_idle, previous_total = previous
        current_idle, current_total = current
        delta_idle = current_idle - previous_idle
        delta_total = current_total - previous_total
        if delta_total <= 0 or delta_idle < 0:
            return {}
        used_pct = (1.0 - min(1.0, delta_idle / delta_total)) * 100.0
        sample = round(max(0.0, min(100.0, used_pct)), 1)
        self._usage_history.append(sample)
        result: dict[str, int | float] = {"cpu_pct": sample}
        result.update(_history_metrics(self._usage_history, prefix="cpu_pct"))
        return result

    @staticmethod
    def _read_times() -> tuple[int, int] | None:
        if os.name == "nt":
            try:
                class _FileTime(ctypes.Structure):
                    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

                idle = _FileTime()
                kernel = _FileTime()
                user = _FileTime()
                if not ctypes.windll.kernel32.GetSystemTimes(
                    ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
                ):
                    return None

                def ticks(value: _FileTime) -> int:
                    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

                idle_ticks = ticks(idle)
                return idle_ticks, ticks(kernel) + ticks(user)
            except (AttributeError, OSError, TypeError):
                return None

        try:
            first_line = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0]
            fields = first_line.split()
            if not fields or fields[0] != "cpu":
                return None
            values = [int(value) for value in fields[1:]]
            if len(values) < 4:
                return None
            idle_ticks = values[3] + (values[4] if len(values) > 4 else 0)
            return idle_ticks, sum(values)
        except (IndexError, OSError, ValueError, UnicodeDecodeError):
            return None


def gpu_resource_ok(
    metrics: dict[str, Any],
    *,
    min_util_pct: float = 15,
    min_power_w: float = 20,
    min_active_power_w: float = 40,
    min_mem_used_mib: float = 512,
) -> bool:
    """Treat a GPU as active while tolerating a transient zero-util sample.

    Some NVIDIA driver samples briefly report zero utilization while a CUDA
    worker still owns its working memory and draws active power. The fallback
    accepts that narrow pattern, while an idle device with no dedicated memory
    remains unhealthy even if its power reading is noisy.
    """
    try:
        util = float(metrics.get("util_pct", 0))
        power = float(metrics.get("power_w", 0))
        memory = float(metrics.get("mem_used_mib", 0))
        if power < min_power_w:
            return False
        if util >= min_util_pct:
            return True
        return power >= min_active_power_w and memory >= min_mem_used_mib
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

    # The bounded tail can end halfway through a status line while KeyHunt is
    # writing it. Never publish a partial cumulative counter from that line.
    if text and not text.endswith(("\n", "\r")):
        last_break = max(text.rfind("\n"), text.rfind("\r"))
        text = text[: last_break + 1] if last_break >= 0 else ""

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
