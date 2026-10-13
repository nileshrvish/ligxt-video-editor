"""Text endpoints: fonts, layout/rasterisation for the preview, saved text styles."""
from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import config
from . import text as TX

router = APIRouter(tags=["text"])
STYLES = config.DATA_ROOT / "presets" / "text_styles.json"
STYLES.parent.mkdir(parents=True, exist_ok=True)


@router.get("/api/text/fonts")
def fonts():
    return {"fonts": [{**f, "url": f"/files/fonts/{f['file']}"} for f in TX.SHARED["fonts"]]}


class LayoutBody(BaseModel):
    spec: dict


@router.post("/api/text/layout")
def layout(body: LayoutBody):
    """Lay out + rasterise a text spec (cached). Images live under /files/cache/_text/<key>/."""
    if len(str(body.spec.get("content", ""))) > 2000:
        raise HTTPException(400, "Text is too long (2000 characters at most).")
    lay = TX.build(body.spec)
    return {**lay, "base": f"/files/cache/_text/{lay['key']}/"}


def _load() -> list[dict]:
    try:
        return json.loads(STYLES.read_text(encoding="utf-8")) if STYLES.exists() else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(items: list[dict]) -> None:
    tmp = STYLES.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.replace(STYLES)


class StyleBody(BaseModel):
    name: str
    style: dict


@router.get("/api/text-styles")
def list_styles():
    return {"styles": _load()}


@router.post("/api/text-styles")
def save_style(body: StyleBody):
    name = body.name.strip()[:60]
    if not name:
        raise HTTPException(400, "Give the style a name.")
    style = {k: v for k, v in body.style.items() if k not in ("content", "words")}
    items = [s for s in _load() if s["name"].lower() != name.lower()]
    item = {"id": uuid.uuid4().hex[:10], "name": name, "style": style, "created": time.time()}
    _save(items + [item])
    return item


@router.delete("/api/text-styles/{sid}")
def delete_style(sid: str):
    items = _load()
    left = [s for s in items if s["id"] != sid]
    if len(left) == len(items):
        raise HTTPException(404, "Style not found.")
    _save(left)
    return {"ok": True}
