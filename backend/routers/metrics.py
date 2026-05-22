import psutil
import subprocess
import platform
import json
from fastapi import APIRouter
from datetime import datetime, timezone
from typing import Any

router = APIRouter(tags=["metrics"])


def _uptime_seconds() -> int:
    boot_ts = psutil.boot_time()
    now_ts = datetime.now(timezone.utc).timestamp()
    return int(now_ts - boot_ts)


@router.get("/metrics/system")
def get_system_metrics() -> dict[str, Any]:
    """CPU, RAM, uptime, hostname — public endpoint."""

    # CPU (short interval to avoid blocking)
    cpu_percent = psutil.cpu_percent(interval=0.3)
    cpu_freq = psutil.cpu_freq()
    cpu_count_logical = psutil.cpu_count(logical=True)
    cpu_count_physical = psutil.cpu_count(logical=False)

    # Per-core usage
    per_core = psutil.cpu_percent(interval=0, percpu=True)

    # RAM
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Load average (Unix only)
    try:
        load_avg = list(psutil.getloadavg())
    except AttributeError:
        load_avg = None

    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "uptime_seconds": _uptime_seconds(),
        "cpu": {
            "percent": round(cpu_percent, 1),
            "cores_logical": cpu_count_logical,
            "cores_physical": cpu_count_physical,
            "freq_mhz": round(cpu_freq.current, 0) if cpu_freq else None,
            "freq_max_mhz": round(cpu_freq.max, 0) if cpu_freq and cpu_freq.max else None,
            "per_core": [round(p, 1) for p in per_core],
            "load_avg_1_5_15": [round(l, 2) for l in load_avg] if load_avg else None,
        },
        "ram": {
            "total_bytes": mem.total,
            "used_bytes": mem.used,
            "available_bytes": mem.available,
            "cached_bytes": getattr(mem, "cached", 0),
            "buffers_bytes": getattr(mem, "buffers", 0),
            "percent": round(mem.percent, 1),
        },
        "swap": {
            "total_bytes": swap.total,
            "used_bytes": swap.used,
            "percent": round(swap.percent, 1),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/metrics/temperatures")
def get_temperatures() -> dict[str, Any]:
    """All available temperature sensors — admin endpoint."""

    temps: dict[str, list] = {}

    # psutil sensor data
    try:
        raw = psutil.sensors_temperatures()
        for chip, readings in raw.items():
            temps[chip] = [
                {
                    "label": r.label if r.label else f"core_{i}",
                    "current": round(r.current, 1),
                    "high": round(r.high, 1) if r.high else None,
                    "critical": round(r.critical, 1) if r.critical else None,
                }
                for i, r in enumerate(readings)
            ]
    except AttributeError:
        pass

    # Try `sensors -j` for richer data (requires lm-sensors)
    sensors_json = None
    try:
        result = subprocess.run(
            ["sensors", "-j"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            sensors_json = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # HDD temperatures via smartctl (needs sudo or setuid)
    disk_temps = []
    try:
        lsblk = subprocess.run(
            ["lsblk", "-dno", "NAME,TYPE"],
            capture_output=True, text=True, timeout=5
        )
        disks = [
            line.split()[0]
            for line in lsblk.stdout.strip().splitlines()
            if "disk" in line
        ]
        for disk in disks:
            try:
                smart = subprocess.run(
                    ["smartctl", "-A", f"/dev/{disk}"],
                    capture_output=True, text=True, timeout=5
                )
                for line in smart.stdout.splitlines():
                    if "Temperature_Celsius" in line or "Airflow_Temperature" in line:
                        parts = line.split()
                        temp_val = int(parts[9]) if len(parts) > 9 else None
                        if temp_val is not None:
                            disk_temps.append({
                                "device": disk,
                                "label": parts[1],
                                "current": temp_val,
                            })
            except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
                pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return {
        "psutil": temps,
        "sensors_json": sensors_json,
        "disk_temps": disk_temps,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}
