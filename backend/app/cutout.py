"""Subject cutout: mattes for clips, cached per media + model + settings.

Stages (all cached in cache/<media id>/cutout/):
  raw     the model's alpha for a window of source frames [k0, k1) on the source's constant frame grid
          (lossless FFV1 gray video, or PNG for photos), made by the AI worker (ai_worker/, ai-venv).
  derived the raw alpha refined here (no GPU needed): flow-guided temporal smoothing (edges stop
          flickering between frames), grow/shrink and feather. Two files: the full-resolution matte
          for the export, and a preview proxy with the picture on the left and the matte on the right
          (one video, so the preview's picture and matte can never drift apart).

Frame k of a window file is source time (k0 + k) / fps. The export reads it through the same timing
code as the picture (layers.source_raw), shifted by k0 / fps, so the matte follows speed, ramps,
reverse and freeze frames exactly.
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import ai_bridge, ai_models, config
from . import motion as MO
from .config import CACHE_DIR
from .ffmpeg_utils import CREATE_NO_WINDOW, Cancelled
from .jobs import JobContext, jobs
from .media import load_meta, original_path

router = APIRouter(prefix="/api/cutout", tags=["cutout"])

RAW_VERSION = 1
DERIVE_VERSION = 2  # 2: flow-aligned 3-frame median before the blend
MARGIN = 1.0  # seconds of footage analysed beyond what the clip shows (transition handles, small trims)
PREVIEW_MAX = config.PROXY_MAX_SIDE
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv,")

DEFAULT = {
    "enabled": False,
    "mode": "person",  # "person" (automatic matting) or "object" (click to select, SAM 2)
    "points": [],  # object mode: [{"t": source s, "x": 0..1, "y": 0..1, "label": 1 | 0}]
    "model": "auto",  # "auto" = Settings > default person model
    "refine": {"smooth": 0.5, "grow": 0.0, "feather": 1.0, "detail": True},
    "background": {"type": "keep", "color": "#14141a", "blur": 24.0, "mediaId": None},
    "subjectFilters": [],
    "backgroundFilters": [],
    "glow": {"enabled": False, "color": "#ffd25a", "width": 14.0, "softness": 0.5, "opacity": 0.9,
             "pulse": {"mode": "none", "amount": 0.7, "rate": 2.0}},
    "shadow": {"enabled": False, "color": "#000000", "opacity": 0.55, "blur": 16.0, "x": 10.0, "y": 16.0},
}


def normalize(cut: dict | None) -> dict:
    c = dict(cut or {})
    out = {**DEFAULT, **{k: v for k, v in c.items() if k in DEFAULT}}
    out["points"] = [{"t": float(p.get("t", 0)), "x": float(p.get("x", 0)), "y": float(p.get("y", 0)), "label": 1 if p.get("label", 1) else 0}
                     for p in (c.get("points") or [])]
    for k in ("refine", "background", "glow", "shadow"):
        out[k] = {**DEFAULT[k], **(c.get(k) or {})}
    out["glow"]["pulse"] = {**DEFAULT["glow"]["pulse"], **((c.get("glow") or {}).get("pulse") or {})}
    out["subjectFilters"] = list(c.get("subjectFilters") or [])
    out["backgroundFilters"] = list(c.get("backgroundFilters") or [])
    return out


def active(cut: dict | None) -> bool:
    return bool(cut and cut.get("enabled"))


def model_for(cut: dict) -> str:
    if cut.get("mode") == "object":
        return ai_models.OBJECT_MODEL
    m = cut.get("model") or "auto"
    if m == "test-key":  # automated tests: a colour key in the worker, no model download
        return m
    if m == "auto" or m not in ai_models.CATALOG:
        m = ai_models.load_settings()["personModel"]
    return m


# ---------------------------------------------------------------- windows of source frames
def frame_count(meta: dict) -> int:
    fps = float(meta.get("fps") or 30)
    return max(1, int(math.floor(float(meta.get("duration") or 0) * fps + 1e-6)))


def needed_span(cd: dict, before: float = 0.0, after: float = 0.0) -> tuple[float, float]:
    """Source seconds a clip shows over clip time [-before, duration + after] (speed, ramps, reverse, freeze)."""
    D = MO.clip_duration(cd)
    n = 240
    vals = [MO.src_time(cd, -before + (D + before + after) * i / n) for i in range(n + 1)]
    return min(vals), max(vals)


def window_for(meta: dict, s0: float, s1: float, margin: float = MARGIN) -> tuple[int, int]:
    fps = float(meta.get("fps") or 30)
    n = frame_count(meta)
    k0 = max(0, int(math.floor((s0 - margin) * fps)))
    k1 = min(n, int(math.ceil((s1 + margin) * fps)) + 1)
    return k0, max(k0 + 1, k1)


def cut_dir(mid: str) -> Path:
    return CACHE_DIR / mid / "cutout"


def _raw_stem(model: str, detail: bool, k0: int, k1: int) -> str:
    return f"raw{RAW_VERSION}_{model}_{'d' if detail else 'n'}_{k0}_{k1}"


def refine_key(refine: dict) -> str:
    r = {"smooth": round(float(refine.get("smooth", 0.5)), 3), "grow": round(float(refine.get("grow", 0)), 2),
         "feather": round(float(refine.get("feather", 1)), 2), "v": DERIVE_VERSION}
    return hashlib.sha1(json.dumps(r, sort_keys=True).encode()).hexdigest()[:10]


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def find_raw(meta: dict, model: str, detail: bool, k0: int, k1: int) -> dict | None:
    """A cached raw matte covering frames [k0, k1) (photos: the one image)."""
    d = cut_dir(meta["id"])
    if meta["type"] == "image":
        info = _read_json(d / (_raw_stem(model, detail, 0, 1) + ".json"))
        return info if info and (d / info["file"]).exists() else None
    best = None
    for j in d.glob(f"raw{RAW_VERSION}_{model}_{'d' if detail else 'n'}_*.json"):
        if "__" in j.stem:
            continue  # a refined version, not an analysis
        info = _read_json(j)
        if not info or "file" not in info or not (d / info["file"]).exists():
            continue
        if info["k0"] <= k0 and info["k1"] >= k1 and (best is None or info["k1"] - info["k0"] < best["k1"] - best["k0"]):
            best = info
    return best


# ---------------------------------------------------------------- stage 1: the model (AI worker)
_job_lock = threading.Lock()
_running: dict[str, str] = {}  # work key -> job id


def analyze(meta: dict, model: str, detail: bool, k0: int, k1: int, ctx: JobContext | None,
            p0: float = 0.0, p1: float = 1.0, key: str | None = None, points: list[dict] | None = None) -> dict:
    mdir = None
    if model in ai_models.CATALOG:
        if not ai_models.is_installed(model):  # first use: download (with progress), then analyse
            ai_models.ensure_model(model, ctx, p0, p0 + (p1 - p0) * 0.3)
            p0 = p0 + (p1 - p0) * 0.3
        mdir = ai_models.model_dir(model)
    elif model != "test-key":
        raise RuntimeError(f"Unknown cutout model '{model}'.")
    d = cut_dir(meta["id"])
    d.mkdir(parents=True, exist_ok=True)
    image = meta["type"] == "image"
    stem = _raw_stem(key or model, detail, 0 if image else k0, 1 if image else k1)
    out = d / (stem + (".png" if image else ".mkv"))
    tmp = out.with_name(out.stem + ".tmp" + out.suffix)
    s = ai_models.load_settings()
    spec = ai_models.CATALOG.get(model, {"res": 1024, "dynamic": False})
    fps = float(meta.get("fps") or 30)
    w, h = int(meta.get("width") or 0), int(meta.get("height") or 0)
    if w < 2 or h < 2:
        raise RuntimeError(f"'{meta['name']}' has no picture size; re-import it.")

    def prog(p: float, msg: str) -> None:
        if ctx:
            ctx.progress(p0 + (p1 - p0) * p, "Analyzing subject" + (f" ({msg})" if msg and "frames" in msg.lower() else "…"))

    try:
        if points is not None:
            res = _segment_call(meta, mdir, image, tmp, w, h, fps, k0, k1, detail, s, points, prog, ctx)
        else:
            res = _matte_call(meta, model, mdir, image, tmp, w, h, fps, k0, k1, spec, detail, s, prog, ctx)
    except BaseException:
        tmp.unlink(missing_ok=True)  # cancelled or failed: no partial files
        raise
    tmp.replace(out)
    info = {"file": out.name, "model": model, "detail": detail, "k0": 0 if image else k0, "k1": 1 if image else k1,
            "fps": fps, "width": w, "height": h, "image": image, "created": time.time(),
            **{k: res.get(k) for k in ("coverage", "device", "deviceName", "peakVram", "fps", "seconds", "batch", "warning")}}
    info["analysisFps"] = info.pop("fps")
    info["fps"] = fps
    (d / (stem + ".json")).write_text(json.dumps(info), encoding="utf-8")
    return info


def worker_points(points: list[dict], w: int, h: int, fps: float, k0: int, k1: int) -> list[dict]:
    """Normalised clicks at source times -> source pixels on window frames."""
    return [{"f": max(0, min(k1 - k0 - 1, int(round(p["t"] * fps)) - k0)), "x": p["x"] * w, "y": p["y"] * h, "label": p["label"]}
            for p in points]


def _segment_call(meta, mdir, image, tmp, w, h, fps, k0, k1, detail, s, points, prog, ctx, preview=False) -> dict:
    spec = ai_models.CATALOG[ai_models.OBJECT_MODEL]
    return ai_bridge.call("segment", {
        "ckpt": str(mdir / spec["files"][0]["name"]), "config": spec["config"], "src": str(original_path(meta)),
        "kind": "image" if image else "video", "out": str(tmp), "width": w, "height": h, "fps": fps,
        "hdr": bool(meta.get("hdr")), "k0": k0, "n": k1 - k0, "points": worker_points(points, w, h, fps, k0, k1),
        "detail": detail, "device": s["device"], "ffmpeg": config.FFMPEG, "preview": preview,
    }, progress=prog, cancelled=ctx.cancelled if ctx else None)


def _matte_call(meta, model, mdir, image, tmp, w, h, fps, k0, k1, spec, detail, s, prog, ctx) -> dict:
    return ai_bridge.call("matte", {
        "model": model, "modelDir": str(mdir) if mdir else None, "src": str(original_path(meta)),
        "kind": "image" if image else "video", "out": str(tmp), "width": w, "height": h, "fps": fps,
        "hdr": bool(meta.get("hdr")), "k0": k0, "n": k1 - k0, "res": spec["res"], "dynamic": spec["dynamic"],
        "detail": detail, "budgetGB": s["budgetGB"], "device": s["device"], "ffmpeg": config.FFMPEG,
    }, progress=prog, cancelled=ctx.cancelled if ctx else None)


# ---------------------------------------------------------------- stage 2: refine (CPU)
def _ff(args: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen([config.FFMPEG, "-v", "error", "-nostdin", *args], creationflags=CREATE_NO_WINDOW, **kw)


def preview_size(w: int, h: int) -> tuple[int, int]:
    m = PREVIEW_MAX
    if w >= h:
        pw = min(m, w) // 2 * 2
        ph = max(2, int(round(h * pw / w / 2)) * 2)
    else:
        ph = min(m, h) // 2 * 2
        pw = max(2, int(round(w * ph / h / 2)) * 2)
    return pw, ph


def _refine_frame(a: np.ndarray, grow: float, feather: float) -> np.ndarray:
    import cv2
    if abs(grow) >= 0.5:
        r = int(round(abs(grow)))
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        a = cv2.dilate(a, k) if grow > 0 else cv2.erode(a, k)
    if feather >= 0.3:
        a = cv2.GaussianBlur(a, (0, 0), feather)
    return a


def derive(meta: dict, raw: dict, refine: dict, ctx: JobContext | None, p0: float = 0.0, p1: float = 1.0) -> dict:
    import cv2
    d = cut_dir(meta["id"])
    stem = raw["file"].rsplit(".", 1)[0] + "__" + refine_key(refine)
    smooth = max(0.0, min(1.0, float(refine.get("smooth", 0.5))))
    grow, feather = float(refine.get("grow", 0)), max(0.0, float(refine.get("feather", 1)))
    w, h, fps = raw["width"], raw["height"], raw["fps"]
    pw, ph = preview_size(w, h)
    if raw["image"]:
        a = cv2.imread(str(d / raw["file"]), cv2.IMREAD_GRAYSCALE)
        if a is None:
            raise RuntimeError("The cutout matte could not be read; analyze again.")
        a = _refine_frame(a.astype(np.float32) / 255.0, grow, feather)
        a8 = (np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8)
        cv2.imwrite(str(d / (stem + ".png")), a8)
        # preview matte at the size of the image's preview (cache/<id>/preview.jpg)
        prev = cv2.imread(str(CACHE_DIR / meta["id"] / "preview.jpg"))
        sz = (prev.shape[1], prev.shape[0]) if prev is not None else (pw, ph)
        cv2.imwrite(str(d / (stem + "_pv.png")), cv2.resize(a8, sz, interpolation=cv2.INTER_AREA))
        info = {"matte": stem + ".png", "preview": stem + "_pv.png", "k0": 0, "frames": 1, "fps": fps,
                "width": w, "height": h, "image": True}
        (d / (stem + ".json")).write_text(json.dumps(info), encoding="utf-8")
        return info

    k0, n = raw["k0"], raw["k1"] - raw["k0"]
    src = str(original_path(meta))
    tm = TONEMAP if meta.get("hdr") else ""
    ss = f"{max(0.0, (k0 - 0.25) / fps):.6f}"
    # stage 1 (slow, cached per smoothing strength): temporal smoothing of the raw matte
    base = _smoothed(meta, raw, smooth, ctx, p0, p0 + (p1 - p0) * 0.75) if smooth > 0 else d / raw["file"]
    p0 = p0 + (p1 - p0) * 0.75 if smooth > 0 else p0
    # stage 2 (fast): grow/shrink + feather, the full-size matte for the export and the preview proxy
    dec_a = _ff(["-i", str(base), "-f", "rawvideo", "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    matte_tmp = d / (stem + ".tmp.mkv")
    prev_tmp = d / (stem + "_pv.tmp.mp4")
    graph = (f"[1:v]fps={fps:.6f}:round=near,{tm}scale={pw}:{ph}:flags=bicubic,format=yuv420p,setsar=1[l];"
             f"[0:v]split[m1][m2];[m2]scale={pw}:{ph}:flags=area,format=yuv420p,setsar=1[r];[l][r]hstack=inputs=2[pv]")
    enc = _ff(["-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-framerate", f"{fps:.6f}", "-i", "-",
               "-ss", ss, "-i", src, "-filter_complex", graph,
               "-map", "[m1]", "-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "4", "-pix_fmt", "gray", "-frames:v", str(n), "-y", str(matte_tmp),
               "-map", "[pv]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-g", str(config.PROXY_GOP), "-bf", "0",
               "-pix_fmt", "yuv420p", "-frames:v", str(n), "-movflags", "+faststart", "-y", str(prev_tmp)],
              stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    last = None
    try:
        for k in range(n):
            if ctx and ctx.cancelled():
                raise Cancelled()
            buf = dec_a.stdout.read(w * h)
            a8 = np.frombuffer(buf, np.uint8).reshape(h, w) if len(buf) == w * h else (last if last is not None else np.zeros((h, w), np.uint8))
            last = a8
            if abs(grow) >= 0.5 or feather >= 0.3:
                a8 = (np.clip(_refine_frame(a8.astype(np.float32) / 255.0, grow, feather), 0, 1) * 255 + 0.5).astype(np.uint8)
            enc.stdin.write(a8.tobytes())
            if ctx and k % 10 == 0:
                ctx.progress(p0 + (p1 - p0) * k / n, "Refining edges…")
    finally:
        try:
            enc.stdin.close()
        except OSError:
            pass
        enc.wait(timeout=300)
        dec_a.kill()
        dec_a.wait(timeout=30)
    if enc.returncode != 0:
        err = (enc.stderr.read() or b"").decode("utf-8", "replace")[-500:]
        matte_tmp.unlink(missing_ok=True)
        prev_tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Writing the refined matte failed: {err}")
    matte_tmp.replace(d / (stem + ".mkv"))
    prev_tmp.replace(d / (stem + "_pv.mp4"))
    info = {"matte": stem + ".mkv", "preview": stem + "_pv.mp4", "k0": k0, "frames": n, "fps": fps,
            "width": w, "height": h, "previewWidth": pw, "previewHeight": ph, "image": False}
    (d / (stem + ".json")).write_text(json.dumps(info), encoding="utf-8")
    _prune(meta["id"])
    return info


def _smoothed(meta: dict, raw: dict, smooth: float, ctx: JobContext | None, p0: float, p1: float) -> Path:
    """The raw matte smoothed over time (cached per strength): flow-aligned, so real motion isn't smeared.
      1) median of (previous, this, next) warped onto this frame: removes one-frame pops (a stray object
         flashing in, a limb flashing out) without lag;
      2) blend with the previous smoothed matte where the two agree (fine edge shimmer); where they
         disagree (real change, occlusion, flow error) the new frame wins."""
    import cv2
    d = cut_dir(meta["id"])
    out = d / (raw["file"].rsplit(".", 1)[0] + f"__s{DERIVE_VERSION}_{int(round(smooth * 100))}.mkv")
    if out.exists():
        return out
    w, h, fps, k0, n = raw["width"], raw["height"], raw["fps"], raw["k0"], raw["k1"] - raw["k0"]
    tm = TONEMAP if meta.get("hdr") else ""
    ss = f"{max(0.0, (k0 - 0.25) / fps):.6f}"
    fw, fh = max(16, w // 4 // 2 * 2), max(16, h // 4 // 2 * 2)
    dec_a = _ff(["-i", str(d / raw["file"]), "-f", "rawvideo", "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dec_f = _ff(["-ss", ss, "-i", str(original_path(meta)), "-an", "-sn", "-vf", f"fps={fps:.6f}:round=near,{tm}scale={fw}:{fh}:flags=area,format=gray",
                 "-frames:v", str(n), "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tmp = out.with_name(out.stem + ".tmp.mkv")
    enc = _ff(["-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-framerate", f"{fps:.6f}", "-i", "-",
               "-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "4", "-pix_fmt", "gray", "-y", str(tmp)],
              stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))

    def read_alpha(last):
        buf = dec_a.stdout.read(w * h)
        if len(buf) < w * h:
            return last if last is not None else np.zeros((h, w), np.float32)
        return np.frombuffer(buf, np.uint8).reshape(h, w).astype(np.float32) / 255.0

    def read_gray(last):
        gb = dec_f.stdout.read(fw * fh)
        return np.frombuffer(gb, np.uint8).reshape(fh, fw) if len(gb) == fw * fh else last

    def warp(img, g_to, g_from):
        """img (in g_from's frame) moved onto g_to's frame with DIS flow at 1/4 size."""
        flow = dis.calc(g_to, g_from, None)
        flow = cv2.resize(flow, (w, h), interpolation=cv2.INTER_LINEAR) * np.float32(w / fw)
        return cv2.remap(img, gx + flow[..., 0], gy + flow[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    prev_s = prev_g = None
    cur, g_cur = read_alpha(None), read_gray(None)
    try:
        for k in range(n):
            if ctx and ctx.cancelled():
                raise Cancelled()
            a = cur
            nxt = read_alpha(cur) if k + 1 < n else None
            g_nxt = read_gray(g_cur) if k + 1 < n else None
            if g_cur is not None:
                w_prev = warp(prev_s, g_cur, prev_g) if prev_s is not None and prev_g is not None else None
                if smooth >= 0.2 and w_prev is not None and nxt is not None and g_nxt is not None:
                    w_next = warp(nxt, g_cur, g_nxt)
                    lo, hi = np.minimum(w_prev, w_next), np.maximum(w_prev, w_next)
                    med = np.maximum(lo, np.minimum(hi, a))
                    a = a + np.float32(min(1.0, smooth * 2)) * (med - a)
                if w_prev is not None:
                    conf = np.exp(-np.abs(a - w_prev) / np.float32(0.12))
                    wgt = np.float32(0.85 * smooth) * conf
                    a = a * (1 - wgt) + w_prev * wgt
                prev_g = g_cur
            prev_s = a
            cur, g_cur = nxt, g_nxt
            enc.stdin.write((np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes())
            if ctx and k % 10 == 0:
                ctx.progress(p0 + (p1 - p0) * k / n, "Smoothing edges over time…")
    finally:
        try:
            enc.stdin.close()
        except OSError:
            pass
        enc.wait(timeout=300)
        for pr in (dec_a, dec_f):
            pr.kill()
            pr.wait(timeout=30)
    if enc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Writing the smoothed matte failed: " + (enc.stderr.read() or b"").decode("utf-8", "replace")[-400:])
    tmp.replace(out)
    return out


def _prune(mid: str, keep: int = 6) -> None:
    """Keep the newest few refined versions per media (the raw analyses are kept)."""
    d = cut_dir(mid)
    ds = sorted((j for j in d.glob("*__*.json")), key=lambda p: p.stat().st_mtime, reverse=True)
    for j in ds[keep:]:
        stem = j.stem
        for f in d.glob(stem + "*"):
            f.unlink(missing_ok=True)
    sm = sorted(d.glob("*__s*_*.mkv"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in sm[3:]:  # smoothing passes: keep the latest few strengths
        f.unlink(missing_ok=True)


def find_derived(meta: dict, raw: dict, refine: dict) -> dict | None:
    d = cut_dir(meta["id"])
    stem = raw["file"].rsplit(".", 1)[0] + "__" + refine_key(refine)
    info = _read_json(d / (stem + ".json"))
    if info and (d / info["matte"]).exists() and (d / info["preview"]).exists():
        return info
    return None


# ---------------------------------------------------------------- everything for one clip
def points_key(points: list[dict]) -> str:
    return hashlib.sha1(json.dumps([[round(p["t"], 3), round(p["x"], 4), round(p["y"], 4), p["label"]] for p in points]).encode()).hexdigest()[:8]


def plan_for(meta: dict, cut: dict, span: tuple[float, float] | None) -> dict:
    """What a clip needs: model, detail, window. span = source seconds shown (None for photos)."""
    cut = normalize(cut)
    model = model_for(cut)
    detail = bool(cut["refine"].get("detail", True))
    obj = cut.get("mode") == "object"
    if meta["type"] == "image" or span is None:
        k0, k1 = 0, 1
    else:
        if obj and cut["points"]:  # the clicked frames must be inside the window
            ts = [p["t"] for p in cut["points"]]
            span = (min(span[0], *ts), max(span[1], *ts))
        k0, k1 = window_for(meta, *span, margin=0.0)
    # object selections are cached per set of clicks
    key = f"{model}-{points_key(cut['points'])}" if obj else model
    return {"model": model, "key": key, "detail": detail, "k0": k0, "k1": k1, "refine": cut["refine"],
            "points": cut["points"] if obj else None}


def lookup(meta: dict, cut: dict, span: tuple[float, float] | None) -> tuple[dict | None, dict | None]:
    """(raw, derived) already in the cache for this clip, or None."""
    pl = plan_for(meta, cut, span)
    raw = find_raw(meta, pl["key"], pl["detail"], pl["k0"], pl["k1"])
    if not raw:
        return None, None
    return raw, find_derived(meta, raw, pl["refine"])


def _new_window(meta: dict, pl: dict) -> tuple[int, int]:
    if meta["type"] == "image":
        return 0, 1
    # analyse a little more than needed so small trims and transitions don't need a new pass
    fps = float(meta.get("fps") or 30)
    return window_for(meta, pl["k0"] / fps, (pl["k1"] - 1) / fps)


def target(meta: dict, cut: dict, span: tuple[float, float] | None) -> dict:
    """The refined matte ensure() returns for this clip (existing, or the one it will make)."""
    pl = plan_for(meta, cut, span)
    raw = find_raw(meta, pl["key"], pl["detail"], pl["k0"], pl["k1"])
    if raw:
        k0, k1 = raw["k0"], raw["k1"]
        stem = raw["file"].rsplit(".", 1)[0]
    else:
        k0, k1 = _new_window(meta, pl)
        stem = _raw_stem(pl["key"], pl["detail"], k0, k1)
    stem += "__" + refine_key(pl["refine"])
    return {"matte": stem + (".png" if meta["type"] == "image" else ".mkv"), "k0": k0, "frames": k1 - k0,
            "fps": float(meta.get("fps") or 30)}


def ensure(meta: dict, cut: dict, span: tuple[float, float] | None, ctx: JobContext | None,
           p0: float = 0.0, p1: float = 1.0) -> tuple[dict, dict]:
    pl = plan_for(meta, cut, span)
    raw = find_raw(meta, pl["key"], pl["detail"], pl["k0"], pl["k1"])
    if not raw:
        k0, k1 = _new_window(meta, pl)
        raw = analyze(meta, pl["model"], pl["detail"], k0, k1, ctx, p0, p0 + (p1 - p0) * 0.85, key=pl["key"], points=pl["points"])
    der = find_derived(meta, raw, pl["refine"])
    if not der:
        der = derive(meta, raw, pl["refine"], ctx, p0 + (p1 - p0) * 0.85, p1)
    return raw, der


def summary(meta: dict, raw: dict, der: dict, span: tuple[float, float] | None) -> dict:
    mid = meta["id"]
    cov = raw.get("coverage") or []
    if not raw["image"] and span and cov:
        fps = raw["fps"]
        a, b = int(span[0] * fps) - raw["k0"], int(span[1] * fps) - raw["k0"] + 1
        cov = cov[max(0, a):max(1, b)] or cov
    empty = sum(1 for c in cov if c < 0.004) / max(1, len(cov))
    return {
        "state": "ready", "image": raw["image"], "fps": raw["fps"], "offset": raw["k0"] / raw["fps"] if not raw["image"] else 0,
        "frames": der["frames"], "preview": f"/files/cache/{mid}/cutout/{der['preview']}",
        "matte": f"/files/cache/{mid}/cutout/{der['matte']}" if raw["image"] else None,
        "previewWidth": der.get("previewWidth"), "previewHeight": der.get("previewHeight"),
        "coverage": round(sum(cov) / max(1, len(cov)), 4), "emptyFrames": round(empty, 3),
        "noSubject": empty > 0.9, "model": raw["model"], "device": raw.get("deviceName") or raw.get("device"),
        "warning": raw.get("warning"), "analysisFps": raw.get("analysisFps"), "peakVram": raw.get("peakVram"),
    }


# ---------------------------------------------------------------- API
class PrepareBody(BaseModel):
    mediaId: str
    cutout: dict | None = None
    clip: dict | None = None  # the clip (timing fields) for the source window; photos: ignored
    start: bool = True  # start the analysis if it isn't cached


def _span_for(meta: dict, clip: dict | None) -> tuple[float, float] | None:
    if meta["type"] == "image" or not clip:
        return None
    cd = {"inPoint": 0, "outPoint": float(meta.get("duration") or 0), "speed": 1, **clip}
    return needed_span(cd, before=0.5, after=0.5)


def work_key(mid: str, pl: dict) -> str:
    return f"{mid}|{pl['key']}|{pl['detail']}|{pl['k0']}|{pl['k1']}|{refine_key(pl['refine'])}"


@router.post("/prepare")
def prepare(body: PrepareBody):
    meta = load_meta(body.mediaId)
    if not meta:
        raise HTTPException(404, "Media not found.")
    if meta["type"] not in ("video", "image"):
        raise HTTPException(400, "Cutout works on videos and photos.")
    cut = normalize(body.cutout)
    if cut["mode"] == "object" and not any(p["label"] == 1 for p in cut["points"]):
        return {"state": "needsPoints"}
    span = _span_for(meta, body.clip)
    raw, der = lookup(meta, cut, span)
    if raw and der:
        return summary(meta, raw, der, span)
    pl = plan_for(meta, cut, span)
    key = work_key(meta["id"], pl)
    with _job_lock:
        jid = _running.get(key)
        j = jobs.get(jid) if jid else None
        if j and j.status in ("queued", "running"):
            return {"state": "running", "jobId": jid, "progress": j.progress, "message": j.message}
        if j and j.status == "error" and not body.start:
            return {"state": "error", "error": j.error}
    model = pl["model"]
    needs_download = model in ai_models.CATALOG and not ai_models.is_installed(model)
    if not body.start:
        return {"state": "missing", "model": model, "modelName": ai_models.CATALOG.get(model, {}).get("name", model),
                "downloadBytes": ai_models.download_size(model) if needs_download else 0,
                "aiInstalled": ai_models.ai_python() is not None, "rawReady": bool(raw)}
    if ai_models.ai_python() is None and not raw:
        raise HTTPException(503, "Cutout needs the AI tools, which are not installed. Run setup.ps1 (it builds ai-venv), then restart Ligxt.")

    def run(ctx: JobContext) -> dict:
        try:
            r, dv = ensure(meta, cut, span, ctx)
        finally:
            with _job_lock:
                _running.pop(key, None)
        s = summary(meta, r, dv, span)
        ctx.message("No person found" if s["noSubject"] else "Cutout ready")
        return s

    job = jobs.submit("cutout", f"Analyzing subject: {meta['name']}", run, queue="ai", ref={"mediaId": meta["id"]})
    with _job_lock:
        _running[key] = job.id
    return {"state": "running", "jobId": job.id, "progress": 0, "message": "Waiting",
            "downloadBytes": ai_models.download_size(model) if needs_download else 0}


# ---------------------------------------------------------------- glow pulse (shared with the preview)
PULSE_DECAY = 0.22  # seconds: how fast a beat pulse fades


def timeline_beats(project: dict, metas: dict) -> list[float]:
    """Beat times on the timeline from the music tracks' songs (detected beats)."""
    out: list[float] = []
    for t in project.get("tracks") or []:
        if t.get("type") != "music" or t.get("muted"):
            continue
        for c in t.get("clips") or []:
            m = metas.get(c.get("mediaId") or "") or {}
            sp = float(c.get("speed") or 1)
            a, b = float(c.get("inPoint", 0)), float(c.get("outPoint", 0))
            for x in m.get("beats") or []:
                if a - 1e-6 <= x < b:
                    out.append(float(c.get("start", 0)) + (x - a) / sp)
    return sorted(out)


def pulse_at(pulse: dict, t: float, beats: list[float]) -> float:
    """0..1 pulse envelope at timeline time t."""
    mode = (pulse or {}).get("mode", "none")
    if mode == "beat":
        import bisect
        i = bisect.bisect_right(beats, t + 1e-6) - 1
        return math.exp(-(t - beats[i]) / PULSE_DECAY) if i >= 0 else 0.0
    if mode == "rate":
        return 0.5 - 0.5 * math.cos(2 * math.pi * float(pulse.get("rate", 2)) * t)
    return 0.0


def glow_levels(glow: dict, p: float, k: float = 1.0) -> tuple[float, float, float, float]:
    """(sigma px, lo, hi, opacity) of the glow: alpha = clip((blur(matte, sigma) - lo) / (hi - lo)) * opacity.
    Width = how far the glow reaches (canvas px), softness 0 (crisp outline) .. 1 (soft halo); the pulse
    widens it and raises its opacity. k scales pixel sizes (preview)."""
    amt = float((glow.get("pulse") or {}).get("amount", 0.7)) if (glow.get("pulse") or {}).get("mode", "none") != "none" else 0.0
    w0 = max(1.0, float(glow.get("width", 14)))
    s = max(0.0, min(1.0, float(glow.get("softness", 0.5))))
    sigma = max(0.8, w0 * (0.25 + 0.5 * s))
    w = w0 * (1 + 0.6 * amt * p)
    lo = 0.5 * math.erfc(w / (sigma * math.sqrt(2)))
    hi = lo + (0.5 - lo) * (0.12 + 0.88 * s)
    op = max(0.0, min(1.0, float(glow.get("opacity", 0.9)) * (1 - 0.5 * amt + 0.5 * amt * p)))
    return sigma * k, lo, hi, op


# ---------------------------------------------------------------- click to select: one-frame preview
class PickBody(BaseModel):
    mediaId: str
    t: float = 0.0  # source seconds of the frame the clicks are on
    points: list[dict] = []  # [{"x": 0..1, "y": 0..1, "label": 1 | 0}]


@router.post("/pick")
def pick_preview(body: PickBody):
    """What the clicks select on that one frame (SAM 2): a small picture with the selection tinted."""
    meta = load_meta(body.mediaId)
    if not meta or meta["type"] not in ("video", "image"):
        raise HTTPException(404, "Media not found.")
    pts = [{"t": body.t, "x": float(p["x"]), "y": float(p["y"]), "label": 1 if p.get("label", 1) else 0} for p in body.points]
    if not any(p["label"] == 1 for p in pts):
        raise HTTPException(400, "Click on the object first.")
    if ai_models.ai_python() is None:
        raise HTTPException(503, "Click-to-select needs the AI tools, which are not installed. Run setup.ps1, then restart Ligxt.")

    def run(ctx: JobContext) -> dict:
        import cv2
        mdir = ai_models.ensure_model(ai_models.OBJECT_MODEL, ctx, 0.0, 0.5)
        fps = float(meta.get("fps") or 30)
        image = meta["type"] == "image"
        k = 0 if image else max(0, min(frame_count(meta) - 1, int(round(body.t * fps))))
        w, h = int(meta["width"]), int(meta["height"])
        d = cut_dir(meta["id"])
        d.mkdir(parents=True, exist_ok=True)
        tag = points_key(pts) + f"_{k}"
        mask_png, out_jpg = d / f"pick_{tag}.png", d / f"pick_{tag}.jpg"
        s = ai_models.load_settings()
        res = _segment_call(meta, mdir, image, mask_png, w, h, fps, k, k + 1, True, s, pts,
                            lambda p, m: ctx.progress(0.5 + 0.5 * p, "Finding the object…"), ctx, preview=True)
        # the frame with the selection tinted, ~540 px
        pw, ph = preview_size(w, h)
        pw, ph = max(2, pw * 540 // max(pw, ph) // 2 * 2), max(2, ph * 540 // max(pw, ph) // 2 * 2)
        tm = TONEMAP if meta.get("hdr") else ""
        args = ["-i", str(original_path(meta))] if image else ["-ss", f"{max(0.0, (k - 0.25) / fps):.6f}", "-i", str(original_path(meta))]
        raw = subprocess.run([config.FFMPEG, "-v", "error", "-nostdin", *args, "-frames:v", "1", "-vf",
                              f"{'' if image else f'fps={fps:.6f}:round=near,'}{tm}scale={pw}:{ph}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                             capture_output=True, creationflags=CREATE_NO_WINDOW).stdout
        frame = np.frombuffer(raw, np.uint8).reshape(ph, pw, 3).copy() if len(raw) == pw * ph * 3 else np.zeros((ph, pw, 3), np.uint8)
        m = cv2.resize(cv2.imread(str(mask_png), cv2.IMREAD_GRAYSCALE), (pw, ph), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
        tint = np.array([255, 90, 140], np.float32)  # BGR violet
        out = frame.astype(np.float32) * (1 - 0.45 * m[..., None]) + tint * 0.45 * m[..., None]
        edge = cv2.morphologyEx((m > 0.5).astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
        out[edge] = (255, 255, 255)
        for p in pts:
            c = (int(p["x"] * pw), int(p["y"] * ph))
            cv2.circle(out, c, 6, (40, 200, 40) if p["label"] else (40, 40, 230), -1)
            cv2.circle(out, c, 6, (255, 255, 255), 1)
        cv2.imwrite(str(out_jpg), out.astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 88])
        ctx.message("Selection ready")
        return {"url": f"/files/cache/{meta['id']}/cutout/{out_jpg.name}", "coverage": (res.get("coverage") or [0])[0],
                "frame": k}

    job = jobs.submit("cutout-pick", f"Selecting in {meta['name']}", run, queue="ai", ref={"mediaId": meta["id"]})
    return {"jobId": job.id}
