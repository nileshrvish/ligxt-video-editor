"""Beat detection for music clips (numpy only).

1. Decode mono 22.05 kHz audio with ffmpeg.
2. Onset strength = half-wave-rectified spectral flux of the log-magnitude STFT.
3. Tempo = strongest autocorrelation peak of the onset envelope in 60-180 BPM
   (weighted towards ~120 BPM to avoid half/double-tempo picks).
4. Beat times = dynamic-programming beat tracker (Ellis 2007): maximise onset strength
   at beats while keeping inter-beat intervals close to the tempo period.
"""
from __future__ import annotations

import subprocess

import numpy as np

from . import config
from .ffmpeg_utils import CREATE_NO_WINDOW, FFmpegError

SR = 22050
N_FFT = 2048
HOP = 512
FPS = SR / HOP  # onset envelope frames per second (~43)


def decode(path: str, max_seconds: float = 900) -> np.ndarray:
    r = subprocess.run(
        [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", path, "-map", "0:a:0",
         "-t", str(max_seconds), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True, timeout=600, creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise FFmpegError(f"Could not decode audio for beat detection: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    return np.frombuffer(r.stdout, dtype=np.float32)


def onset_envelope(y: np.ndarray) -> np.ndarray:
    if len(y) < N_FFT:
        return np.zeros(1)
    n_frames = 1 + (len(y) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = y[idx] * np.hanning(N_FFT)[None, :].astype(np.float32)
    mag = np.abs(np.fft.rfft(frames, axis=1))
    logmag = np.log1p(100 * mag)
    flux = np.maximum(0, np.diff(logmag, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    # Remove slow trend (local mean) and normalise.
    win = int(FPS)  # ~1 s
    kernel = np.ones(win) / win
    flux = np.maximum(0, flux - np.convolve(flux, kernel, mode="same"))
    sd = flux.std()
    return flux / sd if sd > 0 else flux


def estimate_tempo(env: np.ndarray, lo_bpm: float = 60, hi_bpm: float = 180) -> float:
    env = env - env.mean()
    n = len(env)
    if n < 4 * FPS:
        return 120.0
    ac = np.correlate(env, env, mode="full")[n - 1:]
    ac /= ac[0] if ac[0] else 1
    lags = np.arange(len(ac))
    min_lag = int(FPS * 60 / hi_bpm)
    max_lag = min(len(ac) - 1, int(FPS * 60 / lo_bpm))
    if max_lag <= min_lag:
        return 120.0
    cand = lags[min_lag:max_lag + 1]
    bpm = 60 * FPS / cand
    # Log-Gaussian prior around 120 BPM (as in librosa) resolves octave ambiguity.
    prior = np.exp(-0.5 * (np.log2(bpm / 120.0) / 1.0) ** 2)
    score = ac[min_lag:max_lag + 1] * prior
    best = int(cand[np.argmax(score)])
    # Parabolic interpolation for sub-frame precision.
    if 1 <= best < len(ac) - 1:
        a, b, c = ac[best - 1], ac[best], ac[best + 1]
        denom = a - 2 * b + c
        shift = 0.5 * (a - c) / denom if denom else 0.0
    else:
        shift = 0.0
    return float(60 * FPS / (best + shift))


def track_beats(env: np.ndarray, bpm: float, tightness: float = 100.0) -> np.ndarray:
    period = 60 * FPS / bpm
    n = len(env)
    score = env.astype(np.float64).copy()
    backlink = -np.ones(n, dtype=np.int64)
    lo, hi = int(round(period / 2)), int(round(2 * period))
    for t in range(n):
        start, end = max(0, t - hi), t - lo
        if end <= start:
            continue
        prev = np.arange(start, end)
        penalty = -tightness * np.log((t - prev) / period) ** 2
        cand = score[prev] + penalty
        k = int(np.argmax(cand))
        if cand[k] > 0:
            score[t] = env[t] + cand[k]
            backlink[t] = prev[k]
    # Start from the best-scoring frame in the last period and follow the links back.
    tail = max(0, n - int(period) - 1)
    t = tail + int(np.argmax(score[tail:]))
    beats = []
    while t >= 0:
        beats.append(t)
        t = backlink[t]
    beats = np.array(beats[::-1])
    # Drop weak leading/trailing beats (silence at the start/end).
    if len(beats):
        strength = env[beats]
        thresh = 0.25 * np.median(strength[strength > 0]) if np.any(strength > 0) else 0
        keep = np.where(strength > thresh)[0]
        if len(keep):
            beats = beats[keep[0]: keep[-1] + 1]
    return beats


def detect(path: str) -> dict:
    y = decode(path)
    if len(y) < SR:
        return {"bpm": 0.0, "beats": []}
    env = onset_envelope(y)
    bpm = estimate_tempo(env)
    frames = track_beats(env, bpm)
    # Spectral flux peaks once an onset is inside most of the analysis window, i.e. about
    # (N_FFT - HOP) samples after the frame start (measured: 70 ms early without this).
    times = frames / FPS + (N_FFT - HOP) / SR
    return {"bpm": round(bpm, 1), "beats": [round(float(t), 3) for t in times]}
