"""Create the built-in starter templates in templates_builtin/ (template.json, a synthesized CC0
song, a preview rendered from placeholder cards, a thumbnail).

Run:  .venv\\Scripts\\python.exe scripts\\make_builtin_templates.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("LIGXT_DATA_DIR", tempfile.mkdtemp(prefix="ligxt_builtin_"))
sys.path.insert(0, str(ROOT / "backend"))
from app import templates as TP  # noqa: E402

STYLES = json.loads((ROOT / "shared" / "text_styles.json").read_text(encoding="utf-8"))
PRE = {p["id"]: p for p in STYLES["presets"]}
SETTINGS = {"width": 1080, "height": 1920, "fps": 30, "aspect": "9:16", "background": {"mode": "fill", "color": "#000000"}, "sampleRate": 48000}
TR0 = {"x": 0, "y": 0, "scale": 1, "rotation": 0, "opacity": 1, "flipH": False, "flipV": False}


def merge(a, b):
    out = json.loads(json.dumps(a))
    for k, v in b.items():
        out[k] = merge(out.get(k, {}), v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def kf(*pts):
    return [{"beat": b, "value": v, "easing": e} for b, v, e in pts]


def zoom(L, s0, s1):
    return {"scale": kf((0, s0, "inout"), (L, s1, "linear"))}


def pan(L, a, s=1.12):
    return {"scale": kf((0, s, "linear")), "x": kf((0, a, "inout"), (L, -a, "linear"))}


def punch(L=1.0):
    return {"punch": kf((0, 1, "punch"), (0.16, 1.08, "out"), (min(L, 0.6), 1, "linear"))}


def slot(i, ref, start, L, section, keyframes=None, trans=None, filters=None):
    return {"index": i, "ref": ref, "startBeat": float(start), "lengthBeats": float(L), "kind": "image", "fit": None,
            "filters": filters or [], "extend": False, "transform": dict(TR0), "keyframes": keyframes or {}, "speed": 1,
            "transitionOut": {"type": trans[0], "beats": trans[1]} if trans else None, "section": section}


def text(key, role, placeholder, start, L, preset, position, extra=None, words_every=None):
    spec = merge(PRE[preset]["style"], extra or {})
    words = None
    if words_every:
        words = [{"text": w, "beat": k * words_every, "endBeat": (k + 1) * words_every} for k, w in enumerate(placeholder.split())]
        spec["highlight"] = {"style": "revealpop", "color": spec.get("highlight", {}).get("color", "#ffd34d")}
        spec["animIn"] = {"type": "none", "duration": 0.2, "easing": "out"}
    return {"key": key, "role": role, "placeholder": placeholder, "track": "Titles", "startBeat": float(start), "lengthBeats": float(L),
            "spec": spec, "words": words, "transform": {**TR0, **position, "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0}}, "keyframes": {}}


def layer(track, start, L, filters, opacity=1.0):
    return {"track": track, "startBeat": float(start), "lengthBeats": float(L), "filters": filters, "opacity": opacity, "keyframes": {}, "hidden": False}


def preset(name, intensity=1.0):
    return {"type": "preset", "name": name, "intensity": intensity, "params": {"skinProtect": True}, "enabled": True}


def effect(name, **params):
    return {"type": "effect", "name": name, "intensity": 1, "params": params, "enabled": True}


def adjust(**params):
    return {"type": "adjust", "name": "", "intensity": 1, "params": params, "enabled": True}


ORDER = [{"type": "video", "name": "Main video"}, {"type": "overlay", "name": "Grade"}, {"type": "overlay", "name": "Flashes"},
         {"type": "text", "name": "Titles"}, {"type": "audio", "name": "SFX"}, {"type": "music", "name": "Music"}]


def montage():
    bpm, slots, i = 124.0, [], 0
    for k in range(4):  # intro: 2-beat shots, alternating zooms, whips
        slots.append(slot(i, i % 8, 2 * k, 2, "Intro", zoom(2, 1.0, 1.08) if k % 2 == 0 else zoom(2, 1.08, 1.0),
                          ("whipright" if k % 2 == 0 else "whipleft", 0.5) if k < 3 else ("zoomin", 0.5)))
        i += 1
    for k in range(4):  # build
        slots.append(slot(i, i % 8, 8 + 2 * k, 2, "Build", pan(2, 40 if k % 2 else -40, 1.1), ("fadewhite", 0.5) if k == 3 else None))
        i += 1
    for k in range(16):  # drop: one shot per beat, punch on every shot, whip every bar
        kfs = punch()
        if k == 0:
            kfs.update({"shakeX": kf((0, 0, "linear"), (0.1, 9, "linear"), (0.25, -7, "linear"), (0.5, 3, "linear"), (0.7, 0, "linear")),
                        "shakeY": kf((0, 0, "linear"), (0.15, -6, "linear"), (0.35, 5, "linear"), (0.7, 0, "linear"))})
        slots.append(slot(i, i % 8, 16 + k, 1, "Drop", kfs, ("whipup" if (k // 4) % 2 else "whipdown", 0.4) if k % 4 == 3 and k < 15 else ("fade", 1.0) if k == 15 else None))
        i += 1
    slots.append(slot(i, i % 8, 32, 6, "Outro", zoom(6, 1.12, 1.0)))
    texts = [text("hook1", "hook", "YOUR HOOK HERE", 0, 4, "hook", {"y": 330}),
             text("caption1", "caption", "ready set go", 12, 4, "hook", {"y": -560}, {"size": 120, "outline": {"width": 4, "color": "#1a0d05"}}, words_every=1),
             text("outro1", "outro", "follow for part 2", 33, 5, "retro", {"y": -600}, {"size": 84})]
    layers = [layer("Grade", 0, 38, [preset("vivid", 0.55), effect("vignette", amount=0.25, feather=0.5)]),
              layer("Flashes", 16, 0.25, [adjust(exposure=0.8), effect("glow", amount=0.6, threshold=0.6, radius=14)])]
    sfx = [{"sfx": "whoosh-fast", "beat": float(b), "volume": 0.3, "track": "SFX"} for b in (2, 4, 6, 8, 20, 24, 28)] + \
          [{"sfx": "hit", "beat": 16.0, "volume": 0.35, "track": "SFX"}]
    return dict(id="quick-beat-montage", name="Quick beat montage", category="Beat sync", tags=["beat", "montage", "energetic", "photos"],
                description="Fast cuts on every beat with punch-ins, whips on the bars, a white flash into the drop and a hook title.",
                bpm=bpm, slots=slots, texts=texts, layers=layers, sfx=sfx, refs=8,
                sections=[{"name": "Intro", "startBeat": 0, "endBeat": 8}, {"name": "Build", "startBeat": 8, "endBeat": 16},
                          {"name": "Drop", "startBeat": 16, "endBeat": 32}, {"name": "Outro", "startBeat": 32, "endBeat": 38}],
                export={"preset": "instagram", "fps": 30}, fade=4)


def slideshow():
    bpm, slots = 90.0, []
    moves = [zoom(4, 1.0, 1.12), pan(4, 50), zoom(4, 1.12, 1.0), pan(4, -50), {"scale": kf((0, 1.0, "inout"), (4, 1.1, "linear"))},
             {"scale": kf((0, 1.12, "linear")), "y": kf((0, 60, "inout"), (4, -60, "linear"))}, zoom(4, 1.1, 1.0), pan(4, 45)]
    for k in range(8):
        slots.append(slot(k, k, 4 * k, 4, "Photos" if k else "Opening", moves[k], ("fade", 1.0) if k < 7 else None))
    texts = [text("hook1", "hook", "Our summer", 0, 6, "retro", {"y": -560}, {"size": 120}),
             text("caption1", "text", "a place · a date", 5, 7, "lowerthird", {"x": -120, "y": 430}),
             text("outro1", "outro", "thanks for watching", 28, 4, "retro", {"y": -560}, {"size": 80})]
    layers = [layer("Grade", 0, 32, [preset("warmfilm", 0.5), effect("grain", amount=0.1, size=1.5), effect("vignette", amount=0.3, feather=0.5)])]
    return dict(id="ken-burns-slideshow", name="Photo slideshow with Ken Burns", category="Slideshow", tags=["photos", "calm", "ken burns", "memories"],
                description="Slow, eased Ken Burns zooms and pans with soft crossfades and a warm film look.",
                bpm=bpm, slots=slots, texts=texts, layers=layers, sfx=[], refs=8,
                sections=[{"name": "Opening", "startBeat": 0, "endBeat": 4}, {"name": "Photos", "startBeat": 4, "endBeat": 32}],
                export={"preset": "reels", "fps": 30}, fade=4)


def before_after():
    bpm = 100.0
    slots = [slot(0, 0, 0, 8, "Before", zoom(8, 1.0, 1.08), ("wiperight", 2.0), [preset("faded", 0.8)]),
             slot(1, 1, 8, 10, "After", zoom(10, 1.12, 1.0), None, [preset("vivid", 0.6)])]
    texts = [text("label1", "text", "BEFORE", 0, 7, "memetop", {"y": -600}, {"size": 120}),
             text("label2", "text", "AFTER", 9, 9, "hook", {"y": -600}, {"size": 150}),
             text("hook1", "hook", "wait for it", 1, 5, "lowerthird", {"x": 0, "y": 430}, {"align": "center"})]
    sfx = [{"sfx": "whoosh", "beat": 8.0, "volume": 0.35, "track": "SFX"}, {"sfx": "hit", "beat": 9.0, "volume": 0.3, "track": "SFX"}]
    layers = [layer("Flashes", 9, 0.25, [adjust(exposure=0.7)])]
    return dict(id="before-after-reveal", name="Before/after reveal", category="Reveal", tags=["before", "after", "transformation", "reveal"],
                description="A 'before' shot, a wipe on the beat with a whoosh and a hit, then the 'after' in full colour.",
                bpm=bpm, slots=slots, texts=texts, layers=layers, sfx=sfx, refs=2,
                sections=[{"name": "Before", "startBeat": 0, "endBeat": 8}, {"name": "After", "startBeat": 8, "endBeat": 18}],
                export={"preset": "instagram", "fps": 30}, fade=3)


for make in (montage, slideshow, before_after):
    d = make()
    tdir = TP.BUILTIN_DIR / d["id"]
    tdir.mkdir(parents=True, exist_ok=True)
    beats = TP.synth_music(d["bpm"], sum(s["lengthBeats"] for s in d["slots"]) * 60 / d["bpm"] + 6, tdir / "music.mp3")
    t = {"version": TP.TEMPLATE_VERSION, "id": d["id"], "name": d["name"], "description": d["description"], "category": d["category"],
         "tags": d["tags"], "created": time.time(), "updated": time.time(), "bpm": d["bpm"], "settings": SETTINGS, "refs": d["refs"],
         "refHints": [{"ref": i, "kind": "image"} for i in range(d["refs"])], "slots": d["slots"], "layers": d["layers"], "texts": d["texts"],
         "sfx": d["sfx"], "sections": d["sections"], "trackOrder": ORDER, "export": d["export"],
         "music": {"volume": 0.9, "fadeInBeats": 0, "fadeOutBeats": d["fade"], "autoDuck": None, "file": "music.mp3", "license": "synthesized",
                   "beats": beats, "name": f"Ligxt beat {d['bpm']:.0f} BPM (synthesized, CC0)"}}
    (tdir / "template.json").write_text(json.dumps(t, indent=1), encoding="utf-8")
    t = TP.load(d["id"])
    TP.render_preview(t, tdir)
    print("built", d["id"], f"{TP.total_beats(t) * 60 / d['bpm']:.1f} s", sorted(p.name for p in tdir.iterdir()))
