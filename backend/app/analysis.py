"""Media analysis for templates: a song's energy curve and drop, a photo's accent colour."""
from __future__ import annotations

import json
import subprocess

import numpy as np

from . import config
from .beats import decode
from .config import CACHE_DIR
from .media import original_path

ENERGY_RATE = 10  # curve points per second
ENERGY_VERSION = 2


def energy(meta: dict) -> dict:
    """Loudness over time (0..1, ENERGY_RATE points/s; low end weighted like a dance floor hears it)
    and the most likely drop. Cached per media."""
    out = CACHE_DIR / meta["id"] / f"energy_v{ENERGY_VERSION}.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    from .beats import SR
    y = decode(str(original_path(meta)))
    hop = SR // ENERGY_RATE
    n = len(y) // hop
    if n < 4:
        return {"rate": ENERGY_RATE, "curve": [], "drop": None, "candidates": []}
    frames = y[: n * hop].reshape(n, hop)
    # low band (kick + bass) matters most for "the drop"
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(original_path(meta)), "-map", "0:a:0",
                        "-ac", "1", "-ar", str(SR), "-af", "lowpass=f=180,lowpass=f=180", "-f", "f32le", "-"], capture_output=True, timeout=600)
    lp = np.frombuffer(r.stdout, np.float32)
    lp = np.pad(lp, (0, max(0, n * hop - len(lp))))[: n * hop]
    low = np.sqrt((lp.reshape(n, hop) ** 2).mean(axis=1))
    full = np.sqrt((frames ** 2).mean(axis=1))
    e = 0.6 * low / max(1e-6, low.max()) + 0.4 * full / max(1e-6, full.max())
    raw = e / max(1e-6, float(e.max()))
    k = max(1, ENERGY_RATE // 2)
    e = np.convolve(e, np.ones(k) / k, mode="same")
    e = e / max(1e-6, float(e.max()))
    res = {"rate": ENERGY_RATE, "curve": [round(float(v), 3) for v in e]}
    res.update(find_drop(e, meta.get("beats") or [], raw))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res), encoding="utf-8")
    return res


def find_drop(e: np.ndarray, beats: list[float], raw: np.ndarray | None = None) -> dict:
    """The beat where energy jumps the most and stays up (8 beats after vs 8 before), preferring
    bar starts. Returns {"drop": seconds | None, "candidates": [{"time", "score"}]}."""
    if len(beats) < 20:
        return {"drop": None, "candidates": []}
    t = np.arange(len(e)) / ENERGY_RATE

    def mean_between(a: float, b: float) -> float:
        m = (t >= a) & (t < b)
        return float(e[m].mean()) if m.any() else 0.0

    scores = []
    for i in range(8, len(beats) - 8):
        before = mean_between(beats[i - 8], beats[i])
        after = mean_between(beats[i], beats[i + 8])
        s = after - before
        if after < 0.45:
            s *= after / 0.45  # a drop is loud afterwards
        scores.append((s, i))
    scores.sort(reverse=True)
    picked: list[tuple[float, int]] = []
    for s, i in scores:
        if s <= 0.04:
            break
        if all(abs(i - j) >= 8 for _, j in picked):
            picked.append((s, i))
        if len(picked) == 3:
            break
    # the smoothed curve finds the region; the onset is the beat with the sharpest rise (unsmoothed, +-2 beats)
    r = raw if raw is not None else e

    def rise(j: int) -> float:
        m_after = (t >= beats[j]) & (t < beats[min(len(beats) - 1, j + 2)])
        m_before = (t >= beats[max(0, j - 2)]) & (t < beats[j])
        return float(r[m_after].mean() - r[m_before].mean()) if m_after.any() and m_before.any() else -1.0
    picked = [(s, max(range(max(2, i - 2), min(len(beats) - 2, i + 3)), key=rise)) for s, i in picked]
    cands = [{"time": round(float(beats[i]), 3), "score": round(s, 3), "beat": i} for s, i in picked]
    return {"drop": cands[0]["time"] if cands else None, "dropBeat": cands[0]["beat"] if cands else None, "candidates": cands}


# ---------------------------------------------------------------- accent colour
def _small_rgb(meta: dict, side: int = 160) -> np.ndarray | None:
    w, h = float(meta.get("width") or 1), float(meta.get("height") or 1)
    k = side / max(w, h)
    sw, sh = max(2, round(w * k)), max(2, round(h * k))
    args = [config.FFMPEG, "-v", "error"]
    if meta.get("type") == "video":
        args += ["-ss", f"{float(meta.get('duration') or 0) / 2:.2f}"]
    args += ["-i", str(original_path(meta)), "-vf", f"scale={sw}:{sh}", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = subprocess.run(args, capture_output=True)
    if r.returncode or len(r.stdout) != sw * sh * 3:
        return None
    return np.frombuffer(r.stdout, np.uint8).reshape(sh, sw, 3) / 255.0


def accent(meta: dict) -> dict | None:
    """The most striking accent colour: a saturated, bright hue that covers a small-to-medium part of
    the picture (an object, clothing, a sign), not the dominant background. {"hue", "area", "score"}."""
    from .grade import hue_sat
    c = _small_rgb(meta)
    if c is None:
        return None
    h, s = hue_sat(c)
    v = c.max(axis=-1)
    strong = (s > 0.45) & (v > 0.3)
    if strong.sum() < 10:
        return None
    w = (s * v)[strong]
    hh = h[strong]
    bins = np.zeros(36)
    np.add.at(bins, (hh // 10).astype(int) % 36, w)
    bins = np.convolve(np.r_[bins[-1:], bins, bins[:1]], [0.25, 0.5, 0.25], mode="valid")
    total = float(c.shape[0] * c.shape[1])
    best = None
    for b in range(36):
        if bins[b] <= 0:
            continue
        dh = np.abs(((hh - (b * 10 + 5)) + 180) % 360 - 180)
        m = dh < 15
        area = float(m.sum()) / total
        if area < 0.002:
            continue
        # accent, not background: full credit up to 20 % of the frame, none above 35 % (keeping a colour
        # that fills the frame would leave almost nothing black and white)
        if area > 0.35:
            continue
        shape = 1.0 if area <= 0.2 else 1 - (area - 0.2) / 0.15 * 0.85
        score = float(bins[b]) / total * shape * float(np.mean(s[strong][m]) if m.any() else 0)
        if best is None or score > best["score"]:
            hue = float(np.degrees(np.arctan2(np.mean(np.sin(np.radians(hh[m]))), np.mean(np.cos(np.radians(hh[m])))))) % 360
            best = {"hue": round(hue, 1), "area": round(area, 4), "score": round(score, 5)}
    return best


def accent_preview(meta: dict, params: dict, out_path) -> None:
    """A small picture of `meta` with the selective-colour filter (params) applied."""
    from PIL import Image
    from .grade import selective
    c = _small_rgb(meta, 360)
    if c is None:
        raise ValueError("Could not read the picture.")
    Image.fromarray((np.clip(selective(c, params), 0, 1) * 255).round().astype(np.uint8)).save(out_path, quality=88)
