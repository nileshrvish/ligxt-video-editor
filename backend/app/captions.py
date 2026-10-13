"""Auto-captions: faster-whisper (local, free) on the GPU, CPU as a fallback.

The speech sources are the clips with sound on the main/overlay video tracks and the audio tracks
(music tracks and sound effects are left out). Each clip's audio is transcribed with word timestamps,
and the words are mapped back to the timeline. The editor turns the words into caption clips.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .config import FFMPEG, ROOT
from .jobs import JobContext, jobs
from .media import load_meta, original_path
from .models import Project
from .projects import load_project
from . import motion as MO

router = APIRouter(tags=["captions"])
MODEL_DIR = Path(os.environ.get("LIGXT_WHISPER_DIR", ROOT / "models" / "whisper"))
MODELS = ("tiny", "base", "small", "medium")
_lock = threading.Lock()
_loaded: dict = {}


def _cuda_dlls() -> None:
    """faster-whisper (CTranslate2) needs cuBLAS/cuDNN; the pip wheels put them under nvidia/*/bin."""
    if sys.platform != "win32":
        return
    for d in glob.glob(os.path.join(sys.prefix, "Lib", "site-packages", "nvidia", "*", "bin")):
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
        if d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _model(name: str, device: str):
    key = (name, device)
    if key not in _loaded:
        _cuda_dlls()
        from faster_whisper import WhisperModel
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        _loaded.clear()  # one model in memory at a time (6 GB GPU)
        _loaded[key] = WhisperModel(name, device=device, compute_type="float16" if device == "cuda" else "int8",
                                    download_root=str(MODEL_DIR))
    return _loaded[key]


def speech_sources(project: Project) -> tuple[list[tuple], list[str]]:
    """(clip, meta) pairs to transcribe, and notes about clips that were skipped."""
    out, notes = [], []
    for t in project.tracks:
        if t.muted or t.type not in ("video", "overlay", "audio"):
            continue
        for c in t.clips:
            if not c.mediaId or c.muted or c.volume <= 0:
                continue
            m = load_meta(c.mediaId)
            if not m or not m.get("hasAudio") or m.get("sfx") or m.get("type") == "image":
                continue
            if c.reverse or c.freezeFrame is not None or c.speedRamp:
                notes.append(f"Skipped '{m['name']}' (reversed, frozen or speed-ramped clips can't be captioned).")
                continue
            out.append((c, m))
    return out, notes


def transcribe(project: Project, model: str = "small", language: str | None = None, progress=None) -> dict:
    sources, notes = speech_sources(project)
    if not sources:
        return {"words": [], "message": "No clips with sound to caption (music and sound effects are not used).", "notes": notes}
    words, device, lang = [], "cuda", None
    with _lock, tempfile.TemporaryDirectory(prefix="ligxt_cap_") as tmp:
        for i, (c, m) in enumerate(sources):
            wav = Path(tmp) / f"{i}.wav"
            dur = max(0.05, c.outPoint - c.inPoint)
            subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{c.inPoint:.3f}", "-t", f"{dur:.3f}", "-i", str(original_path(m)),
                            "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True, capture_output=True)
            try:
                wm = _model(model, device)
                segs, info = wm.transcribe(str(wav), language=language or None, word_timestamps=True, vad_filter=True,
                                           condition_on_previous_text=False)
                segs = list(segs)
            except Exception:  # noqa: BLE001  (no CUDA, missing DLLs, out of GPU memory...)
                if device != "cuda":
                    raise
                device = "cpu"
                notes.append("The GPU could not be used, so captions ran on the CPU (slower).")
                wm = _model(model, device)
                segs, info = wm.transcribe(str(wav), language=language or None, word_timestamps=True, vad_filter=True,
                                           condition_on_previous_text=False)
                segs = list(segs)
            lang = lang or info.language
            speed = c.speed or 1
            end = c.start + MO.clip_duration(c.model_dump())
            for sg in segs:
                for w in sg.words or []:
                    ts, te = c.start + w.start / speed, c.start + w.end / speed
                    if ts < end and w.word.strip():
                        words.append({"text": w.word.strip(), "start": round(ts, 3), "end": round(min(te, end), 3)})
            if progress:
                progress((i + 1) / len(sources))
    words.sort(key=lambda w: w["start"])
    msg = (f"{len(words)} words transcribed ({lang}, {device.upper()})." if words else
           "No speech found. The clips with sound contain music or silence only, so no captions were added.")
    return {"words": words, "language": lang, "device": device, "message": msg, "notes": notes}


class CaptionBody(BaseModel):
    project: dict | None = None
    model: str = "small"
    language: str | None = None


@router.post("/api/projects/{pid}/captions")
def captions(pid: str, body: CaptionBody):
    if body.model not in MODELS:
        raise HTTPException(400, f"Unknown model '{body.model}'. Use one of: {', '.join(MODELS)}.")
    project = Project.model_validate(body.project) if body.project else load_project(pid)

    def run(ctx: JobContext) -> dict:
        ctx.progress(0.02, "Loading the speech model (first use downloads it)")
        res = transcribe(project, body.model, body.language, lambda f: ctx.progress(0.05 + 0.93 * f, "Transcribing"))
        ctx.message(res["message"])
        return res

    job = jobs.submit("captions", "Auto-captions", run, queue="render", ref={"projectId": project.id})
    return {"jobId": job.id}
