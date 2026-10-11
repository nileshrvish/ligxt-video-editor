// Transition planning: port of backend/app/transitions.py (same rules, same shared test cases in
// shared/transition_cases.json). A transition is centred on the cut and uses footage beyond the
// clips' in/out points ("handles"), so the timeline length and cut positions never change.
import type { Clip, Project, Transition } from "../types";
import { clipEnd } from "../util";
import type { MediaDuration } from "./ops";

export const MIN_DURATION = 0.1;
export const MAX_DURATION = 2;
export const UI_MIN = 0.2;
const ADJACENT_EPS = 1e-3;

export const TRANSITION_GROUPS: { label: string; types: { id: string; label: string }[] }[] = [
  { label: "Basic", types: [
    { id: "fade", label: "Fade" }, { id: "dissolve", label: "Dissolve" },
    { id: "fadeblack", label: "Dip to black" }, { id: "fadewhite", label: "Dip to white" },
  ] },
  { label: "Whip pan", types: [
    { id: "whipleft", label: "Whip ←" }, { id: "whipright", label: "Whip →" },
    { id: "whipup", label: "Whip ↑" }, { id: "whipdown", label: "Whip ↓" },
  ] },
  { label: "Slide", types: [
    { id: "slideleft", label: "Slide ←" }, { id: "slideright", label: "Slide →" },
    { id: "slideup", label: "Slide ↑" }, { id: "slidedown", label: "Slide ↓" },
  ] },
  { label: "Wipe", types: [
    { id: "wipeleft", label: "Wipe ←" }, { id: "wiperight", label: "Wipe →" },
    { id: "wipeup", label: "Wipe ↑" }, { id: "wipedown", label: "Wipe ↓" },
  ] },
  { label: "Shape", types: [
    { id: "zoomin", label: "Zoom in" }, { id: "circleopen", label: "Circle open" },
    { id: "circleclose", label: "Circle close" }, { id: "pixelize", label: "Pixelize" },
    { id: "radial", label: "Radial" },
  ] },
];
export const TRANSITION_TYPES = TRANSITION_GROUPS.flatMap((g) => g.types);
export const transitionLabel = (id: string) => TRANSITION_TYPES.find((t) => t.id === id)?.label ?? id;

export interface TransitionPlan {
  id: string;
  type: string;
  fromId: string;
  toId: string;
  cut: number;
  half: number;
  requested: number;
  spareA: number; // Infinity for photos
  spareB: number;
  status: "ok" | "shortened" | "freeze";
  freezeA: boolean;
  freezeB: boolean;
  message: string;
}

type ClipLike = Pick<Clip, "id" | "start" | "inPoint" | "outPoint" | "speed">;

export function planTransition(
  tr: Pick<Transition, "id" | "type" | "duration">, a: ClipLike, b: ClipLike, srcA: number, srcB: number,
): TransitionPlan | null {
  const dur = (c: ClipLike) => Math.max(0, (c.outPoint - c.inPoint) / (c.speed || 1));
  if (Math.abs(a.start + dur(a) - b.start) > ADJACENT_EPS) return null;
  const req = Math.min(MAX_DURATION, Math.max(MIN_DURATION, Number(tr.duration) || 0.5));
  let h = req / 2;
  const reasons: string[] = [];
  const cap = 0.5 * Math.min(dur(a), dur(b));
  if (cap < h) {
    h = cap;
    reasons.push("a clip is too short for the full length");
  }
  const spareA = isFinite(srcA) ? Math.max(0, (srcA - a.outPoint) / (a.speed || 1)) : Infinity;
  const spareB = isFinite(srcB) ? Math.max(0, b.inPoint / (b.speed || 1)) : Infinity;
  const spare = Math.min(spareA, spareB);
  let freezeA = false;
  let freezeB = false;
  let status: TransitionPlan["status"];
  if (spare >= h - 1e-9) {
    status = reasons.length ? "shortened" : "ok";
  } else if (2 * spare >= MIN_DURATION) {
    h = spare;
    status = "shortened";
    const where = spareA <= spareB ? "after the left clip's out point" : "before the right clip's in point";
    reasons.push(`only ${spare.toFixed(2)} s of spare footage ${where}`);
  } else {
    status = "freeze";
    freezeA = spareA < h;
    freezeB = spareB < h;
    const sides = [freezeA && "the left clip's last frame", freezeB && "the right clip's first frame"].filter(Boolean);
    reasons.push("no spare footage, holding " + sides.join(" and "));
  }
  let message = status === "ok" ? "" : (status === "shortened" ? `Shortened to ${(2 * h).toFixed(2)} s: ` : "") + reasons.join("; ");
  if (message) message = message[0].toUpperCase() + message.slice(1) + ".";
  return {
    id: tr.id, type: tr.type, fromId: a.id, toId: b.id, cut: b.start, half: Math.round(h * 1e6) / 1e6, requested: req,
    spareA, spareB, status, freezeA, freezeB, message,
  };
}

/** Plans for every valid transition on the main video track, keyed by the outgoing clip id. */
export function mainTransitionPlans(p: Project, mediaDuration: MediaDuration): Map<string, TransitionPlan> {
  const out = new Map<string, TransitionPlan>();
  const main = p.tracks.find((t) => t.type === "video");
  if (!main) return out;
  const byId = new Map(main.clips.map((c) => [c.id, c]));
  for (const tr of p.transitions) {
    if (tr.trackId !== main.id) continue;
    const a = byId.get(tr.fromClipId);
    const b = byId.get(tr.toClipId);
    if (!a || !b || out.has(a.id)) continue;
    const plan = planTransition(tr, a, b, mediaDuration(a.mediaId), mediaDuration(b.mediaId));
    if (plan && plan.half > 1e-6) out.set(a.id, plan);
  }
  return out;
}

/** Adjacent clip pairs (cuts) on a track. */
export function cuts(clips: Clip[]): [Clip, Clip][] {
  const s = [...clips].sort((x, y) => x.start - y.start);
  const out: [Clip, Clip][] = [];
  for (let i = 0; i + 1 < s.length; i++) if (Math.abs(clipEnd(s[i]) - s[i + 1].start) < ADJACENT_EPS) out.push([s[i], s[i + 1]]);
  return out;
}

