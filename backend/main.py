from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

from routers import metrics, storage

app = FastAPI(title="VirusLab Dashboard")

app.include_router(metrics.router, prefix="/api")
app.include_router(storage.router, prefix="/api")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def serve_frontend():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/api/ping")
def ping():
    return {"status": "ok"}
