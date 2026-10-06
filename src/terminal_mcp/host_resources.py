from __future__ import annotations

import os
import threading
from pathlib import Path

_CPU_SAMPLE_LOCK = threading.Lock()
_CPU_SAMPLE: tuple[int, int] | None = None


def _read_cpu_times() -> tuple[int, int] | None:
    try:
        first = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        if not first or first[0] != "cpu":
            return None
        values = [int(value) for value in first[1:9]]
        if len(values) < 5:
            return None
        total = sum(values)
        idle = values[3] + values[4]
        return total, idle
    except (OSError, ValueError, IndexError):
        return None


def _cpu_usage_percent() -> float | None:
    global _CPU_SAMPLE
    current = _read_cpu_times()
    if current is None:
        return None
    with _CPU_SAMPLE_LOCK:
        previous = _CPU_SAMPLE
        _CPU_SAMPLE = current
    if previous is None:
        return None
    delta_total = current[0] - previous[0]
    delta_idle = current[1] - previous[1]
    if delta_total <= 0:
        return None
    busy = max(0, min(delta_total, delta_total - delta_idle))
    return round((busy / delta_total) * 100.0, 2)

def _unavailable() -> dict[str, object]:
    return {"status": "unavailable"}


def _percent(used: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round((used / total) * 100.0, 2)


def _cpu() -> dict[str, object]:
    result: dict[str, object] = {"status": "available"}
    cores = os.cpu_count()
    if cores is not None and cores >= 0:
        result["logical_cores"] = cores
    usage_percent = _cpu_usage_percent()
    if usage_percent is not None:
        result["usage_percent"] = usage_percent
    try:
        load1, load5, load15 = os.getloadavg()
        result.update({"load_1m": load1, "load_5m": load5, "load_15m": load15})
    except (AttributeError, OSError):
        pass
    return result


def _memory() -> dict[str, object]:
    try:
        values: dict[str, int] = {}
        for raw in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, tail = raw.partition(":")
            if key not in {"MemTotal", "MemAvailable", "MemFree"}:
                continue
            token = tail.strip().split()[0]
            values[key] = int(token) * 1024
        total = values["MemTotal"]
        available = values.get("MemAvailable", values.get("MemFree"))
        if available is None:
            return _unavailable()
        used = max(0, total - available)
        result: dict[str, object] = {
            "status": "available",
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": available,
        }
        used_percent = _percent(used, total)
        if used_percent is not None:
            result["used_percent"] = used_percent
        return result
    except (OSError, ValueError, KeyError, IndexError):
        return _unavailable()


def _filesystem(path: str | os.PathLike[str]) -> dict[str, object]:
    try:
        stats = os.statvfs(path)
        total = stats.f_frsize * stats.f_blocks
        free = stats.f_frsize * stats.f_bavail
        used = max(0, total - free)
        result: dict[str, object] = {
            "status": "available",
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
        }
        used_percent = _percent(used, total)
        if used_percent is not None:
            result["used_percent"] = used_percent
        return result
    except (OSError, ValueError):
        return _unavailable()


def _uptime() -> dict[str, object]:
    try:
        seconds = int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
        return {"status": "available", "seconds": max(0, seconds)}
    except (OSError, ValueError, IndexError):
        return _unavailable()


def collect_host_resources(filesystem_path: str | os.PathLike[str] = "/") -> dict[str, object]:
    """Best-effort, non-failing host telemetry for the Console snapshot contract."""

    resources = {
        "cpu": _cpu(),
        "memory": _memory(),
        "filesystem": _filesystem(filesystem_path),
        "uptime": _uptime(),
    }
    available = sum(item.get("status") == "available" for item in resources.values())
    if available == len(resources):
        status = "available"
    elif available:
        status = "partial"
    else:
        status = "unavailable"
    return {"status": status, **resources}
