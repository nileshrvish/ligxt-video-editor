"""Template endpoints: gallery, save from a project, manage, previews, .zip export/import, generate."""
from __future__ import annotations

import copy
import shutil
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import templates as TP
from .jobs import JobContext, jobs
from .models import Project
from .projects import load_project, save_project

router = APIRouter(prefix="/api/templates", tags=["templates"])


def _load(tid: str) -> dict:
    try:
        return TP.load(tid)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(404, f"Template not found: {tid}") from e


def _preview_job(t: dict, project: Project | None = None, start: float = 0.0, label: str = "") -> str:
    def run(ctx: JobContext) -> dict:
        ctx.progress(0.1, "Rendering the preview")
        TP.render_preview(t, TP.template_dir(t["id"]), project=project, start=start)
        return {"templateId": t["id"]}
    return jobs.submit("template-preview", label or f"Template preview: {t['name']}", run, queue="render", ref={"templateId": t["id"]}).id


@router.get("")
def list_templates(q: str = "", category: str = ""):
    items = TP.list_all(q, category)
    cats = sorted({s["category"] for s in TP.list_all()})
    return {"templates": items, "categories": cats}


@router.get("/{tid}")
def get_template(tid: str):
    t = _load(tid)
    return {**TP.summary(t), "template": t}


class SaveBody(BaseModel):
    projectId: str | None = None
    project: dict | None = None
    name: str
    category: str = "Beat sync"
    tags: list[str] = []
    description: str = ""
    exportPreset: str | None = None
    previewStart: float | None = None  # preview from your project from this time; None = placeholder cards


@router.post("")
def save_template(body: SaveBody):
    project = Project.model_validate(body.project) if body.project else load_project(body.projectId or "")
    name = body.name.strip()[:80]
    if not name:
        raise HTTPException(400, "Give the template a name.")
    try:
        t, notes = TP.save_from_project(project, name, category=body.category.strip() or "Other", tags=[x.strip() for x in body.tags if x.strip()],
                                        description=body.description.strip(), export_preset=body.exportPreset)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    t = TP.load(t["id"])
    job = _preview_job(t, project if body.previewStart is not None else None, body.previewStart or 0.0)
    return {"template": TP.summary(t), "notes": notes, "previewJobId": job}


class PatchBody(BaseModel):
    name: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    description: str | None = None


@router.patch("/{tid}")
def patch_template(tid: str, body: PatchBody):
    t = _load(tid)
    if t.get("builtin"):
        raise HTTPException(400, "Built-in templates can't be changed. Duplicate it first.")
    for k in ("name", "category", "tags", "description"):
        v = getattr(body, k)
        if v is not None:
            t[k] = v.strip()[:80] if isinstance(v, str) else [x.strip() for x in v if x.strip()]
    if not t["name"]:
        raise HTTPException(400, "The name can't be empty.")
    TP.save(t)
    return TP.summary(TP.load(tid))


@router.post("/{tid}/duplicate")
def duplicate_template(tid: str):
    t = copy.deepcopy(_load(tid))
    src = TP.template_dir(tid)
    t["id"] = TP.new_id(t["name"] + " copy")
    t["name"] = t["name"] + " (copy)"
    t["builtin"] = False
    t["created"] = time.time()
    d = TP.USER_DIR / t["id"]
    d.mkdir(parents=True)
    for f in src.iterdir():
        if f.name != "template.json" and f.is_file():
            shutil.copyfile(f, d / f.name)
    TP.save(t)
    return TP.summary(TP.load(t["id"]))


@router.delete("/{tid}")
def delete_template(tid: str):
    t = _load(tid)
    if t.get("builtin"):
        raise HTTPException(400, "Built-in templates can't be deleted.")
    shutil.rmtree(TP.template_dir(tid), ignore_errors=True)
    return {"ok": True}


class PreviewBody(BaseModel):
    source: str = "placeholder"  # or "project"
    projectId: str | None = None
    start: float = 0.0


@router.post("/{tid}/preview")
def regenerate_preview(tid: str, body: PreviewBody):
    t = _load(tid)
    if t.get("builtin"):
        raise HTTPException(400, "Built-in templates keep their own preview. Duplicate it to change it.")
    project = load_project(body.projectId) if body.source == "project" and body.projectId else None
    return {"jobId": _preview_job(t, project, body.start)}


@router.get("/{tid}/export.zip")
def export_zip(tid: str, includePreview: bool = False):
    _load(tid)
    path = TP.export_zip(tid, includePreview)
    return FileResponse(path, media_type="application/zip", filename=f"{tid}.zip")


@router.post("/import")
async def import_zip(request: Request):
    data = await request.body()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "Template files are at most 200 MB.")
    try:
        t, needs_preview = TP.import_zip(data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    job = _preview_job(TP.load(t["id"])) if needs_preview else None
    return {"template": TP.summary(TP.load(t["id"])), "previewJobId": job}


class GenerateBody(BaseModel):
    media: list[str] = []
    slotMedia: list[str] | None = None
    music: str | None = None  # media id; None = the template's bundled song (if any)
    musicStart: float = 0.0
    texts: dict[str, str] = {}
    name: str = ""


@router.post("/{tid}/generate")
def generate(tid: str, body: GenerateBody):
    t = _load(tid)

    def run(ctx: JobContext) -> dict:
        ctx.progress(0.1, "Fitting the template to your song")
        music = body.music or TP.music_media_for(t) if (body.music or (t.get("music") or {}).get("file")) else body.music
        if not music:
            raise ValueError("This template needs a song: pick one.")
        p, report = TP.generate(t, body.media, music, body.musicStart, body.texts, body.name or t["name"], body.slotMedia)
        save_project(p)
        ctx.message("Project created")
        return {"projectId": p.id, "report": report}

    return {"jobId": jobs.submit("template-generate", f"New project from '{t['name']}'", run, queue="render", ref={"templateId": tid}).id}
