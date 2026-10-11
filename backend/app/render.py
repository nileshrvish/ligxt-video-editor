"""Project JSON -> ffmpeg filter graph -> export.

Phase 1 scope: the main video track (clips + gaps, trimmed, fitted to the canvas with
fit / fill / blurred-background), per-clip volume and mute, and audio/music tracks mixed in.
Range export only renders the clips that overlap the range.
"""
from __future__ import annotations

import math
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import config
from .config import CACHE_DIR, EXPORTS_DIR
from .ffmpeg_utils import FFmpegError, has_nvenc, run_ffmpeg, stream_durations
from .jobs import JobContext, jobs
from .media import load_meta, original_path
from .models import Clip, Project
from .projects import load_project
from . import layers as LY
from . import motion as MO
from .grade import TO_GBRP, grade_chain, needs_grade, ref_scale
from .transitions import ALL_TYPES, WHIP_TYPES, TransitionPlan, plan_transition

router = APIRouter(prefix="/api", tags=["export"])

EPS = 1e-3
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv")


class RenderError(ValueError):
    """A user-fixable problem with the project (shown verbatim in the UI)."""


@dataclass
class Graph:
    inputs: list[list[str]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    _n: int = 0

    def add_input(self, args: list[str]) -> int:
        self.inputs.append(args)
        return len(self.inputs) - 1

    def label(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def add(self, line: str) -> None:
        self.lines.append(line)


def _f(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


def _color(c: str) -> str:
    c = (c or "#000000").lstrip("#")
    return "0x" + (c[:6] if re.fullmatch(r"[0-9a-fA-F]{6,8}", c) else "000000")


def atempo(speed: float) -> str:
    """Pitch-preserving tempo change; atempo accepts 0.5..100 per instance, so chain for slower."""
    if abs(speed - 1) < EPS:
        return ""
    parts = []
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={_f(speed)}")
    return "," + ",".join(parts)


def clip_duration(c: Clip) -> float:
    return max(0.0, MO.clip_duration(c.model_dump()))


def blur_backdrop(g: Graph, src: str, W: int, H: int) -> str:
    """The blurred 'cover' background used by the blur fit mode (same as in fit_chain)."""
    out = g.label("bbg")
    g.add(f"[{src}]scale={W // 8}:{H // 8}:force_original_aspect_ratio=increase,crop={W // 8}:{H // 8},"
          f"gblur=sigma=6,scale={W}:{H},eq=brightness=-0.06,setsar=1,format=yuv420p[{out}]")
    return out


def fit_chain(g: Graph, src: str, mode: str, W: int, H: int, bg: str) -> str:
    """Scale the stream at label `src` onto a WxH canvas. Returns the output label."""
    out = g.label("fit")
    if mode == "fill":
        g.add(f"[{src}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[{out}]")
    elif mode == "blur":
        a, b, bgl, fg = g.label("ba"), g.label("bb"), g.label("bbg"), g.label("bfg")
        g.add(f"[{src}]split=2[{a}][{b}]")
        # Blur at low resolution, then scale up: much faster than blurring at full size.
        g.add(f"[{a}]scale={W // 8}:{H // 8}:force_original_aspect_ratio=increase,crop={W // 8}:{H // 8},"
              f"gblur=sigma=6,scale={W}:{H},eq=brightness=-0.06,setsar=1[{bgl}]")
        g.add(f"[{b}]scale={W}:{H}:force_original_aspect_ratio=decrease,setsar=1[{fg}]")
        g.add(f"[{bgl}][{fg}]overlay=(W-w)/2:(H-h)/2[{out}]")
    else:
        g.add(f"[{src}]scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={bg},setsar=1[{out}]")
    return out


def overlay_geometry(m: dict, c: Clip, W: int, H: int) -> dict:
    """Size of an overlay layer on a WxH canvas (shared contract with the preview player):
    the source is fitted (fit = contain, fill = cover then cropped to the canvas) and then scaled by
    transform.scale about its centre; transform.x/y offset it from the canvas centre."""
    sw0 = max(1, int(m.get("width") or W))
    sh0 = max(1, int(m.get("height") or H))
    mode = c.fit if c.fit in ("fit", "fill") else "fit"
    f = (max if mode == "fill" else min)(W / sw0, H / sh0)
    k = max(0.01, c.transform.scale)
    even = lambda v: max(2, int(round(v / 2)) * 2)  # noqa: E731
    sw, sh = even(sw0 * f * k), even(sh0 * f * k)
    cw, ch = min(sw, even(W * k)), min(sh, even(H * k))
    return {"sw": sw, "sh": sh, "crop": mode == "fill" and (cw < sw or ch < sh), "cw": cw, "ch": ch}


WHIP_FPS = 960  # sub-frame rate for the whip pan's motion blur
WHIP_SAMPLES = 32  # sub-frames averaged per output frame (a 1/30 s shutter at 960 fps; 8 showed stepping)


def whip_chain(g: Graph, va: str, vb: str, out: str, kind: str, D: float, W: int, H: int, fps: float) -> None:
    """Custom whip pan: an eased (cubic in-out) slide of A out and B in, with real motion blur from
    averaging sub-frames. Mirrored by frontend/src/player/transitions.ts."""
    u = f"min(t/{_f(D)},1)"
    e = f"if(lt({u},0.5),4*pow({u},3),1-pow(-2*{u}+2,3)/2)"
    pos = {
        "whipleft": (f"-W*({e})", "0", f"W-W*({e})", "0"),
        "whipright": (f"W*({e})", "0", f"-W+W*({e})", "0"),
        "whipup": ("0", f"-H*({e})", "0", f"H-H*({e})"),
        "whipdown": ("0", f"H*({e})", "0", f"-H+H*({e})"),
    }[kind]
    bgl, a2, b2, t1, t2 = g.label("wbg"), g.label("wa"), g.label("wb"), g.label("wt"), g.label("wt")
    g.add(f"color=c=black:s={W}x{H}:r={WHIP_FPS}:d={_f(D + 0.1)},format=yuv420p[{bgl}]")
    g.add(f"[{va}]fps={WHIP_FPS}[{a2}]")
    g.add(f"[{vb}]fps={WHIP_FPS}[{b2}]")
    g.add(f"[{bgl}][{a2}]overlay=x='{pos[0]}':y='{pos[1]}':eval=frame:eof_action=pass[{t1}]")
    g.add(f"[{t1}][{b2}]overlay=x='{pos[2]}':y='{pos[3]}':eval=frame:eof_action=pass[{t2}]")
    g.add(f"[{t2}]tmix=frames={WHIP_SAMPLES},fps={_f(fps)},trim=duration={_f(D)},format=yuv420p[{out}]")


@dataclass
class Plan:
    args: list[str]
    filter_script: str
    duration: float
    output: Path
    fps: float
    workdir: Path | None = None  # ffmpeg runs here (generated .cube LUTs are referenced by bare name)


def build_plan(project: Project, out_path: Path, *, mode: str = "final",
               range_start: float | None = None, range_end: float | None = None,
               workdir: Path | None = None) -> Plan:
    s = project.settings
    ex = project.export
    W, H = int(ex.width or s.width), int(ex.height or s.height)
    W, H = W - W % 2, H - H % 2
    bg = _color(s.background.color)
    default_fit = s.background.mode

    main = next((t for t in project.tracks if t.type == "video"), None)
    clips = sorted([c for c in (main.clips if main and not main.hidden else []) if clip_duration(c) > EPS],
                   key=lambda c: c.start)
    if not clips:
        raise RenderError("The main video track is empty. Add at least one clip to it before exporting.")

    metas: dict[str, dict] = {}
    for t in project.tracks:
        for c in t.clips:
            if c.mediaId and c.mediaId not in metas:
                m = load_meta(c.mediaId)
                if not m:
                    raise RenderError(f"A clip on '{t.name or t.type}' uses media that no longer exists. "
                                      f"Remove that clip or re-import the file.")
                if not original_path(m).exists():
                    raise RenderError(f"The original file for '{m['name']}' is missing from the media folder.")
                metas[c.mediaId] = m

    from .luts import load_cube
    cubes: dict[str, dict] = {}
    for t in project.tracks:
        for c in t.clips:
            for f in c.filters:
                lid = (f.params or {}).get("lut") if f.type == "lut" else None
                if lid and lid not in cubes:
                    try:
                        cubes[lid] = load_cube(lid)
                    except (FileNotFoundError, Exception) as e:  # noqa: BLE001
                        raise RenderError(f"A clip uses the LUT '{lid}', which is missing or invalid ({e}).") from e
    if workdir is None:
        workdir = CACHE_DIR / "_render" / "plan"
    workdir.mkdir(parents=True, exist_ok=True)
    rs = ref_scale(W, H)

    def filt(c: Clip) -> list[dict]:
        return [f.model_dump() for f in c.filters]

    fps = ex.fps
    if fps == "source":
        first_video = next((metas[c.mediaId] for c in clips if metas[c.mediaId]["type"] == "video"), None)
        fps = (first_video or {}).get("fps") or s.fps or 30
    fps = float(fps)

    total = max(c.start + clip_duration(c) for c in clips)
    r0 = max(0.0, range_start or 0.0)
    r1 = min(total, range_end) if range_end and range_end > 0 else total
    if r1 - r0 < 0.05:
        raise RenderError("The export range is empty. Pick an end time after the start time.")
    dur = r1 - r0

    g = Graph()
    segs_v: list[str] = []
    segs_a: list[str] = []
    main_muted = main.muted if main else False
    main_vol = main.volume if main else 1

    # Segment boundaries are snapped to the output frame grid (absolute, not per segment), so
    # rounding never accumulates: every cut lands within half a frame of its timeline time.
    def frame_of(t: float) -> int:
        return int(round((t - r0) * fps))

    def gap(a_t: float, b_t: float) -> None:
        nf = frame_of(b_t) - frame_of(a_t)
        if nf <= 0:
            return
        v, a = g.label("gv"), g.label("ga")
        g.add(f"color=c={bg}:s={W}x{H}:r={_f(fps)}:d={_f(nf / fps + 0.1)},format=yuv420p,setsar=1,"
              f"trim=end_frame={nf}[{v}]")
        g.add(f"anullsrc=r=48000:cl=stereo,atrim=duration={_f(nf / fps)}[{a}]")
        segs_v.append(v)
        segs_a.append(a)

    def frame_at_fn(c: Clip, tau0: float):
        base = frame_of(c.start + tau0)
        return lambda tau: frame_of(c.start + tau) - base

    def clip_source(c: Clip, t_from: float, nf: int) -> tuple[str, str]:
        """Video + audio labels for clip `c` from timeline time `t_from`, exactly `nf` frames long.
        Reaches past the clip's in/out points (transition handles); where the source has no footage
        the first/last frame is held and the audio is silent. Applies speed/ramps/reverse/freeze,
        the clip's (keyframed) transform, grading and audio gain."""
        m = metas[c.mediaId]
        cd = c.model_dump()
        seg = nf / fps
        tau0 = t_from - c.start
        D = clip_duration(c)
        hdr = TONEMAP + "," if m.get("hdr") else ""
        audible = m.get("hasAudio") and m["type"] == "video" and not c.muted and not main_muted and c.volume > 0
        raw, raw_a = LY.source_raw(g, cd, m, str(original_path(m)), tau0, nf, fps, frame_at_fn(c, tau0),
                                   want_audio=bool(audible), tonemap=hdr)
        mode = c.fit or default_fit
        if MO.has_motion(cd):
            src_w, src_h = int(m.get("width") or W), int(m.get("height") or H)
            if mode == "blur":
                sp_a, sp_b = g.label("sa"), g.label("sb")
                g.add(f"[{raw}]split[{sp_a}][{sp_b}]")
                back = blur_backdrop(g, sp_a, W, H)
                raw = sp_b
            else:
                back = g.label("bk")
                g.add(f"color=c={bg}:s={W}x{H}:r={_f(fps)}:d={_f(seg + 0.1)},format=yuv420p,setsar=1[{back}]")
            lay = LY.motion_layer(g, raw, cd, src_w, src_h, mode == "fill", W, H, tau0, fps, nf, workdir)
            fitted = g.label("mc")
            g.add(f"[{back}][{lay}]overlay=0:0:format=auto:shortest=1,format=yuv420p[{fitted}]")
        else:
            fitted = fit_chain(g, raw, mode, W, H, bg)
        vout = g.label("cv")
        if needs_grade(filt(c)):
            pre_g = g.label("pg")
            g.add(f"[{fitted}]scale=out_range=tv,format=yuv420p[{pre_g}]")
            if (c.keyframes or {}).get("intensity"):
                base_a, base_b = g.label("ia"), g.label("ib")
                g.add(f"[{pre_g}]split[{base_a}][{base_b}]")
                graded = grade_chain(g, base_a, filt(c), W, H, rs=rs, fps=fps, t0=t_from, f0=int(round(t_from * fps)),
                                     duration=seg, workdir=workdir, cubes=cubes)
                fitted = LY.intensity_sendcmd(g, graded, base_b, cd, tau0, nf, fps, workdir)
            else:
                fitted = grade_chain(g, pre_g, filt(c), W, H, rs=rs, fps=fps, t0=t_from, f0=int(round(t_from * fps)),
                                     duration=seg, workdir=workdir, cubes=cubes)
        g.add(f"[{fitted}]scale=out_range=tv,format=yuv420p,tpad=stop_mode=clone:stop_duration={_f(seg + 0.1)},"
              f"trim=end_frame={nf},setpts=PTS-STARTPTS[{vout}]")
        aout = g.label("ca")
        if audible and raw_a:
            g.add(f"[{raw_a}]volume=eval=frame:volume='{LY.gain_expr(cd, D, tau0, main_vol)}',"
                  f"apad=whole_dur={_f(seg)},atrim=duration={_f(seg)},asetpts=PTS-STARTPTS[{aout}]")
        else:
            g.add(f"anullsrc=r=48000:cl=stereo,atrim=duration={_f(seg)}[{aout}]")
        return vout, aout

    # Transitions on the main track (centred on the cut, using handles).
    by_id = {c.id: c for c in clips}

    def src_dur(c: Clip) -> float:
        m = metas[c.mediaId]
        return math.inf if m["type"] == "image" else float(m["duration"])

    plans: dict[str, TransitionPlan] = {}  # keyed by the outgoing clip id
    for tr in project.transitions:
        a, b = by_id.get(tr.fromClipId), by_id.get(tr.toClipId)
        if not main or tr.trackId != main.id or not a or not b or a.id in plans:
            continue
        plan = plan_transition(tr.model_dump(), a.model_dump(), b.model_dump(), src_dur(a), src_dur(b))
        if plan and plan.half > EPS and plan.type in ALL_TYPES:
            plans[a.id] = plan
    incoming = {p.to_id: p.half for p in plans.values()}

    def transition_piece(p: TransitionPlan) -> None:
        fa, fb = frame_of(p.start), frame_of(p.end)
        lo, hi = max(fa, 0), min(fb, frame_of(r1))
        nf = fb - fa
        if nf <= 0 or hi <= lo:
            return
        va, aa = clip_source(by_id[p.from_id], p.start, nf)
        vb, ab = clip_source(by_id[p.to_id], p.start, nf)
        D = nf / fps
        vx = g.label("tx")
        if p.type in WHIP_TYPES:
            whip_chain(g, va, vb, vx, p.type, D, W, H, fps)
        else:
            # Planar RGB: every plane is full size, so xfade's formulas apply as written (on yuv420p its
            # zoomin indexes the half-size chroma planes with luma coordinates). Matches the preview shader.
            ra, rb = g.label("ra"), g.label("rb")
            g.add(f"[{va}]{TO_GBRP}[{ra}]")
            g.add(f"[{vb}]{TO_GBRP}[{rb}]")
            g.add(f"[{ra}][{rb}]xfade=transition={p.type}:duration={_f(D)}:offset=0,format=yuv420p[{vx}]")
        v, a = g.label("tv"), g.label("ta")
        g.add(f"[{vx}]trim=start_frame={lo - fa}:end_frame={hi - fa},setpts=PTS-STARTPTS[{v}]")
        a1, a2 = g.label("tfa"), g.label("tfb")
        g.add(f"[{aa}]afade=t=out:st=0:d={_f(D)}[{a1}]")
        g.add(f"[{ab}]afade=t=in:st=0:d={_f(D)}[{a2}]")
        g.add(f"[{a1}][{a2}]amix=inputs=2:normalize=0:duration=longest,"
              f"atrim=start={_f((lo - fa) / fps)}:end={_f((hi - fa) / fps)},asetpts=PTS-STARTPTS[{a}]")
        segs_v.append(v)
        segs_a.append(a)

    cursor = r0
    for c in clips:
        c_start, c_end = c.start, c.start + clip_duration(c)
        solid_start = c_start + incoming.get(c.id, 0.0)
        solid_end = c_end - (plans[c.id].half if c.id in plans else 0.0)
        if not (solid_end <= r0 + EPS or solid_start >= r1 - EPS):
            a_t, b_t = max(solid_start, r0, cursor), min(solid_end, r1)
            if b_t - a_t > EPS:
                if a_t - cursor > EPS:
                    gap(cursor, a_t)
                nf = frame_of(b_t) - frame_of(a_t)
                if nf > 0:
                    v, a = clip_source(c, a_t, nf)
                    segs_v.append(v)
                    segs_a.append(a)
                cursor = b_t
        if c.id in plans:
            p = plans[c.id]
            if p.end > r0 + EPS and p.start < r1 - EPS:
                if max(p.start, r0) - cursor > EPS:
                    gap(cursor, max(p.start, r0))
                transition_piece(p)
            cursor = max(cursor, min(p.end, r1))

    if r1 - cursor > EPS:
        gap(cursor, r1)

    n = len(segs_v)
    joined = "".join(f"[{v}][{a}]" for v, a in zip(segs_v, segs_a))
    g.add(f"{joined}concat=n={n}:v=1:a=1[vcat][acat]")

    # Overlay tracks: composited over the main track in track order (later tracks on top).
    v_base = "vcat"
    extra: list[str] = []
    for t in project.tracks:
        if t.type != "overlay" or t.hidden:
            continue
        for c in sorted(t.clips, key=lambda c: c.start):
            if c.adjustment and not c.mediaId:
                a_t, b_t = max(c.start, r0), min(c.start + clip_duration(c), r1)
                if b_t - a_t <= EPS or not needs_grade(filt(c)):
                    continue
                keep, gin, out = g.label("ak"), g.label("ag"), g.label("vb")
                g.add(f"[{v_base}]format=yuv420p,split[{keep}][{gin}]")
                graded = grade_chain(g, gin, filt(c), W, H, rs=rs, fps=fps, t0=r0, f0=int(round(r0 * fps)),
                                     duration=dur, workdir=workdir, cubes=cubes)
                op = max(0.0, min(1.0, c.transform.opacity))
                ikf = (c.keyframes or {}).get("intensity")
                if ikf:
                    # Keyframed strength: per-frame blend opacity (blend's first input is the ungraded one).
                    name = g.label("ai")
                    cmd = workdir / f"{name}.txt"
                    ikd = [k.model_dump() if hasattr(k, "model_dump") else k for k in ikf]
                    cmd.write_text("".join(
                        f"{_f(n / fps)} blend@{name} all_opacity "
                        f"{_f(1 - op * max(0.0, min(1.0, MO.value_at(ikd, r0 + n / fps - c.start, 1.0))))};\n"
                        for n in range(frame_of(a_t), frame_of(b_t))), encoding="utf-8")
                    gs = g.label("ais")
                    g.add(f"[{keep}]sendcmd=f={cmd.name}[{gs}]")
                    g.add(f"[{gs}][{graded}]blend@{name}=all_mode=normal:all_opacity={_f(1 - op)}:"
                          f"enable='between(t,{_f(a_t - r0)},{_f(b_t - r0)})'[{out}]")
                else:
                    g.add(f"[{keep}][{graded}]blend=all_mode=normal:c0_opacity={_f(1 - op)}:c1_opacity={_f(1 - op)}:"
                          f"c2_opacity={_f(1 - op)}:enable='between(t,{_f(a_t - r0)},{_f(b_t - r0)})'[{out}]")
                v_base = out
                continue
            if not c.mediaId or clip_duration(c) <= EPS:
                continue
            m = metas[c.mediaId]
            if m["type"] == "audio":
                continue
            c_start, c_end = c.start, c.start + clip_duration(c)
            a_t, b_t = max(c_start, r0), min(c_end, r1)
            if b_t - a_t <= EPS:
                continue
            nf = frame_of(b_t) - frame_of(a_t)
            if nf <= 0:
                continue
            seg = nf / fps
            cd = c.model_dump()
            tau0 = a_t - c_start
            offset = frame_of(a_t) / fps
            hdr = TONEMAP + "," if m.get("hdr") else ""
            audible = m["type"] == "video" and m.get("hasAudio") and not t.muted and not c.muted and c.volume > 0
            raw, raw_a = LY.source_raw(g, cd, m, str(original_path(m)), tau0, nf, fps, frame_at_fn(c, tau0),
                                       want_audio=bool(audible), tonemap=hdr)
            src_w, src_h = int(m.get("width") or W), int(m.get("height") or H)
            fill = (c.fit or "fit") == "fill"
            rect = None
            if needs_grade(filt(c)):
                # Grade the overlay's own picture at its fitted size (scale 1), like the preview.
                x0, y0, cw, ch = LY.rounded_rect(src_w, src_h, (c.transform.crop.model_dump()), fill, W, H)
                f0 = min(W / cw, H / ch)
                gw, gh = max(2, LY.rnd(cw * f0 / 2) * 2), max(2, LY.rnd(ch * f0 / 2) * 2)
                pre_g = g.label("og")
                g.add(f"[{raw}]format=rgba,crop={cw}:{ch}:{x0}:{y0}:exact=1,scale={gw}:{gh},format=yuv420p[{pre_g}]")
                raw = grade_chain(g, pre_g, filt(c), gw, gh, rs=rs, fps=fps, t0=a_t, f0=int(round(a_t * fps)),
                                  duration=seg, workdir=workdir, cubes=cubes)
                src_w, src_h, rect = gw, gh, (0, 0, gw, gh)
            lay = LY.motion_layer(g, raw, cd, src_w, src_h, fill, W, H, tau0, fps, nf, workdir, rect=rect)
            ov = g.label("ov")
            g.add(f"[{lay}]setpts=PTS-STARTPTS+{_f(offset)}/TB[{ov}]")
            out = g.label("vb")
            g.add(f"[{v_base}][{ov}]overlay=0:0:enable='between(t,{_f(offset)},{_f(offset + seg)})':"
                  f"eof_action=pass:format=auto[{out}]")
            v_base = out
            # Overlay video audio is mixed in like an audio-track clip.
            if audible and raw_a:
                lbl = g.label("oa")
                g.add(f"[{raw_a}]volume=eval=frame:volume='{LY.gain_expr(cd, clip_duration(c), tau0, t.volume)}',"
                      f"adelay={int(round(offset * 1000))}:all=1[{lbl}]")
                extra.append(lbl)

    # Audio + music tracks, positioned with adelay and mixed over the main audio. Music tracks with
    # auto-duck lower under speech (main-track and audio-track clips with sound).
    speech = None
    for t in project.tracks:
        if t.type not in ("audio", "music") or t.muted:
            continue
        duck_cfg = getattr(t, "autoDuck", None) or {}
        regions = None
        if t.type == "music" and duck_cfg.get("enabled"):
            if speech is None:
                speech = speech_regions(project, metas)
            regions = speech
        for c in t.clips:
            if not c.mediaId or c.muted or c.volume <= 0 or clip_duration(c) <= EPS:
                continue
            m = metas[c.mediaId]
            if not m.get("hasAudio"):
                continue
            c_start, c_end = c.start, c.start + clip_duration(c)
            a_t, b_t = max(c_start, r0), min(c_end, r1)
            if b_t - a_t <= EPS:
                continue
            nf = max(1, frame_of(b_t) - frame_of(a_t))
            cd = c.model_dump()
            tau0 = a_t - c_start
            _, raw_a = LY.source_raw(g, cd, {**m, "type": "video"}, str(original_path(m)), tau0, nf, fps,
                                     frame_at_fn(c, tau0), want_audio=True, want_video=False) if m["type"] != "image" else (None, None)
            if not raw_a:
                continue
            lbl = g.label("ta")
            expr = LY.gain_expr(cd, clip_duration(c), tau0, t.volume, duck=regions,
                                duck_db=float(duck_cfg.get("amount", -12)), t_abs0=a_t)
            g.add(f"[{raw_a}]volume=eval=frame:volume='{expr}',adelay={int(round((a_t - r0) * 1000))}:all=1[{lbl}]")
            extra.append(lbl)

    if extra:
        mix_in = "[acat]" + "".join(f"[{e}]" for e in extra)
        g.add(f"{mix_in}amix=inputs={len(extra) + 1}:duration=first:dropout_transition=0:normalize=0,"
              f"alimiter=limit=0.98:attack=5:release=50[amix]")
        a_final = "amix"
    else:
        a_final = "acat"
    g.add(f"[{v_base}]format=yuv420p[vout]")
    g.add(f"[{a_final}]atrim=duration={_f(dur)},asetpts=PTS-STARTPTS[aout]")

    if mode == "draft" and has_nvenc():
        venc = ["-c:v", "h264_nvenc", "-preset", "p2", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    elif mode == "draft":
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    else:
        venc = ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-profile:v", "high"]

    script = ";\n".join(g.lines)
    args: list[str] = []
    for inp in g.inputs:
        args += inp
    args += ["-/filter_complex", "{SCRIPT}", "-map", "[vout]", "-map", "[aout]",
             *venc, "-pix_fmt", "yuv420p", "-r", _f(fps),
             "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
             "-t", _f(dur), "-movflags", "+faststart", str(out_path)]
    return Plan(args=args, filter_script=script, duration=dur, output=out_path, fps=fps, workdir=workdir)


_names_lock = threading.Lock()
_reserved: set[str] = set()


def speech_regions(project: Project, metas: dict) -> list[tuple[float, float]]:
    """Timeline regions with speech-like sound (main video and audio tracks), from the waveforms."""
    import json as _json
    sources = []
    for t in project.tracks:
        if t.muted or t.type not in ("video", "audio"):
            continue
        for c in t.clips:
            m = metas.get(c.mediaId or "")
            if not m or not m.get("hasAudio") or not m.get("waveform") or c.muted or c.volume <= 0 or m.get("sfx"):
                continue  # sound effects are not speech
            try:
                wf = _json.loads((CACHE_DIR / m["id"] / "waveform.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pk, pps = wf.get("peaks") or [], wf.get("peaksPerSecond", 100)
            cd = c.model_dump()
            D = clip_duration(c)
            n = int(D * 100)
            vals = []
            for i in range(n):
                src = MO.src_time(cd, i / 100)
                j = int(src * pps)
                vals.append((pk[j] / 255) if 0 <= j < len(pk) else 0.0)
            sources.append((c.start, c.start + D, vals, 0))
    return MO.duck_regions(sources)


def _export_name(project: Project, mode: str) -> Path:
    base = project.export.fileName.strip() or project.name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .") or "export"
    if base.lower().endswith(".mp4"):
        base = base[:-4]
    suffix = "_draft" if mode == "draft" else ""
    stem = f"{base}{suffix}_{time.strftime('%Y%m%d-%H%M%S')}"
    # Two exports in the same second must not overwrite each other (also reserve in-flight names).
    with _names_lock:
        n = 1
        while True:
            name = f"{stem}.mp4" if n == 1 else f"{stem}-{n}.mp4"
            path = EXPORTS_DIR / name
            if name not in _reserved and not path.exists() and not path.with_name(path.stem + ".part.mp4").exists():
                _reserved.add(name)
                return path
            n += 1


def sync_check(path: Path, expected: float, fps: float) -> dict:
    d = stream_durations(str(path))
    v, a = d.get("video", 0), d.get("audio", 0)
    tolerance = max(2 / fps, 0.07)
    ok = abs(v - a) <= tolerance and abs(v - expected) <= max(tolerance, 0.1)
    return {"ok": ok, "video": round(v, 3), "audio": round(a, 3), "expected": round(expected, 3),
            "message": "Audio and video are in sync." if ok else
            f"Audio/video length mismatch: video {v:.2f}s, audio {a:.2f}s, expected {expected:.2f}s."}


def run_export(project: Project, mode: str, range_start: float | None, range_end: float | None):
    out = _export_name(project, mode)
    workdir = CACHE_DIR / "_render" / uuid.uuid4().hex[:12]
    plan = build_plan(project, out, mode=mode, range_start=range_start, range_end=range_end, workdir=workdir)

    def job(ctx: JobContext) -> dict:
        rdir = workdir
        rdir.mkdir(parents=True, exist_ok=True)
        script_path = rdir / f"{ctx.job.id}.txt"
        script_path.write_text(plan.filter_script, encoding="utf-8")
        args = [str(script_path) if a == "{SCRIPT}" else a for a in plan.args]
        ctx.progress(0.0, "Rendering")
        tmp = plan.output.with_name(plan.output.stem + ".part.mp4")
        args[-1] = str(tmp)
        try:
            run_ffmpeg(args, plan.duration, lambda p: ctx.progress(p * 0.98, "Rendering"),
                       ctx.cancelled, ctx.set_process, cwd=str(rdir))
            tmp.replace(plan.output)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(rdir, ignore_errors=True)
        ctx.progress(0.99, "Checking audio sync")
        sync = sync_check(plan.output, plan.duration, plan.fps)
        ctx.message("Done" if sync["ok"] else "Done (sync warning)")
        return {"path": str(plan.output), "file": plan.output.name, "size": plan.output.stat().st_size,
                "duration": round(plan.duration, 3), "sync": sync,
                "url": f"/files/exports/{plan.output.name}"}

    return jobs.submit("export", f"Export {plan.output.name}", job, queue="render", ref={"projectId": project.id})


class ExportBody(BaseModel):
    project: dict | None = None  # unsaved project state from the editor (preferred)
    mode: str = "final"
    rangeStart: float | None = None
    rangeEnd: float | None = None


@router.post("/projects/{pid}/export")
def export_project(pid: str, body: ExportBody):
    project = Project.model_validate(body.project) if body.project else load_project(pid)
    if body.mode not in ("final", "draft"):
        raise HTTPException(400, "Export mode must be 'final' or 'draft'.")
    try:
        job = run_export(project, body.mode, body.rangeStart, body.rangeEnd)
    except RenderError as e:
        raise HTTPException(400, str(e)) from e
    except FFmpegError as e:
        raise HTTPException(500, str(e)) from e
    return {"jobId": job.id}


@router.post("/projects/{pid}/export/preview-graph")
def preview_graph(pid: str, body: ExportBody):
    """Debug helper: returns the ffmpeg arguments and filter graph without rendering."""
    project = Project.model_validate(body.project) if body.project else load_project(pid)
    try:
        plan = build_plan(project, EXPORTS_DIR / "preview.mp4", mode=body.mode,
                          range_start=body.rangeStart, range_end=body.rangeEnd)
    except RenderError as e:
        raise HTTPException(400, str(e)) from e
    return {"args": plan.args, "filter": plan.filter_script, "duration": plan.duration}
