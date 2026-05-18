from fastapi import APIRouter, HTTPException
import subprocess

router = APIRouter()

@router.post("/system/poweroff")
def poweroff():
    try:
        subprocess.Popen(["sudo", "poweroff"])
        return {"status": "ok", "message": "Shutdown initialized"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/system/reboot")
def reboot():
    try:
        subprocess.Popen(["sudo", "reboot"])
        return {"status": "ok", "message": "Reboot initialized"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
