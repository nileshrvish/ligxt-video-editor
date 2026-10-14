"""Backend for the headless UI tests: a throwaway data folder (your real projects and media are never touched),
synthetic test clips written to e2e/.fixtures, then the real Ligxt server on port 8799 serving frontend/dist.

Run by playwright.config.ts (webServer); needs `npm run build` first (`npm run e2e` does both).
"""
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
os.environ["LIGXT_DATA_DIR"] = tempfile.mkdtemp(prefix="ligxt-e2e-")
os.environ["LIGXT_AI_PYTHON"] = sys.executable  # the AI worker runs with this Python...
os.environ["LIGXT_SLOWMO_TEST"] = "1"  # ...and "AI smooth" blends through it (no GPU work in UI tests)
sys.path.insert(0, str(ROOT / "backend"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402


def moving_object(path: Path) -> None:
    """640x360, 25 fps, 4 s: a checkered object on a known path (same as backend/tests/test_phase11.py)."""
    W, H, fps = 640, 360, 25
    rng = np.random.default_rng(3)
    bg = np.clip(np.full((H, W, 3), (40, 60, 80), np.int16) + rng.integers(-6, 7, (H, W, 1)), 0, 255).astype(np.uint8)
    obj = np.zeros((56, 56, 3), np.uint8)
    for y in range(56):
        for x in range(56):
            obj[y, x] = (30, 140, 250) if ((x // 8) + (y // 8)) % 2 == 0 else (250, 250, 250)
    cv2.circle(obj, (28, 28), 10, (40, 200, 40), -1)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
                             "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", str(path)], stdin=subprocess.PIPE)
    for k in range(4 * fps):
        t = k / fps
        f = bg.copy()
        cx, cy = 120 + 100 * t, 180 + 50 * math.sin(t * 1.6)
        x0, y0 = int(round(cx - 28)), int(round(cy - 28))
        f[y0:y0 + 56, x0:x0 + 56] = obj
        proc.stdin.write(f.tobytes())
    proc.stdin.close()
    proc.wait()


fx = HERE / ".fixtures"
fx.mkdir(exist_ok=True)
if not (fx / "object.mp4").exists():
    moving_object(fx / "object.mp4")

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=8799, log_level="warning")
