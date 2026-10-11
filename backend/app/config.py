"""Paths and runtime settings."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Where media/projects/exports live. Tests point this at a temp folder.
DATA_ROOT = Path(os.environ.get("LIGXT_DATA_DIR", ROOT)).resolve()
MEDIA_DIR = DATA_ROOT / "media"
CACHE_DIR = DATA_ROOT / "cache"
PROJECTS_DIR = DATA_ROOT / "projects"
EXPORTS_DIR = DATA_ROOT / "exports"
TEMPLATES_DIR = DATA_ROOT / "templates"
FRONTEND_DIST = ROOT / "frontend" / "dist"

for _d in (MEDIA_DIR, CACHE_DIR, PROJECTS_DIR, EXPORTS_DIR, TEMPLATES_DIR):
    _d.mkdir(parents=True, exist_ok=True)

FFMPEG = os.environ.get("LIGXT_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("LIGXT_FFPROBE", "ffprobe")

PORT = int(os.environ.get("LIGXT_PORT", "8765"))
OPEN_BROWSER = os.environ.get("LIGXT_OPEN_BROWSER", "0") == "1"

# Proxy settings: the longest side of a proxy is at most this many pixels.
PROXY_MAX_SIDE = 960
PROXY_GOP = 15  # short keyframe interval keeps scrubbing responsive
WAVEFORM_PEAKS_PER_SEC = 100
AUTOSAVE_KEEP = 10

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".m2ts", ".3gp", ".wmv", ".flv", ".mpg", ".mpeg", ".ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
