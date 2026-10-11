import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Point the app at a throwaway data folder BEFORE importing it.
_DATA = Path(tempfile.mkdtemp(prefix="ligxt-test-"))
os.environ["LIGXT_DATA_DIR"] = str(_DATA)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

FIX = _DATA / "_fixtures"


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def fixtures() -> dict[str, Path]:
    FIX.mkdir(exist_ok=True)
    f = {
        "landscape": FIX / "landscape.mp4",
        "portrait": FIX / "portrait.mov",
        "photo": FIX / "photo.png",
        "music": FIX / "music.mp3",
        "silent": FIX / "silent.mp4",
    }
    _ff("-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=4", "-f", "lavfi", "-i", "sine=f=440:d=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(f["landscape"]))
    _ff("-f", "lavfi", "-i", "testsrc=s=1080x1920:r=25:d=3", "-f", "lavfi", "-i", "sine=f=660:d=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(f["portrait"]))
    _ff("-f", "lavfi", "-i", "smptebars=s=1600x1200", "-frames:v", "1", str(f["photo"]))
    _ff("-f", "lavfi", "-i", "sine=f=220:d=10", "-c:a", "libmp3lame", "-b:a", "128k", str(f["music"]))
    _ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=24:d=2", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", str(f["silent"]))
    return f


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
