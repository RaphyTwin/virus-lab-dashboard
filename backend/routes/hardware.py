from fastapi import APIRouter
import psutil

router = APIRouter()

@router.get("/hardware")
def get_hardware():
    memory = psutil.virtual_memory()
    cpu_percent = psutil.cpu_percent()
    temperatures: dict = {}
    try:
        raw = psutil.sensors_temperatures()
        for chip_name, entries in raw.items():
            temperatures[chip_name] = [
                {
                    "label": e.label if e.label else f"sensor {i}",
                    "current": round(e.current, 1),
                    "high": round(e.high, 1) if e.high else None,
                    "critical": round(e.critical, 1) if e.critical else None
                }
                for i, e in enumerate(entries)
            ]
    except Exception:
        pass

    return {
        "ram": {
            "total": memory.total,
            "used": memory.used,
            "available": memory.available,
            "percent": memory.percent,
        },
        "cpu_percent": cpu_percent,
        "temperatures": temperatures,
    }