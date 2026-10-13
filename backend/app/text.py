"""Text: layout + rasterisation (Pillow), animation state, frame composition.

One renderer for both sides: the backend lays text out and rasterises it once into images (glyphs
with outline and shadow, the same in a highlight colour, background boxes) plus a layout of
per-character and per-word tiles. The preview draws those images on the canvas, the export
composes them frame by frame here. What moves where (block/unit transforms, alphas) comes from
`text_ops`, which frontend/src/text/anim.ts mirrors (checked by shared/text_cases.json).

Coordinates: "layout" = canvas pixels relative to the top-left of the text block (w x h). The
images cover the block plus a margin `m` on every side, at RES image pixels per canvas pixel.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import motion as M
from .config import CACHE_DIR, ROOT

RES = 2  # image pixels per canvas pixel: keeps text sharp under pops and zooms
SHARED = json.loads((ROOT / "shared" / "text_styles.json").read_text(encoding="utf-8"))
FONTS_DIR = ROOT / "fonts"
FONTS = {f["id"]: f for f in SHARED["fonts"]}
DEFAULTS = SHARED["defaults"]
TEXT_CACHE = CACHE_DIR / "_text"
LAYOUT_VERSION = 2


# ---------------------------------------------------------------- spec
def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def normalize(spec: dict | None) -> dict:
    return _merge(DEFAULTS, spec or {})


STYLE_KEYS = ("content", "font", "size", "color", "outline", "shadow", "box", "letterSpacing", "lineHeight",
              "align", "maxWidth", "uppercase", "highlight")


def needs_highlight(s: dict) -> bool:
    return (s.get("highlight") or {}).get("style") in ("color", "pop", "revealpop")


def layout_key(s: dict) -> str:
    """Everything that changes the pixels (not timing, animation or position)."""
    look = {k: s.get(k) for k in STYLE_KEYS}
    look["highlight"] = s["highlight"]["color"] if needs_highlight(s) else None
    look["_v"] = LAYOUT_VERSION
    look["_res"] = RES
    return hashlib.sha1(json.dumps(look, sort_keys=True).encode()).hexdigest()[:16]


def _rgba(color: str, opacity: float = 1.0) -> tuple[int, int, int, int]:
    c = (color or "#000000").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    a = int(c[6:8], 16) / 255 if len(c) >= 8 else 1.0
    return r, g, b, int(round(255 * max(0.0, min(1.0, a * opacity))))


@lru_cache(maxsize=64)
def _font(font_id: str, px: float) -> ImageFont.FreeTypeFont:
    f = FONTS.get(font_id) or FONTS[DEFAULTS["font"]]
    return ImageFont.truetype(str(FONTS_DIR / f["file"]), px)


def text_of(s: dict) -> str:
    t = str(s.get("content") or "")
    return t.upper() if s.get("uppercase") else t


# ---------------------------------------------------------------- layout + raster
def _wrap(font, text: str, ls: float, max_w: float) -> list[str]:
    def width(line: str) -> float:
        return font.getlength(line) + ls * max(0, len(line) - 1)
    lines: list[str] = []
    for para in text.split("\n"):
        words = para.split(" ")
        cur = ""
        for w in words:
            cand = w if not cur else cur + " " + w
            if cur and max_w > 0 and width(cand) > max_w:
                lines.append(cur)
                cur = w
            else:
                cur = cand
        lines.append(cur)
    return lines


def build(spec: dict) -> dict:
    """Lay out and rasterise (cached on disk by layout_key). Returns the layout dict."""
    s = normalize(spec)
    key = layout_key(s)
    d = TEXT_CACHE / key
    lay_path = d / "layout.json"
    if lay_path.exists():
        try:
            return json.loads(lay_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    R = RES
    size = max(4.0, float(s["size"]))
    font = _font(s["font"], size * R)
    ls = float(s["letterSpacing"]) * R
    line_h = size * float(s["lineHeight"]) * R
    asc, desc = font.getmetrics()
    sw = int(round(max(0.0, float(s["outline"]["width"])) * R))
    text = text_of(s)
    lines = _wrap(font, text, ls, float(s["maxWidth"]) * R)
    widths = [font.getlength(ln) + ls * max(0, len(ln) - 1) for ln in lines]
    w = math.ceil(max(widths + [1.0]) / R)
    h = math.ceil(len(lines) * line_h / R)
    sh = s["shadow"]
    bx = s["box"]
    m = 6 + math.ceil(max(0.0, float(s["outline"]["width"])))
    if sh.get("enabled"):
        m += math.ceil(3 * float(sh["blur"]) + max(abs(float(sh["x"])), abs(float(sh["y"]))))
    if bx.get("enabled"):
        m = max(m, 6 + math.ceil(max(float(bx["padX"]), float(bx["padY"]))))
    iw, ih = (w + 2 * m) * R, (h + 2 * m) * R
    mR = m * R

    # positions (image px) of every character
    chars, words = [], []
    base_y = (line_h - (asc + desc)) / 2 + asc  # baseline within a line slot
    for j, ln in enumerate(lines):
        lw = widths[j]
        ox = {"left": 0.0, "center": (w * R - lw) / 2, "right": w * R - lw}.get(s["align"], (w * R - lw) / 2)
        y_top = j * line_h
        word_i = None
        for i, ch in enumerate(ln):
            x = ox + font.getlength(ln[:i]) + ls * i
            adv = font.getlength(ch)
            if ch.strip():
                bb = font.getbbox(ch, anchor="ls")
                left, right = x + bb[0] - sw, x + bb[2] + sw
                if word_i is None:
                    word_i = len(words)
                    words.append({"text": "", "line": j, "first": len(chars)})
                words[word_i]["text"] += ch
                words[word_i]["last"] = len(chars)
                wi = word_i
            else:
                left = right = x + adv / 2
                word_i = None
                wi = -1
            chars.append({"c": ch, "x": x, "y": y_top, "left": left, "right": right, "word": wi, "line": j})

    # tiles (layout coords) that partition each line slot, so units can be drawn separately
    def slot(j: int) -> tuple[float, float]:
        y0 = -m if j == 0 else j * line_h / R
        y1 = h + m if j == len(lines) - 1 else (j + 1) * line_h / R
        return y0, y1

    for j in range(len(lines)):
        idx = [i for i, c in enumerate(chars) if c["line"] == j]
        y0, y1 = slot(j)
        bounds = [-float(m)]
        for a, b in zip(idx, idx[1:]):
            bounds.append(max(bounds[-1], (chars[a]["right"] + chars[b]["left"]) / 2 / R))
        bounds.append(float(w + m))
        for k, i in enumerate(idx):
            c = chars[i]
            c["tile"] = [bounds[k], y0, bounds[k + 1], y1]
            c["anchor"] = [((c["left"] + c["right"]) / 2) / R if c["c"].strip() else c["x"] / R, (c["y"] + line_h / 2) / R]
        wl = [k for k, wd in enumerate(words) if wd["line"] == j]
        wb = [-float(m)]
        for a, b in zip(wl, wl[1:]):
            wb.append(max(wb[-1], (chars[words[a]["last"]]["right"] + chars[words[b]["first"]]["left"]) / 2 / R))
        wb.append(float(w + m))
        for k, wi in enumerate(wl):
            wd = words[wi]
            lft, rgt = chars[wd["first"]]["left"], chars[wd["last"]]["right"]
            wd["tile"] = [wb[k], y0, wb[k + 1], y1]
            wd["anchor"] = [((lft + rgt) / 2) / R, (j * line_h + line_h / 2) / R]

    # ---- raster
    def glyphs(fill) -> Image.Image:
        im = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        if sw > 0:  # all outlines first, so no outline covers a neighbour's fill
            oc = _rgba(s["outline"]["color"])
            for c in chars:
                if c["c"].strip():
                    dr.text((c["x"] + mR, c["y"] + base_y + mR), c["c"], font=font, anchor="ls", fill=oc,
                            stroke_width=sw, stroke_fill=oc)
        for c in chars:
            if c["c"].strip():
                dr.text((c["x"] + mR, c["y"] + base_y + mR), c["c"], font=font, anchor="ls", fill=fill)
        if sh.get("enabled") and float(sh["opacity"]) > 0:
            a = np.array(im)[..., 3].astype(np.float32)
            r, g, b, _ = _rgba(sh["color"])
            sa = Image.fromarray((a * float(sh["opacity"])).clip(0, 255).astype(np.uint8), "L")
            dx, dy = int(round(float(sh["x"]) * R)), int(round(float(sh["y"]) * R))
            shifted = Image.new("L", (iw, ih), 0)
            shifted.paste(sa, (dx, dy))
            if float(sh["blur"]) > 0:
                shifted = shifted.filter(ImageFilter.GaussianBlur(float(sh["blur"]) * R))
            shadow = Image.new("RGBA", (iw, ih), (r, g, b, 0))
            shadow.putalpha(shifted)
            im = Image.alpha_composite(shadow, im)
        return im

    d.mkdir(parents=True, exist_ok=True)
    base = glyphs(_rgba(s["color"]))
    base.save(d / "base.png", compress_level=3)
    images = {"base": "base.png"}
    if needs_highlight(s):
        glyphs(_rgba(s["highlight"]["color"])).save(d / "hi.png", compress_level=3)
        images["hi"] = "hi.png"
    if bx.get("enabled") and float(bx["opacity"]) > 0:
        box = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
        dr = ImageDraw.Draw(box)
        fill = _rgba(bx["color"], float(bx["opacity"]))
        px, py, rad = float(bx["padX"]) * R, float(bx["padY"]) * R, float(bx["radius"]) * R
        ink_h = asc + desc
        rects = []
        for j, ln in enumerate(lines):
            if not ln.strip():
                continue
            idx = [c for c in chars if c["line"] == j]
            x0 = min(c["x"] for c in idx)
            x1 = max(c["x"] + font.getlength(c["c"]) for c in idx)
            y0 = j * line_h + (line_h - ink_h) / 2
            rects.append([x0 - px, y0 - py, x1 + px, y0 + ink_h + py])
        if bx.get("mode") == "block" and rects:
            rects = [[min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects)]]
        for r in rects:
            dr.rounded_rectangle([r[0] + mR, r[1] + mR, r[2] + mR, r[3] + mR], radius=rad, fill=fill)
        box.save(d / "box.png", compress_level=3)
        images["box"] = "box.png"

    layout = {
        "key": key, "res": R, "w": w, "h": h, "m": m, "iw": iw, "ih": ih, "lines": len(lines),
        "images": images,
        "chars": [{"c": c["c"], "word": c["word"], "line": c["line"], "tile": [round(v, 3) for v in c["tile"]],
                   "anchor": [round(v, 3) for v in c["anchor"]]} for c in chars],
        "words": [{"text": wd["text"], "line": wd["line"], "tile": [round(v, 3) for v in wd["tile"]],
                   "anchor": [round(v, 3) for v in wd["anchor"]]} for wd in words],
    }
    tmp = lay_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(layout), encoding="utf-8")
    os.replace(tmp, lay_path)
    return layout


# ---------------------------------------------------------------- animation (mirrored in anim.ts)
def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


DIRS = {"up": (0, 1), "down": (0, -1), "left": (1, 0), "right": (-1, 0)}


def state(spec: dict, nchars: int, nwords: int, tau: float, D: float) -> dict:
    """Block transform + per-unit states at clip time tau of a D-second text clip."""
    s = normalize(spec)
    blk = {"op": 1.0, "dx": 0.0, "dy": 0.0, "s": 1.0}
    kind = None
    units: list[dict] = []

    def unit_list(n):
        return [{"a": 1.0, "dy": 0.0, "s": 1.0, "hi": 0.0} for _ in range(n)]

    for phase, t in (("animIn", tau), ("animOut", D - tau)):
        a = s[phase]
        typ = a.get("type", "none")
        if typ == "none":
            continue
        dur = max(1e-3, float(a.get("duration", 0.4)))
        u = _clamp(t / dur)
        e = M.ease(a.get("easing", "linear"), u)
        sign = 1 if phase == "animIn" else -1
        if typ == "fade":
            blk["op"] *= _clamp(e)
        elif typ == "slide":
            dist = float(a.get("distance", 80))
            vx, vy = DIRS.get(a.get("direction", "up"), (0, 1))
            blk["dx"] += sign * vx * (1 - e) * dist
            blk["dy"] += sign * vy * (1 - e) * dist
            blk["op"] *= _clamp(e)
        elif typ == "pop":
            blk["s"] *= 0.6 + 0.4 * e
            blk["op"] *= _clamp(u * 4)
        elif typ == "typewriter" and nchars:
            if kind != "chars":
                kind, units = "chars", unit_list(nchars)
            vis = math.floor(e * nchars + 1e-6) if phase == "animIn" else math.ceil(e * nchars - 1e-6)
            for i, un in enumerate(units):
                if i >= vis:
                    un["a"] = 0.0
        elif typ == "words" and nwords:
            if kind is None:
                kind, units = "words", unit_list(nwords)
            if kind != "words":
                continue
            span = dur * 0.5 if nwords > 1 else 0.0
            L = dur - span
            for i, un in enumerate(units):
                st = span * i / max(1, nwords - 1)
                v = _clamp((t - st) / max(1e-3, L))
                ev = M.ease(a.get("easing", "linear"), v)
                un["a"] *= _clamp(ev)
                un["dy"] += sign * (1 - ev) * 24
                un["s"] *= 0.8 + 0.2 * ev
    hl = s["highlight"]
    wt = s.get("words") or []
    if hl.get("style", "none") != "none" and wt and nwords and kind != "chars":
        if kind is None:
            kind, units = "words", unit_list(nwords)
        active = -1
        for i in range(min(nwords, len(wt))):
            if float(wt[i]["start"]) <= tau + 1e-9:
                active = i
        for i, un in enumerate(units):
            if hl["style"] in ("reveal", "revealpop") and i < len(wt) and tau + 1e-9 < float(wt[i]["start"]):
                un["a"] = 0.0
            if i == active:
                un["hi"] = 1.0 if hl["style"] in ("color", "pop", "revealpop") else 0.0
                if hl["style"] in ("pop", "revealpop"):
                    v = _clamp((tau - float(wt[i]["start"])) / 0.12)
                    un["s"] *= 1 + 0.08 * M.ease("punch", v)
    return {"block": blk, "kind": kind, "units": units}


def text_matrix(w: float, h: float, W: float, H: float, t: dict) -> tuple:
    """Layout coords -> canvas: the block's centre at (W/2 + x, H/2 + y), scaled/rotated/flipped."""
    s = t["scale"]
    fx = -1.0 if t.get("flipH") else 1.0
    fy = -1.0 if t.get("flipV") else 1.0
    th = math.radians(t["rotation"])
    co, si = math.cos(th), math.sin(th)
    a, b = s * fx * co, s * fx * si
    c, d = -s * fy * si, s * fy * co
    cx, cy = w / 2, h / 2
    Cx, Cy = W / 2 + t["x"], H / 2 + t["y"]
    return (a, b, c, d, Cx - (a * cx + c * cy), Cy - (b * cx + d * cy))


def mul(m1, m2):
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2, a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def text_ops(lay: dict, clip: dict, tau: float, W: float, H: float) -> list[dict]:
    """Draw operations for one frame: [{img, tile (layout rect), m (layout -> canvas), alpha}]."""
    spec = clip.get("text") or {}
    D = M.clip_duration(clip)
    st = state(spec, len(lay["chars"]), len(lay["words"]), tau, D)
    tv = M.transform_at(clip, tau)
    b = st["block"]
    tv = {**tv, "x": tv["x"] + b["dx"], "y": tv["y"] + b["dy"], "scale": tv["scale"] * b["s"]}
    op = max(0.0, min(1.0, tv["opacity"])) * b["op"]
    if op <= 1e-4:
        return []
    mb = text_matrix(lay["w"], lay["h"], W, H, tv)
    m = lay["m"]
    full = [-m, -m, lay["w"] + m, lay["h"] + m]
    ops = []
    if "box" in lay["images"]:
        ops.append({"img": "box", "tile": full, "m": mb, "alpha": op})
    if st["kind"] is None:
        ops.append({"img": "base", "tile": full, "m": mb, "alpha": op})
        return ops
    items = lay["chars"] if st["kind"] == "chars" else lay["words"]
    # highlighted (possibly enlarged) units go last so their neighbours never cover them
    pairs = sorted(zip(items, st["units"]), key=lambda p: p[1]["hi"] > 0)
    for it, u in pairs:
        a = op * u["a"]
        if a <= 1e-4:
            continue
        ax, ay = it["anchor"]
        mu = mul(mb, (u["s"], 0.0, 0.0, u["s"], ax - u["s"] * ax, ay + u["dy"] - u["s"] * ay))
        if u["hi"] < 1 or "hi" not in lay["images"]:
            ops.append({"img": "base", "tile": it["tile"], "m": mu, "alpha": a})
        if u["hi"] > 0 and "hi" in lay["images"]:
            ops.append({"img": "hi", "tile": it["tile"], "m": mu, "alpha": a * u["hi"]})
    return ops


# ---------------------------------------------------------------- export composition
def _bbox(ops: list[dict], W: int, H: int) -> tuple[int, int, int, int] | None:
    xs, ys = [], []
    for o in ops:
        x0, y0, x1, y1 = o["tile"]
        for X, Y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            px, py = M.apply(o["m"], X, Y)
            xs.append(px)
            ys.append(py)
    if not xs:
        return None
    bx0, by0 = max(0, math.floor(min(xs))), max(0, math.floor(min(ys)))
    bx1, by1 = min(W, math.ceil(max(xs)) + 1), min(H, math.ceil(max(ys)) + 1)
    if bx1 <= bx0 or by1 <= by0:
        return None
    return bx0, by0, bx1, by1


class Composer:
    """Composes text frames for the export (premultiplied, 2x supersampled within a bounding box)."""
    SS = 2

    def __init__(self, lay: dict):
        self.lay = lay
        d = TEXT_CACHE / lay["key"]
        self.images = {k: Image.open(d / f).convert("RGBA").convert("RGBa") for k, f in lay["images"].items()}

    def compose(self, ops: list[dict], box: tuple[int, int, int, int]) -> np.ndarray:
        """Straight-alpha RGBA uint8 array of the box region."""
        bx0, by0, bx1, by1 = box
        S = self.SS
        bw, bh = (bx1 - bx0) * S, (by1 - by0) * S
        acc = np.zeros((bh, bw, 4), np.float32)
        R, m = self.lay["res"], self.lay["m"]
        for o in ops:
            img = self.images[o["img"]]
            x0, y0, x1, y1 = o["tile"]
            cx0, cy0 = max(0, math.floor((x0 + m) * R)), max(0, math.floor((y0 + m) * R))
            cx1, cy1 = min(img.width, math.ceil((x1 + m) * R)), min(img.height, math.ceil((y1 + m) * R))
            if cx1 <= cx0 or cy1 <= cy0:
                continue
            crop = img.crop((cx0, cy0, cx1, cy1))
            # crop px -> layout: (px + cx0)/R - m ; layout -> canvas: o.m ; canvas -> box px: S*(c - b0)
            to_lay = (1 / R, 0.0, 0.0, 1 / R, cx0 / R - m, cy0 / R - m)
            full = mul((S, 0.0, 0.0, S, -S * bx0, -S * by0), mul(o["m"], to_lay))
            # Only this op's own footprint (in supersampled box pixels) is transformed and blended.
            pts = [M.apply(full, X, Y) for X, Y in ((0, 0), (cx1 - cx0, 0), (0, cy1 - cy0), (cx1 - cx0, cy1 - cy0))]
            ox0, oy0 = max(0, math.floor(min(p[0] for p in pts))), max(0, math.floor(min(p[1] for p in pts)))
            ox1, oy1 = min(bw, math.ceil(max(p[0] for p in pts)) + 1), min(bh, math.ceil(max(p[1] for p in pts)) + 1)
            if ox1 <= ox0 or oy1 <= oy0:
                continue
            local = mul((1.0, 0.0, 0.0, 1.0, -ox0, -oy0), full)
            inv = M.invert(local)
            out = crop.transform((ox1 - ox0, oy1 - oy0), Image.AFFINE, data=(inv[0], inv[2], inv[4], inv[1], inv[3], inv[5]),
                                 resample=Image.BILINEAR)
            a = np.asarray(out, np.float32) * float(o["alpha"])
            # The tile's exact (fractional) edges, not the crop's whole-pixel edges, bound what it draws.
            if (x0 + m) * R > cx0 or (x1 + m) * R < cx1 or (y0 + m) * R > cy0 or (y1 + m) * R < cy1:
                a *= self._tile_mask(local, ((x0 + m) * R - cx0, (y0 + m) * R - cy0, (x1 + m) * R - cx0, (y1 + m) * R - cy0),
                                     ox1 - ox0, oy1 - oy0)
            sl = acc[oy0:oy1, ox0:ox1]
            acc[oy0:oy1, ox0:ox1] = a + sl * (1 - a[..., 3:4] / 255.0)
        acc = acc.reshape(bh // S, S, bw // S, S, 4).mean(axis=(1, 3))
        alpha = acc[..., 3:4]
        rgb = np.where(alpha > 1e-3, acc[..., :3] * 255.0 / np.maximum(alpha, 1e-3), 0)
        return np.concatenate([rgb, alpha], axis=-1).round().clip(0, 255).astype(np.uint8)

    @staticmethod
    def _tile_mask(full, rect, bw, bh) -> np.ndarray:
        """Coverage of the exact (fractional) tile rect, in output pixels (anti-aliased by the 2x SS)."""
        inv = M.invert(full)
        ys, xs = np.mgrid[0:bh, 0:bw].astype(np.float32) + 0.5
        sx = inv[0] * xs + inv[2] * ys + inv[4]
        sy = inv[1] * xs + inv[3] * ys + inv[5]
        x0, y0, x1, y1 = rect
        return ((sx >= x0) & (sx < x1) & (sy >= y0) & (sy < y1)).astype(np.float32)[..., None]


def prepare_clip(clip: dict, tau0: float, nf: int, fps: float, W: int, H: int) -> dict | None:
    """Plan a text clip's frames from clip time tau0 (cheap: layout + per-frame ops). Returns
    {"x", "y", "region", "lay", "frames"} or None if nothing is ever visible."""
    lay = build(clip.get("text") or {})
    frames = [text_ops(lay, clip, tau0 + k / fps, W, H) for k in range(nf)]
    live = [b for b in (_bbox(o, W, H) for o in frames) if b]
    if not live:
        return None
    ux0, uy0 = min(b[0] for b in live), min(b[1] for b in live)
    ux1, uy1 = max(b[2] for b in live), max(b[3] for b in live)
    ux1 += (ux1 - ux0) % 2  # even sizes keep ffmpeg's yuv overlay happy
    uy1 += (uy1 - uy0) % 2
    return {"x": ux0, "y": uy0, "region": (ux0, uy0, ux1, uy1), "lay": lay, "frames": frames}


def write_frames(prep: dict, out_dir: Path, prefix: str) -> None:
    """Writes `prefix_%05d.png` (one per frame, the prepared region; identical frames are hard links)."""
    ux0, uy0, ux1, uy1 = prep["region"]
    comp = Composer(prep["lay"])
    seen: dict[str, Path] = {}
    for k, ops in enumerate(prep["frames"]):
        path = out_dir / f"{prefix}_{k:05d}.png"
        sig = json.dumps([[o["img"], [round(v, 3) for v in o["tile"]], [round(v, 4) for v in o["m"]], round(o["alpha"], 4)]
                          for o in ops])
        src = seen.get(sig)
        if src is None:
            if ops:
                img = Image.fromarray(comp.compose(ops, prep["region"]), "RGBA")
            else:
                img = Image.new("RGBA", (ux1 - ux0, uy1 - uy0), (0, 0, 0, 0))
            img.save(path, compress_level=1)
            seen[sig] = path
        else:
            try:
                os.link(src, path)
            except OSError:
                path.write_bytes(src.read_bytes())


def render_clip_frames(clip: dict, tau0: float, nf: int, fps: float, W: int, H: int, out_dir: Path,
                       prefix: str) -> tuple[int, int] | None:
    prep = prepare_clip(clip, tau0, nf, fps, W, H)
    if prep is None:
        return None
    write_frames(prep, out_dir, prefix)
    return prep["x"], prep["y"]
