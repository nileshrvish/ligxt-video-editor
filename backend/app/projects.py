"""Project storage: save/load, recent list, autosave (keeps the last N)."""
from __future__ import annotations

from .fsutil import replace_retry
from .system_api import check_lock

import json
import re
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, ValidationError

from .config import AUTOSAVE_KEEP, PROJECTS_DIR
from .media import load_meta
from .models import ASPECTS, Project, new_project

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _pid(pid: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{12}", pid or ""):
        raise HTTPException(400, f"Invalid project id: {pid!r}")
    return pid


def project_dir(pid: str) -> Path:
    return PROJECTS_DIR / pid


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    replace_retry(tmp, path)


def load_project(pid: str) -> Project:
    p = project_dir(_pid(pid)) / "project.json"
    if not p.exists():
        raise HTTPException(404, "Project not found. It may have been deleted.")
    try:
        return Project.model_validate_json(p.read_text(encoding="utf-8"))
    except ValidationError as e:
        raise HTTPException(500, f"Project file is damaged: {e.errors()[0].get('msg')}. "
                                 f"Try restoring an autosave.") from e


def save_project(project: Project) -> Project:
    project.updated = time.time()
    _write_json(project_dir(project.id) / "project.json", project.model_dump(mode="json"))
    return project


def _project_thumb(data: dict) -> str | None:
    """Thumbnail of the first clip on the main track, falling back to the first imported media."""
    main = next((t for t in data.get("tracks", []) if t.get("type") == "video"), {})
    clips = sorted(main.get("clips", []), key=lambda c: c.get("start", 0))
    ids = [c.get("mediaId") for c in clips if c.get("mediaId")] + list(data.get("media", []))
    for mid in ids[:5]:
        if isinstance(mid, str) and re.fullmatch(r"[a-f0-9]{12}", mid):
            meta = load_meta(mid)
            if meta and meta.get("thumbnail"):
                return meta["thumbnail"]
    return None


class CreateBody(BaseModel):
    name: str = "Untitled project"
    aspect: str = "9:16"


@router.get("")
def list_projects():
    out = []
    for d in PROJECTS_DIR.iterdir():
        p = d / "project.json"
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            out.append({"id": d.name, "name": f"{d.name} (damaged)", "updated": p.stat().st_mtime, "damaged": True})
            continue
        clips = sum(len(t.get("clips", [])) for t in data.get("tracks", []))
        duration = max((c.get("start", 0) + (c.get("outPoint", 0) - c.get("inPoint", 0)) / (c.get("speed") or 1)
                        for t in data.get("tracks", []) for c in t.get("clips", [])), default=0)
        out.append({
            "id": data.get("id", d.name), "name": data.get("name", "Untitled"),
            "updated": data.get("updated", p.stat().st_mtime), "created": data.get("created"),
            "aspect": data.get("settings", {}).get("aspect", "9:16"),
            "clips": clips, "duration": round(duration, 2), "mediaCount": len(data.get("media", [])),
            "thumbnail": _project_thumb(data),
        })
    out.sort(key=lambda x: x.get("updated") or 0, reverse=True)
    return {"projects": out}


@router.post("")
def create_project(body: CreateBody):
    if body.aspect not in ASPECTS:
        raise HTTPException(400, f"Unknown aspect ratio {body.aspect}. Use one of {', '.join(ASPECTS)}.")
    return save_project(new_project(body.name.strip() or "Untitled project", body.aspect)).model_dump(mode="json")


@router.get("/{pid}")
def get_project(pid: str):
    return load_project(pid).model_dump(mode="json")


@router.put("/{pid}")
def put_project(pid: str, body: dict, x_ligxt_client: str | None = Header(default=None)):
    _pid(pid)
    check_lock(pid, x_ligxt_client)
    try:
        project = Project.model_validate(body)
    except ValidationError as e:
        err = e.errors()[0]
        raise HTTPException(422, f"Project data is invalid at {'.'.join(map(str, err['loc']))}: {err['msg']}") from e
    if project.id != pid:
        raise HTTPException(400, "Project id in the body does not match the URL.")
    return {"ok": True, "updated": save_project(project).updated}


@router.post("/{pid}/autosave")
def autosave(pid: str, body: dict, x_ligxt_client: str | None = Header(default=None)):
    """Write a timestamped autosave, prune to the newest AUTOSAVE_KEEP, and update project.json."""
    _pid(pid)
    check_lock(pid, x_ligxt_client)
    try:
        project = Project.model_validate(body)
    except ValidationError as e:
        raise HTTPException(422, f"Autosave rejected: {e.errors()[0]['msg']}") from e
    save_project(project)
    adir = project_dir(pid) / "autosave"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    _write_json(adir / f"autosave-{stamp}.json", project.model_dump(mode="json"))
    files = sorted(adir.glob("autosave-*.json"))
    for old in files[:-AUTOSAVE_KEEP]:
        old.unlink(missing_ok=True)
    return {"ok": True, "updated": project.updated, "autosaves": min(len(files), AUTOSAVE_KEEP)}


@router.get("/{pid}/autosaves")
def list_autosaves(pid: str):
    adir = project_dir(_pid(pid)) / "autosave"
    files = sorted(adir.glob("autosave-*.json"), reverse=True) if adir.exists() else []
    return {"autosaves": [{"name": f.name, "time": f.stat().st_mtime} for f in files]}


@router.post("/{pid}/autosaves/{name}/restore")
def restore_autosave(pid: str, name: str):
    if not re.fullmatch(r"autosave-[\d-]+\.json", name):
        raise HTTPException(400, "Invalid autosave name.")
    f = project_dir(_pid(pid)) / "autosave" / name
    if not f.exists():
        raise HTTPException(404, "Autosave not found.")
    project = Project.model_validate_json(f.read_text(encoding="utf-8"))
    return save_project(project).model_dump(mode="json")


@router.post("/{pid}/duplicate")
def duplicate_project(pid: str):
    from .models import new_id
    project = load_project(pid)
    project.id = new_id()
    project.name = f"{project.name} (copy)"
    project.created = time.time()
    return save_project(project).model_dump(mode="json")


@router.delete("/{pid}")
def delete_project(pid: str):
    d = project_dir(_pid(pid))
    if not d.exists():
        raise HTTPException(404, "Project not found.")
    shutil.rmtree(d)
    return {"ok": True}
