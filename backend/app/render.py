"""Project JSON -> ffmpeg filter graph -> export.

Phase 1 scope: the main video track (clips + gaps, trimmed, fitted to the canvas with
fit / fill / blurred-background), per-clip volume and mute, and audio/music tracks mixed in.
Range export only renders the clips that overlap the range.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
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
from . import cutout as CU
from . import cutout_render as CR
from . import slowmo as SM
from . import layers as LY
from . import motion as MO
from . import text as TX
from .config import ROOT

EXPORT_PRESETS = {p["id"]: p for p in json.loads((ROOT / "shared" / "export_presets.json").read_text(encoding="utf-8"))["presets"]}
# Layers start on a whole output frame (timestamps in frames, never rounded seconds); the enable
# window opens a hair (half a millisecond) early so 6-decimal rounding can't skip that frame.
OFFSET_EPS = 0.0005
GRAIN_REDUCTION = 0.35  # "reduce grain for upload": grain effects at 35 % of their amount
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
    tasks: list = field(default_factory=list)  # (label, fn(workdir)) to run before ffmpeg (text frames)


def build_plan(project: Project, out_path: Path, *, mode: str = "final",
               range_start: float | None = None, range_end: float | None = None,
               workdir: Path | None = None) -> Plan:
    s = project.settings
    ex = project.export
    # Everything is composed on the project canvas (positions are project pixels) and scaled to the
    # export size at the end; a different aspect ratio is letterboxed.
    W, H = int(s.width), int(s.height)
    W, H = W - W % 2, H - H % 2
    OW, OH = int(ex.width or W), int(ex.height or H)
    OW, OH = OW - OW % 2, OH - OH % 2
    preset = EXPORT_PRESETS.get(ex.preset) or EXPORT_PRESETS["custom"]
    reduce_grain = preset.get("reduceGrain", False) if ex.reduceGrain is None else bool(ex.reduceGrain)
    bg = _color(s.background.color)
    default_fit = s.background.mode

    main = next((t for t in project.tracks if t.type == "video"), None)
    clips = sorted([c for c in (main.clips if main and not main.hidden else []) if clip_duration(c) > EPS],
                   key=lambda c: c.start)
    if not clips:
        raise RenderError("The main video track is empty. Add at least one clip to it before exporting.")

    metas: dict[str, dict] = {}
    gone: list[str] = []
    def cut_of(c: Clip) -> dict | None:
        cut = getattr(c, "cutout", None)
        return CU.normalize(cut) if CU.active(cut) and c.mediaId else None

    for t in project.tracks:
        for c in t.clips:
            bgm = ((cut_of(c) or {}).get("background") or {}).get("mediaId") if t.type in ("video", "overlay") else None
            for mid_ in (c.mediaId, bgm):
                if not mid_ or mid_ in metas:
                    continue
                m = load_meta(mid_)
                if not m:
                    gone.append(f"a file used on '{t.name or t.type}' at {c.start:.1f} s (its media folder was deleted)")
                    metas[mid_] = {}
                    continue
                if not original_path(m).exists():
                    gone.append(f"'{m['name']}'")
                metas[mid_] = m
    if gone:
        raise RenderError(f"{len(gone)} media file(s) are missing (moved or deleted): {', '.join(gone[:6])}"
                          f"{' and more' if len(gone) > 6 else ''}. In the Media panel, click 'Relink' on each missing file "
                          f"and choose where it is now, then export again.")

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
        out = [f.model_dump() for f in c.filters]
        if reduce_grain:
            for f in out:
                if f.get("type") == "effect" and f.get("name") == "grain":
                    f["params"] = {**(f.get("params") or {}), "amount": float((f.get("params") or {}).get("amount", 0)) * GRAIN_REDUCTION}
        return out

    from . import extend as EXT
    plan_tasks: list = []

    def ext_layers(c: Clip, m: dict, raw: str, cd: dict, src_w: int, src_h: int, fill: bool, tau0: float, nf: int,
                   rect=None) -> str:
        """Photo extension: the extension image, then the whole photo on top (one canvas-size layer)."""
        info = EXT.extended(m)
        r = rect or LY.rounded_rect(src_w, src_h, c.transform.crop.model_dump(), fill, W, H)
        fx, fy = info["fx"], info["fy"]
        ext_box = (-fx * src_w, -fy * src_h, src_w * (1 + 2 * fx), src_h * (1 + 2 * fy))
        idx = g.add_input(["-loop", "1", "-framerate", _f(fps), "-t", _f(nf / fps + 0.1), "-i", str(CACHE_DIR / m["id"] / info["file"])])
        ev = g.label("ex")
        g.add(f"[{idx}:v]setpts=PTS-STARTPTS,fps={_f(fps)},trim=end_frame={nf}[{ev}]")
        lay_e = LY.motion_layer(g, ev, cd, src_w, src_h, fill, W, H, tau0, fps, nf, workdir, rect=r, extent=ext_box,
                                img_map=(ext_box[0], ext_box[1], info["w"] / ext_box[2], info["h"] / ext_box[3]))
        lay_p = LY.motion_layer(g, raw, cd, src_w, src_h, fill, W, H, tau0, fps, nf, workdir, rect=r,
                                extent=(0, 0, src_w, src_h))
        out = g.label("xl")
        g.add(f"[{lay_e}][{lay_p}]overlay=0:0:format=auto:shortest=1[{out}]")
        return out

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
    segs_m: list[str] = []  # main-track canvas mattes, parallel to segs_v (only for text behind the subject)

    # ---- cutout (subject mattes from the AI analysis; see cutout.py / cutout_render.py)
    cx = CR.Ctx(g, W, H, fps, workdir, rs, cubes, CU.timeline_beats(project.model_dump(mode="json"), metas))
    cut_infos: dict[str, dict] = {}
    cut_missing: list[str] = []

    slow_infos: dict[str, dict | None] = {}
    slow_missing: list[str] = []

    def slow_info(c: Clip, before: float = 0.0, after: float = 0.0) -> dict | None:
        """A clip with Frame blending / AI smooth slow motion: its retimed video (made by a plan task if not
        cached) and the speed-1 clip that plays it (see slowmo.py)."""
        m = metas.get(c.mediaId or "") or {}
        cd = c.model_dump()
        if not SM.needs_retime(cd, m, fps):
            return None
        key = f"{c.id}|{before:.4f}|{after:.4f}"
        if key in slow_infos:
            return slow_infos[key]
        sp = SM.frame_plan(cd, m, c.start, fps, before, after)
        k = SM.plan_key(m, sp, "full")
        path = SM.slow_dir(m["id"]) / f"{k}_full.mp4"
        pseudo = {"id": SM.derived_id(m, k), "name": f"{m['name']} (slow motion)", "type": "video", "fps": fps,
                  "duration": len(sp["frames"]) / fps, "width": m.get("width"), "height": m.get("height"), "hasAudio": False,
                  "hdr": False, "file": f"cache/{m['id']}/slowmo/{k}_full.mp4", "derivedFrom": m["id"]}
        if not SM.lookup(m, sp, "full"):
            if sp["quality"] == "ai":
                slow_missing.append(f"'{m['name']}'")

            def task(wd, ctx=None, m=m, sp=sp):
                inf = SM.render(m, sp, "full", ctx)
                SM.derived_meta(m, sp, inf)
            task.wants_ctx = True  # type: ignore[attr-defined]
            plan_tasks.append((f"Slow motion: {m['name'][:24]}", task))
        info = {"path": path, "meta": pseudo, "cd": SM.retimed_clip(cd, sp, c.start)}
        slow_infos[key] = info
        return info

    def cut_info(c: Clip, before: float = 0.0, after: float = 0.0, mo: dict | None = None, cdo: dict | None = None) -> dict | None:
        """The clip's cutout settings + matte file (analysis queued as a plan task if not cached). mo/cdo: the
        retimed video and clip when the clip uses slow motion (the matte is made from those frames)."""
        cut = cut_of(c)
        m = mo or metas.get(c.mediaId or "") or {}
        if not cut or m.get("type") not in ("video", "image"):
            return None
        key = f"{c.id}|{before:.4f}|{after:.4f}|{m.get('id')}"
        if key in cut_infos:
            return cut_infos[key]
        span = None if m["type"] == "image" else CU.needed_span(cdo or c.model_dump(), before, after)
        info = CR.matte_info(m, cut, span)
        info["cut"] = cut
        if not info["ready"]:
            cut_missing.append(f"'{m['name']}'")
            label = f"Cutout: {m['name'][:28]}"

            def task(wd, ctx=None, m=m, cut=cut, span=span):
                CU.ensure(m, cut, span, ctx)
            task.wants_ctx = True  # type: ignore[attr-defined]
            plan_tasks.append((label, task))
        cut_infos[key] = info
        return info

    behind_any = any(t.type == "text" and not t.hidden and any((c.text or {}).get("behindSubject") for c in t.clips)
                     for t in project.tracks)
    need_mcat = behind_any and bool(main) and any(cut_of(c) for c in clips)

    def bg_source(mid_: str, tau0: float, nf: int) -> str:
        """A cutout background from another photo/video: cover-fitted to the canvas, playing from the clip's start."""
        bm = metas[mid_]
        if bm.get("type") == "image":
            raw_b, _ = LY.source_raw(g, {}, bm, str(original_path(bm)), 0.0, nf, fps, lambda tau: 0, want_audio=False)
        else:
            cdb = {"inPoint": 0.0, "outPoint": float(bm.get("duration") or 0), "speed": 1}
            raw_b, _ = LY.source_raw(g, cdb, bm, str(original_path(bm)), tau0, nf, fps,
                                     lambda tau, t0=tau0: int(round((tau - t0) * fps)), want_audio=False,
                                     tonemap=TONEMAP + "," if bm.get("hdr") else "")
        fitted_b = fit_chain(g, raw_b, "fill", W, H, bg)
        out_b = g.label("cbg")
        g.add(f"[{fitted_b}]format=yuv420p,setsar=1[{out_b}]")
        return out_b
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
        if need_mcat:
            segs_m.append(CR.black_matte(cx, nf))
        v, a = g.label("gv"), g.label("ga")
        g.add(f"color=c={bg}:s={W}x{H}:r={_f(fps)}:d={_f(nf / fps + 0.1)},format=yuv420p,setsar=1,"
              f"trim=end_frame={nf}[{v}]")
        g.add(f"anullsrc=r=48000:cl=stereo,atrim=duration={_f(nf / fps)}[{a}]")
        segs_v.append(v)
        segs_a.append(a)

    def layer_start(frame: int) -> str:
        """Timestamps for a layer whose first frame belongs on output frame `frame`: in units of
        1/(1000 fps), 2 ms early, so it is never held back a frame by the base video's rounding."""
        return f"settb=1/{_f(fps * 1000)},setpts=PTS-STARTPTS+{frame * 1000 - int(fps * 2)}"

    def frame_at_fn(c: Clip, tau0: float):
        base = frame_of(c.start + tau0)
        return lambda tau: frame_of(c.start + tau) - base

    def clip_source(c: Clip, t_from: float, nf: int) -> tuple[str, str, str | None]:
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
        h_in, h_out = incoming.get(c.id, 0.0), plans[c.id].half if c.id in plans else 0.0
        si = slow_info(c, h_in, h_out)
        if si:
            # slow motion with blending / AI frames: the retimed video plays at 1x; the sound is the original's
            raw, _ = LY.source_raw(g, si["cd"], si["meta"], str(si["path"]), tau0, nf, fps, frame_at_fn(c, tau0), want_audio=False)
            _, raw_a = LY.source_raw(g, cd, m, str(original_path(m)), tau0, nf, fps, frame_at_fn(c, tau0),
                                     want_audio=bool(audible), want_video=False) if audible else (None, None)
        else:
            raw, raw_a = LY.source_raw(g, cd, m, str(original_path(m)), tau0, nf, fps, frame_at_fn(c, tau0),
                                       want_audio=bool(audible), tonemap=hdr)
        mode = c.fit or default_fit
        extend = bool(c.extend) and m["type"] == "image"
        ci = cut_info(c, h_in, h_out, si["meta"] if si else None, si["cd"] if si else None)
        M = None
        if ci:
            # the picture and its matte take the same path (layers.motion_layer): they line up exactly
            src_w, src_h = int(m.get("width") or W), int(m.get("height") or H)
            mraw = CR.matte_raw(cx, si["cd"] if si else cd, ci, tau0, nf, frame_at_fn(c, tau0))
            M = CR.canvas_matte(cx, mraw, cd, src_w, src_h, mode == "fill", tau0, nf, extend=extend)
        if MO.has_motion(cd) or extend or ci:
            src_w, src_h = int(m.get("width") or W), int(m.get("height") or H)
            if mode == "blur":
                sp_a, sp_b = g.label("sa"), g.label("sb")
                g.add(f"[{raw}]split[{sp_a}][{sp_b}]")
                back = blur_backdrop(g, sp_a, W, H)
                raw = sp_b
            else:
                back = g.label("bk")
                g.add(f"color=c={bg}:s={W}x{H}:r={_f(fps)}:d={_f(seg + 0.1)},format=yuv420p,setsar=1[{back}]")
            if extend:
                lay = ext_layers(c, m, raw, cd, src_w, src_h, mode == "fill", tau0, nf)
            else:
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
        m_out = None
        if ci:
            if need_mcat:
                M, m_keep = cx.split(M, 2, "cmo")
                m_out = g.label("cmt")
                g.add(f"[{m_keep}]tpad=stop_mode=clone:stop_duration={_f(seg + 0.1)},trim=end_frame={nf},setpts=PTS-STARTPTS[{m_out}]")
            fitted = CR.composite(cx, fitted, M, ci["cut"], nf, tau0, t_from, bg_source)
        g.add(f"[{fitted}]scale=out_range=tv,format=yuv420p,tpad=stop_mode=clone:stop_duration={_f(seg + 0.1)},"
              f"trim=end_frame={nf},setpts=PTS-STARTPTS[{vout}]")
        aout = g.label("ca")
        if audible and raw_a:
            g.add(f"[{raw_a}]volume=eval=frame:volume='{LY.gain_expr(cd, D, tau0, main_vol)}',"
                  f"apad=whole_dur={_f(seg)},atrim=duration={_f(seg)},asetpts=PTS-STARTPTS[{aout}]")
        else:
            g.add(f"anullsrc=r=48000:cl=stereo,atrim=duration={_f(seg)}[{aout}]")
        return vout, aout, m_out

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
        va, aa, ma = clip_source(by_id[p.from_id], p.start, nf)
        vb, ab, mb = clip_source(by_id[p.to_id], p.start, nf)
        D = nf / fps
        if need_mcat:
            ma, mb = ma or CR.black_matte(cx, nf), mb or CR.black_matte(cx, nf)
            mx, mseg = g.label("tmx"), g.label("tms")
            if p.type in WHIP_TYPES:
                whip_chain(g, ma, mb, mx, p.type, D, W, H, fps)
            else:
                r1_, r2_ = g.label("tma"), g.label("tmb")
                g.add(f"[{ma}]{TO_GBRP}[{r1_}]")
                g.add(f"[{mb}]{TO_GBRP}[{r2_}]")
                g.add(f"[{r1_}][{r2_}]xfade=transition={p.type}:duration={_f(D)}:offset=0[{mx}]")
            g.add(f"[{mx}]format=gray,trim=start_frame={lo - fa}:end_frame={hi - fa},setpts=PTS-STARTPTS[{mseg}]")
            segs_m.append(mseg)
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
                    v, a, mm_ = clip_source(c, a_t, nf)
                    segs_v.append(v)
                    segs_a.append(a)
                    if need_mcat:
                        segs_m.append(mm_ or CR.black_matte(cx, nf))
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
    mcat: list[str] = []
    if need_mcat:
        assert len(segs_m) == n, (len(segs_m), n)
        g.add("".join(f"[{x}]" for x in segs_m) + f"concat=n={n}:v=1:a=0,format=gray[mcat]")
        n_behind = sum(1 for t in project.tracks if t.type == "text" and not t.hidden
                       and any((c.text or {}).get("behindSubject") for c in t.clips))
        mcat = cx.split("mcat", n_behind, "mct")

    # Overlay tracks: composited over the main track in track order (later tracks on top).
    v_base = "vcat"
    extra: list[str] = []
    text_n = 0
    for t in project.tracks:
        if t.hidden or t.type not in ("overlay", "text"):
            continue
        order = sorted(t.clips, key=lambda c: c.start)
        subject_pass = None
        if t.type == "text" and need_mcat and mcat and any((c.text or {}).get("behindSubject") for c in order):
            behind = [c for c in order if (c.text or {}).get("behindSubject")]
            order = behind + [None] + [c for c in order if not (c.text or {}).get("behindSubject")]
            keep_, pic_ = g.label("sbk"), g.label("sbp")
            g.add(f"[{v_base}]split[{keep_}][{pic_}]")
            v_base = keep_
            wins = "+".join(f"between(t,{_f(max(c.start, r0) - r0 - OFFSET_EPS)},{_f(min(c.start + clip_duration(c), r1) - r0)})"
                            for c in behind)
            subject_pass = (pic_, mcat.pop(0), wins)
        for c in order:
            if c is None:  # the subject, back on top of the text behind it
                pic_, mm_, wins = subject_pass
                sub_, out = g.label("sbs"), g.label("vb")
                g.add(f"[{pic_}][{mm_}]alphamerge[{sub_}]")
                g.add(f"[{v_base}][{sub_}]overlay=0:0:format=auto:enable='{wins}'[{out}]")
                v_base = out
                continue
            if t.type == "text":
                if not c.text or clip_duration(c) <= EPS:
                    continue
                a_t, b_t = max(c.start, r0), min(c.start + clip_duration(c), r1)
                nf = frame_of(b_t) - frame_of(a_t)
                if b_t - a_t <= EPS or nf <= 0:
                    continue
                cd = c.model_dump()
                # clip time of the first output frame (frames sit on the absolute grid, like the preview)
                tau0 = frame_of(a_t) / fps + r0 - c.start
                prep = TX.prepare_clip(cd, tau0, nf, fps, W, H)
                if prep is None:
                    continue
                prefix = f"txt{text_n}"
                text_n += 1
                plan_tasks.append((f"Text: {str(c.text.get('content', ''))[:24]}", lambda wd, prep=prep, prefix=prefix: TX.write_frames(prep, wd, prefix)))
                offset = frame_of(a_t) / fps
                idx = g.add_input(["-framerate", _f(fps), "-start_number", "0", "-i", f"{prefix}_%05d.png"])
                ov, out = g.label("tx"), g.label("vb")
                g.add(f"[{idx}:v]format=rgba,{layer_start(frame_of(a_t))}[{ov}]")
                g.add(f"[{v_base}][{ov}]overlay={prep['x']}:{prep['y']}:enable='between(t,{_f(offset - OFFSET_EPS)},{_f(offset + nf / fps - OFFSET_EPS)})':"
                      f"eof_action=pass:format=auto[{out}]")
                v_base = out
                continue
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
            si = slow_info(c)
            if si:
                raw, _ = LY.source_raw(g, si["cd"], si["meta"], str(si["path"]), tau0, nf, fps, frame_at_fn(c, tau0), want_audio=False)
                _, raw_a = LY.source_raw(g, cd, m, str(original_path(m)), tau0, nf, fps, frame_at_fn(c, tau0),
                                         want_audio=bool(audible), want_video=False) if audible else (None, None)
            else:
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
            ext_on = bool(c.extend and m["type"] == "image" and rect is None)
            if ext_on:
                lay = ext_layers(c, m, raw, cd, src_w, src_h, fill, tau0, nf)
            else:
                lay = LY.motion_layer(g, raw, cd, src_w, src_h, fill, W, H, tau0, fps, nf, workdir, rect=rect)
            layers_ = [lay]
            ci = cut_info(c, mo=si["meta"] if si else None, cdo=si["cd"] if si else None)
            if ci:
                mraw = CR.matte_raw(cx, si["cd"] if si else cd, ci, tau0, nf, frame_at_fn(c, tau0))
                if rect is not None:  # graded at its fitted size: the matte gets the same crop + scale
                    x0_, y0_, cw_, ch_ = LY.rounded_rect(int(m.get("width") or W), int(m.get("height") or H),
                                                         c.transform.crop.model_dump(), fill, W, H)
                    mr2 = g.label("mrc")
                    g.add(f"[{mraw}]crop={cw_}:{ch_}:{x0_}:{y0_}:exact=1,scale={src_w}:{src_h},format=gray[{mr2}]")
                    mraw = mr2
                M = CR.canvas_matte(cx, mraw, cd, src_w, src_h, fill, tau0, nf, rect=rect, extend=ext_on)
                under, lay = CR.overlay_layers(cx, lay, M, ci["cut"], nf, a_t)
                layers_ = under + [lay]
            for lay_ in layers_:
                ov = g.label("ov")
                g.add(f"[{lay_}]{layer_start(frame_of(a_t))}[{ov}]")
                out = g.label("vb")
                g.add(f"[{v_base}][{ov}]overlay=0:0:enable='between(t,{_f(offset - OFFSET_EPS)},{_f(offset + seg - OFFSET_EPS)})':"
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
    if (OW, OH) != (W, H):
        g.add(f"[{v_base}]scale={OW}:{OH}:force_original_aspect_ratio=decrease:flags=lanczos,"
              f"pad={OW}:{OH}:(ow-iw)/2:(oh-ih)/2:color={bg},setsar=1,format=yuv420p[vout]")
    else:
        g.add(f"[{v_base}]format=yuv420p[vout]")
    g.add(f"[{a_final}]atrim=duration={_f(dur)},asetpts=PTS-STARTPTS[aout]")

    if cut_missing or slow_missing:
        from .ai_models import ai_python
        if ai_python() is None:
            if slow_missing:
                raise RenderError(f"AI smooth slow motion is set for {', '.join(slow_missing[:3])}, but the AI tools are not "
                                  "installed. Run setup.ps1 and restart Ligxt, or set Slow-mo quality to Frame blending.")
            raise RenderError(CR.needs_analysis_message(cut_missing))
    venc, abr = encoder_args(ex, preset, mode, fps)

    script = ";\n".join(g.lines)
    args: list[str] = []
    for inp in g.inputs:
        args += inp
    args += ["-/filter_complex", "{SCRIPT}", "-map", "[vout]", "-map", "[aout]",
             *venc, "-pix_fmt", "yuv420p", "-r", _f(fps),
             "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
             "-c:a", "aac", "-b:a", f"{abr}k", "-ar", "48000", "-ac", "2",
             "-t", _f(dur), "-movflags", "+faststart", str(out_path)]
    return Plan(args=args, filter_script=script, duration=dur, output=out_path, fps=fps, workdir=workdir, tasks=plan_tasks)


def video_quality(ex, preset: dict) -> dict:
    v = dict(preset.get("video") or {"kind": "crf", "crf": 18})
    if ex.video:
        v.update(ex.video)
    return v


def encoder_args(ex, preset: dict, mode: str, fps: float) -> tuple[list[str], int]:
    """Video encoder arguments and the audio bitrate (kbps) for the export settings."""
    abr = int(ex.audioKbps or preset.get("audioKbps") or 192)
    if mode == "draft":
        if has_nvenc():
            return ["-c:v", "h264_nvenc", "-preset", "p2", "-rc", "vbr", "-cq", "23", "-b:v", "0"], abr
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"], abr
    v = video_quality(ex, preset)
    args = ["-c:v", "libx264", "-preset", str(v.get("preset", "slow")), "-profile:v", str(v.get("profile", "high"))]
    if v.get("level"):
        args += ["-level:v", str(v["level"])]
    if v.get("kind") == "bitrate":
        kb = int(v.get("kbps", 12000))
        args += ["-b:v", f"{kb}k", "-maxrate", f"{int(v.get('maxKbps', kb * 4 // 3))}k",
                 "-bufsize", f"{int(v.get('bufKbps', kb * 2))}k"]
    else:
        args += ["-crf", _f(float(v.get("crf", 18)))]
    if v.get("gop"):
        args += ["-g", str(int(v["gop"])), "-keyint_min", str(max(1, int(v["gop"]) // 2))]
    return args, abr


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


class _TaskProgress:
    """A plan task's view of the export job: its progress fills its share of the first half of the bar."""

    def __init__(self, ctx: JobContext, label: str, i: int, n: int, span: float):
        self._ctx, self._label, self._i, self._n, self._span = ctx, label, i, n, span
        self.job = ctx.job

    def progress(self, p: float, message: str | None = None) -> None:
        self._ctx.progress(self._span * (self._i + max(0.0, min(1.0, p))) / self._n,
                           f"{self._label} ({self._i + 1}/{self._n})" + (f": {message}" if message else ""))

    def message(self, msg: str) -> None:
        self._ctx.message(f"{self._label}: {msg}")

    def cancelled(self) -> bool:
        return self._ctx.cancelled()

    def set_process(self, proc) -> None:
        self._ctx.set_process(proc)


def heavy_tasks(plan: Plan) -> bool:
    """Plan tasks that take real time (AI analysis): they get the first half of the progress bar."""
    return any(getattr(fn, "wants_ctx", False) for _, fn in plan.tasks)


def run_tasks(plan: Plan, rdir: Path, ctx: JobContext | None = None) -> None:
    """Pre-render steps of a plan (text frames, cutout analysis), in the job so the request returns at once."""
    heavy = heavy_tasks(plan)
    for i, (label, fn) in enumerate(plan.tasks):
        if ctx:
            if ctx.cancelled():
                raise RenderError("Cancelled")
            ctx.progress(0.5 * i / len(plan.tasks) if heavy else 0.0, f"Preparing {label} ({i + 1}/{len(plan.tasks)})")
        if getattr(fn, "wants_ctx", False):
            fn(rdir, _TaskProgress(ctx, label, i, len(plan.tasks), 0.5) if ctx else None)
        else:
            fn(rdir)


def export_size_bound(project: Project, mode: str, duration: float) -> int:
    """A generous upper bound for the export's size (for the disk space check)."""
    preset = EXPORT_PRESETS.get(project.export.preset) or EXPORT_PRESETS["custom"]
    v = video_quality(project.export, preset)
    w, h = int(project.export.width or project.settings.width), int(project.export.height or project.settings.height)
    px = max(1.0, w * h / (1080 * 1920))
    if mode == "draft":
        kbps = 15000 * px
    elif v.get("kind") == "bitrate":
        kbps = float(v.get("maxKbps") or v.get("kbps", 12000))
    else:
        kbps = 60000 * px  # CRF 18 with heavy film grain reached ~48 Mbps at 1080x1920
    return int((kbps + 320) * 1000 / 8 * duration * 1.2)


def run_export(project: Project, mode: str, range_start: float | None, range_end: float | None):
    from .system_api import record_export, require_space
    out = _export_name(project, mode)
    workdir = CACHE_DIR / "_render" / uuid.uuid4().hex[:12]
    plan = build_plan(project, out, mode=mode, range_start=range_start, range_end=range_end, workdir=workdir)
    require_space(config.EXPORTS_DIR, export_size_bound(project, mode, plan.duration), "this export")

    def job(ctx: JobContext) -> dict:
        rdir = workdir
        rdir.mkdir(parents=True, exist_ok=True)
        script_path = rdir / f"{ctx.job.id}.txt"
        script_path.write_text(plan.filter_script, encoding="utf-8")
        run_tasks(plan, rdir, ctx)
        args = [str(script_path) if a == "{SCRIPT}" else a for a in plan.args]
        r0_ = 0.5 if heavy_tasks(plan) else 0.0
        ctx.progress(r0_, "Rendering")
        tmp = plan.output.with_name(plan.output.stem + ".part.mp4")
        args[-1] = str(tmp)
        try:
            run_ffmpeg(args, plan.duration, lambda p: ctx.progress(r0_ + p * (0.98 - r0_), "Rendering"),
                       ctx.cancelled, ctx.set_process, cwd=str(rdir))
            tmp.replace(plan.output)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(rdir, ignore_errors=True)
        ctx.progress(0.99, "Checking audio sync")
        sync = sync_check(plan.output, plan.duration, plan.fps)
        try:
            record_export(plan.output, project, mode, range_start, range_end)
        except OSError:
            pass
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


# ---------------------------------------------------------------- size estimate, cover image
def _render_range(project: Project, start: float, end: float, out: Path, ctx: JobContext | None = None) -> Plan:
    workdir = CACHE_DIR / "_render" / uuid.uuid4().hex[:12]
    plan = build_plan(project, out, mode="final", range_start=start, range_end=end, workdir=workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        (workdir / "graph.txt").write_text(plan.filter_script, encoding="utf-8")
        run_tasks(plan, workdir, None)
        args = [str(workdir / "graph.txt") if a == "{SCRIPT}" else a for a in plan.args]
        run_ffmpeg(args, plan.duration, lambda p: None, (ctx.cancelled if ctx else (lambda: False)),
                   (ctx.set_process if ctx else (lambda p: None)), cwd=str(workdir))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return plan


def estimate_size(project: Project, ctx: JobContext | None = None) -> dict:
    """Expected file size of a final export: exact-ish for bitrate presets, else from 3 one-second samples."""
    ex = project.export
    preset = EXPORT_PRESETS.get(ex.preset) or EXPORT_PRESETS["custom"]
    total = project_duration(project)
    r = ex.range
    dur = (min(total, r.end) - max(0.0, r.start)) if r else total
    v = video_quality(ex, preset)
    abr = int(ex.audioKbps or preset.get("audioKbps") or 192)
    if v.get("kind") == "bitrate":
        return {"bytes": int((int(v.get("kbps", 12000)) + abr) * 1000 / 8 * dur * 1.01), "method": "bitrate", "duration": round(dur, 3)}
    s0 = max(0.0, r.start) if r else 0.0
    n = 3 if dur > 3 else 1
    seg = min(1.0, dur / n)
    sizes = []
    tmpdir = CACHE_DIR / "_render" / f"est_{uuid.uuid4().hex[:8]}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        for i in range(n):
            a = s0 + (dur - seg) * ((i + 0.5) / n)
            out = tmpdir / f"s{i}.mp4"
            if ctx:
                ctx.progress(i / n, f"Sampling {i + 1}/{n}")
            _render_range(project, a, a + seg, out, ctx)
            sizes.append(out.stat().st_size)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    per_sec = sum(sizes) / (seg * n)
    return {"bytes": int(per_sec * dur), "method": "sampled", "duration": round(dur, 3), "samples": n}


def project_duration(project: Project) -> float:
    main = next((t for t in project.tracks if t.type == "video"), None)
    ends = [c.start + clip_duration(c) for c in (main.clips if main else [])]
    return max(ends) if ends else 0.0


@router.post("/projects/{pid}/export/estimate")
def export_estimate(pid: str, body: ExportBody):
    project = Project.model_validate(body.project) if body.project else load_project(pid)

    def job(ctx: JobContext) -> dict:
        try:
            return estimate_size(project, ctx)
        except RenderError as e:
            raise RuntimeError(str(e)) from e

    return {"jobId": jobs.submit("estimate", "Estimating file size", job, queue="render", ref={"projectId": project.id}).id}


class CoverBody(BaseModel):
    project: dict | None = None
    time: float = 0
    text: dict | None = None  # a clip-like dict: {"text": spec, "transform": {...}} (drawn at rest)
    hideText: bool = False  # leave the timeline's own text out of the frame


def export_cover(project: Project, t: float, text: dict | None, hide_text: bool, ctx: JobContext | None = None) -> dict:
    from PIL import Image
    p = project.model_copy(deep=True)
    if hide_text:
        for tr in p.tracks:
            if tr.type == "text":
                tr.hidden = True
    W, H = p.settings.width - p.settings.width % 2, p.settings.height - p.settings.height % 2
    p.export.width, p.export.height = W, H
    total = project_duration(p)
    t = max(0.0, min(t, total - 0.1))
    fps = float(p.export.fps if p.export.fps != "source" else p.settings.fps)
    tmpdir = CACHE_DIR / "_render" / f"cov_{uuid.uuid4().hex[:8]}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        clip_path = tmpdir / "c.mp4"
        _render_range(p, t, t + 3 / fps, clip_path, ctx)
        png = tmpdir / "f.png"
        subprocess.run([config.FFMPEG, "-v", "error", "-y", "-i", str(clip_path), "-frames:v", "1", str(png)], check=True, capture_output=True)
        img = Image.open(png).convert("RGBA")
        if text and (text.get("text") or {}).get("content"):
            clip = {"start": 0, "inPoint": 0, "outPoint": 10, "speed": 1, "keyframes": {},
                    "transform": {"x": 0, "y": 0, "scale": 1, "rotation": 0, "opacity": 1, **(text.get("transform") or {})},
                    "text": {**text["text"], "animIn": {"type": "none"}, "animOut": {"type": "none"}}}
            lay = TX.build(clip["text"])
            ops = TX.text_ops(lay, clip, 5.0, W, H)
            box = TX._bbox(ops, W, H)
            if box:
                region = TX.Composer(lay).compose(ops, box)
                img.alpha_composite(Image.fromarray(region, "RGBA"), (box[0], box[1]))
        base = re.sub(r'[<>:"/\|?*\x00-\x1f]', "_", (p.export.fileName.strip() or p.name)).strip(" .") or "cover"
        n, out = 1, EXPORTS_DIR / f"{base} cover.jpg"
        while out.exists():
            n += 1
            out = EXPORTS_DIR / f"{base} cover {n}.jpg"
        img.convert("RGB").save(out, quality=93)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {"path": str(out), "file": out.name, "url": f"/files/exports/{out.name}", "size": out.stat().st_size}


@router.post("/projects/{pid}/cover")
def cover(pid: str, body: CoverBody):
    project = Project.model_validate(body.project) if body.project else load_project(pid)

    def job(ctx: JobContext) -> dict:
        return export_cover(project, body.time, body.text, body.hideText, ctx)

    return {"jobId": jobs.submit("cover", "Cover image", job, queue="render", ref={"projectId": project.id}).id}
