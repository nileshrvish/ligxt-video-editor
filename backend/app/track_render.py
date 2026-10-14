"""Export side of motion tracking: resolving every follow/reframe/region in a project (follow.py) and
rendering tracked regions (blur / pixelate / grade inside a moving, feathered ellipse or box).

The region is a mask video written per output frame by a plan task (exact positions, no expressions),
alpha-merged with the effected canvas and laid over it. The preview (player/engine.ts) draws the same
region from the same follow.ts path.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from . import follow as FO
from . import layers as LY
from . import slowmo as SM
from . import text as TX
from .config import FFMPEG

BLUR_MAX = 40.0  # gaussian sigma at amount 100 (reference px, 1080 short side)
PIXEL_MIN, PIXEL_MAX = 4.0, 60.0  # pixelate block size at amount 0 / 100 (reference px)


def region_params(reg: dict) -> dict:
    return {"effect": reg.get("effect", "blur"), "amount": max(0.0, min(100.0, float(reg.get("amount", 70)))),
            "shape": reg.get("shape", "ellipse"), "feather": max(0.0, min(1.0, float(reg.get("feather", 0.2)))),
            "padding": max(0.0, float(reg.get("padding", 0.25)))}


def blur_sigma(amount: float, rs: float) -> float:
    return amount / 100 * BLUR_MAX * rs


def pixel_block(amount: float, rs: float) -> int:
    return max(2, int(round((PIXEL_MIN + (PIXEL_MAX - PIXEL_MIN) * amount / 100) * rs)))


def item_size_fn(metas: dict, W: int, H: int):
    def item_size(cd: dict):
        if cd.get("text"):
            lay = TX.build(cd["text"])
            return float(lay["w"]), float(lay["h"])
        m = metas.get(cd.get("mediaId") or "") or {}
        if not m:
            return None
        sw, sh = float(m.get("width") or W), float(m.get("height") or H)
        fill = (cd.get("fit") or "fit") == "fill"
        x0, y0, cw, ch = LY.rounded_rect(sw, sh, ((cd.get("transform") or {}).get("crop") or {}), fill, W, H)
        f = min(W / cw, H / ch)
        return cw * f, ch * f
    return item_size


def resolve_tracking(project, metas: dict, fps: float, W: int, H: int) -> dict:
    pd = project.model_dump(mode="json")
    if not pd.get("motionTracks"):
        return {}
    return FO.resolve(pd, metas, fps, item_size_fn(metas, W, H), lambda cd, m: SM.needs_retime(cd, m, fps))


def mask_frame(W: int, H: int, x: float, y: float, w: float, h: float, shape: str, feather: float) -> np.ndarray:
    """One region mask (uint8 0..255). Feather = the soft edge as a fraction of the region's half size;
    drawn analytically (smoothstep on the normalised distance), so the preview can match it."""
    x0, x1 = max(0, int(x - w / 2 - 2)), min(W, int(x + w / 2 + 3))
    y0, y1 = max(0, int(y - h / 2 - 2)), min(H, int(y + h / 2 + 3))
    out = np.zeros((H, W), np.uint8)
    if x1 <= x0 or y1 <= y0:
        return out
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    nx = np.abs(xx + 0.5 - x) / max(1.0, w / 2)
    ny = np.abs(yy + 0.5 - y) / max(1.0, h / 2)
    d = np.sqrt(nx * nx + ny * ny) if shape == "ellipse" else np.maximum(nx, ny)
    f = max(1e-3, feather)
    u = np.clip((1 - d) / f, 0, 1)
    out[y0:y1, x0:x1] = (u * u * (3 - 2 * u) * 255 + 0.5).astype(np.uint8)
    return out


def write_mask(path: Path, rg: dict, reg: dict, W: int, H: int, fps: float, tau0: float, nf: int) -> None:
    p = region_params(reg)
    proc = subprocess.Popen([FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}", "-r", f"{fps:.6f}",
                             "-i", "-", "-c:v", "ffv1", "-pix_fmt", "gray", str(path)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for j in range(nf):
            x, y, w, h = FO.region_at(rg, tau0 + j / fps)
            proc.stdin.write(mask_frame(W, H, x, y, w, h, p["shape"], p["feather"]).tobytes())
    finally:
        proc.stdin.close()
        err = proc.stderr.read()
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("Writing a tracked region mask failed: " + err.decode("utf-8", "replace")[-300:])


def region_layer(g, v_base: str, c, rg: dict, a_t: float, nf: int, f0: int, fps: float, W: int, H: int, rs: float,
                 plan_tasks: list, grade, n: int) -> str:
    """The canvas so far with the clip's region effect applied inside its tracked mask."""
    reg = dict(getattr(c, "region", None) or {})
    p = region_params(reg)
    name = f"region{n}"
    tau0 = a_t - c.start
    plan_tasks.append((f"Tracked {p['effect']}", lambda wd, rg=rg, reg=reg, tau0=tau0, name=name:
                       write_mask(Path(wd) / f"{name}.mkv", rg, reg, W, H, fps, tau0, nf)))
    keep, src, eff, out = g.label("rk"), g.label("rs"), g.label("re"), g.label("vb")
    off = f0 / fps
    win = f"between(t,{LY._f(off - 0.001)},{LY._f(off + nf / fps - 0.001)})"
    # The effect runs on the whole stream (trimming one side of a split would make ffmpeg queue every
    # earlier frame of the other side); the mask is black before the region starts.
    g.add(f"[{v_base}]format=yuv420p,split[{keep}][{src}]")
    if p["effect"] == "pixelate":
        b = pixel_block(p["amount"], rs)
        g.add(f"[{src}]scale={max(1, round(W / b))}:{max(1, round(H / b))}:flags=area,scale={W}:{H}:flags=neighbor,format=yuv420p[{eff}]")
    elif p["effect"] == "grade":
        graded = grade(src)
        g.add(f"[{graded}]format=yuv420p[{eff}]")
    else:
        sg = blur_sigma(p["amount"], rs)
        g.add(f"[{src}]gblur=sigma={LY._f(max(0.3, sg))}:steps=4:enable='{win}',format=yuv420p[{eff}]")
    idx = g.add_input(["-i", f"{name}.mkv"])
    mk, rgba = g.label("rm"), g.label("ra")
    g.add(f"[{idx}:v]format=gray,setpts=PTS-STARTPTS,tpad=start={f0}:color=black,setpts=N/({LY._f(fps)}*TB)[{mk}]")
    g.add(f"[{eff}][{mk}]alphamerge,format=yuva420p[{rgba}]")
    g.add(f"[{keep}][{rgba}]overlay=0:0:enable='{win}':eof_action=pass:format=auto[{out}]")
    return out
