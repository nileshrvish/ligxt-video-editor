"""Phase 10: slow motion quality (frame repeat / frame blending / AI smooth with RIFE): frame plans for
constant speed and ramps, interpolating only the slowed frames, scene cuts, export alignment (the export
shows exactly the retimed frames), Cutout on slowed clips, templates, and RIFE itself when installed."""
import json
import os
import subprocess
import sys
import time

import numpy as np
import pytest

os.environ["LIGXT_AI_PYTHON"] = sys.executable
os.environ["LIGXT_SLOWMO_TEST"] = "1"  # "AI smooth" runs through the real worker but blends (no RIFE weights)

from app import ai_bridge, ai_models, config  # noqa: E402
from app import slowmo as SM  # noqa: E402
from test_phase1 import upload, wait_job  # noqa: E402

BG = "0x204060"


def ff(*a):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *a], check=True)


def frames_of(path, w, h, fmt="rgb24"):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", fmt, "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)


def box_x(f):
    """Centre of the bright box on row 320, weighted by brightness (moves smoothly through blended frames)."""
    r = np.clip(f[320, :, 0].astype(float) - 0x30, 0, None)
    return float((np.arange(len(r)) * r).sum() / max(1e-6, r.sum())) if r.sum() > 0 else None


@pytest.fixture(scope="module")
def media(tmp_path_factory, client):
    d = tmp_path_factory.mktemp("p10")
    out = {}
    # a yellow box moving right at 4 px per frame (25 fps)
    f = d / "slide.mp4"
    ff("-f", "lavfi", "-i", f"color=c={BG}:s=360x640:r=25:d=4", "-f", "lavfi", "-i", "color=c=0xe0c040:s=60x160:r=25:d=4",
       "-filter_complex", "[0][1]overlay=x='20+100*t':y=240", "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(f))
    out["slide"] = upload(client, f)
    # two shots with a hard cut at 1.0 s
    a, b = d / "a.mp4", d / "b.mp4"
    ff("-f", "lavfi", "-i", "color=c=0x2050c0:s=360x640:r=25:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(a))
    ff("-f", "lavfi", "-i", "color=c=0xd03020:s=360x640:r=25:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(b))
    cut = d / "cut.mp4"
    ff("-i", str(a), "-i", str(b), "-filter_complex", "[0][1]concat=n=2:v=1:a=0", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cut))
    out["cut"] = upload(client, cut)
    yield out
    ai_bridge.stop()


def test_frame_plan_constant_and_ramp(media):
    m = media["slide"]
    cd = {"inPoint": 1.0, "outPoint": 1.5, "speed": 0.25, "slowmo": {"quality": "ai"}}
    assert SM.quality_of(cd) == "ai" and SM.needs_retime(cd, m, 30)
    plan = SM.frame_plan(cd, m, 0.0, 30.0)
    assert len(plan["frames"]) == 60  # 0.5 s of source at 0.25x = 2 s at 30 fps
    # source position advances 25 * 0.25 / 30 frames per output frame
    for n, (i, t) in enumerate(plan["frames"][:10]):
        p = 25 + n * 25 * 0.25 / 30
        assert abs(i + t - p) < 2e-3
    assert sum(1 for _, t in plan["frames"] if t > 0) > 45
    # auto: AI when slowed, repeat at normal speed; repeat never interpolates
    assert SM.quality_of({"inPoint": 0, "outPoint": 1, "speed": 0.5}) == "ai"
    assert SM.quality_of({"inPoint": 0, "outPoint": 1, "speed": 1}) == "repeat"
    rep = SM.frame_plan({**cd, "slowmo": {"quality": "repeat"}}, m, 0.0, 30.0)
    assert all(t == 0 for _, t in rep["frames"]) and not SM.needs_retime({**cd, "slowmo": {"quality": "repeat"}}, m, 30)
    # a ramp: only the slowed part is interpolated ("Slow-mo into the drop": fast, slow, fast)
    ramp = {"inPoint": 0.0, "outPoint": 2.0, "speed": 1, "slowmo": {"quality": "ai"},
            "speedRamp": {"points": [{"u": 0, "speed": 1}, {"u": 0.3, "speed": 1}, {"u": 0.45, "speed": 0.25}, {"u": 0.7, "speed": 0.25}, {"u": 1, "speed": 1}]}}
    rp = SM.frame_plan(ramp, m, 0.0, 25.0)
    D = len(rp["frames"]) / 25
    first = [t for _, t in rp["frames"][: int(0.25 * D * 25)]]
    middle = [t for _, t in rp["frames"][int(0.5 * D * 25): int(0.65 * D * 25)]]
    assert all(t == 0 for t in first) and sum(1 for t in middle if t > 0) > len(middle) // 2
    # smooth motion: frame-rate conversion at normal speed (25 -> 30 fps)
    sm = {"inPoint": 0, "outPoint": 1, "speed": 1, "slowmo": {"quality": "ai", "smooth": True}}
    assert SM.needs_retime(sm, m, 30) and not SM.needs_retime(sm, m, 25)
    assert any(t > 0 for _, t in SM.frame_plan(sm, m, 0.0, 30.0)["frames"])


def _prep(client, mid, clip, res="preview", fps=30.0, **kw):
    r = client.post("/api/slowmo/prepare", json={"mediaId": mid, "clip": clip, "fps": fps, "res": res, **kw})
    assert r.status_code == 200, r.text
    b = r.json()
    if b["state"] == "running":
        j = wait_job(client, b["jobId"], timeout=300)
        assert j["status"] == "done", (j.get("error"), (j.get("log") or "")[-1500:])
        b = j["result"]
    return b


@pytest.mark.parametrize("quality", ["blend", "ai"])
def test_retimed_frames_are_in_between(client, media, quality):
    m = media["slide"]
    clip = {"start": 0.0, "inPoint": 1.0, "outPoint": 1.5, "speed": 0.25, "slowmo": {"quality": quality}}
    b = _prep(client, m["id"], clip, "full")
    assert b["state"] == "ready" and b["frames"] == 60 and b["interpolated"] > 40
    path = config.DATA_ROOT / b["url"].replace("/files/", "")
    F = frames_of(path, 360, 640)
    assert len(F) == 60
    # blended frames: the box's centre of mass moves evenly (no frame repeats); interpolated frames differ
    same = sum(1 for k in range(1, len(F)) if np.abs(F[k].astype(int) - F[k - 1]).max() < 12)
    assert same <= 2
    again = _prep(client, m["id"], clip, "full")
    assert again["url"] == b["url"]  # cached
    pv = _prep(client, m["id"], clip, "preview")
    assert pv["state"] == "ready" and pv["url"] != b["url"] and pv["offset"] == pytest.approx(0.0)


def test_scene_cut_is_never_mixed(client, media):
    m = media["cut"]
    clip = {"start": 0.0, "inPoint": 0.8, "outPoint": 1.2, "speed": 0.25, "slowmo": {"quality": "blend"}}
    b = _prep(client, m["id"], clip, "full")
    assert b["cutPairs"] and "scene cut" in (b["warning"] or "")
    F = frames_of(config.DATA_ROOT / b["url"].replace("/files/", ""), 360, 640)
    for f in F:
        c = f[320, 180].astype(int)
        blue, red = abs(c - [0x20, 0x50, 0xC0]).max() < 30, abs(c - [0xD0, 0x30, 0x20]).max() < 30
        assert blue or red, c  # never a mix of the two shots


def _project(client, clips, **kw):
    p = client.post("/api/projects", json={"name": "Slow-mo test", "aspect": "9:16"}).json()
    p["settings"].update(width=360, height=640, fps=30)
    p["export"].update(width=360, height=640, preset="custom", fps=30)
    p["tracks"][0]["clips"] = clips
    p.update(kw)
    return p


def _export(client, p):
    r = client.post(f"/api/projects/{p['id']}/export", json={"project": p, "mode": "final"})
    assert r.status_code == 200, r.text
    j = wait_job(client, r.json()["jobId"], timeout=300)
    assert j["status"] == "done", (j.get("error"), (j.get("log") or "")[-1500:])
    return j["result"]["path"]


def test_export_shows_the_retimed_frames(client, media):
    m = media["slide"]
    clips = [{"id": "s", "mediaId": m["id"], "start": 0, "inPoint": 1.0, "outPoint": 1.5, "speed": 0.25, "fit": "fill",
              "slowmo": {"quality": "ai"}}]
    p = _project(client, clips)
    out = _export(client, p)
    E = frames_of(out, 360, 640)
    plan = SM.frame_plan({**clips[0]}, m, 0.0, 30.0)
    R = frames_of(SM.slow_dir(m["id"]) / f"{SM.plan_key(m, plan, 'full')}_full.mp4", 360, 640)
    assert len(E) == 60
    for k in (3, 17, 30, 44, 58):
        assert abs(box_x(E[k]) - box_x(R[k])) <= 1.0, k  # same frame, same position
    # versus frame repeat: repeat holds each source frame ~4.8 times, AI/blend never repeats
    p2 = _project(client, [{**clips[0], "slowmo": {"quality": "repeat"}}])
    E2 = frames_of(_export(client, p2), 360, 640)
    xs1 = [box_x(f) for f in E]
    xs2 = [box_x(f) for f in E2]
    rep1 = sum(1 for a, b in zip(xs1, xs1[1:]) if abs(a - b) < 0.3)
    rep2 = sum(1 for a, b in zip(xs2, xs2[1:]) if abs(a - b) < 0.3)
    assert rep2 > 40 and rep1 < 5, (rep1, rep2)


def test_ramp_into_the_drop_and_cutout_stay_aligned(client, media):
    m = media["slide"]
    ramp = {"points": [{"u": 0, "speed": 1}, {"u": 0.5, "speed": 0.3}, {"u": 1, "speed": 0.3}]}
    cut = {"enabled": True, "model": "test-key", "background": {"type": "color", "color": "#00ff00"}}
    clips = [{"id": "r", "mediaId": m["id"], "start": 0, "inPoint": 0.5, "outPoint": 1.8, "speed": 1, "speedRamp": ramp, "fit": "fill",
              "cutout": cut}]
    p = _project(client, clips)
    out = _export(client, p)
    E = frames_of(out, 360, 640)
    plan = SM.frame_plan(clips[0], m, 0.0, 30.0)
    R = frames_of(SM.slow_dir(m["id"]) / f"{SM.plan_key(m, plan, 'full')}_full.mp4", 360, 640)
    for k in range(5, len(E) - 2, 9):
        assert E[k, 20, 20, 1] > 200 and E[k, 20, 20, 0] < 60  # colour background: the cutout ran
        assert abs(box_x(E[k]) - box_x(R[k])) <= 1.5, k  # the cut-out box sits where the retimed frame has it
    # the derived media for the cutout exists and isn't offered by media cleanup
    unused = {x["id"] for x in client.get("/api/media-cleanup").json()["media"]}
    assert SM.derived_id(m, SM.plan_key(m, plan, "full")) not in unused


def test_template_keeps_the_ramp(client, media):
    from app import templates as TP
    song = config.DATA_ROOT / "p10song.wav"
    TP.synth_music(120, 12, song)
    s = upload(client, song)
    assert wait_job(client, client.post(f"/api/media/{s['id']}/beats").json()["jobId"], timeout=120)["status"] == "done"
    ramp = {"points": [{"u": 0, "speed": 1}, {"u": 0.5, "speed": 0.4}, {"u": 1, "speed": 0.4}], "preset": "drop"}
    clips = [{"id": "a", "mediaId": media["slide"]["id"], "start": 0, "inPoint": 0.2, "outPoint": 1.6, "speed": 1, "speedRamp": ramp,
              "slowmo": {"quality": "ai"}, "fit": "fill"}]
    p = _project(client, clips)
    mu = next(t for t in p["tracks"] if t["type"] == "music")
    mu["clips"] = [{"id": "m", "mediaId": s["id"], "start": 0, "inPoint": 0, "outPoint": 6}]
    r = client.post("/api/templates", json={"project": p, "name": "Ramp tpl"})
    assert r.status_code == 200, r.text
    tid = r.json()["template"]["id"]
    from app.templates_api import _load
    slot = _load(tid)["slots"][0]
    assert slot["speedRamp"]["points"][1]["speed"] == 0.4 and slot["slowmo"]["quality"] == "ai"
    g = client.post(f"/api/templates/{tid}/generate", json={"media": [media["slide"]["id"]], "music": s["id"]}).json()
    j = wait_job(client, g["jobId"])
    assert j["status"] == "done", j
    gp = client.get(f"/api/projects/{j['result']['projectId']}").json()
    c = gp["tracks"][0]["clips"][0]
    assert c["speedRamp"]["points"][1]["speed"] == 0.4 and c["slowmo"]["quality"] == "ai"
    assert any("Slow motion on 1" in line for line in j["result"]["report"])
    client.delete(f"/api/templates/{tid}")


def test_without_ai_tools_ai_smooth_says_so(client, media, monkeypatch):
    monkeypatch.setattr(ai_models, "ai_python", lambda: None)
    clip = {"start": 0.0, "inPoint": 2.0, "outPoint": 2.3, "speed": 0.5, "slowmo": {"quality": "ai"}}
    r = client.post("/api/slowmo/prepare", json={"mediaId": media["slide"]["id"], "clip": clip, "fps": 30, "res": "preview"})
    assert r.status_code == 503 and "Frame blending" in r.json()["detail"]


AI_PY = config.ROOT / "ai-venv" / "Scripts" / "python.exe"


@pytest.mark.skipif(not (AI_PY.exists() and ai_models.is_installed(ai_models.RIFE_MODEL)), reason="needs ai-venv and RIFE")
def test_rife_puts_the_box_halfway(client, media, monkeypatch):
    """The real model: an in-between frame shows the moving box between its two positions (not two ghosts)."""
    ai_bridge.stop()
    monkeypatch.setenv("LIGXT_AI_PYTHON", str(AI_PY))
    monkeypatch.delenv("LIGXT_SLOWMO_TEST", raising=False)
    try:
        m = media["slide"]
        clip = {"start": 0.0, "inPoint": 1.0, "outPoint": 1.2, "speed": 0.25, "slowmo": {"quality": "ai"}}
        b = _prep(client, m["id"], clip, "full")
        assert b["device"] == "cuda" and b["aiFps"] and b["motionP95"] is not None
        F = frames_of(config.DATA_ROOT / b["url"].replace("/files/", ""), 360, 640)
        plan = SM.frame_plan({**clip}, m, 0.0, 30.0)
        for k, (i, t) in enumerate(plan["frames"]):
            if 0.3 < t < 0.7:
                expect = 20 + 100 * (i + t) / 25 + 30  # box centre at that source time
                assert abs(box_x(F[k]) - expect) <= 3, (k, box_x(F[k]), expect)
                xs = np.where(F[k][320, :, 0] > 120)[0]
                assert xs.max() - xs.min() <= 64  # one box, not a double image
    finally:
        ai_bridge.stop()
