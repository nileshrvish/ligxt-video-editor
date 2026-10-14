"""Object tracking with SAM 2.1 small (Apache-2.0, Meta): a box or clicks on one frame, propagated through a
window of source frames (forwards and/or backwards). Each frame's mask becomes a box (centre + size,
fractions of the frame) or [] when the object isn't visible (hidden / out of the picture).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time

import numpy as np

from matting import CREATE_NO_WINDOW, TONEMAP
from segment import SAM_SIDE, _load, _small

MIN_PIXELS = 24  # smaller masks count as "not visible"


def track(ctx, args: dict) -> dict:
    """args: src, fps, hdr, k0, n, seed (frame in the window), width, height (source, after rotation),
    box [x, y, w, h] (fractions) and/or points [{x, y (fractions), label}], direction both|forward|backward,
    ckpt, config, device, ffmpeg."""
    import torch
    t0 = time.time()
    ffmpeg = args.get("ffmpeg", "ffmpeg")
    w, h = int(args["width"]), int(args["height"])
    fps = float(args.get("fps") or 30)
    k0, n, seed = int(args["k0"]), int(args["n"]), int(args["seed"])
    direction = args.get("direction", "both")
    ctx.progress(0.0, "Loading the model", force=True)
    sam = _load(args)
    cuda = sam["device"].type == "cuda"
    if cuda:
        torch.cuda.reset_peak_memory_stats()
    sw, sh = _small(w, h)
    tmp = tempfile.mkdtemp(prefix="ligxt_trk_")
    boxes: list = [None] * n
    try:
        tm = TONEMAP if args.get("hdr") else ""
        proc = subprocess.Popen([ffmpeg, "-v", "error", "-nostdin", "-ss", f"{max(0.0, (k0 - 0.25) / fps):.6f}", "-i", args["src"], "-an", "-sn",
                                 "-vf", f"fps={fps:.6f}:round=near,{tm}scale={sw}:{sh}:flags=area", "-frames:v", str(n),
                                 "-q:v", "2", "-start_number", "0", os.path.join(tmp, "%d.jpg")],
                                stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        while proc.poll() is None:
            ctx.check()
            ctx.progress(0.15 * len(os.listdir(tmp)) / max(1, n), f"Reading frames {len(os.listdir(tmp))}/{n}")
            time.sleep(0.2)
        got = len([x for x in os.listdir(tmp) if x.endswith(".jpg")])
        if got == 0:
            raise RuntimeError("Could not read the frames: " + (proc.stderr.read() or b"").decode("utf-8", "replace")[-300:])
        seed = min(seed, got - 1)
        pred = sam["video"]
        autocast = torch.autocast("cuda", dtype=torch.bfloat16) if cuda else torch.autocast("cpu", enabled=False)
        with torch.inference_mode(), autocast:
            state = pred.init_state(video_path=tmp, offload_video_to_cpu=True, offload_state_to_cpu=not cuda)
            kw = {}
            if args.get("box"):
                bx, by, bw, bh = (float(v) for v in args["box"])
                kw["box"] = np.array([bx * sw, by * sh, (bx + bw) * sw, (by + bh) * sh], np.float32)
            if args.get("points"):
                kw["points"] = np.array([[float(p["x"]) * sw, float(p["y"]) * sh] for p in args["points"]], np.float32)
                kw["labels"] = np.array([int(p.get("label", 1)) for p in args["points"]], np.int32)
            pred.add_new_points_or_box(state, frame_idx=seed, obj_id=1, **kw)
            dirs = [False, True] if direction == "both" else [direction == "backward"]
            total = sum((got - seed) if not r else (seed + 1) for r in dirs)
            done = 0
            for rev in dirs:
                for fi, _ids, masks in pred.propagate_in_video(state, start_frame_idx=seed, reverse=rev):
                    ctx.check()
                    mk = (masks[0, 0] > 0).cpu().numpy()
                    ys, xs = np.nonzero(mk)
                    if len(xs) < MIN_PIXELS:
                        boxes[fi] = []
                    else:
                        x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
                        boxes[fi] = [round(float((x0 + x1) / 2 / sw), 5), round(float((y0 + y1) / 2 / sh), 5),
                                     round(float((x1 - x0) / sw), 5), round(float((y1 - y0) / sh), 5)]
                    done += 1
                    ctx.progress(0.15 + 0.85 * min(1.0, done / max(1, total)), f"Tracking {done}/{total} frames")
            pred.reset_state(state)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    el = max(1e-3, time.time() - t0)
    return {"boxes": boxes, "device": sam["device"].type, "peakVram": int(torch.cuda.max_memory_allocated()) if cuda else 0,
            "seconds": round(el, 2), "fps": round(n / el, 2), "side": SAM_SIDE}
