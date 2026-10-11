"""User filter presets and the generated effect textures the preview shares with the export."""
from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config
from .grade import grain_atlas, leak_texture

router = APIRouter(tags=["grade"])
STORE = config.DATA_ROOT / "presets" / "user_presets.json"
STORE.parent.mkdir(parents=True, exist_ok=True)


def _load() -> list[dict]:
    try:
        return json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(items: list[dict]) -> None:
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.replace(STORE)


class PresetBody(BaseModel):
    name: str
    filters: list[dict]


@router.get("/api/presets")
def list_presets():
    return {"presets": _load()}


@router.post("/api/presets")
def save_preset(body: PresetBody):
    name = body.name.strip()[:60]
    if not name:
        raise HTTPException(400, "Give the preset a name.")
    if not body.filters:
        raise HTTPException(400, "This clip has no filters to save.")
    items = [p for p in _load() if p["name"].lower() != name.lower()]  # same name replaces
    item = {"id": uuid.uuid4().hex[:12], "name": name, "filters": body.filters, "created": time.time()}
    _save(items + [item])
    return item


@router.delete("/api/presets/{pid}")
def delete_preset(pid: str):
    items = _load()
    if not any(p["id"] == pid for p in items):
        raise HTTPException(404, "Preset not found.")
    _save([p for p in items if p["id"] != pid])
    return {"ok": True}


def _dims(w: int, h: int) -> tuple[int, int]:
    if not (2 <= w <= 8192 and 2 <= h <= 8192):
        raise HTTPException(400, "Invalid size.")
    return w, h


@router.get("/api/fx/grain.png")
def fx_grain(w: int = Query(...), h: int = Query(...), s: float = Query(1.0)):
    w, h = _dims(w, h)
    return FileResponse(grain_atlas(w, h, max(1.0, s)), headers={"Cache-Control": "max-age=86400"})


@router.get("/api/fx/leak.png")
def fx_leak(w: int = Query(...), h: int = Query(...)):
    w, h = _dims(w, h)
    return FileResponse(leak_texture(w, h), headers={"Cache-Control": "max-age=86400"})
