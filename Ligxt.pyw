"""Ligxt Video Editor launcher: starts the server without a console window, opens the browser, and
sits in the system tray (Open Ligxt / Exports folder / Stop). Stop shuts the server down cleanly.

Run with the venv's pythonw:  .venv\\Scripts\\pythonw.exe Ligxt.pyw   (the desktop shortcut does this)
"""
import logging
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("LIGXT_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"
OPEN = os.environ.get("LIGXT_OPEN_BROWSER", "1") != "0"
LOG = ROOT / "cache" / "launcher.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=LOG, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
if sys.stdout is None:  # pythonw has no console: give libraries somewhere harmless to print
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(LOG, "a", encoding="utf-8")


def running() -> bool:
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=1.5) as r:
            return r.status == 200
    except OSError:
        return False


def icon_image():
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(139, 123, 255, 255))
    d.rectangle([20, 14, 29, 50], fill=(255, 255, 255, 255))
    d.rectangle([20, 41, 46, 50], fill=(255, 255, 255, 255))
    return im


def main() -> None:
    if running():  # already started (another launcher or start.ps1): just open it
        if OPEN:
            webbrowser.open(URL)
        return
    sys.path.insert(0, str(ROOT / "backend"))
    os.environ.setdefault("LIGXT_PORT", str(PORT))
    import uvicorn
    from app.main import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", log_config=None))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(150):
        if running():
            break
        if not th.is_alive():
            logging.error("The server stopped while starting (port %s in use?)", PORT)
            return
        time.sleep(0.1)
    logging.info("Ligxt running at %s", URL)
    if OPEN:
        webbrowser.open(URL)

    import pystray

    def stop(icon, _item=None):
        logging.info("Stopping")
        server.should_exit = True
        th.join(timeout=15)
        icon.stop()

    def open_exports(_icon, _item):
        os.startfile(str(ROOT / "exports"))  # noqa: S606 (Windows only, our own folder)

    icon = pystray.Icon("ligxt", icon_image(), f"Ligxt Video Editor ({URL})", menu=pystray.Menu(
        pystray.MenuItem("Open Ligxt", lambda *_: webbrowser.open(URL), default=True),
        pystray.MenuItem("Exports folder", open_exports),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Stop Ligxt", stop),
    ))
    icon.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        logging.exception("Launcher failed")
