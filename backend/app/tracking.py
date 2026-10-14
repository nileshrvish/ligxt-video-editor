"""Motion tracking: follow a face or any object through a clip (see follow.py for the data and maths).

Trackers (chosen by measuring accuracy and speed on the user's clips, see PROGRESS.md Phase 11):
- face: OpenCV YuNet on every frame (models/face_detection_yunet_2023mar.onnx, MIT) + association to the
  chosen face (predicted position, size and colour gates); short gaps are bridged with optical flow,
  longer ones held as "lost" until the same face is found again. Angle from the eye line.
- box (default for objects): OpenCV VitTrack (models/object_tracking_vittrack_2023sep.onnx, Apache-2.0),
  confidence from its score; when it loses the target it searches the whole frame for the starting
  appearance (template matching at three scales) and picks it up again.
- sam: SAM 2.1 small in the AI worker (clicks or a box; mask -> box), the most precise and best at
  objects that change shape, about 6-8 fps on the RTX 4050.
Objects get their rotation from feature points inside the box (optical flow, similarity transform).

Frames are decoded exactly like the export (accurate seek a quarter frame early, fps=...:round=near),
at most TRACK_SIDE px on the long side, so frame k of the track is source frame k.
"""
from __future__ import annotations

import math
import subprocess
import threading
import time
import uuid

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import ai_bridge, ai_models
from . import config
from .config import ROOT
from .ffmpeg_utils import Cancelled
from .jobs import JobContext, jobs
from .media import load_meta, original_path

router = APIRouter(prefix="/api/tracking", tags=["tracking"])

TRACK_SIDE = 960
FACE_MODEL = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
VIT_MODEL = ROOT / "models" / "object_tracking_vittrack_2023sep.onnx"
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv,format=yuv420p,")
MARGIN_S = 0.5  # tracked beyond the clip's ends, so small trims stay covered
BRIDGE_FRAMES = 8  # face: frames bridged with optical flow before the face counts as lost
APPEAR_MIN = 0.5  # face: colour similarity needed to pick a face up again after losing it
VIT_MIN = 0.1  # VitTrack's own give-up threshold (its default 0.3 drops hands that change shape)
LOST_RUN = 4  # objects: this many weak frames in a row = lost
REDETECT_MIN = 0.78  # template match needed to pick a lost object up again
_lock = threading.Lock()
_running: dict[str, str] = {}




# ---------------------------------------------------------------- frames
def src_dims(meta: dict) -> tuple[int, int]:
    w, h = int(meta.get("width") or 0), int(meta.get("height") or 0)
    if meta.get("rotation") in (90, -90, 270, -270):
        w, h = h, w
    return w, h


def work_size(meta: dict, side: int = TRACK_SIDE) -> tuple[int, int]:
    w, h = src_dims(meta)
    k = min(1.0, side / max(w, h))
    return max(2, int(round(w * k / 2)) * 2), max(2, int(round(h * k / 2)) * 2)


class FrameStore:
    """Decoded frames k0..k0+n-1 at work size, kept JPEG-compressed in memory (random access for the
    backward pass without holding raw frames)."""

    def __init__(self, meta: dict, k0: int, n: int, ctx=None, p0: float = 0.0, p1: float = 0.3):
        fps = float(meta.get("fps") or 30)
        self.w, self.h = work_size(meta)
        self.k0, self.jpg = k0, []
        tm = TONEMAP if meta.get("hdr") else ""
        args = [config.FFMPEG, "-v", "error", "-nostdin", "-ss", f"{max(0.0, (k0 - 0.25) / fps):.6f}", "-i", str(original_path(meta)),
                "-an", "-sn", "-vf", f"fps={fps:.6f}:round=near,{tm}scale={self.w}:{self.h}:flags=area", "-frames:v", str(n),
                "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        fb = self.w * self.h * 3
        try:
            while len(self.jpg) < n:
                if ctx and ctx.cancelled():
                    raise Cancelled()
                buf = proc.stdout.read(fb)
                if len(buf) < fb:
                    break
                img = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
                self.jpg.append(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 93])[1])
                if ctx and len(self.jpg) % 10 == 0:
                    ctx.progress(p0 + (p1 - p0) * len(self.jpg) / n, f"Reading frames {len(self.jpg)}/{n}")
        finally:
            proc.kill()
            proc.wait()
        if not self.jpg:
            raise RuntimeError("Could not read the video frames.")

    def __len__(self) -> int:
        return len(self.jpg)

    def __getitem__(self, i: int) -> np.ndarray:
        return cv2.imdecode(self.jpg[i], cv2.IMREAD_COLOR)


# ---------------------------------------------------------------- faces
_face_lock = threading.Lock()


def _yunet(w: int, h: int):
    det = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (w, h), 0.6, 0.3, 50)
    return det


def detect_faces(img: np.ndarray, det) -> list[dict]:
    _, r = det.detect(img)
    out = []
    for row in (r if r is not None else []):
        x, y, w, h = (float(v) for v in row[:4])
        lm = [(float(row[4 + 2 * i]), float(row[5 + 2 * i])) for i in range(5)]
        ang = math.degrees(math.atan2(lm[1][1] - lm[0][1], lm[1][0] - lm[0][0]))
        out.append({"box": [x, y, w, h], "c": (x + w / 2, y + h / 2), "score": float(row[-1]), "angle": ang})
    return out


def faces_at(meta: dict, t: float) -> list[dict]:
    """Faces on the frame at source time t (normalised boxes, biggest first) for choosing which to follow."""
    fps = float(meta.get("fps") or 30)
    k = int(math.floor(t * fps + 1e-6)) if meta.get("type") == "video" else 0
    st = FrameStore(meta, k, 1) if meta.get("type") == "video" else None
    if st is None:
        w, h = work_size(meta)
        img = cv2.resize(cv2.imread(str(original_path(meta))), (w, h), interpolation=cv2.INTER_AREA)
    else:
        img, w, h = st[0], st.w, st.h
    with _face_lock:
        fs = detect_faces(img, _yunet(w, h))
    fs.sort(key=lambda f: -f["box"][2] * f["box"][3])
    return [{"box": [round(f["box"][0] / w, 4), round(f["box"][1] / h, 4), round(f["box"][2] / w, 4), round(f["box"][3] / h, 4)],
             "score": round(f["score"], 3)} for f in fs]


def _hist(img: np.ndarray, box) -> np.ndarray | None:
    x, y, w, h = (int(round(v)) for v in box)
    x0, y0, x1, y1 = max(0, x), max(0, y), min(img.shape[1], x + w), min(img.shape[0], y + h)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    hsv = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    hst = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
    return cv2.normalize(hst, hst).flatten()


def _flow_box(prev: np.ndarray, cur: np.ndarray, box) -> tuple[list[float] | None, float]:
    """Move a box with the optical flow of feature points inside it (similarity transform)."""
    g0, g1 = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY), cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY)
    x, y, w, h = box
    mask = np.zeros_like(g0)
    mask[int(max(0, y)):int(max(0, y + h)), int(max(0, x)):int(max(0, x + w))] = 255
    p0 = cv2.goodFeaturesToTrack(g0, 120, 0.01, 3, mask=mask)
    if p0 is None or len(p0) < 6:
        return None, 0.0
    p1, st, _ = cv2.calcOpticalFlowPyrLK(g0, g1, p0, None, winSize=(21, 21), maxLevel=4)
    pb, st2, _ = cv2.calcOpticalFlowPyrLK(g1, g0, p1, None, winSize=(21, 21), maxLevel=4)
    good = (st.ravel() == 1) & (st2.ravel() == 1) & (np.linalg.norm((p0 - pb).reshape(-1, 2), axis=1) < 1.0)
    if good.sum() < 6:
        return None, 0.0
    Mx, inl = cv2.estimateAffinePartial2D(p0[good].reshape(-1, 2), p1[good].reshape(-1, 2), method=cv2.RANSAC, ransacReprojThreshold=2.0)
    if Mx is None:
        return None, 0.0
    s = float(np.hypot(Mx[0, 0], Mx[1, 0]))
    c = Mx @ np.array([x + w / 2, y + h / 2, 1.0])
    return [c[0] - w * s / 2, c[1] - h * s / 2, w * s, h * s], float(np.degrees(np.arctan2(Mx[1, 0], Mx[0, 0])))


def track_face(store: FrameStore, seed_i: int, seed: dict, direction: str, ctx=None, p0: float = 0.3, p1: float = 1.0) -> dict:
    """{frame index in the store: (box px, angle, conf)} for the face chosen at seed_i (by point or box)."""
    n, W, H = len(store), store.w, store.h
    with _face_lock:
        det = _yunet(W, H)
        dets: dict[int, list] = {}

        def get(i: int) -> list:
            if i not in dets:
                dets[i] = detect_faces(store[i], det)
            return dets[i]

        cands = get(seed_i)
        if seed.get("box"):
            bx, by, bw, bh = seed["box"]
            pt = ((bx + bw / 2) * W, (by + bh / 2) * H)
        else:
            px, py = seed.get("point") or (0.5, 0.4)
            pt = (px * W, py * H)
        if not cands:  # the detector can miss a turned face on this exact frame: look a few frames around it
            for off in range(1, 16):
                for j in (seed_i + off, seed_i - off):
                    if 0 <= j < n and get(j):
                        near = min(get(j), key=lambda f: math.hypot(f["c"][0] - pt[0], f["c"][1] - pt[1]))
                        if math.hypot(near["c"][0] - pt[0], near["c"][1] - pt[1]) < 1.5 * max(near["box"][2], near["box"][3]):
                            cands = [{**near, "score": near["score"] * 0.6}]
                            break
                if cands:
                    break
        if not cands:
            raise ValueError("No face found on this frame. Move the playhead to a frame where the face is visible.")
        start = min(cands, key=lambda f: math.hypot(f["c"][0] - pt[0], f["c"][1] - pt[1]))
        ref_hist = _hist(store[seed_i], start["box"])
        res = {seed_i: (start["box"], start["angle"], min(1.0, start["score"]))}
        dirs = [d for d in ((1, -1) if direction == "both" else (1,) if direction == "forward" else (-1,))]
        total = sum(n - 1 - seed_i if d == 1 else seed_i for d in dirs) or 1
        done = 0
        for d in dirs:
            last, vel, lost, prev_img, bridged = start, (0.0, 0.0), 0, store[seed_i], 0
            i = seed_i + d
            while 0 <= i < n:
                if ctx and ctx.cancelled():
                    raise Cancelled()
                img = store[i]
                fs = get(i)
                lb = last["box"]
                size = max(lb[2], lb[3])
                pc = (last["c"][0] + vel[0] * (lost + 1), last["c"][1] + vel[1] * (lost + 1))
                gate = size * min(3.0, 0.6 + 0.15 * lost)
                best, bd = None, 1e9
                for f in fs:
                    dist = math.hypot(f["c"][0] - pc[0], f["c"][1] - pc[1])
                    ratio = f["box"][2] / max(1.0, lb[2])
                    ok = dist < gate and (0.6 if lost < 5 else 0.45) < ratio < (1.6 if lost < 5 else 2.2)
                    sim = None
                    if ref_hist is not None and (lost > 0 or len(fs) > 1):
                        hf = _hist(img, f["box"])
                        sim = cv2.compareHist(ref_hist, hf, cv2.HISTCMP_CORREL) if hf is not None else -1.0
                        if lost > 0 and sim < APPEAR_MIN:  # coming back: it must look like the face we follow
                            ok = False
                        if not ok and lost > 5 and sim > APPEAR_MIN + 0.1 and 0.45 < ratio < 2.2:  # long gap: found by its colours
                            ok = True
                    cost = dist + (0 if sim is None else (1 - sim) * size)  # nearby faces: the look decides
                    if ok and cost < bd:
                        best, bd = f, cost
                if best is not None:
                    vel = ((best["c"][0] - last["c"][0]) / (lost + 1), (best["c"][1] - last["c"][1]) / (lost + 1))
                    res[i] = (best["box"], best["angle"], min(1.0, best["score"]))
                    if ref_hist is not None and lost == 0:  # follow slow lighting changes
                        hb = _hist(img, best["box"])
                        if hb is not None:
                            ref_hist = (0.9 * ref_hist + 0.1 * hb).astype(np.float32)
                    last, lost, bridged = best, 0, 0
                else:
                    moved = None
                    if bridged < BRIDGE_FRAMES and lost == bridged:
                        moved, da = _flow_box(prev_img, img, lb)
                    if moved is not None:
                        c = (moved[0] + moved[2] / 2, moved[1] + moved[3] / 2)
                        last = {"box": moved, "c": c, "score": 0.35, "angle": last["angle"] + da}
                        res[i] = (moved, last["angle"], 0.35)
                        bridged += 1
                        lost += 1
                    else:
                        res[i] = (last["box"], last["angle"], 0.0)
                        lost += 1
                        vel = (vel[0] * 0.8, vel[1] * 0.8)
                prev_img = img
                done += 1
                if ctx and done % 10 == 0:
                    ctx.progress(p0 + (p1 - p0) * done / total, f"Tracking the face {done}/{total}")
                i += d
    return res


# ---------------------------------------------------------------- objects (VitTrack)
def _vit():
    p = cv2.TrackerVit_Params()
    p.net = str(VIT_MODEL)
    try:
        p.tracking_score_threshold = VIT_MIN
    except AttributeError:
        pass
    return cv2.TrackerVit_create(p)


def _redetect(img: np.ndarray, templ: np.ndarray, box_wh) -> tuple[list[float], float] | None:
    best = None
    for s in (0.8, 1.0, 1.25):
        tw, th = max(8, int(templ.shape[1] * s)), max(8, int(templ.shape[0] * s))
        if tw >= img.shape[1] or th >= img.shape[0]:
            continue
        t2 = cv2.resize(templ, (tw, th), interpolation=cv2.INTER_AREA)
        r = cv2.matchTemplate(img, t2, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if best is None or mx > best[1]:
            best = ([float(loc[0]), float(loc[1]), float(tw), float(th)], float(mx))
    return best


def track_box(store: FrameStore, seed_i: int, box_n: list[float], direction: str, ctx=None, p0: float = 0.3, p1: float = 1.0) -> dict:
    n, W, H = len(store), store.w, store.h
    bx, by, bw, bh = box_n
    box = [bx * W, by * H, max(4.0, bw * W), max(4.0, bh * H)]
    img0 = store[seed_i]
    x, y, w, h = (int(round(v)) for v in box)
    templ = img0[max(0, y):max(0, y) + max(4, h), max(0, x):max(0, x) + max(4, w)].copy()
    res = {seed_i: (box, 0.0, 1.0)}
    dirs = [d for d in ((1, -1) if direction == "both" else (1,) if direction == "forward" else (-1,))]
    total = sum(n - 1 - seed_i if d == 1 else seed_i for d in dirs) or 1
    done = 0
    for d in dirs:
        tr = _vit()
        tr.init(img0, tuple(int(round(v)) for v in box))
        last, weak, prev_img, ang = list(box), 0, img0, 0.0
        i = seed_i + d
        while 0 <= i < n:
            if ctx and ctx.cancelled():
                raise Cancelled()
            img = store[i]
            ok, b = tr.update(img)
            score = float(tr.getTrackingScore()) if ok else 0.0
            if ok and score >= 0.15:
                nb = [float(v) for v in b]
                _, da = _flow_box(prev_img, img, last) if weak == 0 else (None, 0.0)
                ang += da
                conf = max(0.05, min(1.0, (score - 0.1) / 0.5))
                weak = 0 if score >= 0.2 else weak + 1
                last = nb
                res[i] = (nb, ang, conf if weak < LOST_RUN else 0.0)
            else:
                weak += 1
                res[i] = (last, ang, 0.0)
                if weak >= LOST_RUN and weak % 4 == 0:
                    found = _redetect(img, templ, (last[2], last[3]))
                    if found and found[1] > REDETECT_MIN:
                        # confirm: a fresh tracker started on the candidate must keep it on the next frame
                        j = i + d
                        cand = _vit()
                        cand.init(img, tuple(int(round(v)) for v in found[0]))
                        ok2 = 0 <= j < n and cand.update(store[j])[0] and cand.getTrackingScore() >= 0.35
                        if ok2:
                            last, weak, tr = found[0], 0, cand
                            tr = _vit()
                            tr.init(img, tuple(int(round(v)) for v in last))
                            res[i] = (last, ang, 0.4)
            prev_img = img
            done += 1
            if ctx and done % 10 == 0:
                ctx.progress(p0 + (p1 - p0) * done / total, f"Tracking {done}/{total}")
            i += d
    return res


# ---------------------------------------------------------------- objects (SAM 2.1, AI worker)
def track_sam(meta: dict, k0: int, n: int, seed_i: int, seed: dict, direction: str, ctx=None, p0: float = 0.0, p1: float = 1.0) -> tuple[dict, dict]:
    if ai_models.ai_python() is None:
        raise RuntimeError("Precise (AI) tracking needs the AI tools, which are not installed. Run setup.ps1, or use the box tracker.")
    mdir = ai_models.ensure_model(ai_models.OBJECT_MODEL, ctx, p0, p0 + (p1 - p0) * 0.1)
    cat = ai_models.CATALOG[ai_models.OBJECT_MODEL]
    sw, sh = src_dims(meta)
    args = {"src": str(original_path(meta)), "fps": float(meta.get("fps") or 30), "hdr": bool(meta.get("hdr")), "k0": k0, "n": n,
            "seed": seed_i, "width": sw, "height": sh, "direction": direction, "ffmpeg": config.FFMPEG,
            "ckpt": str(mdir / cat["files"][0]["name"]), "config": cat["config"], "device": ai_models.load_settings()["device"]}
    if seed.get("box"):
        args["box"] = seed["box"]
    if seed.get("points"):
        args["points"] = seed["points"]
    elif seed.get("point"):
        args["points"] = [{"x": seed["point"][0], "y": seed["point"][1], "label": 1}]
    q0 = p0 + (p1 - p0) * 0.1
    stats = ai_bridge.call("track", args, progress=lambda p, m: ctx.progress(q0 + (p1 - q0) * p, m) if ctx else None,
                           cancelled=ctx.cancelled if ctx else None)
    res = {}
    for i, b in enumerate(stats.get("boxes") or []):
        if b is None:
            continue
        res[i] = b  # [cx, cy, w, h] normalised, or [] when the object is not visible
    return res, stats


# ---------------------------------------------------------------- the job
def new_id() -> str:
    return "trk_" + uuid.uuid4().hex[:8]


def merge(track: dict | None, meta: dict, k0: int, entries: dict[int, list], replace: tuple[int, int]) -> dict:
    """Put new per-frame entries ([cx, cy, w, h, angle, conf] by source frame) into a track, replacing
    frames in [replace[0], replace[1]] and extending its range; frames nobody tracked hold their
    neighbour (conf 0)."""
    old = {}
    if track and track.get("data"):
        ok0 = int(track.get("k0", 0))
        old = {ok0 + i: list(e) for i, e in enumerate(track["data"])}
    for k in range(replace[0], replace[1] + 1):
        old.pop(k, None)
    old.update({k: list(e) for k, e in entries.items()})
    ks = sorted(old)
    a, b = ks[0], ks[-1]
    data, prev = [], None
    for k in range(a, b + 1):
        e = old.get(k)
        if e is None:
            e = list(prev[:5]) + [0.0] if prev else None
        if e is None:  # before the first tracked frame: the next one, held
            nxt = next(old[j] for j in ks if j > k)
            e = list(nxt[:5]) + [0.0]
        data.append([round(float(v), 5) for v in e[:5]] + [round(float(e[5]), 3)])
        prev = e
    out = dict(track or {})
    out.update({"k0": a, "data": data, "fps": float(meta.get("fps") or 30), "mediaId": meta["id"]})
    return out


def run_track(meta: dict, req: dict, ctx: JobContext | None = None) -> dict:
    """req: {method: face|box|sam, seed: {t (source s), box?: [x, y, w, h] (fractions), point?: [x, y]},
    range: [s0, s1] source seconds to cover, direction: both|forward|backward, track?: existing track,
    name?}. Returns {"track": ..., "stats": {...}}."""
    t_start = time.time()
    fps = float(meta.get("fps") or 30)
    dur = float(meta.get("duration") or 0)
    nframes = max(1, int(math.floor(dur * fps + 1e-6)))
    s0, s1 = req.get("range") or (0.0, dur)
    k_lo = max(0, int(math.floor((float(s0) - MARGIN_S) * fps)))
    k_hi = min(nframes - 1, int(math.ceil((float(s1) + MARGIN_S) * fps)))
    seed = req.get("seed") or {}
    k_seed = min(max(int(math.floor(float(seed.get("t", s0)) * fps + 1e-6)), k_lo), k_hi)
    direction = req.get("direction", "both")
    if direction == "forward":
        k_lo = k_seed
    elif direction == "backward":
        k_hi = k_seed
    n = k_hi - k_lo + 1
    method = req.get("method", "box")
    w, h = work_size(meta)
    entries: dict[int, list] = {}
    stats: dict = {"method": method, "frames": n}
    if method == "sam":
        res, st = track_sam(meta, k_lo, n, k_seed - k_lo, seed, direction, ctx, 0.0, 0.85)
        stats.update(peakVram=st.get("peakVram"), device=st.get("device"))
        store = None
        for i, b in res.items():
            if b:
                entries[k_lo + i] = [b[0], b[1], b[2], b[3], 0.0, 1.0]
        # held through frames where the object is hidden
        last = None
        for k in range(k_lo, k_hi + 1):
            if k in entries:
                last = entries[k]
            elif last is not None:
                entries[k] = last[:5] + [0.0]
        if not entries:
            raise ValueError("The object was not found. Click on it (or draw a box around it) on a frame where it is visible.")
    else:
        store = FrameStore(meta, k_lo, n, ctx, 0.0, 0.3)
        k_hi = k_lo + len(store) - 1
        si = min(k_seed - k_lo, len(store) - 1)
        if method == "face":
            res = track_face(store, si, seed, direction, ctx, 0.3, 1.0)
        else:
            if not seed.get("box"):
                raise ValueError("Draw a box around the object to track.")
            res = track_box(store, si, seed["box"], direction, ctx, 0.3, 1.0)
        for i, (b, ang, conf) in res.items():
            entries[k_lo + i] = [(b[0] + b[2] / 2) / w, (b[1] + b[3] / 2) / h, b[2] / w, b[3] / h, ang, conf]
    track = merge(req.get("track"), meta, k_lo, entries, (k_lo, k_hi))
    track.setdefault("id", new_id())
    track.setdefault("name", req.get("name") or ("Face" if method == "face" else "Object"))
    track["kind"] = "face" if method == "face" else "object"
    track["method"] = method
    keys = [k for k in (track.get("keys") or []) if not (k_lo <= int(k.get("k", -1)) <= k_hi) or int(k["k"]) != k_seed]
    if seed.get("box"):
        keys.append({"k": k_seed, "box": [round(float(v), 4) for v in seed["box"]]})
    track["keys"] = keys
    el = max(1e-3, time.time() - t_start)
    confs = [e[5] for e in track["data"]]
    stats.update(seconds=round(el, 2), fps=round(n / el, 1), lost=sum(1 for c in confs if c <= 0),
                 low=sum(1 for c in confs if 0 < c < 0.5))
    return {"track": track, "stats": stats}


# ---------------------------------------------------------------- API
class TrackBody(BaseModel):
    mediaId: str
    method: str = "box"  # face | box | sam
    seed: dict
    range: list[float] | None = None
    direction: str = "both"
    track: dict | None = None
    name: str | None = None


@router.post("/start")
def start(body: TrackBody):
    meta = load_meta(body.mediaId)
    if not meta or meta.get("type") != "video":
        raise HTTPException(404, "Tracking needs a video clip.")
    if body.method not in ("face", "box", "sam"):
        raise HTTPException(400, "method must be face, box or sam.")
    if body.direction not in ("both", "forward", "backward"):
        raise HTTPException(400, "direction must be both, forward or backward.")
    if body.method == "sam" and ai_models.ai_python() is None:
        raise HTTPException(503, "Precise (AI) tracking needs the AI tools, which are not installed. Run setup.ps1, or draw a box instead.")
    req = body.model_dump()

    def run(ctx: JobContext) -> dict:
        return run_track(meta, req, ctx)

    label = {"face": "Tracking a face", "box": "Tracking an object", "sam": "Tracking an object (AI)"}[body.method]
    job = jobs.submit("track", f"{label}: {meta['name']}", run, queue="ai" if body.method == "sam" else "media",
                      ref={"mediaId": meta["id"]})
    return {"jobId": job.id}


class FacesBody(BaseModel):
    mediaId: str
    t: float = 0.0


@router.post("/faces")
def faces(body: FacesBody):
    meta = load_meta(body.mediaId)
    if not meta or meta.get("type") not in ("video", "image"):
        raise HTTPException(404, "Media not found.")
    return {"faces": faces_at(meta, body.t)}
