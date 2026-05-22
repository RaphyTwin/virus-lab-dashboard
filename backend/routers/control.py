import asyncio
import subprocess
import shutil
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
from typing import Any, Optional

router = APIRouter(tags=["control"])

# ── Module-level Tailscale auto-off state ────────────────────────────────────
_tailscale_timer_task: Optional[asyncio.Task] = None
_tailscale_timer_end: Optional[float] = None   # unix timestamp when it fires


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"Befehl nicht gefunden: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", "Timeout"


# ── Shutdown ──────────────────────────────────────────────────────────────────

@router.post("/control/shutdown")
async def shutdown() -> dict[str, Any]:
    """Fährt den Server herunter. Braucht root / sudo."""
    rc, _, err = _run(["shutdown", "-h", "now"])
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "Shutdown fehlgeschlagen")
    return {"ok": True, "message": "Shutdown eingeleitet"}


# ── Reboot ────────────────────────────────────────────────────────────────────

@router.post("/control/reboot")
async def reboot() -> dict[str, Any]:
    """Startet den Server neu. Braucht root / sudo."""
    rc, _, err = _run(["reboot"])
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "Reboot fehlgeschlagen")
    return {"ok": True, "message": "Neustart eingeleitet"}


# ── Tailscale: Status ─────────────────────────────────────────────────────────

@router.get("/control/tailscale/status")
async def tailscale_status() -> dict[str, Any]:
    global _tailscale_timer_task, _tailscale_timer_end

    if not shutil.which("tailscale"):
        return {"available": False}

    rc, out, err = _run(["tailscale", "status", "--json"])
    if rc != 0:
        return {"available": True, "connected": False, "error": err}

    connected = False
    state = "unknown"
    self_ip = None
    self_name = None
    peer_count = 0

    try:
        data = json.loads(out)
        state = data.get("BackendState", "unknown")
        connected = state == "Running"
        if "Self" in data and data["Self"]:
            ips = data["Self"].get("TailscaleIPs") or []
            self_ip = ips[0] if ips else None
            self_name = data["Self"].get("HostName")
        peer_count = len(data.get("Peer", {}))
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # Remaining auto-off seconds
    timer_remaining: Optional[int] = None
    if (
        _tailscale_timer_task
        and not _tailscale_timer_task.done()
        and _tailscale_timer_end is not None
    ):
        remaining = _tailscale_timer_end - datetime.now(timezone.utc).timestamp()
        timer_remaining = max(0, int(remaining))

    return {
        "available": True,
        "connected": connected,
        "state": state,
        "ip": self_ip,
        "hostname": self_name,
        "peer_count": peer_count,
        "auto_off_remaining_seconds": timer_remaining,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Tailscale: Up ─────────────────────────────────────────────────────────────

@router.post("/control/tailscale/up")
async def tailscale_up() -> dict[str, Any]:
    rc, out, err = _run(["tailscale", "up", "--accept-routes"], timeout=20)
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or out or "tailscale up fehlgeschlagen")
    return {"ok": True}


# ── Tailscale: Down ───────────────────────────────────────────────────────────

@router.post("/control/tailscale/down")
async def tailscale_down() -> dict[str, Any]:
    global _tailscale_timer_task, _tailscale_timer_end
    # Cancel any running auto-off timer first
    if _tailscale_timer_task and not _tailscale_timer_task.done():
        _tailscale_timer_task.cancel()
    _tailscale_timer_task = None
    _tailscale_timer_end = None

    rc, out, err = _run(["tailscale", "down"])
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or out or "tailscale down fehlgeschlagen")
    return {"ok": True}


# ── Tailscale: Auto-Off Timer ─────────────────────────────────────────────────

class AutoOffRequest(BaseModel):
    minutes: int = 30


@router.post("/control/tailscale/auto-off")
async def tailscale_auto_off(req: AutoOffRequest) -> dict[str, Any]:
    """Schaltet Tailscale nach N Minuten automatisch ab."""
    global _tailscale_timer_task, _tailscale_timer_end

    if not 1 <= req.minutes <= 480:
        raise HTTPException(status_code=400, detail="minutes muss zwischen 1 und 480 liegen")

    # Cancel any existing timer
    if _tailscale_timer_task and not _tailscale_timer_task.done():
        _tailscale_timer_task.cancel()

    seconds = req.minutes * 60
    _tailscale_timer_end = datetime.now(timezone.utc).timestamp() + seconds

    async def _auto_off():
        await asyncio.sleep(seconds)
        _run(["tailscale", "down"])

    _tailscale_timer_task = asyncio.create_task(_auto_off())

    return {
        "ok": True,
        "auto_off_in_seconds": seconds,
        "minutes": req.minutes,
    }


# ── Tailscale: Cancel Auto-Off ────────────────────────────────────────────────

@router.post("/control/tailscale/cancel-auto-off")
async def tailscale_cancel_auto_off() -> dict[str, Any]:
    global _tailscale_timer_task, _tailscale_timer_end
    if _tailscale_timer_task and not _tailscale_timer_task.done():
        _tailscale_timer_task.cancel()
    _tailscale_timer_task = None
    _tailscale_timer_end = None
    return {"ok": True}
