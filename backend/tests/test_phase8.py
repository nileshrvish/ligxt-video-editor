"""Phase 8: project locks (two tabs), relink of a missing file, disk-space refusal, recent exports and
re-export, unused-media cleanup, song energy + drop, accent suggestion, video slots muted in templates."""
import subprocess
import time

import pytest

from app import system_api as S
from app import templates as TP
from app import config
from test_phase1 import upload, wait_job


def ff(*a):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *a], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory, client, fixtures):
    d = tmp_path_factory.mktemp("p8")
    out = {"video": upload(client, fixtures["landscape"]), "photo": upload(client, fixtures["photo"])}
    for i, col in enumerate(["0x202020", "0x303030", "0x252525"]):
        f = d / f"grey{i}.png"  # grey photos with one saturated red patch: the accent should be red
        ff("-f", "lavfi", "-i", f"color=c={col}:s=800x1000", "-f", "lavfi", "-i", "color=c=0xd01818:s=160x160",
           "-filter_complex", "[0][1]overlay=320:420", "-frames:v", "1", str(f))
        out[f"grey{i}"] = upload(client, f)
    song = d / "drop_song.wav"
    TP.synth_music(120, 40, song, drop_at=16.0)
    m = upload(client, song)
    assert wait_job(client, client.post(f"/api/media/{m['id']}/beats").json()["jobId"], timeout=120)["status"] == "done"
    out["song"] = client.get(f"/api/media/{m['id']}").json()
    return out


def _project(client, media, name="P8"):
    p = client.post("/api/projects", json={"name": name, "aspect": "9:16"}).json()
    p["tracks"][0]["clips"] = [{"id": "a", "mediaId": media["video"]["id"], "start": 0, "inPoint": 0, "outPoint": 1.0}]
    r = client.put(f"/api/projects/{p['id']}", json=p)
    assert r.status_code == 200, r.text
    return client.get(f"/api/projects/{p['id']}").json()


def test_lock_two_tabs(client, media):
    p = _project(client, media, "Locked")
    pid = p["id"]
    assert client.post(f"/api/projects/{pid}/lock", json={"client": "tabA", "label": "tab A"}).status_code == 200
    r = client.post(f"/api/projects/{pid}/lock", json={"client": "tabB"})
    assert r.status_code == 409 and "tab A" in r.json()["detail"]
    # saves from the other tab are refused, from the owner accepted
    assert client.put(f"/api/projects/{pid}", json=p, headers={"X-Ligxt-Client": "tabB"}).status_code == 409
    assert client.post(f"/api/projects/{pid}/autosave", json=p, headers={"X-Ligxt-Client": "tabB"}).status_code == 409
    assert client.put(f"/api/projects/{pid}", json=p, headers={"X-Ligxt-Client": "tabA"}).status_code == 200
    # take over
    assert client.post(f"/api/projects/{pid}/lock", json={"client": "tabB", "force": True}).status_code == 200
    assert client.put(f"/api/projects/{pid}", json=p, headers={"X-Ligxt-Client": "tabA"}).status_code == 409
    # releasing by a non-owner does nothing; by the owner frees it
    client.post(f"/api/projects/{pid}/unlock", json={"client": "tabA"})
    assert client.post(f"/api/projects/{pid}/lock", json={"client": "tabA"}).status_code == 409
    client.post(f"/api/projects/{pid}/unlock", json={"client": "tabB"})
    assert client.post(f"/api/projects/{pid}/lock", json={"client": "tabA"}).status_code == 200
    # a stale lock (no heartbeat) expires
    S._locks[pid]["seen"] = time.time() - S.LOCK_TTL - 1
    assert client.post(f"/api/projects/{pid}/lock", json={"client": "tabC"}).status_code == 200
    client.post(f"/api/projects/{pid}/unlock", json={"client": "tabC"})


def test_relink_missing_file(client, media, fixtures):
    m = upload(client, fixtures["silent"])
    p = client.post("/api/projects", json={"name": "Relink", "aspect": "9:16"}).json()
    p["tracks"][0]["clips"] = [{"id": "r", "mediaId": m["id"], "start": 0, "inPoint": 0, "outPoint": 1.0}]
    client.put(f"/api/projects/{p['id']}", json=p)
    (config.DATA_ROOT / m["file"]).unlink()
    assert client.get(f"/api/media/{m['id']}").json().get("fileMissing") is True
    r = client.post(f"/api/projects/{p['id']}/export", json={"project": p, "mode": "draft"})
    assert r.status_code == 400 and "missing" in r.text.lower() and "Relink" in r.text
    # wrong type is refused, keeping the item as it was
    r = client.post(f"/api/media/{m['id']}/relink?filename=photo.png", content=fixtures["photo"].read_bytes())
    assert r.status_code == 400 and "video" in r.json()["detail"]
    r = client.post(f"/api/media/{m['id']}/relink?filename=found.mp4", content=fixtures["silent"].read_bytes())
    assert r.status_code == 200, r.text
    assert wait_job(client, r.json()["jobId"])["status"] == "done"
    after = client.get(f"/api/media/{m['id']}").json()
    assert after["id"] == m["id"] and not after.get("fileMissing") and after["name"] == "found.mp4"
    r = client.post(f"/api/projects/{p['id']}/export", json={"project": p, "mode": "draft"})
    assert r.status_code == 200, r.text
    assert wait_job(client, r.json()["jobId"])["status"] == "done"


def test_disk_space_refused(client, media, monkeypatch):
    p = _project(client, media, "Full disk")
    monkeypatch.setattr(S, "free_bytes", lambda path: 50 * 1024 * 1024)
    r = client.post(f"/api/projects/{p['id']}/export", json={"project": p, "mode": "draft"})
    assert r.status_code == 507
    assert "Not enough disk space" in r.json()["detail"] and "free" in r.json()["detail"]


def test_recent_exports_and_reexport(client, media):
    p = _project(client, media, "Exported")
    r = client.post(f"/api/projects/{p['id']}/export", json={"project": p, "mode": "draft"})
    j = wait_job(client, r.json()["jobId"])
    assert j["status"] == "done", j
    ex = client.get("/api/exports").json()["exports"]
    mine = next(e for e in ex if e["projectId"] == p["id"])
    assert mine["canReexport"] and mine["size"] > 0 and mine["mode"] == "draft"
    r = client.post(f"/api/exports/{mine['file']}/reexport")
    assert r.status_code == 200, r.text
    assert wait_job(client, r.json()["jobId"])["status"] == "done"
    assert client.post("/api/exports/nope.mp4/reexport").status_code == 404


def test_media_cleanup(client, media, fixtures):
    unused = upload(client, fixtures["music"])
    lst = client.get("/api/media-cleanup").json()
    ids = {x["id"] for x in lst["media"]}
    assert unused["id"] in ids and media["video"]["id"] not in ids
    assert lst["total"] >= next(x["size"] for x in lst["media"] if x["id"] == unused["id"]) > 0
    r = client.post("/api/media-cleanup", json={"ids": [unused["id"], media["video"]["id"]]}).json()
    assert r["removed"] == 1 and r["freed"] > 0  # the used video is never deleted
    assert client.get(f"/api/media/{unused['id']}").status_code == 404
    assert client.get(f"/api/media/{media['video']['id']}").status_code == 200


def test_energy_and_drop(client, media):
    e = client.get(f"/api/media/{media['song']['id']}/energy").json()
    assert e["rate"] == 10 and len(e["curve"]) > 350
    assert e["drop"] is not None and abs(e["drop"] - 16.0) <= 0.6, e["drop"]
    assert client.get(f"/api/media/{media['photo']['id']}/energy").status_code == 400


def test_accent_suggestion_and_generate(client, media):
    # a template with a selective-colour layer: save one from a project with a selective filter
    p = _project(client, media, "Selective src")
    p["tracks"][0]["clips"] = [{"id": f"s{i}", "mediaId": media[f"grey{i}"]["id"], "start": i * 1.0, "inPoint": 0, "outPoint": 1.0,
                                "filters": [{"type": "selective", "name": "selective", "intensity": 1,
                                             "params": {"hue": 30, "range": 25, "desat": 1}}]} for i in range(3)]
    mu = next(t for t in p["tracks"] if t["type"] == "music")
    mu["clips"] = [{"mediaId": media["song"]["id"], "start": 0, "inPoint": 0, "outPoint": 3.0}]
    r = client.post("/api/templates", json={"project": p, "name": "Selective tpl"})
    assert r.status_code == 200, r.text
    tid = r.json()["template"]["id"]
    ids = [media[f"grey{i}"]["id"] for i in range(3)]
    s = client.post(f"/api/templates/{tid}/accent", json={"media": ids}).json()
    assert s["selective"] is True
    assert s["hue"] is not None and (s["hue"] < 20 or s["hue"] > 340), s  # red
    assert s.get("preview", "").startswith("/files/cache/_accent/")
    r = client.post(f"/api/templates/{tid}/generate", json={"media": ids, "music": media["song"]["id"], "accent": "auto"}).json()
    j = wait_job(client, r["jobId"])
    assert j["status"] == "done", j
    g = client.get(f"/api/projects/{j['result']['projectId']}").json()
    hues = [f["params"]["hue"] for tr in g["tracks"] for c in tr["clips"] for f in c.get("filters") or [] if f["type"] == "selective"]
    assert hues and all(abs(h - s["hue"]) < 1 for h in hues)
    client.delete(f"/api/templates/{tid}")


def test_template_video_slots_muted(client, media):
    ids = [media["video"]["id"], media["photo"]["id"], media["video"]["id"], media["photo"]["id"]]
    r = client.post("/api/templates/quick-beat-montage/generate", json={"media": ids, "music": media["song"]["id"]}).json()
    j = wait_job(client, r["jobId"])
    assert j["status"] == "done", j
    assert any("without their own sound" in line for line in j["result"]["report"])
    g = client.get(f"/api/projects/{j['result']['projectId']}").json()
    vids = [c for t in g["tracks"] if t["type"] == "video" for c in t["clips"] if c.get("mediaId") == media["video"]["id"]]
    assert vids and all(c.get("muted") or c.get("volume", 1) == 0 for c in vids)
