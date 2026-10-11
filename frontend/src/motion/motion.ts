// Keyframes, easing, layer transforms, speed/time mapping and ducking: a port of backend/app/motion.py
// (checked against shared/motion_cases.json). Keyframe times are seconds from the clip's start.
import type { Clip, Keyframe } from "../types";

export const EASINGS = ["linear", "in", "out", "inout", "punch"] as const;
export type Easing = (typeof EASINGS)[number];
export const EASING_LABELS: Record<Easing, string> = { linear: "Linear", in: "Ease in", out: "Ease out", inout: "Ease in-out", punch: "Punch" };
export const RAMP_SEGMENTS = 16;
export const REVERSE_EPS = 0.001; // reverse starts just before the out point (which is exclusive)
// punch multiplies the scale and shakeX/Y/R add offsets, so beat punches and camera shake layer on
// top of the clip's own (e.g. Ken Burns) movement without touching its keyframes.
export const ANIMATED = ["x", "y", "scale", "rotation", "opacity", "punch", "shakeX", "shakeY", "shakeR"] as const;
export type Prop = (typeof ANIMATED)[number] | "intensity" | "volume";

export function ease(name: string, u: number): number {
  u = Math.min(1, Math.max(0, u));
  switch (name) {
    case "in": return u ** 3;
    case "out": return 1 - (1 - u) ** 3;
    case "inout": return u < 0.5 ? 4 * u ** 3 : 1 - (-2 * u + 2) ** 3 / 2;
    case "punch": return 1 + 3.2 * (u - 1) ** 3 + 2.2 * (u - 1) ** 2;
    default: return u;
  }
}

const sorted = (k?: Keyframe[]) => [...(k ?? [])].sort((a, b) => a.time - b.time);

export function valueAt(kfs: Keyframe[] | undefined, t: number, dflt: number): number {
  const ks = sorted(kfs);
  if (!ks.length) return dflt;
  if (t <= ks[0].time) return ks[0].value;
  if (t >= ks[ks.length - 1].time) return ks[ks.length - 1].value;
  for (let i = 0; i + 1 < ks.length; i++) {
    const a = ks[i], b = ks[i + 1];
    if (a.time <= t && t < b.time) {
      const span = b.time - a.time;
      const u = span > 1e-9 ? (t - a.time) / span : 1;
      return a.value + (b.value - a.value) * ease(a.easing ?? "linear", u);
    }
  }
  return ks[ks.length - 1].value;
}

export interface TransformValues { x: number; y: number; scale: number; rotation: number; opacity: number; flipH: boolean; flipV: boolean }

export function transformAt(c: Clip, tau: number): TransformValues {
  const tr = c.transform;
  const kf = c.keyframes ?? {};
  return {
    x: valueAt(kf.x, tau, tr?.x ?? 0) + valueAt(kf.shakeX, tau, 0),
    y: valueAt(kf.y, tau, tr?.y ?? 0) + valueAt(kf.shakeY, tau, 0),
    scale: valueAt(kf.scale, tau, tr?.scale ?? 1) * valueAt(kf.punch, tau, 1),
    rotation: valueAt(kf.rotation, tau, tr?.rotation ?? 0) + valueAt(kf.shakeR, tau, 0),
    opacity: valueAt(kf.opacity, tau, tr?.opacity ?? 1),
    flipH: !!tr?.flipH, flipV: !!tr?.flipV,
  };
}

export function hasMotion(c: Clip): boolean {
  const tr = c.transform;
  const kf = c.keyframes ?? {};
  const crop = tr?.crop ?? { left: 0, top: 0, right: 0, bottom: 0 };
  return ANIMATED.some((p) => (kf[p]?.length ?? 0) > 0)
    || Math.abs(tr?.x ?? 0) > 1e-6 || Math.abs(tr?.y ?? 0) > 1e-6 || Math.abs((tr?.scale ?? 1) - 1) > 1e-6
    || Math.abs(tr?.rotation ?? 0) > 1e-6 || (tr?.opacity ?? 1) < 1 - 1e-6 || !!tr?.flipH || !!tr?.flipV
    || crop.left > 1e-6 || crop.top > 1e-6 || crop.right > 1e-6 || crop.bottom > 1e-6;
}

export type Rect = [number, number, number, number];
export type Mat = [number, number, number, number, number, number];

export function sourceRect(srcW: number, srcH: number, crop: { left: number; top: number; right: number; bottom: number } | undefined, fill: boolean, W: number, H: number): Rect {
  const c = crop ?? { left: 0, top: 0, right: 0, bottom: 0 };
  let x0 = c.left * srcW, y0 = c.top * srcH;
  let cw = Math.max(1, srcW * (1 - c.left - c.right));
  let ch = Math.max(1, srcH * (1 - c.top - c.bottom));
  if (fill) {
    if (cw / ch > W / H) { const nw = (ch * W) / H; x0 += (cw - nw) / 2; cw = nw; }
    else { const nh = (cw * H) / W; y0 += (ch - nh) / 2; ch = nh; }
  }
  return [x0, y0, cw, ch];
}

/** Source pixel -> canvas affine matrix (setTransform order) and the shown source rect. */
export function layerMatrix(srcW: number, srcH: number, crop: Clip["transform"]["crop"] | undefined, fill: boolean, W: number, H: number, t: TransformValues): { m: Mat; rect: Rect } {
  const rect = sourceRect(srcW, srcH, crop, fill, W, H);
  return { m: rectMatrix(rect, W, H, t), rect };
}

/** Round half up, like backend/app/layers.py rnd(). */
export const rnd = (v: number) => Math.floor(v + 0.5);

/** The shown source rect in whole pixels (what the export crops: backend layers.rounded_rect). */
export function roundedRect(srcW: number, srcH: number, crop: Clip["transform"]["crop"] | undefined, fill: boolean, W: number, H: number): Rect {
  const [x0, y0, cw, ch] = sourceRect(srcW, srcH, crop, fill, W, H);
  return [rnd(x0), rnd(y0), Math.max(2, rnd(cw)), Math.max(2, rnd(ch))];
}

/** The layer matrix for an already chosen source rect. */
export function rectMatrix(rect: Rect, W: number, H: number, t: TransformValues): Mat {
  const [x0, y0, cw, ch] = rect;
  const f = Math.min(W / cw, H / ch);
  const s = t.scale * f;
  const fx = t.flipH ? -1 : 1, fy = t.flipV ? -1 : 1;
  const th = (t.rotation * Math.PI) / 180;
  const co = Math.cos(th), si = Math.sin(th);
  const a = s * fx * co, b = s * fx * si, c = -s * fy * si, d = s * fy * co;
  const cx = x0 + cw / 2, cy = y0 + ch / 2;
  const Cx = W / 2 + t.x, Cy = H / 2 + t.y;
  return [a, b, c, d, Cx - (a * cx + c * cy), Cy - (b * cx + d * cy)];
}

export function invert(m: Mat): Mat {
  const [a, b, c, d, e, f] = m;
  const det = a * d - b * c;
  return [d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det];
}

export const apply = (m: Mat, x: number, y: number): [number, number] => [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]];

// ---------------------------------------------------------------- speed
export interface RampPoint { u: number; speed: number }

export function rampPoints(c: Clip): RampPoint[] | null {
  const r = c.speedRamp as { points?: RampPoint[] } | null | undefined;
  if (!r?.points || r.points.length < 2) return null;
  const pts = r.points
    .map((p) => ({ u: Math.min(1, Math.max(0, Number(p.u))), speed: Math.min(4, Math.max(0.1, Number(p.speed))) }))
    .sort((a, b) => a.u - b.u);
  if (pts[0].u > 0) pts.unshift({ u: 0, speed: pts[0].speed });
  if (pts[pts.length - 1].u < 1) pts.push({ u: 1, speed: pts[pts.length - 1].speed });
  return pts;
}

function speedIntegral(pts: RampPoint[], a: number, b: number): number {
  let total = 0;
  for (let i = 0; i + 1 < pts.length; i++) {
    const p = pts[i], q = pts[i + 1];
    const lo = Math.max(a, p.u), hi = Math.min(b, q.u);
    if (hi <= lo) continue;
    const span = q.u - p.u;
    const sp = (u: number) => p.speed + (q.speed - p.speed) * (span > 1e-12 ? (u - p.u) / span : 0);
    total += ((sp(lo) + sp(hi)) / 2) * (hi - lo);
  }
  return total;
}

export function clipDuration(c: Clip): number {
  const src = Math.max(0, c.outPoint - c.inPoint);
  const pts = rampPoints(c);
  if (pts && c.freezeFrame == null) return src / Math.max(1e-6, speedIntegral(pts, 0, 1));
  return src / (c.speed || 1);
}

export interface RampSeg { t0: number; t1: number; src0: number; speed: number }

export function rampSegments(c: Clip): RampSeg[] {
  const pts = rampPoints(c);
  const D = clipDuration(c);
  if (!pts) return [{ t0: 0, t1: D, src0: c.inPoint, speed: c.speed || 1 }];
  const out: RampSeg[] = [];
  let src = c.inPoint;
  const n = RAMP_SEGMENTS;
  for (let k = 0; k < n; k++) {
    const u0 = k / n, u1 = (k + 1) / n;
    const sp = speedIntegral(pts, u0, u1) / (u1 - u0);
    out.push({ t0: D * u0, t1: D * u1, src0: src, speed: sp });
    src += sp * D * (u1 - u0);
  }
  return out;
}

/** Source time shown at clip-relative time tau, and the speed there. */
export function srcTime(c: Clip, tau: number): { src: number; speed: number } {
  if (c.freezeFrame != null) return { src: c.freezeFrame, speed: 0 };
  const segs = rampSegments(c);
  let seg = segs[segs.length - 1];
  for (const s of segs) if (tau < s.t1) { seg = s; break; }
  let src = seg.src0 + (tau - seg.t0) * seg.speed;
  // Mirror, starting on the last frame before the (exclusive) out point.
  if (c.reverse) src = c.inPoint + c.outPoint - src - REVERSE_EPS;
  return { src, speed: c.reverse ? -seg.speed : seg.speed };
}

export const RAMP_PRESETS: { id: string; label: string; points: RampPoint[] }[] = [
  { id: "slowdrop", label: "Slow-mo into the drop", points: [{ u: 0, speed: 1 }, { u: 0.55, speed: 1 }, { u: 1, speed: 0.3 }] },
  { id: "speedout", label: "Speed up out", points: [{ u: 0, speed: 0.6 }, { u: 0.5, speed: 1 }, { u: 1, speed: 3 }] },
  { id: "bullet", label: "Fast, slow-mo, fast", points: [{ u: 0, speed: 2.5 }, { u: 0.4, speed: 0.3 }, { u: 0.6, speed: 0.3 }, { u: 1, speed: 2.5 }] },
  { id: "rampin", label: "Ramp in (slow to fast)", points: [{ u: 0, speed: 0.4 }, { u: 1, speed: 2.5 }] },
];

// ---------------------------------------------------------------- ducking
export function duckRegions(sources: [number, number, number[]][], threshold = 0.08, mergeGap = 0.35, minLen = 0.15): [number, number][] {
  const marks: [number, number][] = [];
  for (const [start, , peaks] of sources) {
    let on: number | null = null;
    peaks.forEach((v, i) => {
      const t = start + i / 100;
      if (v > threshold && on === null) on = t;
      else if (v <= threshold && on !== null) { marks.push([on, t]); on = null; }
    });
    if (on !== null) marks.push([on, start + peaks.length / 100]);
  }
  marks.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged: [number, number][] = [];
  for (const [a, b] of marks) {
    if (merged.length && a - merged[merged.length - 1][1] <= mergeGap) merged[merged.length - 1][1] = Math.max(merged[merged.length - 1][1], b);
    else merged.push([a, b]);
  }
  return merged.filter(([a, b]) => b - a >= minLen);
}

export function duckGain(regions: [number, number][], t: number, amountDb = -12, attack = 0.15, release = 0.45): number {
  const level = 10 ** (amountDb / 20);
  let g = 1;
  for (const [a, b] of regions) {
    let w = 0;
    if (a <= t && t <= b) w = 1;
    else if (a - attack < t && t < a) w = (t - (a - attack)) / attack;
    else if (b < t && t < b + release) w = 1 - (t - b) / release;
    g = Math.min(g, 1 - (1 - level) * w);
  }
  return g;
}
