from fastapi import APIRouter
import psutil

router = APIRouter()

@router.get("/disks")
def get_disks():
    partitions = psutil.disk_partitions(all=False)
    result = []
    for p in partitions:
        if p.device.startswith(("/dev/loop", "/boot", "/boot/efi")):
            continue
        try:
            usage = psutil.disk_usage(p.mountpoint)
            result.append({
                "device": p.device,
                "mountpoint": p.mountpoint,
                "fstype": p.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
            })
        except PermissionError:
            pass
    return result
