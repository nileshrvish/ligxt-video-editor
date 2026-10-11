"""Keyframes, easing, layer transforms, speed/time mapping and ducking.

Shared contract with frontend/src/motion/motion.ts (checked by shared/motion_cases.json).
Keyframe times are seconds relative to the clip's start on the timeline.
"""
from __future__ import annotations

import math

EASINGS = ("linear", "in", "out", "inout", "punch")
RAMP_SEGMENTS = 16
REVERSE_EPS = 0.001  # reverse starts just before the out point (which is exclusive)
ANIMATED = ("x", "y", "scale", "rotation", "opacity", "punch", "shakeX", "shakeY", "shakeR")
# punch multiplies the scale and shakeX/Y/R add offsets, so beat punches and camera shake layer on
# top of the clip's own (e.g. Ken Burns) movement without touching its keyframes.


def ease(name: str, u: float) -> float:
    u = min(1.0, max(0.0, u))
    if name == "in":
        return u ** 3
    if name == "out":
        return 1 - (1 - u) ** 3
    if name == "inout":
        return 4 * u ** 3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2
    if name == "punch":  # snappy, overshoots ~10 % then settles (ease-out-back)
        return 1 + 3.2 * (u - 1) ** 3 + 2.2 * (u - 1) ** 2
    return u


def sorted_kfs(kfs) -> list[dict]:
    out = [k if isinstance(k, dict) else k.model_dump() for k in (kfs or [])]
    return sorted(out, key=lambda k: k["time"])


def value_at(kfs, t: float, default: float) -> float:
    ks = sorted_kfs(kfs)
    if not ks:
        return default
    if t <= ks[0]["time"]:
        return float(ks[0]["value"])
    if t >= ks[-1]["time"]:
        return float(ks[-1]["value"])
    for a, b in zip(ks, ks[1:]):
        if a["time"] <= t < b["time"]:
            span = b["time"] - a["time"]
            u = (t - a["time"]) / span if span > 1e-9 else 1.0
            return float(a["value"]) + (float(b["value"]) - float(a["value"])) * ease(a.get("easing", "linear"), u)
    return float(ks[-1]["value"])


def _tr(c: dict) -> dict:
    return c.get("transform") or {}


def transform_at(c: dict, tau: float) -> dict:
    tr = _tr(c)
    kf = c.get("keyframes") or {}
    return {
        "x": value_at(kf.get("x"), tau, float(tr.get("x", 0))) + value_at(kf.get("shakeX"), tau, 0.0),
        "y": value_at(kf.get("y"), tau, float(tr.get("y", 0))) + value_at(kf.get("shakeY"), tau, 0.0),
        "scale": value_at(kf.get("scale"), tau, float(tr.get("scale", 1))) * value_at(kf.get("punch"), tau, 1.0),
        "rotation": value_at(kf.get("rotation"), tau, float(tr.get("rotation", 0))) + value_at(kf.get("shakeR"), tau, 0.0),
        "opacity": value_at(kf.get("opacity"), tau, float(tr.get("opacity", 1))),
        "flipH": bool(tr.get("flipH")), "flipV": bool(tr.get("flipV")),
    }


def has_motion(c: dict) -> bool:
    """True if the clip needs the general transform path (anything but a plain fitted picture)."""
    tr = _tr(c)
    kf = c.get("keyframes") or {}
    crop = tr.get("crop") or {}
    return (any(kf.get(p) for p in ANIMATED)
            or abs(float(tr.get("x", 0))) > 1e-6 or abs(float(tr.get("y", 0))) > 1e-6
            or abs(float(tr.get("scale", 1)) - 1) > 1e-6 or abs(float(tr.get("rotation", 0))) > 1e-6
            or float(tr.get("opacity", 1)) < 1 - 1e-6 or bool(tr.get("flipH")) or bool(tr.get("flipV"))
            or any(float(crop.get(k, 0)) > 1e-6 for k in ("left", "top", "right", "bottom")))


def source_rect(src_w: float, src_h: float, crop: dict, fill: bool, W: float, H: float) -> tuple[float, float, float, float]:
    """The part of the source that is shown: user crop, then (fill) a centre crop to the canvas aspect."""
    x0 = float(crop.get("left", 0)) * src_w
    y0 = float(crop.get("top", 0)) * src_h
    cw = max(1.0, src_w * (1 - float(crop.get("left", 0)) - float(crop.get("right", 0))))
    ch = max(1.0, src_h * (1 - float(crop.get("top", 0)) - float(crop.get("bottom", 0))))
    if fill:
        if cw / ch > W / H:
            nw = ch * W / H
            x0 += (cw - nw) / 2
            cw = nw
        else:
            nh = cw * H / W
            y0 += (ch - nh) / 2
            ch = nh
    return x0, y0, cw, ch


def layer_matrix(src_w: float, src_h: float, crop: dict, fill: bool, W: float, H: float, t: dict):
    """Affine matrix (a, b, c, d, e, f) mapping source pixel coords to canvas coords
    (canvas = [a c e; b d f] . [x y 1], the CanvasRenderingContext2D.setTransform order), plus the
    shown source rect (x0, y0, cw, ch)."""
    x0, y0, cw, ch = source_rect(src_w, src_h, crop, fill, W, H)
    f = min(W / cw, H / ch)
    s = t["scale"] * f
    fx = -1.0 if t["flipH"] else 1.0
    fy = -1.0 if t["flipV"] else 1.0
    th = math.radians(t["rotation"])
    co, si = math.cos(th), math.sin(th)
    a, b = s * fx * co, s * fx * si
    c, d = -s * fy * si, s * fy * co
    cx, cy = x0 + cw / 2, y0 + ch / 2
    Cx, Cy = W / 2 + t["x"], H / 2 + t["y"]
    return (a, b, c, d, Cx - (a * cx + c * cy), Cy - (b * cx + d * cy)), (x0, y0, cw, ch)


def invert(m):
    a, b, c, d, e, f = m
    det = a * d - b * c
    return (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)


def apply(m, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


# ---------------------------------------------------------------- speed
def ramp_points(c: dict) -> list[dict] | None:
    r = c.get("speedRamp")
    pts = (r or {}).get("points") if isinstance(r, dict) else None
    if not pts or len(pts) < 2:
        return None
    pts = sorted(({"u": min(1.0, max(0.0, float(p["u"]))), "speed": min(4.0, max(0.1, float(p["speed"])))} for p in pts),
                 key=lambda p: p["u"])
    if pts[0]["u"] > 0:
        pts.insert(0, {"u": 0.0, "speed": pts[0]["speed"]})
    if pts[-1]["u"] < 1:
        pts.append({"u": 1.0, "speed": pts[-1]["speed"]})
    return pts


def _speed_integral(pts: list[dict], a: float, b: float) -> float:
    """Integral of the piecewise-linear speed curve over u in [a, b]."""
    total = 0.0
    for p, q in zip(pts, pts[1:]):
        lo, hi = max(a, p["u"]), min(b, q["u"])
        if hi <= lo:
            continue
        span = q["u"] - p["u"]
        sp = lambda u: p["speed"] + (q["speed"] - p["speed"]) * ((u - p["u"]) / span if span > 1e-12 else 0)  # noqa: E731
        total += (sp(lo) + sp(hi)) / 2 * (hi - lo)
    return total


def clip_duration(c: dict) -> float:
    src = max(0.0, float(c.get("outPoint", 0)) - float(c.get("inPoint", 0)))
    pts = ramp_points(c)
    if pts and c.get("freezeFrame") is None:
        return src / max(1e-6, _speed_integral(pts, 0, 1))
    return src / (float(c.get("speed") or 1))


def ramp_segments(c: dict) -> list[dict]:
    """Constant-speed pieces approximating the ramp: [{t0, t1, src0, speed}] (clip-relative times)."""
    pts = ramp_points(c)
    D = clip_duration(c)
    if not pts:
        return [{"t0": 0.0, "t1": D, "src0": float(c.get("inPoint", 0)), "speed": float(c.get("speed") or 1)}]
    out, src = [], float(c.get("inPoint", 0))
    n = RAMP_SEGMENTS
    for k in range(n):
        u0, u1 = k / n, (k + 1) / n
        sp = _speed_integral(pts, u0, u1) / (u1 - u0)
        out.append({"t0": D * u0, "t1": D * u1, "src0": src, "speed": sp})
        src += sp * D * (u1 - u0)
    return out


def src_time(c: dict, tau: float) -> float:
    """Source time shown at clip-relative timeline time tau (speed, ramps, reverse, freeze)."""
    if c.get("freezeFrame") is not None:
        return float(c["freezeFrame"])
    segs = ramp_segments(c)
    seg = segs[-1]
    for s in segs:
        if tau < s["t1"]:
            seg = s
            break
    src = seg["src0"] + (tau - seg["t0"]) * seg["speed"]
    if c.get("reverse"):
        # Mirror, starting on the last frame before the (exclusive) out point.
        src = float(c.get("inPoint", 0)) + float(c.get("outPoint", 0)) - src - REVERSE_EPS
    return src


# ---------------------------------------------------------------- ducking
def duck_regions(sources: list[tuple[float, float, list[float], float]], threshold: float = 0.08,
                 merge_gap: float = 0.35, min_len: float = 0.15) -> list[tuple[float, float]]:
    """`sources`: (timeline_start, timeline_end, peaks 0..1 at 100/s sampled on the timeline, _). Returns
    merged speech regions."""
    marks = []
    for start, _end, peaks, _ in sources:
        on = None
        for i, v in enumerate(peaks):
            t = start + i / 100.0
            if v > threshold and on is None:
                on = t
            elif v <= threshold and on is not None:
                marks.append((on, t))
                on = None
        if on is not None:
            marks.append((on, start + len(peaks) / 100.0))
    marks.sort()
    merged: list[list[float]] = []
    for a, b in marks:
        if merged and a - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_len]


def duck_gain(regions: list[tuple[float, float]], t: float, amount_db: float = -12.0,
              attack: float = 0.15, release: float = 0.45) -> float:
    level = 10 ** (amount_db / 20)
    g = 1.0
    for a, b in regions:
        if a <= t <= b:
            w = 1.0
        elif a - attack < t < a:
            w = (t - (a - attack)) / attack
        elif b < t < b + release:
            w = 1 - (t - b) / release
        else:
            w = 0.0
        g = min(g, 1 - (1 - level) * w)
    return g
