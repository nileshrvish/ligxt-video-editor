"""Colour grading and effects (shared contract with frontend/src/grade/math.ts + renderer.ts).

Pipeline per clip, in this fixed order (the order of filters in the list only matters among
presets/LUTs, which are applied in list order):
  1. blur (gblur)                       spatial
  2. sharpen (unsharp, luma)            spatial
  3. colour LUT                         point: adjust -> presets/LUTs (intensity, skin protection) -> VHS colour
  4. chromatic aberration (rgbashift)   spatial
  5. VHS chroma bleed                   spatial
  6. glow / bloom                       spatial
  7. light leak (screen)                spatial, animated
  8. VHS scanlines (multiply)           spatial
  9. vignette (multiply)                spatial
 10. film grain (grain merge)           spatial, animated

All point operations are baked into one 33^3 3D LUT (lut3d, trilinear), so the preview (which
builds the identical LUT in TypeScript) and the export match. Spatial sizes are given in
"reference pixels" at a 1080-pixel short side and scaled with the output.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

import numpy as np

from .config import CACHE_DIR, ROOT

LUT_SIZE = 33
GRAIN_FRAMES = 8
SHARED = ROOT / "shared"
PRESETS: dict = json.loads((SHARED / "presets.json").read_text(encoding="utf-8"))
FX_DIR = CACHE_DIR / "_fx"
FX_DIR.mkdir(parents=True, exist_ok=True)

LUMA = np.array([0.2126, 0.7152, 0.0722])
MONO = np.array([0.30, 0.59, 0.11])

ADJUST_KEYS = ("exposure", "temperature", "tint", "brightness", "contrast", "highlights", "shadows",
               "whites", "blacks", "saturation", "vibrance", "sharpen", "blur")
EFFECT_KEYS = ("grain", "vignette", "lightleak", "vhs", "glow", "chroma")
EFFECT_DEFAULTS = {
    "grain": {"amount": 0.0, "size": 1.5},
    "vignette": {"amount": 0.0, "feather": 0.5},
    "lightleak": {"amount": 0.0, "speed": 0.35},
    "vhs": {"amount": 0.0},
    "glow": {"amount": 0.0, "threshold": 0.7, "radius": 14.0},
    "chroma": {"amount": 0.0},
}


# ---------------------------------------------------------------- colour math (numpy, (..., 3))
def smoothstep(e0: float, e1: float, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def luma(c):
    return c @ LUMA


def adjust(c: np.ndarray, p: dict) -> np.ndarray:
    g = lambda k: float(p.get(k) or 0.0)  # noqa: E731
    e, T, Ti = g("exposure"), g("temperature") / 100, g("tint") / 100
    if e or T or Ti:
        lin = np.clip(c, 0, None) ** 2.2
        lin = lin * (2.0 ** e) * np.array([1 + 0.15 * T, 1 - 0.12 * Ti, 1 - 0.15 * T])
        c = np.clip(lin, 0, None) ** (1 / 2.2)
    B = g("brightness") / 100
    if B:
        c = c + 0.15 * B
    C = g("contrast") / 100
    if C:
        k = 1 + 0.8 * C if C >= 0 else 1 + 0.6 * C
        c = (c - 0.5) * k + 0.5
    sh, hi, bl, wh = g("shadows") / 100, g("highlights") / 100, g("blacks") / 100, g("whites") / 100
    if sh or hi or bl or wh:
        Y = luma(c)[..., None]
        c = c + (0.25 * sh * (1 - smoothstep(0, 0.6, Y)) + 0.25 * hi * smoothstep(0.4, 1, Y)
                 + 0.12 * bl * (1 - smoothstep(0, 0.25, Y)) + 0.12 * wh * smoothstep(0.75, 1, Y))
    S, V = g("saturation") / 100, g("vibrance") / 100
    if S or V:
        Y = luma(c)[..., None]
        m = np.clip(c.max(axis=-1, keepdims=True) - c.min(axis=-1, keepdims=True), 0, 1)
        f = np.maximum(0.0, 1 + S + V * (1 - m))
        c = Y + (c - Y) * f
    return np.clip(c, 0, 1)


def preset_color(c: np.ndarray, d: dict) -> np.ndarray:
    c = adjust(c, d.get("adjust", {}))
    sp = d.get("split")
    if sp:
        Y = luma(c)[..., None]
        c = c + np.array(sp.get("shadows", [0, 0, 0])) * (1 - smoothstep(0, 0.6, Y)) \
              + np.array(sp.get("highlights", [0, 0, 0])) * smoothstep(0.4, 1, Y)
    f = float(d.get("fade") or 0)
    if f:
        c = f + c * (1 - f)
    if d.get("mono"):
        y = np.clip(c, 0, 1) @ MONO
        c = np.repeat(y[..., None], 3, axis=-1)
    return np.clip(c, 0, 1)


def skin_weight(c: np.ndarray) -> np.ndarray:
    v = c.max(axis=-1)
    mn = c.min(axis=-1)
    d = v - mn
    s = np.where(v > 1e-6, d / np.maximum(v, 1e-6), 0.0)
    r, g, b = c[..., 0], c[..., 1], c[..., 2]
    dd = np.maximum(d, 1e-6)
    h = np.where(v == r, ((g - b) / dd) % 6, np.where(v == g, (b - r) / dd + 2, (r - g) / dd + 4)) * 60
    h = np.where(d < 1e-6, 0.0, h)
    dh = np.abs(h - 18)
    dh = np.minimum(dh, 360 - dh)
    wh = 1 - smoothstep(20, 40, dh)
    ws = smoothstep(0.08, 0.18, s) * (1 - smoothstep(0.65, 0.85, s))
    wv = smoothstep(0.12, 0.30, v)
    return wh * ws * wv


def protect_skin(orig: np.ndarray, graded: np.ndarray) -> np.ndarray:
    """Keep the hue/saturation of skin tones while following the grade's brightness."""
    w = (0.85 * skin_weight(orig))[..., None]
    yo = luma(orig)[..., None]
    yg = luma(graded)[..., None]
    target = np.clip(orig * (yg / np.maximum(yo, 1e-4)), 0, 1)
    return graded * (1 - w) + target * w


def hue_sat(c: np.ndarray):
    """HSV hue (degrees) and saturation, same formula as skin_weight."""
    v = c.max(axis=-1)
    d = v - c.min(axis=-1)
    s = np.where(v > 1e-6, d / np.maximum(v, 1e-6), 0.0)
    r, g, b = c[..., 0], c[..., 1], c[..., 2]
    dd = np.maximum(d, 1e-6)
    h = np.where(v == r, ((g - b) / dd) % 6, np.where(v == g, (b - r) / dd + 2, (r - g) / dd + 4)) * 60
    return np.where(d < 1e-6, 0.0, h), s


def selective(c: np.ndarray, p: dict) -> np.ndarray:
    """Keep one hue range in colour; everything else becomes black and white (same weights as B&W)."""
    hue = float(p.get("hue", 25)) % 360
    rng = max(0.0, float(p.get("range", 25)))
    soft = max(0.5, float(p.get("softness", 15)))
    h, s = hue_sat(c)
    dh = np.abs(h - hue)
    dh = np.minimum(dh, 360 - dh)
    lo, hi = float(p.get("satLow", 0.08)), float(p.get("satHigh", 0.2))
    w = (1 - smoothstep(rng, rng + soft, dh)) * smoothstep(lo, max(lo + 0.01, hi), s)
    y = np.clip(c, 0, 1) @ MONO
    grey = np.repeat(y[..., None], 3, axis=-1)
    return grey + (c - grey) * w[..., None]


def sample_cube(cube: dict, c: np.ndarray) -> np.ndarray:
    """Trilinear lookup into a parsed .cube LUT."""
    n = cube["size"]
    t = cube["table"]  # (n, n, n, 3) indexed [b, g, r]
    lo, hi = np.array(cube["min"]), np.array(cube["max"])
    x = np.clip((c - lo) / (hi - lo), 0, 1) * (n - 1)
    i0 = np.clip(np.floor(x).astype(int), 0, n - 2)
    f = x - i0
    r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]

    def at(db, dg, dr):
        return t[b0 + db, g0 + dg, r0 + dr]

    c00 = at(0, 0, 0) * (1 - fr) + at(0, 0, 1) * fr
    c01 = at(0, 1, 0) * (1 - fr) + at(0, 1, 1) * fr
    c10 = at(1, 0, 0) * (1 - fr) + at(1, 0, 1) * fr
    c11 = at(1, 1, 0) * (1 - fr) + at(1, 1, 1) * fr
    c0 = c00 * (1 - fg) + c01 * fg
    c1 = c10 * (1 - fg) + c11 * fg
    return c0 * (1 - fb) + c1 * fb


def grade_color(c: np.ndarray, filters: list[dict], cubes: dict[str, dict]) -> np.ndarray:
    """All point operations of a filter stack."""
    active = [f for f in filters if f.get("enabled", True)]
    for f in active:
        if f.get("type") == "adjust":
            c = adjust(c, f.get("params") or {})
    for f in active:
        kind = f.get("type")
        if kind not in ("preset", "lut"):
            continue
        I = float(f.get("intensity", 1.0))
        if I <= 0:
            continue
        if kind == "preset":
            d = PRESETS["presets"].get(f.get("name") or "")
            if not d:
                continue
            graded = preset_color(c, d)
        else:
            cube = cubes.get((f.get("params") or {}).get("lut") or "")
            if not cube:
                continue
            graded = np.clip(sample_cube(cube, c), 0, 1)
        # Skin protection never applies to monochrome presets (B&W is meant to be grey).
        mono = kind == "preset" and bool(PRESETS["presets"].get(f.get("name") or "", {}).get("mono"))
        if (f.get("params") or {}).get("skinProtect", True) and not mono:
            graded = protect_skin(c, graded)
        c = c + (graded - c) * I
    for f in active:
        if f.get("type") == "selective" and float(f.get("intensity", 1)) > 0:
            c = c + (selective(c, f.get("params") or {}) - c) * float(f.get("intensity", 1))
    vhs = effects(filters)["vhs"]["amount"]
    if vhs > 0:
        c = c + (preset_color(c, PRESETS["vhs"]) - c) * vhs
    return np.clip(c, 0, 1)


def effects(filters: list[dict]) -> dict:
    """Effective effect parameters: explicit effect filters plus preset contributions (x intensity)."""
    out = {k: dict(v) for k, v in EFFECT_DEFAULTS.items()}
    for f in filters:
        if not f.get("enabled", True):
            continue
        if f.get("type") == "effect" and f.get("name") in out:
            p = f.get("params") or {}
            e = out[f["name"]]
            e["amount"] = min(1.0, e["amount"] + float(p.get("amount", 0)) * float(f.get("intensity", 1)))
            for k, v in p.items():
                if k != "amount":
                    e[k] = float(v)
        elif f.get("type") == "preset":
            d = PRESETS["presets"].get(f.get("name") or "", {})
            I = float(f.get("intensity", 1))
            for name, p in (d.get("effects") or {}).items():
                e = out[name]
                e["amount"] = min(1.0, e["amount"] + float(p.get("amount", 0)) * I)
                for k, v in p.items():
                    if k != "amount":
                        e[k] = float(v)
    return out


def adjust_params(filters: list[dict]) -> dict:
    for f in filters:
        if f.get("type") == "adjust" and f.get("enabled", True):
            return f.get("params") or {}
    return {}


def has_color(filters: list[dict]) -> bool:
    for f in filters:
        if not f.get("enabled", True):
            continue
        if f.get("type") == "adjust" and any(float((f.get("params") or {}).get(k) or 0) for k in ADJUST_KEYS[:11]):
            return True
        if f.get("type") in ("preset", "lut", "selective") and float(f.get("intensity", 1)) > 0:
            return True
    return effects(filters)["vhs"]["amount"] > 0


def build_lut(filters: list[dict], cubes: dict[str, dict], n: int = LUT_SIZE) -> np.ndarray:
    """(n, n, n, 3) indexed [b, g, r] (the .cube order: red changes fastest)."""
    v = np.linspace(0, 1, n)
    b, g, r = np.meshgrid(v, v, v, indexing="ij")
    grid = np.stack([r, g, b], axis=-1)
    return grade_color(grid, filters, cubes)


# ---------------------------------------------------------------- .cube files
def parse_cube(text: str) -> dict:
    size, lo, hi, rows = None, [0.0, 0.0, 0.0], [1.0, 1.0, 1.0], []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        key = s.split()[0].upper()
        if key == "LUT_3D_SIZE":
            size = int(s.split()[1])
        elif key == "LUT_1D_SIZE":
            raise ValueError("1D LUTs are not supported; use a 3D .cube LUT.")
        elif key == "DOMAIN_MIN":
            lo = [float(x) for x in s.split()[1:4]]
        elif key == "DOMAIN_MAX":
            hi = [float(x) for x in s.split()[1:4]]
        elif key == "TITLE" or re.match(r"[A-Z_]+$", key):
            continue
        else:
            rows.append([float(x) for x in s.split()[:3]])
    if not size or not 2 <= size <= 128:
        raise ValueError("Missing or invalid LUT_3D_SIZE.")
    if len(rows) != size ** 3:
        raise ValueError(f"Expected {size ** 3} table rows, found {len(rows)}.")
    table = np.array(rows, dtype=np.float64).reshape(size, size, size, 3)
    return {"size": size, "min": lo, "max": hi, "table": table}


def write_cube(path: Path, lut: np.ndarray, title: str = "Ligxt grade") -> None:
    n = lut.shape[0]
    flat = lut.reshape(-1, 3)
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {n}"] + [f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in flat]
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


# ---------------------------------------------------------------- generated textures (shared with the preview)
def grain_atlas(w: int, h: int, size: float) -> Path:
    """GRAIN_FRAMES noise tiles stacked vertically, each ceil(w/size) x ceil(h/size), uniform 0..255."""
    tw, th = max(2, math.ceil(w / size)), max(2, math.ceil(h / size))
    path = FX_DIR / f"grain_{tw}x{th}.png"
    if not path.exists():
        rng = np.random.default_rng(1234)
        data = rng.integers(0, 256, size=(th * GRAIN_FRAMES, tw), dtype=np.uint8)
        _write_png(path, np.repeat(data[..., None], 3, axis=-1))
    return path


def leak_texture(w: int, h: int) -> Path:
    """A warm light-leak texture twice as wide as the frame (it slides across over time)."""
    W2 = 2 * w
    path = FX_DIR / f"leak_{W2}x{h}.png"
    if not path.exists():
        y, x = np.mgrid[0:h, 0:W2].astype(np.float32)
        u, v = x / W2, y / h
        img = np.zeros((h, W2, 3), np.float32)
        blobs = [(0.12, 0.25, 0.22, (1.0, 0.55, 0.15)), (0.38, 0.8, 0.30, (1.0, 0.25, 0.1)),
                 (0.62, 0.35, 0.25, (1.0, 0.75, 0.35)), (0.88, 0.7, 0.28, (0.95, 0.35, 0.3))]
        for cx, cy, r, col in blobs:
            d = np.sqrt(((u - cx) * 2.0) ** 2 + (v - cy) ** 2) / r
            img += np.exp(-d * d * 2.2)[..., None] * np.array(col, np.float32)
        _write_png(path, np.clip(img * 255, 0, 255).astype(np.uint8))
    return path


def vignette_mask(w: int, h: int, amount: float, feather: float) -> Path:
    key = f"{w}x{h}_{amount:.3f}_{feather:.3f}"
    path = FX_DIR / f"vig_{hashlib.md5(key.encode()).hexdigest()[:12]}.png"
    if not path.exists():
        y, x = np.mgrid[0:h, 0:w].astype(np.float64)
        m = vignette_value((x + 0.5) / w, (y + 0.5) / h, amount, feather)
        _write_png(path, np.repeat(np.round(m * 255).astype(np.uint8)[..., None], 3, axis=-1))
    return path


def vignette_value(u, v, amount: float, feather: float):
    d = np.sqrt((u - 0.5) ** 2 + (v - 0.5) ** 2) / 0.70710678
    r0 = 0.25 + 0.4 * (1 - feather)
    r1 = r0 + 0.3 + 0.5 * feather
    return 1 - amount * smoothstep(r0, r1, d)


def scanline_mask(w: int, h: int, amount: float, period: float) -> Path:
    key = f"{w}x{h}_{amount:.3f}_{period:.3f}"
    path = FX_DIR / f"scan_{hashlib.md5(key.encode()).hexdigest()[:12]}.png"
    if not path.exists():
        y = np.arange(h, dtype=np.float64) + 0.5
        row = scanline_value(y, amount, period)
        m = np.round(np.repeat(row[:, None], w, axis=1) * 255).astype(np.uint8)
        _write_png(path, np.repeat(m[..., None], 3, axis=-1))
    return path


def scanline_value(y, amount: float, period: float):
    return 1 - 0.18 * amount * (0.5 + 0.5 * np.cos(2 * np.pi * y / period))


def _write_png(path: Path, rgb: np.ndarray) -> None:
    import zlib
    import struct
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[i].tobytes() for i in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(png)
    tmp.replace(path)


# ---------------------------------------------------------------- ffmpeg chain
# FFmpeg 8.1 corrupts the last 8 columns when converting yuv420p -> gbrp at widths like 1080
# (= 8 mod 16). Going through packed rgb24 first is exact. Use this to enter planar RGB from YUV.
TO_GBRP = "format=rgb24,format=gbrp"


def _f(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


def needs_grade(filters: list[dict] | None) -> bool:
    if not filters:
        return False
    p = adjust_params(filters)
    fx = effects(filters)
    return has_color(filters) or float(p.get("sharpen") or 0) > 0 or float(p.get("blur") or 0) > 0 \
        or any(fx[k]["amount"] > 0 for k in EFFECT_KEYS)


def ref_scale(W: int, H: int) -> float:
    return min(W, H) / 1080.0


def grade_chain(g, src: str, filters: list[dict], w: int, h: int, *, rs: float, fps: float,
                t0: float, f0: int, duration: float, workdir: Path, cubes: dict[str, dict]) -> str:
    """Append the grading chain for a w x h stream (yuv420p in, yuv420p out). `t0`/`f0` are the
    absolute timeline time/frame of the stream's first frame (for animated grain and light leaks)."""
    p = adjust_params(filters)
    fx = effects(filters)
    cur = src

    def step(expr: str, prefix: str = "gr") -> None:
        nonlocal cur
        out = g.label(prefix)
        g.add(f"[{cur}]{expr}[{out}]")
        cur = out

    blur = float(p.get("blur") or 0) / 100 * 20 * rs
    if blur >= 0.3:
        step(f"gblur=sigma={_f(blur)}:steps=4")
    sharp = float(p.get("sharpen") or 0) / 100 * 1.5
    if sharp > 0:
        step(f"unsharp=lx=5:ly=5:la={_f(sharp)}:cx=5:cy=5:ca=0")
    step(TO_GBRP)
    if has_color(filters):
        lut = build_lut(filters, cubes)
        name = "g_" + hashlib.md5(lut.tobytes()).hexdigest()[:16] + ".cube"
        if not (workdir / name).exists():
            write_cube(workdir / name, lut)
        step(f"lut3d=file={name}:interp=trilinear")
    ca = round(fx["chroma"]["amount"] * 6 * rs + fx["vhs"]["amount"] * PRESETS["vhs"]["chroma"] * rs)
    if ca >= 1:
        step(f"rgbashift=rh={ca}:bh={-ca}")
    if fx["vhs"]["amount"] > 0:
        sig = 4 * rs * fx["vhs"]["amount"]
        if sig >= 0.3:
            step(f"format=yuv444p,gblur=sigma={_f(sig)}:sigmaV=0.01:steps=4:planes=6,format=gbrp")
    if fx["glow"]["amount"] > 0:
        e = fx["glow"]
        th = min(0.98, e["threshold"])
        a, b2, bl, out = g.label("ga"), g.label("gb"), g.label("gbl"), g.label("go")
        thr = f"'clip((val-{_f(th * 255)})/{_f(1 - th)},0,255)'"
        g.add(f"[{cur}]split[{a}][{b2}]")
        g.add(f"[{b2}]lutrgb=r={thr}:g={thr}:b={thr},gblur=sigma={_f(max(0.5, e['radius'] * rs))}:steps=4[{bl}]")
        g.add(f"[{a}][{bl}]blend=all_mode=screen:all_opacity={_f(e['amount'])}[{out}]")
        cur = out
    if fx["lightleak"]["amount"] > 0:
        e = fx["lightleak"]
        idx = g.add_input(["-loop", "1", "-framerate", _f(fps), "-t", _f(duration + 0.2), "-i", str(leak_texture(w, h))])
        lk, out = g.label("lk"), g.label("lo")
        g.add(f"[{idx}:v]crop=w={w}:h={h}:x='(iw-ow)*(0.5+0.5*sin((t+{_f(t0)})*{_f(e['speed'])}))':y=0,"
              f"format=gbrp[{lk}]")
        g.add(f"[{cur}][{lk}]blend=all_mode=screen:all_opacity={_f(e['amount'])}:shortest=1[{out}]")
        cur = out
    if fx["vhs"]["amount"] > 0:
        idx = g.add_input(["-loop", "1", "-framerate", _f(fps), "-t", _f(duration + 0.2), "-i",
                           str(scanline_mask(w, h, fx["vhs"]["amount"], 3 * rs))])
        sl, out = g.label("sl"), g.label("so")
        g.add(f"[{idx}:v]format=gbrp[{sl}]")
        g.add(f"[{cur}][{sl}]blend=all_mode=multiply:shortest=1[{out}]")
        cur = out
    if fx["vignette"]["amount"] > 0:
        e = fx["vignette"]
        idx = g.add_input(["-loop", "1", "-framerate", _f(fps), "-t", _f(duration + 0.2), "-i",
                           str(vignette_mask(w, h, e["amount"], e["feather"]))])
        vm, out = g.label("vm"), g.label("vo")
        g.add(f"[{idx}:v]format=gbrp[{vm}]")
        g.add(f"[{cur}][{vm}]blend=all_mode=multiply:shortest=1[{out}]")
        cur = out
    if fx["grain"]["amount"] > 0:
        e = fx["grain"]
        atlas = grain_atlas(w, h, max(1.0, e["size"] * rs))
        tw, th = max(2, math.ceil(w / max(1.0, e["size"] * rs))), max(2, math.ceil(h / max(1.0, e["size"] * rs)))
        idx = g.add_input(["-loop", "1", "-framerate", _f(fps), "-t", _f(duration + 0.2), "-i", str(atlas)])
        gn, out = g.label("gn"), g.label("go")
        amt = e["amount"] * 0.5  # full amount = +-64/255 around the pixel
        lv = f"'128+(val-128)*{_f(amt)}'"
        g.add(f"[{idx}:v]crop=w={tw}:h={th}:x=0:y='{th}*mod(n+{f0},{GRAIN_FRAMES})',scale={w}:{h}:flags=bilinear,"
              f"format=gbrp,lutrgb=r={lv}:g={lv}:b={lv}[{gn}]")
        g.add(f"[{cur}][{gn}]blend=all_mode=grainmerge:shortest=1[{out}]")
        cur = out
    step("format=yuv420p")
    return cur
