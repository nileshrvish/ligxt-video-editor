"""Phase 7: templates (save from a project, built-ins, generate on a new song, face-aware motion,
text placement, media count handling, zip export/import without media, version migration)."""
import io
import json
import subprocess
import zipfile

import numpy as np
import pytest

from app import templates as TP
from app.media import update_meta
from app import motion as M
from test_phase1 import upload, wait_job


def ff(*a):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *a], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory, client):
    d = tmp_path_factory.mktemp("p7")
    out = {}
    for i, col in enumerate(["0x804020", "0x204080", "0x408020", "0x806040", "0x305070", "0x707030"]):
        f = d / f"photo{i}.png"
        ff("-f", "lavfi", "-i", f"color=c={col}:s=900x1200", "-frames:v", "1", str(f))
        out[f"photo{i}"] = upload(client, f)
    for bpm in (120, 132):
        f = d / f"song{bpm}.wav"
        TP.synth_music(bpm, 40, f)
        m = upload(client, f)
        job = client.post(f"/api/media/{m['id']}/beats").json()["jobId"]
        assert wait_job(client, job, timeout=120)["status"] == "done"
        out[f"song{bpm}"] = client.get(f"/api/media/{m['id']}").json()
    return out


def _project(client, media):
    """A small beat-synced project: 6 shots on the 120 BPM song, transitions, a zoom, a punch, a
    grade layer, hook + beat caption + outro, a whoosh and a hit, Drop/Outro markers."""
    song = media["song120"]
    beats = song["beats"]
    b0 = 4  # cut grid from the 5th beat of the song
    grid = TP.Grid(beats)
    T = lambda b: grid.time(b0 + b) - grid.time(b0)  # noqa: E731
    p = client.post("/api/projects", json={"name": "Tpl source", "aspect": "9:16"}).json()
    main = p["tracks"][0]
    lens = [4, 4, 2, 2, 2, 4]
    starts = np.cumsum([0] + lens[:-1])
    order = ["photo0", "photo1", "photo2", "photo0", "photo3", "photo4"]
    main["clips"] = []
    for i, (s, L) in enumerate(zip(starts, lens)):
        D = T(int(s + L)) - T(int(s))
        main["clips"].append({"id": f"c{i}", "mediaId": media[order[i]]["id"], "start": round(T(int(s)), 4), "inPoint": 0, "outPoint": round(D, 4),
                              "fit": "fill", "keyframes": {"scale": [{"time": 0, "value": 1.0, "easing": "inout"}, {"time": round(D, 4), "value": 1.12, "easing": "linear"}]}
                              if i == 0 else {"punch": [{"time": 0, "value": 1, "easing": "punch"}, {"time": 0.08, "value": 1.08, "easing": "out"}, {"time": 0.3, "value": 1, "easing": "linear"}]}
                              if i == 2 else {}})
    p["transitions"] = [{"id": "t1", "trackId": main["id"], "fromClipId": "c0", "toClipId": "c1", "type": "whipleft", "duration": round(T(4.25) - T(3.75), 4)},
                        {"id": "t2", "trackId": main["id"], "fromClipId": "c1", "toClipId": "c2", "type": "zoomin", "duration": round(T(8.25) - T(7.75), 4)}]
    total = T(18)
    ov = next(t for t in p["tracks"] if t["type"] == "overlay")
    ov["name"] = "Grade"
    ov["clips"] = [{"mediaId": None, "adjustment": True, "start": 0, "inPoint": 0, "outPoint": round(total, 4),
                    "filters": [{"type": "preset", "name": "warmfilm", "intensity": 0.6, "params": {}}]}]
    tt = next(t for t in p["tracks"] if t["type"] == "text")
    tt["clips"] = [
        {"mediaId": None, "start": 0, "inPoint": 0, "outPoint": round(T(4), 4), "transform": {"x": 0, "y": -560},
         "text": {"content": "HOOK TITLE", "font": "Anton-Regular", "size": 140, "animIn": {"type": "pop", "duration": 0.3, "easing": "punch"}}},
        {"mediaId": None, "start": round(T(8), 4), "inPoint": 0, "outPoint": round(T(11) - T(8), 4), "transform": {"x": 0, "y": -560},
         "text": {"content": "one two three", "font": "Anton-Regular", "size": 110, "highlight": {"style": "revealpop", "color": "#ffb347"},
                  "words": [{"text": w, "start": round(T(8 + k) - T(8), 4), "end": round(T(9 + k) - T(8), 4)} for k, w in enumerate(["one", "two", "three"])]}},
        {"mediaId": None, "start": round(T(14), 4), "inPoint": 0, "outPoint": round(total - T(14), 4), "transform": {"x": 0, "y": -560},
         "text": {"content": "see you soon", "font": "DMSerifDisplay-Italic", "size": 80}},
    ]
    sfx = {s: client.post(f"/api/media/sfx/{s}/import").json()["media"] for s in ("whoosh", "hit")}
    au = next(t for t in p["tracks"] if t["type"] == "audio")
    au["clips"] = [{"mediaId": sfx["whoosh"]["id"], "start": round(T(4) - sfx["whoosh"]["sfxPeak"], 4), "inPoint": 0, "outPoint": sfx["whoosh"]["duration"], "volume": 0.4},
                   {"mediaId": sfx["hit"]["id"], "start": round(T(8), 4), "inPoint": 0, "outPoint": sfx["hit"]["duration"], "volume": 0.3}]
    mu = next(t for t in p["tracks"] if t["type"] == "music")
    mu["clips"] = [{"mediaId": song["id"], "start": 0, "inPoint": round(beats[b0], 4), "outPoint": round(beats[b0] + total, 4), "fadeOut": 1.0}]
    p["markers"] = [{"time": round(T(8), 4), "label": "Drop"}, {"time": round(T(14), 4), "label": "Outro"}]
    p["media"] = list({c["mediaId"] for t in p["tracks"] for c in t["clips"] if c.get("mediaId")})
    client.put(f"/api/projects/{p['id']}", json=p)
    return p


def test_builtin_templates_load(client):
    r = client.get("/api/templates").json()
    ids = {t["id"] for t in r["templates"] if t["builtin"]}
    assert {"quick-beat-montage", "ken-burns-slideshow", "before-after-reveal"} <= ids
    for t in r["templates"]:
        if t["builtin"]:
            assert t["thumb"] and t["preview"] and t["music"]["bundled"] and t["version"] == TP.TEMPLATE_VERSION
    assert "Slideshow" in r["categories"]
    assert [t["id"] for t in client.get("/api/templates?q=before").json()["templates"]] == ["before-after-reveal"]


def test_save_and_generate_on_a_new_song(client, media):
    p = _project(client, media)
    r = client.post("/api/templates", json={"project": p, "name": "My beat template", "tags": ["test"], "exportPreset": "instagram"})
    assert r.status_code == 200, r.text
    body = r.json()
    tid = body["template"]["id"]
    assert any("not marked license-safe" in n for n in body["notes"]), "the user's song is not bundled"
    t = client.get(f"/api/templates/{tid}").json()["template"]
    assert [s["lengthBeats"] for s in t["slots"]] == [4, 4, 2, 2, 2, 4]
    assert [s["ref"] for s in t["slots"]] == [0, 1, 2, 0, 3, 4], "photo0 repeats as the same shot"
    assert t["slots"][0]["transitionOut"]["type"] == "whipleft" and abs(t["slots"][0]["transitionOut"]["beats"] - 0.5) < 0.02
    assert [x["role"] for x in t["texts"]] == ["hook", "caption", "outro"]
    assert [w["beat"] for w in t["texts"][1]["words"]] == pytest.approx([0, 1, 2], abs=0.02)
    assert {e["sfx"] for e in t["sfx"]} == {"whoosh", "hit"} and t["sfx"][0]["beat"] == pytest.approx(4, abs=0.02)
    assert [s["name"] for s in t["sections"]] == ["Intro", "Drop", "Outro"]
    assert wait_job(client, body["previewJobId"], timeout=300)["status"] == "done"
    assert client.get(f"/api/templates/{tid}").json()["preview"]

    # generate on the 132 BPM song, from 3 s in, with different photos and new text
    song = media["song132"]
    new = [media[f"photo{i}"]["id"] for i in (5, 4, 3, 2, 1)]
    g = client.post(f"/api/templates/{tid}/generate", json={"media": new, "music": song["id"], "musicStart": 3.0,
                                                           "texts": {"hook1": "NEW HOOK", "caption1": "four five six seven"}, "name": "From template"}).json()
    job = wait_job(client, g["jobId"], timeout=120)
    assert job["status"] == "done", job
    q = client.get(f"/api/projects/{job['result']['projectId']}").json()
    beats = np.array(song["beats"])
    b0 = int(np.argmin(np.abs(beats - 3.0)))
    tl_beats = beats - beats[b0]
    cuts = [c["start"] for c in q["tracks"][0]["clips"]] + [q["tracks"][0]["clips"][-1]["start"] + q["tracks"][0]["clips"][-1]["outPoint"]]
    for c in cuts:
        assert np.min(np.abs(tl_beats - c)) < 0.002, f"cut at {c} is not on a beat of the new song"
    assert [round(c2 - c1, 3) for c1, c2 in zip(cuts, cuts[1:])] == pytest.approx([round(tl_beats[b0 + k2] - tl_beats[b0 + k1], 3) for k1, k2 in
                                                                                    zip([0, 4, 8, 10, 12, 14], [4, 8, 10, 12, 14, 18])], abs=0.003)
    mclip = next(t for t in q["tracks"] if t["type"] == "music")["clips"][0]
    assert mclip["inPoint"] == pytest.approx(beats[b0], abs=0.002)
    assert len(q["transitions"]) == 2 and q["transitions"][0]["type"] == "whipleft"
    texts = [c for t in q["tracks"] if t["type"] == "text" for c in t["clips"]]
    assert [c["text"]["content"] for c in texts] == ["NEW HOOK", "four five six seven", "see you soon"]
    w = texts[1]["text"]["words"]
    assert len(w) == 4 and all(np.min(np.abs(tl_beats - (texts[1]["start"] + x["start"]))) < 0.002 for x in w), "words stay on beats"
    au = [c for t in q["tracks"] if t["type"] == "audio" for c in t["clips"]]
    assert len(au) == 2
    whoosh = next(c for c in au if c["volume"] == 0.4)
    assert whoosh["start"] + 0.45 == pytest.approx(cuts[1], abs=0.01), "whoosh peak on the cut"
    assert [m["label"] for m in q["markers"]] == ["Drop", "Outro"]
    assert q["export"]["preset"] == "instagram"
    rep = " ".join(job["result"]["report"])
    assert any(f"{b} BPM" in rep for b in ("131", "132", "133")) or "BPM" in rep


def test_media_count_handling(client, media):
    p = _project(client, media)
    t, _ = TP.from_project(__import__("app.models", fromlist=["Project"]).Project.model_validate(p), "count")
    ids = [media[f"photo{i}"]["id"] for i in range(6)]
    few, rep = TP.generate(t, ids[:2], media["song120"]["id"], 0.0, color=False)
    assert "reused" in " ".join(rep)
    ms = [c.mediaId for c in few.tracks[0].clips]
    assert all(a != b for a, b in zip(ms, ms[1:])), "never the same file twice in a row"
    many, rep = TP.generate(t, ids + [ids[0]], media["song120"]["id"], 0.0, color=False)
    assert "replace repeated shots" in " ".join(rep)
    assert len({c.mediaId for c in many.tracks[0].clips}) == 6


def test_face_aware_motion_and_text(client, media):
    """A face at the very top is moved below the Reels header (photo extended); text moves off a face."""
    p = _project(client, media)
    from app.models import Project
    t, _ = TP.from_project(Project.model_validate(p), "faces")
    top, mid = media["photo5"]["id"], media["photo4"]["id"]
    update_meta(top, faces={"version": 1, "frames": [{"t": 0, "boxes": [[0.42, 0.01, 0.16, 0.12, 0.95]]}]})
    # a face exactly where the hook title sits (y -560 -> around 400 px on a 1920 canvas)
    update_meta(mid, faces={"version": 1, "frames": [{"t": 0, "boxes": [[0.4, 0.17, 0.2, 0.1, 0.93]]}]})
    q, rep = TP.generate(t, [mid, top, top, top, top], media["song120"]["id"], 0.0, color=False)
    W, H = 1080, 1920
    from app.media import load_meta
    for c in q.tracks[0].clips:
        if c.mediaId != top:
            continue
        cd = c.model_dump()
        for k in range(9):
            tau = M.clip_duration(cd) * k / 8
            for b in TP._face_boxes(cd, load_meta(top), tau, W, H, True):
                assert b[1] >= TP.FACE_TOP_MIN - 1, ("face under the header", b)
            assert TP._covered(cd, load_meta(top), tau, W, H, True, cd["extend"]), "edges never show"
        assert cd["extend"], "the photo was extended to fill the top"
    hook = next(c for tr in q.tracks if tr.type == "text" for c in tr.clips)
    faces = [b for k in range(5) for b in TP._face_boxes(q.tracks[0].clips[0].model_dump(), load_meta(mid), k * 0.4, W, H, True)]
    box = TP._text_box({**hook.text}, hook.transform.model_dump(), W, H)
    assert not any(TP._overlap(box, f) > 0 for f in faces), "hook title moved off the face"
    assert any("doesn't cover a face" in r for r in rep) and any("down" in r for r in rep)


def test_zip_export_import_has_no_media(client, media):
    p = _project(client, media)
    tid = client.post("/api/templates", json={"project": p, "name": "Zip me"}).json()["template"]["id"]
    data = client.get(f"/api/templates/{tid}/export.zip").content
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.namelist() == ["template.json"], "no media, no song (not license-safe), no preview unless asked"
    body = json.loads(z.read("template.json"))
    assert body["version"] == TP.TEMPLATE_VERSION and "mediaId" not in json.dumps(body)
    r = client.post("/api/templates/import", content=data, headers={"Content-Type": "application/zip"}).json()
    assert r["template"]["name"] == "Zip me" and r["template"]["id"] != tid and r["previewJobId"]
    # an older (version 0) template still imports
    body0 = {**body, "version": 0}
    for s in body0["slots"]:
        s["beats"] = s.pop("lengthBeats")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z0:
        z0.writestr("template.json", json.dumps(body0))
        z0.writestr("../evil.txt", "x")
    r0 = client.post("/api/templates/import", content=buf.getvalue(), headers={"Content-Type": "application/zip"}).json()
    t0 = client.get(f"/api/templates/{r0['template']['id']}").json()["template"]
    assert t0["version"] == TP.TEMPLATE_VERSION and t0["slots"][0]["lengthBeats"] == 4
    assert not (TP.USER_DIR.parent / "evil.txt").exists()
    assert client.delete(f"/api/templates/{r0['template']['id']}").json()["ok"]
    assert client.delete("/api/templates/quick-beat-montage").status_code == 400


def test_builtin_generates_with_its_bundled_song(client, media):
    g = client.post("/api/templates/before-after-reveal/generate", json={"media": [media["photo0"]["id"], media["photo1"]["id"]], "texts": {"label1": "OLD"}}).json()
    job = wait_job(client, g["jobId"], timeout=120)
    assert job["status"] == "done", job
    q = client.get(f"/api/projects/{job['result']['projectId']}").json()
    assert q["transitions"][0]["type"] == "wiperight"
    assert next(t for t in q["tracks"] if t["type"] == "music")["clips"][0]["mediaId"]


def test_color_match():
    s = {"a": {"r": 0.20, "g": 0.20, "b": 0.20}, "b": {"r": 0.05, "g": 0.05, "b": 0.05}, "c": {"r": 0.24, "g": 0.18, "b": 0.12}}
    g = TP.color_match(s, strength=1.0)
    assert g["b"]["exposure"] > 0.9 and g["a"]["exposure"] == pytest.approx(0, abs=0.35)
    assert g["c"]["temperature"] < 0, "a warm shot is cooled towards the group"
