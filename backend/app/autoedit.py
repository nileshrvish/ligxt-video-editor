"""Auto-edit, step 2: a draft edit from analysed footage (autoedit_analysis.py) and a song, then a normal project.

Structure: the song's beats and drop (analysis.py, Phase 8). The reel starts on the beat that puts the drop at the
style's spot; sections around it: intro (calm) -> build -> drop (the strongest shot) -> peak (high motion) ->
breakdown (where the song's energy dips) -> outro. Each section is filled with slots of the style's lengths (in
beats). Or a template's own slots and sections.

Choosing shots: every usable shot offers its best window for a slot (the moment with the best weighted score for
that section: quality, energy match, faces, steadiness); the drop slot is filled first with the strongest shot,
then the rest in order with penalties for reusing a shot, the same clip twice in a row, a similar-looking shot next
to it, the same framing (wide/close) twice, near-duplicates anywhere. Locked slots and excluded clips are respected.

Building: grade, transitions, punch-ins/shake, AI slow-mo slam on the drop, auto-reframe for clips that don't fit
the frame (tracking the main face or the most salient subject), minimal text (hook + optional outro) placed clear
of faces and the Reels UI, the hook tracked onto a face or put behind the subject where the style asks for it.
No sound effects.
"""
from __future__ import annotations

import copy
import math
import time

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import analysis as SA
from . import autoedit_analysis as AA
from . import motion as M
from .jobs import JobContext, jobs
from .media import load_meta
from .models import Clip, Project, new_project
from .projects import save_project

router = APIRouter(prefix="/api/autoedit", tags=["autoedit"])

NEAR_DUP = 0.70  # shots this alike count as the same picture
SIMILAR = 0.55  # don't put these next to each other
TR0 = {"x": 0, "y": 0, "scale": 1, "rotation": 0, "opacity": 1, "flipH": False, "flipV": False, "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0}}

STYLES: dict[str, dict] = {
    "energetic": {
        "label": "Energetic", "target": 18.0, "dropAt": 4.5, "dropLen": 2, "outroLen": 4,
        "lengths": {"intro": [2, 2, 1, 1], "build": [1, 1, 2], "drop": [2], "peak": [1, 1, 2, 1, 1, 2, 1, 1], "breakdown": [2, 2], "outro": [4]},
        "motion": {"hook": 0.7, "intro": 0.45, "build": 0.6, "drop": 1.0, "peak": 0.8, "breakdown": 0.3, "outro": 0.35},
        "weights": {"quality": 1.0, "energy": 0.9, "faces": 0.35, "stable": 0.15},
        "grade": [{"type": "preset", "name": "vivid", "intensity": 0.3, "params": {"skinProtect": True}},
                  {"type": "adjust", "name": "", "intensity": 1, "params": {"contrast": 12, "vibrance": 10, "highlights": -10}},
                  {"type": "effect", "name": "vignette", "intensity": 1, "params": {"amount": 0.2, "feather": 0.6}}],
        "transition": {"section": "whipleft", "drop": "whipup", "within": None}, "punch": True, "shake": True, "flash": True,
        "slam": True, "slams": 2, "push": False, "slowCalm": None, "hook": {"font": "Anton-Regular", "size": 190, "follow": True, "behind": False},
    },
    "cinematic": {
        "label": "Cinematic", "target": 24.0, "dropAt": 8.0, "dropLen": 4, "outroLen": 8,
        "lengths": {"intro": [4, 4], "build": [2, 2, 4], "drop": [4], "peak": [2, 2, 4, 2, 2], "breakdown": [4, 4], "outro": [8]},
        "motion": {"hook": 0.4, "intro": 0.2, "build": 0.45, "drop": 0.95, "peak": 0.7, "breakdown": 0.2, "outro": 0.15},
        "weights": {"quality": 1.2, "energy": 0.6, "faces": 0.3, "stable": 0.45},
        "grade": [{"type": "preset", "name": "warmfilm", "intensity": 0.35, "params": {"skinProtect": True}},
                  {"type": "adjust", "name": "", "intensity": 1, "params": {"contrast": 8, "vibrance": 8, "highlights": -14, "blacks": -6}},
                  {"type": "effect", "name": "vignette", "intensity": 1, "params": {"amount": 0.28, "feather": 0.6}},
                  {"type": "effect", "name": "grain", "intensity": 1, "params": {"amount": 0.09, "size": 1.5}}],
        "transition": {"section": "fade", "drop": "whipup", "within": None}, "punch": False, "shake": False, "flash": True,
        "slam": True, "slams": 1, "push": True, "slowCalm": None, "hook": {"font": "Anton-Regular", "size": 210, "follow": False, "behind": True},
    },
    "chill": {
        "label": "Chill", "target": 24.0, "dropAt": 9.0, "dropLen": 4, "outroLen": 8,
        "lengths": {"intro": [4, 4], "build": [4], "drop": [4], "peak": [4, 2, 2, 4], "breakdown": [8], "outro": [8]},
        "motion": {"hook": 0.2, "intro": 0.15, "build": 0.25, "drop": 0.5, "peak": 0.35, "breakdown": 0.1, "outro": 0.1},
        "weights": {"quality": 1.2, "energy": 0.35, "faces": 0.25, "stable": 0.6},
        "grade": [{"type": "preset", "name": "goldenhour", "intensity": 0.3, "params": {"skinProtect": True}},
                  {"type": "preset", "name": "faded", "intensity": 0.25, "params": {"skinProtect": True}},
                  {"type": "adjust", "name": "", "intensity": 1, "params": {"highlights": -10, "vibrance": 6}},
                  {"type": "effect", "name": "grain", "intensity": 1, "params": {"amount": 0.06, "size": 1.4}}],
        "transition": {"section": "dissolve", "drop": "fade", "within": "dissolve"}, "punch": False, "shake": False, "flash": False,
        "slam": False, "slams": 0, "push": True, "slowCalm": 0.75, "hook": {"font": "DMSerifDisplay-Italic", "size": 110, "follow": False, "behind": False},
    },
}
SECTION_ORDER = ("intro", "build", "drop", "peak", "breakdown", "outro")


# ---------------------------------------------------------------- song structure
def song_plan(music: dict, style: dict, music_start: float | None = None, length: float | None = None) -> dict:
    """Reel beats and sections: {"b0", "beats" (reel seconds of each beat), "sections": [(name, b0, b1)], "dropBeat"}."""
    beats = [float(b) for b in (music.get("beats") or [])]
    if len(beats) < 8:
        raise ValueError("The song needs detected beats: run beat detection on it first.")
    e = SA.energy(music)
    period = float(np.median(np.diff(beats)))
    drop_beat = int(e.get("dropBeat") if e.get("dropBeat") is not None else len(beats) // 3)
    if music_start is not None:
        b0 = int(np.argmin([abs(b - music_start) for b in beats]))
    else:
        b0 = max(0, drop_beat - int(round(style["dropAt"] / period)))
    d = drop_beat - b0
    if d < 2:  # the drop is too close to the start: use the beat grid without a drop moment
        d = min(len(beats) - b0 - 1, int(round(style["dropAt"] / period)))
    target = length or style["target"]
    n = d + 4 * max(2, int(math.ceil((target - (beats[b0 + d] - beats[b0])) / (4 * period))))
    n = min(n, len(beats) - 1 - b0)
    R = [beats[b0 + k] - beats[b0] for k in range(n + 1)]
    # per-beat song energy (for peak vs breakdown after the drop)
    curve = np.array(e.get("curve") or [], dtype=float)
    rate = float(e.get("rate") or 10)

    def en(k):
        if not len(curve):
            return 0.5
        a, b = int(beats[b0 + k] * rate), int(beats[min(len(beats) - 1, b0 + k + 1)] * rate)
        return float(curve[a:max(a + 1, b)].mean()) if a < len(curve) else 0.5

    E = [en(k) for k in range(n)]
    med = float(np.median(E[d:])) if n > d else 0.5
    outro0 = max(d + style["dropLen"], n - style["outroLen"])
    build0 = max(0, d - 4)
    secs = []
    if build0 > 0:
        secs.append(["intro", 0, build0])
    if d > build0:
        secs.append(["build", build0, d])
    secs.append(["drop", d, min(n, d + style["dropLen"])])
    k = d + style["dropLen"]
    while k < outro0:  # peak / breakdown by the song's energy, in whole bars
        k1 = min(outro0, k + 4)
        name = "breakdown" if np.mean(E[k:k1]) < 0.85 * med else "peak"
        if secs[-1][0] == name:
            secs[-1][2] = k1
        else:
            secs.append([name, k, k1])
        k = k1
    if outro0 < n:
        secs.append(["outro", outro0, n])
    return {"b0": b0, "musicStart": beats[b0], "beats": R, "sections": [tuple(s) for s in secs], "dropBeat": d, "period": period,
            "energy": [round(v, 3) for v in E]}


def slots_for(plan: dict, style: dict) -> list[dict]:
    out = []
    for name, a, b in plan["sections"]:
        pat = style["lengths"].get(name) or [2]
        k, i = a, 0
        while k < b:
            ln = min(pat[i % len(pat)], b - k)
            out.append({"section": name, "b0": k, "b1": k + ln})
            k += ln
            i += 1
    R = plan["beats"]
    for i, s in enumerate(out):
        s.update(i=i, start=round(R[s["b0"]], 4), dur=round(R[s["b1"]] - R[s["b0"]], 4))
    return out


def template_slots(t: dict, plan: dict) -> list[dict]:
    """A template's slots on this song (its sections named by its markers; unnamed = by the song's energy)."""
    R = plan["beats"]
    out = []
    secs = t.get("sections") or []
    for i, s in enumerate(t["slots"]):
        b0, b1 = int(round(s["startBeat"])), int(round(s["startBeat"] + s["lengthBeats"]))
        if b1 >= len(R):
            break
        nm = next((x["name"] for x in secs if x["startBeat"] <= s["startBeat"] < x.get("endBeat", 1e9)), "")
        low = nm.lower()
        sec = next((k for k in SECTION_ORDER if k in low), None) or ("peak" if "chorus" in low else "intro" if i == 0 else "peak")
        out.append({"i": i, "section": sec, "b0": b0, "b1": b1, "start": round(R[b0], 4), "dur": round(R[b1] - R[b0], 4),
                    "speed": float(s.get("speed") or 1), "ramp": s.get("speedRamp"), "kind": s.get("kind")})
    return out


# ---------------------------------------------------------------- candidates
def _window_score(S: list[dict], style: dict, section: str, kind: str = "", fit: float = 0.0) -> tuple[float, dict]:
    """Score of a window of samples for a slot. `fit` = how far the clip's look is from the rest of the footage
    (0 = typical, larger = an outlier), so one odd clip doesn't break a consistent edit."""
    w = style["weights"]
    q = float(np.mean([AA.quality(s) for s in S]))
    e = float(np.mean([s["energy"] for s in S]))
    luma = float(np.mean([s["luma"] for s in S]))
    st = float(np.mean([s["stable"] for s in S]))
    nf = max(s["nfaces"] for s in S)
    fr = max(s["frontal"] for s in S)
    face = min(1.0, fr * 0.5 + nf * 0.2)
    target = style["motion"].get(section, 0.5)
    if section == "drop":
        em = e  # the strongest: as much motion as there is
    else:
        em = 1 - min(1.0, abs(e - target) * 1.6)
    total = w["quality"] * q + w["energy"] * em + w["faces"] * face + w["stable"] * st
    if section == "hook":  # the first second: striking (motion, faces to camera, a clean picture)
        total += 0.35 * min(1.0, e * 1.5) + 0.25 * min(1.0, fr * 0.5) + 0.2 * q
    if section == "outro":  # a calm, open ending
        total += {"wide": 0.4, "medium": 0.05, "detail": -0.2, "close": -0.25}.get(kind, 0) + 0.2 * min(1.0, luma / 0.45)
    if luma < 0.32:  # dark moments look muddy in a bright edit
        total -= (0.32 - luma) * 4.0
    total -= fit
    return total, {"quality": round(q, 3), "energy": round(e, 3), "stable": round(st, 3), "faces": nf, "frontal": fr,
                   "sharp": round(float(np.mean([s["sharp"] for s in S])), 3), "exposure": round(float(np.mean([s["exp"] for s in S])), 3),
                   "comp": round(float(np.mean([s["comp"] for s in S])), 3)}


def best_window(an: dict, shot: dict, need: float, style: dict, section: str, avoid: list[tuple[float, float]] | None = None, fit: float = 0.0):
    """The best window [t, t + need] inside a shot for this slot (or None if the shot is too short / unusable)."""
    S = an["samples"]
    if an.get("type") == "image":
        sc, parts = _window_score(S, style, section, shot.get("kind", ""), fit)
        return {"in": 0.0, "out": round(need, 3), "score": sc, "parts": parts}
    fps = an["fps"]
    a, b = shot["start"] + 0.12, shot["end"] - 0.12
    if b - a < need:
        return None
    best = None
    step = 1 / fps
    t = a
    while t + need <= b + 1e-6:
        i0, i1 = int(t * fps), max(int(t * fps) + 1, int((t + need) * fps))
        W = S[i0:i1]
        if W and not any(s["bad"] for s in W) and not any(not (t + need <= x0 or t >= x1) for x0, x1 in (avoid or [])):
            sc, parts = _window_score(W, style, section, shot.get("kind", ""), fit)
            if best is None or sc > best["score"]:
                best = {"in": round(t, 3), "out": round(t + need, 3), "score": sc, "parts": parts}
        t += step
    return best


def reasons(parts: dict, kind: str, section: str, an: dict) -> list[str]:
    r = []
    if parts["sharp"] > 0.7:
        r.append("sharp")
    if parts["frontal"]:
        r.append(f"{parts['frontal']} face{'s' if parts['frontal'] > 1 else ''} to camera")
    elif parts["faces"]:
        r.append(f"{parts['faces']} face{'s' if parts['faces'] > 1 else ''}")
    if parts["energy"] > 0.5:
        r.append("high motion")
    elif parts["energy"] < 0.15:
        r.append("calm")
    if parts["stable"] > 0.9:
        r.append("steady")
    if parts["exposure"] > 0.75:
        r.append("well exposed")
    if parts["comp"] > 0.62:
        r.append("good composition")
    r.append(kind)
    if section == "drop":
        r.insert(0, "strongest shot, on the drop")
    elif section == "hook":
        r.insert(0, "hook: the most striking opener")
    elif section == "outro":
        r.insert(0, "calm, open ending")
    return r


# ---------------------------------------------------------------- the draft
def draft(media_ids: list[str], music_id: str, style_id: str = "energetic", *, template_id: str | None = None,
          locks: dict | None = None, exclude: list[str] | None = None, music_start: float | None = None, length: float | None = None,
          aspect: str = "9:16", texts: dict | None = None) -> dict:
    style = STYLES.get(style_id) or STYLES["energetic"]
    music = load_meta(music_id)
    if not music:
        raise ValueError("The song is missing.")
    plan = song_plan(music, style, music_start, length)
    t = None
    if template_id:
        from .templates_api import _load
        t = _load(template_id)
        slots = template_slots(t, plan)
    else:
        slots = slots_for(plan, style)
    exclude = set(exclude or [])
    ans = {}
    for mid in media_ids:
        if mid in exclude:
            continue
        a = AA.cached(mid)
        if a and a.get("shots"):
            ans[mid] = a
    if not ans:
        raise ValueError("No analysed clips: analyse the footage first (or a clip is excluded).")
    W, H = (1080, 1920) if aspect == "9:16" else (1920, 1080) if aspect == "16:9" else (1080, 1080) if aspect == "1:1" else (1080, 1350)
    shots = [(mid, s) for mid, a in ans.items() for s in a["shots"] if s["badFrac"] < 0.8]
    locks = {int(k): v for k, v in (locks or {}).items()}
    fits = group_fit(ans)
    if slots and not template_id:
        slots[0]["section"] = "hook" if slots[0]["section"] in ("intro", "build") else slots[0]["section"]
        # more slams: 2-beat peak slots at least 8 beats after the last one
        last = next((s["b0"] for s in slots if s["section"] == "drop"), -99)
        extra = style.get("slams", 0) - 1
        for s in slots:
            if extra <= 0:
                break
            if s["section"] == "peak" and s["b1"] - s["b0"] >= 2 and s["b0"] - last >= 8:
                s["slam"] = True
                last = s["b0"]
                extra -= 1

    def speed_for(slot):
        if slot.get("ramp") or slot.get("speed"):
            if slot.get("ramp"):
                return 1.0 / max(1e-6, M.clip_duration({"inPoint": 0, "outPoint": 1, "speed": 1, "speedRamp": slot["ramp"]}))
            return slot["speed"]
        if (slot["section"] == "drop" and style["slam"]) or slot.get("slam"):
            return 1.0 / M.clip_duration({"inPoint": 0, "outPoint": 1, "speed": 1, "speedRamp": SLAM})
        if style["slowCalm"] and slot["section"] in ("intro", "breakdown", "outro"):
            return style["slowCalm"]
        return 1.0

    cache: dict = {}

    def cands(slot):
        key = (slot["i"],)
        if key in cache:
            return cache[key]
        need = slot["dur"] * speed_for(slot)
        out = []
        for mid, sh in shots:
            bw = best_window(ans[mid], sh, need, style, slot["section"], fit=fits.get(mid, 0.0))
            if bw:
                out.append({"mediaId": mid, "shot": sh["i"], "in": bw["in"], "out": bw["out"], "base": bw["score"], "parts": bw["parts"],
                            "kind": sh["kind"], "thumb": sh["thumb"], "name": ans[mid].get("name")})
        cache[key] = out
        return out

    chosen: dict[int, dict] = {}
    sig = {(mid, sh["i"]): sh for mid, sh in shots}

    def penalty(c, i):
        p = 0.0
        uses = 0
        me = sig[(c["mediaId"], c["shot"])]
        for j, o in chosen.items():
            if j == i:
                continue
            same_shot = o["mediaId"] == c["mediaId"] and o["shot"] == c["shot"]
            if same_shot:
                p += 0.9 if not (c["out"] <= o["in"] or c["in"] >= o["out"]) else 0.35
                if abs(j - i) == 1:
                    p += 3.0
            else:
                sim = AA.similarity(me, sig[(o["mediaId"], o["shot"])])
                if sim >= NEAR_DUP:
                    p += 0.45
                if abs(j - i) == 1:
                    if o["mediaId"] == c["mediaId"]:
                        p += 3.0  # the same clip twice in a row looks like a mistake
                    if sim >= SIMILAR:
                        p += 0.6 * sim
                    if o["kind"] == c["kind"]:
                        p += 0.1
            if o["mediaId"] == c["mediaId"]:
                uses += 1
        return p + min(0.66, 0.22 * uses)  # spread the edit over the clips (saturates when footage is scarce)

    for i, lk in locks.items():
        if i < len(slots):
            hit = next((c for c in cands(slots[i]) if c["mediaId"] == lk.get("mediaId") and c["shot"] == lk.get("shot", c["shot"])), None)
            if hit:
                need = slots[i]["dur"] * speed_for(slots[i])
                tin = float(lk.get("in", hit["in"]))
                chosen[i] = {**hit, "in": round(tin, 3), "out": round(tin + need, 3), "locked": True}
    order = sorted(range(len(slots)), key=lambda i: (0 if slots[i]["section"] == "drop" else 1, i))
    for i in order:
        if i in chosen:
            continue
        cs = cands(slots[i])
        if not cs:
            continue
        best = max(cs, key=lambda c: c["base"] - penalty(c, i))
        chosen[i] = {**best, "locked": False}
    out_slots = []
    for i, s in enumerate(slots):
        c = chosen.get(i)
        alts = sorted(cands(s), key=lambda x: x["base"] - penalty(x, i), reverse=True)
        seen, ranked = set(), []
        for a in alts:
            key = (a["mediaId"], a["shot"])
            if key in seen or (c and key == (c["mediaId"], c["shot"])):
                continue
            seen.add(key)
            ranked.append({**{k: a[k] for k in ("mediaId", "shot", "in", "out", "kind", "thumb", "name")}, "score": round(a["base"] - penalty(a, i), 3),
                           "reasons": reasons(a["parts"], a["kind"], s["section"], ans[a["mediaId"]])})
            if len(ranked) >= 6:
                break
        pick = None
        if c:
            pick = {**{k: c[k] for k in ("mediaId", "shot", "in", "out", "kind", "thumb", "name", "parts")}, "locked": c.get("locked", False),
                    "score": round(c["base"] - penalty(c, i), 3), "reasons": reasons(c["parts"], c["kind"], s["section"], ans[c["mediaId"]])}
        out_slots.append({**s, "speed": round(speed_for(s), 4), "pick": pick, "alternatives": ranked})
    used = {(s["pick"]["mediaId"], s["pick"]["shot"]) for s in out_slots if s["pick"]}
    return {"style": style_id, "styleLabel": style["label"], "template": template_id, "media": media_ids, "exclude": sorted(exclude),
            "music": music_id, "musicStart": plan["musicStart"], "length": plan["beats"][-1], "dropAt": plan["beats"][plan["dropBeat"]],
            "sections": plan["sections"], "beats": plan["beats"], "slots": out_slots, "aspect": aspect, "width": W, "height": H,
            "texts": texts or {}, "shotsUsed": len(used), "shotsAvailable": len(shots)}


def group_fit(ans: dict) -> dict[str, float]:
    """Per clip: a penalty for looking unlike the rest of the footage (its shots' closest matches among the other
    clips, against the median). 0 for typical clips; ~0.2-0.6 for clear outliers (e.g. a dark gym among outdoor travel)."""
    if len(ans) < 4:
        return {}
    fit = {}
    for mid, a in ans.items():
        others = [s for m2, b in ans.items() if m2 != mid for s in b["shots"]]
        sims = [float(np.mean(sorted(AA.similarity(s, o) for o in others)[-5:])) for s in a["shots"]]
        fit[mid] = float(np.mean(sims)) if sims else 0.0
    med = float(np.median(list(fit.values())))
    return {mid: round(min(1.0, max(0.0, (med - f) / 0.03) * 0.3), 3) for mid, f in fit.items()}


SLAM = {"points": [{"u": 0, "speed": 0.32}, {"u": 0.4, "speed": 0.36}, {"u": 0.46, "speed": 1.8}, {"u": 1, "speed": 1.8}], "preset": "slam"}


# ---------------------------------------------------------------- the project
def _punch(t, amount=0.1):
    return [{"time": round(t, 4), "value": 1, "easing": "punch"}, {"time": round(t + 0.08, 4), "value": 1 + amount, "easing": "out"},
            {"time": round(t + 0.36, 4), "value": 1, "easing": "linear"}]


def _shake(px=14, dur=0.4, fps=30.0, seed=3):
    xs, ys = [], []
    n = int(dur * fps)
    for i in range(n + 1):
        a = px * (1 - i / n) ** 2
        xs.append({"time": round(i / fps, 4), "value": round(a * math.sin(i * 2.7 + seed), 2), "easing": "linear"})
        ys.append({"time": round(i / fps, 4), "value": round(a * math.cos(i * 3.9 + seed * 2), 2), "easing": "linear"})
    return xs, ys


def _subject_track(meta: dict, an: dict, cin: float, cout: float, name: str) -> dict | None:
    """Track the main face (or the most salient subject) of a window, for auto-reframe / a tracked title."""
    from . import tracking as TR
    S = [s for s in an["samples"] if cin <= s["t"] < cout]
    if not S:
        return None
    mid_s = S[len(S) // 2]

    class Ctx:
        def progress(self, *a):
            pass

        def cancelled(self):
            return False

    faces = [f for f in (mid_s.get("faces") or []) if f["score"] > 0.75]
    try:
        if faces:
            f = max(faces, key=lambda x: x["size"])
            return TR.run_track(meta, {"method": "face", "seed": {"t": mid_s["t"], "box": f["box"]}, "range": [cin, cout], "direction": "both",
                                       "name": name}, Ctx())["track"]
        cx, cy = mid_s["cx"], mid_s["cy"]
        bw, bh = 0.22, 0.22 * an["width"] / max(1, an["height"])
        box = [min(max(0, cx - bw / 2), 1 - bw), min(max(0, cy - bh / 2), 1 - bh), bw, bh]
        return TR.run_track(meta, {"method": "box", "seed": {"t": mid_s["t"], "box": box}, "range": [cin, cout], "direction": "both",
                                   "name": name}, Ctx())["track"]
    except (ValueError, RuntimeError):
        return None


def build_project(d: dict, name: str, ctx: JobContext | None = None) -> tuple[Project, list[str]]:
    style = STYLES.get(d["style"]) or STYLES["energetic"]
    W, H = d["width"], d["height"]
    report: list[str] = []
    slots = [s for s in d["slots"] if s.get("pick")]
    if not slots:
        raise ValueError("The draft has no shots.")
    if d.get("template"):
        return _build_from_template(d, name, ctx)
    p = new_project(name, d["aspect"])
    p.settings.fps = 30
    p.settings.background.mode = "fill"
    p.settings.background.color = "#000000"
    pd = p.model_dump(mode="json")
    main = next(t for t in pd["tracks"] if t["type"] == "video")
    main["name"] = "Main video"
    metas = {s["pick"]["mediaId"]: load_meta(s["pick"]["mediaId"]) for s in slots}
    ans = {m: AA.cached(m) for m in metas}
    clips, tracks_ = [], []
    n = len(slots)
    for k, s in enumerate(slots):
        pk = s["pick"]
        m = metas[pk["mediaId"]]
        if ctx:
            ctx.progress(0.05 + 0.6 * k / n, f"Shot {k + 1} of {n}")
        c = {"id": f"s{k + 1}", "mediaId": pk["mediaId"], "start": s["start"], "inPoint": pk["in"], "outPoint": pk["out"], "speed": 1,
             "fit": "fill", "muted": True, "transform": dict(TR0), "keyframes": {}, "filters": []}
        if m["type"] == "image":
            c["inPoint"], c["outPoint"] = 0.0, s["dur"]
            c["extend"] = True
            c["keyframes"]["scale"] = [{"time": 0, "value": 1.0, "easing": "inout"}, {"time": s["dur"], "value": 1.08, "easing": "linear"}]
        else:
            sp = s.get("speed", 1.0)
            if (s["section"] == "drop" and style["slam"]) or s.get("slam"):
                c["speedRamp"] = copy.deepcopy(SLAM)
                c["slowmo"] = {"quality": "ai"}
                if s.get("slam") and style["punch"]:
                    c["keyframes"]["punch"] = _punch(0.0, 0.1)
            elif abs(sp - 1) > 1e-3:
                c["speed"] = sp
                c["slowmo"] = {"quality": "ai"}
            if m.get("fps") and abs(float(m["fps"]) - 30) > 0.5:  # 24/25 fps footage: AI frame-rate conversion, no repeated frames
                c["slowmo"] = {**(c.get("slowmo") or {"quality": "ai"}), "smooth": True}
            kf = c["keyframes"]
            if style["push"]:
                kf["scale"] = [{"time": 0, "value": 1.0, "easing": "inout"}, {"time": s["dur"], "value": 1.06, "easing": "linear"}]
            if style["punch"] and s["section"] in ("drop", "peak"):
                kf["punch"] = _punch(0.0, 0.12 if s["section"] == "drop" else 0.08)
            if style["shake"] and s["section"] == "drop":
                kf["shakeX"], kf["shakeY"] = _shake()
            # a clip that doesn't fit the frame: reframe on its main face / subject
            an = ans[pk["mediaId"]]
            if abs(an["width"] / max(1, an["height"]) - W / H) > 0.15:
                span = sorted([pk["in"], pk["out"]])
                tr = _subject_track(m, an, span[0], span[1], f"Subject (shot {k + 1})")
                if tr:
                    tracks_.append(tr)
                    c["reframe"] = {"trackId": tr["id"], "third": "center", "smoothing": 0.6}
                    report.append(f"Shot {k + 1}: {'landscape' if an['landscape'] else 'portrait'} clip auto-reframed on its "
                                  f"{'main face' if tr['kind'] == 'face' else 'main subject'}.")
        clips.append(c)
    main["clips"] = clips
    # transitions: at section changes (and within sections for Chill), centred on the cut, half a beat
    trs = []
    half = d["beats"][1] - d["beats"][0]
    for k in range(1, len(slots)):
        a, b = slots[k - 1], slots[k]
        if b["section"] == "drop" and a["section"] != "drop":
            ty = style["transition"]["drop"]
        elif a["section"] != b["section"]:
            ty = style["transition"]["section"]
        else:
            ty = style["transition"]["within"]
        if ty:
            dur = half if ty.startswith("whip") else half * (2 if style["label"] == "Chill" else 1)
            trs.append({"trackId": main["id"], "fromClipId": clips[k - 1]["id"], "toClipId": clips[k]["id"], "type": ty, "duration": round(dur, 4)})
    pd["transitions"] = trs
    total = d["beats"][-1]
    ov = next(t for t in pd["tracks"] if t["type"] == "overlay")
    ov["name"] = "Grade"
    ov["clips"] = [{"id": "grade", "mediaId": None, "adjustment": True, "start": 0, "inPoint": 0, "outPoint": round(total, 4), "transform": dict(TR0),
                    "keyframes": {}, "filters": [{**f, "enabled": True} for f in copy.deepcopy(style["grade"])]}]
    if style["flash"]:
        drop_t = d["dropAt"]
        ov["clips"].append({"id": "flash", "mediaId": None, "adjustment": True, "start": round(drop_t, 4), "inPoint": 0, "outPoint": round(4 / 30, 4),
                            "transform": dict(TR0),
                            "keyframes": {"intensity": [{"time": 0, "value": 1, "easing": "linear"}, {"time": round(4 / 30, 4), "value": 0, "easing": "linear"}]},
                            "filters": [{"type": "adjust", "name": "", "intensity": 1, "params": {"exposure": 1.1}, "enabled": True}]})
        ov["clips"].sort(key=lambda c: c["start"])
        # the flash overlaps the grade on the same track: give it its own track
        pd["tracks"].insert(pd["tracks"].index(ov) + 1, {"id": "fx", "type": "overlay", "name": "Flash", "muted": False, "hidden": False, "locked": False,
                                                          "volume": 1, "clips": [c for c in ov["clips"] if c["id"] == "flash"]})
        ov["clips"] = [c for c in ov["clips"] if c["id"] != "flash"]
    mu = next(t for t in pd["tracks"] if t["type"] == "music")
    mu["clips"] = [{"id": "song", "mediaId": d["music"], "start": 0, "inPoint": round(d["musicStart"], 4), "outPoint": round(d["musicStart"] + total, 4),
                    "fadeOut": round(min(1.6, half * 3), 3)}]
    # text: a hook (and an optional closing line), minimal, clear of faces and the Reels UI
    texts = d.get("texts") or {}
    tt = next(t for t in pd["tracks"] if t["type"] == "text")
    tt["name"] = "Titles"
    hook = (texts.get("hook") or "").strip()
    from . import templates as TP
    first = clips[0]
    first_an = ans[first["mediaId"]]
    if hook:
        hs = style["hook"]
        end = slots[1]["start"] + slots[1]["dur"] if len(slots) > 1 and slots[0]["dur"] < 1.6 else slots[0]["start"] + slots[0]["dur"]
        spec = {"content": hook.upper() if hs["font"] == "Anton-Regular" else hook, "font": hs["font"], "size": hs["size"], "color": "#fff4e2",
                "uppercase": hs["font"] == "Anton-Regular", "maxWidth": 0, "lineHeight": 0.92, "letterSpacing": 2,
                "shadow": {"enabled": True, "color": "#000000", "opacity": 0.5, "blur": 16, "x": 0, "y": 6},
                "animIn": {"type": "pop" if hs["font"] == "Anton-Regular" else "fade", "duration": 0.25, "easing": "punch"},
                "animOut": {"type": "fade", "duration": 0.15, "easing": "out"}}
        S0 = [s for s in first_an["samples"] if first["inPoint"] <= s["t"] < first["outPoint"]]
        has_person = any(s["nfaces"] for s in S0) and first_an["type"] == "video"
        cl = {"id": "hook", "mediaId": None, "start": 0.05, "inPoint": 0, "outPoint": round(end - 0.1, 4), "transform": {**TR0, "y": -360},
              "keyframes": {}, "filters": [], "text": spec}
        from . import text as TX
        lay = TX.build(spec)
        if lay["w"] > 0.86 * W:  # a long title: shrink it to fit inside the frame's safe width
            cl["transform"]["scale"] = round(0.86 * W / lay["w"], 3)
        from .ai_models import ai_python
        if hs["behind"] and has_person and ai_python() is not None:
            first["cutout"] = {"enabled": True, "mode": "person"}
            spec["behindSubject"] = True
            # just above the main face, so the lower part of the letters tucks behind the hair / head
            sc = float(cl["transform"].get("scale", 1))
            th = lay["h"] * sc
            y = -430.0
            # the main face's highest point during the shot (same framing: source fractions = canvas)
            tops = []
            for smp in S0:
                fs = [f for f in (smp.get("faces") or []) if f["score"] > 0.75]
                if fs:
                    fb = max(fs, key=lambda f: f["size"])["box"]
                    tops.append((fb[1] - 0.45 * fb[3]) * H)  # the top of the head (hair above the face box)
            if tops and abs(first_an["width"] / max(1, first_an["height"]) - W / H) < 0.05:
                head = float(np.percentile(tops, 30))  # robust: a stray detection in the grass mustn't move it
                y = head + 0.3 * th - th / 2 - H / 2  # the bottom third of the letters tucks behind the head
            cl["transform"]["y"] = round(max(y, 236 + th / 2 - H / 2), 1)
            cl["behind"] = True
            report.append("The title sits behind the person in the first shot (Cutout).")
        elif hs["follow"] and has_person:
            tr = next((x for x in tracks_ if x["name"].endswith("(shot 1)") and x["kind"] == "face"), None) or \
                _subject_track(load_meta(first["mediaId"]), first_an, first["inPoint"], first["outPoint"], "Face (shot 1)")
            if tr and tr["kind"] == "face":
                if tr not in tracks_:
                    tracks_.append(tr)
                cl["follow"] = {"trackId": tr["id"], "clipId": first["id"], "position": True, "smoothing": 0.5, "safe": True}
                cl["transform"]["y"] = -330
                report.append("The title rides above the face in the first shot (tracked, kept inside the safe area).")
        if cl.pop("behind", False):
            pass  # overlapping the person is the point
        elif "follow" not in cl:
            c0 = Clip.model_validate(first).model_dump()
            fill = True
            boxes = TP._face_boxes(c0, load_meta(first["mediaId"]), 0.5, W, H, fill)
            tr2, moved = TP.place_text(spec, cl["transform"], boxes, W, H)
            cl["transform"] = tr2
            if moved:
                report.append(f"The title moved to y={tr2['y']:+.0f} so it doesn't cover a face.")
        tt["clips"].append(cl)
    outro = (texts.get("outro") or "").strip()
    if outro:
        last = slots[-1]
        spec = {"content": outro, "font": "DMSerifDisplay-Italic", "size": 90, "color": "#fff4e2", "maxWidth": 0,
                "shadow": {"enabled": True, "color": "#000000", "opacity": 0.5, "blur": 16, "x": 0, "y": 6},
                "animIn": {"type": "fade", "duration": 0.4, "easing": "out"}, "animOut": {"type": "fade", "duration": 0.3, "easing": "out"}}
        a = last["start"] + min(0.6, last["dur"] / 3)
        cl = {"id": "outro", "mediaId": None, "start": round(a, 4), "inPoint": 0, "outPoint": round(total - a - 0.3, 4), "transform": {**TR0, "y": -440},
              "keyframes": {}, "filters": [], "text": spec}
        tt["clips"].append(cl)
    # the picture fades out with the music on the last shot
    lastc = clips[-1]
    Dl = slots[-1]["dur"]
    lastc["keyframes"]["opacity"] = [{"time": 0, "value": 1, "easing": "linear"}, {"time": round(max(0, Dl - 0.6), 4), "value": 1, "easing": "in"},
                                     {"time": round(Dl, 4), "value": 0, "easing": "linear"}]
    pd["motionTracks"] = tracks_
    pd["media"] = list(dict.fromkeys([*metas.keys(), d["music"]]))
    pd["export"].update(fileName=name, preset="instagram", fps=30)
    p = Project.model_validate(pd)
    save_project(p)
    return p, report


def _build_from_template(d: dict, name: str, ctx=None) -> tuple[Project, list[str]]:
    from . import templates as TP
    from .templates_api import _load
    t = _load(d["template"])
    slots = d["slots"]
    slot_media = [s["pick"]["mediaId"] if s.get("pick") else None for s in slots]
    media = list(dict.fromkeys(m for m in slot_media if m))
    full = [m or media[0] for m in slot_media] + [media[0]] * (len(t["slots"]) - len(slot_media))
    p, report = TP.generate(t, media, d["music"], d["musicStart"], d.get("texts") or {}, name, full[:len(t["slots"])])
    main = next(x for x in p.tracks if x.type == "video")
    for s, c in zip(slots, sorted(main.clips, key=lambda c: c.start)):
        pk = s.get("pick")
        if not pk or c.mediaId != pk["mediaId"]:
            continue
        m = load_meta(c.mediaId)
        if m["type"] == "video":
            ln = c.outPoint - c.inPoint
            c.inPoint = round(min(pk["in"], max(0.0, float(m["duration"]) - ln - 0.05)), 4)
            c.outPoint = round(c.inPoint + ln, 4)
    save_project(p)
    report.insert(0, f"Template '{t['name']}': each slot got the best moment of the shot chosen for it.")
    return p, report


def summary_report(d: dict, extra: list[str]) -> list[str]:
    st = STYLES[d["style"]]
    out = [f"{st['label']} auto-edit: {len(d['slots'])} shots on the song from {d['musicStart']:.2f} s, the drop at {d['dropAt']:.2f} s, "
           f"{d['length']:.1f} s long; every cut on a beat. {d['shotsUsed']} different shots used out of {d['shotsAvailable']} usable."]
    for k, s in enumerate(d["slots"]):
        pk = s.get("pick")
        if pk:
            out.append(f"{s['start']:5.2f} s {s['section']:<9} {pk['name'][:28]} {pk['in']:.1f}-{pk['out']:.1f} s: {', '.join(pk['reasons'])}"
                       f"{' (locked)' if pk.get('locked') else ''}")
    return out + extra


# ---------------------------------------------------------------- API
class AnalyzeBody(BaseModel):
    media: list[str]
    force: bool = False


@router.post("/analyze")
def analyze(body: AnalyzeBody):
    metas = [m for m in (load_meta(x) for x in body.media) if m and m.get("type") in ("video", "image")]
    if not metas:
        raise HTTPException(400, "No videos or photos to analyse.")

    def run(ctx: JobContext) -> dict:
        t0 = time.time()
        total = sum(float(m.get("duration") or 1) for m in metas)
        done, out = 0.0, []
        for m in metas:
            w = float(m.get("duration") or 1) / total
            ctx.message(f"Analysing {m['name']}")
            a = AA.analyze(m, ctx, done, done + w, force=body.force)
            out.append({"mediaId": m["id"], "shots": len(a["shots"]), "unusable": len(a["unusable"]), "seconds": a["stats"]["seconds"]})
            done += w
        foot = sum(float(m.get("duration") or 0) for m in metas)
        el = time.time() - t0
        return {"media": out, "footage": round(foot, 1), "seconds": round(el, 2), "speed": round(foot / max(1e-3, el), 1),
                "duplicates": [[list(a), list(b), s] for a, b, s in AA.duplicates([AA.cached(m["id"]) for m in metas], NEAR_DUP)]}

    return {"jobId": jobs.submit("autoedit-analyze", f"Analysing {len(metas)} clip(s)", run, queue="media").id}


@router.get("/analysis/{mid}")
def get_analysis(mid: str):
    a = AA.cached(mid)
    if not a:
        raise HTTPException(404, "Not analysed yet.")
    return {k: v for k, v in a.items() if k != "samples"}


class DraftBody(BaseModel):
    media: list[str]
    music: str
    style: str = "energetic"
    template: str | None = None
    locks: dict | None = None
    exclude: list[str] | None = None
    musicStart: float | None = None
    length: float | None = None
    aspect: str = "9:16"
    texts: dict | None = None


@router.post("/draft")
def make_draft(body: DraftBody):
    try:
        return draft(body.media, body.music, body.style, template_id=body.template, locks=body.locks, exclude=body.exclude,
                     music_start=body.musicStart, length=body.length, aspect=body.aspect, texts=body.texts)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class CreateBody(BaseModel):
    draft: dict
    name: str = "Auto edit"


@router.post("/create")
def create(body: CreateBody):
    d = body.draft

    def run(ctx: JobContext) -> dict:
        p, extra = build_project(d, body.name, ctx)
        rep = summary_report(d, extra)
        return {"projectId": p.id, "report": rep}

    return {"jobId": jobs.submit("autoedit-create", f"Auto-edit: {body.name}", run, queue="render").id}
