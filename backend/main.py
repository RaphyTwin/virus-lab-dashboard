from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from routers import metrics, storage

app = FastAPI(title="VirusLab Dashboard")

app.include_router(metrics.router, prefix="/api")
app.include_router(storage.router, prefix="/api")

frontend_path = Path(__file__).parent.parent / "static" # change to "frontend" in near future
app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")

@app.get("/")
async def serve_frontend():
    return FileResponse(str(frontend_path / "index.html"))

@app.get("/api/ping")
def ping():
    return {"status": "ok"}
