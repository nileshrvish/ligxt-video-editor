"""Transition planning (shared contract with frontend/src/timeline/transitions.ts).

A transition between adjacent clips A -> B is centred on the cut T = B.start and spans
[T - h, T + h] where h = duration / 2. During it, A keeps playing past its out point and B starts
before its in point ("handles"), so the timeline length and every cut position stay unchanged.

Rules (both sides must agree; see shared/transition_cases.json):
1. Requested duration is clamped to 0.1..2 s.
2. h is capped at half of the shorter clip, so two transitions on one clip never overlap.
3. Spare footage: A has (source duration - A.outPoint) / A.speed after its out point,
   B has B.inPoint / B.speed before its in point. Photos have unlimited spare footage.
4. If both sides have >= h spare: full transition.
   Else if 2 * min(spare) >= 0.1 s: shorten to h = min(spare)                ("shortened").
   Else: keep h and hold the edge frame of the side(s) without footage          ("freeze").
"""
from __future__ import annotations

import math
from dataclasses import dataclass

MIN_DURATION = 0.1
MAX_DURATION = 2.0
ADJACENT_EPS = 1e-3

XFADE_TYPES = {
    "fade", "dissolve", "fadeblack", "fadewhite",
    "slideleft", "slideright", "slideup", "slidedown",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "zoomin", "circleopen", "circleclose", "pixelize", "radial",
}
WHIP_TYPES = {"whipleft", "whipright", "whipup", "whipdown"}
ALL_TYPES = XFADE_TYPES | WHIP_TYPES


@dataclass
class TransitionPlan:
    id: str
    type: str
    from_id: str
    to_id: str
    cut: float          # T
    half: float         # effective h
    requested: float    # requested duration (after clamping)
    spare_a: float      # spare footage after A (inf for photos)
    spare_b: float      # spare footage before B
    status: str         # "ok" | "shortened" | "freeze"
    freeze_a: bool
    freeze_b: bool
    message: str

    @property
    def start(self) -> float:
        return self.cut - self.half

    @property
    def end(self) -> float:
        return self.cut + self.half

    def to_dict(self) -> dict:
        inf = lambda v: None if math.isinf(v) else round(v, 4)  # noqa: E731
        return {"id": self.id, "type": self.type, "cut": round(self.cut, 4), "half": round(self.half, 4),
                "duration": round(2 * self.half, 4), "requested": self.requested,
                "spareA": inf(self.spare_a), "spareB": inf(self.spare_b), "status": self.status,
                "freezeA": self.freeze_a, "freezeB": self.freeze_b, "message": self.message}


def _dur(c: dict) -> float:
    return max(0.0, (c["outPoint"] - c["inPoint"]) / (c.get("speed") or 1))


def plan_transition(tr: dict, a: dict, b: dict, source_dur_a: float, source_dur_b: float) -> TransitionPlan | None:
    """`a`/`b` are clip dicts; source durations are math.inf for photos. None if not adjacent."""
    a_end = a["start"] + _dur(a)
    if abs(a_end - b["start"]) > ADJACENT_EPS:
        return None
    req = min(MAX_DURATION, max(MIN_DURATION, float(tr.get("duration") or 0.5)))
    h = req / 2
    reasons = []
    cap = 0.5 * min(_dur(a), _dur(b))
    if cap < h:
        h = cap
        reasons.append("a clip is too short for the full length")
    spare_a = math.inf if math.isinf(source_dur_a) else max(0.0, (source_dur_a - a["outPoint"]) / (a.get("speed") or 1))
    spare_b = math.inf if math.isinf(source_dur_b) else max(0.0, b["inPoint"] / (b.get("speed") or 1))
    spare = min(spare_a, spare_b)
    freeze_a = freeze_b = False
    if spare >= h - 1e-9:
        status = "shortened" if reasons else "ok"
    elif 2 * spare >= MIN_DURATION:
        h = spare
        status = "shortened"
        side = "left" if spare_a <= spare_b else "right"
        where = "after the left clip's out point" if side == "left" else "before the right clip's in point"
        reasons.append(f"only {spare:.2f} s of spare footage {where}")
    else:
        status = "freeze"
        freeze_a, freeze_b = spare_a < h, spare_b < h
        sides = []
        if freeze_a:
            sides.append("the left clip's last frame")
        if freeze_b:
            sides.append("the right clip's first frame")
        reasons.append("no spare footage, holding " + " and ".join(sides))
    msg = "" if status == "ok" else (f"Shortened to {2 * h:.2f} s: " if status == "shortened" else "") + "; ".join(reasons)
    if msg:
        msg = msg[0].upper() + msg[1:] + "."
    return TransitionPlan(tr.get("id", ""), tr.get("type", "fade"), a["id"], b["id"], b["start"], round(h, 6), req,
                          spare_a, spare_b, status, freeze_a, freeze_b, msg)


def ease_in_out_cubic(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return 4 * u * u * u if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2
