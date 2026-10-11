"""Project JSON -> ffmpeg filter graph -> export.

Phase 1 scope: the main video track (clips + gaps, trimmed, fitted to the canvas with
fit / fill / blurred-background), per-clip volume and mute, and audio/music tracks mixed in.
Range export only renders the clips that overlap the range.
"""
from __future__ import annotations

import math
import re
import time
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
    return max(0.0, (c.outPoint - c.inPoint) / (c.speed or 1))


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


def build_plan(project: Project, out_path: Path, *, mode: str = "final",
               range_start: float | None = None, range_end: float | None = None) -> Plan:
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

    def clip_source(c: Clip, t_from: float, nf: int) -> tuple[str, str]:
        """Video + audio labels for clip `c` from timeline time `t_from`, exactly `nf` frames long.
        Reaches past the clip's in/out points (transition handles); where the source has no footage
        the first/last frame is held and the audio is silent."""
        m = metas[c.mediaId]
        seg = nf / fps
        speed = c.speed or 1
        src = c.inPoint + (t_from - c.start) * speed
        lead = 0.0
        if m["type"] != "image":
            if src < 0:
                lead, src = -src / speed, 0.0
            mfps = m.get("fps") or fps
            last = max(0.0, (m.get("duration") or 0) - 1.5 / mfps)
            if src > last:  # entirely past the end: start at the last frame and hold it
                src = last
        path = str(original_path(m))
        if m["type"] == "image":
            idx = g.add_input(["-loop", "1", "-framerate", _f(fps), "-t", _f(seg + 0.1), "-i", path])
        else:
            idx = g.add_input(["-ss", _f(src), "-t", _f(max(0.1, (seg - lead) * speed + 0.1)), "-i", path])
        vin = g.label("v")
        pre = f"setpts=(PTS-STARTPTS)/{_f(speed)}," if abs(speed - 1) > EPS else "setpts=PTS-STARTPTS,"
        hdr = TONEMAP + "," if m.get("hdr") else ""
        g.add(f"[{idx}:v]{hdr}{pre}fps={_f(fps)}[{vin}]")
        fitted = fit_chain(g, vin, c.fit or default_fit, W, H, bg)
        vout = g.label("cv")
        pad = f"tpad=start_mode=clone:start_duration={_f(lead)}:stop_mode=clone:stop_duration={_f(seg + 0.1)}"
        g.add(f"[{fitted}]scale=out_range=tv,format=yuv420p,{pad},trim=end_frame={nf},setpts=PTS-STARTPTS[{vout}]")
        aout = g.label("ca")
        if m.get("hasAudio") and m["type"] == "video" and not c.muted and not main_muted and c.volume > 0:
            delay = f"adelay={int(round(lead * 1000))}:all=1," if lead > 0 else ""
            g.add(f"[{idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo{atempo(speed)},"
                  f"volume={_f(c.volume * main_vol)},{delay}apad=whole_dur={_f(seg)},atrim=duration={_f(seg)},"
                  f"asetpts=PTS-STARTPTS[{aout}]")
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
            g.add(f"[{va}]format=gbrp[{ra}]")
            g.add(f"[{vb}]format=gbrp[{rb}]")
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
            if not c.mediaId or clip_duration(c) <= EPS:
                continue
            m = metas[c.mediaId]
            if m["type"] == "audio":
                continue
            c_start, c_end = c.start, c.start + clip_duration(c)
            a_t, b_t = max(c_start, r0), min(c_end, r1)
            if b_t - a_t <= EPS:
                continue
            seg = b_t - a_t
            speed = c.speed or 1
            src_in = c.inPoint + (a_t - c_start) * speed
            path = str(original_path(m))
            if m["type"] == "image":
                idx = g.add_input(["-loop", "1", "-framerate", _f(fps), "-t", _f(seg + 0.1), "-i", path])
            else:
                idx = g.add_input(["-ss", _f(src_in), "-t", _f(seg * speed + 0.05), "-i", path])
            geo = overlay_geometry(m, c, W, H)
            offset = a_t - r0
            ov = g.label("ov")
            pre = f"setpts=(PTS-STARTPTS)/{_f(speed)}," if abs(speed - 1) > EPS else "setpts=PTS-STARTPTS,"
            hdr = TONEMAP + "," if m.get("hdr") else ""
            chain = [f"[{idx}:v]{hdr}{pre}fps={_f(fps)}", f"scale={geo['sw']}:{geo['sh']}"]
            if geo["crop"]:
                chain.append(f"crop={geo['cw']}:{geo['ch']}")
            chain.append("format=rgba")
            if c.transform.flipH:
                chain.append("hflip")
            if c.transform.flipV:
                chain.append("vflip")
            if abs(c.transform.rotation) > EPS:
                a = f"{_f(c.transform.rotation)}*PI/180"
                chain.append(f"rotate={a}:ow=rotw({a}):oh=roth({a}):c=none")
            if c.transform.opacity < 1 - EPS:
                chain.append(f"colorchannelmixer=aa={_f(max(0.0, c.transform.opacity))}")
            chain.append(f"trim=duration={_f(seg)},setpts=PTS-STARTPTS+{_f(offset)}/TB[{ov}]")
            g.add(",".join(chain))
            out = g.label("vb")
            g.add(f"[{v_base}][{ov}]overlay=x=(W-w)/2+({_f(c.transform.x)}):y=(H-h)/2+({_f(c.transform.y)}):"
                  f"enable='between(t,{_f(offset)},{_f(offset + seg)})':eof_action=pass:format=auto[{out}]")
            v_base = out
            # Overlay video audio is mixed in like an audio-track clip.
            if m["type"] == "video" and m.get("hasAudio") and not t.muted and not c.muted and c.volume > 0:
                lbl = g.label("oa")
                g.add(f"[{idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo{atempo(speed)},"
                      f"atrim=duration={_f(seg)},asetpts=PTS-STARTPTS,volume={_f(c.volume * t.volume)},"
                      f"adelay={int(round(offset * 1000))}:all=1[{lbl}]")
                extra.append(lbl)

    # Audio + music tracks, positioned with adelay and mixed over the main audio.
    for t in project.tracks:
        if t.type not in ("audio", "music") or t.muted:
            continue
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
            speed = c.speed or 1
            src_in = c.inPoint + (a_t - c_start) * speed
            idx = g.add_input(["-ss", _f(src_in), "-t", _f((b_t - a_t) * speed + 0.05), "-i", str(original_path(m))])
            lbl = g.label("ta")
            delay_ms = int(round((a_t - r0) * 1000))
            fades = ""
            if c.fadeIn > 0 and a_t - c_start < c.fadeIn:
                fades += f",afade=t=in:st=0:d={_f(c.fadeIn - (a_t - c_start))}"
            if c.fadeOut > 0 and c_end - b_t < c.fadeOut:
                fo = c.fadeOut - (c_end - b_t)
                fades += f",afade=t=out:st={_f(max(0.0, (b_t - a_t) - fo))}:d={_f(fo)}"
            g.add(f"[{idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo{atempo(speed)},"
                  f"atrim=duration={_f(b_t - a_t)},asetpts=PTS-STARTPTS,volume={_f(c.volume * t.volume)}{fades},"
                  f"adelay={delay_ms}:all=1[{lbl}]")
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
    return Plan(args=args, filter_script=script, duration=dur, output=out_path, fps=fps)


def _export_name(project: Project, mode: str) -> Path:
    base = project.export.fileName.strip() or project.name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .") or "export"
    if base.lower().endswith(".mp4"):
        base = base[:-4]
    suffix = "_draft" if mode == "draft" else ""
    return EXPORTS_DIR / f"{base}{suffix}_{time.strftime('%Y%m%d-%H%M%S')}.mp4"


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
    plan = build_plan(project, out, mode=mode, range_start=range_start, range_end=range_end)

    def job(ctx: JobContext) -> dict:
        rdir = CACHE_DIR / "_render"
        rdir.mkdir(parents=True, exist_ok=True)
        script_path = rdir / f"{ctx.job.id}.txt"
        script_path.write_text(plan.filter_script, encoding="utf-8")
        args = [str(script_path) if a == "{SCRIPT}" else a for a in plan.args]
        ctx.progress(0.0, "Rendering")
        tmp = plan.output.with_name(plan.output.stem + ".part.mp4")
        args[-1] = str(tmp)
        try:
            run_ffmpeg(args, plan.duration, lambda p: ctx.progress(p * 0.98, "Rendering"),
                       ctx.cancelled, ctx.set_process)
            tmp.replace(plan.output)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            script_path.unlink(missing_ok=True)
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
