"""Everyday reliability: app version, crash marker, project locks (one editing tab per project),
disk space checks, recent exports (+ re-export), unused-media cleanup."""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import config

router = APIRouter(tags=["system"])
VERSION = (config.ROOT / "VERSION").read_text(encoding="utf-8").strip() if (config.ROOT / "VERSION").exists() else "dev"
RUN_MARKER = config.DATA_ROOT / ".ligxt-running"
EXPORT_META = config.EXPORTS_DIR / ".ligxt"
_state = {"recovered": False}


# ---------------------------------------------------------------- crash marker
def mark_started() -> None:
    """A marker that only a clean shutdown removes: if it is already there, the last run crashed."""
    _state["recovered"] = RUN_MARKER.exists()
    RUN_MARKER.write_text(str(time.time()), encoding="utf-8")
    # exports interrupted by a crash leave .part files behind
    for f in config.EXPORTS_DIR.glob("*.part.mp4"):
        f.unlink(missing_ok=True)


def mark_stopped() -> None:
    RUN_MARKER.unlink(missing_ok=True)


@router.get("/api/version")
def version():
    return {"version": VERSION, "recoveredFromCrash": _state["recovered"]}


# ---------------------------------------------------------------- project locks
LOCK_TTL = 25.0  # seconds without a heartbeat before a lock is free again
_locks: dict[str, dict] = {}
_lock_mu = threading.Lock()


class LockBody(BaseModel):
    client: str
    force: bool = False
    label: str = ""


def lock_owner(pid: str) -> dict | None:
    with _lock_mu:
        lk = _locks.get(pid)
        if lk and time.time() - lk["seen"] > LOCK_TTL:
            _locks.pop(pid, None)
            return None
        return dict(lk) if lk else None


def check_lock(pid: str, client: str | None) -> None:
    """Saves from a tab that doesn't hold the project's lock are refused (another tab is editing)."""
    lk = lock_owner(pid)
    if lk and client and lk["client"] != client:
        raise HTTPException(409, "This project is being edited in another tab or window, so this tab didn't save. "
                                 "Close the other one, or click 'Edit here' in this tab to take over.")


@router.post("/api/projects/{pid}/lock")
def take_lock(pid: str, body: LockBody):
    with _lock_mu:
        lk = _locks.get(pid)
        alive = lk and time.time() - lk["seen"] <= LOCK_TTL
        if alive and lk["client"] != body.client and not body.force:
            raise HTTPException(409, json.dumps({"owner": lk["label"] or "another tab", "since": lk["since"]}))
        if not alive or lk["client"] != body.client:
            _locks[pid] = {"client": body.client, "label": body.label, "since": time.time(), "seen": time.time()}
        else:
            lk["seen"] = time.time()
    return {"ok": True, "ttl": LOCK_TTL}


@router.post("/api/projects/{pid}/unlock")
def release_lock(pid: str, body: LockBody):
    with _lock_mu:
        lk = _locks.get(pid)
        if lk and lk["client"] == body.client:
            _locks.pop(pid, None)
    return {"ok": True}


# ---------------------------------------------------------------- disk space
def free_bytes(path: Path) -> int:
    p = path
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def require_space(path: Path, needed: int, what: str) -> None:
    free = free_bytes(path)
    margin = 300 * 1024 * 1024  # never fill the disk to the last byte
    if free < needed + margin:
        drive = Path(path).anchor or str(path)
        raise HTTPException(507, f"Not enough disk space for {what}: it needs about {fmt_bytes(needed + margin)} and only "
                                 f"{fmt_bytes(free)} is free on {drive}. Free some space (Home > Media cleanup can help) and try again.")


@router.get("/api/system/disk")
def disk():
    return {"free": free_bytes(config.DATA_ROOT), "freeText": fmt_bytes(free_bytes(config.DATA_ROOT))}


# ---------------------------------------------------------------- recent exports
def record_export(out: Path, project, mode: str, range_start, range_end) -> None:
    EXPORT_META.mkdir(parents=True, exist_ok=True)
    info = {"file": out.name, "projectId": project.id, "projectName": project.name, "mode": mode,
            "range": [range_start, range_end] if range_start is not None or range_end is not None else None,
            "export": project.export.model_dump(mode="json"), "time": time.time()}
    (EXPORT_META / (out.name + ".json")).write_text(json.dumps(info), encoding="utf-8")


@router.get("/api/exports")
def recent_exports(limit: int = 30):
    files = sorted((f for f in config.EXPORTS_DIR.glob("*.mp4") if not f.name.endswith(".part.mp4")),
                   key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    out = []
    for f in files:
        side = EXPORT_META / (f.name + ".json")
        info = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
        out.append({"file": f.name, "path": str(f), "url": f"/files/exports/{f.name}", "size": f.stat().st_size,
                    "time": f.stat().st_mtime, "projectId": info.get("projectId"), "projectName": info.get("projectName"),
                    "preset": (info.get("export") or {}).get("preset"), "mode": info.get("mode"), "canReexport": bool(info)})
    return {"exports": out}


@router.post("/api/exports/{name}/reexport")
def reexport(name: str):
    """Export the project again (its current, saved state) with the settings of an earlier export."""
    from .models import ExportSettings
    from .projects import load_project
    from .render import RenderError, run_export
    side = EXPORT_META / (Path(name).name + ".json")
    if not side.exists():
        raise HTTPException(404, "No settings were recorded for that export (it was made before v1.0).")
    info = json.loads(side.read_text(encoding="utf-8"))
    try:
        project = load_project(info["projectId"])
    except HTTPException as e:
        raise HTTPException(404, "The project of that export no longer exists.") from e
    project.export = ExportSettings.model_validate(info["export"])
    r = info.get("range") or [None, None]
    try:
        job = run_export(project, info.get("mode", "final"), r[0], r[1])
    except RenderError as e:
        raise HTTPException(400, str(e)) from e
    return {"jobId": job.id}


# ---------------------------------------------------------------- unused media
def _used_media() -> set[str]:
    used: set[str] = set()
    for pj in config.PROJECTS_DIR.glob("*/project.json"):
        try:
            p = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        used.update(p.get("media") or [])
        for t in p.get("tracks") or []:
            for c in t.get("clips") or []:
                if c.get("mediaId"):
                    used.add(c["mediaId"])
                bgm = ((c.get("cutout") or {}).get("background") or {}).get("mediaId")
                if bgm:
                    used.add(bgm)  # a cutout's replacement background
    return used


def _dir_size(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.exists() else 0


@router.get("/api/media-cleanup")
def unused_media():
    used = _used_media()
    out = []
    for mj in config.MEDIA_DIR.glob("*/meta.json"):
        mid = mj.parent.name
        if mid in used:
            continue
        try:
            m = json.loads(mj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            m = {"name": mid}
        size = _dir_size(mj.parent) + _dir_size(config.CACHE_DIR / mid)
        kind = "template card" if m.get("placeholder") else "sound effect" if m.get("sfx") else m.get("type", "file")
        out.append({"id": mid, "name": m.get("name", mid), "type": kind, "size": size, "imported": m.get("imported", 0),
                    "thumbnail": m.get("thumbnail")})
    out.sort(key=lambda x: -x["size"])
    return {"media": out, "total": sum(x["size"] for x in out)}


class CleanupBody(BaseModel):
    ids: list[str]


@router.post("/api/media-cleanup")
def delete_unused(body: CleanupBody):
    used = _used_media()
    removed, freed = 0, 0
    for mid in body.ids:
        if mid in used or not mid.isalnum():
            continue  # never delete something a project uses (it may have changed since the list was made)
        for d in (config.MEDIA_DIR / mid, config.CACHE_DIR / mid):
            freed += _dir_size(d)
            shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return {"removed": removed, "freed": freed}
