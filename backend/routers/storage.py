import subprocess
import re
import shutil
from fastapi import APIRouter
from datetime import datetime, timezone
from typing import Any

router = APIRouter(tags=["storage"])


# Eine Hilfsliste von Befehlen, die Root-Rechte benötigen
PRIVILEGED_COMMANDS = {"smartctl", "zpool"}

def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    # Wenn der Hauptbefehl Root-Rechte benötigt, "sudo" davor hängen
    if cmd and cmd[0] in PRIVILEGED_COMMANDS:
        cmd = ["sudo"] + cmd

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", "Timeout"


# ---------------------------------------------------------------------------
# Volume / disk usage
# ---------------------------------------------------------------------------

@router.get("/storage/volumes")
async def get_volumes() -> dict[str, Any]:
    """Disk usage of all mounted filesystems (df-based) + psutil."""
    import psutil

    partitions = psutil.disk_partitions(all=False)
    volumes = []
    for p in partitions:
        # skip pseudo-filesystems
        if p.fstype in ("", "tmpfs", "devtmpfs", "squashfs", "efivarfs"):
            continue
        try:
            usage = psutil.disk_usage(p.mountpoint)
        except PermissionError:
            continue
        volumes.append({
            "device": p.device,
            "mountpoint": p.mountpoint,
            "fstype": p.fstype,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent": round(usage.percent, 1),
        })

    return {"volumes": volumes, "timestamp": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# ZFS pool list
# ---------------------------------------------------------------------------

def _parse_zpool_list(output: str) -> list[dict]:
    pools = []
    for line in output.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 10:
            continue
        name, size, alloc, free, _, cap, frag, _, health, altroot = parts[:10]

        def _to_bytes(s: str) -> int | None:
            s = s.strip()
            if s in ("-", ""):
                return None
            multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
            m = re.match(r"^([\d.]+)([KMGTP]?)$", s, re.I)
            if not m:
                return None
            val = float(m.group(1))
            suffix = m.group(2).upper()
            return int(val * multipliers.get(suffix, 1))

        def _pct(s: str) -> float | None:
            s = s.strip().rstrip("%")
            try:
                return float(s)
            except ValueError:
                return None

        pools.append({
            "name": name,
            "size_bytes": _to_bytes(size),
            "alloc_bytes": _to_bytes(alloc),
            "free_bytes": _to_bytes(free),
            "capacity_percent": _pct(cap),
            "fragmentation_percent": _pct(frag),
            "health": health.strip(),
            "altroot": altroot.strip() if altroot.strip() != "-" else None,
        })
    return pools


@router.get("/storage/zfs/pools")
async def get_zfs_pools() -> dict[str, Any]:
    rc, out, err = _run(["zpool", "list", "-H", "-p", "-o",
                         "name,size,alloc,free,expandsize,cap,frag,dedup,health,altroot"])
    if rc != 0:
        return {"available": False, "error": err, "pools": []}
    return {
        "available": True,
        "pools": _parse_zpool_list(out),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# ZFS pool status (degraded / errors)
# ---------------------------------------------------------------------------

def _parse_zpool_status(output: str) -> list[dict]:
    """Parse `zpool status` output into per-pool dicts."""
    pools: list[dict] = []
    current: dict | None = None

    for line in output.splitlines():
        stripped = line.strip()

        if stripped.startswith("pool:"):
            if current:
                pools.append(current)
            current = {
                "name": stripped[5:].strip(),
                "state": None,
                "status": None,
                "action": None,
                "scan": None,
                "errors": None,
                "vdevs_raw": [],
            }

        elif current is None:
            continue

        elif stripped.startswith("state:"):
            current["state"] = stripped[6:].strip()

        elif stripped.startswith("status:"):
            current["status"] = stripped[7:].strip()

        elif stripped.startswith("action:"):
            current["action"] = stripped[7:].strip()

        elif stripped.startswith("scan:"):
            current["scan"] = stripped[5:].strip()

        elif stripped.startswith("errors:"):
            current["errors"] = stripped[7:].strip()

        elif stripped and not stripped.startswith("config:") and not stripped.startswith("NAME"):
            current["vdevs_raw"].append(stripped)

    if current:
        pools.append(current)

    return pools


def _parse_scrub_info(scan_line: str | None) -> dict:
    if not scan_line:
        return {"has_scrub": False}

    scrub_m = re.search(
        r"scrub repaired (\S+) in ([\d:]+) with (\d+) errors? on (.+)",
        scan_line, re.I
    )
    if scrub_m:
        return {
            "has_scrub": True,
            "repaired": scrub_m.group(1),
            "duration": scrub_m.group(2),
            "errors": int(scrub_m.group(3)),
            "finished_at": scrub_m.group(4).strip(),
        }

    # scrub in progress
    prog_m = re.search(r"scrub in progress", scan_line, re.I)
    if prog_m:
        return {"has_scrub": True, "in_progress": True, "raw": scan_line}

    return {"has_scrub": True, "raw": scan_line}


@router.get("/storage/zfs/status")
async def get_zfs_status() -> dict[str, Any]:
    rc, out, err = _run(["zpool", "status", "-v"])
    if rc != 0:
        return {"available": False, "error": err, "pools": []}

    pools = _parse_zpool_status(out)
    enriched = []
    for p in pools:
        scrub = _parse_scrub_info(p.get("scan"))
        enriched.append({
            **p,
            "scrub": scrub,
            "degraded": p.get("state", "").upper() != "ONLINE",
            "has_errors": p.get("errors", "").lower() not in ("no known data errors", ""),
        })

    return {
        "available": True,
        "pools": enriched,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# ZFS scrub timer / cron check
# ---------------------------------------------------------------------------

@router.get("/storage/zfs/scrub-schedule")
async def get_scrub_schedule() -> dict[str, Any]:
    result: dict[str, Any] = {"systemd_timer": None, "cron": None}

    # Check systemd timer
    rc, out, _ = _run(["systemctl", "list-timers", "--all", "--no-pager"])
    if rc == 0:
        for line in out.splitlines():
            if "scrub" in line.lower() or "zfs" in line.lower():
                result["systemd_timer"] = {"active": True, "line": line.strip()}
                break
        if result["systemd_timer"] is None:
            result["systemd_timer"] = {"active": False}

    # Check crontab for scrub entries
    cron_hits = []
    for cmd in [["crontab", "-l"], ["cat", "/etc/cron.d/zfs-auto-scrub"]]:
        rc2, out2, _ = _run(cmd)
        if rc2 == 0:
            for line in out2.splitlines():
                if "scrub" in line.lower() and not line.strip().startswith("#"):
                    cron_hits.append(line.strip())
    result["cron"] = cron_hits if cron_hits else None

    result["scrub_scheduled"] = bool(
        (result["systemd_timer"] or {}).get("active") or result["cron"]
    )
    return result


# ---------------------------------------------------------------------------
# S.M.A.R.T.
# ---------------------------------------------------------------------------

def _parse_smart_attributes(output: str) -> dict:
    attrs: dict[str, Any] = {}
    section = False
    for line in output.splitlines():
        if "ID#" in line and "ATTRIBUTE_NAME" in line:
            section = True
            continue
        if section:
            parts = line.split()
            if len(parts) < 10:
                section = False
                continue
            attr_id = parts[0]
            name = parts[1]
            flags = parts[2]
            value = parts[3]
            worst = parts[4]
            thresh = parts[5]
            raw = parts[9]
            attrs[name] = {
                "id": attr_id,
                "value": int(value) if value.isdigit() else value,
                "worst": int(worst) if worst.isdigit() else worst,
                "thresh": int(thresh) if thresh.isdigit() else thresh,
                "raw": raw,
                "failing": value.isdigit() and thresh.isdigit() and int(value) <= int(thresh),
            }
    return attrs


@router.get("/storage/smart")
async def get_smart() -> dict[str, Any]:
    if not shutil.which("smartctl"):
        return {"available": False, "error": "smartctl not found", "disks": []}

    rc, out, _ = _run(["lsblk", "-dno", "NAME,TYPE,ROTA"])
    disks_raw = []
    if rc == 0:
        for line in out.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "disk":
                disks_raw.append({"name": parts[0], "rotational": parts[2] == "1" if len(parts) > 2 else None})

    results = []
    for disk in disks_raw:
        dev = f"/dev/{disk['name']}"

        # Overall health
        rc_h, out_h, _ = _run(["smartctl", "-H", dev])
        health_ok = "PASSED" in out_h or "OK" in out_h

        # Full attributes
        rc_a, out_a, _ = _run(["smartctl", "-A", dev])
        attrs = _parse_smart_attributes(out_a) if rc_a == 0 else {}

        # Device info
        rc_i, out_i, _ = _run(["smartctl", "-i", dev])
        model = None
        serial = None
        hours = None
        smart_enabled = None
        for line in out_i.splitlines():
            if "Device Model" in line or "Model Number" in line:
                model = line.split(":", 1)[1].strip()
            if "Serial Number" in line:
                serial = line.split(":", 1)[1].strip()
            if "SMART support is:" in line:
                smart_enabled = "Enabled" in line

        # Power-on hours from attributes
        if "Power_On_Hours" in attrs:
            hours = attrs["Power_On_Hours"].get("raw")
        elif "Power_On_Hours_and_Msec" in attrs:
            hours = attrs["Power_On_Hours_and_Msec"].get("raw")

        # Key indicators
        reallocated = attrs.get("Reallocated_Sector_Ct", {}).get("raw", "0")
        pending = attrs.get("Current_Pending_Sector", {}).get("raw", "0")
        uncorrectable = attrs.get("Offline_Uncorrectable", {}).get("raw", "0")

        results.append({
            "device": dev,
            "model": model,
            "serial": serial,
            "rotational": disk["rotational"],
            "smart_enabled": smart_enabled,
            "health_ok": health_ok,
            "power_on_hours": hours,
            "reallocated_sectors": reallocated,
            "pending_sectors": pending,
            "uncorrectable_sectors": uncorrectable,
            "attributes": attrs,
        })

    return {
        "available": True,
        "disks": results,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# TRIM status
# ---------------------------------------------------------------------------

@router.get("/storage/trim")
async def get_trim_status() -> dict[str, Any]:
    result: dict[str, Any] = {"systemd_timer": None, "last_run": None, "journal": None}

    # systemd fstrim timer
    rc, out, _ = _run(["systemctl", "status", "fstrim.timer"])
    if rc in (0, 3):  # 3 = inactive but exists
        active = "active" in out.lower() and "inactive" not in out.lower()
        for line in out.splitlines():
            if "Trigger:" in line or "Last trigger:" in line.lower() or "Triggered" in line:
                result["systemd_timer"] = {"active": active, "detail": line.strip()}
                break
        if not result["systemd_timer"]:
            result["systemd_timer"] = {"active": active, "detail": None}

    # last fstrim from journalctl
    rc2, out2, _ = _run(["journalctl", "-u", "fstrim", "--no-pager", "-n", "20", "--output", "short-iso"])
    if rc2 == 0 and out2.strip():
        lines = [l for l in out2.strip().splitlines() if l and not l.startswith("--")]
        result["journal"] = lines[-5:] if lines else None
        # Try to parse the date of the last run
        if lines:
            date_m = re.match(r"^(\d{4}-\d{2}-\d{2}T[\d:+]+)", lines[-1])
            if date_m:
                result["last_run"] = date_m.group(1)

    # Also check if fstrim is enabled via /etc/fstab DISCARD option
    discard_mounts = []
    try:
        with open("/etc/fstab") as f:
            for line in f:
                if "discard" in line and not line.strip().startswith("#"):
                    discard_mounts.append(line.strip())
    except FileNotFoundError:
        pass
    result["fstab_discard_mounts"] = discard_mounts

    return result
