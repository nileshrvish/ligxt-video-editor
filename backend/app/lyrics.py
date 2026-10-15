"""Song lyrics captions from a music clip (Phase 13a).

There is no vocal separation (Demucs' weights are research-only, so they were not added; see PROGRESS.md):
everything works on the music clip's own audio (the full mix).

- Voice activity: faster-whisper's Silero VAD (MIT) per second over the clip's used range: an *estimate* on
  the full mix (instruments can trigger it, buried vocals can be missed).
- Auto: faster-whisper with word timestamps + VAD on the range (+ margin), "small" or "large-v3-turbo" (MIT,
  pinned in ai_models.CATALOG). Hallucination guard: low-probability words, unreliable segments and runs of
  evenly spaced repeated words (what Whisper produced on the reel's song in Phase 6.1) are dropped. The
  language is only forced when the user picks one.
- Paste lyrics: the user's exact text aligned to the audio by stable-ts (MIT) in the AI worker
  (`ai_worker/lyrics_align.py`), line breaks kept as caption lines; words that can't be aligned are
  interpolated between their neighbours and flagged.
- Tap to sync happens in the editor (no AI).
Source times map to timeline times through the clip's start, in point and speed; ramped, reversed or frozen
clips are skipped with a note, like speech captions.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import ai_bridge, ai_models
from . import motion as MO
from .config import FFMPEG
from .jobs import JobContext, jobs
from .media import load_meta, original_path
from .models import Project

router = APIRouter(tags=["lyrics"])

MARGIN = 1.0  # seconds of context beyond the clip's ends (words outside the clip are dropped)
SR = 16000
P_MIN = 0.35  # words below this probability are dropped (Auto)
INSTRUMENTAL = 0.03  # voice activity below this share of the range = instrumental
MODELS = {"small": "small", "large-v3-turbo": ai_models.LYRICS_MODEL}


# ---------------------------------------------------------------- the music clip
def music_clip(project: Project, clip_id: str | None, playhead: float = 0.0):
    """(clip, meta, note) for the lyrics source: the named clip, else the music clip under the playhead, else the first."""
    cands = [(c, t) for t in project.tracks if t.type == "music" and not t.muted for c in t.clips if c.mediaId]
    if not cands:
        return None, None, "There is no song on a music track."
    pick = next((c for c, _ in cands if c.id == clip_id), None) if clip_id else None
    if pick is None:
        pick = next((c for c, _ in cands if c.start <= playhead < c.start + MO.clip_duration(c.model_dump())), cands[0][0])
    m = load_meta(pick.mediaId)
    if not m or not m.get("hasAudio"):
        return None, None, "The music clip has no sound."
    if pick.reverse or pick.freezeFrame is not None or pick.speedRamp:
        return pick, m, "This music clip is reversed, frozen or speed-ramped, so its lyrics can't be timed."
    return pick, m, None


def source_range(c) -> tuple[float, float]:
    return float(c.inPoint), float(c.outPoint)


def to_timeline(c, src: float) -> float:
    """Source seconds -> timeline seconds for a plain (constant-speed, forward) clip."""
    return float(c.start) + (src - float(c.inPoint)) / float(c.speed or 1)


def extract(m: dict, a: float, b: float, out: Path) -> float:
    """16 kHz mono wav of source [a - MARGIN, b + MARGIN]; returns its start time (source seconds)."""
    s0 = max(0.0, a - MARGIN)
    s1 = min(float(m.get("duration") or b), b + MARGIN)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{s0:.3f}", "-t", f"{max(0.05, s1 - s0):.3f}", "-i", str(original_path(m)),
                    "-vn", "-ac", "1", "-ar", str(SR), str(out)], check=True, capture_output=True)
    return s0


# ---------------------------------------------------------------- voice activity
def voice_activity(wav: Path, s0: float, a: float, b: float) -> dict:
    """Per second of the clip's range: the share detected as voice (Silero VAD on the mix)."""
    from faster_whisper import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    audio = decode_audio(str(wav), sampling_rate=SR)
    spans = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=300, speech_pad_ms=100))
    n = max(1, int(math.ceil(b - a)))
    per = np.zeros(n)
    for sp in spans:
        x0, x1 = s0 + sp["start"] / SR, s0 + sp["end"] / SR
        for k in range(n):
            lo, hi = a + k, min(b, a + k + 1)
            per[k] += max(0.0, min(hi, x1) - max(lo, x0)) / max(1e-6, hi - lo)
    per = np.clip(per, 0, 1)
    return {"perSecond": [round(float(v), 2) for v in per], "share": round(float(per.mean()), 3),
            "spans": [[round(s0 + sp["start"] / SR, 3), round(s0 + sp["end"] / SR, 3)] for sp in spans]}


def trim_to_voice(st: float, en: float, spans: list) -> float:
    """A word that starts where the VAD hears no voice but ends inside a voiced span starts with that span instead
    (the aligner stretches a line's first word back over the intro when the music leaves no silence to cut)."""
    if not spans or any(x0 <= st <= x1 for x0, x1 in spans):
        return st
    for x0, x1 in spans:
        if st < x0 < en:
            return min(x0, en - 0.05)
    return st


# ---------------------------------------------------------------- hallucination guard
def _norm(w: str) -> str:
    return re.sub(r"[^\w']+", "", w.lower())


def reject_repeats(words: list[dict], min_run: int = 3, cv_max: float = 0.25) -> tuple[list[dict], int]:
    """Drop runs of the same word (or the same 2-word phrase) repeated min_run+ times at near-even spacing: Whisper's
    typical hallucination on music ("la la la la", "you you you"). Returns (kept words, dropped count)."""
    n = len(words)
    drop = [False] * n
    for period in (1, 2):
        i = 0
        while i + period < n:
            j = i
            while j + period < n and _norm(words[j + period]["text"]) == _norm(words[j]["text"]) and _norm(words[j]["text"]):
                j += 1
            reps = (j - i) // period + 1  # how many times the unit occurs
            if reps >= min_run:
                starts = [words[k]["start"] for k in range(i, j + period, period)]
                gaps = np.diff(starts)
                if len(gaps) >= 2 and gaps.mean() > 0 and gaps.std() / gaps.mean() < cv_max:
                    for k in range(i, min(n, j + period)):
                        drop[k] = True
                i = j + 1
            else:
                i += 1
    kept = [w for k, w in enumerate(words) if not drop[k]]
    return kept, n - len(kept)


def guard(segments, src0: float) -> tuple[list[dict], dict]:
    """Words (source seconds) from faster-whisper segments, minus the unreliable ones."""
    words, low, bad_seg = [], 0, 0
    for sg in segments:
        if getattr(sg, "no_speech_prob", 0) > 0.7 or getattr(sg, "avg_logprob", 0) < -1.2 or getattr(sg, "compression_ratio", 0) > 2.4:
            bad_seg += 1
            continue
        for w in sg.words or []:
            if not w.word.strip():
                continue
            if w.probability < P_MIN:
                low += 1
                continue
            words.append({"text": w.word.strip(), "start": src0 + w.start, "end": src0 + w.end, "p": round(float(w.probability), 3)})
    words, rep = reject_repeats(words)
    return words, {"lowProbability": low, "unreliableSegments": bad_seg, "repeatedRuns": rep}


# ---------------------------------------------------------------- the modes
def _whisper(model_key: str, ctx=None, p0=0.0, p1=0.2):
    from . import captions as CAP
    if model_key == "large-v3-turbo":
        path = str(ai_models.ensure_model(ai_models.LYRICS_MODEL, ctx, p0, p1))
    else:
        path = MODELS.get(model_key, "small")
    try:
        return CAP._model(path, "cuda"), "cuda"
    except Exception:  # noqa: BLE001
        return CAP._model(path, "cpu"), "cpu"


def run_auto(c, m: dict, model: str, language: str | None, ctx=None) -> dict:
    a, b = source_range(c)
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="ligxt_lyr_") as tmp:
        wav = Path(tmp) / "song.wav"
        s0 = extract(m, a, b, wav)
        if ctx:
            ctx.progress(0.05, "Listening for vocals")
        va = voice_activity(wav, s0, a, b)
        if os.environ.get("LIGXT_LYRICS_TEST"):  # automated tests without the speech model: VAD only
            return {"words": [], "voice": va, "instrumental": va["share"] < INSTRUMENTAL, "guard": {}, "seconds": round(time.time() - t0, 2)}
        wm, device = _whisper(model, ctx, 0.1, 0.3)
        if ctx:
            ctx.progress(0.35, "Transcribing the vocals")
        segs, info = wm.transcribe(str(wav), language=language or None, word_timestamps=True, vad_filter=True,
                                   condition_on_previous_text=False, temperature=0.0)
        segs = list(segs)
    words, g = guard(segs, s0)
    words = [w for w in words if a - 0.05 <= w["start"] < b]
    inst = va["share"] < INSTRUMENTAL or not words
    out = [{"text": w["text"], "start": round(to_timeline(c, w["start"]), 3), "end": round(to_timeline(c, min(w["end"], b)), 3), "p": w["p"]} for w in words]
    return {"words": out, "voice": va, "instrumental": inst, "guard": g, "language": info.language, "languageProb": round(float(info.language_probability), 3),
            "device": device, "model": model, "seconds": round(time.time() - t0, 2)}


def parse_lines(text: str) -> list[list[str]]:
    return [ln.split() for ln in (x.strip() for x in text.replace("\r", "").split("\n")) if ln.split()]


def interpolate_flagged(lines: list[list[dict]], a: float, b: float) -> int:
    """Words the aligner couldn't place (flag) get times evenly between their placed neighbours. Returns the count."""
    flat = [w for ln in lines for w in ln]
    n = 0
    i = 0
    while i < len(flat):
        if not flat[i].get("flag"):
            i += 1
            continue
        j = i
        while j < len(flat) and flat[j].get("flag"):
            j += 1
        lo = flat[i - 1]["end"] if i > 0 else a
        hi = flat[j]["start"] if j < len(flat) else b
        if hi <= lo:
            hi = lo + 0.25 * (j - i)
        step = (hi - lo) / (j - i)
        for k in range(i, j):
            flat[k]["start"] = round(lo + step * (k - i), 3)
            flat[k]["end"] = round(lo + step * (k - i + 1), 3)
            n += 1
        i = j
    return n


def run_align(c, m: dict, text: str, model: str, language: str | None, ctx=None) -> dict:
    a, b = source_range(c)
    lines_txt = parse_lines(text)
    if not lines_txt:
        raise ValueError("Paste the lyrics first (one line per caption).")
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="ligxt_lyr_") as tmp:
        wav = Path(tmp) / "song.wav"
        s0 = extract(m, a, b, wav)
        va = voice_activity(wav, s0, a, b)
        args = {"wav": str(wav), "lines": [" ".join(ln) for ln in lines_txt], "language": language, "device": ai_models.load_settings()["device"]}
        if os.environ.get("LIGXT_LYRICS_TEST"):
            args["mode"] = "test-align"
        else:
            if ai_models.ai_python() is None:
                raise RuntimeError("Aligning pasted lyrics needs the AI tools (ai-venv), which are not installed. Use Tap to sync instead.")
            args["model"] = str(ai_models.ensure_model(ai_models.LYRICS_MODEL, ctx, 0.05, 0.3))
        res = ai_bridge.call("align_lyrics", args, progress=(lambda p, msg: ctx.progress(0.3 + 0.65 * p, msg)) if ctx else None,
                             cancelled=ctx.cancelled if ctx else None)
    # worker times are seconds into the wav: -> source -> timeline; keep the user's words and lines
    lines = []
    for ln_words, ln_res in zip(lines_txt, res["lines"]):
        out = []
        for k, word in enumerate(ln_words):
            r = ln_res[k] if k < len(ln_res) else None
            ok = r is not None and r.get("end", 0) - r.get("start", 0) > 0.02 and r.get("p", 1) >= 0.05
            st = s0 + (r["start"] if r else 0.0)
            en = s0 + (r["end"] if r else 0.0)
            if ok:
                st = trim_to_voice(st, en, va.get("spans", []))
            out.append({"text": word, "start": round(to_timeline(c, st), 3), "end": round(to_timeline(c, en), 3),
                        "p": round(float(r.get("p", 0)) if r else 0.0, 3), "flag": not ok})
        lines.append(out)
    ta, tb = to_timeline(c, a), to_timeline(c, b)
    flagged = interpolate_flagged(lines, ta, tb)
    for ln in lines:  # inside the clip, in order
        for w in ln:
            w["start"] = round(min(max(w["start"], ta), tb - 0.05), 3)
            w["end"] = round(min(max(w["end"], w["start"] + 0.05), tb), 3)
    return {"lines": lines, "voice": va, "flagged": flagged, "device": res.get("device"), "seconds": round(time.time() - t0, 2),
            "language": res.get("language"), "instrumental": va["share"] < INSTRUMENTAL}


# ---------------------------------------------------------------- API
class LyricsBody(BaseModel):
    project: dict | None = None
    clipId: str | None = None
    playhead: float = 0.0
    mode: str = "auto"  # auto | align
    model: str = "large-v3-turbo"
    language: str | None = None
    text: str | None = None


@router.post("/api/projects/{pid}/lyrics")
def lyrics(pid: str, body: LyricsBody):
    from .projects import load_project
    if body.mode not in ("auto", "align"):
        raise HTTPException(400, "mode must be auto or align (Tap to sync needs no server).")
    if body.model not in MODELS:
        raise HTTPException(400, f"Unknown model '{body.model}'.")
    project = Project.model_validate(body.project) if body.project else load_project(pid)
    c, m, note = music_clip(project, body.clipId, body.playhead)
    if c is None or note:
        raise HTTPException(400, note or "No music clip.")

    def run(ctx: JobContext) -> dict:
        if body.mode == "auto":
            r = run_auto(c, m, body.model, body.language, ctx)
            nw = len(r["words"])
            if r["instrumental"]:
                r["message"] = ("No vocals in this part of the song (instrumental): voice activity "
                                f"{r['voice']['share'] * 100:.0f} %, {nw} reliable words. Paste the lyrics or tap them in instead.")
            else:
                r["message"] = f"{nw} words found ({r.get('language')}, {r.get('device', '').upper()})."
        else:
            r = run_align(c, m, body.text or "", body.model, body.language, ctx)
            nw = sum(len(ln) for ln in r["lines"])
            r["message"] = f"{nw} words in {len(r['lines'])} lines aligned; {r['flagged']} could not be placed and were spread out (flagged)."
            if r["instrumental"]:
                r["message"] += " Little or no voice detected in this part: check the timing, or use Tap to sync."
        r.update(clipId=c.id, mode=body.mode, mediaName=m.get("name"), range=[c.inPoint, c.outPoint])
        ctx.message(r["message"])
        return r

    job = jobs.submit("lyrics", "Song lyrics", run, queue="ai" if body.mode == "align" else "render", ref={"projectId": project.id})
    return {"jobId": job.id}
