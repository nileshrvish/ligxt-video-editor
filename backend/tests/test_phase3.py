"""Phase 3: transitions centred on the cut, using handles; shorten/freeze fallbacks; all types render."""
import subprocess

import numpy as np
import pytest

from app.ffmpeg_utils import stream_durations
from app.transitions import ALL_TYPES
from test_phase1 import _export, upload

FW, FH = 54, 96


def ff(*a):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *a], check=True)


def frame(path, t):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.4f}", "-i", path, "-frames:v", "1",
                        "-vf", f"scale={FW}:{FH}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(FH, FW, 3).astype(float)


def mean_rgb(path, t):
    return frame(path, t)[FH // 4: 3 * FH // 4, FW // 4: 3 * FW // 4].reshape(-1, 3).mean(axis=0)


def close(rgb, want, tol=30):
    return all(abs(a - b) <= tol for a, b in zip(rgb, want))


@pytest.fixture(scope="module")
def two_tone(tmp_path_factory):
    """A: 3 s green then 3 s yellow (with a tone). B: 2 s magenta then 4 s cyan (silent)."""
    d = tmp_path_factory.mktemp("p3")
    a, b = d / "a.mp4", d / "b.mp4"
    ff("-f", "lavfi", "-i", "color=c=0x00ff00:s=540x960:r=30:d=3", "-f", "lavfi", "-i", "color=c=0xffff00:s=540x960:r=30:d=3",
       "-f", "lavfi", "-i", "sine=f=400:d=6", "-filter_complex", "[0][1]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
       "-c:v", "libx264", "-preset", "ultrafast", "-g", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(a))
    ff("-f", "lavfi", "-i", "color=c=0xff00ff:s=540x960:r=30:d=2", "-f", "lavfi", "-i", "color=c=0x00ffff:s=540x960:r=30:d=4",
       "-filter_complex", "[0][1]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast",
       "-g", "10", "-pix_fmt", "yuv420p", str(b))
    red, blue = d / "red.png", d / "blue.png"
    ff("-f", "lavfi", "-i", "color=c=0xff0000:s=600x800", "-frames:v", "1", str(red))
    ff("-f", "lavfi", "-i", "color=c=0x0000ff:s=600x800", "-frames:v", "1", str(blue))
    return {"a": a, "b": b, "red": red, "blue": blue}


def project(client, clips, transitions, **export):
    p = client.post("/api/projects", json={"name": "Transitions"}).json()
    main = p["tracks"][0]
    for c in clips:
        c.setdefault("fit", "fill")
    main["clips"] = clips
    p["transitions"] = [{"trackId": main["id"], **t} for t in transitions]
    p["media"] = list({c["mediaId"] for c in clips})
    p["export"].update(export)
    return p


def test_handles_used_and_cut_positions_unchanged(client, two_tone):
    A, B = upload(client, two_tone["a"]), upload(client, two_tone["b"])
    clips = [{"id": "clipA", "mediaId": A["id"], "start": 0, "inPoint": 0, "outPoint": 3},       # green part only
             {"id": "clipB", "mediaId": B["id"], "start": 3, "inPoint": 2, "outPoint": 5}]       # cyan part only
    p = project(client, clips, [{"fromClipId": "clipA", "toClipId": "clipB", "type": "fade", "duration": 1}])
    res = _export(client, p, mode="draft")
    d = stream_durations(res["path"])
    assert abs(d["video"] - 6) < 0.05 and abs(d["audio"] - 6) < 0.05, f"timeline length unchanged (3 s + 3 s): {d}"
    assert close(mean_rgb(res["path"], 2.2), (0, 255, 0)), "before the transition: A (green)"
    assert close(mean_rgb(res["path"], 4.0), (0, 255, 255)), "after the transition: B (cyan)"
    # The transition spans 2.5..3.5 s (p = elapsed fraction, A weight = 1 - p).
    # 2.8 s, p=0.3: A is still in range (green); B shows its handle from before its in point (magenta).
    before = mean_rgb(res["path"], 2.8)
    assert close(before, (77, 179, 77), tol=30), f"B's handle (magenta) mixed in, got {before}"
    # 3.2 s, p=0.7: A shows its handle beyond its out point (yellow); B is in range (cyan).
    after = mean_rgb(res["path"], 3.2)
    assert close(after, (77, 255, 179), tol=30), f"A's handle (yellow) mixed in, got {after}"


def test_freeze_when_no_spare_footage(client, two_tone):
    A, B = upload(client, two_tone["a"]), upload(client, two_tone["b"])
    # A ends exactly at the source end (yellow); B starts at source 0 (magenta): no handles anywhere.
    clips = [{"id": "fa", "mediaId": A["id"], "start": 0, "inPoint": 3, "outPoint": 6},
             {"id": "fb", "mediaId": B["id"], "start": 3, "inPoint": 0, "outPoint": 1.5}]
    p = project(client, clips, [{"fromClipId": "fa", "toClipId": "fb", "type": "fade", "duration": 0.8}])
    res = _export(client, p, mode="draft")
    assert abs(stream_durations(res["path"])["video"] - 4.5) < 0.05
    mid = mean_rgb(res["path"], 3.0)
    assert close(mid, (255, 255 * 0.5 + 0, 128), tol=40), f"held yellow last frame mixed with held magenta first frame, got {mid}"


def test_photos_have_unlimited_handles_and_range_matches_full(client, two_tone):
    R, Bl = upload(client, two_tone["red"]), upload(client, two_tone["blue"])
    clips = [{"id": "r", "mediaId": R["id"], "start": 0, "inPoint": 0, "outPoint": 2},
             {"id": "b", "mediaId": Bl["id"], "start": 2, "inPoint": 0, "outPoint": 2}]
    p = project(client, clips, [{"fromClipId": "r", "toClipId": "b", "type": "wipeleft", "duration": 1.2}])
    full = _export(client, p, mode="draft")
    part = _export(client, p, mode="draft", rangeStart=1.7, rangeEnd=2.6)
    assert abs(stream_durations(part["path"])["video"] - 0.9) < 0.05
    for t in (1.8, 2.0, 2.3):
        diff = np.abs(frame(full["path"], t) - frame(part["path"], t - 1.7)).mean()
        assert diff < 6, f"range export differs from full export at {t}s by {diff:.1f}"


def test_whip_pan_moves_and_blurs(client, two_tone):
    R, Bl = upload(client, two_tone["red"]), upload(client, two_tone["blue"])
    clips = [{"id": "r", "mediaId": R["id"], "start": 0, "inPoint": 0, "outPoint": 2},
             {"id": "b", "mediaId": Bl["id"], "start": 2, "inPoint": 0, "outPoint": 2}]
    p = project(client, clips, [{"fromClipId": "r", "toClipId": "b", "type": "whipleft", "duration": 0.6}])
    res = _export(client, p, mode="draft")
    f = frame(res["path"], 2.0)  # middle of the whip: e(0.5) = 0.5 -> boundary at x = W/2, blurred
    row = f[FH // 2]
    assert row[2][0] > 180 and row[2][2] < 80, "left edge still red (A)"
    assert row[-3][2] > 180 and row[-3][0] < 80, "right edge already blue (B)"
    blended = [(px[0] > 40 and px[2] > 40) for px in row[FW // 2 - 6: FW // 2 + 6]]
    assert sum(blended) >= 3, "motion blur blends red and blue around the moving edge"
    assert abs(stream_durations(res["path"])["video"] - 4) < 0.05


def test_audio_crossfades(client, two_tone):
    A, B = upload(client, two_tone["a"]), upload(client, two_tone["b"])
    clips = [{"id": "x", "mediaId": A["id"], "start": 0, "inPoint": 0, "outPoint": 3},
             {"id": "y", "mediaId": B["id"], "start": 3, "inPoint": 2, "outPoint": 5}]
    p = project(client, clips, [{"fromClipId": "x", "toClipId": "y", "type": "fade", "duration": 1}])
    res = _export(client, p, mode="draft")

    def rms(t0, t1):
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(t0), "-t", str(t1 - t0), "-i",
                            res["path"], "-ac", "1", "-f", "f32le", "-"], capture_output=True, check=True)
        return float(np.sqrt(np.mean(np.frombuffer(r.stdout, np.float32) ** 2)))

    full, mid, after = rms(1.0, 2.4), rms(2.95, 3.05), rms(3.7, 4.8)
    assert full > 0.05 and after < 0.005
    assert 0.3 * full < mid < 0.7 * full, f"A's tone fades out across the cut ({mid:.3f} vs {full:.3f})"


def test_every_transition_type_renders(client, two_tone):
    R, Bl = upload(client, two_tone["red"]), upload(client, two_tone["blue"])
    clips, trs = [], []
    types = sorted(ALL_TYPES)
    for i in range(len(types) + 1):
        clips.append({"id": f"c{i}", "mediaId": (R if i % 2 == 0 else Bl)["id"], "start": i * 0.8, "inPoint": 0, "outPoint": 0.8})
    for i, t in enumerate(types):
        trs.append({"fromClipId": f"c{i}", "toClipId": f"c{i + 1}", "type": t, "duration": 0.4})
    p = project(client, clips, trs, width=360, height=640)
    res = _export(client, p, mode="draft")
    d = stream_durations(res["path"])
    assert abs(d["video"] - 0.8 * len(clips)) < 0.05 and res["sync"]["ok"]
