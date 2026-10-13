"""Person matting (BiRefNet family, MIT) for videos and photos.

Frames are decoded by ffmpeg (same decoder, rotation and HDR tone mapping as the Ligxt export), run
through the model in fp16 batches sized to the VRAM budget, upsampled to the source resolution with a
fast guided filter (edges follow the full-resolution picture: hair, fingers) and written as a lossless
8-bit alpha video (FFV1, gray) or PNG. Frame k of the output is source frame `k0 + k` on the source's
constant frame grid (k / fps seconds from the start of the stream).
"""
from __future__ import annotations

import math
import os
import subprocess
import time

import numpy as np

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv,")
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)

_model = {"key": None, "net": None, "device": None, "dtype": None}


# ---------------------------------------------------------------- frames in / alpha out
def decode_proc(ffmpeg: str, src: str, kind: str, w: int, h: int, fps: float, hdr: bool, k0: int, n: int):
    tm = TONEMAP if hdr else ""
    vf = f"{tm}scale={w}:{h}:flags=bicubic,format=rgb24"
    if kind == "image":
        args = [ffmpeg, "-v", "error", "-nostdin", "-i", src, "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-"]
    else:
        # Accurate seek a quarter frame before frame k0 keeps exactly frames k0, k0+1, ... (re-stamped from
        # 0.25/fps), and fps=...:round=near maps them onto 0, 1, ... of the constant frame grid.
        ss = max(0.0, (k0 - 0.25) / fps)
        args = [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{ss:.6f}", "-i", src, "-an", "-sn",
                "-vf", f"fps={fps:.6f}:round=near,{vf}", "-frames:v", str(n), "-f", "rawvideo", "-"]
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)


def encode_proc(ffmpeg: str, out: str, w: int, h: int, fps: float, kind: str):
    if kind == "image":
        args = [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-i", "-",
                "-frames:v", "1", out]
    else:
        args = [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}",
                "-framerate", f"{fps:.6f}", "-i", "-", "-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "4",
                "-pix_fmt", "gray", out]
    return subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)


def read_frames(proc, w: int, h: int, n: int):
    size = w * h * 3
    out = []
    for _ in range(n):
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        out.append(np.frombuffer(buf, np.uint8).reshape(h, w, 3))
    return out


# ---------------------------------------------------------------- models
class _Cpu:
    type = "cpu"


def load_model(args: dict):
    key = (args["model"], args.get("modelDir"), args.get("device", "auto"))
    if _model["key"] == key:
        return _model["net"], _model["device"], _model["dtype"]
    unload()
    if args["model"] == "test-key":  # tests only: no torch needed
        _model.update(key=key, net="test-key", device=_Cpu(), dtype=None)
        return "test-key", _Cpu(), None
    import torch
    want = args.get("device", "auto")
    use_cuda = want != "cpu" and torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    from transformers import AutoModelForImageSegmentation
    net = AutoModelForImageSegmentation.from_pretrained(args["modelDir"], trust_remote_code=True, local_files_only=True)
    torch.set_float32_matmul_precision("high")
    dtype = torch.float16 if use_cuda else torch.float32
    net = net.to(device=device, dtype=dtype).eval()
    _model.update(key=key, net=net, device=device, dtype=dtype)
    return net, device, dtype


def unload() -> None:
    import sys
    _model.update(key=None, net=None, device=None, dtype=None)
    seg = sys.modules.get("segment")
    if seg is not None:
        seg._sam.update(key=None, video=None, image=None, device=None)
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def _box(x, r: int):
    import torch.nn.functional as F
    return F.avg_pool2d(x, 2 * r + 1, stride=1, padding=r, count_include_pad=False)


def run_batch(net, device, dtype, frames: list, res: int, dynamic: bool, detail: bool):
    """Alpha (uint8, HxW) for each RGB frame."""
    if net == "test-key":
        # Tests only: everything that differs from the top-left pixel's colour is "subject".
        out = []
        for f in frames:
            x = f.astype(np.float32) / 255.0
            d = np.abs(x - x[:1, :1]).max(axis=2)
            out.append((np.clip((d - 0.08) / 0.08, 0, 1) * 255 + 0.5).astype(np.uint8))
        return out
    import torch
    import torch.nn.functional as F
    h, w = frames[0].shape[:2]
    x = torch.from_numpy(np.stack(frames)).to(device).permute(0, 3, 1, 2).float().div_(255.0)  # B,3,H,W
    if dynamic:
        s = res / max(h, w)
        mh, mw = max(32, int(round(h * s / 32)) * 32), max(32, int(round(w * s / 32)) * 32)
    else:
        mh = mw = res
    xi = F.interpolate(x, size=(mh, mw), mode="bilinear", align_corners=False, antialias=True)
    mean = torch.tensor(MEAN, device=device).view(1, 3, 1, 1)
    std = torch.tensor(STD, device=device).view(1, 3, 1, 1)
    xi = ((xi - mean) / std).to(dtype)
    with torch.inference_mode():
        pred = net(xi)[-1].float().sigmoid()  # B,1,mh,mw
    return to_full(x, pred, detail)


def to_full(x, pred, detail: bool) -> list:
    """Low-resolution alpha `pred` (B,1,h,w, 0..1) -> uint8 alpha at the frames' size; x = frames (B,3,H,W, 0..1)."""
    import torch
    import torch.nn.functional as F
    h, w = x.shape[-2:]
    if detail:
        # Fast guided filter (He & Sun 2015): local linear model alpha = a*I + b fitted at a low,
        # aspect-correct resolution and applied to the full-resolution luma.
        lh, lw = (h, w) if max(h, w) <= 1024 else (max(8, round(h * 1024 / max(h, w))), max(8, round(w * 1024 / max(h, w))))
        g_full = (0.299 * x[:, 0:1] + 0.587 * x[:, 1:2] + 0.114 * x[:, 2:3])
        g = F.interpolate(g_full, size=(lh, lw), mode="bilinear", align_corners=False, antialias=True)
        p = F.interpolate(pred, size=(lh, lw), mode="bilinear", align_corners=False)
        r, eps = max(2, round(4 * max(lh, lw) / 1024)), 1e-3
        mI, mp = _box(g, r), _box(p, r)
        cov = _box(g * p, r) - mI * mp
        var = _box(g * g, r) - mI * mI
        A = cov / (var + eps)
        b = mp - A * mI
        A, b = _box(A, r), _box(b, r)
        A = F.interpolate(A, size=(h, w), mode="bilinear", align_corners=False)
        b = F.interpolate(b, size=(h, w), mode="bilinear", align_corners=False)
        out = (A * g_full + b).clamp_(0, 1)
        # keep solid areas solid (the linear model can leak a little into flat regions)
        base = F.interpolate(pred, size=(h, w), mode="bilinear", align_corners=False)
        out = torch.where((base > 0.98) | (base < 0.02), base, out)
    else:
        out = F.interpolate(pred, size=(h, w), mode="bilinear", align_corners=False)
    a8 = (out[:, 0] * 255 + 0.5).clamp_(0, 255).byte().cpu().numpy()
    return [a8[i] for i in range(a8.shape[0])]


# ---------------------------------------------------------------- the op
def matte(ctx, args: dict) -> dict:
    """args: model, modelDir, src, kind (video|image), out, width, height, fps, hdr, k0, n, res, dynamic,
    detail, budgetGB, device (auto|cpu), ffmpeg."""
    t_start = time.time()
    ffmpeg = args.get("ffmpeg", "ffmpeg")
    kind = args.get("kind", "video")
    w, h = int(args["width"]), int(args["height"])
    fps = float(args.get("fps") or 30)
    k0, n = int(args.get("k0", 0)), int(args.get("n", 1)) if kind == "video" else 1
    res, dynamic, detail = int(args.get("res", 1024)), bool(args.get("dynamic")), bool(args.get("detail", True))
    budget = float(args.get("budgetGB", 4.5)) * 1024 ** 3
    ctx.progress(0.0, "Loading the model", force=True)
    t0 = time.time()
    net, device, dtype = load_model(args)
    load_s = time.time() - t0
    warning = None
    if device.type == "cpu" and args.get("model") != "test-key":
        warning = "No compatible GPU found: running on the CPU, which is much slower."
        if args.get("device") == "cpu":
            warning = "Running on the CPU (as set in Settings): this is much slower than the GPU."
    cuda = device.type == "cuda"
    if cuda:
        import torch
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        base_mem = torch.cuda.memory_allocated()
        free, _total = torch.cuda.mem_get_info()
        budget = min(budget, base_mem + free * 0.92)
    dec = decode_proc(ffmpeg, args["src"], kind, w, h, fps, bool(args.get("hdr")), k0, n)
    enc = encode_proc(ffmpeg, args["out"], w, h, fps, kind)
    coverage: list[float] = []
    batch = 1
    done = 0
    peak = 0
    infer_s = 0.0
    try:
        pending: list = []
        while done < n:
            ctx.check()
            need = min(batch, n - done)
            pending += read_frames(dec, w, h, need - len(pending))
            if not pending:
                break
            take = pending[:need]
            t1 = time.time()
            try:
                alphas = run_batch(net, device, dtype, take, res, dynamic, detail)
            except Exception as e:  # noqa: BLE001
                if not (cuda and "OutOfMemory" in type(e).__name__):
                    raise
                torch.cuda.empty_cache()
                if batch > 1:
                    batch = max(1, batch // 2)
                    continue
                raise
            if cuda:
                torch.cuda.synchronize()
            infer_s += time.time() - t1
            pending = pending[len(take):]
            for a in alphas:
                try:
                    enc.stdin.write(a.tobytes())
                except OSError as e:
                    enc.wait(timeout=10)
                    raise RuntimeError("The matte encoder stopped: "
                                       + (enc.stderr.read() or b"").decode("utf-8", "replace")[-600:]) from e
                coverage.append(round(float(a.mean()) / 255.0, 4))
            done += len(alphas)
            if cuda:
                peak = max(peak, torch.cuda.max_memory_allocated())
                if done == len(alphas) and kind == "video" and net != "test-key":
                    # size the batch from the first frame's memory use, under the budget
                    per = max(1, peak - base_mem)
                    batch = int(max(1, min(8, (budget - base_mem) * 0.85 // per)))
            fps_now = done / max(1e-3, infer_s)
            ctx.progress(done / max(1, n), f"{done}/{n} frames · {fps_now:.1f} fps" if kind == "video" else "Analyzing")
        if 0 < done < n and kind == "video":
            # the source ended early (variable frame rate / rounding): repeat the last alpha so the
            # frame count is exact for the export
            for _ in range(n - done):
                enc.stdin.write(alphas[-1].tobytes())
                coverage.append(coverage[-1])
    except BaseException:
        enc.kill()
        enc.wait(timeout=30)
        dec.kill()
        dec.wait(timeout=30)
        try:
            os.unlink(args["out"])  # cancelled or failed: no partial output
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
        raise RuntimeError("Writing the matte failed: " + (enc.stderr.read() or b"").decode("utf-8", "replace")[-400:])
    if done == 0:
        raise RuntimeError("Could not read any frames: " + (dec.stderr.read() or b"").decode("utf-8", "replace")[-400:])
    return {"frames": len(coverage), "decoded": done, "coverage": coverage, "batch": batch, "device": device.type,
            "deviceName": torch.cuda.get_device_name(0) if cuda else "CPU", "peakVram": int(peak),
            "loadSeconds": round(load_s, 2), "inferSeconds": round(infer_s, 2),
            "seconds": round(time.time() - t_start, 2), "fps": round(done / max(1e-3, infer_s), 2), "warning": warning}
