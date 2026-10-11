"""Phase 2b: overlay compositing in export, overlay audio, beat detection."""
import subprocess

import numpy as np
import pytest

from app.ffmpeg_utils import stream_durations
from test_phase1 import _export, _project_with, upload, wait_job

FW, FH = 108, 192  # frames are sampled at 1/10 of 1080x1920


def frame_at(path, t):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", path,
                        "-frames:v", "1", "-vf", f"scale={FW}:{FH}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(FH, FW, 3).astype(int)


def is_color(px, rgb, tol=40):
    return all(abs(int(a) - b) <= tol for a, b in zip(px, rgb))


@pytest.fixture(scope="module")
def colors(fixtures, tmp_path_factory):
    d = tmp_path_factory.mktemp("colors")
    ff = lambda *a: subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *a], check=True)  # noqa: E731
    blue = d / "blue.mp4"
    ff("-f", "lavfi", "-i", "color=c=0x0000ff:s=1080x1920:r=30:d=5", "-f", "lavfi", "-i", "sine=f=300:d=5",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(blue))
    red = d / "red.png"
    ff("-f", "lavfi", "-i", "color=c=0xff0000:s=400x400", "-frames:v", "1", str(red))
    green = d / "green.mp4"  # 16:9 video with a loud tone, used as overlay with audio
    ff("-f", "lavfi", "-i", "color=c=0x00ff00:s=1280x720:r=30:d=3", "-f", "lavfi", "-i", "sine=f=1000:d=3",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(green))
    return {"blue": blue, "red": red, "green": green}


def test_overlay_composited_with_transform_and_timing(client, colors):
    blue = upload(client, colors["blue"])
    red = upload(client, colors["red"])
    main = [{"mediaId": blue["id"], "start": 0, "inPoint": 0, "outPoint": 5}]
    # Red square: fit (=> 1080x1080 on the 1080x1920 canvas), scale 0.5 => 540x540, moved 200 px up.
    ov = [{"mediaId": red["id"], "start": 2, "inPoint": 0, "outPoint": 1,
           "transform": {"x": 0, "y": -200, "scale": 0.5, "opacity": 1}}]
    p = _project_with(client, main, {"overlay": ov})
    res = _export(client, p, mode="draft")

    f1 = frame_at(res["path"], 1.0)
    assert is_color(f1[FH // 2, FW // 2], (0, 0, 255)), "before the overlay starts: main only"
    f2 = frame_at(res["path"], 2.5)
    cy = FH // 2 - 20  # centre moved 200px up (20 px at 1/10 scale)
    assert is_color(f2[cy, FW // 2], (255, 0, 0)), "overlay visible during its clip"
    assert is_color(f2[cy, 5], (0, 0, 255)), "overlay is 540 px wide, so the edges stay blue"
    assert is_color(f2[cy - 24, FW // 2], (255, 0, 0)) and is_color(f2[cy - 30, FW // 2], (0, 0, 255)), "540 px tall"
    f3 = frame_at(res["path"], 3.5)
    assert is_color(f3[FH // 2, FW // 2], (0, 0, 255)), "after the overlay ends"


def test_overlay_opacity_hidden_track_and_order(client, colors):
    blue = upload(client, colors["blue"])
    red = upload(client, colors["red"])
    green = upload(client, colors["green"])
    p = client.post("/api/projects", json={"name": "Overlay order"}).json()
    p["tracks"][0]["clips"] = [{"mediaId": blue["id"], "start": 0, "inPoint": 0, "outPoint": 3}]
    ov1 = next(t for t in p["tracks"] if t["type"] == "overlay")
    ov1["clips"] = [{"mediaId": green["id"], "start": 0, "inPoint": 0, "outPoint": 3, "fit": "fill"}]
    # A second overlay track (drawn on top) with a half-transparent red square.
    ov2 = {**ov1, "id": "ov2ov2ov2ov2", "name": "Overlay 2",
           "clips": [{"mediaId": red["id"], "start": 0, "inPoint": 0, "outPoint": 3,
                      "transform": {"scale": 0.5, "opacity": 0.5}}]}
    p["tracks"].insert(2, ov2)
    p["media"] = [blue["id"], red["id"], green["id"]]
    res = _export(client, p, mode="draft")
    f = frame_at(res["path"], 1.0)
    assert is_color(f[5, 5], (0, 255, 0)), "fill overlay covers the whole frame (corner is green)"
    mid = f[FH // 2, FW // 2]
    assert is_color(mid, (128, 128, 0), tol=45), f"50% red over green, got {mid}"

    ov2["hidden"] = True
    ov1["hidden"] = True
    res2 = _export(client, p, mode="draft")
    assert is_color(frame_at(res2["path"], 1.0)[FH // 2, FW // 2], (0, 0, 255)), "hidden overlay tracks are skipped"


def test_overlay_audio_is_mixed(client, colors):
    blue = upload(client, colors["blue"])
    green = upload(client, colors["green"])
    main = [{"mediaId": blue["id"], "start": 0, "inPoint": 0, "outPoint": 5, "muted": True}]
    ov = [{"mediaId": green["id"], "start": 1, "inPoint": 0, "outPoint": 2}]
    p = _project_with(client, main, {"overlay": ov})
    res = _export(client, p, mode="draft")

    def rms(t0, t1):
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(t0), "-t", str(t1 - t0),
                            "-i", res["path"], "-ac", "1", "-f", "f32le", "-"], capture_output=True, check=True)
        return float(np.sqrt(np.mean(np.frombuffer(r.stdout, np.float32) ** 2)))

    assert rms(0.1, 0.9) < 0.01, "main audio muted -> silence before the overlay"
    assert rms(1.2, 2.8) > 0.05, "overlay clip audio audible (lavfi sine has amplitude 1/8 -> RMS ~0.088)"
    assert rms(3.2, 4.8) < 0.01
    d = stream_durations(res["path"])
    assert abs(d["video"] - 5) < 0.1 and abs(d["audio"] - 5) < 0.1


@pytest.mark.parametrize("bpm", [100, 128])
def test_beat_detection(client, tmp_path, bpm):
    period, start = 60 / bpm, 0.5
    path = tmp_path / f"click{bpm}.wav"
    expr = (f"if(lt(mod(t-{start}\\,{period})\\,0.03)*gte(t\\,{start})\\,sin(2*PI*1000*t)*exp(-60*mod(t-{start}\\,{period}))\\,0)"
            f"+0.05*sin(2*PI*220*t)")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc={expr}:s=44100:d=15", str(path)], check=True)
    m = upload(client, path)
    r = client.post(f"/api/media/{m['id']}/beats")
    assert r.status_code == 200, r.text
    job = wait_job(client, r.json()["jobId"])
    assert job["status"] == "done", job["error"]
    meta = client.get(f"/api/media/{m['id']}").json()
    assert abs(meta["bpm"] - bpm) < 1.5
    beats = np.array(meta["beats"])
    expected = start + period * np.round((beats - start) / period)
    assert np.abs(beats - expected).max() < 0.03, "every beat within 30 ms of a click"
    assert len(beats) >= int((15 - start) / period) - 2


def test_beats_rejects_silent_video(client, fixtures):
    v = upload(client, fixtures["silent"])
    r = client.post(f"/api/media/{v['id']}/beats")
    assert r.status_code == 400 and "no audio" in r.json()["detail"]
