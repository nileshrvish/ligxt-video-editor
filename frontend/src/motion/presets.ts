// Quick motion presets. They only produce keyframes (the renderers just play keyframes), so they
// stay fully editable on the timeline.
import type { Keyframe } from "../types";

type K = Keyframe;
const kf = (time: number, value: number, easing = "linear"): K => ({ time: +time.toFixed(4), value: +value.toFixed(5), easing });

export interface Focus { x: number; y: number } // canvas offset (px) of the point to keep still while zooming

/** Slow zoom (Ken Burns): eased, optionally towards a focal point so a face stays put. */
export function kenBurns(D: number, dir: "in" | "out", amount = 0.12, focus: Focus = { x: 0, y: 0 }) {
  const [s0, s1] = dir === "in" ? [1, 1 + amount] : [1 + amount, 1];
  // Zooming about the centre moves a point p to s*p; shifting by (1 - s)*p keeps it still.
  return {
    scale: [kf(0, s0, "inout"), kf(D, s1)],
    x: [kf(0, (1 - s0) * focus.x, "inout"), kf(D, (1 - s1) * focus.x)],
    y: [kf(0, (1 - s0) * focus.y, "inout"), kf(D, (1 - s1) * focus.y)],
  };
}

/** Pan across the picture. `picW/picH` = the picture's size on the canvas at scale 1. Zooms in just
 *  enough to have room to move, and never pans past the picture's edge. */
export function pan(D: number, dir: "left" | "right" | "up" | "down", W: number, H: number, picW: number, picH: number, zoom = 1.12) {
  const horiz = dir === "left" || dir === "right";
  const need = horiz ? (W * 1.06) / picW : (H * 1.06) / picH;
  const z = Math.max(zoom, need);
  const margin = Math.max(0, horiz ? (z * picW - W) / 2 : (z * picH - H) / 2);
  const a = 0.8 * margin;
  const [from, to] = dir === "left" || dir === "up" ? [a, -a] : [-a, a]; // content moves in `dir`
  const move = [kf(0, from, "inout"), kf(D, to)];
  return { scale: [kf(0, z, "linear"), kf(D, z)], x: horiz ? move : [], y: horiz ? [] : move };
}

const without = (ks: K[] | undefined, a: number, b: number) => (ks ?? []).filter((k) => k.time < a - 1e-6 || k.time > b + 1e-6);
const merge = (base: K[], add: K[]) => [...base, ...add].sort((x, y) => x.time - y.time);

/** A snappy scale-up on a beat at clip time `t`, settling back (on the punch channel). */
export function punchIn(existing: K[] | undefined, t: number, amount = 0.1, attack = 0.1, settle = 0.3) {
  const add = [kf(t, 1, "punch"), kf(t + attack, 1 + amount, "out"), kf(t + attack + settle, 1)];
  return merge(without(existing, t, t + attack + settle), add);
}

function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Camera shake starting at clip time `t`: per-frame offsets that decay to rest. Deterministic. */
export function shake(existing: { x?: K[]; y?: K[]; r?: K[] }, t: number, amountPx = 14, duration = 0.35, fps = 30, seed = 7) {
  const n = Math.max(2, Math.round(duration * fps));
  const r = rng(seed + Math.round(t * 1000));
  const xs: K[] = [], ys: K[] = [], rs: K[] = [];
  for (let k = 0; k <= n; k++) {
    const decay = k === n ? 0 : (1 - k / n) ** 1.5;
    const tt = t + k / fps;
    xs.push(kf(tt, k === 0 ? 0 : (r() * 2 - 1) * amountPx * decay));
    ys.push(kf(tt, k === 0 ? 0 : (r() * 2 - 1) * amountPx * 0.8 * decay));
    rs.push(kf(tt, k === 0 ? 0 : (r() * 2 - 1) * (amountPx / 14) * 0.8 * decay));
  }
  const end = t + n / fps;
  return {
    shakeX: merge(without(existing.x, t, end), xs),
    shakeY: merge(without(existing.y, t, end), ys),
    shakeR: merge(without(existing.r, t, end), rs),
  };
}
