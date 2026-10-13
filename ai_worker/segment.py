"""Click-to-select cutout for any object (SAM 2.1, Apache-2.0, Meta).

Points (x, y in source pixels, label 1 = this, 0 = not this) on one or more frames of a window of
source frames select the object; SAM 2 tracks it through the window (forwards and backwards from the
clicked frames). Masks come out at SAM's resolution and are upsampled to the source size with the same
guided filter as the person matting, then written exactly like a matting result (FFV1 gray / PNG).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time

import numpy as np

from matting import CREATE_NO_WINDOW, TONEMAP, decode_proc, encode_proc, read_frames, to_full, unload

_sam = {"key": None, "video": None, "image": None, "device": None}
SAM_SIDE = 1024  # frames handed to SAM 2 (it works at 1024x1024 internally)


def _load(args: dict):
    import torch
    key = (args["ckpt"], args.get("device", "auto"))
    if _sam["key"] == key:
        return _sam
    unload()  # free the matting model (and any previous SAM)
    use_cuda = args.get("device", "auto") != "cpu" and torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")
    from sam2.build_sam import build_sam2, build_sam2_video_predictor
    from sam2.sam2_image_predictor import SAM2ImagePredictor
    cfg = args.get("config", "configs/sam2.1/sam2.1_hiera_s.yaml")
    video = build_sam2_video_predictor(cfg, args["ckpt"], device=device)
    image = SAM2ImagePredictor(build_sam2(cfg, args["ckpt"], device=device))
    _sam.update(key=key, video=video, image=image, device=device)
    return _sam


def _small(w: int, h: int) -> tuple[int, int]:
    s = min(1.0, SAM_SIDE / max(w, h))
    return max(2, int(round(w * s / 2)) * 2), max(2, int(round(h * s / 2)) * 2)


def segment(ctx, args: dict) -> dict:
    """args: ckpt, config, src, kind (video|image), out, width, height, fps, hdr, k0, n, points
    [{"f": frame in the window, "x", "y" (source px), "label": 1|0}], detail, device, ffmpeg, preview (bool:
    only the first point's frame, written as a PNG)."""
    import torch
    t_start = time.time()
    ffmpeg = args.get("ffmpeg", "ffmpeg")
    kind = args.get("kind", "video")
    w, h = int(args["width"]), int(args["height"])
    fps = float(args.get("fps") or 30)
    k0, n = int(args.get("k0", 0)), int(args.get("n", 1)) if kind == "video" else 1
    pts = args.get("points") or []
    if not any(int(p.get("label", 1)) == 1 for p in pts):
        raise ValueError("Click at least once on the object to select it.")
    ctx.progress(0.0, "Loading the model", force=True)
    sam = _load(args)
    dev = sam["device"]
    cuda = dev.type == "cuda"
    if cuda:
        torch.cuda.reset_peak_memory_stats()
    sw, sh = _small(w, h)
    sx, sy = sw / w, sh / h
    preview = bool(args.get("preview"))
    by_frame: dict[int, list] = {}
    for p in pts:
        by_frame.setdefault(int(p.get("f", 0)) if kind == "video" else 0, []).append(p)
    autocast = torch.autocast("cuda", dtype=torch.bfloat16) if cuda else torch.autocast("cpu", enabled=False)
    low: dict[int, np.ndarray] = {}  # frame -> logits at (sh, sw), float16

    if kind == "image" or preview:
        f0 = min(by_frame) if kind == "video" else 0
        dec = decode_proc(ffmpeg, args["src"], kind, sw, sh, fps, bool(args.get("hdr")), k0 + f0, 1)
        img = read_frames(dec, sw, sh, 1)
        dec.kill()
        if not img:
            raise RuntimeError("Could not read the frame.")
        fp = by_frame[f0]
        with torch.inference_mode(), autocast:
            sam["image"].set_image(img[0])
            masks, scores, logits = sam["image"].predict(
                point_coords=np.array([[p["x"] * sx, p["y"] * sy] for p in fp], np.float32),
                point_labels=np.array([int(p.get("label", 1)) for p in fp], np.int32),
                multimask_output=False, return_logits=True)
        low[0] = masks[0].astype(np.float16)
        frames_out, first = 1, f0
    else:
        tmp = tempfile.mkdtemp(prefix="ligxt_sam_")
        try:
            # 1) the window's frames as JPEGs for SAM 2 (its video input)
            tm = TONEMAP if args.get("hdr") else ""
            ss = max(0.0, (k0 - 0.25) / fps)
            proc = subprocess.Popen([ffmpeg, "-v", "error", "-nostdin", "-ss", f"{ss:.6f}", "-i", args["src"], "-an", "-sn",
                                     "-vf", f"fps={fps:.6f}:round=near,{tm}scale={sw}:{sh}:flags=area", "-frames:v", str(n),
                                     "-q:v", "2", "-start_number", "0", os.path.join(tmp, "%d.jpg")],
                                    stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
            while proc.poll() is None:
                ctx.check()
                done = len(os.listdir(tmp))
                ctx.progress(0.1 * done / max(1, n), f"Reading frames {done}/{n}")
                time.sleep(0.2)
            got = len([x for x in os.listdir(tmp) if x.endswith(".jpg")])
            if got == 0:
                raise RuntimeError("Could not read the frames: " + (proc.stderr.read() or b"").decode("utf-8", "replace")[-300:])
            # 2) track: forwards from the first clicked frame, then backwards
            pred = sam["video"]
            with torch.inference_mode(), autocast:
                state = pred.init_state(video_path=tmp, offload_video_to_cpu=True, offload_state_to_cpu=not cuda)
                for f, fp in sorted(by_frame.items()):
                    pred.add_new_points_or_box(state, frame_idx=min(f, got - 1), obj_id=1,
                                               points=np.array([[p["x"] * sx, p["y"] * sy] for p in fp], np.float32),
                                               labels=np.array([int(p.get("label", 1)) for p in fp], np.int32))
                first = min(by_frame)
                k = 0
                for direction in (False, True):
                    for fi, _ids, masks in pred.propagate_in_video(state, start_frame_idx=min(first, got - 1), reverse=direction):
                        ctx.check()
                        low[fi] = masks[0, 0].float().cpu().numpy().astype(np.float16)
                        k += 1
                        ctx.progress(0.1 + 0.7 * min(1.0, k / got), f"Tracking {len(low)}/{got} frames")
                pred.reset_state(state)
            frames_out = n
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 3) full resolution: logits -> alpha, guided upsampling on the full-size frames, written like a matte
    out_kind = "image" if (kind == "image" or preview) else "video"
    dec = decode_proc(ffmpeg, args["src"], kind, w, h, fps, bool(args.get("hdr")), k0 + (first if preview and kind == "video" else 0),
                      frames_out)
    enc = encode_proc(ffmpeg, args["out"], w, h, fps, out_kind)
    coverage = []
    last = None
    try:
        for i in range(frames_out):
            ctx.check()
            fr = read_frames(dec, w, h, 1)
            if not fr:
                break
            key = 0 if out_kind == "image" else i
            lg = low.get(key)
            if lg is None:  # SAM didn't reach this frame (e.g. the file ended early): keep the last mask
                lg = last if last is not None else np.full((sh, sw), -20, np.float16)
            last = lg
            x = torch.from_numpy(fr[0]).to(dev).permute(2, 0, 1)[None].float().div_(255)
            p = torch.sigmoid(torch.from_numpy(lg.astype(np.float32)).to(dev))[None, None]
            a = to_full(x, p, bool(args.get("detail", True)))[0]
            enc.stdin.write(a.tobytes())
            coverage.append(round(float(a.mean()) / 255.0, 4))
            if i % 5 == 0:
                ctx.progress(0.8 + 0.2 * i / max(1, frames_out), "Refining edges")
        while len(coverage) < frames_out and last is not None and out_kind == "video":
            enc.stdin.write(a.tobytes())
            coverage.append(coverage[-1])
    except BaseException:
        enc.kill()
        enc.wait(timeout=30)
        dec.kill()
        try:
            os.unlink(args["out"])
        except OSError:
            pass
        raise
    finally:
        try:
            enc.stdin.close()
        except OSError:
            pass
        enc.wait(timeout=120)
        dec.kill()
        dec.wait(timeout=30)
    if enc.returncode != 0:
        raise RuntimeError("Writing the mask failed: " + (enc.stderr.read() or b"").decode("utf-8", "replace")[-400:])
    return {"frames": len(coverage), "coverage": coverage, "device": dev.type,
            "deviceName": torch.cuda.get_device_name(0) if cuda else "CPU",
            "peakVram": int(torch.cuda.max_memory_allocated()) if cuda else 0,
            "seconds": round(time.time() - t_start, 2), "fps": round(len(coverage) / max(1e-3, time.time() - t_start), 2),
            "warning": None if cuda else "No compatible GPU found: running on the CPU, which is much slower."}
