"""Media library: import, probe, thumbnails, waveforms, proxies.

Layout on disk:
  media/<id>/original<ext>   the untouched original (exports always use this)
  media/<id>/meta.json       metadata (the registry entry)
  cache/<id>/thumb.jpg       library thumbnail
  cache/<id>/proxy.mp4       low-res video proxy (or proxy.m4a / preview.jpg)
  cache/<id>/waveform.json   audio peaks
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request

from . import config
from .config import AUDIO_EXT, CACHE_DIR, IMAGE_EXT, MEDIA_DIR, VIDEO_EXT
from .ffmpeg_utils import CREATE_NO_WINDOW, FFmpegError, has_nvenc, probe, run_ffmpeg
from .jobs import JobContext, jobs

router = APIRouter(prefix="/api/media", tags=["media"])
_meta_lock = threading.Lock()

BROWSER_AUDIO = {"mp3", "aac", "flac", "opus", "vorbis", "pcm_s16le", "pcm_s24le", "pcm_f32le"}


# ---------------------------------------------------------------- registry
def media_dir(mid: str) -> Path:
    return MEDIA_DIR / mid


def cache_dir(mid: str) -> Path:
    d = CACHE_DIR / mid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _valid_id(mid: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{12}", mid or ""):
        raise HTTPException(400, f"Invalid media id: {mid!r}")
    return mid


def load_meta(mid: str) -> dict | None:
    p = media_dir(mid) / "meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_meta(meta: dict) -> None:
    with _meta_lock:
        p = media_dir(meta["id"]) / "meta.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        tmp.replace(p)


def update_meta(mid: str, **changes) -> dict:
    with _meta_lock:
        p = media_dir(mid) / "meta.json"
        meta = json.loads(p.read_text(encoding="utf-8"))
        meta.update(changes)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        tmp.replace(p)
        return meta


def original_path(meta: dict) -> Path:
    return config.DATA_ROOT / meta["file"]


def media_type_for(ext: str, info: dict) -> str:
    ext = ext.lower()
    if ext in IMAGE_EXT and not (ext == ".gif" and info.get("duration", 0) > 0.2):
        return "image"
    if ext in AUDIO_EXT or (not info.get("hasVideo") and info.get("hasAudio")):
        return "audio"
    if info.get("hasVideo") and info.get("duration", 0) <= 0.05:
        return "image"  # single-frame "video" such as some image formats
    return "video"


def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:180] or "file"


# ---------------------------------------------------------------- processing
def _ffmpeg_quick(args: list[str], timeout: float = 120) -> None:
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise FFmpegError(f"ffmpeg failed: {r.stderr.strip()[-400:]}", r.stderr[-3000:])


def make_thumbnail(meta: dict) -> str | None:
    if meta["type"] == "audio":
        return None
    src = str(original_path(meta))
    out = cache_dir(meta["id"]) / "thumb.jpg"
    seek = []
    if meta["type"] == "video":
        seek = ["-ss", f"{min(1.0, meta.get('duration', 0) * 0.1):.3f}"]
    _ffmpeg_quick([*seek, "-i", src, "-frames:v", "1",
                   "-vf", "scale=320:320:force_original_aspect_ratio=decrease:force_divisible_by=2",
                   "-q:v", "4", str(out)])
    return f"/files/cache/{meta['id']}/thumb.jpg?v={int(time.time())}"


def make_waveform(meta: dict) -> str | None:
    if not meta.get("hasAudio"):
        return None
    src = str(original_path(meta))
    sr = 8000
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", src,
                        "-map", "0:a:0", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                       capture_output=True, timeout=600, creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise FFmpegError(f"Waveform extraction failed: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    samples = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    bucket = sr // config.WAVEFORM_PEAKS_PER_SEC
    n = len(samples) // bucket
    if n == 0:
        peaks = []
    else:
        peaks = np.abs(samples[: n * bucket]).reshape(n, bucket).max(axis=1)
        peaks = np.round(np.clip(peaks, 0, 1) * 255).astype(np.uint8).tolist()
    data = {"peaksPerSecond": config.WAVEFORM_PEAKS_PER_SEC, "scale": 255, "peaks": peaks}
    (cache_dir(meta["id"]) / "waveform.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return f"/files/cache/{meta['id']}/waveform.json"


def _proxy_video_args(src: str, out: str, use_gpu: bool, hdr: bool = False) -> list[str]:
    m = config.PROXY_MAX_SIDE
    tonemap = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
               "zscale=t=bt709:m=bt709:r=tv," if hdr else "")
    scale = tonemap + (f"scale='if(gte(iw,ih),trunc(min({m},iw)/2)*2,-2)':'if(gte(iw,ih),-2,trunc(min({m},ih)/2)*2)',"
             "format=yuv420p")
    venc = (["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "28", "-b:v", "0"]
            if use_gpu else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"])
    return ["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", scale, *venc,
            "-g", str(config.PROXY_GOP), "-bf", "0",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
            "-movflags", "+faststart", out]


def make_proxy(meta: dict, ctx: JobContext, p0: float, p1: float) -> tuple[str | None, str]:
    """Returns (proxy url, status)."""
    mid = meta["id"]
    src = str(original_path(meta))
    cdir = cache_dir(mid)

    def prog(p: float) -> None:
        ctx.progress(p0 + (p1 - p0) * p)

    if meta["type"] == "image":
        out = cdir / "preview.jpg"
        _ffmpeg_quick(["-i", src, "-frames:v", "1", "-vf",
                       "scale=1920:1920:force_original_aspect_ratio=decrease:force_divisible_by=2",
                       "-q:v", "3", str(out)])
        return f"/files/cache/{mid}/preview.jpg", "ready"

    if meta["type"] == "audio":
        if meta.get("audioCodec") in BROWSER_AUDIO:
            return None, "notNeeded"
        out = cdir / "proxy.m4a"
        tmp = cdir / "proxy.tmp.m4a"
        run_ffmpeg(["-i", src, "-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", str(tmp)],
                   meta.get("duration", 0), prog, ctx.cancelled, ctx.set_process)
        tmp.replace(out)
        return f"/files/cache/{mid}/proxy.m4a", "ready"

    out = cdir / "proxy.mp4"
    tmp = cdir / "proxy.tmp.mp4"
    use_gpu = has_nvenc()
    try:
        run_ffmpeg(_proxy_video_args(src, str(tmp), use_gpu, bool(meta.get("hdr"))), meta.get("duration", 0), prog,
                   ctx.cancelled, ctx.set_process)
    except FFmpegError:
        if not use_gpu or ctx.cancelled():
            raise
        ctx.message("GPU proxy failed, retrying on CPU…")
        run_ffmpeg(_proxy_video_args(src, str(tmp), False, bool(meta.get("hdr"))), meta.get("duration", 0), prog,
                   ctx.cancelled, ctx.set_process)
    tmp.replace(out)
    return f"/files/cache/{mid}/proxy.mp4", "ready"


def process_media(mid: str):
    """Job body: thumbnail -> waveform -> proxy. Metadata is saved after every step."""

    def run(ctx: JobContext) -> dict:
        meta = load_meta(mid)
        if meta is None:
            raise RuntimeError("Media was removed before processing.")
        errors: list[str] = []

        ctx.progress(0.02, "Thumbnail")
        try:
            meta = update_meta(mid, thumbnail=make_thumbnail(meta))
        except Exception as e:  # noqa: BLE001
            errors.append(f"Thumbnail: {e}")

        ctx.progress(0.08, "Waveform")
        try:
            meta = update_meta(mid, waveform=make_waveform(meta))
        except Exception as e:  # noqa: BLE001
            errors.append(f"Waveform: {e}")

        ctx.progress(0.15, "Proxy")
        meta = update_meta(mid, proxyStatus="pending")
        try:
            url, status = make_proxy(meta, ctx, 0.15, 1.0)
            meta = update_meta(mid, proxy=url, proxyStatus=status)
        except Exception as e:
            update_meta(mid, proxyStatus="error", status="error",
                        error="; ".join(errors + [f"Proxy: {e}"]))
            raise
        meta = update_meta(mid, status="ready" if not errors else "warning", error="; ".join(errors) or None)
        ctx.message("Ready" if not errors else "Ready with warnings")
        return {"mediaId": mid}

    return run


def queue_processing(meta: dict):
    return jobs.submit("media", f"Preparing {meta['name']}", process_media(meta["id"]),
                       queue="media", ref={"mediaId": meta["id"]})


# ---------------------------------------------------------------- routes
@router.post("/import")
async def import_media(request: Request, filename: str = Query(..., min_length=1)):
    """Raw-body upload (one file per request) so the browser can show upload progress
    and large files stream straight to disk."""
    name = _safe_name(filename)
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_EXT | IMAGE_EXT | AUDIO_EXT:
        raise HTTPException(415, f"Unsupported file type '{ext or name}'. Use video (mp4, mov, mkv, webm…), "
                                 f"image (jpg, png, webp…) or audio (mp3, wav, m4a…).")
    mid = uuid.uuid4().hex[:12]
    d = media_dir(mid)
    d.mkdir(parents=True)
    dest = d / f"original{ext}"
    size = 0
    try:
        with dest.open("wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                size += len(chunk)
        if size == 0:
            raise HTTPException(400, f"'{name}' is empty.")
        try:
            info = probe(str(dest))
        except FFmpegError as e:
            raise HTTPException(422, f"'{name}' could not be read: {e}") from e
        mtype = media_type_for(ext, info)
        if mtype == "video" and not info.get("hasVideo"):
            raise HTTPException(422, f"'{name}' has no video stream.")
        if mtype == "image":
            info["duration"] = 0
        meta = {
            "id": mid, "name": name, "type": mtype,
            "file": f"media/{mid}/original{ext}", "size": size, "imported": time.time(),
            **info,
            "thumbnail": None, "waveform": None, "proxy": None,
            "proxyStatus": "none", "status": "processing", "error": None,
        }
        save_meta(meta)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    job = queue_processing(meta)
    return {"media": meta, "jobId": job.id}


@router.get("")
def list_media(ids: str = ""):
    """Metadata for the given comma-separated ids (missing ones are reported, not fatal)."""
    out, missing = [], []
    for mid in [i for i in ids.split(",") if i]:
        meta = load_meta(_valid_id(mid))
        (out.append(meta) if meta else missing.append(mid))
    return {"media": out, "missing": missing}


@router.get("/{mid}")
def get_media(mid: str):
    meta = load_meta(_valid_id(mid))
    if not meta:
        raise HTTPException(404, "Media not found.")
    return meta


@router.post("/{mid}/reprocess")
def reprocess(mid: str):
    meta = load_meta(_valid_id(mid))
    if not meta:
        raise HTTPException(404, "Media not found.")
    meta = update_meta(mid, status="processing", error=None, proxyStatus="none")
    return {"jobId": queue_processing(meta).id}


@router.delete("/{mid}")
def delete_media(mid: str):
    """Permanently remove media files (the frontend only calls this when no project uses it)."""
    _valid_id(mid)
    for j in jobs.list():
        if j["ref"].get("mediaId") == mid and j["status"] in ("queued", "running"):
            jobs.cancel(j["id"])
    shutil.rmtree(media_dir(mid), ignore_errors=True)
    shutil.rmtree(CACHE_DIR / mid, ignore_errors=True)
    return {"ok": True}


SFX_DIR = config.ROOT / "sfx"


@router.get("/sfx/library")
def sfx_library():
    out = []
    for p in sorted(SFX_DIR.glob("*.wav")):
        side = p.with_suffix(".json")
        info = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
        out.append({"id": p.stem, "name": info.get("name", p.stem), "peak": info.get("peak", 0.0)})
    return {"sfx": out}


@router.post("/sfx/{sid}/import")
def sfx_import(sid: str):
    """Add a bundled sound effect to the media library (a normal audio media item)."""
    if not re.fullmatch(r"[a-z0-9-]{1,40}", sid) or not (SFX_DIR / f"{sid}.wav").exists():
        raise HTTPException(404, "Unknown sound effect.")
    side = SFX_DIR / f"{sid}.json"
    info = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
    mid = uuid.uuid4().hex[:12]
    d = media_dir(mid)
    d.mkdir(parents=True)
    dest = d / "original.wav"
    shutil.copyfile(SFX_DIR / f"{sid}.wav", dest)
    meta = {"id": mid, "name": f"SFX {info.get('name', sid)}.wav", "type": "audio", "file": f"media/{mid}/original.wav",
            "size": dest.stat().st_size, "imported": time.time(), **probe(str(dest)),
            "thumbnail": None, "waveform": None, "proxy": None, "proxyStatus": "none", "status": "processing",
            "error": None, "sfx": sid, "sfxPeak": float(info.get("peak", 0.0))}
    save_meta(meta)
    job = queue_processing(meta)
    return {"media": meta, "jobId": job.id}


@router.post("/{mid}/beats")
def detect_beats(mid: str):
    """Background job: detect tempo + beat times; stored in the media metadata (bpm, beats)."""
    meta = load_meta(_valid_id(mid))
    if not meta:
        raise HTTPException(404, "Media not found.")
    if not meta.get("hasAudio"):
        raise HTTPException(400, f"'{meta['name']}' has no audio to detect beats in.")

    def run(ctx: JobContext) -> dict:
        from .beats import detect
        ctx.progress(0.1, "Analysing beats")
        res = detect(str(original_path(meta)))
        update_meta(mid, bpm=res["bpm"], beats=res["beats"])
        ctx.message(f"{len(res['beats'])} beats at {res['bpm']} BPM")
        return {"mediaId": mid, "bpm": res["bpm"], "count": len(res["beats"])}

    job = jobs.submit("beats", f"Beat detection: {meta['name']}", run, queue="media", ref={"mediaId": mid})
    return {"jobId": job.id}
