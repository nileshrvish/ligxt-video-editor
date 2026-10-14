"""Phase 12: auto-edit from raw footage. Synthetic clips with known content: a hard scene cut, black and blurred
stretches, a fast-moving clip, calm clips and an exact duplicate; a synthesized song with a drop. Analysis (shots,
unusable moments, duplicates, caching, cancel), drafts (cuts on beats, the strongest shot on the drop, best moment
inside a shot, no near-duplicates, locks, excludes, styles, templates), the project and its export."""
import math
import shutil
import subprocess

import cv2
import numpy as np
import pytest

from app import autoedit as AE
from app import autoedit_analysis as AA
from app import config
from test_phase1 import upload, wait_job

W0, H0, FPS = 640, 360, 25


def write(path, frames_fn, n):
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W0}x{H0}", "-r", str(FPS), "-i", "-",
                             "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(path)], stdin=subprocess.PIPE)
    for k in range(n):
        proc.stdin.write(frames_fn(k).tobytes())
    proc.stdin.close()
    proc.wait()


def scene(seed, shift=0.0):
    """A textured, colourful still scene (with a slow drift)."""
    rng = np.random.default_rng(seed)
    base = np.zeros((H0, W0, 3), np.uint8)
    for _ in range(40):
        c = tuple(int(v) for v in rng.integers(30, 230, 3))
        x, y = int(rng.integers(0, W0)), int(rng.integers(0, H0))
        cv2.circle(base, (x, y), int(rng.integers(10, 60)), c, -1)
    base = cv2.add(base, rng.integers(0, 25, (H0, W0, 3), dtype=np.uint8))
    return base


@pytest.fixture(scope="module")
def footage(tmp_path_factory, client):
    d = tmp_path_factory.mktemp("p12")
    A, B, C = scene(1), scene(2), scene(3)

    def drift(img, k, px=0.6):
        m = np.float32([[1, 0, -px * k], [0, 1, 0]])
        return cv2.warpAffine(img, m, (W0, H0), borderMode=cv2.BORDER_REFLECT)

    files = {}
    files["calm"] = d / "calm.mp4"
    write(files["calm"], lambda k: drift(A, k), 5 * FPS)
    files["cut"] = d / "cut.mp4"  # scene B for 2.4 s, then scene C
    write(files["cut"], lambda k: drift(B, k) if k < 60 else drift(C, k - 60), 5 * FPS)
    files["bad"] = d / "bad.mp4"  # black 1.0-1.8 s, heavy blur 2.8-3.6 s

    def bad(k):
        t = k / FPS
        img = drift(C, k, 0.3)
        if 1.0 <= t < 1.8:
            return np.zeros_like(img)
        if 2.8 <= t < 3.6:
            return cv2.GaussianBlur(img, (0, 0), 14)
        return img
    write(files["bad"], bad, 5 * FPS)
    files["fast"] = d / "fast.mp4"  # lots of motion

    def fast(k):
        img = drift(B, k, 9.0)
        cv2.rectangle(img, (int(40 + 18 * k) % (W0 - 80), 100), (int(40 + 18 * k) % (W0 - 80) + 80, 260), (255, 255, 255), -1)
        return img
    write(files["fast"], fast, 5 * FPS)
    files["dup"] = d / "calm_copy.mp4"
    shutil.copyfile(files["calm"], files["dup"])
    media = {k: upload(client, v) for k, v in files.items()}
    from app import templates as TP
    song = config.DATA_ROOT / "p12song.wav"
    TP.synth_music(120, 40, song, drop_at=12.0)
    s = upload(client, song)
    assert wait_job(client, client.post(f"/api/media/{s['id']}/beats").json()["jobId"], timeout=120)["status"] == "done"
    media["song"] = client.get(f"/api/media/{s['id']}").json()
    return media


@pytest.fixture(scope="module")
def analysed(client, footage):
    ids = [footage[k]["id"] for k in ("calm", "cut", "bad", "fast", "dup")]
    r = client.post("/api/autoedit/analyze", json={"media": ids})
    assert r.status_code == 200, r.text
    j = wait_job(client, r.json()["jobId"], timeout=300)
    assert j["status"] == "done", j
    return j["result"]


def test_shots_unusable_moments_and_duplicates(footage, analysed):
    cut = AA.cached(footage["cut"]["id"])
    assert len(cut["shots"]) == 2 and abs(cut["shots"][1]["start"] - 2.4) < 0.3, [(s["start"], s["end"]) for s in cut["shots"]]
    bad = AA.cached(footage["bad"]["id"])
    kinds = {u[2] for u in bad["unusable"]}
    assert "black" in kinds and "blurry" in kinds, bad["unusable"]
    blk = [u for u in bad["unusable"] if u[2] == "black"]
    assert abs(blk[0][0] - 1.0) < 0.2 and abs(blk[-1][1] - 1.8) < 0.25
    fast, calm = AA.cached(footage["fast"]["id"]), AA.cached(footage["calm"]["id"])
    assert np.mean([s["energy"] for s in fast["samples"]]) > 3 * np.mean([s["energy"] for s in calm["samples"]])
    pair = {frozenset((a[0], b[0])) for a, b, _ in [(x[0], x[1], x[2]) for x in analysed["duplicates"]]}
    assert frozenset((footage["calm"]["id"], footage["dup"]["id"])) in pair
    assert analysed["speed"] > 1.0  # seconds of footage per second of processing


def test_analysis_is_cached_and_can_be_cancelled(client, footage, analysed):
    j = wait_job(client, client.post("/api/autoedit/analyze", json={"media": [footage["calm"]["id"]]}).json()["jobId"])
    assert j["status"] == "done" and j["result"]["seconds"] < 0.5  # cached
    r = client.post("/api/autoedit/analyze", json={"media": [footage["fast"]["id"], footage["bad"]["id"]], "force": True}).json()
    client.post(f"/api/jobs/{r['jobId']}/cancel")
    j = wait_job(client, r["jobId"])
    assert j["status"] == "cancelled"
    assert AA.cached(footage["fast"]["id"])  # the earlier result is still there


def ids(footage):
    return [footage[k]["id"] for k in ("calm", "cut", "bad", "fast", "dup")]


def test_draft_on_beats_drop_and_variety(client, footage, analysed):
    r = client.post("/api/autoedit/draft", json={"media": ids(footage), "music": footage["song"]["id"], "style": "energetic"})
    assert r.status_code == 200, r.text
    d = r.json()
    beats = [b - d["musicStart"] for b in footage["song"]["beats"]]
    for s in d["slots"]:
        assert min(abs(s["start"] - b) for b in beats) < 1e-3, s["start"]  # every cut on a beat
        pk = s["pick"]
        assert pk, s
        bad = AA.cached(pk["mediaId"])
        assert not any(u[0] < pk["out"] and pk["in"] < u[1] for u in bad["unusable"]), (pk, bad["unusable"])
    drop = next(s for s in d["slots"] if s["section"] == "drop")
    assert drop["pick"]["mediaId"] == footage["fast"]["id"], drop["pick"]  # the strongest (most motion) on the drop
    assert "strongest shot, on the drop" in drop["pick"]["reasons"]
    assert abs(d["dropAt"] - (footage["song"]["beats"][0] * 0 + d["dropAt"])) < 1e-9
    seq = [(s["pick"]["mediaId"], s["pick"]["shot"]) for s in d["slots"]]
    dup = {footage["calm"]["id"], footage["dup"]["id"]}
    for a, b in zip(seq, seq[1:]):
        assert not (a[0] in dup and b[0] in dup), seq  # near-duplicates never back to back
        assert a != b, [(s["section"], s["dur"], s["pick"]["mediaId"][:4], s["pick"]["shot"], s["pick"]["in"], round(s["pick"]["score"], 2), [(x["mediaId"][:4], x["shot"], round(x["score"], 2)) for x in s["alternatives"][:3]]) for s in d["slots"]]
    # calm on the intro, motion on the peak
    intro = [s["pick"]["parts"]["energy"] for s in d["slots"] if s["section"] == "intro"]
    peak = [s["pick"]["parts"]["energy"] for s in d["slots"] if s["section"] in ("drop", "peak")]
    assert np.mean(peak) > np.mean(intro)


def test_locks_excludes_and_styles(client, footage, analysed):
    base = client.post("/api/autoedit/draft", json={"media": ids(footage), "music": footage["song"]["id"], "style": "energetic"}).json()
    s0 = base["slots"][0]
    alt = s0["alternatives"][0]
    locks = {"0": {"mediaId": alt["mediaId"], "shot": alt["shot"], "in": alt["in"]}}
    d = client.post("/api/autoedit/draft", json={"media": ids(footage), "music": footage["song"]["id"], "style": "energetic", "locks": locks,
                                                 "exclude": [footage["cut"]["id"]]}).json()
    assert d["slots"][0]["pick"]["mediaId"] == alt["mediaId"] and d["slots"][0]["pick"]["locked"]
    assert abs(d["slots"][0]["pick"]["in"] - alt["in"]) < 1e-6
    assert all(s["pick"]["mediaId"] != footage["cut"]["id"] for s in d["slots"])
    lens = {}
    for st in ("energetic", "cinematic", "chill"):
        x = client.post("/api/autoedit/draft", json={"media": ids(footage), "music": footage["song"]["id"], "style": st}).json()
        lens[st] = (np.mean([s["dur"] for s in x["slots"]]), x["length"])
    assert lens["energetic"][0] < lens["cinematic"][0] <= lens["chill"][0] + 1e-6, lens
    chill = client.post("/api/autoedit/draft", json={"media": ids(footage), "music": footage["song"]["id"], "style": "chill"}).json()
    assert any(abs(s["speed"] - 0.75) < 1e-6 for s in chill["slots"])  # soft slow motion on calm sections


def test_create_project_and_export(client, footage, analysed):
    d = client.post("/api/autoedit/draft", json={"media": ids(footage), "music": footage["song"]["id"], "style": "energetic",
                                                 "texts": {"hook": "test reel", "outro": "bye"}}).json()
    j = wait_job(client, client.post("/api/autoedit/create", json={"draft": d, "name": "AE test"}).json()["jobId"], timeout=300)
    assert j["status"] == "done", (j.get("error"), j.get("log"))
    p = client.get(f"/api/projects/{j['result']['projectId']}").json()
    main = p["tracks"][0]["clips"]
    assert len(main) == len(d["slots"])
    assert [round(c["start"], 3) for c in main] == [round(s["start"], 3) for s in d["slots"]]
    texts = [c["text"]["content"] for t in p["tracks"] if t["type"] == "text" for c in t["clips"]]
    assert texts == ["TEST REEL", "bye"]
    assert not [c for t in p["tracks"] if t["type"] == "audio" for c in t["clips"]]  # no sound effects
    drop = next(c for c in main if c.get("speedRamp"))
    assert drop["slowmo"]["quality"] == "ai"
    assert any("strongest shot, on the drop" in line for line in j["result"]["report"])
    # the landscape footage in a 9:16 frame is reframed on its subject
    assert sum(1 for c in main if c.get("reframe")) >= len(main) // 2
    # export (AI slow-mo blends in tests): cuts land on the planned frames
    import os
    os.environ["LIGXT_SLOWMO_TEST"] = "1"
    import sys
    os.environ["LIGXT_AI_PYTHON"] = sys.executable
    r = client.post(f"/api/projects/{p['id']}/export", json={"mode": "final"})
    e = wait_job(client, r.json()["jobId"], timeout=600)
    assert e["status"] == "done", (e.get("error"), (e.get("log") or "")[-1200:])
    assert e["result"]["sync"]["ok"]


def test_template_structure(client, footage, analysed):
    tpl = client.get("/api/templates").json()["templates"]
    tid = next(t["id"] for t in tpl if t["builtin"] and t["mediaSlots"] >= 3)
    d = client.post("/api/autoedit/draft", json={"media": ids(footage), "music": footage["song"]["id"], "style": "energetic", "template": tid}).json()
    assert d["template"] == tid and len(d["slots"]) >= 3
    j = wait_job(client, client.post("/api/autoedit/create", json={"draft": d, "name": "AE template"}).json()["jobId"], timeout=300)
    assert j["status"] == "done", (j.get("error"), j.get("log"))
    p = client.get(f"/api/projects/{j['result']['projectId']}").json()
    main = sorted(p["tracks"][0]["clips"], key=lambda c: c["start"])
    picked = [s["pick"] for s in d["slots"] if s["pick"]]
    for c, pk in zip(main, picked):
        if c["mediaId"] == pk["mediaId"] and AA.cached(pk["mediaId"])["type"] == "video":
            assert abs(c["inPoint"] - pk["in"]) < 0.06 or c["inPoint"] < pk["in"], (c["inPoint"], pk["in"])  # the chosen moment
