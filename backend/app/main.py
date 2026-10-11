"""Ligxt Video Editor - FastAPI entry point."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, media, projects, render
from .ffmpeg_utils import ffmpeg_version, has_nvenc
from .jobs import jobs

log = logging.getLogger("ligxt")


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=has_nvenc, daemon=True).start()  # warm the encoder check
    if config.OPEN_BROWSER:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{config.PORT}")).start()
    print(f"Ligxt Video Editor running at http://127.0.0.1:{config.PORT}", flush=True)
    yield
    jobs.shutdown()


app = FastAPI(title="Ligxt Video Editor", lifespan=lifespan)
app.include_router(media.router)
app.include_router(projects.router)
app.include_router(render.router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"Server error: {type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------- system
@app.get("/api/health")
def health():
    return {"ok": True, "app": "Ligxt Video Editor"}


@app.get("/api/system")
def system_info():
    return {"ffmpeg": ffmpeg_version(), "nvenc": has_nvenc(), "root": str(config.DATA_ROOT),
            "exportsDir": str(config.EXPORTS_DIR)}


class OpenBody(BaseModel):
    path: str | None = None  # a file inside the app folder to highlight; default: exports folder


@app.post("/api/system/open-folder")
def open_folder(body: OpenBody):
    target = Path(body.path).resolve() if body.path else config.EXPORTS_DIR
    if config.DATA_ROOT not in target.parents and target != config.DATA_ROOT:
        raise HTTPException(400, "Can only open folders inside the Ligxt folder.")
    if not target.exists():
        raise HTTPException(404, f"Not found: {target}")
    if os.name != "nt":
        raise HTTPException(501, "Opening folders is only supported on Windows.")
    if target.is_file():
        subprocess.Popen(["explorer", "/select,", str(target)])
    else:
        os.startfile(str(target))  # type: ignore[attr-defined]
    return {"ok": True}


# ---------------------------------------------------------------- jobs
@app.get("/api/jobs")
def list_jobs():
    return {"jobs": jobs.list()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job not found.")
    return j.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    if not jobs.cancel(job_id):
        raise HTTPException(409, "That job is not running.")
    return {"ok": True}


@app.get("/api/events")
async def events(request: Request):
    """Server-sent events: a snapshot of all jobs, then every change as it happens."""

    async def stream():
        version = jobs.version
        yield f"event: snapshot\ndata: {json.dumps({'jobs': jobs.list()})}\n\n"
        last_beat = time.time()
        while True:
            if await request.is_disconnected():
                break
            new_version = await asyncio.to_thread(jobs.wait_for_change, version, 2.0)
            if new_version != version:
                changed = jobs.changed_since(version)
                version = new_version
                for j in changed:
                    yield f"event: job\ndata: {json.dumps(j)}\n\n"
            elif time.time() - last_beat > 15:
                last_beat = time.time()
                yield ": keepalive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- files + UI
app.mount("/files/media", StaticFiles(directory=config.MEDIA_DIR), name="media-files")
app.mount("/files/cache", StaticFiles(directory=config.CACHE_DIR), name="cache-files")
app.mount("/files/exports", StaticFiles(directory=config.EXPORTS_DIR), name="export-files")
app.mount("/files/projects", StaticFiles(directory=config.PROJECTS_DIR), name="project-files")
app.mount("/files/templates", StaticFiles(directory=config.TEMPLATES_DIR), name="template-files")

if (config.FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "Unknown API route.")
    f = (config.FRONTEND_DIST / path).resolve()
    if path and f.is_file() and config.FRONTEND_DIST in f.parents:
        return FileResponse(f)
    index = config.FRONTEND_DIST / "index.html"
    if not index.exists():
        return JSONResponse(status_code=503, content={
            "detail": "The UI is not built yet. Run setup.ps1 (or 'npm run build' in frontend\\)."})
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
