"""Separated vocals for song lyrics (Phase 13b).

The AI worker's `separate` op (python-audio-separator: Mel-Band RoFormer vocals, then optionally UVR DeEcho-DeReverb)
runs on the music clip's source range (+ margin). The vocal stem is cached as FLAC in
`cache/<media>/stems/<key>.flac`; the key covers the range, the models (their pinned SHA-256) and the de-reverb pass,
so a different section or setting makes a new stem and a repeat run is instant. Settings shows the stems' disk use and
can clear them.

Vocal presence = how loud the vocal stem is against the mix, per second of the clip's range (not a speech detector on
the full mix): 1 when the vocals are within 10 dB of the mix, 0 below -24 dB or under -60 dBFS.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from . import ai_bridge, ai_models, config
from .config import FFMPEG
from .media import original_path

SR = 44100
MARGIN = 1.0
VERSION = 1  # bump when the separation pipeline changes (invalidates cached stems)
PRESENT_DB, ABSENT_DB, SILENT_DBFS = -10.0, -24.0, -60.0


def stems_dir(media_id: str) -> Path:
    return config.CACHE_DIR / media_id / "stems"


def settings_steps(settings: dict | None = None) -> dict:
    s = settings or ai_models.load_settings()
    sep = ai_models.SEPARATORS.get(s.get("separator", "mel-roformer"))
    return {"separator": sep, "dereverb": bool(s.get("dereverb")) and sep is not None}


def stem_key(media_id: str, s0: float, s1: float, steps: dict, test: bool = False) -> str:
    parts = {"v": VERSION, "media": media_id, "range": [round(s0, 3), round(s1, 3)], "test": test}
    for k in ("separator", "dereverb"):
        mid = steps[k] if k == "separator" else (ai_models.DEREVERB_MODEL if steps[k] else None)
        parts[k] = [f["sha256"] for f in ai_models.CATALOG[mid]["files"]] if mid else None
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def extract_mix(m: dict, s0: float, s1: float, out: Path) -> None:
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{s0:.3f}", "-t", f"{max(0.05, s1 - s0):.3f}", "-i", str(original_path(m)),
                    "-vn", "-ac", "2", "-ar", str(SR), "-c:a", "pcm_s16le", str(out)], check=True, capture_output=True)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        return x.reshape(-1, w.getnchannels())


def decode(path: Path, sr: int = SR, channels: int = 2) -> np.ndarray:
    """Any audio file (the FLAC stems) -> float32 (samples, channels)."""
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "s16le", "-ac", str(channels), "-ar", str(sr), "-"],
                       check=True, capture_output=True)
    return (np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768).reshape(-1, channels)


def presence(mix: np.ndarray, voc: np.ndarray, s0: float, a: float, b: float) -> dict:
    """Per second of [a, b] (source seconds): vocal presence 0..1 from the stem's level against the mix."""
    n = max(1, int(math.ceil(b - a - 1e-6)))
    per, db = [], []
    for k in range(n):
        lo = int(round((a + k - s0) * SR))
        hi = int(round((min(b, a + k + 1) - s0) * SR))
        mv, vv = mix[max(0, lo):max(0, hi)], voc[max(0, lo):max(0, hi)]
        if len(mv) == 0:
            per.append(0.0)
            db.append(None)
            continue
        em, ev = float(np.mean(mv ** 2)) + 1e-12, float(np.mean(vv ** 2)) + 1e-12
        rel, lvl = 10 * math.log10(ev / em), 10 * math.log10(ev)
        p = 0.0 if lvl < SILENT_DBFS else min(1.0, max(0.0, (rel - ABSENT_DB) / (PRESENT_DB - ABSENT_DB)))
        per.append(round(p, 2))
        db.append(round(rel, 1))
    return {"perSecond": per, "share": round(float(np.mean(per)), 3), "relDb": db, "method": "stem"}


def voiced_spans(pres: dict, a: float, min_p: float = 0.25) -> list[list[float]]:
    """Source-time spans of consecutive seconds with vocal presence (for chunked transcription)."""
    out = []
    for k, p in enumerate(pres["perSecond"]):
        if p < min_p:
            continue
        if out and abs(out[-1][1] - (a + k)) < 1e-6:
            out[-1][1] = a + k + 1
        else:
            out.append([a + k, a + k + 1])
    return out


def vocal_stem(m: dict, a: float, b: float, ctx=None, p0: float = 0.0, p1: float = 0.3) -> dict | None:
    """The clip range's vocal stem (cached) + presence. None when separation is off or the AI tools are missing."""
    steps = settings_steps()
    test = bool(os.environ.get("LIGXT_LYRICS_TEST"))
    if steps["separator"] is None or (not test and ai_models.ai_python() is None):
        return None
    s0 = max(0.0, a - MARGIN)
    s1 = min(float(m.get("duration") or b), b + MARGIN)
    d = stems_dir(m["id"])
    key = stem_key(m["id"], s0, s1, steps, test)
    flac = d / f"{key}.flac"
    info_file = d / f"{key}.json"
    with tempfile.TemporaryDirectory(prefix="ligxt_stem_") as tmp:
        mix_wav = Path(tmp) / "mix.wav"
        extract_mix(m, s0, s1, mix_wav)
        mix = read_wav(mix_wav)
        cached = flac.exists() and info_file.exists()
        if cached:
            info = json.loads(info_file.read_text(encoding="utf-8"))
        else:
            voc_wav = Path(tmp) / "vocals.wav"
            args = {"wav": str(mix_wav), "out_vocals": str(voc_wav), "ffmpeg": FFMPEG,
                    "device": ai_models.load_settings()["device"], "budgetGB": ai_models.load_settings()["budgetGB"]}
            if test:
                args["mode"] = "test-separate"
            else:
                args["roformer"] = str(ai_models.ensure_model(steps["separator"], ctx, p0, p0 + (p1 - p0) * 0.3))
                if steps["dereverb"]:
                    args["dereverb"] = str(ai_models.ensure_model(ai_models.DEREVERB_MODEL, ctx, p0, p0 + (p1 - p0) * 0.4))
            if ctx:
                ctx.progress(p0 + (p1 - p0) * 0.4, "Separating the vocals from the music")
            res = ai_bridge.call("separate", args,
                                 progress=(lambda p, msg: ctx.progress(p0 + (p1 - p0) * (0.4 + 0.55 * p), msg)) if ctx else None,
                                 cancelled=ctx.cancelled if ctx else None)
            d.mkdir(parents=True, exist_ok=True)
            part = d / f"{key}.part.flac"
            subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(voc_wav), "-c:a", "flac", str(part)], check=True, capture_output=True)
            part.replace(flac)
            info = {"range": [s0, s1], "steps": res.get("steps"), "device": res.get("device"), "seconds": res.get("seconds"),
                    "peakVram": res.get("peakVram"), "separator": steps["separator"], "dereverb": steps["dereverb"]}
            info_file.write_text(json.dumps(info), encoding="utf-8")
        voc = decode(flac)
        n = min(len(voc), len(mix))
        pres = presence(mix[:n], voc[:n], s0, a, b)
    return {"path": flac, "s0": s0, "presence": pres, "cached": cached, **info}
