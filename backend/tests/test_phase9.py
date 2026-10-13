"""Phase 9: AI foundation (worker subprocess, model manager) and subject cutout (analysis cache, refine,
export effects, preview/export alignment, templates). The cutout tests use the worker's "test-key" model
(a colour key, no download, no PyTorch) through the real worker process and the real pipeline."""
import hashlib
import json
import os
import subprocess
import sys
import time

import numpy as np
import pytest

os.environ["LIGXT_AI_PYTHON"] = sys.executable  # the test model runs in any Python with numpy

from app import ai_bridge, ai_models, config  # noqa: E402
from app import cutout as CU  # noqa: E402
from app import templates as TP  # noqa: E402
from test_phase1 import upload, wait_job  # noqa: E402

BG = "0x204060"


def ff(*a):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *a], check=True)


def frame(path, n, w, h, fmt="rgb24"):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{n})", "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", fmt, "-"], capture_output=True).stdout
    ch = 3 if fmt == "rgb24" else 1
    return np.frombuffer(raw, np.uint8).reshape(h, w, ch) if ch > 1 else np.frombuffer(raw, np.uint8).reshape(h, w)


@pytest.fixture(scope="module")
def media(tmp_path_factory, client):
    d = tmp_path_factory.mktemp("p9")
    out = {}
    # a yellow "person" moving left/right on a blue background: x = 100 + 60 sin(2 pi t)
    f = d / "walker.mp4"
    ff("-f", "lavfi", "-i", f"color=c={BG}:s=360x640:r=30:d=6", "-f", "lavfi", "-i", "color=c=0xe0c040:s=80x220:r=30:d=6",
       "-filter_complex", "[0][1]overlay=x='100+60*sin(2*PI*t)':y=210", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(f))
    out["walker"] = upload(client, f)
    f = d / "empty.mp4"
    ff("-f", "lavfi", "-i", f"color=c={BG}:s=360x640:r=30:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(f))
    out["empty"] = upload(client, f)
    f = d / "pose.png"
    ff("-f", "lavfi", "-i", f"color=c={BG}:s=600x800", "-f", "lavfi", "-i", "color=c=0xd02020:s=200x300",
       "-filter_complex", "[0][1]overlay=200:300", "-frames:v", "1", str(f))
    out["photo"] = upload(client, f)
    f = d / "wall.png"
    ff("-f", "lavfi", "-i", "color=c=0x30c030:s=400x400", "-frames:v", "1", str(f))
    out["wall"] = upload(client, f)
    song = d / "song.wav"
    TP.synth_music(120, 8, song)
    m = upload(client, song)
    assert wait_job(client, client.post(f"/api/media/{m['id']}/beats").json()["jobId"], timeout=120)["status"] == "done"
    out["song"] = client.get(f"/api/media/{m['id']}").json()
    yield out
    ai_bridge.stop()


CUT = {"enabled": True, "model": "test-key", "refine": {"smooth": 0.5, "grow": 0, "feather": 0.5, "detail": True}}


def _prepare(client, mid, cut=CUT, clip=None):
    r = client.post("/api/cutout/prepare", json={"mediaId": mid, "cutout": cut, "clip": clip, "start": True})
    assert r.status_code == 200, r.text
    b = r.json()
    if b["state"] == "running":
        j = wait_job(client, b["jobId"], timeout=120)
        assert j["status"] == "done", j
        b = j["result"]
    return b


def test_prepare_analyses_once_then_cached(client, media):
    m = media["walker"]
    clip = {"inPoint": 2.5, "outPoint": 4.0, "speed": 1}
    r = client.post("/api/cutout/prepare", json={"mediaId": m["id"], "cutout": CUT, "clip": clip, "start": False}).json()
    assert r["state"] == "missing" and r["aiInstalled"] is True
    b = _prepare(client, m["id"], clip=clip)
    assert b["state"] == "ready" and not b["noSubject"] and 0.05 < b["coverage"] < 0.12
    assert b["preview"].endswith("_pv.mp4") and b["offset"] >= 0
    t0 = time.time()
    again = client.post("/api/cutout/prepare", json={"mediaId": m["id"], "cutout": CUT, "clip": clip, "start": True}).json()
    assert again["state"] == "ready" and time.time() - t0 < 1.0  # cached: instant
    # new edge settings reuse the analysis (only the refine step runs)
    raw_files = sorted(p.name for p in CU.cut_dir(m["id"]).glob("raw1_*.mkv") if "__" not in p.name)
    b2 = _prepare(client, m["id"], {**CUT, "refine": {**CUT["refine"], "grow": 3}}, clip)
    assert b2["state"] == "ready" and b2["preview"] != b["preview"]
    assert sorted(p.name for p in CU.cut_dir(m["id"]).glob("raw1_*.mkv") if "__" not in p.name) == raw_files


def test_window_frames_line_up_with_the_source(client, media):
    """The window file's frame k is source frame k0 + k, in the export matte and in the preview proxy."""
    m = media["walker"]
    from app.media import load_meta, original_path
    meta = load_meta(m["id"])
    raw, der = CU.lookup(meta, CU.normalize(CUT), CU.needed_span({"inPoint": 2.5, "outPoint": 4.0, "speed": 1}, 0.5, 0.5))
    assert raw and der and der["k0"] > 0
    d = CU.cut_dir(m["id"])
    for k in (0, 7, 20):
        src = frame(original_path(meta), der["k0"] + k, 360, 640)
        xs = np.where(src[320, :, 0] > 150)[0]  # the yellow box (red high; the background is 0x20)
        mat = frame(d / der["matte"], k, 360, 640, "gray")
        xm = np.where(mat[320] > 128)[0]
        assert abs(xs.min() - xm.min()) <= 1 and abs(xs.max() - xm.max()) <= 1, (k, xs.min(), xm.min())
        pv = frame(d / der["preview"], k, 720, 640, "gray")
        xp = np.where(pv[320, 360:] > 128)[0]
        assert abs(xp.min() - xm.min()) <= 1 and abs(xp.max() - xm.max()) <= 1


def test_no_subject_is_reported(client, media):
    b = _prepare(client, media["empty"]["id"], clip={"inPoint": 0, "outPoint": 2, "speed": 1})
    assert b["noSubject"] is True and b["coverage"] < 0.004


def _project(client, media, **kw):
    p = client.post("/api/projects", json={"name": "Cutout test", "aspect": "9:16"}).json()
    p["settings"].update(width=360, height=640)
    p["export"].update(width=360, height=640, preset="custom")
    w = media["walker"]["id"]
    p["tracks"][0]["clips"] = [
        {"id": "a", "mediaId": w, "start": 0, "inPoint": 2.5, "outPoint": 4.0, "fit": "fill", "cutout": kw.get("cut_a", CUT)},
        {"id": "b", "mediaId": media["empty"]["id"], "start": 1.5, "inPoint": 0, "outPoint": 1.5, "fit": "fill"},
    ]
    p["transitions"] = kw.get("transitions", [])
    mu = next(t for t in p["tracks"] if t["type"] == "music")
    mu["clips"] = [{"id": "m", "mediaId": media["song"]["id"], "start": 0, "inPoint": 0, "outPoint": 3.0}]
    tt = next(t for t in p["tracks"] if t["type"] == "text")
    tt["clips"] = kw.get("texts", [])
    return p


def _export(client, p):
    r = client.post(f"/api/projects/{p['id']}/export", json={"project": p, "mode": "final"})
    assert r.status_code == 200, r.text
    j = wait_job(client, r.json()["jobId"], timeout=300)
    assert j["status"] == "done", (j.get("error"), (j.get("log") or "")[-2000:])
    return j["result"]["path"]


def test_export_background_glow_shadow_grade_and_text_behind(client, media):
    cut = {**CUT, "background": {"type": "color", "color": "#00ff00"},
           "glow": {"enabled": True, "color": "#ff0000", "width": 16, "softness": 0.3, "opacity": 1, "pulse": {"mode": "beat", "amount": 0.8}},
           "shadow": {"enabled": True, "color": "#000000", "opacity": 0.6, "blur": 4, "x": 6, "y": 8},
           "subjectFilters": [{"type": "preset", "name": "bw", "intensity": 1, "params": {}}]}
    texts = [{"id": "t", "mediaId": None, "start": 0, "inPoint": 0, "outPoint": 1.4,
              "text": {"content": "BEHIND", "font": "Anton-Regular", "size": 130, "color": "#ffffff", "behindSubject": True}}]
    p = _project(client, media, cut_a=cut, texts=texts,
                 transitions=[{"trackId": None, "fromClipId": "a", "toClipId": "b", "type": "fade", "duration": 0.4}])
    p["transitions"][0]["trackId"] = p["tracks"][0]["id"]
    out = _export(client, p)
    from app.media import load_meta, original_path
    src_path = original_path(load_meta(media["walker"]["id"]))
    for n in (3, 15, 30):  # timeline frame n shows source frame 75 + n
        f = frame(out, n, 360, 640).astype(int)
        s = frame(src_path, 75 + n, 360, 640)
        xs = np.where(s[320, :, 0] > 150)[0]
        cx = (xs.min() + xs.max()) // 2
        assert f[20, 20, 1] > 200 and f[20, 20, 0] < 60  # green background
        sub = f[320, cx]
        assert abs(sub[0] - sub[1]) < 12 and abs(sub[1] - sub[2]) < 12 and sub[0] > 120  # grey subject (B&W grade)
        # the subject sits on top of the text: the text row through the box shows the subject, not white
        assert sub.max() < 235
        # glow (red) just outside the box edge
        edge = f[225, xs.max() + 3]  # above the text (the text is drawn over the glow)
        assert edge[0] > edge[1] + 40, edge
    # during the fade into clip b the matte fades too, and after it there is no cutout
    f = frame(out, 60, 360, 640).astype(int)
    assert abs(f[20, 20] - np.array([0x20, 0x40, 0x60])).max() < 14  # clip b's own blue picture


def test_glow_pulses_on_the_beat(client, media):
    beats = CU.timeline_beats({"tracks": [{"type": "music", "clips": [{"mediaId": "s", "start": 0, "inPoint": 0, "outPoint": 3.0}]}]},
                              {"s": media["song"]})
    assert beats and abs(beats[1] - beats[0] - 0.5) < 0.03
    g = {"width": 16, "softness": 0.3, "opacity": 1, "pulse": {"mode": "beat", "amount": 0.8}}
    on = CU.glow_levels(g, CU.pulse_at(g["pulse"], beats[1] + 0.01, beats))
    off = CU.glow_levels(g, CU.pulse_at(g["pulse"], beats[1] + 0.45, beats))
    assert on[3] > off[3] and on[1] < off[1]  # brighter and wider right on the beat


def test_photo_and_overlay_cutout(client, media):
    b = _prepare(client, media["photo"]["id"])
    assert b["image"] and b["matte"].endswith(".png") and 0.1 < b["coverage"] < 0.16
    p = _project(client, media)
    p["tracks"][0]["clips"] = [{"id": "w", "mediaId": media["wall"]["id"], "start": 0, "inPoint": 0, "outPoint": 1.0, "fit": "fill"}]
    ov = next(t for t in p["tracks"] if t["type"] == "overlay")
    ov["clips"] = [{"id": "o", "mediaId": media["photo"]["id"], "start": 0, "inPoint": 0, "outPoint": 1.0, "fit": "fit",
                    "cutout": {**CUT, "background": {"type": "remove"}}}]
    out = _export(client, p)
    f = frame(out, 10, 360, 640).astype(int)
    assert f[30, 30, 1] > 150 and f[30, 30, 0] < 90  # the green wall shows where the photo's background was removed
    assert f[320, 180, 0] > 150 and f[320, 180, 1] < 90  # the red subject on top


def test_prepare_then_export_and_background_media_is_kept_by_cleanup(client, media):
    cut = {**CUT, "background": {"type": "image", "mediaId": media["wall"]["id"]}}
    p = _project(client, media, cut_a=cut)
    p["media"] = [media["walker"]["id"], media["empty"]["id"], media["song"]["id"]]
    assert client.put(f"/api/projects/{p['id']}", json=p).status_code == 200
    _prepare(client, media["walker"]["id"], cut, {"inPoint": 2.5, "outPoint": 4.0, "speed": 1})
    out = _export(client, p)
    f = frame(out, 5, 360, 640).astype(int)
    assert f[20, 20, 1] > 150 and f[20, 20, 0] < 90  # the green wall photo behind the subject
    unused = {x["id"] for x in client.get("/api/media-cleanup").json()["media"]}
    assert media["wall"]["id"] not in unused  # a cutout's background counts as used


def test_template_keeps_cutout_look_and_text_behind(client, media):
    cut = {**CUT, "background": {"type": "image", "mediaId": media["wall"]["id"]},
           "glow": {"enabled": True, "color": "#00ffff", "width": 20, "pulse": {"mode": "beat"}}}
    texts = [{"id": "t", "mediaId": None, "start": 0, "inPoint": 0, "outPoint": 1.0,
              "text": {"content": "HOOK", "font": "Anton-Regular", "size": 120, "behindSubject": True}}]
    p = _project(client, media, cut_a=cut, texts=texts)
    p["tracks"][0]["clips"] = p["tracks"][0]["clips"][:1]
    r = client.post("/api/templates", json={"project": p, "name": "Cutout tpl"})
    assert r.status_code == 200, r.text
    tid = r.json()["template"]["id"]
    from app.templates_api import _load
    t = _load(tid)
    slot = t["slots"][0]
    assert slot["cutout"]["glow"]["enabled"] and slot["cutout"]["glow"]["color"] == "#00ffff"
    assert slot["cutout"]["background"]["type"] == "keep" and slot["cutout"]["background"]["mediaId"] is None  # no personal media
    assert any((x.get("spec") or {}).get("behindSubject") for x in t["texts"])
    g = client.post(f"/api/templates/{tid}/generate", json={"media": [media["walker"]["id"]], "music": media["song"]["id"]}).json()
    j = wait_job(client, g["jobId"])
    assert j["status"] == "done", j
    assert any("Cutout on 1 shot" in line for line in j["result"]["report"])
    gp = client.get(f"/api/projects/{j['result']['projectId']}").json()
    assert gp["tracks"][0]["clips"][0]["cutout"]["glow"]["enabled"]
    client.delete(f"/api/templates/{tid}")


def test_cancel_stops_the_analysis(client, media, tmp_path):
    f = tmp_path / "long.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=30", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(f))
    m = upload(client, f)
    r = client.post("/api/cutout/prepare", json={"mediaId": m["id"], "cutout": CUT, "clip": {"inPoint": 0, "outPoint": 30, "speed": 1}}).json()
    assert r["state"] == "running"
    time.sleep(1.5)
    client.post(f"/api/jobs/{r['jobId']}/cancel")
    j = wait_job(client, r["jobId"], timeout=60)
    assert j["status"] == "cancelled"
    assert not any(p.suffix == ".mkv" and "__" not in p.name for p in CU.cut_dir(m["id"]).glob("*.mkv"))
    # the worker still works afterwards
    assert _prepare(client, media["photo"]["id"])["state"] == "ready"


def test_without_ai_tools(client, media, monkeypatch, tmp_path):
    f = tmp_path / "fresh.mp4"
    ff("-f", "lavfi", "-i", f"color=c={BG}:s=320x320:r=30:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(f))
    fresh = upload(client, f)  # never analysed
    monkeypatch.setenv("LIGXT_AI_PYTHON", str(tmp_path / "missing" / "python.exe"))
    monkeypatch.setattr(ai_models, "ai_python", lambda: None)
    r = client.post("/api/cutout/prepare", json={"mediaId": fresh["id"], "cutout": CUT, "clip": {"inPoint": 0, "outPoint": 1, "speed": 1}})
    assert r.status_code == 503 and "setup.ps1" in r.json()["detail"]
    p = _project(client, media)
    p["tracks"][0]["clips"][0].update(mediaId=fresh["id"], inPoint=0.0, outPoint=1.5)
    r = client.post(f"/api/projects/{p['id']}/export", json={"project": p, "mode": "draft"})
    assert r.status_code == 400 and "AI tools are not installed" in r.json()["detail"]


def test_model_manager_checksums(client, monkeypatch, tmp_path):
    for mid, spec in ai_models.CATALOG.items():
        assert spec["license"] in ("MIT", "Apache-2.0", "BSD-3-Clause") and spec["files"]
        assert all(len(f["sha256"]) == 64 and f["size"] > 0 for f in spec["files"])
    good = tmp_path / "weights.bin"
    good.write_bytes(b"ligxt" * 1000)
    sha = hashlib.sha256(good.read_bytes()).hexdigest()
    monkeypatch.setattr(ai_models, "AI_MODELS_DIR", tmp_path / "models")
    monkeypatch.setitem(ai_models.CATALOG, "fake", {"name": "Fake", "task": "test", "license": "MIT", "source": "", "about": "",
                                                    "res": 64, "dynamic": False,
                                                    "files": [{"name": "w.bin", "size": 5000, "sha256": sha, "url": good.as_uri()}]})
    j = wait_job(client, client.post("/api/ai/models/fake/download").json()["jobId"])
    assert j["status"] == "done" and ai_models.is_installed("fake")
    st = client.get("/api/ai/status").json()
    assert next(m for m in st["models"] if m["id"] == "fake")["diskBytes"] >= 5000
    assert client.delete("/api/ai/models/fake").json()["ok"] and not ai_models.is_installed("fake")
    ai_models.CATALOG["fake"]["files"][0]["sha256"] = "0" * 64  # tampered upstream
    j = wait_job(client, client.post("/api/ai/models/fake/download").json()["jobId"])
    assert j["status"] == "error" and "checksum" in j["error"]
    assert not any((tmp_path / "models" / "fake").glob("w.bin*"))
    assert client.put("/api/ai/settings", json={"device": "gpu-please"}).status_code == 400
    s = client.put("/api/ai/settings", json={"budgetGB": 9}).json()
    assert s["budgetGB"] == 5.5


def test_shared_cutout_cases():
    cases = json.loads((config.ROOT / "shared" / "cutout_cases.json").read_text(encoding="utf-8"))
    for c in cases["pulse"]:
        assert abs(CU.pulse_at(c["pulse"], c["t"], c["beats"]) - c["value"]) < 1e-12
    for c in cases["glow"]:
        assert np.allclose(CU.glow_levels(c["glow"], c["p"], c["k"]), c["value"])
    assert CU.timeline_beats(cases["beats"]["project"], cases["beats"]["media"]) == cases["beats"]["value"]
    assert cases["defaults"] == CU.DEFAULT


AI_PY = config.ROOT / "ai-venv" / "Scripts" / "python.exe"


@pytest.mark.skipif(not (AI_PY.exists() and ai_models.is_installed(ai_models.OBJECT_MODEL)),
                    reason="needs ai-venv with SAM 2 and its checkpoint (Settings > Models)")
def test_object_mode_click_to_select(client, media, monkeypatch):
    ai_bridge.stop()
    monkeypatch.setenv("LIGXT_AI_PYTHON", str(AI_PY))
    try:
        m = media["walker"]
        # click the box at source time 3.0 s (x = 100 + 60 sin(2 pi 3.0) + 40 = 140 px of 360, y = 320 of 640)
        pts = [{"t": 3.0, "x": 140 / 360, "y": 320 / 640, "label": 1}]
        pk = client.post("/api/cutout/pick", json={"mediaId": m["id"], "t": 3.0, "points": pts}).json()
        j = wait_job(client, pk["jobId"], timeout=300)
        assert j["status"] == "done", j
        assert 0.04 < j["result"]["coverage"] < 0.12 and j["result"]["url"].endswith(".jpg")
        cut = {"enabled": True, "mode": "object", "points": pts, "refine": {"smooth": 0.3, "grow": 0, "feather": 0.5}}
        assert client.post("/api/cutout/prepare", json={"mediaId": m["id"], "cutout": {**cut, "points": []}, "clip": {"inPoint": 2.5, "outPoint": 4.0}}).json()["state"] == "needsPoints"
        b = _prepare(client, m["id"], cut, {"inPoint": 2.5, "outPoint": 4.0, "speed": 1})
        assert b["state"] == "ready" and 0.05 < b["coverage"] < 0.12, b
        # the tracked mask follows the moving box
        from app.media import load_meta, original_path
        meta = load_meta(m["id"])
        raw, der = CU.lookup(meta, cut, CU.needed_span({"inPoint": 2.5, "outPoint": 4.0, "speed": 1}, 0.5, 0.5))
        for k in (0, 20, 40):
            src = frame(original_path(meta), der["k0"] + k, 360, 640)
            xs = np.where(src[320, :, 0] > 150)[0]
            mat = frame(CU.cut_dir(m["id"]) / der["matte"], k, 360, 640, "gray")
            xm = np.where(mat[320] > 128)[0]
            assert abs(xs.min() - xm.min()) <= 4 and abs(xs.max() - xm.max()) <= 4, (k, xs.min(), xm.min(), xs.max(), xm.max())
    finally:
        ai_bridge.stop()
