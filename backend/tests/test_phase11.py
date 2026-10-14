"""Phase 11: motion tracking. A synthetic clip with a textured object on a known path (hidden for 0.4 s) gives
exact ground truth: the box tracker follows it and flags the gap; text follows it in the export; a tracked
pixelate region stays on it; auto-reframe keeps it centred without showing an edge; tracks survive split /
speed / slow motion; the face tracker's association (stand-in detector); re-tracking from a fixed frame;
templates pin a caption to the main face; the TS port matches (shared/follow_cases.json)."""
import json
import math
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from app import follow as FO
from app import tracking as TR
from test_phase1 import upload, wait_job

ROOT = Path(__file__).resolve().parents[2]
W0, H0, FPS, DUR = 640, 360, 25, 4.0
HIDE = (2.0, 2.4)
BGC = (40, 60, 80)  # BGR, not black


def obj_pos(t: float) -> tuple[float, float]:
    """Centre of the object (source px) at time t."""
    return 120 + 100 * t, 180 + 50 * math.sin(t * 1.6)


def ff(*a):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *a], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory, client):
    d = tmp_path_factory.mktemp("p11")
    n = int(DUR * FPS)
    rng = np.random.default_rng(3)
    bg = np.full((H0, W0, 3), BGC, np.uint8)
    # faint static texture so the frame isn't flat
    bg = np.clip(bg.astype(int) + rng.integers(-6, 7, (H0, W0, 1)), 0, 255).astype(np.uint8)
    obj = np.zeros((56, 56, 3), np.uint8)
    for y in range(56):
        for x in range(56):
            obj[y, x] = (30, 140, 250) if ((x // 8) + (y // 8)) % 2 == 0 else (250, 250, 250)
    cv2.circle(obj, (28, 28), 10, (40, 200, 40), -1)
    path = d / "obj.mp4"
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W0}x{H0}", "-r", str(FPS), "-i", "-",
                             "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(path)], stdin=subprocess.PIPE)
    for k in range(n):
        t = k / FPS
        f = bg.copy()
        if not (HIDE[0] <= t < HIDE[1]):
            cx, cy = obj_pos(t)
            x0, y0 = int(round(cx - 28)), int(round(cy - 28))
            f[y0:y0 + 56, x0:x0 + 56] = obj
        proc.stdin.write(f.tobytes())
    proc.stdin.close()
    proc.wait()
    return {"obj": upload(client, path)}


def _track(client, m, seed_t=0.5, direction="both", track=None, box=None):
    cx, cy = obj_pos(seed_t)
    box = box or [(cx - 30) / W0, (cy - 30) / H0, 60 / W0, 60 / H0]
    r = client.post("/api/tracking/start", json={"mediaId": m["id"], "method": "box", "seed": {"t": seed_t + 0.5 / FPS, "box": box},
                                                "range": [0, DUR], "direction": direction, "track": track})
    assert r.status_code == 200, r.text
    j = wait_job(client, r.json()["jobId"], timeout=120)
    assert j["status"] == "done", (j.get("error"), j.get("log"))
    return j["result"]


@pytest.fixture(scope="module")
def tracked(client, media):
    return _track(client, media["obj"])


def test_box_tracker_follows_and_flags_the_gap(tracked):
    tr = tracked["track"]
    assert tr["kind"] == "object" and tr["method"] == "box" and tr["mediaId"]
    errs, gap_conf, after = [], [], []
    for k in range(int(DUR * FPS) - 1):
        t = k / FPS
        e = FO.track_at(tr, t + 0.5 / FPS, interp=False)
        if HIDE[0] + 0.12 <= t < HIDE[1]:
            gap_conf.append(e["c"])
            continue
        if HIDE[0] <= t < HIDE[0] + 0.12:
            continue
        cx, cy = obj_pos(t)
        err = math.hypot(e["x"] * W0 - cx, e["y"] * H0 - cy)
        (after if t > HIDE[1] + 0.2 else errs).append(err)
    assert max(errs) < 4.0, max(errs)  # source px, before the gap
    assert sum(1 for c in gap_conf if c <= 0) >= len(gap_conf) - 1, gap_conf  # flagged as lost while hidden
    assert np.median(after) < 4.0 and max(after) < 10, (np.median(after), max(after))  # picked up again
    assert tracked["stats"]["lost"] >= 5


def _project(client, clips, tracks, W=640, H=360, motion=None, **kw):
    p = client.post("/api/projects", json={"name": "Tracking test", "aspect": "16:9"}).json()
    p["settings"].update(width=W, height=H, fps=25)
    p["settings"]["background"] = {"mode": "fit", "color": "#000000"}
    p["export"].update(width=W, height=H, preset="custom", fps=25)
    p["tracks"][0]["clips"] = clips
    for t in p["tracks"]:
        if t["type"] in tracks:
            t["clips"] = tracks[t["type"]]
    p["motionTracks"] = motion or []
    p.update(kw)
    return p


def _export(client, p):
    r = client.post(f"/api/projects/{p['id']}/export", json={"project": p, "mode": "final"})
    assert r.status_code == 200, r.text
    j = wait_job(client, r.json()["jobId"], timeout=300)
    assert j["status"] == "done", (j.get("error"), (j.get("log") or "")[-1500:])
    return j["result"]["path"]


def frames_of(path, w, h):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)


def centroid(mask):
    ys, xs = np.nonzero(mask)
    return (float(xs.mean()), float(ys.mean())) if len(xs) > 20 else None


def test_text_follows_the_object_in_the_export(client, media, tracked):
    m = media["obj"]
    tr = tracked["track"]
    main = {"id": "v", "mediaId": m["id"], "start": 0, "inPoint": 0.2, "outPoint": 1.9, "fit": "fit"}
    text = {"id": "tx", "mediaId": None, "start": 0, "inPoint": 0, "outPoint": 1.7, "transform": {"x": 0, "y": -60},
            "text": {"content": "HI", "font": "Anton-Regular", "size": 40, "color": "#ff00ff", "shadow": {"enabled": False}},
            "follow": {"trackId": tr["id"], "clipId": "v", "smoothing": 0.0}}
    p = _project(client, [main], {"text": [text]}, motion=[tr])
    E = frames_of(_export(client, p), 640, 360)
    offs = []
    for k in range(2, len(E) - 2, 3):
        f = E[k].astype(int)
        mag = (f[..., 0] > 180) & (f[..., 2] > 180) & (f[..., 1] < 90)  # magenta text
        c = centroid(mag)
        ox, oy = obj_pos(0.2 + k / 25)
        assert c is not None, k
        offs.append((c[0] - ox, c[1] - oy))
    dx = [o[0] for o in offs]
    dy = [o[1] for o in offs]
    assert max(abs(v) for v in dx) < 4 and all(-70 < v < -50 for v in dy), offs
    assert np.std(dy) < 1.5, np.std(dy)


def test_pixelate_region_stays_on_the_object(client, media, tracked):
    m = media["obj"]
    tr = tracked["track"]
    main = {"id": "v", "mediaId": m["id"], "start": 0, "inPoint": 0.2, "outPoint": 1.9, "fit": "fit"}
    reg = {"id": "rg", "mediaId": None, "adjustment": True, "start": 0, "inPoint": 0, "outPoint": 1.7,
           "region": {"effect": "pixelate", "amount": 100, "padding": 0.2, "feather": 0.1, "shape": "rect"},
           "follow": {"trackId": tr["id"], "clipId": "v", "smoothing": 0.1}}
    p = _project(client, [main], {"overlay": [reg]}, motion=[tr])
    E = frames_of(_export(client, p), 640, 360)
    p0 = _project(client, [main], {}, motion=[tr])
    O = frames_of(_export(client, p0), 640, 360)
    for k in (3, 15, 30):
        ox, oy = obj_pos(0.2 + k / 25)
        x0, y0 = int(ox - 20), int(oy - 20)
        inner_e = E[k][y0:y0 + 40, x0:x0 + 40].astype(float)
        inner_o = O[k][y0:y0 + 40, x0:x0 + 40].astype(float)
        hf = lambda a: float(np.abs(np.diff(a, axis=1)).mean())  # noqa: E731
        assert hf(inner_e) < 0.5 * hf(inner_o), (k, hf(inner_e), hf(inner_o))  # the checker is pixelated
        far = (slice(20, 60), slice(560, 620))  # background far from the object: untouched
        assert np.abs(E[k][far].astype(float) - O[k][far].astype(float)).mean() < 2.0


def test_auto_reframe_keeps_the_subject_centred(client, media, tracked):
    m = media["obj"]
    tr = tracked["track"]
    main = {"id": "v", "mediaId": m["id"], "start": 0, "inPoint": 0.2, "outPoint": 1.9, "fit": "fill",
            "reframe": {"trackId": tr["id"], "third": "center", "smoothing": 0.4}}
    p = _project(client, [main], {}, W=360, H=640, motion=[tr])
    p["settings"]["background"]["mode"] = "fill"
    E = frames_of(_export(client, p), 360, 640)
    xs = []
    for k in range(2, len(E) - 2, 2):
        f = E[k].astype(int)
        org = (f[..., 2] > 200) & (f[..., 1] > 100) & (f[..., 1] < 180) & (f[..., 0] < 80)  # the orange squares
        c = centroid(org)
        assert c is not None, k
        xs.append(c[0])
        # never an edge: the outer columns show the clip's background, not the project's black
        assert f[:, :3].mean() > 20 and f[:, -3:].mean() > 20, k
    assert max(abs(x - 180) for x in xs) < 14, xs  # on the centre line (the object moves 400 px in the source)
    # smooth camera: no jumps between frames
    assert max(abs(a - b) for a, b in zip(xs, xs[1:])) < 6


def test_tracks_survive_split_speed_and_slow_motion(media, tracked):
    m = media["obj"]
    tr = tracked["track"]
    metas = {m["id"]: m}
    base = {"mediaId": m["id"], "fit": "fit", "transform": {}, "keyframes": {}}
    # split into two clips (the follower names the first), the second slowed 2x with blending
    a = {**base, "id": "a", "start": 0, "inPoint": 0.2, "outPoint": 0.8, "speed": 1}
    b = {**base, "id": "b", "start": 0.6, "inPoint": 0.8, "outPoint": 1.4, "speed": 0.5, "slowmo": {"quality": "blend"}}
    text = {"id": "t", "mediaId": None, "start": 0, "inPoint": 0, "outPoint": 1.8, "transform": {}, "keyframes": {},
            "text": {"content": "x"}, "follow": {"trackId": tr["id"], "clipId": "a", "smoothing": 0.0}}
    proj = {"settings": {"width": 640, "height": 360, "background": {"mode": "fit"}}, "motionTracks": [tr],
            "tracks": [{"type": "video", "clips": [a, b]}, {"type": "text", "clips": [text]}]}
    am = FO.resolve(proj, metas, 25.0)["t"]["autoMotion"]
    for tau in (0.1, 0.5, 0.7, 1.0, 1.5):
        src = 0.2 + tau if tau < 0.6 else 0.8 + (tau - 0.6) * 0.5
        ox, oy = obj_pos(src)
        v = FO.auto_at(am, tau)
        assert abs(320 + v["x"] - ox) < 4 and abs(180 + v["y"] - oy) < 4, (tau, v, ox, oy)


def test_face_tracking_keeps_the_chosen_face(monkeypatch):
    """Association with a stand-in detector: two 'faces' (bright discs) cross paths; the chosen one hides for a
    while and comes back. The tracker must stay on it, hold while hidden, and pick it up again."""
    W, H, n = 320, 180, 60

    def pos(i, which):
        return (60 + 3 * i, 90) if which == 0 else (260 - 3 * i, 50)

    class Store:
        w, h = W, H

        def __len__(self):
            return n

        def __getitem__(self, i):
            img = np.zeros((H, W, 3), np.uint8)
            for which, col in ((0, (200, 160, 120)), (1, (90, 200, 60))):
                if which == 0 and 25 <= i < 35:
                    continue  # the chosen face is hidden
                x, y = pos(i, which)
                cv2.circle(img, (int(x), int(y)), 14, col, -1)
            return img

    def fake_detect(img, det):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        nlab, lab, st, cen = cv2.connectedComponentsWithStats((g > 30).astype(np.uint8))
        return [{"box": [float(st[i, 0]), float(st[i, 1]), float(st[i, 2]), float(st[i, 3])], "c": (float(cen[i, 0]), float(cen[i, 1])),
                 "score": 0.9, "angle": 0.0} for i in range(1, nlab) if st[i, 4] > 50]

    monkeypatch.setattr(TR, "detect_faces", fake_detect)
    monkeypatch.setattr(TR, "_yunet", lambda w, h: None)
    res = TR.track_face(Store(), 5, {"point": [pos(5, 0)[0] / W, pos(5, 0)[1] / H]}, "both")
    for i in range(n):
        box, _, conf = res[i]
        cx = box[0] + box[2] / 2
        if 25 <= i < 35:
            if i >= 25 + TR.BRIDGE_FRAMES:
                assert conf == 0.0, (i, conf)
            continue
        assert abs(cx - pos(i, 0)[0]) < 4, (i, cx, pos(i, 0))  # the chosen face, never the other one
        assert conf > 0.5


def test_retrack_from_a_fixed_frame(client, media, tracked):
    m = media["obj"]
    tr = json.loads(json.dumps(tracked["track"]))
    # spoil frames 40..60 (as if the tracker had jumped), then fix frame 40 and re-track forward
    k0 = tr["k0"]
    for k in range(40, 61):
        tr["data"][k - k0][0] = 0.9
    t40 = 40 / FPS
    cx, cy = obj_pos(t40)
    res = _track(client, m, seed_t=t40, direction="forward", track=tr, box=[(cx - 30) / W0, (cy - 30) / H0, 60 / W0, 60 / H0])
    t2 = res["track"]
    for k in (41, 45, 49):
        e = FO.track_at(t2, k / FPS + 0.5 / FPS, interp=False)
        ox, oy = obj_pos(k / FPS)
        assert abs(e["x"] * W0 - ox) < 4, (k, e["x"] * W0, ox)
    # frames before the fix are kept as they were
    e = FO.track_at(t2, 0.5 + 0.5 / FPS, interp=False)
    assert abs(e["x"] * W0 - obj_pos(0.5)[0]) < 4
    assert any(k["k"] == 40 for k in t2["keys"])


def test_shared_follow_cases_are_current():
    """shared/follow_cases.json (checked by the TS port) is what follow.py computes now."""
    old = (ROOT / "shared" / "follow_cases.json").read_text(encoding="utf-8")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_follow_cases.py")], check=True, capture_output=True)
    assert (ROOT / "shared" / "follow_cases.json").read_text(encoding="utf-8") == old


def test_template_pins_a_caption_to_the_main_face(client, media, tracked, monkeypatch):
    """A caption following a face track is saved as "follow the main face"; a new project made from the template
    tracks the main face of the shot under the caption and pins the caption to it. (The synthetic clip has no real
    face: the face finder is replaced by one that 'finds' the moving object.)"""
    from app import config
    from app import templates as TP
    m = media["obj"]
    song = config.DATA_ROOT / "p11song.wav"
    TP.synth_music(120, 12, song)
    s = upload(client, song)
    assert wait_job(client, client.post(f"/api/media/{s['id']}/beats").json()["jobId"], timeout=120)["status"] == "done"
    face = {**tracked["track"], "id": "trk_face", "kind": "face", "method": "face", "name": "Face 1"}
    obj = {**tracked["track"], "id": "trk_obj", "name": "Object 1"}
    main = {"id": "v", "mediaId": m["id"], "start": 0, "inPoint": 0.2, "outPoint": 2.2, "fit": "fill"}
    text = {"id": "tx", "mediaId": None, "start": 0.5, "inPoint": 0, "outPoint": 1.0, "transform": {"x": 0, "y": -90},
            "text": {"content": "THAT'S ME"}, "follow": {"trackId": "trk_face", "clipId": "v", "smoothing": 0.2, "safe": True}}
    text2 = {"id": "tx2", "mediaId": None, "start": 1.5, "inPoint": 0, "outPoint": 0.5, "text": {"content": "obj"},
             "follow": {"trackId": "trk_obj", "clipId": "v"}}
    p = _project(client, [main], {"text": [text, text2]}, W=360, H=640, motion=[face, obj])
    mu = next(t for t in p["tracks"] if t["type"] == "music")
    mu["clips"] = [{"id": "m", "mediaId": s["id"], "start": 0, "inPoint": 0, "outPoint": 6}]
    r = client.post("/api/templates", json={"project": p, "name": "Face pin tpl"})
    assert r.status_code == 200, r.text
    tid = r.json()["template"]["id"]
    from app.templates_api import _load
    tt = {x["placeholder"]: x for x in _load(tid)["texts"]}
    assert tt["THAT'S ME"]["follow"] == {"target": "mainFace", "smoothing": 0.2, "safe": True}
    assert tt["obj"]["follow"] is None  # object tracks don't carry over to new media

    calls = []

    def fake_faces(meta, t):
        cx, cy = obj_pos(t)
        return [{"box": [(cx - 28) / W0, (cy - 28) / H0, 56 / W0, 56 / H0], "score": 0.95}]

    real_run = TR.run_track

    def run_box(meta, req, ctx=None):  # track the stand-in "face" with the box tracker, as a face track
        calls.append(req)
        out = real_run(meta, {**req, "method": "box"}, ctx)
        out["track"].update(kind="face", method="face")
        return out

    monkeypatch.setattr(TR, "faces_at", fake_faces)
    monkeypatch.setattr(TR, "run_track", run_box)
    g = client.post(f"/api/templates/{tid}/generate", json={"media": [m["id"]], "music": s["id"]}).json()
    j = wait_job(client, g["jobId"], timeout=180)
    assert j["status"] == "done", j
    gp = client.get(f"/api/projects/{j['result']['projectId']}").json()
    cap = next(c for t in gp["tracks"] if t["type"] == "text" for c in t["clips"] if c["text"]["content"] == "THAT'S ME")
    assert cap["follow"]["trackId"] in [x["id"] for x in gp["motionTracks"]]
    assert cap["follow"]["clipId"] == gp["tracks"][0]["clips"][0]["id"]
    assert cap["transform"]["y"] == -90  # the offset from the face is kept
    assert len(calls) == 1 and any("pinned to the main face" in line for line in j["result"]["report"])
    client.delete(f"/api/templates/{tid}")


def test_transition_between_frames_repeats_no_frame(client, media):
    """A whip centred on a cut that falls between output frames: the transition piece used to start up to half a
    frame early, so a clip running at the timeline's rate showed its last solid frame twice (found in Travel Dump v2)."""
    m = media["obj"]
    a = {"id": "a", "mediaId": m["id"], "start": 0, "inPoint": 0.0, "outPoint": 1.01, "fit": "fit"}  # cut at 25.25 frames
    b = {"id": "b", "mediaId": m["id"], "start": 1.01, "inPoint": 2.6, "outPoint": 3.6, "fit": "fit"}
    p = _project(client, [a, b], {})
    p["transitions"] = [{"trackId": p["tracks"][0]["id"], "fromClipId": "a", "toClipId": "b", "type": "whipleft", "duration": 0.3}]
    E = frames_of(_export(client, p), 640, 360).astype(np.float32)
    d = [float(np.abs(E[k] - E[k - 1]).mean()) for k in range(1, 30)]
    assert min(d[:24]) > 0.3, [round(x, 2) for x in d]  # the object moves 4 px every frame: no frame shown twice
