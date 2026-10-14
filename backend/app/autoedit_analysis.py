"""Auto-edit, step 1: analyse raw footage (one background job per batch, cached per file).

Each video is decoded once at SIDE px / SAMPLE_FPS (ffmpeg, like the export: rotation and HDR tone-mapped) and gets
- shots: PySceneDetect's ContentDetector (BSD-3-Clause) on the decoded frames,
- per-sample scores (0..1): sharpness (Laplacian variance, log scale), exposure (mid-tones, clipping),
  motion energy (optical-flow magnitude), stability (camera shake = high-frequency part of the global motion),
  composition (colourfulness + where the salient subject sits: thirds / centre) and faces (YuNet at FACE_FPS:
  count, size, facing the camera),
- unusable moments: blur, black, lens covered, extreme shake,
- per shot: a signature (dHash + colour histogram) for near-duplicate detection, a wide / close / detail guess, and a
  thumbnail of its best moment.
Photos get one sample. No GPU is used. Results: cache/<media>/autoedit/analysis_v<VERSION>.json (+ shot thumbnails).
"""
from __future__ import annotations

import json
import math
import subprocess
import threading
import time

import cv2
import numpy as np

from . import config
from .config import CACHE_DIR
from .media import original_path

VERSION = 1
SIDE = 320  # analysis frames (long side)
SAMPLE_FPS = 8.0
FACE_FPS = 2.0
FACE_SIDE = 640
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv,format=yuv420p,")
# score calibration (on 320 px frames)
SHARP_LO, SHARP_HI = 1.2, 3.0  # log10 Laplacian variance: blurry .. crisp
MOTION_HI = 4.0  # px per sample of flow (at 160 px, 8 fps) = "a lot happening"
SHAKE_HI = 1.6  # px of jitter per sample (at 160 px) = shaky
BLUR_UNUSABLE = 0.12
BLACK_LUMA = 0.06
_face_lock = threading.Lock()


def adir(mid: str):
    d = CACHE_DIR / mid / "autoedit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_path(mid: str):
    return adir(mid) / f"analysis_v{VERSION}.json"


def cached(mid: str) -> dict | None:
    p = cache_path(mid)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def _size(meta: dict, side: int) -> tuple[int, int]:
    w, h = float(meta.get("width") or 16), float(meta.get("height") or 9)
    if meta.get("rotation") in (90, -90, 270, -270):
        w, h = h, w
    k = side / max(w, h)
    return max(2, int(round(w * k / 2)) * 2), max(2, int(round(h * k / 2)) * 2)


def decode(meta: dict, fps: float, side: int, ctx=None, p0=0.0, p1=1.0):
    """Yield (t, BGR frame) at `fps` over the whole video (or the photo once)."""
    w, h = _size(meta, side)
    src = str(original_path(meta))
    if meta.get("type") == "image":
        img = cv2.imread(src, cv2.IMREAD_COLOR)
        if img is None:
            return
        yield 0.0, cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        return
    tm = TONEMAP if meta.get("hdr") else ""
    proc = subprocess.Popen([config.FFMPEG, "-v", "error", "-nostdin", "-i", src, "-an", "-sn", "-vf",
                             f"fps={fps:.6f}:round=near,{tm}scale={w}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    fb = w * h * 3
    n = max(1, int(float(meta.get("duration") or 1) * fps))
    i = 0
    try:
        while True:
            if ctx and ctx.cancelled():
                from .ffmpeg_utils import Cancelled
                raise Cancelled()
            buf = proc.stdout.read(fb)
            if len(buf) < fb:
                break
            yield i / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            i += 1
            if ctx and i % 16 == 0:
                ctx.progress(p0 + (p1 - p0) * min(1.0, i / n))
    finally:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------- per-frame measures
def sharpness(gray: np.ndarray) -> float:
    return float(np.clip((math.log10(max(1e-3, cv2.Laplacian(gray, cv2.CV_32F).var())) - SHARP_LO) / (SHARP_HI - SHARP_LO), 0, 1))


def exposure(gray: np.ndarray) -> tuple[float, float, float, float]:
    """(score, mean, crushed fraction, clipped fraction)."""
    g = gray.astype(np.float32) / 255
    m = float(g.mean())
    crushed, clipped = float((g < 0.03).mean()), float((g > 0.97).mean())
    s = 1 - min(1.0, abs(m - 0.45) * 1.5) - crushed * 1.5 - clipped * 1.5
    return float(np.clip(s, 0, 1)), m, crushed, clipped


def colourfulness(bgr: np.ndarray) -> float:
    b, g, r = (bgr[..., i].astype(np.float32) for i in range(3))
    rg, yb = r - g, 0.5 * (r + g) - b
    c = math.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * math.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
    return float(np.clip(c / 90, 0, 1))


def saliency(gray: np.ndarray) -> np.ndarray:
    """Spectral-residual saliency (Hou & Zhang 2007) on a 64 px image, normalised 0..1."""
    small = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
    f = np.fft.fft2(small)
    la = np.log(np.abs(f) + 1e-6)
    sr = la - cv2.blur(la, (3, 3))
    s = np.abs(np.fft.ifft2(np.exp(sr + 1j * np.angle(f)))) ** 2
    s = cv2.GaussianBlur(s.astype(np.float32), (0, 0), 2.5)
    return s / max(1e-9, s.max())


def placement(sal: np.ndarray) -> tuple[float, tuple[float, float], float]:
    """(score, salient centroid (fractions), spread): a subject near the thirds or the centre scores high."""
    ys, xs = np.mgrid[0:64, 0:64] / 63.0
    m = sal > 0.35
    if m.sum() < 3:
        return 0.5, (0.5, 0.5), 1.0
    w = sal * m
    cx, cy = float((xs * w).sum() / w.sum()), float((ys * w).sum() / w.sum())
    lines = (1 / 3, 0.5, 2 / 3)
    d = min(abs(cx - a) for a in lines) + min(abs(cy - a) for a in lines)
    spread = float(m.mean())
    return float(np.clip(1 - d * 3, 0, 1)), (cx, cy), spread


def dof(gray: np.ndarray) -> float:
    """Shallow depth of field, 0..1: among the textured 8x8 blocks (flat sky doesn't count), the fraction that are
    soft compared with the sharpest. High = a close-up / detail shot with a blurred background."""
    g = gray.astype(np.float32) / 255
    lap = np.abs(cv2.Laplacian(g, cv2.CV_32F))
    h, w = lap.shape
    hh, ww = h // 8 * 8, w // 8 * 8
    lb = lap[:hh, :ww].reshape(8, h // 8, 8, w // 8).mean((1, 3))
    sb = g[:hh, :ww].reshape(8, h // 8, 8, w // 8).std((1, 3))
    tex = sb > 0.035
    if lb.max() <= 0 or tex.sum() < 6:
        return 0.0
    return float((lb[tex] < 0.3 * lb.max()).mean())


def camera_motion(a: np.ndarray, b: np.ndarray, flow: np.ndarray) -> tuple[float, float]:
    """The camera's own motion between two frames: the dominant (RANSAC) motion of feature points, so a big
    moving subject (a hand, a car) isn't mistaken for camera shake. Falls back to the median flow."""
    p = cv2.goodFeaturesToTrack(a, 150, 0.01, 4)
    if p is not None and len(p) >= 12:
        q, st, _ = cv2.calcOpticalFlowPyrLK(a, b, p, None, winSize=(15, 15), maxLevel=3)
        ok = st.ravel() == 1
        if ok.sum() >= 10:
            m, inl = cv2.estimateAffinePartial2D(p[ok].reshape(-1, 2), q[ok].reshape(-1, 2), method=cv2.RANSAC, ransacReprojThreshold=1.0)
            if m is not None and inl is not None and inl.mean() > 0.4:
                return float(m[0, 2]), float(m[1, 2])
    return float(np.median(flow[..., 0])), float(np.median(flow[..., 1]))


def dhash(gray: np.ndarray) -> int:
    s = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (s[:, 1:] > s[:, :-1]).flatten()
    return int(sum(1 << i for i, v in enumerate(bits) if v))


def hsv_hist(bgr: np.ndarray) -> list[float]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256]).flatten()
    h = h / max(1e-9, h.sum())
    return [round(float(v), 5) for v in h]


def similarity(a: dict, b: dict) -> float:
    """0..1 how alike two shots look (dHash bits + colour histogram)."""
    ham = bin(int(a["dhash"]) ^ int(b["dhash"])).count("1")
    hd = 1 - ham / 64
    ha, hb = np.array(a["hist"]), np.array(b["hist"])
    hi = float(np.minimum(ha, hb).sum())
    return round(0.5 * hd + 0.5 * hi, 4)


# ---------------------------------------------------------------- faces
def _yunet(w: int, h: int):
    from .tracking import FACE_MODEL
    return cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (w, h), 0.7, 0.3, 50)


def faces_in(img: np.ndarray, det) -> list[dict]:
    _, r = det.detect(img)
    out = []
    H, W = img.shape[:2]
    for row in (r if r is not None else []):
        x, y, w, h = (float(v) for v in row[:4])
        re, le, nose = (row[4], row[5]), (row[6], row[7]), (row[8], row[9])
        ed = math.hypot(le[0] - re[0], le[1] - re[1])
        mid = (re[0] + le[0]) / 2
        frontal = ed > 1 and abs(nose[0] - mid) / ed < 0.28
        out.append({"box": [x / W, y / H, w / W, h / H], "size": h / H, "frontal": bool(frontal), "score": float(row[-1])})
    return out


# ---------------------------------------------------------------- the analysis
def analyze(meta: dict, ctx=None, p0: float = 0.0, p1: float = 1.0, force: bool = False) -> dict:
    mid = meta["id"]
    if not force:
        c = cached(mid)
        if c:
            return c
    t_start = time.time()
    from scenedetect.detectors import ContentDetector
    det_scene = ContentDetector(threshold=27.0, min_scene_len=int(SAMPLE_FPS * 0.8))
    fw, fh = _size(meta, FACE_SIDE)
    samples = []
    cuts: list[float] = []
    prev_small = None
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST)
    face_every = max(1, int(round(SAMPLE_FPS / FACE_FPS)))
    is_img = meta.get("type") == "image"
    gmot = []
    frames_kept = {}
    with _face_lock:
        fdet = _yunet(fw, fh)
        for i, (t, f) in enumerate(decode(meta, SAMPLE_FPS, SIDE, ctx, p0, p0 + (p1 - p0) * 0.9)):
            gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (gray.shape[1] // 2, gray.shape[0] // 2), interpolation=cv2.INTER_AREA)
            if not is_img:
                for c in det_scene.process_frame(i, f):
                    if c > 0:
                        cuts.append(round(c / SAMPLE_FPS, 3))
            sh = sharpness(gray)
            ex, mean, crushed, clipped = exposure(gray)
            col = colourfulness(f)
            sal = saliency(gray)
            pl, cen, spread = placement(sal)
            motion, gx, gy = 0.0, 0.0, 0.0
            if prev_small is not None:
                flow = dis.calc(prev_small, small, None)
                mag = np.sqrt((flow ** 2).sum(-1))
                motion = float(mag.mean())
                gx, gy = camera_motion(prev_small, small, flow)
            prev_small = small
            gmot.append((gx, gy))
            s = {"t": round(t, 3), "sharp": round(sh, 3), "exp": round(ex, 3), "luma": round(mean, 3), "col": round(col, 3),
                 "place": round(pl, 3), "cx": round(cen[0], 3), "cy": round(cen[1], 3), "motion": round(motion, 3),
                 "dof": round(dof(gray), 3), "std": round(float(gray.std()) / 255, 3), "faces": None}
            if i % face_every == 0:
                big = cv2.resize(f, (fw, fh), interpolation=cv2.INTER_LINEAR)
                s["faces"] = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in fc.items() if k != "box"} | {"box": [round(v, 4) for v in fc["box"]]}
                              for fc in faces_in(big, fdet)]
            samples.append(s)
            if i % 4 == 0:  # for shot thumbnails / signatures, kept small
                frames_kept[i] = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 88])[1]
    if not samples:
        raise RuntimeError(f"Could not read '{meta.get('name')}'.")
    # camera shake: the high-frequency part of the global (median) motion
    g = np.array(gmot, dtype=np.float32)
    if len(g) >= 5:
        k = np.ones(5) / 5
        smooth = np.stack([np.convolve(g[:, j], k, mode="same") for j in range(2)], 1)
        jitter = np.sqrt(((g - smooth) ** 2).sum(1))
    else:
        jitter = np.zeros(len(g))
    # faces carried to the samples between face samples
    last_faces: list = []
    for i, s in enumerate(samples):
        if s["faces"] is not None:
            last_faces = s["faces"]
        else:
            s["faces"] = last_faces
        s["shake"] = round(float(jitter[i]), 3) if i < len(jitter) else 0.0
        s["stable"] = round(float(np.clip(1 - s["shake"] / SHAKE_HI, 0, 1)), 3)
        s["energy"] = round(float(np.clip(s["motion"] / MOTION_HI, 0, 1)), 3)
        nf = [f for f in s["faces"] if f["score"] > 0.75]
        s["nfaces"] = len(nf)
        s["frontal"] = sum(1 for f in nf if f["frontal"])
        s["faceSize"] = round(max([f["size"] for f in nf] or [0.0]), 3)
        s["comp"] = round(0.55 * s["place"] + 0.45 * s["col"], 3)
        bad = []
        if s["luma"] < BLACK_LUMA:
            bad.append("black")
        elif s["luma"] < 0.13 and s["std"] < 0.05:
            bad.append("lens covered")
        if s["sharp"] < BLUR_UNUSABLE and s["energy"] < 0.6:
            bad.append("blurry")
        win = [samples[j]["shake"] if "shake" in samples[j] else float(jitter[j]) for j in range(max(0, i - 2), min(len(samples), i + 3)) if j < len(jitter)]
        if win and float(np.median(win)) > SHAKE_HI * 2.2:  # sustained, not one jolt
            bad.append("extreme shake")
        s["bad"] = bad
    dur = float(meta.get("duration") or 0) if not is_img else 0.0
    bounds = [0.0] + sorted(c for c in cuts if 0.3 < c < dur - 0.3) + [dur if not is_img else 0.0]
    shots = []
    for si, (a, b) in enumerate(zip(bounds, bounds[1:] if not is_img else [0.0])):
        idx = [i for i, s in enumerate(samples) if (a <= s["t"] < b) or is_img]
        if not idx:
            continue
        good = [i for i in idx if not samples[i]["bad"]] or idx
        q = [quality(samples[i]) for i in good]
        best = good[int(np.argmax(q))]
        # signature + thumbnail from the best moment (decoded again at SIDE: the kept frames are every 4th sample)
        inside = [k for k in frames_kept if idx[0] <= k <= idx[-1]] or list(frames_kept)
        if not inside:
            continue
        img = cv2.imdecode(frames_kept[min(inside, key=lambda k: abs(k - best))], cv2.IMREAD_COLOR)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        thumb = f"shot_{si}.jpg"
        tw = 180
        cv2.imwrite(str(adir(mid) / thumb), cv2.resize(img, (tw, int(round(img.shape[0] * tw / img.shape[1])))), [cv2.IMWRITE_JPEG_QUALITY, 82])
        fs = max(samples[i]["faceSize"] for i in idx)
        d_ = float(np.median([samples[i]["dof"] for i in idx]))
        kind = "close" if fs > 0.18 else "medium" if fs > 0.07 else ("detail" if d_ > 0.7 else "wide")
        bad_frac = sum(1 for i in idx if samples[i]["bad"]) / len(idx)
        shots.append({"i": si, "start": round(a, 3), "end": round(b, 3), "best": samples[best]["t"], "thumb": f"/files/cache/{mid}/autoedit/{thumb}",
                      "dhash": str(dhash(gray)), "hist": hsv_hist(img), "kind": kind, "badFrac": round(bad_frac, 3),
                      "quality": round(float(np.mean(q)), 3), "energy": round(float(np.mean([samples[i]["energy"] for i in idx])), 3)})
    # unusable stretches
    unusable = []
    for s in samples:
        if s["bad"]:
            if unusable and abs(unusable[-1][1] - s["t"]) < 1.5 / SAMPLE_FPS and unusable[-1][2] == s["bad"][0]:
                unusable[-1][1] = round(s["t"] + 1 / SAMPLE_FPS, 3)
            else:
                unusable.append([s["t"], round(s["t"] + 1 / SAMPLE_FPS, 3), s["bad"][0]])
    el = max(1e-3, time.time() - t_start)
    w0, h0 = _size(meta, 10000)
    out = {"version": VERSION, "mediaId": mid, "name": meta.get("name"), "type": meta.get("type"), "duration": dur, "fps": SAMPLE_FPS,
           "width": w0, "height": h0, "landscape": w0 > h0, "samples": samples, "shots": shots, "unusable": unusable,
           "stats": {"seconds": round(el, 2), "speed": round(dur / el, 2) if dur else None, "shots": len(shots)}}
    cache_path(mid).write_text(json.dumps(out), encoding="utf-8")
    return out


def quality(s: dict) -> float:
    """Overall look of one moment, 0..1 (style weights are applied when drafting)."""
    if s.get("bad"):
        return 0.05
    face = min(1.0, s.get("frontal", 0) * 0.6 + min(0.4, s.get("faceSize", 0) * 2)) if s.get("nfaces") else 0.0
    return round(0.32 * s["sharp"] + 0.22 * s["exp"] + 0.2 * s["comp"] + 0.14 * s["stable"] + 0.12 * face, 4)


def duplicates(analyses: list[dict], thr: float = 0.86) -> list[tuple[tuple[str, int], tuple[str, int], float]]:
    """Pairs of shots (media id, shot index) that look nearly the same."""
    shots = [(a["mediaId"], s) for a in analyses for s in a["shots"]]
    out = []
    for i in range(len(shots)):
        for j in range(i + 1, len(shots)):
            sim = similarity(shots[i][1], shots[j][1])
            if sim >= thr:
                out.append(((shots[i][0], shots[i][1]["i"]), (shots[j][0], shots[j][1]["i"]), sim))
    return out
