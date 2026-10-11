"""Building blocks for clip rendering: timed source reading (speed, ramps, reverse, freeze),
transformed layers (crop/scale/rotate/flip/position + keyframes) via `perspective`, and audio gain.

The geometry is backend/app/motion.py's layer matrix (shared with the preview). `perspective`
(sense=source) maps the output frame's edges to source coordinates edge-to-edge, which is exactly
the canvas drawImage convention, so the preview's setTransform and the export agree.
"""
from __future__ import annotations

import math
from pathlib import Path

from . import motion as M

PAD = 2  # transparent border so perspective's edge clamp yields transparency outside the picture


def _f(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


def rnd(v: float) -> int:
    """Round half up (same as Math.floor(v + 0.5) in the preview)."""
    return int(math.floor(v + 0.5))


# ---------------------------------------------------------------- expressions
EASE_EXPR = {
    "linear": "{u}",
    "in": "pow({u},3)",
    "out": "(1-pow(1-{u},3))",
    "inout": "if(lt({u},0.5),4*pow({u},3),1-pow(-2*{u}+2,3)/2)",
    "punch": "(1+3.2*pow({u}-1,3)+2.2*pow({u}-1,2))",
}


def kf_expr(kfs, default: float, T: str) -> str:
    """FFmpeg expression for motion.value_at(kfs, T, default)."""
    ks = M.sorted_kfs(kfs)
    if not ks:
        return _f(default)
    expr = _f(ks[-1]["value"])
    for a, b in reversed(list(zip(ks, ks[1:]))):
        span = b["time"] - a["time"]
        if span <= 1e-9:
            continue
        u = f"clip(({T}-({_f(a['time'])}))/{_f(span)},0,1)"
        e = EASE_EXPR.get(a.get("easing", "linear"), "{u}").format(u=u)
        seg = f"({_f(a['value'])}+({_f(b['value'] - a['value'])})*{e})"
        expr = f"if(lt({T},{_f(b['time'])}),{seg},{expr})"
    return f"if(lt({T},{_f(ks[0]['time'])}),{_f(ks[0]['value'])},{expr})"


def gain_expr(c: dict, D: float, tau0: float, base: float, duck: list[tuple[float, float]] | None = None,
              duck_db: float = -12.0, t_abs0: float = 0.0) -> str:
    """Volume expression (volume filter, eval=frame; `t` = time since this audio stream started at
    clip-relative time tau0): keyframed volume x base x linear fades x ducking."""
    T = f"({_f(tau0)}+t)"
    vol = kf_expr((c.get("keyframes") or {}).get("volume"), float(c.get("volume", 1)), T)
    parts = [f"({vol})*{_f(base)}"]
    fi, fo = float(c.get("fadeIn") or 0), float(c.get("fadeOut") or 0)
    if fi > 0:
        parts.append(f"clip({T}/{_f(fi)},0,1)")
    if fo > 0:
        parts.append(f"clip(({_f(D)}-{T})/{_f(fo)},0,1)")
    if duck:
        level = 10 ** (duck_db / 20)
        A, R = 0.15, 0.45
        ta = f"({_f(t_abs0)}+t)"
        g = "1"
        for a, b in duck:
            w = (f"if(between({ta},{_f(a)},{_f(b)}),1,if(between({ta},{_f(a - A)},{_f(a)}),({ta}-{_f(a - A)})/{_f(A)},"
                 f"if(between({ta},{_f(b)},{_f(b + R)}),1-({ta}-{_f(b)})/{_f(R)},0)))")
            g = f"min({g},1-{_f(1 - level)}*{w})"
        parts.append(g)
    return "*".join(parts)


# ---------------------------------------------------------------- timed source
def time_pieces(c: dict, tau0: float, nf: int, fps: float, frame_at) -> list[dict]:
    """Split [tau0, tau0 + nf/fps) into constant-speed pieces on the frame grid.
    `frame_at(tau)` returns the frame index (relative to tau0's frame) of clip time tau."""
    if c.get("freezeFrame") is not None:
        return [{"nf": nf, "src": float(c["freezeFrame"]), "speed": 0.0, "freeze": True}]
    segs = M.ramp_segments(c)
    bounds = sorted({0, nf} | {min(nf, max(0, frame_at(s["t1"]))) for s in segs[:-1]})
    out = []
    for f0, f1 in zip(bounds, bounds[1:]):
        if f1 <= f0:
            continue
        tau = tau0 + f0 / fps
        seg = segs[-1]
        for s in segs:
            if tau + 0.5 / fps < s["t1"]:
                seg = s
                break
        out.append({"nf": f1 - f0, "src": seg["src0"] + (tau - seg["t0"]) * seg["speed"], "speed": seg["speed"], "freeze": False})
    return out


def source_raw(g, c: dict, m: dict, path: str, tau0: float, nf: int, fps: float, frame_at, *,
               want_audio: bool, tonemap: str = "", want_video: bool = True) -> tuple[str | None, str | None]:
    """Video (native size, exactly nf frames at fps) and audio (nf/fps s, stereo 48 kHz, tempo-adjusted,
    no gain) for clip-relative time [tau0, tau0 + nf/fps). Missing footage holds the edge frame."""
    seg_total = nf / fps
    if m["type"] == "image":
        idx = g.add_input(["-loop", "1", "-framerate", _f(fps), "-t", _f(seg_total + 0.1), "-i", path])
        v = g.label("rv")
        g.add(f"[{idx}:v]setpts=PTS-STARTPTS,fps={_f(fps)},trim=end_frame={nf}[{v}]")
        return v, None
    dur_m = float(m.get("duration") or 0)
    mfps = m.get("fps") or fps
    last = max(0.0, dur_m - 1.5 / mfps)
    rev = bool(c.get("reverse"))
    lo_src, hi_src = float(c.get("inPoint", 0)), float(c.get("outPoint", 0))
    vs, as_ = [], []
    for pc in time_pieces(c, tau0, nf, fps, frame_at):
        n, seg = pc["nf"], pc["nf"] / fps
        v, a = g.label("rv"), g.label("ra")
        if pc["freeze"]:
            s = min(max(0.0, pc["src"]), last)
            idx = g.add_input(["-ss", _f(s), "-t", "0.3", "-i", path])
            if want_video:
                g.add(f"[{idx}:v]{tonemap}setpts=PTS-STARTPTS,trim=end_frame=1,fps={_f(fps)},"
                      f"tpad=stop_mode=clone:stop_duration={_f(seg + 0.2)},trim=end_frame={n}[{v}]")
            if want_audio:
                g.add(f"anullsrc=r=48000:cl=stereo,atrim=duration={_f(seg)}[{a}]")
            vs.append(v)
            as_.append(a)
            continue
        sp = max(0.05, pc["speed"])
        s0 = pc["src"]  # forward source time at the piece start (before mirroring)
        if rev:
            # Mirrored: the piece shows source from (lo+hi-s0) going backwards over seg*sp seconds.
            top = lo_src + hi_src - s0
            start, length = top - seg * sp, seg * sp
        else:
            start, length = s0, seg * sp
        lead = 0.0
        if start < 0:
            lead, length, start = -start / sp, length + start, 0.0
        start = min(start, last)
        length = max(0.1, length)
        rv = "reverse," if rev else ""
        # Each output frame shows the last source frame at or before its time (like a paused <video>
        # element in the preview): seek to the frame on screen at `start` (an accurate seek would
        # begin at the next one and STARTPTS would pull it early), place frames at their true offset
        # from `start`, and let fps round up (with a hair of slack for timestamp rounding).
        delta = 0.0
        if not rev:
            aligned = math.floor(start * mfps + 1e-6) / mfps
            delta, start, length = start - aligned, aligned, length + (start - aligned)
        # (the seek point sits a hair before the frame, so 6-decimal rounding can't skip it)
        idx = g.add_input(["-ss", _f(max(0.0, start - (0.0002 if not rev else 0.0))), "-t", _f(length + 0.1 * sp), "-i", path])
        shift = f"-{_f(delta)}/TB" if delta > 1e-9 else ""
        pre = (f"setpts=(PTS-STARTPTS{shift})/{_f(sp)}-0.0001/TB," if abs(sp - 1) > 1e-6 else f"setpts=PTS-STARTPTS{shift}-0.0001/TB,")
        # trim to the needed source span before reversing so reverse buffers only this piece
        if want_video:
            g.add(f"[{idx}:v]{tonemap}trim=duration={_f(length)},{rv}{pre}fps={_f(fps)}:round=up:start_time=0,"
              f"tpad=start_mode=clone:start_duration={_f(lead if not rev else 0)}:"
                  f"stop_mode=clone:stop_duration={_f(seg + 0.2 + (lead if rev else 0))},trim=end_frame={n}[{v}]")
        if want_audio and m.get("hasAudio"):
            ar = "areverse," if rev else ""
            g.add(f"[{idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,atrim=start={_f(delta)}:end={_f(length)},"
                  f"{ar}asetpts=PTS-STARTPTS{atempo(sp)},"
                  f"{'adelay=' + str(int(round(lead * 1000))) + ':all=1,' if lead > 0 and not rev else ''}"
                  f"apad=whole_dur={_f(seg)},atrim=duration={_f(seg)},asetpts=PTS-STARTPTS[{a}]")
        elif want_audio:
            g.add(f"anullsrc=r=48000:cl=stereo,atrim=duration={_f(seg)}[{a}]")
        vs.append(v)
        as_.append(a)
    if len(vs) == 1:
        return (vs[0] if want_video else None), (as_[0] if want_audio else None)
    v, a = g.label("rcv"), g.label("rca")
    if want_video and want_audio:
        g.add("".join(f"[{x}][{y}]" for x, y in zip(vs, as_)) + f"concat=n={len(vs)}:v=1:a=1[{v}][{a}]")
    elif want_video:
        g.add("".join(f"[{x}]" for x in vs) + f"concat=n={len(vs)}:v=1:a=0[{v}]")
    else:
        g.add("".join(f"[{y}]" for y in as_) + f"concat=n={len(as_)}:v=0:a=1[{a}]")
    return (v if want_video else None), (a if want_audio else None)


def atempo(speed: float) -> str:
    if abs(speed - 1) < 1e-6:
        return ""
    parts = []
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    while speed > 2.0:
        parts.append("atempo=2")
        speed /= 2.0
    parts.append(f"atempo={_f(speed)}")
    return "," + ",".join(parts)


# ---------------------------------------------------------------- transformed layer
def max_scale(c: dict) -> float:
    kf = c.get("keyframes") or {}
    vals = [float((c.get("transform") or {}).get("scale", 1))]
    vals += [float(k["value"]) for k in M.sorted_kfs(kf.get("scale"))]
    punch = max([1.0] + [float(k["value"]) for k in M.sorted_kfs(kf.get("punch"))])
    return max(0.01, max(vals) * punch)


def rounded_rect(src_w: float, src_h: float, crop: dict, fill: bool, W: int, H: int) -> tuple[int, int, int, int]:
    x0, y0, cw, ch = M.source_rect(src_w, src_h, crop, fill, W, H)
    return rnd(x0), rnd(y0), max(2, rnd(cw)), max(2, rnd(ch))


def motion_layer(g, src: str, c: dict, src_w: int, src_h: int, fill: bool, W: int, H: int, tau0: float, fps: float,
                 nf: int, workdir: Path, *, rect: tuple[int, int, int, int] | None = None) -> str:
    """Canvas-sized RGBA layer of `src` placed by the clip's (keyframed) transform."""
    tr = c.get("transform") or {}
    crop = tr.get("crop") or {}
    x0, y0, cw, ch = rect or rounded_rect(src_w, src_h, crop, fill, W, H)
    f = min(W / cw, H / ch)
    dens = min(1.0, f * max_scale(c))
    ww, wh = max(2, rnd(cw * dens)), max(2, rnd(ch * dens))
    kx, ky = ww / cw, wh / ch
    Wp, Hp = max(ww + 2 * PAD, W), max(wh + 2 * PAD, H)
    kf = c.get("keyframes") or {}
    T = f"({_f(tau0)}+(in-1)/{_f(fps)})"
    X = f"({kf_expr(kf.get('x'), float(tr.get('x', 0)), T)})+({kf_expr(kf.get('shakeX'), 0.0, T)})"
    Y = f"({kf_expr(kf.get('y'), float(tr.get('y', 0)), T)})+({kf_expr(kf.get('shakeY'), 0.0, T)})"
    S = f"({kf_expr(kf.get('scale'), float(tr.get('scale', 1)), T)})*({kf_expr(kf.get('punch'), 1.0, T)})"
    R = f"({kf_expr(kf.get('rotation'), float(tr.get('rotation', 0)), T)})+({kf_expr(kf.get('shakeR'), 0.0, T)})"
    fx = -1 if tr.get("flipH") else 1
    fy = -1 if tr.get("flipV") else 1
    cx, cy = x0 + cw / 2, y0 + ch / 2
    pre = f"st(0,{X});st(1,{Y});st(2,{S});st(3,({R})*PI/180);"

    def corner(Qx: float, Qy: float) -> tuple[str, str]:
        dx, dy = f"({_f(Qx)}-{_f(W / 2)}-ld(0))", f"({_f(Qy)}-{_f(H / 2)}-ld(1))"
        px = f"((cos(ld(3))*{dx}+sin(ld(3))*{dy})/(ld(2)*{_f(f * fx)})+{_f(cx)})"
        py = f"((-sin(ld(3))*{dx}+cos(ld(3))*{dy})/(ld(2)*{_f(f * fy)})+{_f(cy)})"
        return f"'{pre}({px}-{_f(x0)})*{_f(kx)}+{PAD}'", f"'{pre}({py}-{_f(y0)})*{_f(ky)}+{PAD}'"

    cs = [corner(0, 0), corner(W, 0), corner(0, H), corner(W, H)]
    out = g.label("ml")
    chain = [f"[{src}]format=rgba,crop={cw}:{ch}:{x0}:{y0}:exact=1"]
    if (ww, wh) != (cw, ch):
        chain.append(f"scale={ww}:{wh}:flags=bicubic")
    chain.append(f"pad={Wp}:{Hp}:{PAD}:{PAD}:color=0x00000000")
    chain.append("perspective=" + ":".join(f"x{i}={cs[i][0]}:y{i}={cs[i][1]}" for i in range(4))
                 + ":sense=source:eval=frame:interpolation=linear")
    if (Wp, Hp) != (W, H):
        chain.append(f"scale={W}:{H}:flags=bicubic")
    # Opacity: constant, or per frame via sendcmd (keyframed).
    okf = kf.get("opacity")
    if okf:
        name = g.label("op")
        cmd = workdir / f"{name}.txt"
        cmd.write_text("".join(f"{_f(k / fps)} colorchannelmixer@{name} aa {_f(max(0.0, min(1.0, M.value_at(okf, tau0 + k / fps, 1.0))))};\n"
                               for k in range(nf)), encoding="utf-8")
        chain.append(f"sendcmd=f={cmd.name},colorchannelmixer@{name}=aa=1")
    elif float(tr.get("opacity", 1)) < 1 - 1e-6:
        chain.append(f"colorchannelmixer=aa={_f(max(0.0, float(tr.get('opacity', 1))))}")
    g.add(",".join(chain) + f"[{out}]")
    return out


def intensity_sendcmd(g, graded: str, base: str, c: dict, tau0: float, nf: int, fps: float, workdir: Path) -> str:
    """Mix graded over ungraded by the keyframed 'intensity' (1 = full grade)."""
    kfs = (c.get("keyframes") or {}).get("intensity")
    name = g.label("gi")
    cmd = workdir / f"{name}.txt"
    cmd.write_text("".join(f"{_f(k / fps)} blend@{name} all_opacity {_f(max(0.0, min(1.0, M.value_at(kfs, tau0 + k / fps, 1.0))))};\n"
                           for k in range(nf)), encoding="utf-8")
    a, b, out = g.label("gia"), g.label("gib"), g.label("gio")
    g.add(f"[{graded}]format=yuv420p,sendcmd=f={cmd.name}[{a}]")
    g.add(f"[{base}]format=yuv420p[{b}]")
    g.add(f"[{a}][{b}]blend@{name}=all_mode=normal:all_opacity=1[{out}]")
    return out
