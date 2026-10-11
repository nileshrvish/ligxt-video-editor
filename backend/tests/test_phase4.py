"""Phase 4: grading in export matches the colour maths; effects; adjustment layers; LUT + preset APIs."""
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.grade import grade_color, parse_cube
from test_phase1 import _export, upload

W, H = 216, 384  # small exports keep these tests fast


def ff(*a):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *a], check=True)


def frame(path, t, w=W, h=H):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                        "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(h, w, 3).astype(float)


@pytest.fixture(scope="module")
def swatches(tmp_path_factory):
    """A 4x2 grid of flat colours (incl. a skin tone), 1080x1920, plus a black image with one white column."""
    d = tmp_path_factory.mktemp("p4")
    cols = ["0xE69E7A", "0x3366CC", "0x22AA44", "0x808080", "0xF2F2F2", "0x1A1A1A", "0xC0603A", "0x99CCFF"]
    parts = [f"color=c={c}:s=540x480" for c in cols]
    inputs = []
    for p in parts:
        inputs += ["-f", "lavfi", "-i", p]
    sw = d / "swatch.png"
    ff(*inputs, "-filter_complex", "".join(f"[{i}]" for i in range(8)) +
       "xstack=inputs=8:layout=0_0|540_0|0_480|540_480|0_960|540_960|0_1440|540_1440", "-frames:v", "1", str(sw))
    col = d / "column.png"
    ff("-f", "lavfi", "-i", "color=c=black:s=1080x1920", "-vf", "drawbox=x=530:y=0:w=20:h=1920:color=white:t=fill",
       "-frames:v", "1", str(col))
    return {"swatch": sw, "column": col}


def proj(client, clips, extra_tracks=None, **export):
    p = client.post("/api/projects", json={"name": "Grade"}).json()
    p["tracks"][0]["clips"] = clips
    for ttype, cl in (extra_tracks or {}).items():
        next(t for t in p["tracks"] if t["type"] == ttype)["clips"] = cl
    p["media"] = list({c["mediaId"] for t in p["tracks"] for c in t["clips"] if c.get("mediaId")})
    p["export"].update({"width": 1080, "height": 1920, **export})
    return p


def swatch_means(path, t):
    f = frame(path, t)
    out = []
    for row in range(4):
        for colx in range(2):
            y0, x0 = int((row + 0.25) * H / 4), int((colx + 0.25) * W / 2)
            out.append(f[y0: y0 + H // 8, x0: x0 + W // 4].reshape(-1, 3).mean(axis=0))
    return np.array(out)


def test_export_grade_matches_colour_maths(client, swatches):
    S = upload(client, swatches["swatch"])
    filters = [{"type": "adjust", "params": {"exposure": 0.3, "contrast": 20, "temperature": 25, "saturation": 15}},
               {"type": "preset", "name": "coolteal", "intensity": 0.8, "params": {"skinProtect": True}}]
    clean = _export(client, proj(client, [{"mediaId": S["id"], "start": 0, "inPoint": 0, "outPoint": 1}]), mode="draft")
    graded = _export(client, proj(client, [{"mediaId": S["id"], "start": 0, "inPoint": 0, "outPoint": 1, "filters": filters}]),
                     mode="draft")
    src = swatch_means(clean["path"], 0.5) / 255
    want = grade_color(src, filters, {}) * 255
    got = swatch_means(graded["path"], 0.5)
    err = np.abs(got - want).max()
    # Each yuv<->rgb round trip in ffmpeg rounds down by ~1/255; the graded path has two more than the
    # clean one and this grade has a gain of ~1.6x, so allow 9/255 (the 33^3 LUT itself is within 1.2/255).
    assert err < 9, f"export differs from the colour maths by {err:.1f}/255\n{np.round(got)}\n{np.round(want)}"


def test_skin_protection_keeps_skin_hue(client, swatches):
    S = upload(client, swatches["swatch"])
    base = {"type": "lut", "intensity": 1, "params": {"lut": "builtin/matte-fade"}}  # shifts skin hue by ~-4 deg
    on = _export(client, proj(client, [{"mediaId": S["id"], "start": 0, "inPoint": 0, "outPoint": 1,
                                        "filters": [{**base, "params": {**base["params"], "skinProtect": True}}]}]), mode="draft")
    off = _export(client, proj(client, [{"mediaId": S["id"], "start": 0, "inPoint": 0, "outPoint": 1,
                                         "filters": [{**base, "params": {**base["params"], "skinProtect": False}}]}]), mode="draft")
    clean = _export(client, proj(client, [{"mediaId": S["id"], "start": 0, "inPoint": 0, "outPoint": 1}]), mode="draft")
    hue = lambda c: np.degrees(np.arctan2(np.sqrt(3) * (c[1] - c[2]), 2 * c[0] - c[1] - c[2]))  # noqa: E731
    skin0, skin_on, skin_off = (swatch_means(x["path"], 0.5)[0] for x in (clean, on, off))
    assert abs(hue(skin_on) - hue(skin0)) < 0.5 * abs(hue(skin_off) - hue(skin0)), "protection keeps skin closer to its hue"
    sat = lambda c: (c.max() - c.min()) / c.max()  # noqa: E731
    assert abs(sat(skin_on) - sat(skin0)) < 0.5 * abs(sat(skin_off) - sat(skin0)), "and closer to its saturation (not grey)"
    bw = _export(client, proj(client, [{"mediaId": S["id"], "start": 0, "inPoint": 0, "outPoint": 1,
                                        "filters": [{"type": "preset", "name": "bw", "intensity": 1, "params": {"skinProtect": True}}]}]), mode="draft")
    assert sat(swatch_means(bw["path"], 0.5)[0]) < 0.05, "B&W stays black and white even with skin protection on"
    blue0, blue_on = swatch_means(clean["path"], 0.5)[1], swatch_means(on["path"], 0.5)[1]
    assert np.abs(blue_on - blue0).max() > 8, "non-skin colours are still graded"


def test_exports_in_the_same_second_get_unique_names(client, swatches):
    """Regression: two exports of the same project within one second used to share a file name."""
    from app.models import Project
    from app.render import _export_name
    p = Project(name="Same name")
    names = {_export_name(p, "draft").name for _ in range(3)}
    assert len(names) == 3


def test_effects_vignette_grain_chroma_glow(client, swatches):
    S = upload(client, swatches["swatch"])
    C = upload(client, swatches["column"])
    vig = _export(client, proj(client, [{"mediaId": S["id"], "start": 0, "inPoint": 0, "outPoint": 1, "filters": [
        {"type": "effect", "name": "vignette", "intensity": 1, "params": {"amount": 0.8, "feather": 0.5}}]}]), mode="draft")
    clean = _export(client, proj(client, [{"mediaId": S["id"], "start": 0, "inPoint": 0, "outPoint": 1}]), mode="draft")
    fv, fc = frame(vig["path"], 0.5), frame(clean["path"], 0.5)
    assert fv[2:8, 2:8].mean() < 0.5 * fc[2:8, 2:8].mean(), "corners darkened"
    assert abs(fv[H // 2 - 4:H // 2 + 4, W // 2 - 4:W // 2 + 4].mean() - fc[H // 2 - 4:H // 2 + 4, W // 2 - 4:W // 2 + 4].mean()) < 4, "centre kept"

    grain = _export(client, proj(client, [{"mediaId": S["id"], "start": 0, "inPoint": 0, "outPoint": 1, "filters": [
        {"type": "effect", "name": "grain", "intensity": 1, "params": {"amount": 0.6, "size": 2}}]}]), mode="final")
    g1, g2 = frame(grain["path"], 0.3, 1080, 1920), frame(grain["path"], 0.4, 1080, 1920)
    patch = (slice(600, 840), slice(650, 950))  # mid-grey swatch (no clipping; final quality keeps grain)
    assert g1[patch].std() > 8, "grain adds texture"
    assert np.abs(g1[patch] - g2[patch]).mean() > 4, "grain changes from frame to frame"
    assert abs(g1[patch].mean() - frame(clean["path"], 0.3, 1080, 1920)[patch].mean()) < 4, "grain is zero-mean"

    ca = _export(client, proj(client, [{"mediaId": C["id"], "start": 0, "inPoint": 0, "outPoint": 1, "filters": [
        {"type": "effect", "name": "chroma", "intensity": 1, "params": {"amount": 1}}]}]), mode="draft")
    row = frame(ca["path"], 0.5, 1080, 1920)[960]
    red_x = np.where(row[:, 0] > 128)[0]
    blue_x = np.where(row[:, 2] > 128)[0]
    assert red_x.mean() - blue_x.mean() == pytest.approx(12, abs=2), "red shifted right, blue left (6 px each)"

    glow = _export(client, proj(client, [{"mediaId": C["id"], "start": 0, "inPoint": 0, "outPoint": 1, "filters": [
        {"type": "effect", "name": "glow", "intensity": 1, "params": {"amount": 1, "threshold": 0.5, "radius": 20}}]}]), mode="draft")
    gr = frame(glow["path"], 0.5, 1080, 1920)[960]
    # A 20 px bar blurred with sigma 20: 40 px from its centre a Gaussian gives 255*(Phi(2.5)-Phi(1.5)) ~ 15.
    assert gr[520, 0] > 40 and 8 < gr[500, 0] < 30 and gr[440, 0] < 4, f"bloom falls off like a Gaussian: {gr[[440, 500, 520], 0]}"


def test_every_effect_and_preset_renders(client, swatches):
    S = upload(client, swatches["swatch"])
    from app.grade import EFFECT_KEYS, PRESETS
    clips, t = [], 0.0
    for name in PRESETS["presets"]:
        clips.append({"mediaId": S["id"], "start": t, "inPoint": 0, "outPoint": 0.3,
                      "filters": [{"type": "preset", "name": name, "intensity": 1, "params": {}}]})
        t += 0.3
    for name in EFFECT_KEYS:
        clips.append({"mediaId": S["id"], "start": t, "inPoint": 0, "outPoint": 0.3,
                      "filters": [{"type": "effect", "name": name, "intensity": 1, "params": {"amount": 0.7}},
                                  {"type": "adjust", "params": {"sharpen": 50, "blur": 10}}]})
        t += 0.3
    res = _export(client, proj(client, clips, width=360, height=640), mode="draft")
    assert res["sync"]["ok"]


def test_adjustment_layer_applies_only_during_its_clip(client, swatches):
    S = upload(client, swatches["swatch"])
    main = [{"mediaId": S["id"], "start": 0, "inPoint": 0, "outPoint": 3}]
    adj = [{"mediaId": None, "adjustment": True, "start": 1, "inPoint": 0, "outPoint": 1,
            "filters": [{"type": "preset", "name": "bw", "intensity": 1, "params": {}}]}]
    res = _export(client, proj(client, main, {"overlay": adj}), mode="draft")
    sat = lambda c: c.max() - c.min()  # noqa: E731
    before, during, after = (swatch_means(res["path"], t)[1] for t in (0.5, 1.5, 2.5))
    assert sat(before) > 60 and sat(after) > 60, "colour outside the adjustment layer"
    assert sat(during) < 8, "black and white while the adjustment layer is active"


def test_lut_import_and_user_presets(client, tmp_path):
    good = Path(__file__).resolve().parents[2] / "luts" / "matte-fade.cube"
    r = client.post("/api/luts/import?filename=My Look.cube", content=good.read_bytes())
    assert r.status_code == 200, r.text
    lut_id = r.json()["id"]
    assert any(x["id"] == lut_id and x["name"] == "My Look" for x in client.get("/api/luts").json()["luts"])
    assert any(x["id"] == "builtin/teal-orange" for x in client.get("/api/luts").json()["luts"])
    bad = client.post("/api/luts/import?filename=bad.cube", content=b"LUT_3D_SIZE 4\n0 0 0\n")
    assert bad.status_code == 422 and "valid 3D .cube" in bad.json()["detail"]
    assert parse_cube(client.get(f"/api/luts/file?id={lut_id}").text)["size"] == 33

    item = client.post("/api/presets", json={"name": "Reel grade", "filters": [{"type": "preset", "name": "warmfilm"}]}).json()
    assert any(p["id"] == item["id"] for p in client.get("/api/presets").json()["presets"])
    client.post("/api/presets", json={"name": "reel GRADE", "filters": [{"type": "preset", "name": "vivid"}]})
    assert sum(p["name"].lower() == "reel grade" for p in client.get("/api/presets").json()["presets"]) == 1
    assert client.get("/api/fx/grain.png?w=540&h=960&s=2").status_code == 200


def test_no_edge_corruption_at_1080_wide(client, tmp_path):
    """Regression: FFmpeg 8.1's yuv420p->gbrp conversion garbled the last 8 columns at 1080 px wide,
    which hit graded clips and xfade transitions in 1080x1920 exports."""
    imgs = []
    for i, c in enumerate(["0x7090B0", "0xC08050"]):
        f = tmp_path / f"flat{i}.png"
        ff("-f", "lavfi", "-i", f"color=c={c}:s=1080x1920", "-frames:v", "1", str(f))
        imgs.append(upload(client, f))
    clips = [{"id": "e1", "mediaId": imgs[0]["id"], "start": 0, "inPoint": 0, "outPoint": 1,
              "filters": [{"type": "adjust", "params": {"contrast": 20}}]},
             {"id": "e2", "mediaId": imgs[1]["id"], "start": 1, "inPoint": 0, "outPoint": 1}]
    p = proj(client, clips)
    p["transitions"] = [{"trackId": p["tracks"][0]["id"], "fromClipId": "e1", "toClipId": "e2", "type": "fade", "duration": 0.6}]
    res = _export(client, p, mode="final")
    for t in (0.4, 1.0):  # graded clip, then the middle of the fade
        f = frame(res["path"], t, 1080, 1920)
        assert np.abs(f[:, -8:] - f[:, -20:-12].mean(axis=1, keepdims=True)).max() < 6, f"edge columns differ at {t}s"
