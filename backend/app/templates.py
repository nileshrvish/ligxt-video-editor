"""Style templates: a project's structure and style in beats, re-fitted to new media and music.

A template (template.json, `version` = TEMPLATE_VERSION) stores no media. Times are in beats of
the template's song, so a template plays at any tempo:
  slots     shot slots on the main track: start/length in beats, which distinct media they show
            (`ref`; the same ref repeats a shot), fit, look filters, motion keyframes (in beats
            from the slot start), the transition out of the slot (length in beats)
  layers    adjustment layers (grade sections, flashes) on named overlay tracks
  texts     text clips with a role (hook / caption / outro / text), placeholder text, style,
            position, word timings and keyframes in beats
  sfx       bundled sound effects at a beat (the effect's peak lands on it)
  music     the music track's level and fades, whether a bundled (license-safe) song comes along
  sections  intro / breakdown / drop / ... as beat ranges (from the project's markers)
Generation maps beat b to time = beat grid of the new song from the chosen start beat, so every
cut lands on a detected beat, then adapts each shot to its media (faces, safe areas, colour).
"""
from __future__ import annotations

import copy
import json
import math
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import numpy as np

from . import config
from . import motion as M
from . import text as TX
from .config import CACHE_DIR, ROOT, TEMPLATES_DIR
from .media import load_meta, original_path
from .models import Clip, Project, new_project

TEMPLATE_VERSION = 1
BUILTIN_DIR = ROOT / "templates_builtin"
USER_DIR = TEMPLATES_DIR
W0, H0 = 1080, 1920
SAFE_TOP, SAFE_BOTTOM_PX, SAFE_RIGHT_PX, RIGHT_FROM = 220, 420, 140, 0.42
FACE_TOP_MIN = 236
FACE_MIN_SCORE = 0.85
LICENSE_SAFE = {"cc0", "cc0-1.0", "cc-by-4.0", "public-domain", "own", "synthesized"}


# ---------------------------------------------------------------- storage
def _tid(tid: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,60}", tid or ""):
        raise ValueError(f"Invalid template id: {tid!r}")
    return tid


def template_dir(tid: str) -> Path:
    tid = _tid(tid)
    b = BUILTIN_DIR / tid
    return b if b.exists() else USER_DIR / tid


def migrate(t: dict) -> dict:
    """Bring an older template.json up to TEMPLATE_VERSION (keeps old templates working)."""
    v = int(t.get("version") or 0)
    if v > TEMPLATE_VERSION:
        raise ValueError(f"This template was made by a newer version of Ligxt (template version {v}).")
    if v < 1:  # version 0 (pre-release drafts): slots had "beats" instead of "lengthBeats"
        for s in t.get("slots", []):
            s.setdefault("lengthBeats", s.pop("beats", 4))
        t.setdefault("layers", [])
        t.setdefault("sfx", [])
        t.setdefault("texts", [])
    t["version"] = TEMPLATE_VERSION
    return t


def load(tid: str) -> dict:
    d = template_dir(tid)
    p = d / "template.json"
    if not p.exists():
        raise FileNotFoundError(tid)
    t = migrate(json.loads(p.read_text(encoding="utf-8")))
    t["id"] = tid
    t["builtin"] = d.parent == BUILTIN_DIR
    return t


def save(t: dict) -> dict:
    if t.get("builtin"):
        raise ValueError("Built-in templates can't be changed. Duplicate it first.")
    d = USER_DIR / _tid(t["id"])
    d.mkdir(parents=True, exist_ok=True)
    out = {k: v for k, v in t.items() if k != "builtin"}
    out["version"] = TEMPLATE_VERSION
    out["updated"] = time.time()
    tmp = d / "template.json.tmp"
    tmp.write_text(json.dumps(out, indent=1), encoding="utf-8")
    tmp.replace(d / "template.json")
    return t


def new_id(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "template"
    return f"{base}-{uuid.uuid4().hex[:6]}"


def total_beats(t: dict) -> float:
    ends = [s["startBeat"] + s["lengthBeats"] for s in t["slots"]]
    return max(ends) if ends else 0.0


def summary(t: dict) -> dict:
    d = template_dir(t["id"])
    bpm = float(t.get("bpm") or 120)
    url = (f"/files/templates_builtin/{t['id']}/" if t.get("builtin") else f"/files/templates/{t['id']}/")
    has = lambda f: (d / f).exists()  # noqa: E731
    return {"id": t["id"], "name": t["name"], "description": t.get("description", ""), "category": t.get("category", "Other"),
            "tags": t.get("tags", []), "builtin": bool(t.get("builtin")), "version": t["version"],
            "bpm": round(bpm, 1), "beats": total_beats(t), "duration": round(total_beats(t) * 60 / bpm, 2),
            "slots": len(t["slots"]), "mediaSlots": int(t.get("refs", len(t["slots"]))),
            "texts": [{"key": x["key"], "role": x["role"], "placeholder": x["placeholder"]} for x in t.get("texts", [])],
            "music": {"bundled": bool((t.get("music") or {}).get("file")), "license": (t.get("music") or {}).get("license")},
            "thumb": url + "thumb.jpg?v=" + str(int((d / "thumb.jpg").stat().st_mtime)) if has("thumb.jpg") else None,
            "preview": url + "preview.mp4?v=" + str(int((d / "preview.mp4").stat().st_mtime)) if has("preview.mp4") else None,
            "updated": t.get("updated", 0)}


def list_all(q: str = "", category: str = "") -> list[dict]:
    out = []
    for base in (BUILTIN_DIR, USER_DIR):
        if not base.exists():
            continue
        for d in sorted(base.iterdir()):
            if (d / "template.json").exists():
                try:
                    t = load(d.name)
                except Exception:  # noqa: BLE001  (a damaged template never hides the others)
                    continue
                s = summary(t)
                hay = " ".join([s["name"], s["description"], s["category"], *s["tags"]]).lower()
                if q and not all(w in hay for w in q.lower().split()):
                    continue
                if category and s["category"].lower() != category.lower():
                    continue
                out.append(s)
    return sorted(out, key=lambda s: (not s["builtin"], -s["updated"], s["name"].lower()))


# ---------------------------------------------------------------- beat grids
class Grid:
    """Beat positions <-> seconds for a list of beat times (extrapolated past both ends)."""

    def __init__(self, times: list[float]):
        t = sorted(float(x) for x in times)
        if len(t) < 2:
            raise ValueError("The song needs detected beats (at least two).")
        self.t = np.array(t)
        d = np.diff(self.t)
        self.period = float(np.median(d[-8:])) if len(d) else 0.5
        self.period0 = float(np.median(d[:8])) if len(d) else 0.5

    def pos(self, sec: float) -> float:
        t = self.t
        if sec <= t[0]:
            return (sec - t[0]) / self.period0
        if sec >= t[-1]:
            return len(t) - 1 + (sec - t[-1]) / self.period
        i = int(np.searchsorted(t, sec) - 1)
        return i + (sec - t[i]) / (t[i + 1] - t[i])

    def time(self, b: float) -> float:
        t = self.t
        if b <= 0:
            return float(t[0] + b * self.period0)
        if b >= len(t) - 1:
            return float(t[-1] + (b - (len(t) - 1)) * self.period)
        i = int(math.floor(b))
        return float(t[i] + (b - i) * (t[i + 1] - t[i]))


def _snap(b: float, tol: float = 0.15) -> float:
    r = round(b)
    return float(r) if abs(b - r) <= tol else round(b, 4)


# ---------------------------------------------------------------- project -> template
def _normalize_motion(kfb: dict) -> tuple[dict, str]:
    """Keep the move, not the photo-specific framing: zooms keep only their scale curve (they are
    re-aimed at the new photo's face), pans keep their travel centred on 0."""
    vary = lambda p: len({round(k["value"], 3) for k in kfb.get(p, [])}) > 1  # noqa: E731
    if vary("scale"):
        kfb.pop("x", None)
        kfb.pop("y", None)
        return kfb, "zoom"
    if vary("x") or vary("y"):
        for p in ("x", "y"):
            if kfb.get(p):
                vals = [k["value"] for k in kfb[p]]
                mid = (max(vals) + min(vals)) / 2
                for k in kfb[p]:
                    k["value"] = round(k["value"] - mid, 3)
        return kfb, "pan"
    kfb.pop("x", None)
    kfb.pop("y", None)
    return kfb, "static"



def _kf_to_beats(kfs: dict, abs_start: float, B) -> dict:
    """Clip-relative keyframe seconds -> beats from the clip's own (unsnapped) start."""
    out = {}
    b0 = B(abs_start)
    for prop, ks in (kfs or {}).items():
        if ks:
            out[prop] = [{"beat": round(B(abs_start + float(k["time"])) - b0, 4), "value": k["value"], "easing": k.get("easing", "linear")}
                         for k in ks]
    return out


def from_project(project: Project, name: str, *, category: str = "Beat sync", tags: list[str] | None = None,
                 description: str = "", export_preset: str | None = None) -> tuple[dict, list[str]]:
    """Build a template from a project. Returns (template, notes about what was left out)."""
    notes: list[str] = []
    music = next((c for t in project.tracks if t.type == "music" and not t.muted for c in t.clips if c.mediaId), None)
    mmeta = load_meta(music.mediaId) if music else None
    if not music or not mmeta or not mmeta.get("beats"):
        raise ValueError("Templates are built on the music's beats: add a song on a music track and run Detect beats on it first.")
    grid = Grid([music.start + (b - music.inPoint) / (music.speed or 1) for b in mmeta["beats"]])
    origin = round(grid.pos(0.0))
    B = lambda sec: grid.pos(sec) - origin  # noqa: E731
    end_t = max(c.start + M.clip_duration(c.model_dump()) for t in project.tracks if t.type == "video" for c in t.clips)
    inside = grid.t[(grid.t >= -0.01) & (grid.t <= end_t + 0.01)]
    bpm = 60 * (len(inside) - 1) / float(inside[-1] - inside[0]) if len(inside) > 2 else 60 / grid.period

    main = next(t for t in project.tracks if t.type == "video")
    clips = sorted(main.clips, key=lambda c: c.start)
    refs: dict[str, int] = {}
    dropped: list[int] = []
    slots = []
    id2slot = {}
    for i, c in enumerate(clips):
        if not c.mediaId:
            continue
        m = load_meta(c.mediaId) or {}
        ref = refs.setdefault(c.mediaId, len(refs))
        s0, s1 = _snap(B(c.start)), _snap(B(c.start + M.clip_duration(c.model_dump())))
        cd = c.model_dump()
        looks = [f for f in cd["filters"] if f["type"] != "adjust"]  # per-photo corrections are redone per media
        if len(looks) != len(cd["filters"]):
            dropped.append(i + 1)
        kfb, motion = _normalize_motion(_kf_to_beats(cd.get("keyframes"), c.start, B))
        tr0 = {k: cd["transform"][k] for k in ("x", "y", "scale", "rotation", "opacity", "flipH", "flipV")}
        tr0["x"] = tr0["y"] = 0.0  # framing offsets belonged to that photo; framing is redone per media
        slots.append({"index": len(slots), "ref": ref, "startBeat": s0, "lengthBeats": round(s1 - s0, 4), "kind": m.get("type", "image"),
                      "fit": cd.get("fit"), "filters": looks, "extend": bool(cd.get("extend")), "motion": motion,
                      "transform": tr0,
                      "keyframes": kfb,
                      "speed": cd.get("speed", 1), "transitionOut": None})
        id2slot[c.id] = len(slots) - 1
    if dropped:
        notes.append(f"Per-photo colour corrections (shots {', '.join(map(str, dropped))}) are not stored: "
                     f"shots are colour-matched to each other when the template is used.")
    for tr in project.transitions:
        a = id2slot.get(tr.fromClipId)
        if a is not None and tr.trackId == main.id:
            ca = clips[[c.id for c in clips].index(tr.fromClipId)]
            cut = ca.start + M.clip_duration(ca.model_dump())
            slots[a]["transitionOut"] = {"type": tr.type, "beats": round(B(cut + tr.duration / 2) - B(cut - tr.duration / 2), 4)}

    layers, texts, sfx = [], [], []
    for t in project.tracks:
        for c in t.clips:
            cd = c.model_dump()
            end = c.start + M.clip_duration(cd)
            if t.type == "overlay":
                if c.adjustment and not c.mediaId:
                    sb = _snap(B(c.start))
                    layers.append({"track": t.name, "startBeat": sb, "lengthBeats": round(_snap(B(end)) - sb, 4), "filters": cd["filters"],
                                   "opacity": cd["transform"]["opacity"], "keyframes": _kf_to_beats(cd.get("keyframes"), c.start, B),
                                   "hidden": t.hidden})
                elif c.mediaId:
                    notes.append(f"Overlay '{t.name}': media clips on overlay tracks are not part of templates.")
            elif t.type == "text" and c.text:
                sb = _snap(B(c.start), 0.08)
                spec = copy.deepcopy(cd["text"])
                words = spec.pop("words", None)
                content = spec.pop("content", "")
                if words:
                    words = [{"text": w["text"], "beat": round(B(c.start + float(w["start"])) - B(c.start), 4),
                              "endBeat": round(B(c.start + float(w["end"])) - B(c.start), 4)} for w in words]
                role = "caption" if words else ("hook" if c.start < 0.2 else "outro" if end >= project_end(project) - 0.2 else "text")
                texts.append({"key": f"{role}{sum(1 for x in texts if x['role'] == role) + 1}", "role": role, "placeholder": content,
                              "track": t.name, "startBeat": sb, "lengthBeats": round(_snap(B(end), 0.08) - sb, 4), "spec": spec,
                              "words": words, "transform": cd["transform"], "keyframes": _kf_to_beats(cd.get("keyframes"), c.start, B)})
            elif t.type == "audio" and c.mediaId:
                m = load_meta(c.mediaId) or {}
                if m.get("sfx"):
                    peak = c.start + float(m.get("sfxPeak") or 0) - c.inPoint
                    sfx.append({"sfx": m["sfx"], "beat": round(B(peak), 4), "volume": c.volume, "track": t.name})
                else:
                    notes.append(f"Audio '{m.get('name', 'clip')}' on '{t.name}' is your own recording, so it is not stored.")
    texts.sort(key=lambda x: x["startBeat"])
    sections = [{"name": "Intro", "startBeat": 0.0}]
    for mk in sorted(project.markers, key=lambda m: m.time):
        if mk.label.strip():
            sections.append({"name": mk.label.strip(), "startBeat": _snap(B(mk.time))})
    tb = max(s["startBeat"] + s["lengthBeats"] for s in slots)
    for i, s in enumerate(sections):
        s["endBeat"] = sections[i + 1]["startBeat"] if i + 1 < len(sections) else tb
    for s in slots:
        s["section"] = next((x["name"] for x in sections if x["startBeat"] - 1e-6 <= s["startBeat"] < x["endBeat"] - 1e-6), "Intro")
    mt = next(t for t in project.tracks if t.type == "music")
    lic = str(mmeta.get("license") or "").lower()
    music_info = {"volume": music.volume * mt.volume, "fadeInBeats": round(music.fadeIn * bpm / 60, 3),
                  "fadeOutBeats": round(music.fadeOut * bpm / 60, 3), "autoDuck": (mt.autoDuck or None),
                  "file": None, "license": None, "name": mmeta.get("name")}
    if lic in LICENSE_SAFE:
        music_info["license"] = lic
        music_info["file"] = "music" + Path(mmeta["file"]).suffix
    else:
        notes.append(f"The song '{mmeta.get('name')}' is not marked license-safe, so it is not bundled: the template asks for a song.")
    t = {"version": TEMPLATE_VERSION, "id": new_id(name), "name": name, "description": description, "category": category,
         "tags": tags or [], "created": time.time(), "updated": time.time(), "bpm": round(bpm, 2),
         "settings": project.settings.model_dump(), "refs": len(refs),
         "refHints": [{"ref": i, "kind": (load_meta(mid) or {}).get("type", "image")} for mid, i in refs.items()],
         "slots": slots, "layers": layers, "texts": texts, "sfx": sfx, "sections": sections, "music": music_info,
         "trackOrder": [{"type": t.type, "name": t.name} for t in project.tracks],
         "export": {"preset": export_preset or project.export.preset, "fps": project.export.fps}}
    if music_info["file"]:
        t["_music_src"] = str(original_path(mmeta))
    return t, notes


def project_end(project: Project) -> float:
    main = next(t for t in project.tracks if t.type == "video")
    return max((c.start + M.clip_duration(c.model_dump()) for c in main.clips), default=0.0)


# ---------------------------------------------------------------- media analysis
def media_stats(meta: dict) -> dict | None:
    """Mean linear RGB of the picture (64x64) for colour matching."""
    if meta.get("type") not in ("image", "video"):
        return None
    args = [config.FFMPEG, "-v", "error"]
    if meta["type"] == "video":
        args += ["-ss", f"{min(1.0, float(meta.get('duration') or 0) / 2):.2f}"]
    args += ["-i", str(original_path(meta)), "-vf", "scale=64:64", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = subprocess.run(args, capture_output=True)
    if r.returncode or len(r.stdout) != 64 * 64 * 3:
        return None
    lin = (np.frombuffer(r.stdout, np.uint8).reshape(-1, 3) / 255.0) ** 2.2
    return {"r": float(lin[:, 0].mean()), "g": float(lin[:, 1].mean()), "b": float(lin[:, 2].mean())}


def color_match(stats: dict[str, dict], strength: float = 0.6) -> dict[str, dict]:
    """Adjust params per media that bring each shot towards the group's median brightness and white
    balance (grade.adjust: exposure gain, temperature R/B, tint G, in linear light)."""
    ok = {k: v for k, v in stats.items() if v and v["g"] > 1e-4}
    if len(ok) < 2:
        return {}
    lum = lambda s: 0.2126 * s["r"] + 0.7152 * s["g"] + 0.0722 * s["b"]  # noqa: E731
    Lt = float(np.median([lum(s) for s in ok.values()]))
    rt = float(np.median([s["r"] / s["g"] for s in ok.values()]))
    bt = float(np.median([s["b"] / s["g"] for s in ok.values()]))
    out = {}
    for k, s in ok.items():
        q = (rt / bt) * (s["b"] / max(1e-5, s["r"]))
        T = (q - 1) / (0.15 * (q + 1)) * strength
        Gk = (1 + 0.15 * T) * s["r"] / (max(1e-5, rt) * s["g"])
        Ti = max(-0.4, min(0.4, (1 - Gk) / 0.12 * strength))
        T = max(-0.4, min(0.4, T))
        gains = np.array([1 + 0.15 * T, 1 - 0.12 * Ti, 1 - 0.15 * T])
        L1 = lum({"r": s["r"] * gains[0], "g": s["g"] * gains[1], "b": s["b"] * gains[2]})
        e = max(-1.0, min(1.0, math.log2(Lt / max(1e-5, L1)) * strength))
        out[k] = {"exposure": round(e, 3), "temperature": round(T * 100, 1), "tint": round(Ti * 100, 1)}
    return out


# ---------------------------------------------------------------- generation
def _faces(meta: dict, sw: float, sh: float) -> list[tuple[float, float, float, float]]:
    fr = ((meta.get("faces") or {}).get("frames") or [])
    if not fr:
        return []
    f0 = fr[len(fr) // 2]
    return [(x * sw, y * sh, w * sw, h * sh) for x, y, w, h, s in f0["boxes"] if s >= FACE_MIN_SCORE]


def _face_boxes(c: dict, meta: dict, tau: float, W: int, H: int, fill: bool):
    sw, sh = float(meta.get("width") or W), float(meta.get("height") or H)
    tv = M.transform_at(c, tau)
    mat, _ = M.layer_matrix(sw, sh, (c.get("transform") or {}).get("crop") or {}, fill, W, H, tv)
    out = []
    for x, y, w, h in _faces(meta, sw, sh):
        pts = [M.apply(mat, X, Y) for X, Y in ((x, y), (x + w, y), (x, y + h), (x + w, y + h))]
        out.append((min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)))
    return out


def _covered(c: dict, meta: dict, tau: float, W: int, H: int, fill: bool, extend: bool) -> bool:
    sw, sh = float(meta.get("width") or W), float(meta.get("height") or H)
    tv = M.transform_at(c, tau)
    mat, (x0, y0, cw, ch) = M.layer_matrix(sw, sh, (c.get("transform") or {}).get("crop") or {}, fill, W, H, tv)
    if extend:
        x0, y0, cw, ch = -0.35 * sw, -0.35 * sh, 1.7 * sw, 1.7 * sh
    inv = M.invert(mat)
    for X, Y in ((0, 0), (W, 0), (0, H), (W, H)):
        sx, sy = M.apply(inv, X, Y)
        if max(x0 - sx, sx - (x0 + cw), y0 - sy, sy - (y0 + ch)) > 0.5:
            return False
    return True


def _shift(c: dict, dx: float, dy: float) -> None:
    kf = c.setdefault("keyframes", {})
    for prop, d in (("x", dx), ("y", dy)):
        if not d:
            continue
        if kf.get(prop):
            for k in kf[prop]:
                k["value"] = round(k["value"] + d, 3)
        else:
            c["transform"][prop] = round(c["transform"].get(prop, 0) + d, 3)


def adapt_motion(c: dict, meta: dict, W: int, H: int, fill: bool, report: list[str], label: str) -> None:
    """Face-aware motion: zooms keep the face still, then the shot moves (extending a photo's canvas
    when needed) until no face is under the Reels header, caption area or buttons."""
    sw, sh = float(meta.get("width") or W), float(meta.get("height") or H)
    faces = _faces(meta, sw, sh)
    kf = c.setdefault("keyframes", {})
    D = M.clip_duration(c)
    scales = [k["value"] for k in kf.get("scale", [])] or [c["transform"].get("scale", 1)]
    s0, smin = scales[0], min(scales)
    if faces:
        # 1. reframe: bring the main (largest) face into view, as far as the picture allows
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        base = {**c, "keyframes": {}, "transform": {**c["transform"], "x": 0, "y": 0, "scale": 1}}
        mat, (rx0, ry0, rcw, rch) = M.layer_matrix(sw, sh, {}, fill, W, H, M.transform_at(base, 0))
        qx, qy = M.apply(mat, x + w / 2, y + h / 2)
        qx, qy = qx - W / 2, qy - H / 2  # face centre relative to the canvas centre at scale 1, no offset
        lim_x = max(0.0, (sw * mat[0] * smin - W) / 2)
        lim_y = max(0.0, (sh * mat[3] * smin - H) / 2)
        ox = oy = 0.0
        if abs(s0 * qx) > 0.3 * W:  # face far off-centre (or outside the crop): pan towards it
            ox = max(-lim_x, min(lim_x, -s0 * qx))
            report.append(f"{label}: reframed {abs(ox):.0f} px {'right' if ox > 0 else 'left'} to bring the face into view.")
        if abs(s0 * qy) > 0.3 * H:
            oy = max(-lim_y, min(lim_y, -s0 * qy))
        # 2. zooms are re-aimed at the face: it keeps its place on screen while the picture scales
        if len(set(round(v, 4) for v in scales)) > 1:
            kf["x"] = [{"time": k["time"], "value": round(ox + (s0 - k["value"]) * qx, 3), "easing": k["easing"]} for k in kf["scale"]]
            kf["y"] = [{"time": k["time"], "value": round(oy + (s0 - k["value"]) * qy, 3), "easing": k["easing"]} for k in kf["scale"]]
        elif ox or oy:
            _shift(c, ox, oy)
    taus = [D * i / 12 for i in range(13)]
    moved = [0.0, 0.0]
    for _ in range(40):
        boxes = [b for tau in taus for b in _face_boxes(c, meta, tau, W, H, fill)]
        if not boxes:
            break
        top = min(b[1] for b in boxes)
        bottom = max(b[3] for b in boxes)
        right_low = [b for b in boxes if b[2] > W - SAFE_RIGHT_PX and b[3] > RIGHT_FROM * H]
        dx = dy = 0.0
        if top < FACE_TOP_MIN:
            dy = 10
        elif bottom > H - SAFE_BOTTOM_PX - 10:
            dy = -10
        if right_low:
            dx = -10
        if not dx and not dy:
            break
        _shift(c, dx, dy)
        moved[0] += dx
        moved[1] += dy
    if moved != [0.0, 0.0]:
        parts = ([f"{abs(moved[1]):.0f} px {'down' if moved[1] > 0 else 'up'}"] if moved[1] else []) +                 ([f"{abs(moved[0]):.0f} px left"] if moved[0] else [])
        report.append(f"{label}: moved {' and '.join(parts)} to keep the face out of the Reels UI.")
    if not all(_covered(c, meta, tau, W, H, fill, bool(c.get("extend"))) for tau in taus):
        if meta.get("type") == "image":
            c["extend"] = True
            report.append(f"{label}: canvas extended (blurred fill) so the move never shows the photo's edge.")
        else:
            need = 1.0
            for _ in range(30):
                need += 0.02
                if all(_covered({**c, "transform": {**c["transform"], "scale": c["transform"].get("scale", 1) * need}}, meta, tau, W, H, fill, False) for tau in taus):
                    break
            c["transform"]["scale"] = round(c["transform"].get("scale", 1) * need, 3)
            report.append(f"{label}: zoomed in {round((need - 1) * 100)} % so the video's edges never show.")


def _kf_from_beats(kfs: dict, slot_beat: float, T) -> dict:
    out = {}
    for prop, ks in (kfs or {}).items():
        t0 = T(slot_beat)
        out[prop] = [{"time": round(T(slot_beat + k["beat"]) - t0, 4), "value": k["value"], "easing": k.get("easing", "linear")} for k in ks]
    return out


def _text_box(spec: dict, tr: dict, W: int, H: int):
    lay = TX.build({**spec})
    mt = TX.text_matrix(lay["w"], lay["h"], W, H, {"x": tr.get("x", 0), "y": tr.get("y", 0), "scale": tr.get("scale", 1),
                                                     "rotation": tr.get("rotation", 0), "flipH": False, "flipV": False})
    pts = [M.apply(mt, X, Y) for X, Y in ((0, 0), (lay["w"], 0), (0, lay["h"]), (lay["w"], lay["h"]))]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _overlap(a, b) -> float:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def place_text(spec: dict, tr: dict, faces_at, W: int, H: int) -> tuple[dict, bool]:
    """Keep the text's position if it clears faces and the Reels UI; else the first clean candidate."""
    zones = [(0, 0, W, SAFE_TOP), (0, H - SAFE_BOTTOM_PX, W, H), (W - SAFE_RIGHT_PX, RIGHT_FROM * H, W, H)]
    cands = [tr.get("y", 0), -560, 330, -400, 460, -700, 200]
    for y in cands:
        t2 = {**tr, "y": y}
        b = _text_box(spec, t2, W, H)
        if b[0] < 0 or b[2] > W or any(_overlap(b, z) > 1 for z in zones):
            continue
        if any(_overlap(b, f) > 0 for f in faces_at):
            continue
        return t2, y != tr.get("y", 0)
    return tr, False


def generate(t: dict, media_ids: list[str], music_id: str, music_start: float, texts: dict[str, str] | None = None,
             name: str = "", slot_media: list[str] | None = None, color: bool = True) -> tuple[Project, list[str]]:
    """Build a normal project from a template. `media_ids` fill the template's distinct media (refs) in
    order; `slot_media` (one id per slot) overrides that. Returns (project, report)."""
    report: list[str] = []
    texts = texts or {}
    mmeta = load_meta(music_id)
    if not mmeta or not mmeta.get("beats"):
        raise ValueError("Run beat detection on the song first.")
    song = Grid(mmeta["beats"])
    b0 = int(round(song.pos(max(0.0, music_start))))
    T = lambda b: song.time(b0 + b) - song.time(b0)  # noqa: E731  timeline seconds of template beat b
    need = total_beats(t)
    if b0 + need > len(song.t) - 1:
        report.append(f"The song has detected beats up to {song.t[-1]:.1f} s; the last {b0 + need - (len(song.t) - 1):.0f} beats use its tempo "
                      f"({60 / song.period:.1f} BPM) beyond that. Pick an earlier start for a perfect fit.")
    src_start = song.time(b0)
    total = T(need)
    if src_start + total > float(mmeta.get("duration") or 0) + 0.05:
        report.append(f"The song ends {src_start + total - float(mmeta['duration']):.1f} s before the template: the end is silent. Pick an earlier start.")

    # media -> slots
    refs = int(t.get("refs") or len(t["slots"]))
    metas = {m: load_meta(m) for m in set(media_ids + (slot_media or []))}
    if any(v is None for v in metas.values()):
        raise ValueError("Some of the chosen media no longer exist.")
    if slot_media:
        assign = list(slot_media)
        if len(assign) != len(t["slots"]):
            raise ValueError("slot_media needs one media id per slot.")
    else:
        if not media_ids:
            raise ValueError("Add at least one photo or video.")
        ref_media = [media_ids[i % len(media_ids)] for i in range(refs)]
        assign = [ref_media[s["ref"]] for s in t["slots"]]
        if len(media_ids) < refs:
            report.append(f"The template has {refs} different shots and you gave {len(media_ids)} files: "
                          f"files are reused in order (shot {len(media_ids) + 1} shows file 1 again, and so on).")
        extra = media_ids[refs:]
        if extra:
            seen, k = set(), 0
            for i, s in enumerate(t["slots"]):
                if s["ref"] in seen and k < len(extra):
                    assign[i] = extra[k]
                    k += 1
                seen.add(s["ref"])
            used = k
            if used:
                report.append(f"You gave {len(media_ids)} files for {refs} shots: {used} extra file(s) replace repeated shots "
                              f"(slots that showed an earlier photo again).")
            if used < len(extra):
                report.append(f"{len(extra) - used} file(s) were not used (no free slots); drag them onto the timeline if you want them.")
        if len(set(media_ids)) > 1:  # never the same file twice in a row when files are reused
            for i in range(1, len(assign)):
                if assign[i] == assign[i - 1]:
                    nxt = assign[i + 1] if i + 1 < len(assign) else None
                    alt = (next((m for m in media_ids if m not in (assign[i - 1], nxt)), None)
                           or next(m for m in media_ids if m != assign[i - 1]))
                    assign[i] = alt

    p = new_project(name or t["name"], t["settings"].get("aspect", "9:16"))
    st = t["settings"]
    p.settings.fps = st.get("fps", 30)
    p.settings.background.mode = st.get("background", {}).get("mode", "fill")
    p.settings.background.color = st.get("background", {}).get("color", "#000000")
    W, H = int(p.settings.width), int(p.settings.height)
    # tracks in the template's order (main video first)
    from .models import Track
    tracks = []
    for tt in t.get("trackOrder") or [{"type": "video", "name": "Main video"}, {"type": "text", "name": "Text"}, {"type": "audio", "name": "Audio"}, {"type": "music", "name": "Music"}]:
        tracks.append(Track(type=tt["type"], name=tt["name"]))
    if not any(x.type == "video" for x in tracks):
        tracks.insert(0, Track(type="video", name="Main video"))
    p.tracks = tracks
    track_by = lambda typ, nm: next((x for x in p.tracks if x.type == typ and x.name == nm), None) or next(x for x in p.tracks if x.type == typ)  # noqa: E731
    main = next(x for x in p.tracks if x.type == "video")

    stats = {m: media_stats(metas[m]) for m in set(assign)} if color else {}
    grades = color_match(stats)
    if grades:
        ev = [g["exposure"] for g in grades.values()]
        report.append(f"Colour-matched {len(grades)} shots to each other (exposure {min(ev):+.2f} to {max(ev):+.2f} EV, white balance evened out).")

    slot_clips = []
    for i, s in enumerate(t["slots"]):
        mid = assign[i]
        m = metas[mid]
        start, end = T(s["startBeat"]), T(s["startBeat"] + s["lengthBeats"])
        D = end - start
        cd = {"mediaId": mid, "start": round(start, 4), "inPoint": 0.0, "outPoint": round(D, 4), "fit": s.get("fit"),
              "transform": {**s["transform"], "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0}},
              "keyframes": _kf_from_beats(s.get("keyframes"), s["startBeat"], T), "filters": copy.deepcopy(s.get("filters") or []),
              "extend": False, "speed": 1}
        if m["type"] == "video":
            dur = float(m.get("duration") or 0)
            if dur >= D:
                cd["inPoint"] = round(min(1.0, (dur - D) / 2), 4)
                cd["outPoint"] = round(cd["inPoint"] + D, 4)
            else:
                cd["speed"] = round(max(0.25, dur / D), 4)
                cd["outPoint"] = round(D * cd["speed"], 4)
                report.append(f"Slot {i + 1}: the video is shorter than the slot, so it plays at {cd['speed']:.2f}x.")
        if mid in grades:
            cd["filters"].insert(0, {"type": "adjust", "name": "", "intensity": 1, "params": grades[mid], "enabled": True})
        fill = (cd.get("fit") or p.settings.background.mode) == "fill"
        adapt_motion(cd, m, W, H, fill, report, f"Slot {i + 1} ({s.get('section', '')})")
        clip = Clip.model_validate(cd)
        main.clips.append(clip)
        slot_clips.append(clip)
    from .models import Transition
    for i, s in enumerate(t["slots"][:-1]):
        tro = s.get("transitionOut")
        if tro:
            dur = T(s["startBeat"] + s["lengthBeats"] + tro["beats"] / 2) - T(s["startBeat"] + s["lengthBeats"] - tro["beats"] / 2)
            p.transitions.append(Transition(trackId=main.id, fromClipId=slot_clips[i].id, toClipId=slot_clips[i + 1].id,
                                            type=tro["type"], duration=round(max(0.1, dur), 3)))

    for L in t.get("layers", []):
        tr = track_by("overlay", L["track"]) if any(x.type == "overlay" for x in p.tracks) else None
        if tr is None:
            from .models import Track as _T
            tr = _T(type="overlay", name=L["track"])
            p.tracks.insert(1, tr)
        tr.hidden = bool(L.get("hidden"))
        s0, s1 = T(L["startBeat"]), T(L["startBeat"] + L["lengthBeats"])
        tr.clips.append(Clip.model_validate({"mediaId": None, "adjustment": True, "start": round(s0, 4), "inPoint": 0, "outPoint": round(s1 - s0, 4),
                                             "filters": copy.deepcopy(L["filters"]), "transform": {"opacity": L.get("opacity", 1)},
                                             "keyframes": _kf_from_beats(L.get("keyframes"), L["startBeat"], T)}))

    def faces_during(a: float, b: float):
        out = []
        for clip in slot_clips:
            c0, c1 = clip.start, clip.start + M.clip_duration(clip.model_dump())
            if c1 <= a or c0 >= b:
                continue
            cd = clip.model_dump()
            fill = (cd.get("fit") or p.settings.background.mode) == "fill"
            for k in range(5):
                tau = max(0.0, min(c1 - c0, max(a, c0) - c0 + (min(b, c1) - max(a, c0)) * k / 4))
                out += _face_boxes(cd, metas[clip.mediaId], tau, W, H, fill)
        return out

    for x in t.get("texts", []):
        content = (texts.get(x["key"]) or "").strip() or x["placeholder"]
        if not content:
            continue
        s0, s1 = T(x["startBeat"]), T(x["startBeat"] + x["lengthBeats"])
        spec = {**copy.deepcopy(x["spec"]), "content": content}
        if x.get("words"):
            toks = content.split()
            tw = x["words"]
            step = (tw[-1]["beat"] - tw[0]["beat"]) / max(1, len(tw) - 1) if len(tw) > 1 else 1.0
            wb = [tw[k]["beat"] if k < len(tw) else tw[-1]["beat"] + step * (k - len(tw) + 1) for k in range(len(toks))]
            spec["words"] = [{"text": w, "start": round(T(x["startBeat"] + wb[k]) - s0, 4),
                              "end": round(T(x["startBeat"] + (wb[k + 1] if k + 1 < len(wb) else min(x["lengthBeats"], wb[k] + step))) - s0, 4)}
                             for k, w in enumerate(toks)]
            if len(toks) != len(tw):
                report.append(f"Text '{content}': {len(toks)} words instead of {len(tw)}; one word per {step:g} beat(s) from the same start.")
                if wb[-1] >= x["lengthBeats"]:
                    s1 = T(x["startBeat"] + wb[-1] + step)
        tr2, moved = place_text(spec, x["transform"], faces_during(s0, s1), W, H)
        if moved:
            report.append(f"Text '{content}' moved to y={tr2['y']:+.0f} so it doesn't cover a face.")
        track = track_by("text", x.get("track", "Text"))
        track.clips.append(Clip.model_validate({"mediaId": None, "start": round(s0, 4), "inPoint": 0, "outPoint": round(s1 - s0, 4),
                                                "transform": tr2, "keyframes": _kf_from_beats(x.get("keyframes"), x["startBeat"], T),
                                                "text": spec}))

    if t.get("sfx"):
        from .media import import_local, SFX_DIR
        sfx_media: dict[str, dict] = {}
        for e in t["sfx"]:
            if e["sfx"] not in sfx_media:
                info = json.loads((SFX_DIR / f"{e['sfx']}.json").read_text(encoding="utf-8"))
                sfx_media[e["sfx"]] = import_local(SFX_DIR / f"{e['sfx']}.wav", f"SFX {info['name']}.wav",
                                                   {"sfx": e["sfx"], "sfxPeak": float(info.get("peak", 0))})
            sm = sfx_media[e["sfx"]]
            at = T(e["beat"])
            st0 = at - sm["sfxPeak"]
            inp = max(0.0, -st0)
            track = track_by("audio", e.get("track", "Audio"))
            track.clips.append(Clip.model_validate({"mediaId": sm["id"], "start": round(max(0.0, st0), 4), "inPoint": round(inp, 4),
                                                    "outPoint": float(sm["duration"]), "volume": e.get("volume", 0.3)}))
        _split_overlaps(p)  # SFX that would overlap on one track move to a second one

    mt = next(x for x in p.tracks if x.type == "music")
    mu = t.get("music") or {}
    bpm = 60 / song.period
    mt.clips = [Clip.model_validate({"mediaId": music_id, "start": 0, "inPoint": round(src_start, 4),
                                     "outPoint": round(min(float(mmeta.get("duration") or 0), src_start + total), 4),
                                     "volume": mu.get("volume", 1), "fadeIn": round(mu.get("fadeInBeats", 0) * 60 / bpm, 3),
                                     "fadeOut": round(mu.get("fadeOutBeats", 0) * 60 / bpm, 3)})]
    if mu.get("autoDuck"):
        mt.autoDuck = mu["autoDuck"]
    from .models import Marker
    for sct in t.get("sections", [])[1:]:
        p.markers.append(Marker(time=round(T(sct["startBeat"]), 4), label=sct["name"]))
    ex = t.get("export") or {}
    p.export.preset = ex.get("preset", p.export.preset)
    p.export.fileName = name or t["name"]
    p.media = list(dict.fromkeys([*assign, music_id, *[c.mediaId for tr in p.tracks for c in tr.clips if c.mediaId]]))
    report.insert(0, f"{len(t['slots'])} shots fitted to the song at {60 / song.period:.1f} BPM from {src_start:.2f} s "
                     f"(template made at {t.get('bpm', 0):.1f} BPM): every cut is on a beat; {total:.1f} s long.")
    return p, report


def _split_overlaps(p: Project) -> None:
    from .models import Track
    new_tracks = []
    for tr in p.tracks:
        if tr.type != "audio":
            continue
        keep, spill = [], []
        end = -1.0
        for c in sorted(tr.clips, key=lambda c: c.start):
            if c.start < end - 1e-6:
                spill.append(c)
            else:
                keep.append(c)
                end = c.start + M.clip_duration(c.model_dump())
        tr.clips = keep
        if spill:
            nt = Track(type="audio", name=tr.name + " 2", clips=spill)
            new_tracks.append((tr, nt))
    for tr, nt in new_tracks:
        p.tracks.insert(p.tracks.index(tr) + 1, nt)
    if new_tracks:
        _split_overlaps(p)  # a spill track can itself overlap


# ---------------------------------------------------------------- previews (placeholder media)
PH_COLORS = [(214, 120, 70), (70, 140, 200), (226, 182, 80), (120, 90, 170), (60, 160, 130), (200, 80, 120),
             (150, 150, 160), (240, 150, 90), (90, 120, 220), (180, 200, 90), (110, 180, 200), (220, 110, 160)]


def placeholder_media(n: int) -> list[str]:
    """Simple numbered colour cards (no faces, no personal media) for template previews."""
    from PIL import Image, ImageDraw
    from .media import import_local
    ids = []
    for i in range(n):
        mid = f"feed{i:08x}"
        if load_meta(mid):
            ids.append(mid)
            continue
        c = PH_COLORS[i % len(PH_COLORS)]
        im = Image.new("RGB", (900, 1200), c)
        dr = ImageDraw.Draw(im)
        for y in range(1200):
            k = 0.65 + 0.35 * (1 - y / 1200)
            dr.line([(0, y), (900, y)], fill=tuple(int(v * k) for v in c))
        for r in range(6):
            dr.ellipse([120 + r * 90, 260 + r * 120, 420 + r * 90, 560 + r * 120], outline=(255, 255, 255), width=3)
        f = TX._font("Anton-Regular", 300)
        dr.text((450, 650), str(i + 1), font=f, anchor="mm", fill=(255, 255, 255))
        tmp = Path(tempfile.mkdtemp()) / "card.png"
        im.save(tmp)
        import_local(tmp, f"Template card {i + 1}.png", {"placeholder": True}, process=False, mid=mid)
        ids.append(mid)
    return ids


def synth_music(bpm: float, seconds: float, out: Path) -> list[float]:
    """A simple CC0 beat (kick, hat, bass, pad) at `bpm`; returns the exact beat times."""
    sr = 48000
    n = int(seconds * sr)
    t = np.arange(n) / sr
    x = np.zeros(n)
    period = 60 / bpm
    beats = [i * period for i in range(int(seconds / period) + 1)]
    rng = np.random.default_rng(3)
    roots = [55.0, 55.0 * 2 ** (8 / 12), 55.0 * 2 ** (3 / 12), 55.0 * 2 ** (10 / 12)]
    for i, b in enumerate(beats):
        s = int(b * sr)
        L = min(n - s, int(0.35 * sr))
        if L <= 0:
            continue
        tt = np.arange(L) / sr
        x[s:s + L] += 0.9 * np.sin(2 * np.pi * np.cumsum(45 + 90 * np.exp(-tt * 30)) / sr) * np.exp(-tt * 9)
        h = s + int(period / 2 * sr)
        if h + 2000 < n:
            x[h:h + 2000] += 0.15 * rng.standard_normal(2000) * np.exp(-np.arange(2000) / 300)
        f = roots[(i // 8) % 4]
        bl = min(n - s, int(period * sr))
        tb = np.arange(bl) / sr
        x[s:s + bl] += 0.25 * np.sign(np.sin(2 * np.pi * f * tb)) * np.exp(-tb * 4)
    for k, f in enumerate([220, 277.2, 329.6]):
        x += 0.04 * np.sin(2 * np.pi * f * t + k)
    x = np.tanh(x * 1.2) * 0.8
    pcm = (np.stack([x, x], 1) * 32767).astype("<i2")
    wav = out.with_suffix(".wav")
    import wave
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    if out.suffix != ".wav":
        subprocess.run([config.FFMPEG, "-v", "error", "-y", "-i", str(wav), "-b:a", "192k", str(out)], check=True)
        wav.unlink()
    return beats


def music_media_for(t: dict) -> str:
    """Media id of the template's bundled song, or a synthesized beat at the template's tempo."""
    from .media import import_local
    d = template_dir(t["id"])
    mu = t.get("music") or {}
    if mu.get("file") and (d / mu["file"]).exists():
        import hashlib
        key = f"{t['id']}-{int((d / mu['file']).stat().st_mtime)}"
        mid = "feed" + hashlib.sha1(key.encode()).hexdigest()[:8]
        if not load_meta(mid):
            import_local(d / mu["file"], f"{t['name']} (bundled music)", {"beats": mu.get("beats"), "bpm": t.get("bpm"),
                                                                        "license": mu.get("license")}, process=False, mid=mid)
        return mid
    bpm = float(t.get("bpm") or 120)
    mid = f"feedb{int(round(bpm * 10)):07x}"
    if not load_meta(mid):
        tmp = Path(tempfile.mkdtemp()) / "beat.wav"
        beats = synth_music(bpm, total_beats(t) * 60 / bpm + 4, tmp)
        import_local(tmp, f"Template beat {bpm:.0f} BPM.wav", {"beats": beats, "bpm": bpm, "license": "synthesized"}, process=False, mid=mid)
    return mid


def render_preview(t: dict, out_dir: Path, *, project: Project | None = None, start: float = 0.0, seconds: float = 8.0,
                   thumb_at: float = 0.6) -> None:
    """preview.mp4 (270x480, ~8 s, no sound) + thumb.jpg. From `project` if given (your own media),
    else the template filled with placeholder cards and a synthesized beat."""
    from . import render as R
    if project is None:
        mids = placeholder_media(int(t.get("refs") or len(t["slots"])))
        project, _ = generate(t, mids, music_media_for(t), 0.0, name=t["name"], color=False)
        start = 0.0
    p = project.model_copy(deep=True)
    p.export.width, p.export.height, p.export.fps = 270, 480, 30
    p.export.preset = "custom"
    end = min(project_end(p), start + seconds)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "preview.tmp.mp4"
    R._render_range(p, start, end, tmp)
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-i", str(tmp), "-an", "-c:v", "libx264", "-crf", "30", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_dir / "preview.mp4")], check=True)
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-ss", f"{min(thumb_at, end - start - 0.05):.2f}", "-i", str(tmp),
                    "-frames:v", "1", "-q:v", "3", str(out_dir / "thumb.jpg")], check=True)
    tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- zip export / import
def export_zip(tid: str, include_preview: bool = False) -> Path:
    t = load(tid)
    d = template_dir(tid)
    out = CACHE_DIR / "_tplzip" / f"{tid}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        body = {k: v for k, v in t.items() if k not in ("builtin", "id")}
        body["exportedFrom"] = tid
        z.writestr("template.json", json.dumps(body, indent=1))
        mu = t.get("music") or {}
        if mu.get("file") and str(mu.get("license", "")).lower() in LICENSE_SAFE and (d / mu["file"]).exists():
            z.write(d / mu["file"], mu["file"])
        if include_preview:
            for f in ("thumb.jpg", "preview.mp4"):
                if (d / f).exists():
                    z.write(d / f, f)
    return out


ZIP_ALLOWED = re.compile(r"^(template\.json|thumb\.jpg|preview\.mp4|music\.(mp3|wav|m4a|ogg|flac))$")


def import_zip(data: bytes) -> tuple[dict, bool]:
    """Returns (template, needs_preview)."""
    import io
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("That file is not a template .zip.") from e
    names = z.namelist()
    if "template.json" not in names:
        raise ValueError("The .zip has no template.json, so it isn't a Ligxt template.")
    t = migrate(json.loads(z.read("template.json").decode("utf-8")))
    for k in ("slots", "name"):
        if k not in t:
            raise ValueError(f"template.json is missing '{k}'.")
    t["id"] = new_id(t["name"])
    d = USER_DIR / t["id"]
    d.mkdir(parents=True)
    for n in names:
        if ZIP_ALLOWED.match(n) and n != "template.json":  # nothing else (and no paths) is unpacked
            (d / n).write_bytes(z.read(n))
    t.pop("exportedFrom", None)
    t["builtin"] = False
    save(t)
    return t, not (d / "preview.mp4").exists()


def save_from_project(project: Project, name: str, **kw) -> tuple[dict, list[str]]:
    t, notes = from_project(project, name, **kw)
    src = t.pop("_music_src", None)
    d = USER_DIR / t["id"]
    d.mkdir(parents=True, exist_ok=True)
    if src:
        shutil.copyfile(src, d / t["music"]["file"])
        mm = next((load_meta(c.mediaId) for tr in project.tracks if tr.type == "music" for c in tr.clips if c.mediaId), None)
        t["music"]["beats"] = (mm or {}).get("beats")
    save(t)
    return t, notes
