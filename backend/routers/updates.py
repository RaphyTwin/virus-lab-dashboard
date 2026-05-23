import asyncio
import subprocess
import re
import json
import os
import urllib.request
import urllib.error
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from datetime import datetime, timezone
from typing import Any

router = APIRouter(tags=["updates"])

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")

# Cache: avoid hammering GitHub API (rate limit: 60 req/h unauthenticated)
_gh_cache: dict[str, dict] = {}   # repo -> {data, fetched_at}
_GH_CACHE_TTL = 300               # seconds


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"services": []}


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"Befehl nicht gefunden: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", "Timeout"


# ── apt: check upgradable packages ───────────────────────────────────────────

@router.get("/updates/apt")
def apt_check() -> dict[str, Any]:
    """Prüft verfügbare apt-Upgrades ohne zu installieren."""

    # Refresh package list first
    rc_update, _, err_update = _run(["sudo", "apt-get", "update", "-qq"], timeout=90)

    # Get upgradable list
    rc_list, out_list, err_list = _run(
        ["apt", "list", "--upgradable", "--quiet=2"],
        timeout=30
    )

    packages = []
    if rc_list == 0 and out_list.strip():
        for line in out_list.strip().splitlines():
            # Format: "package/repo version arch [upgradable from: old_ver]"
            line = line.strip()
            if not line or line.startswith("Listing"):
                continue
            try:
                pkg_part, rest = line.split("/", 1)
                parts = rest.split()
                new_ver = parts[1] if len(parts) > 1 else "?"
                old_ver_match = re.search(r"upgradable from: ([^\]]+)", line)
                old_ver = old_ver_match.group(1) if old_ver_match else "?"
                packages.append({
                    "name": pkg_part.strip(),
                    "current_version": old_ver,
                    "new_version": new_ver,
                })
            except (ValueError, IndexError):
                continue

    # Security-only count: rough heuristic via package names + sources
    security_count = sum(
        1 for p in packages
        if "security" in p.get("new_version", "").lower()
        or any(kw in p["name"] for kw in ["openssl", "openssh", "linux-image", "libc6", "sudo", "curl"])
    )

    return {
        "available": rc_list != -1,
        "update_ran": rc_update == 0,
        "update_error": err_update.strip() if rc_update != 0 else None,
        "package_count": len(packages),
        "security_estimate": security_count,
        "packages": packages,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── apt: run upgrade (streaming output) ──────────────────────────────────────

@router.post("/updates/apt/upgrade")
async def apt_upgrade():
    """Führt apt-get upgrade durch und streamt die Ausgabe zeilenweise."""

    async def _stream():
        proc = await asyncio.create_subprocess_exec(
            "sudo", "apt-get", "upgrade", "-y",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        yield "data: [STARTED]\n\n"
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                yield f"data: {line}\n\n"
        await proc.wait()
        rc = proc.returncode
        yield f"data: [DONE rc={rc}]\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── GitHub: fetch latest release ─────────────────────────────────────────────

def _fetch_github_release(repo: str) -> dict[str, Any] | None:
    """Fetches the latest release from GitHub API with simple caching."""
    now = datetime.now(timezone.utc).timestamp()

    cached = _gh_cache.get(repo)
    if cached and (now - cached["fetched_at"]) < _GH_CACHE_TTL:
        return cached["data"]

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "homeserver-dashboard/0.5",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            result = {
                "tag_name": data.get("tag_name", ""),
                "name": data.get("name", ""),
                "published_at": data.get("published_at", ""),
                "html_url": data.get("html_url", ""),
                "prerelease": data.get("prerelease", False),
                "body_excerpt": (data.get("body") or "")[:300],
            }
            _gh_cache[repo] = {"data": result, "fetched_at": now}
            return result
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return None


def _get_installed_version(svc: dict) -> str | None:
    """Runs the version_cmd for a service and extracts version via regex."""
    cmd = svc.get("version_cmd")
    regex = svc.get("version_regex")
    if not cmd:
        return None
    rc, out, _ = _run(cmd, timeout=10)
    if rc != 0 or not out.strip():
        return None
    if regex:
        m = re.search(regex, out.strip())
        return m.group(1) if m else out.strip().split()[-1]
    return out.strip().split()[-1]


def _version_compare(installed: str | None, latest: str | None) -> str:
    """Returns 'up-to-date', 'update-available', or 'unknown'."""
    if not installed or not latest:
        return "unknown"
    # Strip leading 'v'
    a = installed.lstrip("v")
    b = latest.lstrip("v")
    if a == b:
        return "up-to-date"
    try:
        from packaging.version import Version
        if Version(a) < Version(b):
            return "update-available"
        return "up-to-date"
    except Exception:
        # Fallback: simple string compare
        return "update-available" if a != b else "up-to-date"


# ── Services: check all configured services ───────────────────────────────────

@router.get("/updates/services")
def services_check() -> dict[str, Any]:
    """Prüft für alle konfigurierten Dienste ob Updates verfügbar sind."""
    config = _load_config()
    services_conf = config.get("services", [])

    results = []
    for svc in services_conf:
        repo = svc.get("github_repo", "")
        release = _fetch_github_release(repo) if repo else None
        latest_tag = release["tag_name"] if release else None
        installed = _get_installed_version(svc)
        status = _version_compare(installed, latest_tag)

        results.append({
            "id": svc.get("id"),
            "name": svc.get("name", svc.get("id")),
            "description": svc.get("description", ""),
            "github_repo": repo,
            "installed_version": installed,
            "latest_version": latest_tag,
            "latest_published_at": release["published_at"] if release else None,
            "release_url": release["html_url"] if release else None,
            "release_excerpt": release["body_excerpt"] if release else None,
            "status": status,    # "up-to-date" | "update-available" | "unknown"
            "prerelease": release["prerelease"] if release else False,
        })

    update_count = sum(1 for r in results if r["status"] == "update-available")

    return {
        "services": results,
        "update_count": update_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Services: add / remove (config management) ───────────────────────────────

@router.get("/updates/config")
def get_update_config() -> dict[str, Any]:
    """Gibt die aktuelle Konfiguration der zu überwachenden Dienste zurück."""
    config = _load_config()
    return {"services": config.get("services", [])}


from pydantic import BaseModel

class ServiceEntry(BaseModel):
    id: str
    name: str
    description: str = ""
    github_repo: str
    version_cmd: list[str] = []
    version_regex: str = ""


@router.post("/updates/config/services")
def add_service(entry: ServiceEntry) -> dict[str, Any]:
    """Fügt einen neuen Dienst zur Überwachung hinzu."""
    config = _load_config()
    services = config.get("services", [])

    # Update if exists, else append
    existing = next((i for i, s in enumerate(services) if s["id"] == entry.id), None)
    entry_dict = entry.model_dump()
    if existing is not None:
        services[existing] = entry_dict
    else:
        services.append(entry_dict)
    config["services"] = services

    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "services": services}


@router.delete("/updates/config/services/{service_id}")
def remove_service(service_id: str) -> dict[str, Any]:
    """Entfernt einen Dienst aus der Überwachung."""
    config = _load_config()
    services = config.get("services", [])
    config["services"] = [s for s in services if s["id"] != service_id]

    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True}
