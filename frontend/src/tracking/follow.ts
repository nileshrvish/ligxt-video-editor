// Motion-tracking maths, a line-by-line port of backend/app/follow.py (checked by shared/follow_cases.json).
// Tracks live in source frames (project.motionTracks); clips that follow one, tracked regions and
// auto-reframed clips get per-frame channels (AutoMotion / AutoRegion) sampled at tau = i / fps.
import { apply, clipDuration, sourceRect, srcTime, transformAt, type Mat, type TransformValues } from "../motion/motion";
import type { Clip, MediaMeta, Project } from "../types";
import type { AutoMotion, AutoRegion, MotionTrack } from "./types";

export const LOST = 0;
export const LOW_CONF = 0.5;
export const HELD_WEIGHT = 0.02;
export const SAFE = { top: 220 / 1920, bottom: 420 / 1920, right: 140 / 1080, rightFrom: 0.42, left: 0.04 };
export const THIRDS: Record<string, number> = { left: 1 / 3, center: 0.5, right: 2 / 3 };

export interface TrackSample { x: number; y: number; w: number; h: number; a: number; c: number }

const mod = (a: number, n: number) => ((a % n) + n) % n;

export function trackAt(track: MotionTrack, s: number, interp = true): TrackSample | null {
  const data = track.data ?? [];
  const n = data.length;
  if (!n) return null;
  let f = s * track.fps - (track.k0 ?? 0);
  if (!interp) f = Math.floor(f + 1e-6);
  if (f <= 0 || f >= n - 1) {
    const e = f <= 0 ? data[0] : data[n - 1];
    const out = { x: e[0], y: e[1], w: e[2], h: e[3], a: e[4], c: e[5] };
    if (f < -0.5 || f > n - 0.5) out.c = e[5] * 0.5;
    return out;
  }
  const i = Math.floor(f);
  const u = f - i;
  const a = data[i], b = data[i + 1];
  const da = mod(b[4] - a[4] + 180, 360) - 180;
  return { x: a[0] + (b[0] - a[0]) * u, y: a[1] + (b[1] - a[1]) * u, w: a[2] + (b[2] - a[2]) * u, h: a[3] + (b[3] - a[3]) * u, a: a[4] + da * u, c: Math.min(a[5], b[5]) };
}

export const weight = (conf: number) => (conf <= LOST ? HELD_WEIGHT : Math.max(HELD_WEIGHT, Math.min(1, conf)));

export function lam(amount: number, fps: number): number {
  const a = Math.min(1, Math.max(0, amount));
  const period = 0.05 + 2 * a * a;
  return ((period * fps) / (2 * Math.PI)) ** 4;
}

/** Whittaker smoother (banded LDL^T), see follow.py smooth(). */
export function smooth(y: number[], w: number[], lmb: number): number[] {
  const n = y.length;
  if (n < 3 || lmb <= 1e-12) return y.slice();
  const A0 = w.map(Number), A1 = new Array(n).fill(0), A2 = new Array(n).fill(0);
  for (let r = 0; r < n - 2; r++) {
    A0[r] += lmb; A0[r + 1] += 4 * lmb; A0[r + 2] += lmb;
    A1[r] += -2 * lmb; A1[r + 1] += -2 * lmb;
    A2[r] += lmb;
  }
  const d = new Array(n).fill(0), l1 = new Array(n).fill(0), l2 = new Array(n).fill(0);
  for (let i = 0; i < n; i++) {
    if (i >= 2) l2[i] = A2[i - 2] / d[i - 2];
    if (i >= 1) l1[i] = (A1[i - 1] - (i >= 2 ? l2[i] * d[i - 2] * l1[i - 1] : 0)) / d[i - 1];
    d[i] = A0[i] - (i >= 1 ? l1[i] ** 2 * d[i - 1] : 0) - (i >= 2 ? l2[i] ** 2 * d[i - 2] : 0);
  }
  const u = new Array(n).fill(0);
  for (let i = 0; i < n; i++) u[i] = w[i] * y[i] - (i >= 1 ? l1[i] * u[i - 1] : 0) - (i >= 2 ? l2[i] * u[i - 2] : 0);
  const z = new Array(n).fill(0);
  for (let i = n - 1; i >= 0; i--) z[i] = u[i] / d[i] - (i + 1 < n ? l1[i + 1] * z[i + 1] : 0) - (i + 2 < n ? l2[i + 2] * z[i + 2] : 0);
  return z;
}

export function unwrap(a: number[]): number[] {
  const out = a.slice(0, 1);
  for (const v of a.slice(1)) out.push(out[out.length - 1] + mod(v - out[out.length - 1] + 180, 360) - 180);
  return out;
}

export function rectMatrixT(rect: [number, number, number, number], W: number, H: number, t: TransformValues): Mat {
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

export function srcDims(meta: MediaMeta, W: number, H: number): [number, number] {
  let sw = meta.width || W, sh = meta.height || H;
  const rot = (meta as { rotation?: number }).rotation;
  if (rot === 90 || rot === -90 || rot === 270 || rot === -270) [sw, sh] = [sh, sw];
  return [sw, sh];
}

export function hostMatrix(host: Clip, meta: MediaMeta, isMain: boolean, defaultFit: string, W: number, H: number, tau: number): { m: Mat; sw: number; sh: number } {
  const [sw, sh] = srcDims(meta, W, H);
  const fill = (host.fit ?? (isMain ? defaultFit : "fit")) === "fill";
  const [x0, y0, cw, ch] = sourceRect(sw, sh, host.transform?.crop, fill, W, H);
  const rect: [number, number, number, number] = [Math.floor(x0 + 0.5), Math.floor(y0 + 0.5), Math.max(2, Math.floor(cw + 0.5)), Math.max(2, Math.floor(ch + 0.5))];
  return { m: rectMatrixT(rect, W, H, transformAt(host, tau)), sw, sh };
}

export const nSamples = (D: number, fps: number) => Math.max(2, Math.ceil(D * fps - 1e-9) + 1);

type Host = [Clip, boolean];
export interface Ctx {
  W: number; H: number; fps: number; metas: Record<string, MediaMeta>;
  visual: Host[]; clips: Map<string, Host>; defaultFit: string;
  interp: (c: Clip, m: MediaMeta) => boolean;
}

const covers = (c: Clip, t: number) => c.start - 1e-9 <= t && t < c.start + clipDuration(c);

export function hostsFor(ctx: Ctx, track: MotionTrack, follow: { clipId?: string }, t: number): Host | null {
  const named = ctx.clips.get(follow.clipId ?? "");
  const cands = ctx.visual.filter((h) => h[0].mediaId === track.mediaId);
  if (named && named[0].mediaId === track.mediaId && covers(named[0], t)) return named;
  for (const h of cands) if (covers(h[0], t)) return h;
  if (named && named[0].mediaId === track.mediaId) return named;
  if (!cands.length) return null;
  const dist = (h: Host) => Math.min(Math.abs(t - h[0].start), Math.abs(t - h[0].start - clipDuration(h[0])));
  return cands.reduce((a, b) => (dist(b) < dist(a) ? b : a));
}

export interface Target { x: number[]; y: number[]; s: number[]; bw: number[]; bh: number[]; a: number[]; w: number[] }

export function targetSeries(ctx: Ctx, clip: Clip, track: MotionTrack, follow: { clipId?: string }, fps: number): Target | null {
  const D = clipDuration(clip);
  const n = nSamples(D, fps);
  const T: Target = { x: [], y: [], s: [], bw: [], bh: [], a: [], w: [] };
  const angs: number[] = [];
  for (let i = 0; i < n; i++) {
    const tau = Math.min(i / fps, Math.max(0, D - 1e-6));
    const t = clip.start + tau;
    const h = hostsFor(ctx, track, follow, t);
    if (!h) return null;
    const [host, isMain] = h;
    const meta = ctx.metas[host.mediaId ?? ""];
    if (!meta) return null;
    const Dh = clipDuration(host);
    const th = Math.min(Math.max(0, t - host.start), Math.max(0, Dh - 1e-6));
    const inside = covers(host, t);
    const s = srcTime(host, th).src;
    const e = trackAt(track, s, ctx.interp(host, meta));
    if (!e) return null;
    const { m, sw, sh } = hostMatrix(host, meta, isMain, ctx.defaultFit, ctx.W, ctx.H, th);
    const [px, py] = apply(m, e.x * sw, e.y * sh);
    const k = Math.sqrt(Math.abs(m[0] * m[3] - m[1] * m[2]));
    T.x.push(px);
    T.y.push(py);
    T.s.push(Math.sqrt(Math.max(1e-9, e.w * sw * e.h * sh)) * k);
    T.bw.push(e.w * sw * k);
    T.bh.push(e.h * sh * k);
    angs.push(e.a + (Math.atan2(m[1], m[0]) * 180) / Math.PI);
    T.w.push(inside ? weight(e.c) : HELD_WEIGHT);
  }
  T.a = unwrap(angs);
  return T;
}

export function safeRect(W: number, H: number, yCenter: number, itemH: number): [number, number, number, number] {
  const x0 = SAFE.left * W, y0 = SAFE.top * H, y1 = H - SAFE.bottom * H;
  let x1 = W - SAFE.left * W;
  if (yCenter + itemH / 2 > SAFE.rightFrom * H) x1 = W - SAFE.right * W;
  return [x0, y0, x1, y1];
}

const noAuto = (c: Clip): Clip => ({ ...c, autoMotion: null });

export function followMotion(ctx: Ctx, clip: Clip, track: MotionTrack, item: [number, number] | null): AutoMotion | null {
  const fo = (clip.follow ?? {}) as NonNullable<Clip["follow"]>;
  const fps = ctx.fps;
  const tg = targetSeries(ctx, clip, track, fo, fps);
  if (!tg) return null;
  const lmb = lam(fo.smoothing ?? 0.3, fps);
  const w = tg.w;
  const X = smooth(tg.x, w, lmb), Y = smooth(tg.y, w, lmb), S = smooth(tg.s, w, lmb), A = smooth(tg.a, w, lmb);
  let ref = w.findIndex((v) => v > HELD_WEIGHT);
  if (ref < 0) ref = 0;
  const sRef = Math.max(1e-6, S[ref]), aRef = A[ref];
  const pos = fo.position !== false, scl = !!fo.scale, rot = !!fo.rotation;
  const { W, H } = ctx;
  const out: AutoMotion = { fps, x: [], y: [], s: [], r: [] };
  const base0 = noAuto(clip);
  for (let i = 0; i < X.length; i++) {
    const ds = scl ? S[i] / sRef : 1;
    const dr = rot ? A[i] - aRef : 0;
    const base = transformAt(base0, i / fps);
    const ox = base.x, oy = base.y;
    const th = (dr * Math.PI) / 180;
    const rx = (ox * Math.cos(th) - oy * Math.sin(th)) * ds;
    const ry = (ox * Math.sin(th) + oy * Math.cos(th)) * ds;
    let dx: number, dy: number;
    if (pos) { dx = X[i] - W / 2 + rx - ox; dy = Y[i] - H / 2 + ry - oy; } else { dx = rx - ox; dy = ry - oy; }
    if (fo.safe && item) {
      const sc = base.scale * ds;
      const hw = (item[0] * sc) / 2, hh = (item[1] * sc) / 2;
      const cx = W / 2 + ox + dx, cy = H / 2 + oy + dy;
      const [x0, y0, x1, y1] = safeRect(W, H, cy, hh * 2);
      const cx2 = x1 - x0 > 2 * hw ? Math.min(Math.max(cx, x0 + hw), x1 - hw) : (x0 + x1) / 2;
      const cy2 = y1 - y0 > 2 * hh ? Math.min(Math.max(cy, y0 + hh), y1 - hh) : (y0 + y1) / 2;
      dx += cx2 - cx;
      dy += cy2 - cy;
    }
    out.x!.push(dx);
    out.y!.push(dy);
    out.s!.push(ds);
    out.r!.push(dr);
  }
  return out;
}

export function regionSeries(ctx: Ctx, clip: Clip, track: MotionTrack): AutoRegion | null {
  const fo = (clip.follow ?? {}) as NonNullable<Clip["follow"]>;
  const reg = clip.region ?? {};
  const fps = ctx.fps;
  const tg = targetSeries(ctx, clip, track, fo, fps);
  if (!tg) return null;
  const lmb = lam(fo.smoothing ?? 0.15, fps);
  const w = tg.w;
  const pad = 1 + 2 * Math.max(0, reg.padding ?? 0.25);
  const X = smooth(tg.x, w, lmb), Y = smooth(tg.y, w, lmb), BW = smooth(tg.bw, w, lmb), BH = smooth(tg.bh, w, lmb);
  const base0 = noAuto(clip);
  const base = X.map((_, i) => transformAt(base0, i / fps));
  return {
    fps,
    x: X.map((v, i) => v + base[i].x), y: Y.map((v, i) => v + base[i].y),
    w: BW.map((v, i) => Math.max(4, v * pad * base[i].scale)), h: BH.map((v, i) => Math.max(4, v * pad * base[i].scale)),
  };
}

export function reframeMotion(ctx: Ctx, clip: Clip, track: MotionTrack, isMain: boolean): AutoMotion | null {
  const rf = clip.reframe ?? {};
  const fps = ctx.fps;
  const meta = ctx.metas[clip.mediaId ?? ""];
  if (!meta) return null;
  const { W, H } = ctx;
  const D = clipDuration(clip);
  const n = nSamples(D, fps);
  const baseClip = noAuto(clip);
  const tx: number[] = [], ty: number[] = [], w: number[] = [], loX: number[] = [], hiX: number[] = [], loY: number[] = [], hiY: number[] = [];
  for (let i = 0; i < n; i++) {
    const tau = Math.min(i / fps, Math.max(0, D - 1e-6));
    const s = srcTime(clip, tau).src;
    const e = trackAt(track, s, ctx.interp(clip, meta));
    if (!e) return null;
    const { m, sw, sh } = hostMatrix(baseClip, meta, isMain, ctx.defaultFit, W, H, tau);
    const [px, py] = apply(m, e.x * sw, e.y * sh);
    tx.push(W * (THIRDS[rf.third ?? "center"] ?? 0.5) - px);
    ty.push(H * 0.5 - py);
    w.push(weight(e.c));
    const cs = [apply(m, 0, 0), apply(m, sw, 0), apply(m, 0, sh), apply(m, sw, sh)];
    loX.push(W - Math.max(...cs.map((p) => p[0])));
    hiX.push(-Math.min(...cs.map((p) => p[0])));
    loY.push(H - Math.max(...cs.map((p) => p[1])));
    hiY.push(-Math.min(...cs.map((p) => p[1])));
  }
  const lmb = lam(rf.smoothing ?? 0.6, fps);
  const clamp = (v: number, lo: number, hi: number) => (lo <= hi ? Math.min(Math.max(v, lo), hi) : (lo + hi) / 2);
  const X = smooth(tx.map((v, i) => clamp(v, loX[i], hiX[i])), w, lmb);
  const Y = rf.vertical ? smooth(ty.map((v, i) => clamp(v, loY[i], hiY[i])), w, lmb) : new Array(n).fill(0);
  return { fps, x: X.map((v, i) => clamp(v, loX[i], hiX[i])), y: Y.map((v, i) => clamp(v, loY[i], hiY[i])), s: null, r: null, wholeSource: true };
}

export interface Resolved { autoMotion?: AutoMotion; autoRegion?: AutoRegion }

export function resolve(project: Project, metas: Record<string, MediaMeta>, fps: number,
  itemSize?: (c: Clip) => [number, number] | null, interp?: (c: Clip, m: MediaMeta) => boolean): Map<string, Resolved> {
  const st = project.settings;
  const W = st.width || 1080, H = st.height || 1920;
  const tracks = new Map((project.motionTracks ?? []).filter((t) => t.id).map((t) => [t.id, t] as const));
  const visual: Host[] = [];
  const clips = new Map<string, Host>();
  for (const t of project.tracks) {
    if (t.type !== "video" && t.type !== "overlay") continue;
    for (const c of t.clips) {
      const ty = metas[c.mediaId ?? ""]?.type;
      if (c.mediaId && (ty === "video" || ty === "image")) {
        const h: Host = [c, t.type === "video"];
        visual.push(h);
        clips.set(c.id, h);
      }
    }
  }
  // explicit autoMotion on every host copy: transformAt must not read the registry's previous result
  visual.forEach((h, i) => { visual[i] = [{ ...h[0], autoMotion: null }, h[1]]; clips.set(h[0].id, visual[i]); });
  const ctx: Ctx = { W, H, fps, metas, visual, clips, defaultFit: st.background?.mode ?? "fit", interp: interp ?? (() => true) };
  const out = new Map<string, Resolved>();
  for (const [c, isMain] of visual) {
    const rf = c.reframe;
    const tr = tracks.get(rf?.trackId ?? "");
    if (rf && rf.enabled !== false && tr && tr.mediaId === c.mediaId) {
      const am = reframeMotion(ctx, c, tr, isMain);
      if (am) out.set(c.id, { autoMotion: am });
    }
  }
  if (out.size) {
    visual.forEach((h, i) => {
      const r = out.get(h[0].id);
      if (r) {
        const c2: Host = [{ ...h[0], autoMotion: r.autoMotion }, h[1]];
        visual[i] = c2;
        clips.set(h[0].id, c2);
      }
    });
  }
  for (const t of project.tracks) {
    if (t.type !== "overlay" && t.type !== "text") continue;
    for (const c of t.clips) {
      const fo = c.follow;
      const tr = tracks.get(fo?.trackId ?? "");
      if (!fo || !tr || fo.enabled === false) continue;
      if (c.region && c.adjustment) {
        const rg = regionSeries(ctx, c, tr);
        if (rg) out.set(c.id, { autoRegion: rg });
        continue;
      }
      const am = followMotion(ctx, c, tr, itemSize && fo.safe ? itemSize(c) : null);
      if (am) out.set(c.id, { autoMotion: am });
    }
  }
  return out;
}

export function regionAt(rg: AutoRegion, tau: number): [number, number, number, number] {
  const n = rg.x.length;
  const f = Math.min(Math.max(0, tau * rg.fps), n - 1);
  const i = Math.floor(f), j = Math.min(i + 1, n - 1), u = f - i;
  const at = (a: number[]) => a[i] + (a[j] - a[i]) * u;
  return [at(rg.x), at(rg.y), at(rg.w), at(rg.h)];
}
