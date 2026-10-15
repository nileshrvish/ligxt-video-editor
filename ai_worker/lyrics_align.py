"""Align the user's lyrics to a song (stable-ts, MIT, on faster-whisper large-v3-turbo; the user's lines are kept).

op `align_lyrics`: args wav (16 kHz mono), lines (one string per caption line), language (None = detect), model (the
CTranslate2 model folder), device, mode ("test-align" = no model: words spread over the loud parts, for tests).
Returns {"lines": [[{start, end, p} per word of that line]], "device", "seconds"} with times in seconds of the wav.
"""
from __future__ import annotations

import time
import wave

import numpy as np

_model = {"key": None, "m": None}


def _read(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    return x


def _test_align(wav: str, lines: list[str]) -> list[list[dict]]:
    """No model: the words, in order, spread evenly over the loudest parts of the audio (a voice over the music)."""
    x = _read(wav)
    hop = 160  # 10 ms
    env = np.sqrt(np.convolve(x ** 2, np.ones(hop) / hop, "same"))[::hop]
    voiced = env > max(1e-4, 0.6 * float(np.percentile(env, 99)))  # the loudest parts (a voice over the music)
    t = np.nonzero(voiced)[0] / 100.0
    words = [w for ln in lines for w in ln.split()]
    out, k = [], 0
    if len(t) < len(words):
        t = np.linspace(0, len(env) / 100.0, max(2, len(words) + 1))
    edges = np.linspace(0, len(t) - 1, len(words) + 1).astype(int)
    for ln in lines:
        row = []
        for _ in ln.split():
            a, b = float(t[edges[k]]), float(t[max(edges[k], edges[k + 1] - 1)]) + 0.01
            row.append({"start": round(a, 3), "end": round(max(b, a + 0.05), 3), "p": 0.9})
            k += 1
        out.append(row)
    return out


def align(ctx, args: dict) -> dict:
    t0 = time.time()
    lines = [str(s) for s in args["lines"]]
    if args.get("mode") == "test-align":
        return {"lines": _test_align(args["wav"], lines), "device": "cpu", "seconds": round(time.time() - t0, 3)}
    import torch
    import stable_whisper
    dev = "cuda" if args.get("device", "auto") != "cpu" and torch.cuda.is_available() else "cpu"
    key = (args["model"], dev)
    ctx.progress(0.05, "Loading the speech model", force=True)
    if _model["key"] != key:
        from matting import unload
        unload()  # one GPU model at a time
        _model["m"] = stable_whisper.load_faster_whisper(args["model"], device=dev, compute_type="float16" if dev == "cuda" else "int8")
        _model["key"] = key
    ctx.check()
    lang, lang_p = args.get("language") or None, None
    if not lang:  # stable-ts needs a language with faster-whisper: detect it from the song (never forced)
        from faster_whisper import decode_audio
        lang, lang_p, _ = _model["m"].detect_language(decode_audio(args["wav"], sampling_rate=16000))
    ctx.progress(0.2, "Aligning the lyrics to the song")
    res = _model["m"].align(args["wav"], "\n".join(lines), language=lang, original_split=True, fast_mode=False, verbose=None)
    ctx.check()
    # over a music bed there is no silence to cut, so the first word of a line can stretch back over the intro:
    # clip each word's start to 2.5x the median word length (the end, where the note is held, stays)
    res.clamp_max(medium_factor=2.5, clip_start=True)
    segs = res.segments
    out = []
    # original_split keeps one segment per line; fall back to re-splitting the word stream by the line lengths
    flat = [w for s in segs for w in s.words]
    k = 0
    for ln in lines:
        row = []
        for _ in ln.split():
            w = flat[k] if k < len(flat) else None
            k += 1
            row.append({"start": round(float(w.start), 3), "end": round(float(w.end), 3), "p": round(float(getattr(w, "probability", 0) or 0), 3)}
                       if w is not None else None)
        out.append(row)
    return {"lines": out, "device": dev, "seconds": round(time.time() - t0, 2), "language": lang,
            "languageProb": round(float(lang_p), 3) if lang_p is not None else None}


def unload() -> None:
    _model.update(key=None, m=None)
