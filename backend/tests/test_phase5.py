"""Phase 5: transforms + keyframes (geometry, smoothness), opacity/intensity keyframes, speed/ramps/
reverse/freeze (frame-exact), audio gain (volume keyframes, fades, ducking), selective colour."""
import subprocess

import numpy as np
import pytest

from app import motion as M
from app.ffmpeg_utils import stream_durations
from test_phase1 import _export, upload

W, H = 540, 960  # small canvas keeps these fast; geometry is resolution independent


def ff(*a):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *a], check=True)


def frames(path, w=W, h=H):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, h, w, 3).astype(float)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("p5")
    marker = d / "marker.png"  # grey 800x600 with a red 80x80 square centred at (500, 200)
    ff("-f", "lavfi", "-i", "color=c=0x808080:s=800x600", "-vf", "drawbox=x=460:y=160:w=80:h=80:color=red:t=fill",
       "-frames:v", "1", str(marker))
    counter = d / "counter.mp4"  # luma = frame number (lossless), with a 440 Hz tone
    ff("-f", "lavfi", "-i", "color=c=black:s=64x64:r=30:d=8", "-f", "lavfi", "-i", "sine=f=440:d=8",
       "-vf", "geq=lum='N':cb=128:cr=128", "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-shortest", str(counter))
    speech = d / "speech.wav"  # silence, a 1 s burst at 2..3 s, silence
    ff("-f", "lavfi", "-i", "aevalsrc='if(between(t,2,3),0.5*sin(2*PI*300*t),0)':s=48000:d=5", str(speech))
    music = d / "music.wav"
    ff("-f", "lavfi", "-i", "sine=f=220:d=6:sample_rate=48000", str(music))
    return {"marker": marker, "counter": counter, "speech": speech, "music": music}


def proj(client, main, extra=None, aspect="9:16"):
    p = client.post("/api/projects", json={"name": "Motion", "aspect": aspect}).json()
    p["tracks"][0]["clips"] = main
    for ttype, cl in (extra or {}).items():
        next(t for t in p["tracks"] if t["type"] == ttype)["clips"] = cl
    p["media"] = list({c["mediaId"] for t in p["tracks"] for c in t["clips"] if c.get("mediaId")})
    p["export"].update({"width": W, "height": H, "fps": 30})
    p["settings"].update({"width": W, "height": H})  # positions are canvas (project) pixels
    return p


def luma_frames(path, w=W, h=H):
    """Raw Y plane (no range conversion): for the frame-counter video, Y == source frame number."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"], capture_output=True, check=True)
    b = np.frombuffer(r.stdout, np.uint8).reshape(-1, w * h * 3 // 2)
    return b[:, : w * h].reshape(-1, h, w).astype(float)


def dark_centroid(y, near):
    """Sub-pixel centre of the marker from luma (full resolution): red (Y~63) is darker than the grey
    (Y~126); only a window around the expected position counts, and black bars (Y~16) are excluded."""
    cx, cy = int(near[0]), int(near[1])
    win = y[cy - 70:cy + 70, cx - 70:cx + 70]
    wgt = np.clip(126 - win, 0, 63) * (win > 40)
    ys, xs = np.mgrid[cy - 70:cy + 70, cx - 70:cx + 70]
    return (wgt * (xs + 0.5)).sum() / wgt.sum(), (wgt * (ys + 0.5)).sum() / wgt.sum()


def red_box(f):
    m = (f[..., 0] > 160) & (f[..., 1] < 90) & (f[..., 2] < 90)
    ys, xs = np.where(m)
    if not len(xs):
        return None
    return xs.mean() + 0.5, ys.mean() + 0.5, xs.max() - xs.min() + 1


def predicted_box(clip, tau):
    t = M.transform_at(clip, tau)
    t["scale"] = t["scale"]
    m, _ = M.layer_matrix(800, 600, (clip.get("transform") or {}).get("crop", {}), False, W, H, t)
    cx, cy = M.apply(m, 500, 200)
    return cx, cy, 80 * abs(m[0] if abs(t["rotation"]) < 1e-9 else np.hypot(m[0], m[1]))


def test_keyframed_motion_matches_the_matrix_and_is_smooth(client, media):
    Mk = upload(client, media["marker"])
    clip = {"mediaId": Mk["id"], "start": 0, "inPoint": 0, "outPoint": 2, "fit": "fit",
            "transform": {"x": 0, "y": 0, "scale": 1, "rotation": 0, "opacity": 1},
            "keyframes": {"scale": [{"time": 0, "value": 1.0, "easing": "inout"}, {"time": 2, "value": 1.6, "easing": "linear"}],
                          "x": [{"time": 0, "value": 0, "easing": "linear"}, {"time": 2, "value": -60, "easing": "linear"}]}}
    res = _export(client, proj(client, [clip]), mode="final")
    fr = frames(res["path"])
    centres = []
    for k in range(0, 60, 4):
        got = red_box(fr[k])
        want = predicted_box(clip, k / 30)
        assert got is not None
        assert abs(got[0] - want[0]) < 1.5 and abs(got[1] - want[1]) < 1.5, (k, got, want)
        assert abs(got[2] - want[2]) < 2.5, (k, got, want)
    lum = luma_frames(res["path"])
    resid = np.array([dark_centroid(lum[k], predicted_box(clip, k / 30))[0] - predicted_box(clip, k / 30)[0] for k in range(60)])
    # The eased move itself accelerates; jitter = frame-to-frame wobble of the error against the path.
    assert np.abs(resid).max() < 0.6, f"off the predicted path by up to {np.abs(resid).max():.2f} px"
    assert np.abs(np.diff(resid)).max() < 0.25, f"jitter {np.abs(np.diff(resid)).max():.3f} px between frames"


def test_static_transform_rotation_flip_crop(client, media):
    Mk = upload(client, media["marker"])
    clip = {"mediaId": Mk["id"], "start": 0, "inPoint": 0, "outPoint": 1, "fit": "fit",
            "transform": {"x": 40, "y": -30, "scale": 0.8, "rotation": 90, "flipH": True, "opacity": 1,
                          "crop": {"left": 0.1, "top": 0, "right": 0, "bottom": 0.1}}}
    res = _export(client, proj(client, [clip]), mode="final")
    got = red_box(frames(res["path"])[10])
    t = M.transform_at(clip, 0.3)
    m, _ = M.layer_matrix(800, 600, clip["transform"]["crop"], False, W, H, t)
    want = M.apply(m, 500, 200)
    assert abs(got[0] - want[0]) < 2 and abs(got[1] - want[1]) < 2, (got, want)


def test_opacity_and_intensity_keyframes(client, media):
    Mk = upload(client, media["marker"])
    clip = {"mediaId": Mk["id"], "start": 0, "inPoint": 0, "outPoint": 2, "fit": "fit",
            "transform": {"x": 0, "y": 0, "scale": 1, "opacity": 1},
            "keyframes": {"opacity": [{"time": 0, "value": 0, "easing": "linear"}, {"time": 2, "value": 1, "easing": "linear"}]}}
    res = _export(client, proj(client, [clip]), mode="final")
    fr = frames(res["path"])
    for k in (15, 30, 45):
        grey = fr[k][H // 2, 20][0]  # grey 128 over black background, fading in
        assert abs(grey - 128 * k / 60) < 6, (k, grey)
    graded = {"mediaId": Mk["id"], "start": 0, "inPoint": 0, "outPoint": 2, "fit": "fit",
              "filters": [{"type": "preset", "name": "bw", "intensity": 1, "params": {}}],
              "keyframes": {"intensity": [{"time": 0, "value": 0, "easing": "linear"}, {"time": 2, "value": 1, "easing": "linear"}]}}
    fr = frames(_export(client, proj(client, [graded]), mode="final")["path"])
    red = lambda f: int(((f[..., 0] - f[..., 1]) > 30).sum())  # noqa: E731
    assert red(fr[5]) > 500 and red(fr[55]) < 20, ("colour at the start, black and white at the end", red(fr[5]), red(fr[55]))


@pytest.mark.parametrize("variant", ["plain", "speed2", "ramp", "reverse", "freeze"])
def test_speed_ramp_reverse_freeze_frame_exact(client, media, variant):
    C = upload(client, media["counter"])
    clip = {"mediaId": C["id"], "start": 0, "inPoint": 1.0, "outPoint": 5.0, "fit": "fill", "speed": 1}
    if variant == "speed2":
        clip["speed"] = 2
    elif variant == "ramp":
        clip["speedRamp"] = {"points": [{"u": 0, "speed": 1}, {"u": 0.55, "speed": 1}, {"u": 1, "speed": 0.3}]}
    elif variant == "reverse":
        clip["reverse"] = True
    else:
        clip["freezeFrame"] = 2.5
    D = M.clip_duration(clip)
    res = _export(client, proj(client, [clip]), mode="final")
    fr = luma_frames(res["path"])
    assert abs(len(fr) / 30 - D) < 1.5 / 30, (len(fr), D)
    diffs = []
    for k in range(0, len(fr), 7):
        shown = fr[k][H // 2, W // 2]  # luma == source frame number (lossy encode: +-1)
        want = np.floor(M.src_time(clip, k / 30) * 30 + 1e-6)  # the source frame on screen at that time (floor, like <video>)
        diffs.append(shown - want)
    # The encode reads the counter one luma level low (the plain clip shows the same, and a
    # testsrc2 comparison confirms the frames themselves line up), so exact means shown == want - 1.
    assert all(abs(d + 1) < 0.6 for d in diffs), (variant, diffs)


def test_audio_volume_keyframes_fades_and_ducking(client, media):
    C = upload(client, media["counter"])
    S = upload(client, media["speech"])
    Mu = upload(client, media["music"])
    main = [{"mediaId": C["id"], "start": 0, "inPoint": 0, "outPoint": 5, "muted": True, "fit": "fill"}]
    speech = [{"mediaId": S["id"], "start": 0, "inPoint": 0, "outPoint": 5}]
    music = [{"mediaId": Mu["id"], "start": 0, "inPoint": 0, "outPoint": 5, "fadeIn": 0.5,
              "keyframes": {"volume": [{"time": 0, "value": 1, "easing": "linear"}, {"time": 4, "value": 1, "easing": "linear"},
                                       {"time": 5, "value": 0.25, "easing": "linear"}]}}]
    p = proj(client, main, {"audio": speech, "music": music})
    next(t for t in p["tracks"] if t["type"] == "music")["autoDuck"] = {"enabled": True, "amount": -12}
    res = _export(client, p, mode="draft")

    def rms(t0, t1, f=None):
        af = ["-af", f"bandpass=f={f}:w=40"] if f else []
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t0), "-t", str(t1 - t0), "-i", res["path"], *af,
                            "-ac", "1", "-f", "f32le", "-"], capture_output=True, check=True)
        return float(np.sqrt(np.mean(np.frombuffer(r.stdout, np.float32) ** 2)))

    full = rms(1.0, 1.6, 220)
    assert rms(0.05, 0.2, 220) < 0.6 * full, "music fades in"
    ducked = rms(2.3, 2.8, 220)
    assert abs(20 * np.log10(ducked / full) - (-12)) < 2.5, f"music ducks ~12 dB under speech ({20 * np.log10(ducked / full):.1f} dB)"
    assert rms(3.6, 3.9, 220) > 0.8 * full, "music recovers after the speech"
    assert rms(4.75, 4.95, 220) < 0.45 * full, "volume keyframe pulls the music down at the end"
    assert abs(stream_durations(res["path"])["audio"] - 5) < 0.05


def test_selective_colour_export(client, media):
    Mk = upload(client, media["marker"])
    clip = {"mediaId": Mk["id"], "start": 0, "inPoint": 0, "outPoint": 1, "fit": "fit",
            "filters": [{"type": "selective", "intensity": 1, "params": {"hue": 0, "range": 20, "softness": 10}}]}
    f = frames(_export(client, proj(client, [clip]), mode="final")["path"])[10]
    box = red_box(f)
    assert box is not None, "red kept"
    assert np.abs(f[H // 2 + 100, 20] - f[H // 2 + 100, 20].mean()).max() < 3, "grey stays grey"


def test_adjustment_layer_strength_keyframes(client, media):
    Mk = upload(client, media["marker"])
    main = [{"mediaId": Mk["id"], "start": 0, "inPoint": 0, "outPoint": 2, "fit": "fit"}]
    adj = [{"mediaId": None, "adjustment": True, "start": 0, "inPoint": 0, "outPoint": 2,
            "filters": [{"type": "preset", "name": "bw", "intensity": 1, "params": {}}],
            "keyframes": {"intensity": [{"time": 0, "value": 0, "easing": "linear"}, {"time": 2, "value": 1, "easing": "linear"}]}}]
    fr = frames(_export(client, proj(client, main, {"overlay": adj}), mode="final")["path"])
    red = lambda f: int(((f[..., 0] - f[..., 1]) > 30).sum())  # noqa: E731
    assert red(fr[3]) > 500 and red(fr[57]) < 20, (red(fr[3]), red(fr[57]))


def test_sfx_library_and_import(client):
    lib = client.get("/api/media/sfx/library").json()["sfx"]
    ids = {s["id"] for s in lib}
    assert {"whoosh", "hit"} <= ids
    m = client.post("/api/media/sfx/whoosh/import").json()["media"]
    assert m["type"] == "audio" and m["sfx"] == "whoosh" and abs(m["sfxPeak"] - 0.45) < 1e-6 and m["hasAudio"]
