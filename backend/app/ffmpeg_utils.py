"""Helpers for running ffmpeg/ffprobe: probing, progress parsing, encoder detection."""
from __future__ import annotations

import json
import re
import subprocess
import threading
from fractions import Fraction
from typing import Callable

from .config import FFMPEG, FFPROBE

# Hide console windows spawned from the server on Windows.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FFmpegError(RuntimeError):
    """ffmpeg failed. `message` is human-readable, `log` holds the tail of stderr."""

    def __init__(self, message: str, log: str = ""):
        super().__init__(message)
        self.log = log


class Cancelled(RuntimeError):
    pass


def _run(args: list[str], timeout: float | None = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )


_nvenc_cache: bool | None = None
_nvenc_lock = threading.Lock()


def has_nvenc() -> bool:
    global _nvenc_cache
    with _nvenc_lock:
        if _nvenc_cache is None:
            try:
                r = _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                          "color=s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=30)
                _nvenc_cache = r.returncode == 0
            except Exception:
                _nvenc_cache = False
        return _nvenc_cache


def ffmpeg_version() -> str:
    try:
        return _run([FFMPEG, "-hide_banner", "-version"], timeout=15).stdout.splitlines()[0]
    except Exception as e:  # noqa: BLE001
        return f"unavailable ({e})"


def _fps(rate: str | None) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    try:
        return round(float(Fraction(rate)), 3)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: str) -> dict:
    """Return normalized metadata for a media file."""
    r = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], timeout=60)
    if r.returncode != 0:
        raise FFmpegError(f"Could not read this file (ffprobe): {r.stderr.strip()[:300] or 'unknown format'}")
    data = json.loads(r.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(fmt.get("duration") or 0) or float((v or a or {}).get("duration") or 0)
    info: dict = {
        "duration": round(duration, 3),
        "hasVideo": v is not None,
        "hasAudio": a is not None,
        "container": fmt.get("format_name", ""),
        "bitrate": int(fmt.get("bit_rate") or 0),
    }
    if v is not None:
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        rotation = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rotation = int(round(float(sd["rotation"])))
        rotation = rotation or int((v.get("tags") or {}).get("rotate", 0) or 0)
        if abs(rotation) % 180 == 90:
            w, h = h, w  # ffmpeg autorotates on decode, so report display size
        info.update({
            "width": w, "height": h, "rotation": rotation,
            "fps": _fps(v.get("avg_frame_rate")) or _fps(v.get("r_frame_rate")),
            "videoCodec": v.get("codec_name", ""),
            "pixFmt": v.get("pix_fmt", ""),
            "hdr": v.get("color_transfer") in ("smpte2084", "arib-std-b67"),
        })
    if a is not None:
        info.update({
            "audioCodec": a.get("codec_name", ""),
            "sampleRate": int(a.get("sample_rate") or 0),
            "channels": int(a.get("channels") or 0),
        })
    return info


def stream_durations(path: str) -> dict:
    """Durations of the first video and audio stream (for the audio sync check)."""
    r = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path], timeout=60)
    data = json.loads(r.stdout or "{}")
    out: dict = {"format": float(data.get("format", {}).get("duration") or 0)}
    for s in data.get("streams", []):
        t = s.get("codec_type")
        if t in ("video", "audio") and t not in out:
            out[t] = float(s.get("duration") or 0)
    return out


_TIME_RE = re.compile(r"out_time_(?:us|ms)=(\d+)")


def run_ffmpeg(
    args: list[str],
    total_seconds: float,
    on_progress: Callable[[float], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_process: Callable[[subprocess.Popen], None] | None = None,
    cwd: str | None = None,
) -> None:
    """Run ffmpeg with `-progress pipe:1`, reporting 0..1 progress. Raises FFmpegError on failure."""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd,
        encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
    )
    if on_process:
        on_process(proc)

    err_lines: list[str] = []

    def drain_stderr():
        assert proc.stderr is not None
        for line in proc.stderr:
            err_lines.append(line.rstrip())
            if len(err_lines) > 400:
                del err_lines[:200]

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()

    assert proc.stdout is not None
    for line in proc.stdout:
        if is_cancelled and is_cancelled():
            proc.kill()
            break
        m = _TIME_RE.match(line.strip())
        if m and total_seconds > 0 and on_progress:
            secs = int(m.group(1)) / 1_000_000
            on_progress(max(0.0, min(1.0, secs / total_seconds)))
    proc.wait()
    t.join(timeout=5)

    if is_cancelled and is_cancelled():
        raise Cancelled("Cancelled")
    if proc.returncode != 0:
        log = "\n".join(err_lines[-40:])
        raise FFmpegError(summarize_error(err_lines), log)


def summarize_error(lines: list[str]) -> str:
    """Pick the most useful line(s) out of ffmpeg's stderr."""
    keys = ("error", "invalid", "no such file", "not found", "failed", "cannot", "unable", "permission denied")
    hits = [ln for ln in lines if any(k in ln.lower() for k in keys)]
    pick = hits[-3:] if hits else lines[-3:]
    msg = " | ".join(s.strip() for s in pick if s.strip())
    return f"ffmpeg failed: {msg[:500]}" if msg else "ffmpeg failed with no output."
