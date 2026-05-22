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
            filtered_entries = []
            for i, e in enumerate(entries):
                if e.current <= 0:
                    continue

                label = e.label if e.label else f"sensor {i}"

                if chip_name == "applesmc":
                    if label.startswith("TA"):
                        label = f"Ambient ({label})"
                    elif label.startswith("TC"):
                        label = f"CPU Prox ({label})"
                    elif label.startswith("TM"):
                        label = f"Memory ({label})"

                filtered_entries.append({
                    "label": label,
                    "current": round(e.current, 1),
                    "high": round(e.high, 1) if e.high else None,
                    "critical": round(e.critical, 1) if e.critical else None
                })

            # Only add if the chip still has sensors left after filtering
            if filtered_entries:
                temperatures[chip_name] = filtered_entries

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