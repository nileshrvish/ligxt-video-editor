"""Export graph pieces for cutout clips (the preview mirrors these in frontend/src/player/cutout.ts).

A cutout clip is composed on the canvas from:
  F  the clip's picture as it would normally look (fitted/moved, whole-clip grade applied)
  M  the canvas matte: the refined matte through the same timing and transform as the picture (gray)
in this order (later on top):
  background  keep (F) | blur (F blurred) | color | image | video, then the background-only grade
  shadow      M shifted and blurred, coloured
  glow        M blurred, levels (reach/softness), coloured; optionally pulsing on the beat
  subject     F with the subject-only grade, alpha = M
"""
from __future__ import annotations

import math

from . import cutout as CU
from . import layers as LY
from .grade import grade_chain, needs_grade


def _f(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


def _color(c: str) -> str:
    c = (c or "#000000").lstrip("#")
    return "0x" + (c[:6] if len(c) >= 6 and all(ch in "0123456789abcdefABCDEF" for ch in c[:6]) else "000000")


class Ctx:
    """Everything the helpers need from build_plan."""

    def __init__(self, g, W: int, H: int, fps: float, workdir, rs: float, cubes: dict, beats: list[float]):
        self.g, self.W, self.H, self.fps, self.workdir, self.rs, self.cubes, self.beats = g, W, H, fps, workdir, rs, cubes, beats

    def split(self, src: str, n: int, prefix: str = "cs") -> list[str]:
        if n <= 1:
            return [src]
        outs = [self.g.label(prefix) for _ in range(n)]
        self.g.add(f"[{src}]split={n}" + "".join(f"[{o}]" for o in outs))
        return outs

    def grade(self, src: str, filters: list[dict], t_abs0: float, nf: int) -> str:
        if not needs_grade(filters):
            return src
        pre = self.g.label("cg")
        self.g.add(f"[{src}]scale=out_range=tv,format=yuv420p[{pre}]")
        return grade_chain(self.g, pre, filters, self.W, self.H, rs=self.rs, fps=self.fps, t0=t_abs0,
                           f0=int(round(t_abs0 * self.fps)), duration=nf / self.fps, workdir=self.workdir, cubes=self.cubes)

    def solid(self, color: str, nf: int, fmt: str = "yuv420p") -> str:
        out = self.g.label("cc")
        self.g.add(f"color=c={color}:s={self.W}x{self.H}:r={_f(self.fps)}:d={_f(nf / self.fps + 0.1)},format={fmt},"
                   f"setsar=1,trim=end_frame={nf}[{out}]")
        return out


def matte_raw(cx: Ctx, cd: dict, info: dict, tau0: float, nf: int, frame_at) -> str:
    """The refined matte (source size, gray) for clip time [tau0, tau0 + nf / fps), through the same timing
    code as the picture: the window file's frame k is source time (k0 + k) / fps."""
    g = cx.g
    out = g.label("mr")
    if info["image"]:
        idx = g.add_input(["-loop", "1", "-framerate", _f(cx.fps), "-t", _f(nf / cx.fps + 0.1), "-i", info["path"]])
        g.add(f"[{idx}:v]setpts=PTS-STARTPTS,fps={_f(cx.fps)},trim=end_frame={nf},format=gray[{out}]")
        return out
    off = info["k0"] / info["fps"]
    cdm = {**cd, "inPoint": float(cd.get("inPoint", 0)) - off, "outPoint": float(cd.get("outPoint", 0)) - off}
    if cd.get("freezeFrame") is not None:
        cdm["freezeFrame"] = float(cd["freezeFrame"]) - off
    mm = {"type": "video", "duration": info["frames"] / info["fps"], "fps": info["fps"], "hasAudio": False}
    v, _ = LY.source_raw(g, cdm, mm, info["path"], tau0, nf, cx.fps, frame_at, want_audio=False)
    g.add(f"[{v}]format=gray[{out}]")
    return out


def canvas_matte(cx: Ctx, mraw: str, cd: dict, src_w: int, src_h: int, fill: bool, tau0: float, nf: int,
                 rect=None, extend: bool = False) -> str:
    """The matte placed on the canvas exactly like the picture (layers.motion_layer), over black: gray WxH.
    Its value is matte x picture coverage x opacity."""
    lay = LY.motion_layer(cx.g, mraw, cd, src_w, src_h, fill, cx.W, cx.H, tau0, cx.fps, nf, cx.workdir, rect=rect,
                          extent=(0, 0, src_w, src_h) if extend else None)
    black = cx.solid("black", nf, "rgba")
    out = cx.g.label("cm")
    cx.g.add(f"[{black}][{lay}]overlay=0:0:format=auto:shortest=1,format=gray[{out}]")
    return out


def black_matte(cx: Ctx, nf: int) -> str:
    return cx.solid("black", nf, "gray")


def glow_layer(cx: Ctx, M: str, glow: dict, t_abs0: float, nf: int) -> str:
    """RGBA glow around the subject (see cutout.glow_levels); pulses per frame via sendcmd."""
    g = cx.g
    pulse = glow.get("pulse") or {}
    sigma, lo, hi, op = CU.glow_levels(glow, 0.0)
    blur, col, out = g.label("gb"), cx.solid(_color(glow.get("color", "#ffd25a")), nf, "rgba"), g.label("gl")
    g.add(f"[{M}]gblur=sigma={_f(sigma)}:steps=2,format=gray[{blur}]")
    name = g.label("glv")
    chain = f"[{col}][{blur}]alphamerge"
    if pulse.get("mode", "none") != "none":
        cmd = cx.workdir / f"{name}.txt"
        lines = []
        for k in range(nf):
            p = CU.pulse_at(pulse, t_abs0 + k / cx.fps, cx.beats)
            _, l2, h2, o2 = CU.glow_levels(glow, p)
            lines.append(f"{_f(k / cx.fps)} colorlevels@{name} aimin {_f(l2)}, colorlevels@{name} aimax {_f(h2)}, "
                         f"colorlevels@{name} aomax {_f(o2)};\n")
        cmd.write_text("".join(lines), encoding="utf-8")
        chain += f",sendcmd=f={cmd.name}"
    g.add(f"{chain},colorlevels@{name}=aimin={_f(lo)}:aimax={_f(hi)}:aomax={_f(op)}[{out}]")
    return out


def shadow_layer(cx: Ctx, M: str, sh: dict, nf: int, k: float = 1.0) -> str:
    g = cx.g
    dx, dy = int(round(float(sh.get("x", 10)) * k)), int(round(float(sh.get("y", 16)) * k))
    a = max(abs(dx), abs(dy), 1)
    blur = max(0.0, float(sh.get("blur", 16)) * k)
    m2, col, out = g.label("sm"), cx.solid(_color(sh.get("color", "#000000")), nf, "rgba"), g.label("sl")
    chain = f"[{M}]pad={cx.W + 2 * a}:{cx.H + 2 * a}:{a + dx}:{a + dy}:color=black,crop={cx.W}:{cx.H}:{a}:{a}"
    if blur >= 0.3:
        chain += f",gblur=sigma={_f(blur)}:steps=2"
    g.add(chain + f",format=gray[{m2}]")
    g.add(f"[{col}][{m2}]alphamerge,colorlevels=aomax={_f(max(0.0, min(1.0, float(sh.get('opacity', 0.55)))))}[{out}]")
    return out


def background(cx: Ctx, F: str | None, cut: dict, nf: int, tau0: float, t_abs0: float, bg_source) -> str:
    """Background picture (yuv420p canvas) for the cutout; F is the clip's own picture (keep/blur)."""
    bg = cut["background"]
    kind = bg.get("type", "keep")
    g = cx.g
    if kind == "color":
        B = cx.solid(_color(bg.get("color", "#14141a")), nf)
    elif kind == "blur":
        B = g.label("cbb")
        sigma = max(0.5, float(bg.get("blur", 24)))
        # blur at reduced size (like the blur fit mode): same look, far cheaper
        s = max(1, min(8, int(sigma // 6)))
        w2, h2 = max(2, cx.W // s // 2 * 2), max(2, cx.H // s // 2 * 2)
        g.add(f"[{F}]scale={w2}:{h2}:flags=area,gblur=sigma={_f(sigma / s)}:steps=2,scale={cx.W}:{cx.H}:flags=bicubic,"
              f"format=yuv420p,setsar=1[{B}]")
    elif kind in ("image", "video") and bg.get("mediaId"):
        B = bg_source(bg["mediaId"], tau0, nf)
    else:
        B = F
    return cx.grade(B, cut.get("backgroundFilters") or [], t_abs0, nf)


def composite(cx: Ctx, F: str, M: str, cut: dict, nf: int, tau0: float, t_abs0: float, bg_source) -> str:
    """The cutout clip on the canvas (yuv420p)."""
    g = cx.g
    bgk = cut["background"].get("type", "keep")
    uses_f = bgk in ("keep", "blur") or (bgk in ("image", "video") and not cut["background"].get("mediaId"))
    fs = cx.split(F, 2 if uses_f else 1, "cf")
    glow_on, shadow_on = bool(cut["glow"].get("enabled")), bool(cut["shadow"].get("enabled"))
    ms = cx.split(M, 1 + glow_on + shadow_on, "cmm")
    S = cx.grade(fs[0], cut.get("subjectFilters") or [], t_abs0, nf)
    B = background(cx, fs[1] if uses_f else None, cut, nf, tau0, t_abs0, bg_source)
    i = 1
    if shadow_on:
        sl, o = shadow_layer(cx, ms[i], cut["shadow"], nf), g.label("cb")
        g.add(f"[{B}][{sl}]overlay=0:0:format=auto:shortest=1[{o}]")
        B, i = o, i + 1
    if glow_on:
        gl, o = glow_layer(cx, ms[i], cut["glow"], t_abs0, nf), g.label("cb")
        g.add(f"[{B}][{gl}]overlay=0:0:format=auto:shortest=1[{o}]")
        B, i = o, i + 1
    sub, out = g.label("csub"), g.label("cco")
    g.add(f"[{S}][{ms[0]}]alphamerge[{sub}]")
    g.add(f"[{B}][{sub}]overlay=0:0:format=auto:shortest=1,format=yuv420p[{out}]")
    return out


def overlay_layers(cx: Ctx, lay: str, M: str, cut: dict, nf: int, t_abs0: float) -> tuple[list[str], str]:
    """An overlay-track cutout: (layers under the subject: shadow/glow, the subject layer). Background
    'keep' keeps the overlay's whole picture; anything else removes it (transparent)."""
    g = cx.g
    glow_on, shadow_on = bool(cut["glow"].get("enabled")), bool(cut["shadow"].get("enabled"))
    keep = cut["background"].get("type", "keep") == "keep"
    ms = cx.split(M, (0 if keep else 1) + glow_on + shadow_on, "omm")
    under, i = [], 0
    if not keep:
        sub = g.label("osub")
        g.add(f"[{lay}][{ms[0]}]alphamerge[{sub}]")
        lay, i = sub, 1
    if shadow_on:
        under.append(shadow_layer(cx, ms[i], cut["shadow"], nf))
        i += 1
    if glow_on:
        under.append(glow_layer(cx, ms[i], cut["glow"], t_abs0, nf))
    return under, lay


def matte_info(meta: dict, cut: dict, span) -> dict:
    """Where the refined matte for this clip is (or will be, after the plan's cutout task)."""
    raw, der = CU.lookup(meta, cut, span)
    d = CU.cut_dir(meta["id"])
    if raw and der:
        return {"ready": True, "path": str(d / der["matte"]), "k0": der["k0"], "fps": der["fps"], "frames": der["frames"],
                "image": der["image"]}
    tgt = CU.target(meta, cut, span)
    return {"ready": False, "path": str(d / tgt["matte"]), "k0": tgt["k0"], "fps": tgt["fps"], "frames": tgt["frames"],
            "image": meta["type"] == "image"}


def needs_analysis_message(names: list[str]) -> str:
    return (f"Cutout is on for {', '.join(names[:3])}{' and more' if len(names) > 3 else ''}, but the AI tools are not installed, "
            "so the subject can't be analysed. Run setup.ps1 (it builds ai-venv) and restart Ligxt, or turn Cutout off.")


__all__ = ["Ctx", "matte_raw", "canvas_matte", "black_matte", "composite", "overlay_layers", "matte_info", "math"]
