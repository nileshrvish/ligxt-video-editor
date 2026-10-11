"""Generate the bundled sound effects in sfx/ (CC0, see sfx/LICENSE.md).

Run:  .venv\\Scripts\\python.exe scripts\\make_sfx.py
Everything is synthesised from noise and sine waves with numpy, so there are no licensing questions.
Each effect's sidecar JSON records `peak` (seconds): the moment that should land on the cut.
"""
import json
import wave
from pathlib import Path

import numpy as np

SR = 48000
OUT = Path(__file__).resolve().parents[1] / "sfx"
rng = np.random.default_rng(7)


def lowpass_sweep(x: np.ndarray, cutoff_hz: np.ndarray) -> np.ndarray:
    """One-pole low-pass with a time-varying cutoff (Hz per sample)."""
    y = np.zeros_like(x)
    acc = 0.0
    a = 1 - np.exp(-2 * np.pi * cutoff_hz / SR)
    for i in range(len(x)):
        acc += a[i] * (x[i] - acc)
        y[i] = acc
    return y


def whoosh(dur: float, peak: float) -> np.ndarray:
    n = int(SR * dur)
    t = np.arange(n) / SR
    env = np.exp(-((t - peak) / (0.28 * dur)) ** 2)
    cut = 300 + 5200 * env  # opens up as it passes
    noise = rng.standard_normal(n)
    lp = lowpass_sweep(noise, cut)
    hp = lp - lowpass_sweep(lp, np.full(n, 180.0))  # remove rumble
    l = hp * env
    pan = np.clip((t - peak) / (0.6 * dur), -1, 1)  # sweeps left to right
    st = np.stack([l * (1 - 0.5 * pan), l * (1 + 0.5 * pan)], axis=1)
    return st / np.abs(st).max() * 0.8


def hit(dur: float = 1.4) -> np.ndarray:
    n = int(SR * dur)
    t = np.arange(n) / SR
    f = 42 + 70 * np.exp(-t * 18)  # pitch drop
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 3.2)
    click = rng.standard_normal(n) * np.exp(-t * 90) * 0.6
    snap = lowpass_sweep(click, np.full(n, 3500.0))
    x = boom + snap
    x = np.tanh(1.6 * x) / np.tanh(1.6)
    return np.stack([x, x], axis=1) / np.abs(x).max() * 0.85


def riser(dur: float = 1.6) -> np.ndarray:
    n = int(SR * dur)
    t = np.arange(n) / SR
    u = t / dur
    env = u ** 2.2 * (1 - np.exp(-(1 - u) * 60))
    tone = np.sin(2 * np.pi * np.cumsum(180 + 900 * u ** 2) / SR) * 0.35
    noise = lowpass_sweep(rng.standard_normal(n), 400 + 6000 * u ** 2) * 0.9
    x = (tone + noise) * env
    return np.stack([x, x], axis=1) / np.abs(x).max() * 0.8


def write(name: str, label: str, data: np.ndarray, peak: float) -> None:
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(str(OUT / f"{name}.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    (OUT / f"{name}.json").write_text(json.dumps({"name": label, "peak": peak, "license": "CC0-1.0"}), encoding="utf-8")
    print("wrote", name)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    write("whoosh", "Whoosh", whoosh(0.8, 0.45), 0.45)
    write("whoosh-fast", "Whoosh (fast)", whoosh(0.45, 0.25), 0.25)
    write("hit", "Hit / boom", hit(), 0.0)
    write("riser", "Riser", riser(), 1.6)
