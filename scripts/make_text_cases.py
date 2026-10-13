"""Generate shared/text_cases.json: text layouts + the draw operations the export computes for them.
frontend/src/text/anim.test.ts must reproduce the operations exactly (the TS port of text_ops).

Run:  .venv\\Scripts\\python.exe scripts\\make_text_cases.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("LIGXT_DATA_DIR", tempfile.mkdtemp(prefix="ligxt_textcases_"))
sys.path.insert(0, str(ROOT / "backend"))
from app import text as T  # noqa: E402

P = {p["id"]: p for p in T.SHARED["presets"]}


def clip(spec, D=2.0, tr=None, kf=None):
    return {"start": 0, "inPoint": 0, "outPoint": D, "speed": 1,
            "transform": {"x": 0, "y": 0, "scale": 1, "rotation": 0, "opacity": 1, "flipH": False, "flipV": False,
                          "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0}, **(tr or {})},
            "keyframes": kf or {}, "text": spec}


words = "we walk through the old town".split()
timed = [{"text": w, "start": 0.25 * i, "end": 0.25 * i + 0.25} for i, w in enumerate(words)]
cases = [
    ("hook pop + fade", clip(T._merge(P["hook"]["style"], {"content": "Goa in golden hour"}), tr={"y": -420}), [0, 0.05, 0.17, 0.4, 1.0, 1.9]),
    ("lower third slide + box", clip(T._merge(P["lowerthird"]["style"], {"content": "Old Goa, India"}), tr={"x": -150, "y": 420}), [0, 0.2, 0.5, 1.85]),
    ("typewriter in / out", clip({"content": "Typed line\nsecond line", "animIn": {"type": "typewriter", "duration": 1.0, "easing": "linear"},
                                  "animOut": {"type": "typewriter", "duration": 0.5, "easing": "linear"}}), [0, 0.33, 0.71, 1.0, 1.7, 1.95]),
    ("words in / out", clip({"content": "one two three four", "size": 80, "animIn": {"type": "words", "duration": 0.8, "easing": "out"},
                             "animOut": {"type": "words", "duration": 0.6, "easing": "in"}}), [0.1, 0.45, 1.0, 1.6]),
    ("caption pop", clip(T._merge(P["caption"]["style"], {"content": " ".join(words), "words": timed}), D=1.6, tr={"y": 380}), [0, 0.3, 0.55, 1.3]),
    ("caption reveal", clip({"content": " ".join(words), "words": timed, "highlight": {"style": "reveal", "color": "#ffd34d"}}, D=1.6), [0.1, 0.6, 1.4]),
    ("beat words reveal + pop", clip({"content": "three two one go", "size": 110, "words": [{"text": w, "start": 0.5 * i, "end": 0.5 * i + 0.5} for i, w in enumerate("three two one go".split())],
                                      "highlight": {"style": "revealpop", "color": "#ffd34d"}}, D=2.2), [0.0, 0.55, 1.05, 1.6, 2.1]),
    ("caption colour", clip({"content": " ".join(words), "words": timed, "highlight": {"style": "color", "color": "#ff0000"}}, D=1.6), [0.6]),
    ("slide left + keyframes", clip({"content": "Keyframed", "font": "BebasNeue-Regular", "size": 120, "letterSpacing": 6,
                                     "animIn": {"type": "slide", "duration": 0.5, "easing": "inout", "direction": "left", "distance": 120}},
                                    tr={"x": 50, "flipH": True},
                                    kf={"rotation": [{"time": 0, "value": -10, "easing": "out"}, {"time": 2, "value": 10, "easing": "linear"}],
                                        "opacity": [{"time": 0, "value": 0.5, "easing": "linear"}, {"time": 1, "value": 1, "easing": "linear"}]}),
     [0.1, 0.25, 0.9, 1.5]),
    ("retro fade, right aligned", clip(T._merge(P["retro"]["style"], {"content": "A long retro title that wraps", "align": "right", "maxWidth": 600})), [0.2, 1.0, 1.8]),
]
out = []
for name, c, taus in cases:
    lay = T.build(c["text"])
    lay = {k: v for k, v in lay.items() if k != "base"}
    out.append({"name": name, "clip": c, "layout": lay,
                "ops": [{"tau": tau, "ops": T.text_ops(lay, c, tau, 1080, 1920)} for tau in taus]})
(ROOT / "shared" / "text_cases.json").write_text(json.dumps({"W": 1080, "H": 1920, "cases": out}, indent=1), encoding="utf-8")
print("wrote", len(out), "cases")
