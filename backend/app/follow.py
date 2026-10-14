"""Motion-tracking maths: track sampling, smoothing, things that follow a track, tracked regions and
auto-reframe. Shared contract with frontend/src/tracking/follow.ts (checked by shared/follow_cases.json).

A track (project.motionTracks[]) belongs to a media file and lives in *source frames*, so it survives
trimming, splitting, speed changes, slow motion and cutout: any clip showing that media maps its
timeline time to a source time (motion.src_time) and looks the track up there.

    {"id", "mediaId", "name", "kind": "face"|"object", "method": "face"|"box"|"sam",
     "fps": source fps, "k0": first source frame, "data": [[cx, cy, w, h, angle, conf], ...],
     "keys": [{"k": frame, "box": [x, y, w, h]}]}

cx, cy, w, h are fractions of the source frame (after rotation); angle in degrees; conf 0..1, where
0 = the target was lost on that frame (the values hold its last known position).

Followers (text, stickers, overlay clips) have `follow`: {"trackId", "clipId" (the clip showing the
tracked media; any clip of that media covering the time is used when it is gone, e.g. after a split),
"position", "scale", "rotation" (bools), "smoothing" 0..1, "safe" (keep inside the Reels safe area)}.
Their transform x/y is the offset from the target (rotated/scaled with it when those are followed).

The result for a clip is `autoMotion` {"fps", "x", "y", "s", "r"} sampled at tau = i / fps
(clip-relative): x/y are added to the clip's x/y, s multiplies its scale, r is added to its rotation.
Regions (blur / pixelate that follows a face or object) get `autoRegion` {"fps", "x", "y", "w", "h"}
(canvas px, centre and size). Auto-reframe puts an x (and y) pan on the clip itself and draws the whole
source frame (`wholeSource`), not just the fill crop.
"""
from __future__ import annotations

import math

from . import motion as M

LOST = 0.0
LOW_CONF = 0.5
HELD_WEIGHT = 0.02  # how much a lost (held) frame pulls the smoothed path
# The Reels safe area (frontend/src/player/geometry.ts REELS_SAFE), as fractions of the canvas.
SAFE = {"top": 220 / 1920, "bottom": 420 / 1920, "right": 140 / 1080, "rightFrom": 0.42, "left": 0.04}
THIRDS = {"left": 1 / 3, "center": 0.5, "right": 2 / 3}


# ---------------------------------------------------------------- tracks
def track_at(track: dict, s: float, interp: bool = True) -> dict | None:
    """The track at source time s: {x, y, w, h, a, c} (fractions of the source, degrees, confidence).
    Outside the tracked range the nearest end holds (with its confidence halved). interp=False takes
    the frame on screen (the last one at or before s), like the plain frame-repeat picture."""
    data = track.get("data") or []
    n = len(data)
    if not n:
        return None
    f = s * float(track["fps"]) - int(track.get("k0", 0))
    if not interp:
        f = math.floor(f + 1e-6)
    if f <= 0 or f >= n - 1:
        e = data[0] if f <= 0 else data[-1]
        out = {"x": e[0], "y": e[1], "w": e[2], "h": e[3], "a": e[4], "c": e[5]}
        if f < -0.5 or f > n - 0.5:
            out["c"] = e[5] * 0.5
        return out
    i = int(math.floor(f))
    u = f - i
    a, b = data[i], data[i + 1]
    da = (b[4] - a[4] + 180) % 360 - 180
    return {"x": a[0] + (b[0] - a[0]) * u, "y": a[1] + (b[1] - a[1]) * u, "w": a[2] + (b[2] - a[2]) * u,
            "h": a[3] + (b[3] - a[3]) * u, "a": a[4] + da * u, "c": min(a[5], b[5])}


def weight(conf: float) -> float:
    return HELD_WEIGHT if conf <= LOST else max(HELD_WEIGHT, min(1.0, conf))


# ---------------------------------------------------------------- smoothing
def lam(amount: float, fps: float) -> float:
    """Smoothing strength for the Whittaker smoother: amount 0..1 -> the shortest movement kept, from
    0.05 s (0) to about 2 s (1)."""
    a = min(1.0, max(0.0, float(amount)))
    period = 0.05 + 2.0 * a * a
    return (period * fps / (2 * math.pi)) ** 4


def smooth(y: list[float], w: list[float], lmb: float) -> list[float]:
    """Whittaker smoother: minimise sum w (z - y)^2 + lmb sum (second difference of z)^2. Zero lag (it
    looks both ways), low-weight samples are bridged smoothly. Banded LDL^T solve (5 diagonals)."""
    n = len(y)
    if n < 3 or lmb <= 1e-12:
        return list(y)
    A0 = [float(w[i]) for i in range(n)]
    A1 = [0.0] * n
    A2 = [0.0] * n
    for r in range(n - 2):  # D^T D accumulated row by row of D = [1, -2, 1]
        A0[r] += lmb
        A0[r + 1] += 4 * lmb
        A0[r + 2] += lmb
        A1[r] += -2 * lmb
        A1[r + 1] += -2 * lmb
        A2[r] += lmb
    d = [0.0] * n
    l1 = [0.0] * n
    l2 = [0.0] * n
    for i in range(n):
        if i >= 2:
            l2[i] = A2[i - 2] / d[i - 2]
        if i >= 1:
            l1[i] = (A1[i - 1] - (l2[i] * d[i - 2] * l1[i - 1] if i >= 2 else 0.0)) / d[i - 1]
        d[i] = A0[i] - (l1[i] ** 2 * d[i - 1] if i >= 1 else 0.0) - (l2[i] ** 2 * d[i - 2] if i >= 2 else 0.0)
    u = [0.0] * n
    for i in range(n):
        u[i] = w[i] * y[i] - (l1[i] * u[i - 1] if i >= 1 else 0.0) - (l2[i] * u[i - 2] if i >= 2 else 0.0)
    z = [0.0] * n
    for i in range(n - 1, -1, -1):
        z[i] = u[i] / d[i] - (l1[i + 1] * z[i + 1] if i + 1 < n else 0.0) - (l2[i + 2] * z[i + 2] if i + 2 < n else 0.0)
    return z


def unwrap(a: list[float]) -> list[float]:
    out = list(a[:1])
    for v in a[1:]:
        out.append(out[-1] + (v - out[-1] + 180) % 360 - 180)
    return out


# ---------------------------------------------------------------- geometry
def host_matrix(host: dict, meta: dict, is_main: bool, default_fit: str, W: float, H: float, tau: float):
    """Source px -> canvas px for a clip showing the tracked media (its own transform and autoMotion)."""
    sw, sh = float(meta.get("width") or W), float(meta.get("height") or H)
    if meta.get("rotation") in (90, -90, 270, -270):
        sw, sh = sh, sw
    fill = (host.get("fit") or (default_fit if is_main else "fit")) == "fill"
    crop = (host.get("transform") or {}).get("crop") or {}
    x0, y0, cw, ch = M.source_rect(sw, sh, crop, fill, W, H)
    rect = (math.floor(x0 + 0.5), math.floor(y0 + 0.5), max(2, math.floor(cw + 0.5)), max(2, math.floor(ch + 0.5)))
    return rect_matrix(rect, W, H, M.transform_at(host, tau)), sw, sh


def rect_matrix(rect, W: float, H: float, t: dict):
    x0, y0, cw, ch = rect
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
    return (a, b, c, d, Cx - (a * cx + c * cy), Cy - (b * cx + d * cy))


def n_samples(D: float, fps: float) -> int:
    return max(2, int(math.ceil(D * fps - 1e-9)) + 1)


def hosts_for(ctx: dict, track: dict, follow: dict, t: float):
    """The clip showing the tracked media at timeline time t: the named clip if it covers t, else any
    visual clip of that media covering t, else the named clip (or the nearest such clip)."""
    named = ctx["clips"].get(follow.get("clipId") or "")
    cands = [h for h in ctx["visual"] if h[0].get("mediaId") == track.get("mediaId")]
    if named and named[0].get("mediaId") == track.get("mediaId"):
        c = named[0]
        if c["start"] - 1e-9 <= t < c["start"] + M.clip_duration(c):
            return named
    for h in cands:
        c = h[0]
        if c["start"] - 1e-9 <= t < c["start"] + M.clip_duration(c):
            return h
    if named and named[0].get("mediaId") == track.get("mediaId"):
        return named
    if not cands:
        return None
    return min(cands, key=lambda h: min(abs(t - h[0]["start"]), abs(t - h[0]["start"] - M.clip_duration(h[0]))))


def target_series(ctx: dict, clip: dict, track: dict, follow: dict, fps: float):
    """Per sample (tau = i / fps over the clip): the target on the canvas: x, y, size (sqrt(w h)),
    bw, bh (axis box), angle, weight. None if no clip shows the tracked media."""
    D = M.clip_duration(clip)
    n = n_samples(D, fps)
    xs, ys, ss, bws, bhs, angs, ws = [], [], [], [], [], [], []
    for i in range(n):
        tau = min(i / fps, max(0.0, D - 1e-6))
        t = clip["start"] + tau
        h = hosts_for(ctx, track, follow, t)
        if h is None:
            return None
        host, is_main = h
        meta = ctx["metas"].get(host["mediaId"]) or {}
        Dh = M.clip_duration(host)
        th = min(max(0.0, t - host["start"]), max(0.0, Dh - 1e-6))
        inside = host["start"] - 1e-9 <= t < host["start"] + Dh
        s = M.src_time(host, th)
        e = track_at(track, s, ctx["interp"](host, meta))
        if e is None:
            return None
        m, sw, sh = host_matrix(host, meta, is_main, ctx["defaultFit"], ctx["W"], ctx["H"], th)
        px, py = M.apply(m, e["x"] * sw, e["y"] * sh)
        k = math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))
        xs.append(px)
        ys.append(py)
        ss.append(math.sqrt(max(1e-9, e["w"] * sw * e["h"] * sh)) * k)
        bws.append(e["w"] * sw * k)
        bhs.append(e["h"] * sh * k)
        angs.append(e["a"] + math.degrees(math.atan2(m[1], m[0])))
        ws.append(weight(e["c"]) if inside else HELD_WEIGHT)
    return {"x": xs, "y": ys, "s": ss, "bw": bws, "bh": bhs, "a": unwrap(angs), "w": ws}


def safe_rect(W: float, H: float, y_center: float, item_h: float) -> tuple[float, float, float, float]:
    """Where an item may sit (x0, y0, x1, y1): below the header, above the caption area, left of the
    button column where it is (its lower part)."""
    x0, y0 = SAFE["left"] * W, SAFE["top"] * H
    y1 = H - SAFE["bottom"] * H
    x1 = W - SAFE["left"] * W
    if y_center + item_h / 2 > SAFE["rightFrom"] * H:
        x1 = W - SAFE["right"] * W
    return x0, y0, x1, y1


def follow_motion(ctx: dict, clip: dict, track: dict, item: tuple[float, float] | None = None) -> dict | None:
    """autoMotion for a clip that follows a track. item = its size at scale 1 (canvas px), for "safe"."""
    fo = clip.get("follow") or {}
    fps = ctx["fps"]
    tg = target_series(ctx, clip, track, fo, fps)
    if tg is None:
        return None
    lmb = lam(float(fo.get("smoothing", 0.3)), fps)
    w = tg["w"]
    X, Y = smooth(tg["x"], w, lmb), smooth(tg["y"], w, lmb)
    S, A = smooth(tg["s"], w, lmb), smooth(tg["a"], w, lmb)
    ref = next((i for i, v in enumerate(w) if v > HELD_WEIGHT), 0)
    s_ref, a_ref = max(1e-6, S[ref]), A[ref]
    pos, scl, rot = fo.get("position", True) is not False, bool(fo.get("scale")), bool(fo.get("rotation"))
    W, H = ctx["W"], ctx["H"]
    out = {"fps": fps, "x": [], "y": [], "s": [], "r": []}
    for i in range(len(X)):
        ds = S[i] / s_ref if scl else 1.0
        dr = A[i] - a_ref if rot else 0.0
        base = M.transform_at({**clip, "autoMotion": None}, i / fps)
        ox, oy = base["x"], base["y"]
        th = math.radians(dr)
        rx = (ox * math.cos(th) - oy * math.sin(th)) * ds
        ry = (ox * math.sin(th) + oy * math.cos(th)) * ds
        if pos:
            dx, dy = X[i] - W / 2 + rx - ox, Y[i] - H / 2 + ry - oy
        else:
            dx, dy = rx - ox, ry - oy
        if fo.get("safe") and item:
            sc = base["scale"] * ds
            hw, hh = item[0] * sc / 2, item[1] * sc / 2
            cx, cy = W / 2 + ox + dx, H / 2 + oy + dy
            x0, y0, x1, y1 = safe_rect(W, H, cy, hh * 2)
            cx2 = min(max(cx, x0 + hw), x1 - hw) if x1 - x0 > 2 * hw else (x0 + x1) / 2
            cy2 = min(max(cy, y0 + hh), y1 - hh) if y1 - y0 > 2 * hh else (y0 + y1) / 2
            dx, dy = dx + cx2 - cx, dy + cy2 - cy
        out["x"].append(dx)
        out["y"].append(dy)
        out["s"].append(ds)
        out["r"].append(dr)
    return out


def region_series(ctx: dict, clip: dict, track: dict) -> dict | None:
    """autoRegion for a blur/pixelate region that follows a track: centre + size (canvas px), padded."""
    fo = clip.get("follow") or {}
    reg = clip.get("region") or {}
    fps = ctx["fps"]
    tg = target_series(ctx, clip, track, fo, fps)
    if tg is None:
        return None
    lmb = lam(float(fo.get("smoothing", 0.15)), fps)
    w = tg["w"]
    pad = 1 + 2 * max(0.0, float(reg.get("padding", 0.25)))
    X, Y = smooth(tg["x"], w, lmb), smooth(tg["y"], w, lmb)
    BW, BH = smooth(tg["bw"], w, lmb), smooth(tg["bh"], w, lmb)
    base = [M.transform_at({**clip, "autoMotion": None}, i / fps) for i in range(len(X))]
    return {"fps": fps, "x": [X[i] + b["x"] for i, b in enumerate(base)], "y": [Y[i] + b["y"] for i, b in enumerate(base)],
            "w": [max(4.0, BW[i] * pad * b["scale"]) for i, b in enumerate(base)],
            "h": [max(4.0, BH[i] * pad * b["scale"]) for i, b in enumerate(base)]}


def reframe_motion(ctx: dict, clip: dict, track: dict, is_main: bool) -> dict | None:
    """autoMotion for auto-reframe: pan the clip (drawn whole, not just its fill crop) so the subject sits
    on the chosen third, with a smooth virtual camera that never shows an edge."""
    rf = clip.get("reframe") or {}
    fps = ctx["fps"]
    meta = ctx["metas"].get(clip.get("mediaId") or "") or {}
    W, H = ctx["W"], ctx["H"]
    D = M.clip_duration(clip)
    n = n_samples(D, fps)
    base_clip = {**clip, "autoMotion": None}
    tx, ty, w = [], [], []
    lo_x, hi_x, lo_y, hi_y = [], [], [], []
    for i in range(n):
        tau = min(i / fps, max(0.0, D - 1e-6))
        s = M.src_time(clip, tau)
        e = track_at(track, s, ctx["interp"](clip, meta))
        if e is None:
            return None
        m, sw, sh = host_matrix(base_clip, meta, is_main, ctx["defaultFit"], W, H, tau)
        px, py = M.apply(m, e["x"] * sw, e["y"] * sh)
        tx.append(W * THIRDS.get(rf.get("third", "center"), 0.5) - px)
        ty.append(H * 0.5 - py)
        w.append(weight(e["c"]))
        # how far the picture can move before an edge shows (whole source drawn)
        xs = [M.apply(m, 0, 0)[0], M.apply(m, sw, 0)[0], M.apply(m, 0, sh)[0], M.apply(m, sw, sh)[0]]
        ys = [M.apply(m, 0, 0)[1], M.apply(m, sw, 0)[1], M.apply(m, 0, sh)[1], M.apply(m, sw, sh)[1]]
        lo_x.append(W - max(xs))
        hi_x.append(-min(xs))
        lo_y.append(H - max(ys))
        hi_y.append(-min(ys))
    lmb = lam(float(rf.get("smoothing", 0.6)), fps)
    clamp = lambda v, lo, hi: min(max(v, lo), hi) if lo <= hi else (lo + hi) / 2  # noqa: E731
    # clamp the wish first (an unreachable subject position isn't chased), smooth, clamp again
    X = smooth([clamp(v, lo_x[i], hi_x[i]) for i, v in enumerate(tx)], w, lmb)
    Y = smooth([clamp(v, lo_y[i], hi_y[i]) for i, v in enumerate(ty)], w, lmb) if rf.get("vertical") else [0.0] * n
    return {"fps": fps, "x": [clamp(v, lo_x[i], hi_x[i]) for i, v in enumerate(X)],
            "y": [clamp(v, lo_y[i], hi_y[i]) for i, v in enumerate(Y)], "s": None, "r": None, "wholeSource": True}


# ---------------------------------------------------------------- the whole project
def resolve(project: dict, metas: dict, fps: float, item_size=None, interp=None) -> dict:
    """{clip id: {"autoMotion": ..., "autoRegion": ...}} for every clip that is reframed, follows a
    track or is a tracked region. item_size(clip) -> (w, h) at scale 1 for "keep inside safe areas";
    interp(clip, meta) -> whether the picture shows in-between frames (slow motion)."""
    st = project.get("settings") or {}
    W, H = float(st.get("width") or 1080), float(st.get("height") or 1920)
    tracks = {t["id"]: t for t in (project.get("motionTracks") or []) if t.get("id")}
    visual = []
    clips = {}
    for t in project.get("tracks") or []:
        if t.get("type") not in ("video", "overlay"):
            continue
        for c in t.get("clips") or []:
            if c.get("mediaId") and (metas.get(c["mediaId"]) or {}).get("type") in ("video", "image"):
                visual.append((c, t.get("type") == "video"))
                clips[c["id"]] = (c, t.get("type") == "video")
    ctx = {"W": W, "H": H, "fps": fps, "metas": metas, "visual": visual, "clips": clips,
           "defaultFit": ((st.get("background") or {}).get("mode") or "fit"), "interp": interp or (lambda c, m: True)}
    out: dict[str, dict] = {}
    # 1) reframed clips first: followers on them see the reframed picture
    for c, is_main in visual:
        rf = c.get("reframe") or {}
        tr = tracks.get(rf.get("trackId") or "")
        if rf.get("enabled", True) and tr and tr.get("mediaId") == c.get("mediaId"):
            am = reframe_motion(ctx, c, tr, is_main)
            if am:
                out[c["id"]] = {"autoMotion": am}
    if out:
        for i, (c, is_main) in enumerate(visual):
            if c["id"] in out:
                c2 = {**c, "autoMotion": out[c["id"]]["autoMotion"]}
                visual[i] = (c2, is_main)
                clips[c["id"]] = (c2, is_main)
    # 2) followers and regions
    for t in project.get("tracks") or []:
        if t.get("type") not in ("overlay", "text"):
            continue
        for c in t.get("clips") or []:
            fo = c.get("follow") or {}
            tr = tracks.get(fo.get("trackId") or "")
            if not tr or fo.get("enabled") is False:
                continue
            if c.get("region") and c.get("adjustment"):
                rg = region_series(ctx, c, tr)
                if rg:
                    out[c["id"]] = {"autoRegion": rg}
                continue
            am = follow_motion(ctx, c, tr, item_size(c) if (item_size and fo.get("safe")) else None)
            if am:
                out[c["id"]] = {"autoMotion": am}
    return out


# ---------------------------------------------------------------- sampling the result
def auto_at(am: dict | None, tau: float) -> dict | None:
    return M.auto_at(am, tau)


def region_at(rg: dict, tau: float) -> tuple[float, float, float, float]:
    fps = float(rg["fps"])
    n = len(rg["x"])
    f = min(max(0.0, tau * fps), n - 1)
    i = int(math.floor(f))
    j = min(i + 1, n - 1)
    u = f - i
    return tuple(rg[k][i] + (rg[k][j] - rg[k][i]) * u for k in ("x", "y", "w", "h"))  # type: ignore[return-value]


def simplify(vals: list[float], fps: float, tol: float) -> list[dict]:
    """Linear keyframes (clip-relative times) within `tol` of the sampled values (Douglas-Peucker),
    for the export's per-frame expressions."""
    n = len(vals)
    if n == 0:
        return []
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        va, vb = vals[a], vals[b]
        worst, wi = -1.0, -1
        for i in range(a + 1, b):
            e = abs(vals[i] - (va + (vb - va) * (i - a) / (b - a)))
            if e > worst:
                worst, wi = e, i
        if worst > tol:
            keep[wi] = True
            stack.append((a, wi))
            stack.append((wi, b))
    return [{"time": i / fps, "value": vals[i], "easing": "linear"} for i in range(n) if keep[i]]
