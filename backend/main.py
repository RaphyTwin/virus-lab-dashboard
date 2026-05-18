from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse
from pathlib import Path

from routes import disks, hardware, system

app = FastAPI(title="VirusLab Dashboard")

app.include_router(disks.router, prefix="/api")
app.include_router(hardware.router, prefix="/api")
app.include_router(system.router, prefix="/api")

frontend_path = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")

@app.get("/")
def root():
    return FileResponse(str(frontend_path / "index.html"))

@app.get("/api/ping")
def ping():
    return {"status": "ok"}
