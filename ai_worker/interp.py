"""Retimed clip frames: frame blending (numpy, no GPU) or RIFE v4.26 interpolation (PyTorch).

The backend sends the exact list of output frames: each is (i, t) = "source frame i, fraction t of the way
to frame i+1" on the source's constant frame grid (the same grid the export decodes). t == 0 copies frame i.
Pairs that span a scene cut are never interpolated (the nearest frame is used). Only the source frames
those output frames need are decoded. Output: a CFR video at the timeline's frame rate.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

import numpy as np

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv,")
_rife = {"key": None, "net": None, "device": None, "dtype": None}


# ---------------------------------------------------------------- frames
def decode_range(ffmpeg: str, src: str, w: int, h: int, fps: float, hdr: bool, i0: int, i1: int, path: str, check=None):
    """Source frames i0..i1 (inclusive) at w x h, RGB, into a memory-mapped file (random access, any size)."""
    n = i1 - i0 + 1
    mm = np.lib.format.open_memmap(path, mode="w+", dtype=np.uint8, shape=(n, h, w, 3))
    tm = TONEMAP if hdr else ""
    ss = max(0.0, (i0 - 0.25) / fps)
    p = subprocess.Popen([ffmpeg, "-v", "error", "-nostdin", "-ss", f"{ss:.6f}", "-i", src, "-an", "-sn",
                          "-vf", f"fps={fps:.6f}:round=near,{tm}scale={w}:{h}:flags=bicubic,format=rgb24",
                          "-frames:v", str(n), "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=CREATE_NO_WINDOW)
    size = w * h * 3
    got = 0
    try:
        while got < n:
            if check:
                check()
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            mm[got] = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            got += 1
    finally:
        p.kill()
        p.wait(timeout=30)
    for k in range(got, n):  # the file ended early: hold the last frame
        mm[k] = mm[got - 1] if got else 0
    mm.flush()
    return mm, got


def encode_proc(ffmpeg: str, out: str, w: int, h: int, fps: float, quality: str):
    q = (["-c:v", "libx264", "-preset", "medium", "-crf", "14", "-g", str(max(1, int(round(fps))))] if quality == "full" else
         ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", "15", "-bf", "0"])
    return subprocess.Popen([ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                             "-framerate", f"{fps:.6f}", "-i", "-", *q, "-pix_fmt", "yuv420p",
                             "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                             "-movflags", "+faststart", out],
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)


def small(f: np.ndarray) -> np.ndarray:
    h, w = f.shape[:2]
    sy, sx = max(1, h // 54), max(1, w // 96)
    return f[: (h // sy) * sy, : (w // sx) * sx].reshape(h // sy, sy, w // sx, sx, 3).mean(axis=(1, 3)) / 255.0


def is_cut(a: np.ndarray, b: np.ndarray) -> tuple[bool, float]:
    """Scene cut between two frames: large picture change and a different colour histogram."""
    sa, sb = small(a), small(b)
    d = float(np.abs(sa - sb).mean())
    ha = np.concatenate([np.histogram(sa[..., c], 16, (0, 1))[0] for c in range(3)]).astype(np.float64)
    hb = np.concatenate([np.histogram(sb[..., c], 16, (0, 1))[0] for c in range(3)]).astype(np.float64)
    inter = float(np.minimum(ha / ha.sum(), hb / hb.sum()).sum())
    return (d > 0.16 and inter < 0.65) or inter < 0.4, d


# ---------------------------------------------------------------- RIFE
def load_rife(args: dict):
    import torch
    from rife_net import IFNet
    key = (args["weights"], args.get("device", "auto"))
    if _rife["key"] == key:
        return _rife["net"], _rife["device"], _rife["dtype"]
    from matting import unload
    unload()  # one model on the GPU at a time
    use_cuda = args.get("device", "auto") != "cpu" and torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")
    net = IFNet()
    sd = torch.load(args["weights"], map_location="cpu", weights_only=True)
    sd = {k.replace("module.", ""): v for k, v in sd.items()}
    missing, unexpected = net.load_state_dict(sd, strict=False)
    if missing:
        raise RuntimeError(f"RIFE weights don't match the network ({len(missing)} missing keys).")
    dtype = torch.float16 if use_cuda else torch.float32
    torch.backends.cudnn.benchmark = True  # fixed frame sizes: let cuDNN pick the fastest kernels
    net = net.to(device=device, dtype=dtype).eval()
    _rife.update(key=key, net=net, device=device, dtype=dtype)
    return net, device, dtype


def unload_rife() -> None:
    _rife.update(key=None, net=None, device=None, dtype=None)


# ---------------------------------------------------------------- the op
def interp(ctx, args: dict) -> dict:
    """args: src, width/height (source), fps (source), hdr, frames [[i, t], ...], out, outWidth, outHeight,
    outFps, quality (full|preview), mode (ai|blend|ai-test), weights, scale (RIFE, 0 = auto), budgetGB,
    device, ffmpeg, staticLock (bool)."""
    t_start = time.time()
    ffmpeg = args.get("ffmpeg", "ffmpeg")
    frames = [(int(i), float(t)) for i, t in args["frames"]]
    w, h = int(args["outWidth"]), int(args["outHeight"])
    mode = args.get("mode", "ai")
    fps = float(args["fps"])
    i0 = min(i for i, _ in frames)
    i1 = max(i + (1 if t > 0 else 0) for i, t in frames)
    tmpdir = tempfile.mkdtemp(prefix="ligxt_interp_")
    ctx.progress(0.0, "Reading frames", force=True)
    stats = {"interpolated": 0, "copied": 0, "cutPairs": [], "motion": [], "peakVram": 0, "device": "cpu"}
    tm_ = {"decode": 0.0, "infer": 0.0, "write": 0.0}
    t_dec = time.time()
    try:
        src, got = decode_range(ffmpeg, args["src"], w, h, fps, bool(args.get("hdr")), i0, i1, os.path.join(tmpdir, "f.npy"), ctx.check)
        if got == 0:
            raise RuntimeError("Could not read the clip's frames.")
        n_src = i1 - i0 + 1
        tm_["decode"] = time.time() - t_dec

        def fr(i):
            return src[min(max(i - i0, 0), n_src - 1)]

        # scene cuts between the pairs that would be interpolated
        cuts = set()
        for i in sorted({i for i, t in frames if t > 0}):
            c, _ = is_cut(fr(i), fr(i + 1))
            if c:
                cuts.add(i)
        stats["cutPairs"] = sorted(i for i in cuts)
        ctx.progress(0.1, "Interpolating")
        net = None
        if mode == "ai":
            import torch
            import torch.nn.functional as F
            net, device, dtype = load_rife(args)
            stats["device"] = device.type
            cuda = device.type == "cuda"
            if cuda:
                torch.cuda.reset_peak_memory_stats()
            scale = float(args.get("scale") or 0) or (0.5 if min(w, h) > 1440 else 1.0)
            mult = max(128, int(128 / scale))
            ph, pw = (h + mult - 1) // mult * mult, (w + mult - 1) // mult * mult
            budget = float(args.get("budgetGB", 4.5)) * 1024 ** 3
            batch = 1
        enc = encode_proc(ffmpeg, args["out"], w, h, float(args["outFps"]), args.get("quality", "full"))
        try:
            k = 0
            n = len(frames)
            while k < n:
                ctx.check()
                i, t = frames[k]
                if t <= 0 or i in cuts or mode not in ("ai", "blend", "ai-test"):
                    # an exact source frame (or a cut: the nearest frame, never a mix of two shots)
                    j = i + (1 if (i in cuts and t >= 0.5) else 0)
                    enc.stdin.write(np.ascontiguousarray(fr(j)).tobytes())
                    stats["copied"] += 1
                    k += 1
                elif mode in ("blend", "ai-test"):
                    a, b = fr(i).astype(np.float32), fr(i + 1).astype(np.float32)
                    enc.stdin.write((a * (1 - t) + b * t + 0.5).astype(np.uint8).tobytes())
                    stats["interpolated"] += 1
                    k += 1
                else:
                    # a batch of consecutive interpolated frames
                    group = []
                    while k < n and len(group) < batch:
                        i2, t2 = frames[k]
                        if t2 <= 0 or i2 in cuts:
                            break
                        group.append((i2, t2))
                        k += 1
                    t_inf = time.time()
                    with torch.inference_mode():
                        x0 = torch.from_numpy(np.stack([fr(a) for a, _ in group])).to(device).permute(0, 3, 1, 2).to(dtype).div_(255)
                        x1 = torch.from_numpy(np.stack([fr(a + 1) for a, _ in group])).to(device).permute(0, 3, 1, 2).to(dtype).div_(255)
                        pad = (0, pw - w, 0, ph - h)
                        p0, p1 = F.pad(x0, pad), F.pad(x1, pad)
                        ts = torch.tensor([b for _, b in group], device=device, dtype=dtype).view(-1, 1, 1, 1)
                        try:
                            out, flow = net(p0, p1, ts, scale=scale)
                        except torch.cuda.OutOfMemoryError:
                            torch.cuda.empty_cache()
                            if batch > 1:
                                batch = max(1, batch // 2)
                                k -= len(group)
                                continue
                            raise
                        out = out[:, :, :h, :w].float()
                        if args.get("staticLock", True):
                            # Text, graphics and other still areas: where both frames agree, use them as they are
                            # (interpolation can drag still edges next to moving ones).
                            lin = x0.float() * (1 - ts.float()) + x1.float() * ts.float()
                            diff = (x0.float() - x1.float()).abs().amax(1, keepdim=True)
                            diff = F.avg_pool2d(diff, 9, stride=1, padding=4)
                            still = (1 - ((diff - 0.02) / 0.04).clamp(0, 1))
                            out = out * (1 - still) + lin * still
                        o8 = (out.clamp(0, 1) * 255 + 0.5).byte().permute(0, 2, 3, 1).contiguous().cpu().numpy()
                        # motion of each pair (p95 flow, % of the frame's long side), from the flow towards frame i
                        for g, (_, tt) in enumerate(group):
                            if 0.2 <= tt <= 0.8:
                                f0 = flow[g, :2, :h, :w].float()
                                mag = (f0[0] ** 2 + f0[1] ** 2).sqrt().flatten() / tt
                                q = torch.quantile(mag[:: max(1, mag.numel() // 200000)], 0.95).item()
                                stats["motion"].append(round(100 * q / max(w, h), 2))
                    t_w = time.time()
                    tm_["infer"] += t_w - t_inf
                    for g in range(len(group)):
                        enc.stdin.write(o8[g].tobytes())
                    tm_["write"] += time.time() - t_w
                    stats["interpolated"] += len(group)
                    if cuda:
                        stats["peakVram"] = max(stats["peakVram"], torch.cuda.max_memory_allocated())
                        if batch == 1 and len(group) == 1 and stats["interpolated"] == 1:
                            base = torch.cuda.memory_allocated()
                            per = max(1, stats["peakVram"] - base)
                            free, _ = torch.cuda.mem_get_info()
                            cap = min(budget, base + free * 0.9)
                            batch = int(max(1, min(2, (cap - base) * 0.8 // per)))  # bigger batches are no faster on this GPU
                el = max(1e-3, time.time() - t_start)
                ctx.progress(0.1 + 0.9 * k / n, f"{k}/{n} frames · {stats['interpolated'] / el:.1f} fps")
        finally:
            try:
                enc.stdin.close()
            except OSError:
                pass
            enc.wait(timeout=300)
        if enc.returncode != 0:
            raise RuntimeError("Writing the slow-motion frames failed: " + (enc.stderr.read() or b"").decode("utf-8", "replace")[-400:])
        del src
    except BaseException:
        try:
            os.unlink(args["out"])
        except OSError:
            pass
        raise
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    el = time.time() - t_start
    stats["timing"] = {k: round(v, 2) for k, v in tm_.items()}
    stats.update(frames=len(frames), seconds=round(el, 2), fps=round(stats["interpolated"] / max(1e-3, el), 2),
                 scale=locals().get("scale"), batch=locals().get("batch"))
    if mode == "ai" and stats["device"] == "cpu":
        stats["warning"] = "No compatible GPU found: AI smooth ran on the CPU, which is much slower."
    return stats
