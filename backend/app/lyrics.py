"""Song lyrics captions from a music clip (Phase 13a; vocal separation since Phase 13b).

- Vocals: the clip's range is separated first (`stems.py`: Mel-Band RoFormer, then UVR DeEcho-DeReverb when on),
  and vocal presence per second is the vocal stem's level against the mix. Without the AI tools, or with the
  separator off in Settings, everything falls back to the full mix with Silero VAD (Phase 13a: an estimate that
  misses sung vocals under loud music).
- Auto: faster-whisper ("small", "large-v3-turbo" or "large-v3", MIT, pinned in ai_models.CATALOG) on the vocal
  stem, chunk by chunk over the seconds with vocals, no conditioning on previous text, temperature fallback and a
  short lyrics prompt. Hallucination guard: low-probability words, unreliable segments and runs of evenly spaced
  repeated words are dropped. The language is only forced when the user picks one.
- Paste lyrics: the user's exact text aligned to the vocal stem (or the mix) by stable-ts (MIT) in the AI worker
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
from . import stems as ST
from .config import FFMPEG
from .jobs import JobContext, jobs
from .media import load_meta, original_path
from .models import Project

router = APIRouter(tags=["lyrics"])

MARGIN = 1.0  # seconds of context beyond the clip's ends (words outside the clip are dropped)
SR = 16000
P_MIN = 0.35  # words below this probability are dropped (Auto)
INSTRUMENTAL = 0.03  # voice activity below this share of the range = instrumental
MODELS = {"small": "small", "large-v3-turbo": ai_models.LYRICS_MODEL, "large-v3": ai_models.LYRICS_MODEL_V3}
TEMPERATURES = [0.0, 0.2, 0.4, 0.6, 0.8]  # fallback when a chunk decodes badly (compression / log-prob thresholds)
PROMPTS = {None: "Song lyrics.", "hi": "गीत के बोल", "es": "Letra de la canción.",
           "fr": "Paroles de la chanson.", "de": "Liedtext.", "pt": "Letra da música."}
CHUNK_MAX = 25.0  # seconds per transcription chunk (Whisper's window is 30 s)


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
    if model_key in ("large-v3-turbo", "large-v3"):
        path = str(ai_models.ensure_model(MODELS[model_key], ctx, p0, p1))
    else:
        path = MODELS.get(model_key, "small")
    try:
        return CAP._model(path, "cuda"), "cuda"
    except Exception:  # noqa: BLE001
        return CAP._model(path, "cpu"), "cpu"


def chunks(spans: list[list[float]], gap: float = 1.5, pad: float = 0.3, max_len: float = CHUNK_MAX) -> list[list[float]]:
    """Voiced spans -> transcription chunks: close gaps under `gap` s, pad the ends, split anything longer than max_len."""
    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] < gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    out = []
    for a, b in merged:
        a, b = a - pad, b + pad
        n = max(1, math.ceil((b - a) / max_len))
        step = (b - a) / n
        out += [[a + k * step, a + (k + 1) * step] for k in range(n)]
    return out


def _vad_voice(wav16: Path, s0: float, a: float, b: float) -> dict:
    va = voice_activity(wav16, s0, a, b)
    va["method"] = "vad"
    return va


def _stem_wav16(stem: dict, out: Path) -> None:
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(stem["path"]), "-ac", "1", "-ar", str(SR), str(out)], check=True, capture_output=True)


def _sep_info(stem: dict | None) -> dict | None:
    return {k: stem.get(k) for k in ("separator", "dereverb", "cached", "device", "steps")} if stem else None


def run_auto(c, m: dict, model: str, language: str | None, ctx=None) -> dict:
    a, b = source_range(c)
    t0 = time.time()
    stem = ST.vocal_stem(m, a, b, ctx, 0.02, 0.4)
    with tempfile.TemporaryDirectory(prefix="ligxt_lyr_") as tmp:
        wav = Path(tmp) / "song.wav"
        if stem:
            s0 = stem["s0"]
            _stem_wav16(stem, wav)
            voice = stem["presence"]
        else:
            s0 = extract(m, a, b, wav)
            if ctx:
                ctx.progress(0.05, "Listening for vocals")
            voice = _vad_voice(wav, s0, a, b)
        base = {"voice": voice, "separation": _sep_info(stem)}
        if os.environ.get("LIGXT_LYRICS_TEST"):  # automated tests without the speech model: one placeholder word per voiced second
            inst = voice["share"] < INSTRUMENTAL
            fake = [] if inst else [{"text": f"w{k + 1}", "start": round(to_timeline(c, a + k + 0.1), 3), "end": round(to_timeline(c, a + k + 0.8), 3), "p": 0.9}
                                    for k, p in enumerate(voice["perSecond"]) if p >= 0.25]
            return {"words": fake, **base, "instrumental": inst, "guard": {}, "language": language or "en", "seconds": round(time.time() - t0, 2)}
        if voice["share"] < INSTRUMENTAL:
            return {"words": [], **base, "instrumental": True, "guard": {}, "seconds": round(time.time() - t0, 2)}
        wm, device = _whisper(model, ctx, 0.4, 0.5)
        from faster_whisper import decode_audio
        audio = decode_audio(str(wav), sampling_rate=SR)
        todo = chunks(ST.voiced_spans(voice, a)) if stem else [[s0, s0 + len(audio) / SR]]
        segs, info = [], None
        for k, (ca, cb) in enumerate(todo):
            if ctx:
                if ctx.cancelled():
                    from .ffmpeg_utils import Cancelled
                    raise Cancelled()
                ctx.progress(0.5 + 0.45 * k / max(1, len(todo)), f"Transcribing the vocals {k + 1}/{len(todo)}")
            lo, hi = max(0, int((ca - s0) * SR)), min(len(audio), int((cb - s0) * SR))
            if hi - lo < SR // 4:
                continue
            got, info = wm.transcribe(audio[lo:hi], language=language or None, word_timestamps=True, vad_filter=not stem,
                                      condition_on_previous_text=False, temperature=TEMPERATURES,
                                      initial_prompt=PROMPTS.get(language, PROMPTS[None]))
            segs += [(s0 + lo / SR, sg) for sg in got]
    words, g = [], {"lowProbability": 0, "unreliableSegments": 0, "repeatedRuns": 0}
    for off, sg in segs:
        w, gg = guard([sg], off)
        words += w
        for kk in ("lowProbability", "unreliableSegments"):
            g[kk] += gg[kk]
    words, g["repeatedRuns"] = reject_repeats(sorted(words, key=lambda w: w["start"]))
    words = [w for w in words if a - 0.05 <= w["start"] < b]
    inst = voice["share"] < INSTRUMENTAL or (not words and not stem)
    out = [{"text": w["text"], "start": round(to_timeline(c, w["start"]), 3), "end": round(to_timeline(c, min(w["end"], b)), 3), "p": w["p"]} for w in words]
    return {"words": out, **base, "instrumental": inst, "guard": g, "chunks": len(todo),
            "language": info.language if info else language, "languageProb": round(float(info.language_probability), 3) if info else None,
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
    stem = ST.vocal_stem(m, a, b, ctx, 0.02, 0.3)
    with tempfile.TemporaryDirectory(prefix="ligxt_lyr_") as tmp:
        wav = Path(tmp) / "song.wav"
        if stem:
            s0 = stem["s0"]
            _stem_wav16(stem, wav)
            va = dict(stem["presence"], spans=ST.voiced_spans(stem["presence"], a, 0.5))
        else:
            s0 = extract(m, a, b, wav)
            va = _vad_voice(wav, s0, a, b)
        args = {"wav": str(wav), "lines": [" ".join(ln) for ln in lines_txt], "language": language, "device": ai_models.load_settings()["device"]}
        if os.environ.get("LIGXT_LYRICS_TEST"):
            args["mode"] = "test-align"
        else:
            if ai_models.ai_python() is None:
                raise RuntimeError("Aligning pasted lyrics needs the AI tools (ai-venv), which are not installed. Use Tap to sync instead.")
            args["model"] = str(ai_models.ensure_model(MODELS["large-v3" if model == "large-v3" else "large-v3-turbo"], ctx, 0.3, 0.35))
        res = ai_bridge.call("align_lyrics", args, progress=(lambda p, msg: ctx.progress(0.35 + 0.6 * p, msg)) if ctx else None,
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
    va.pop("spans", None)
    return {"lines": lines, "voice": va, "flagged": flagged, "device": res.get("device"), "seconds": round(time.time() - t0, 2),
            "separation": _sep_info(stem),
            "language": res.get("language"), "instrumental": va["share"] < INSTRUMENTAL}


def run_lrc(c, m: dict, lrc_text: str, model: str, language: str | None, ctx=None, source: dict | None = None) -> dict:
    """Synced lyrics (LRC) -> the clip's lines: find where the clip sits in the song, then time the words with stable-ts
    on the vocal stem; lines the aligner can't place keep the LRC's own line timing (words spread over the line)."""
    from . import lrc as LRC
    a, b = source_range(c)
    t0 = time.time()
    parsed = LRC.parse(lrc_text)
    spans = LRC.line_spans(parsed, (source or {}).get("duration"))
    if not spans:
        raise ValueError("These lyrics have no time tags (not synced). Paste them as plain lyrics instead.")
    stem = ST.vocal_stem(m, a, b, ctx, 0.02, 0.3)
    if stem:
        pres = stem["presence"]
    else:
        with tempfile.TemporaryDirectory(prefix="ligxt_lyr_") as tmp:
            wav = Path(tmp) / "song.wav"
            s0 = extract(m, a, b, wav)
            pres = _vad_voice(wav, s0, a, b)
    hint = []
    if not os.environ.get("LIGXT_LYRICS_TEST") and pres["share"] >= INSTRUMENTAL:
        if ctx:
            ctx.progress(0.32, "Listening for a few words to find the place in the song")
        r = run_auto(c, m, model, language, None)
        hint = [dict(w, start=float(c.inPoint) + (w["start"] - float(c.start)) * float(c.speed or 1)) for w in r["words"]]
    dur = float(m.get("duration") or b)
    song_len = max(e for _, e, _ in spans)
    loc = LRC.locate(spans, pres["perSecond"], a, hint, max_offset=max(0.0, song_len - (b - a)) if song_len > dur + 3 else 0.0)
    if song_len <= dur + 3 and abs(loc["offset"]) > 0.0:
        # the file is the whole song: the LRC's times are already the media's (only a small lead-in drift is allowed)
        loc = LRC.locate(spans, pres["perSecond"], a, hint, max_offset=0.0) | {"note": "whole song"}
    sel = LRC.lines_in_range(spans, loc["offset"], a, b)
    if not sel:
        raise ValueError("None of these lyrics fall inside this music clip (check the song, or the clip's range).")
    if ctx:
        ctx.progress(0.4, f"Timing {len(sel)} lines to the vocals")
    al = run_align(c, m, "\n".join(x["text"] for x in sel), model, language, ctx)
    lines, kept_lrc = [], 0
    for ln, x in zip(al["lines"], sel):
        ts, te = to_timeline(c, x["start"]), to_timeline(c, x["end"])
        bad = sum(1 for w in ln if w.get("flag")) > len(ln) / 2 or abs(ln[0]["start"] - ts) > 2.0
        if bad:  # the LRC's line timing, words spread evenly
            step = max(0.05, (te - ts) / max(1, len(ln)))
            ln = [dict(w, start=round(ts + k * step, 3), end=round(ts + (k + 1) * step, 3), flag=True) for k, w in enumerate(ln)]
            kept_lrc += 1
        lines.append(ln)
    return {"lines": lines, "voice": al["voice"], "flagged": sum(1 for ln in lines for w in ln if w.get("flag")),
            "lrc": {"offset": loc["offset"], "method": loc["method"], "score": loc["score"], "lines": len(sel), "lrcTimed": kept_lrc,
                    "hintWords": len(hint), **({k: source[k] for k in ("id", "artist", "title") if k in source} if source else {})},
            "separation": al.get("separation"), "device": al.get("device"), "language": al.get("language"),
            "instrumental": False, "seconds": round(time.time() - t0, 2)}


# ---------------------------------------------------------------- API
class LyricsBody(BaseModel):
    project: dict | None = None
    clipId: str | None = None
    playhead: float = 0.0
    mode: str = "auto"  # auto | align | lrc
    model: str = "large-v3-turbo"
    language: str | None = None
    text: str | None = None
    lrclibId: int | None = None  # lrc mode: fetched from LRCLIB inside the job (opt-in, needs internet)
    lrc: str | None = None  # lrc mode: or LRC text supplied by the user


@router.post("/api/projects/{pid}/lyrics")
def lyrics(pid: str, body: LyricsBody):
    from .projects import load_project
    if body.mode not in ("auto", "align", "lrc"):
        raise HTTPException(400, "mode must be auto, align or lrc (Tap to sync needs no server).")
    if body.mode == "lrc" and not body.lrclibId and not (body.lrc or "").strip():
        raise HTTPException(400, "Choose synced lyrics from the search, or paste LRC text.")
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
            what = "vocal presence" if r["voice"].get("method") == "stem" else "voice activity (estimated on the full mix)"
            if r["instrumental"]:
                r["message"] = ("No vocals in this part of the song (instrumental): "
                                f"{what} {r['voice']['share'] * 100:.0f} %, {nw} reliable words. Paste the lyrics or tap them in instead.")
            elif not nw:
                r["message"] = (f"Vocals are there ({what} {r['voice']['share'] * 100:.0f} %) but no words could be recognised reliably. "
                                "Try Best accuracy, pick the language, or paste / tap the lyrics.")
            else:
                r["message"] = f"{nw} words found ({r.get('language')}, {r.get('device', '').upper()}; {what} {r['voice']['share'] * 100:.0f} %)."
        elif body.mode == "lrc":
            from . import lrc as LRC
            src = None
            text = body.lrc or ""
            if body.lrclibId:
                ctx.progress(0.01, "Fetching the synced lyrics from LRCLIB")
                src = LRC.fetch(body.lrclibId)
                if src["instrumental"] or not src["synced"]:
                    raise ValueError("LRCLIB has no synced lyrics for this entry (instrumental or plain text only).")
                text = src["synced"]
            r = run_lrc(c, m, text, body.model, body.language, ctx, src)
            nw = sum(len(ln) for ln in r["lines"])
            lr = r["lrc"]
            r["message"] = (f"{nw} words in {len(r['lines'])} lines from the synced lyrics; the clip starts {lr['offset']:+.1f} s into the "
                            f"lyrics' timing ({lr['method']}). {lr['lrcTimed']} line(s) kept the lyrics' own line timing.")
        else:
            r = run_align(c, m, body.text or "", body.model, body.language, ctx)
            nw = sum(len(ln) for ln in r["lines"])
            r["message"] = f"{nw} words in {len(r['lines'])} lines aligned; {r['flagged']} could not be placed and were spread out (flagged)."
            if r["instrumental"]:
                r["message"] += " Little or no voice detected in this part: check the timing, or use Tap to sync."
        r.update(clipId=c.id, mode=body.mode, mediaName=m.get("name"), range=[c.inPoint, c.outPoint])
        ctx.message(r["message"])
        return r

    job = jobs.submit("lyrics", "Song lyrics", run, queue="ai", ref={"projectId": project.id})  # separation uses the GPU worker
    return {"jobId": job.id}


@router.get("/api/lyrics/tags/{media_id}")
def lyrics_tags(media_id: str):
    """Artist / title from the song file's tags (to prefill the synced-lyrics search). Local only."""
    from . import lrc as LRC
    m = load_meta(media_id)
    if not m:
        raise HTTPException(404, "Media not found.")
    t = LRC.media_tags(str(original_path(m)))
    if not t["title"]:
        t["title"] = Path(m.get("name") or "").stem
    return t


@router.get("/api/lyrics/search")
def lyrics_search(title: str, artist: str = "", album: str = ""):
    """Opt-in: asks LRCLIB (internet) for synced lyrics; only the artist / title / album are sent. Returns candidates
    without the lyrics text (that is fetched inside the lyrics job and only ends up in the project's text clips)."""
    from . import lrc as LRC
    if not title.strip():
        raise HTTPException(400, "Type the song title.")
    try:
        return {"results": LRC.search(artist, title, album)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"LRCLIB could not be reached ({type(e).__name__}). Check the internet connection, or paste the lyrics.") from e


# ---------------------------------------------------------------- translation (Phase 13b)
# targets offered (both M2M100 and MADLAD-400 cover these)
LANGUAGES = {"en": "English", "hi": "Hindi", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese", "it": "Italian",
             "ru": "Russian", "ar": "Arabic", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "bn": "Bengali", "ta": "Tamil",
             "ur": "Urdu", "mr": "Marathi", "gu": "Gujarati", "tr": "Turkish", "id": "Indonesian", "he": "Hebrew", "th": "Thai"}


def guess_language(lines: list[str]) -> str:
    """Source language from the script when nobody told us (M2M100 needs one): Devanagari -> hi, Arabic -> ar, ..."""
    txt = " ".join(lines)
    for lo, hi_, code in ((0x0900, 0x097F, "hi"), (0x0600, 0x06FF, "ar"), (0x0980, 0x09FF, "bn"), (0x0B80, 0x0BFF, "ta"),
                          (0x0A80, 0x0AFF, "gu"), (0x0590, 0x05FF, "he"), (0x0E00, 0x0E7F, "th"), (0x3040, 0x30FF, "ja"),
                          (0xAC00, 0xD7AF, "ko"), (0x4E00, 0x9FFF, "zh"), (0x0400, 0x04FF, "ru")):
        if any(lo <= ord(ch) <= hi_ for ch in txt):
            return code
    return "en"


class TranslateBody(BaseModel):
    lines: list[str]
    source: str | None = None
    target: str
    model: str = "good"  # good = M2M100 1.2B, best = MADLAD-400 3B
    script: str = "native"  # Hindi output: native (Devanagari) | roman


def run_translate(lines: list[str], source: str | None, target: str, model: str, script: str = "native", ctx=None) -> dict:
    from . import translit
    t0 = time.time()
    src = source or guess_language(lines)
    mid = ai_models.TRANSLATION_MODELS[model]
    args = {"lines": lines, "source": src, "target": target, "kind": ai_models.CATALOG[mid]["convert"]["kind"],
            "device": ai_models.load_settings()["device"]}
    if os.environ.get("LIGXT_LYRICS_TEST"):
        args["mode"] = "test-translate"
    else:
        if ai_models.ai_python() is None:
            raise RuntimeError("Translation needs the AI tools (ai-venv), which are not installed.")
        args["model"] = str(ai_models.ensure_model(mid, ctx, 0.0, 0.5) / ai_models.CATALOG[mid]["convert"]["out"])
    res = ai_bridge.call("translate", args, progress=(lambda p, msg: ctx.progress(0.5 + 0.48 * p, msg)) if ctx else None,
                         cancelled=ctx.cancelled if ctx else None)
    out = res["lines"]
    if target == "hi" and script == "roman":
        out = [translit.romanize(x) for x in out]
    return {"lines": out, "source": src, "target": target, "model": model, "modelName": ai_models.CATALOG[mid]["name"],
            "device": res.get("device"), "seconds": round(time.time() - t0, 2)}


@router.post("/api/lyrics/translate")
def lyrics_translate(body: TranslateBody):
    if body.target not in LANGUAGES:
        raise HTTPException(400, "Unsupported target language.")
    if body.model not in ai_models.TRANSLATION_MODELS:
        raise HTTPException(400, "model must be good or best.")
    lines = [str(x).strip() for x in body.lines]
    if not any(lines):
        raise HTTPException(400, "Nothing to translate.")

    def run(ctx: JobContext) -> dict:
        r = run_translate(lines, body.source, body.target, body.model, body.script, ctx)
        r["message"] = f"{len(lines)} line(s) translated {r['source']} -> {r['target']} ({r['modelName']}, {str(r.get('device') or '').upper()})."
        ctx.message(r["message"])
        return r

    job = jobs.submit("translate", "Translate lyrics", run, queue="ai")
    return {"jobId": job.id}


class RomanizeBody(BaseModel):
    lines: list[str]
    diacritics: bool = False


@router.post("/api/lyrics/romanize")
def lyrics_romanize(body: RomanizeBody):
    """Hindi (Devanagari) in Latin letters; other text unchanged. No AI, instant."""
    from . import translit
    return {"lines": [translit.romanize(x, body.diacritics) for x in body.lines]}


@router.get("/api/lyrics/languages")
def lyrics_languages():
    return {"languages": LANGUAGES, "models": {k: {"id": v, "name": ai_models.CATALOG[v]["name"], "installed": ai_models.is_installed(v),
                                                 "downloadBytes": ai_models.download_size(v)} for k, v in ai_models.TRANSLATION_MODELS.items()}}
