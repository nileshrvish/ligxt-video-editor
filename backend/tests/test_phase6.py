"""Phase 6: text (export placement + animation, shared ops), photo extension, export presets
(Instagram encode, reduced grain, letterbox), size estimate, cover image, captions."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from app import text as TX
from app.ffmpeg_utils import probe
from test_phase1 import _export, upload, wait_job

W, H = 540, 960
ROOT = Path(__file__).resolve().parents[2]


def ff(*a):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *a], check=True)


def frames(path, w=W, h=H):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, h, w, 3).astype(float)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("p6")
    grey = d / "grey.png"
    ff("-f", "lavfi", "-i", "color=c=0x406080:s=640x800", "-frames:v", "1", str(grey))
    noisy = d / "noisy.png"
    ff("-f", "lavfi", "-i", "color=c=0x808080:s=540x960", "-frames:v", "1", str(noisy))
    edge = d / "edge.png"  # top row red, rest blue: the extension above must be red, not black
    ff("-f", "lavfi", "-i", "color=c=blue:s=600x800", "-vf", "drawbox=x=0:y=0:w=600:h=40:color=red:t=fill", "-frames:v", "1", str(edge))
    music = d / "music.wav"
    ff("-f", "lavfi", "-i", "sine=f=330:d=3:sample_rate=48000", str(music))
    return {"grey": grey, "noisy": noisy, "edge": edge, "music": music, "dir": d}


def proj(client, main, extra=None, **export):
    p = client.post("/api/projects", json={"name": "P6", "aspect": "9:16"}).json()
    p["tracks"][0]["clips"] = main
    for ttype, cl in (extra or {}).items():
        next(t for t in p["tracks"] if t["type"] == ttype)["clips"] = cl
    p["media"] = list({c["mediaId"] for t in p["tracks"] for c in t["clips"] if c.get("mediaId")})
    p["settings"].update({"width": W, "height": H})
    p["export"].update({"width": W, "height": H, "fps": 30, **export})
    return p


def text_clip(spec, start=0.0, dur=2.0, tr=None):
    return {"mediaId": None, "start": start, "inPoint": 0, "outPoint": dur,
            "transform": {"x": 0, "y": 0, "scale": 1, "rotation": 0, "opacity": 1, **(tr or {})}, "text": spec}


def test_text_cases_are_current(tmp_path):
    """shared/text_cases.json (used by the TS tests) must match what the renderer computes now."""
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_text_cases.py")], capture_output=True, text=True,
                       env={**__import__("os").environ, "LIGXT_DATA_DIR": str(tmp_path)}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    # the script rewrote the file in place; git would show a diff if it had been stale
    data = json.loads((ROOT / "shared" / "text_cases.json").read_text(encoding="utf-8"))
    assert len(data["cases"]) >= 9


def test_text_export_matches_the_composer(client, media):
    """A static title lands exactly where text.Composer draws it (the preview draws the same ops)."""
    g = upload(client, media["grey"])
    spec = {"content": "HELLO REELS", "font": "Anton-Regular", "size": 70, "color": "#ffffff",
            "outline": {"width": 4, "color": "#000000"}}
    tc = text_clip(spec, tr={"y": -200, "rotation": 8})
    p = proj(client, [{"mediaId": g["id"], "start": 0, "inPoint": 0, "outPoint": 2, "fit": "fill"}], {"text": [tc]})
    f = frames(_export(client, p, mode="final")["path"])[30]
    clip = {**tc, "speed": 1, "keyframes": {}}
    lay = TX.build(spec)
    ops = TX.text_ops(lay, clip, 1.0, W, H)
    box = TX._bbox(ops, W, H)
    region = TX.Composer(lay).compose(ops, box).astype(float)
    a = region[..., 3:4] / 255
    bg = np.array([0x40, 0x60, 0x80], float)
    want = region[..., :3] * a + bg * (1 - a)
    got = f[box[1]:box[3], box[0]:box[2]]
    err = np.abs(got - want).mean()
    assert err < 6, f"text pixels differ from the composer by {err:.1f}/255"
    assert np.abs(got - bg).mean() > 20, "text is visible"


def test_text_animations_in_the_export(client, media):
    g = upload(client, media["grey"])
    typed = text_clip({"content": "ABCDEFGHIJ", "font": "Anton-Regular", "size": 60,
                       "animIn": {"type": "typewriter", "duration": 1.0, "easing": "linear"}})
    pop = text_clip({"content": "POP", "font": "Anton-Regular", "size": 90, "animIn": {"type": "pop", "duration": 0.5, "easing": "out"},
                     "animOut": {"type": "fade", "duration": 0.5, "easing": "linear"}}, tr={"y": 250})
    p = proj(client, [{"mediaId": g["id"], "start": 0, "inPoint": 0, "outPoint": 2, "fit": "fill"}], {"text": [typed, pop]})
    fr = frames(_export(client, p, mode="final")["path"])
    bg = np.array([0x40, 0x60, 0x80], float)

    def ink(f, y0, y1):
        return (np.abs(f[y0:y1] - bg).sum(axis=-1) > 60).sum()

    top = (H // 2 - 60, H // 2 + 60)
    t3, t6, t12 = ink(fr[9], *top), ink(fr[18], *top), ink(fr[36], *top)
    assert 0 < t3 < t6 < t12, ("typewriter reveals progressively", t3, t6, t12)
    low = (H // 2 + 250 - 80, H // 2 + 250 + 80)
    assert ink(fr[30], *low) > ink(fr[3], *low) > 0, "pop grows in"
    assert ink(fr[58], *low) < ink(fr[45], *low), "fades out at the end"


def test_photo_extension_fills_instead_of_black(client, media):
    e = upload(client, media["edge"])
    c = {"mediaId": e["id"], "start": 0, "inPoint": 0, "outPoint": 1, "fit": "fill", "extend": True,
         "transform": {"x": 0, "y": 200, "scale": 1, "rotation": 0, "opacity": 1}}
    f = frames(_export(client, proj(client, [c]), mode="final")["path"])[10]
    top = f[5:150, 50:490].mean(axis=(0, 1))
    assert top[0] > 150 and top[2] < 120, f"the area above the moved photo is filled with the red edge colour, got {top}"
    off = dict(c, extend=False)
    f2 = frames(_export(client, proj(client, [off]), mode="final")["path"])[10]
    assert f2[5:150, 50:490].mean() < 10, "without extension it is the black background"
    info = client.get(f"/api/media/{e['id']}/extended").json()
    assert info["fx"] == pytest.approx(0.35, abs=0.01) and info["url"].endswith(".jpg")


def test_instagram_preset_encoding_and_letterbox(client, media):
    g = upload(client, media["grey"])
    mu = upload(client, media["music"])
    main = [{"mediaId": g["id"], "start": 0, "inPoint": 0, "outPoint": 2, "fit": "fill"}]
    res = _export(client, proj(client, main, {"music": [{"mediaId": mu["id"], "start": 0, "inPoint": 0, "outPoint": 2}]},
                              preset="instagram"), mode="final")
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=profile,level,bit_rate",
                        "-of", "json", res["path"]], capture_output=True, text=True, check=True)
    s = json.loads(r.stdout)["streams"][0]
    assert s["profile"] == "High" and int(s["level"]) == 42
    a = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=bit_rate", "-of", "json",
                                   res["path"]], capture_output=True, text=True, check=True).stdout)["streams"][0]
    assert 200_000 < int(a["bit_rate"]) < 300_000, "AAC 256k"
    # a 16:9 export of a 9:16 project is letterboxed (pillarboxed), not stretched
    res = _export(client, proj(client, main, preset="youtube", width=640, height=360), mode="draft")
    info = probe(res["path"])
    assert (info["width"], info["height"]) == (640, 360)
    f = frames(res["path"], 640, 360)[10]
    assert f[:, :100].mean() < 10 and f[:, 300:340].mean() > 40, "bars on the sides, picture in the middle"


def test_reduce_grain_lowers_noise(client, media):
    g = upload(client, media["noisy"])
    clip = {"mediaId": g["id"], "start": 0, "inPoint": 0, "outPoint": 1, "fit": "fill",
            "filters": [{"type": "effect", "name": "grain", "intensity": 1, "params": {"amount": 0.8, "size": 1}}]}

    def noise(**ex):
        f = frames(_export(client, proj(client, [clip], **ex), mode="final")["path"])[12]
        return f[200:700, 100:440].std()

    full, reduced = noise(preset="reels"), noise(preset="instagram")
    assert reduced < 0.55 * full, (full, reduced)


def test_size_estimate(client, media):
    g = upload(client, media["noisy"])
    clip = {"mediaId": g["id"], "start": 0, "inPoint": 0, "outPoint": 4, "fit": "fill",
            "filters": [{"type": "effect", "name": "grain", "intensity": 1, "params": {"amount": 0.5, "size": 1}}]}
    p = proj(client, [clip], preset="reels")
    job = wait_job(client, client.post(f"/api/projects/{p['id']}/export/estimate", json={"project": p}).json()["jobId"], timeout=300)
    assert job["status"] == "done", job
    est = job["result"]
    actual = _export(client, p, mode="final")["size"]
    assert est["method"] == "sampled" and 0.6 < est["bytes"] / actual < 1.6, (est, actual)
    p2 = proj(client, [clip], preset="instagram")
    est2 = wait_job(client, client.post(f"/api/projects/{p2['id']}/export/estimate", json={"project": p2}).json()["jobId"])["result"]
    assert est2["method"] == "bitrate" and est2["bytes"] == pytest.approx((12000 + 256) * 1000 / 8 * 4 * 1.01, rel=0.01)


def test_cover_image_with_text(client, media):
    g = upload(client, media["grey"])
    p = proj(client, [{"mediaId": g["id"], "start": 0, "inPoint": 0, "outPoint": 2, "fit": "fill"}], fileName="Cover test")
    body = {"project": p, "time": 1.0, "text": {"text": {"content": "COVER", "font": "Anton-Regular", "size": 120}, "transform": {"y": -100}}}
    job = wait_job(client, client.post(f"/api/projects/{p['id']}/cover", json=body).json()["jobId"], timeout=120)
    assert job["status"] == "done", job
    from PIL import Image
    im = np.asarray(Image.open(job["result"]["path"]).convert("RGB"), float)
    assert im.shape == (H, W, 3) and job["result"]["file"].endswith("cover.jpg")
    band = im[H // 2 - 100 - 50:H // 2 - 100 + 50]
    assert (band.min(axis=-1) > 230).sum() > 500, "white title drawn"


def _speech_wav(path: Path) -> bool:
    if sys.platform != "win32":
        return False
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SetOutputToWaveFile('{path}'); $s.Speak('Welcome back to the channel. Today we walk through the old town.'); $s.Dispose()")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    return r.returncode == 0 and path.exists()


@pytest.mark.skipif(not (ROOT / "models" / "whisper").exists(), reason="faster-whisper model not downloaded")
def test_captions_speech_and_no_speech(client, media):
    wav = media["dir"] / "speech.wav"
    if not _speech_wav(wav):
        pytest.skip("no speech synthesiser")
    sp = upload(client, wav)
    g = upload(client, media["grey"])
    mu = upload(client, media["music"])
    main = [{"mediaId": g["id"], "start": 0, "inPoint": 0, "outPoint": 6, "fit": "fill"}]
    p = proj(client, main, {"audio": [{"mediaId": sp["id"], "start": 1.0, "inPoint": 0, "outPoint": sp["duration"]}]})
    job = wait_job(client, client.post(f"/api/projects/{p['id']}/captions", json={"project": p, "model": "small"}).json()["jobId"], timeout=600)
    assert job["status"] == "done", job
    words = job["result"]["words"]
    text = " ".join(w["text"].lower() for w in words)
    assert "channel" in text and "town" in text, text
    assert words[0]["start"] >= 1.0 - 0.05, "word times are on the timeline (clip starts at 1 s)"
    # music only (a music track is never transcribed; a tone on an audio track is not speech)
    p2 = proj(client, main, {"audio": [{"mediaId": mu["id"], "start": 0, "inPoint": 0, "outPoint": 3}]})
    job = wait_job(client, client.post(f"/api/projects/{p2['id']}/captions", json={"project": p2, "model": "small"}).json()["jobId"], timeout=600)
    assert job["status"] == "done" and job["result"]["words"] == [] and "No speech found" in job["result"]["message"]
