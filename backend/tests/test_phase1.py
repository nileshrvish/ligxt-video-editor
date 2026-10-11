"""Phase 1: media import with proxies, project save/load/autosave, single-clip and multi-clip export."""
import time

from app.ffmpeg_utils import probe, stream_durations


def wait_job(client, job_id, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error", "cancelled"):
            return j
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} timed out")


def upload(client, path):
    r = client.post(f"/api/media/import?filename={path.name}", content=path.read_bytes())
    assert r.status_code == 200, r.text
    body = r.json()
    job = wait_job(client, body["jobId"])
    assert job["status"] == "done", job
    return client.get(f"/api/media/{body['media']['id']}").json()


def test_health(client):
    assert client.get("/api/health").json()["ok"]


def test_import_all_types(client, fixtures):
    v = upload(client, fixtures["landscape"])
    assert v["type"] == "video" and v["width"] == 1920 and v["height"] == 1080 and v["fps"] == 30
    assert v["hasAudio"] and abs(v["duration"] - 4) < 0.2
    assert v["proxyStatus"] == "ready" and v["thumbnail"] and v["waveform"]
    proxy_file = v["proxy"].replace("/files/cache/", "")
    from app.config import CACHE_DIR
    pinfo = probe(str(CACHE_DIR / proxy_file))
    assert max(pinfo["width"], pinfo["height"]) <= 960 and pinfo["videoCodec"] == "h264"
    # proxy is served with HTTP range support (needed for seeking in the browser)
    r = client.get(v["proxy"], headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100

    wf = client.get(v["waveform"]).json()
    assert wf["peaksPerSecond"] == 100 and len(wf["peaks"]) > 350

    img = upload(client, fixtures["photo"])
    assert img["type"] == "image" and img["proxy"].endswith("preview.jpg") and img["duration"] == 0

    music = upload(client, fixtures["music"])
    assert music["type"] == "audio" and music["proxyStatus"] == "notNeeded" and music["waveform"]

    portrait = upload(client, fixtures["portrait"])
    assert portrait["width"] == 1080 and portrait["height"] == 1920


def test_rejects_bad_files(client):
    r = client.post("/api/media/import?filename=notes.txt", content=b"hello")
    assert r.status_code == 415 and "Unsupported" in r.json()["detail"]
    r = client.post("/api/media/import?filename=broken.mp4", content=b"not a video at all")
    assert r.status_code == 422 and "could not be read" in r.json()["detail"]


def test_project_crud_and_autosave(client):
    p = client.post("/api/projects", json={"name": "Reel test", "aspect": "9:16"}).json()
    assert p["settings"]["width"] == 1080 and p["settings"]["height"] == 1920
    assert [t["type"] for t in p["tracks"]] == ["video", "overlay", "text", "audio", "music"]
    p["name"] = "Renamed"
    p["futureField"] = {"kept": True}  # unknown fields survive a round-trip
    assert client.put(f"/api/projects/{p['id']}", json=p).status_code == 200
    got = client.get(f"/api/projects/{p['id']}").json()
    assert got["name"] == "Renamed" and got["futureField"] == {"kept": True}

    for i in range(12):
        got["name"] = f"v{i}"
        r = client.post(f"/api/projects/{p['id']}/autosave", json=got)
        assert r.status_code == 200
        time.sleep(1.01) if i in (0, 11) else None  # make sure some timestamps differ
    autos = client.get(f"/api/projects/{p['id']}/autosaves").json()["autosaves"]
    assert 1 <= len(autos) <= 10
    assert any(x["id"] == p["id"] for x in client.get("/api/projects").json()["projects"])
    assert client.delete(f"/api/projects/{p['id']}").status_code == 200


def _project_with(client, clips_main, extra_tracks=None, aspect="9:16", **export):
    p = client.post("/api/projects", json={"name": "Export test", "aspect": aspect}).json()
    p["tracks"][0]["clips"] = clips_main
    for ttype, clips in (extra_tracks or {}).items():
        next(t for t in p["tracks"] if t["type"] == ttype)["clips"] = clips
    p["export"].update(export)
    p["media"] = list({c["mediaId"] for t in p["tracks"] for c in t["clips"]})
    client.put(f"/api/projects/{p['id']}", json=p)
    return p


def _export(client, p, **body):
    r = client.post(f"/api/projects/{p['id']}/export", json={"project": p, **body})
    assert r.status_code == 200, r.text
    job = wait_job(client, r.json()["jobId"], timeout=300)
    assert job["status"] == "done", (job["error"], job["log"])
    return job["result"]


def test_export_single_clip_final(client, fixtures):
    v = upload(client, fixtures["landscape"])
    p = _project_with(client, [{"mediaId": v["id"], "start": 0, "inPoint": 0.5, "outPoint": 3.0}])
    res = _export(client, p, mode="final")
    info = probe(res["path"])
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["videoCodec"] == "h264" and info["audioCodec"] == "aac" and info["pixFmt"] == "yuv420p"
    assert abs(info["duration"] - 2.5) < 0.1 and abs(info["fps"] - 30) < 0.01
    assert res["sync"]["ok"], res["sync"]


def test_export_sequence_draft_blur_gap_music_range(client, fixtures):
    v = upload(client, fixtures["landscape"])
    pr = upload(client, fixtures["portrait"])
    img = upload(client, fixtures["photo"])
    silent = upload(client, fixtures["silent"])
    music = upload(client, fixtures["music"])
    main = [
        {"mediaId": v["id"], "start": 0, "inPoint": 0, "outPoint": 2, "fit": "blur"},
        {"mediaId": img["id"], "start": 2, "inPoint": 0, "outPoint": 1.5, "fit": "fill"},
        # gap 3.5 .. 4.0 -> black
        {"mediaId": pr["id"], "start": 4, "inPoint": 0, "outPoint": 2, "volume": 0.5},
        {"mediaId": silent["id"], "start": 6, "inPoint": 0, "outPoint": 2, "speed": 2},
    ]
    music_clips = [{"mediaId": music["id"], "start": 0.5, "inPoint": 1, "outPoint": 8, "volume": 0.3,
                    "fadeIn": 1, "fadeOut": 1}]
    p = _project_with(client, main, {"music": music_clips}, aspect="16:9", width=1920, height=1080)
    res = _export(client, p, mode="draft")
    info = probe(res["path"])
    assert (info["width"], info["height"]) == (1920, 1080)
    assert info["pixFmt"] == "yuv420p"  # photo (full-range JPEG) must not leak yuvj420p
    assert abs(info["duration"] - 7.0) < 0.15, info["duration"]
    assert res["sync"]["ok"], res["sync"]

    # Range export: 1.0 .. 5.0
    res2 = _export(client, p, mode="draft", rangeStart=1.0, rangeEnd=5.0)
    d = stream_durations(res2["path"])
    assert abs(d["video"] - 4.0) < 0.1 and abs(d["audio"] - 4.0) < 0.1


def test_speed_keeps_audio_in_sync(client, fixtures):
    v = upload(client, fixtures["landscape"])
    p = _project_with(client, [{"mediaId": v["id"], "start": 0, "inPoint": 0, "outPoint": 4, "speed": 2},
                               {"mediaId": v["id"], "start": 2, "inPoint": 0, "outPoint": 1, "speed": 0.25}])
    res = _export(client, p, mode="draft")
    d = stream_durations(res["path"])
    assert abs(d["video"] - 6.0) < 0.1 and abs(d["audio"] - 6.0) < 0.1, d


def test_export_errors_are_clear(client):
    p = client.post("/api/projects", json={"name": "Empty"}).json()
    r = client.post(f"/api/projects/{p['id']}/export", json={"project": p})
    assert r.status_code == 400 and "main video track is empty" in r.json()["detail"]
    p["tracks"][0]["clips"] = [{"mediaId": "0123456789ab", "start": 0, "inPoint": 0, "outPoint": 1}]
    r = client.post(f"/api/projects/{p['id']}/export", json={"project": p})
    assert r.status_code == 400 and "no longer exists" in r.json()["detail"]


def test_cancel_export(client, fixtures):
    v = upload(client, fixtures["landscape"])
    clips = [{"mediaId": v["id"], "start": i * 4, "inPoint": 0, "outPoint": 4} for i in range(10)]
    p = _project_with(client, clips)
    r = client.post(f"/api/projects/{p['id']}/export", json={"project": p, "mode": "final"})
    jid = r.json()["jobId"]
    time.sleep(1.0)
    client.post(f"/api/jobs/{jid}/cancel")
    j = wait_job(client, jid)
    assert j["status"] in ("cancelled", "done")
