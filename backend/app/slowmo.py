"""Slow motion quality: retimed clip frames by frame blending or RIFE (AI) interpolation.

A clip whose "Slow-mo quality" is Frame blending or AI smooth, and which is slowed (speed < 1 anywhere,
constant or ramp) or has "Smooth motion" on with a source frame rate that doesn't match the timeline, is
rendered once as a *retimed* video: frame j is exactly what the timeline shows on timeline frame n0 + j
(speed, ramps, reverse, freeze already applied), at the timeline frame rate, including transition handles.
The export then reads it as a plain speed-1 clip and the preview plays it at 1x, so both show the same
frames. Only frames that fall between two source frames are interpolated (normal-speed parts are copied),
and pairs across a scene cut are never mixed.

Cache: cache/<media>/slowmo/<key>_<full|preview>.mp4 (+ .json stats); key = the exact frame list.
The full-resolution version is also registered as a derived media item so Cutout analyses the retimed
frames themselves: picture and matte stay aligned.
"""
from __future__ import annotations

import hashlib
import os
import json
import math
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import ai_bridge, ai_models, config
from . import motion as MO
from .config import CACHE_DIR
from .jobs import JobContext, jobs
from .media import load_meta, original_path, save_meta

router = APIRouter(prefix="/api/slowmo", tags=["slowmo"])

VERSION = 2  # 2: motion measured against the long side
QUALITIES = ("auto", "repeat", "blend", "ai")
DEFAULT = {"quality": "auto", "smooth": False}
EPS_T = 1e-3  # fractions closer than this to a source frame are that frame


def normalize(s: dict | None) -> dict:
    out = {**DEFAULT, **{k: v for k, v in (s or {}).items() if k in DEFAULT}}
    if out["quality"] not in QUALITIES:
        out["quality"] = "auto"
    out["smooth"] = bool(out["smooth"])
    return out


def min_speed(cd: dict) -> float:
    if cd.get("freezeFrame") is not None:
        return 1.0
    return min(s["speed"] for s in MO.ramp_segments(cd))


def quality_of(cd: dict) -> str:
    """repeat | blend | ai. Auto: AI smooth when the clip is slowed below 1x, frame repeat otherwise."""
    q = normalize(cd.get("slowmo"))["quality"]
    if q == "auto":
        return "ai" if min_speed(cd) < 1 - 1e-6 else "repeat"
    return q


def slow_factor(cd: dict) -> float:
    """How much the clip is slowed at most (4 = 4x slow motion)."""
    return 1.0 / max(0.05, min_speed(cd))


def _local_speed(cd: dict, tau: float) -> float:
    if cd.get("freezeFrame") is not None:
        return 0.0
    segs = MO.ramp_segments(cd)
    for s in segs:
        if tau < s["t1"]:
            return s["speed"]
    return segs[-1]["speed"]


def frame_plan(cd: dict, meta: dict, clip_start: float, fps: float, before: float = 0.0, after: float = 0.0) -> dict:
    """Timeline frames n0..n0+len-1 of the clip (with handles): each as [source frame i, fraction t].
    Same mapping as the export (motion.src_time; the last frame at or before when not interpolating)."""
    q = quality_of(cd)
    smooth = normalize(cd.get("slowmo"))["smooth"]
    sfps = float(meta.get("fps") or 30)
    last = max(0, int(math.floor(float(meta.get("duration") or 0) * sfps + 1e-6)) - 1)
    D = MO.clip_duration(cd)
    n0 = int(round((clip_start - before) * fps))
    n1 = int(round((clip_start + D + after) * fps))
    frames = []
    for n in range(n0, max(n0 + 1, n1)):
        tau = n / fps - clip_start
        s = MO.src_time(cd, tau)
        p = min(max(0.0, s * sfps), float(last))
        i = int(math.floor(p + 1e-6))
        t = p - i
        interp = q in ("blend", "ai") and (_local_speed(cd, tau) < 1 - 1e-6 and _local_speed(cd, tau) > 0 or smooth)
        if not interp or t < EPS_T or i >= last:
            t = 0.0
        elif t > 1 - EPS_T:
            i, t = i + 1, 0.0
        frames.append([i, round(t, 4)])
    return {"n0": n0, "frames": frames, "quality": q, "fps": fps, "sourceFps": sfps}


def needs_retime(cd: dict, meta: dict, fps: float) -> bool:
    if meta.get("type") != "video" or quality_of(cd) not in ("blend", "ai"):
        return False
    smooth = normalize(cd.get("slowmo"))["smooth"]
    return min_speed(cd) < 1 - 1e-6 or (smooth and abs(float(meta.get("fps") or 30) - fps) > 0.01)


def plan_key(meta: dict, plan: dict, res: str) -> str:
    body = json.dumps([VERSION, meta["id"], plan["quality"], round(plan["fps"], 4), plan["frames"], res])
    return hashlib.sha1(body.encode()).hexdigest()[:16]


def slow_dir(mid: str) -> Path:
    return CACHE_DIR / mid / "slowmo"


def preview_size(w: int, h: int) -> tuple[int, int]:
    from .cutout import preview_size as ps
    return ps(w, h)


def derived_id(meta: dict, key: str) -> str:
    return hashlib.sha1(f"{meta['id']}|{key}".encode()).hexdigest()[:12]


# ---------------------------------------------------------------- render
def _interp_module():
    p = str(config.ROOT / "ai_worker")
    if p not in sys.path:
        sys.path.insert(0, p)
    import interp  # numpy only for blending
    return interp


class _Ctx:
    def __init__(self, ctx: JobContext | None, p0: float, p1: float, label: str):
        self.ctx, self.p0, self.p1, self.label = ctx, p0, p1, label

    def progress(self, p: float, msg: str = "", force: bool = False) -> None:
        if self.ctx:
            self.ctx.progress(self.p0 + (self.p1 - self.p0) * p, f"{self.label}" + (f" ({msg})" if msg and "frames" in msg else "…"))

    def check(self) -> None:
        if self.ctx and self.ctx.cancelled():
            from .ffmpeg_utils import Cancelled
            raise Cancelled()


def lookup(meta: dict, plan: dict, res: str) -> dict | None:
    key = plan_key(meta, plan, res)
    info_p = slow_dir(meta["id"]) / f"{key}_{res}.json"
    if info_p.exists() and (slow_dir(meta["id"]) / f"{key}_{res}.mp4").exists():
        try:
            return json.loads(info_p.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def render(meta: dict, plan: dict, res: str, ctx: JobContext | None, p0: float = 0.0, p1: float = 1.0) -> dict:
    """Make (or reuse) the retimed video for a plan. res: "full" (export) or "preview" (low-res)."""
    have = lookup(meta, plan, res)
    if have:
        return have
    key = plan_key(meta, plan, res)
    d = slow_dir(meta["id"])
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{key}_{res}.mp4"
    tmp = d / f"{key}_{res}.tmp.mp4"
    w, h = int(meta["width"]), int(meta["height"])
    ow, oh = (w, h) if res == "full" else preview_size(w, h)
    args = {"src": str(original_path(meta)), "fps": plan["sourceFps"], "hdr": bool(meta.get("hdr")), "frames": plan["frames"],
            "out": str(tmp), "outWidth": ow, "outHeight": oh, "outFps": plan["fps"], "quality": res,
            "mode": plan["quality"], "ffmpeg": config.FFMPEG, "staticLock": True}
    label = "AI smooth slow motion" if plan["quality"] == "ai" else "Blending frames"
    pc = _Ctx(ctx, p0, p1, label)
    try:
        if plan["quality"] == "ai":
            if os.environ.get("LIGXT_SLOWMO_TEST"):  # automated tests: the worker blends instead of running RIFE
                args["mode"] = "ai-test"
            else:
                mdir = ai_models.ensure_model(ai_models.RIFE_MODEL, ctx, p0, p0 + (p1 - p0) * 0.2)
                args["weights"] = str(mdir / "flownet.pkl")
                pc.p0 = p0 + (p1 - p0) * 0.2
            s = ai_models.load_settings()
            args.update(budgetGB=s["budgetGB"], device=s["device"])
            stats = ai_bridge.call("interp", args, progress=lambda p, m: pc.progress(p, m),
                                   cancelled=ctx.cancelled if ctx else None)
        else:
            stats = _interp_module().interp(pc, args)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out)
    mot = sorted(stats.get("motion") or [])
    p95 = mot[min(len(mot) - 1, int(len(mot) * 0.95))] if mot else None
    info = {"file": out.name, "res": res, "width": ow, "height": oh, "fps": plan["fps"], "frames": len(plan["frames"]),
            "quality": plan["quality"], "interpolated": stats.get("interpolated"), "copied": stats.get("copied"),
            "cutPairs": stats.get("cutPairs") or [], "motionP95": p95, "aiFps": stats.get("fps"), "peakVram": stats.get("peakVram"),
            "device": stats.get("device"), "seconds": stats.get("seconds"), "warning": stats.get("warning"), "created": time.time()}
    (d / f"{key}_{res}.json").write_text(json.dumps(info), encoding="utf-8")
    _prune(meta["id"])
    return info


def _prune(mid: str, keep: int = 10) -> None:
    d = slow_dir(mid)
    js = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for j in js[keep:]:
        for f in d.glob(j.stem + "*"):
            f.unlink(missing_ok=True)


def warning_for(cd: dict, info: dict) -> str | None:
    """Too much motion for clean interpolation at this slow-down: suggest a gentler factor."""
    m = info.get("motionP95")
    if m is None or info.get("quality") != "ai":
        return "This clip contains a scene cut: frames across the cut are repeated, not mixed." if info.get("cutPairs") else None
    f = slow_factor(cd)
    # calibrated on the test clips (Phase 10): held-out frames show warping on a fast hand reach (3.8 % of the long side
    # per frame) but not on 0.7-1.3 % clips
    limit = 4.0 if f <= 2.01 else 2.5 if f <= 4.01 else 1.8
    if m > limit:
        better = "2x" if f > 2.01 else "frame blending"
        return (f"Very fast motion ({m:.1f}% of the frame between two source frames): at {f:.0f}x slow motion the in-between "
                f"frames can warp. Try {better}, or a shot with less movement.")
    if info.get("cutPairs"):
        return "This clip contains a scene cut: frames across the cut are repeated, not mixed."
    return None


def derived_meta(meta: dict, plan: dict, info: dict) -> dict:
    """The full-resolution retimed video as a media item (for Cutout on slowed clips)."""
    key = plan_key(meta, plan, "full")
    mid = derived_id(meta, key)
    m = load_meta(mid)
    if m:
        return m
    m = {"id": mid, "name": f"{meta['name']} (slow motion)", "type": "video", "derivedFrom": meta["id"],
         "file": f"cache/{meta['id']}/slowmo/{key}_full.mp4", "size": (slow_dir(meta["id"]) / f"{key}_full.mp4").stat().st_size,
         "imported": time.time(), "duration": info["frames"] / plan["fps"], "hasVideo": True, "hasAudio": False,
         "width": info["width"], "height": info["height"], "fps": plan["fps"], "rotation": 0, "hdr": False,
         "thumbnail": None, "waveform": None, "proxy": None, "proxyStatus": "notNeeded", "status": "ready", "error": None}
    from .media import media_dir
    media_dir(mid).mkdir(parents=True, exist_ok=True)
    save_meta(m)
    return m


def retimed_clip(cd: dict, plan: dict, clip_start: float) -> dict:
    """The clip as a plain speed-1 clip of the retimed video."""
    off = clip_start - plan["n0"] / plan["fps"]
    D = MO.clip_duration(cd)
    out = {**cd, "inPoint": off, "outPoint": off + D, "speed": 1, "speedRamp": None, "reverse": False, "freezeFrame": None}
    return out


# ---------------------------------------------------------------- API
class PrepareBody(BaseModel):
    mediaId: str
    clip: dict  # start, inPoint, outPoint, speed, speedRamp, reverse, freezeFrame, slowmo
    fps: float = 30.0
    before: float = 0.0  # transition handles (s)
    after: float = 0.0
    res: str = "preview"  # preview | full
    start: bool = True


_lock = threading.Lock()
_running: dict[str, str] = {}


def summary(meta: dict, cd: dict, plan: dict, info: dict, res: str) -> dict:
    out = {"state": "ready", "url": f"/files/cache/{meta['id']}/slowmo/{info['file']}", "fps": plan["fps"],
           "offset": float(cd.get("start", 0)) - plan["n0"] / plan["fps"], "frames": info["frames"], "quality": plan["quality"],
           "interpolated": info.get("interpolated"), "cutPairs": info.get("cutPairs"), "motionP95": info.get("motionP95"),
           "aiFps": info.get("aiFps"), "peakVram": info.get("peakVram"), "device": info.get("device"),
           "warning": warning_for(cd, info) or info.get("warning")}
    if res == "full":
        out["mediaId"] = derived_meta(meta, plan, info)["id"]
    return out


@router.post("/prepare")
def prepare(body: PrepareBody):
    meta = load_meta(body.mediaId)
    if not meta or meta.get("type") != "video":
        raise HTTPException(404, "Video not found.")
    if body.res not in ("preview", "full"):
        raise HTTPException(400, "res must be preview or full.")
    cd = {"inPoint": 0, "outPoint": float(meta.get("duration") or 0), "speed": 1, **body.clip}
    if not needs_retime(cd, meta, body.fps):
        return {"state": "notNeeded", "quality": quality_of(cd)}
    plan = frame_plan(cd, meta, float(cd.get("start", 0)), body.fps, body.before, body.after)
    info = lookup(meta, plan, body.res)
    if info:
        return summary(meta, cd, plan, info, body.res)
    wk = f"{meta['id']}|{plan_key(meta, plan, body.res)}"
    with _lock:
        jid = _running.get(wk)
        j = jobs.get(jid) if jid else None
        if j and j.status in ("queued", "running"):
            return {"state": "running", "jobId": jid, "progress": j.progress, "message": j.message}
    if not body.start:
        return {"state": "missing", "quality": plan["quality"]}
    if plan["quality"] == "ai" and ai_models.ai_python() is None:
        raise HTTPException(503, "AI smooth needs the AI tools, which are not installed. Run setup.ps1, or choose Frame blending.")

    def run(ctx: JobContext) -> dict:
        try:
            inf = render(meta, plan, body.res, ctx)
        finally:
            with _lock:
                _running.pop(wk, None)
        return summary(meta, cd, plan, inf, body.res)

    job = jobs.submit("slowmo", f"Slow motion: {meta['name']}", run, queue="ai" if plan["quality"] == "ai" else "media",
                      ref={"mediaId": meta["id"]})
    with _lock:
        _running[wk] = job.id
    return {"state": "running", "jobId": job.id, "progress": 0, "message": "Waiting"}
