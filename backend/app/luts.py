"""LUT library: bundled LUTs (repo luts/, see luts/LICENSE.md) and user-imported .cube files."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from . import config
from .grade import parse_cube

router = APIRouter(prefix="/api/luts", tags=["luts"])
BUNDLED = config.ROOT / "luts"
USER = config.DATA_ROOT / "user_luts"  # never the repo's luts/ (the data root is the app folder by default)
USER.mkdir(parents=True, exist_ok=True)
_cache: dict[str, dict] = {}


def _path(lut_id: str) -> Path:
    m = re.fullmatch(r"(builtin|user)/([A-Za-z0-9_-]{1,64})", lut_id or "")
    if not m:
        raise HTTPException(400, f"Invalid LUT id {lut_id!r}.")
    return (BUNDLED if m.group(1) == "builtin" else USER) / f"{m.group(2)}.cube"


def load_cube(lut_id: str) -> dict:
    if lut_id not in _cache:
        p = _path(lut_id)
        if not p.exists():
            raise FileNotFoundError(lut_id)
        _cache[lut_id] = parse_cube(p.read_text(encoding="utf-8", errors="replace"))
    return _cache[lut_id]


def _meta(p: Path, kind: str) -> dict:
    side = p.with_suffix(".json")
    name = json.loads(side.read_text(encoding="utf-8")).get("name") if side.exists() else p.stem.replace("-", " ").title()
    return {"id": f"{kind}/{p.stem}", "name": name, "kind": kind}


@router.get("")
def list_luts():
    out = [_meta(p, "builtin") for p in sorted(BUNDLED.glob("*.cube"))]
    out += [_meta(p, "user") for p in sorted(USER.glob("*.cube"), key=lambda p: p.stat().st_mtime)]
    return {"luts": out}


@router.get("/file")
def get_lut(id: str = Query(...)):
    p = _path(id)
    if not p.exists():
        raise HTTPException(404, "LUT not found.")
    return PlainTextResponse(p.read_text(encoding="utf-8", errors="replace"))


@router.post("/import")
async def import_lut(request: Request, filename: str = Query(..., min_length=1)):
    if not filename.lower().endswith(".cube"):
        raise HTTPException(415, "Only .cube LUT files are supported.")
    body = (await request.body()).decode("utf-8", errors="replace")
    if len(body) > 40_000_000:
        raise HTTPException(413, "That LUT file is too large.")
    try:
        cube = parse_cube(body)
    except ValueError as e:
        raise HTTPException(422, f"'{filename}' is not a valid 3D .cube LUT: {e}") from e
    stem = uuid.uuid4().hex[:12]
    (USER / f"{stem}.cube").write_text(body, encoding="utf-8")
    name = Path(filename).stem[:60]
    (USER / f"{stem}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    return {"id": f"user/{stem}", "name": name, "kind": "user", "size": cube["size"]}


@router.delete("/{kind}/{stem}")
def delete_lut(kind: str, stem: str):
    if kind != "user":
        raise HTTPException(400, "Bundled LUTs can't be deleted.")
    p = _path(f"user/{stem}")
    p.unlink(missing_ok=True)
    p.with_suffix(".json").unlink(missing_ok=True)
    _cache.pop(f"user/{stem}", None)
    return {"ok": True}
