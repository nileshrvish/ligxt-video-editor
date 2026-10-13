"""Face detection (OpenCV YuNet, models/face_detection_yunet_2023mar.onnx, MIT) for safe-area warnings.

Stored in the media meta as {"faces": {"version": 1, "frames": [{"t": seconds, "boxes": [[x, y, w, h, score], ...]}]}}
with boxes normalised to the source frame. Photos have one frame; videos are sampled once a second.
"""
from __future__ import annotations

import subprocess
import threading

import numpy as np

from .config import FFMPEG, ROOT
from .media import original_path

MODEL = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
VERSION = 1
DETECT_SIDE = 1280  # detection runs on frames scaled to this long side
MIN_SCORE = 0.6
MAX_SAMPLES = 120
_lock = threading.Lock()  # one detector, used by one job at a time
_det = None


def _detector():
    global _det
    if _det is None:
        import cv2
        _det = cv2.FaceDetectorYN.create(str(MODEL), "", (320, 320), MIN_SCORE, 0.3, 200)
    return _det


def _frames(meta: dict, sw: int, sh: int, times: list[float]):
    path = str(original_path(meta))
    for t in times:
        args = [FFMPEG, "-v", "error"] + (["-ss", f"{t:.3f}"] if meta["type"] == "video" else []) + [
            "-i", path, "-vf", f"scale={sw}:{sh}", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        r = subprocess.run(args, capture_output=True)
        if r.returncode == 0 and len(r.stdout) == sw * sh * 3:
            yield t, np.frombuffer(r.stdout, np.uint8).reshape(sh, sw, 3)


def detect(meta: dict, progress=None) -> dict:
    w, h = float(meta.get("width") or 0), float(meta.get("height") or 0)
    if meta.get("type") not in ("image", "video") or not w or not h:
        return {"version": VERSION, "frames": []}
    if meta.get("rotation") in (90, -90, 270, -270):
        w, h = h, w
    k = min(1.0, DETECT_SIDE / max(w, h))
    sw, sh = max(2, round(w * k)), max(2, round(h * k))
    if meta["type"] == "image":
        times = [0.0]
    else:
        dur = float(meta.get("duration") or 0)
        n = max(1, min(MAX_SAMPLES, int(dur) + 1))
        times = [min(dur - 0.05, i * dur / n + 0.05) for i in range(n)] if dur > 0.1 else [0.0]
    frames = []
    with _lock:
        det = _detector()
        det.setInputSize((sw, sh))
        for i, (t, img) in enumerate(_frames(meta, sw, sh, times)):
            _, f = det.detect(img)
            boxes = [[round(float(x) / sw, 4), round(float(y) / sh, 4), round(float(bw) / sw, 4), round(float(bh) / sh, 4), round(float(row[-1]), 3)]
                     for row in (f if f is not None else []) for x, y, bw, bh in [row[:4]]]
            frames.append({"t": round(t, 3), "boxes": boxes})
            if progress:
                progress((i + 1) / len(times))
    return {"version": VERSION, "frames": frames}
