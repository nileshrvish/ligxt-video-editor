"""Generate the bundled example LUTs in luts/ (CC0, see luts/LICENSE.md).

Run:  .venv\\Scripts\\python.exe scripts\\make_luts.py
Each LUT is a 33^3 .cube built from simple, documented colour formulas, so there are no
third-party licensing questions.
"""
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.grade import luma, smoothstep, write_cube  # noqa: E402

N = 33
OUT = ROOT / "luts"


def grid():
    v = np.linspace(0, 1, N)
    b, g, r = np.meshgrid(v, v, v, indexing="ij")
    return np.stack([r, g, b], axis=-1)


def s_curve(c, k):
    return np.clip(0.5 + (c - 0.5) * k - 0.25 * (k - 1) * np.sin(2 * np.pi * (c - 0.5)) / np.pi, 0, 1)


def teal_orange(c):
    Y = luma(c)[..., None]
    c = c + np.array([-0.05, 0.02, 0.07]) * (1 - smoothstep(0, 0.55, Y)) + np.array([0.08, 0.03, -0.06]) * smoothstep(0.45, 1, Y)
    return s_curve(np.clip(c, 0, 1), 1.12)


def bleach_bypass(c):
    Y = luma(c)[..., None]
    c = Y + (c - Y) * 0.45
    return s_curve(np.clip(c, 0, 1), 1.35)


def matte_fade(c):
    c = 0.07 + c * 0.86
    Y = luma(c)[..., None]
    return np.clip(Y + (c - Y) * 0.8 + np.array([0.01, 0.0, 0.02]), 0, 1)


def sunset_warm(c):
    c = np.clip(c * np.array([1.06, 1.0, 0.88]) + np.array([0.02, 0.005, -0.01]), 0, 1)
    Y = luma(c)[..., None]
    return np.clip(c + np.array([0.04, 0.02, -0.02]) * smoothstep(0.5, 1, Y), 0, 1)


LUTS = {
    "teal-orange": ("Teal & Orange", teal_orange),
    "bleach-bypass": ("Bleach Bypass", bleach_bypass),
    "matte-fade": ("Matte Fade", matte_fade),
    "sunset-warm": ("Sunset Warm", sunset_warm),
}

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for stem, (name, fn) in LUTS.items():
        write_cube(OUT / f"{stem}.cube", fn(grid()), title=name)
        (OUT / f"{stem}.json").write_text(json.dumps({"name": name, "license": "CC0-1.0"}), encoding="utf-8")
        print("wrote", stem)
