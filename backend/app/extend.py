"""Photo extension ("extend canvas"): a blurred, colour-matched fill around a photo, so it can be
moved or zoomed out without showing its edges.

The fill is one image per photo (cached), shared by the preview and the export: the photo's edge
pixels stretched outwards (not mirrored, so a head touching the frame is never duplicated), blurred
more and more with distance from the picture until it becomes a soft wash of the nearby colours.
It covers the source rect [-fx*w, (1+fx)*w] x [-fy*h, (1+fy)*h] (fx = fy = MARGIN in source units);
the photo itself is drawn on top at full resolution.
"""
from __future__ import annotations

import json
import subprocess

import numpy as np
from PIL import Image, ImageFilter

from .config import CACHE_DIR, FFMPEG
from .media import original_path

MARGIN = 0.35  # of the photo's width/height, on every side
LONG_SIDE = 1600  # pixels of the whole extended image's long side
VERSION = 1


def extended(meta: dict) -> dict:
    """{"file", "url", "fx", "fy", "w", "h"} for a photo's extension image (built on first use)."""
    d = CACHE_DIR / meta["id"]
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"extend_v{VERSION}.jpg"
    info_p = d / f"extend_v{VERSION}.json"
    if out.exists() and info_p.exists():
        return json.loads(info_p.read_text(encoding="utf-8"))
    sw, sh = float(meta.get("width") or 1000), float(meta.get("height") or 1000)
    lp = LONG_SIDE / (1 + 2 * MARGIN)  # long side of the photo part
    k = lp / max(sw, sh)
    w, h = max(8, round(sw * k)), max(8, round(sh * k))
    # decode with ffmpeg so orientation/colour handling matches the export's reading of the photo
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(original_path(meta)), "-vf", f"scale={w}:{h}:flags=area",
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    src = np.frombuffer(r.stdout, np.uint8).reshape(h, w, 3)
    mx, my = round(MARGIN * w), round(MARGIN * h)
    pad = np.pad(src, ((my, my), (mx, mx), (0, 0)), mode="edge").astype(np.float32)
    S = min(w, h)

    def blur(img: np.ndarray, sigma: float) -> np.ndarray:
        im = Image.fromarray(img.clip(0, 255).astype(np.uint8))
        return np.asarray(im.filter(ImageFilter.GaussianBlur(sigma)), np.float32)

    b1, b2, b3 = blur(pad, 0.012 * S), blur(pad, 0.05 * S), blur(pad, 0.16 * S)
    H, W = pad.shape[:2]
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    dx = np.maximum(np.maximum(mx - xs, xs - (mx + w - 1)), 0)
    dy = np.maximum(np.maximum(my - ys, ys - (my + h - 1)), 0)
    t = (np.sqrt(dx * dx + dy * dy) / (0.3 * S)).clip(0, 1)[..., None]
    # sharp edge -> light blur -> medium -> wash, by distance from the picture
    f1 = (t / 0.12).clip(0, 1)
    f2 = ((t - 0.12) / 0.38).clip(0, 1)
    f3 = ((t - 0.5) / 0.5).clip(0, 1)
    out_img = pad * (1 - f1) + b1 * f1
    out_img = out_img * (1 - f2) + b2 * f2
    out_img = out_img * (1 - f3) + b3 * f3
    out_img[my:my + h, mx:mx + w] = src
    Image.fromarray(out_img.round().clip(0, 255).astype(np.uint8)).save(out, quality=92)
    info = {"file": out.name, "url": f"/files/cache/{meta['id']}/{out.name}", "fx": mx / w, "fy": my / h,
            "w": int(W), "h": int(H)}
    info_p.write_text(json.dumps(info), encoding="utf-8")
    return info
