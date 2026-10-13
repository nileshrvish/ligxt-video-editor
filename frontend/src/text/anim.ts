// Text animation state and draw operations: a port of backend/app/text.py (state, text_matrix,
// text_ops), checked against shared/text_cases.json. The backend lays text out and rasterises it;
// the preview only decides what to draw where, exactly like the export.
import stylesJson from "../../../shared/text_styles.json";
import type { Clip } from "../types";
import { clipDuration, ease, transformAt, type Mat, type TransformValues } from "../motion/motion";

export interface TextAnim { type: string; duration: number; easing: string; direction?: string; distance?: number }
export interface TextSpec {
  content: string; font: string; size: number; color: string;
  outline: { width: number; color: string };
  shadow: { enabled: boolean; color: string; opacity: number; blur: number; x: number; y: number };
  box: { enabled: boolean; color: string; opacity: number; padX: number; padY: number; radius: number; mode: string };
  letterSpacing: number; lineHeight: number; align: "left" | "center" | "right"; maxWidth: number; uppercase: boolean;
  animIn: TextAnim; animOut: TextAnim;
  highlight: { style: "none" | "color" | "pop" | "reveal" | "revealpop"; color: string };
  words: { text: string; start: number; end: number }[] | null;
}
export interface FontInfo { id: string; name: string; file: string; license: string }
export interface TextPreset { id: string; label: string; position: { x: number; y: number }; style: Partial<TextSpec> }

export const TEXT_FONTS = stylesJson.fonts as FontInfo[];
export const TEXT_DEFAULTS = stylesJson.defaults as unknown as TextSpec;
export const TEXT_PRESETS = stylesJson.presets as unknown as TextPreset[];

export interface Unit { tile: [number, number, number, number]; anchor: [number, number] }
export interface TextLayout {
  key: string; res: number; w: number; h: number; m: number; iw: number; ih: number; lines: number;
  images: { base: string; hi?: string; box?: string };
  chars: (Unit & { c: string; word: number; line: number })[];
  words: (Unit & { text: string; line: number })[];
}

type Obj = Record<string, unknown>;
function merge<T>(base: T, over: unknown): T {
  const out = structuredClone(base) as Obj;
  for (const [k, v] of Object.entries((over ?? {}) as Obj)) {
    if (v && typeof v === "object" && !Array.isArray(v) && out[k] && typeof out[k] === "object" && !Array.isArray(out[k])) out[k] = merge(out[k], v);
    else out[k] = structuredClone(v);
  }
  return out as T;
}
export const normalize = (spec: unknown): TextSpec => merge(TEXT_DEFAULTS, spec);
export const mergeSpec = merge;

const clamp = (v: number, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, v));
const DIRS: Record<string, [number, number]> = { up: [0, 1], down: [0, -1], left: [1, 0], right: [-1, 0] };

export interface UnitState { a: number; dy: number; s: number; hi: number }
export interface TextState { block: { op: number; dx: number; dy: number; s: number }; kind: "chars" | "words" | null; units: UnitState[] }

export function state(spec: unknown, nchars: number, nwords: number, tau: number, D: number): TextState {
  const s = normalize(spec);
  const blk = { op: 1, dx: 0, dy: 0, s: 1 };
  let kind: TextState["kind"] = null;
  let units: UnitState[] = [];
  const list = (n: number) => Array.from({ length: n }, () => ({ a: 1, dy: 0, s: 1, hi: 0 }));
  for (const [phase, t] of [["animIn", tau], ["animOut", D - tau]] as const) {
    const a = s[phase];
    const typ = a.type ?? "none";
    if (typ === "none") continue;
    const dur = Math.max(1e-3, a.duration ?? 0.4);
    const u = clamp(t / dur);
    const e = ease(a.easing ?? "linear", u);
    const sign = phase === "animIn" ? 1 : -1;
    if (typ === "fade") blk.op *= clamp(e);
    else if (typ === "slide") {
      const dist = a.distance ?? 80;
      const [vx, vy] = DIRS[a.direction ?? "up"] ?? [0, 1];
      blk.dx += sign * vx * (1 - e) * dist;
      blk.dy += sign * vy * (1 - e) * dist;
      blk.op *= clamp(e);
    } else if (typ === "pop") {
      blk.s *= 0.6 + 0.4 * e;
      blk.op *= clamp(u * 4);
    } else if (typ === "typewriter" && nchars) {
      if (kind !== "chars") { kind = "chars"; units = list(nchars); }
      const vis = phase === "animIn" ? Math.floor(e * nchars + 1e-6) : Math.ceil(e * nchars - 1e-6);
      units.forEach((un, i) => { if (i >= vis) un.a = 0; });
    } else if (typ === "words" && nwords) {
      if (kind === null) { kind = "words"; units = list(nwords); }
      if (kind !== "words") continue;
      const span = nwords > 1 ? dur * 0.5 : 0;
      const L = dur - span;
      units.forEach((un, i) => {
        const st = (span * i) / Math.max(1, nwords - 1);
        const v = clamp((t - st) / Math.max(1e-3, L));
        const ev = ease(a.easing ?? "linear", v);
        un.a *= clamp(ev);
        un.dy += sign * (1 - ev) * 24;
        un.s *= 0.8 + 0.2 * ev;
      });
    }
  }
  const hl = s.highlight;
  const wt = s.words ?? [];
  if ((hl.style ?? "none") !== "none" && wt.length && nwords && kind !== "chars") {
    if (kind === null) { kind = "words"; units = list(nwords); }
    let active = -1;
    for (let i = 0; i < Math.min(nwords, wt.length); i++) if (wt[i].start <= tau + 1e-9) active = i;
    units.forEach((un, i) => {
      if ((hl.style === "reveal" || hl.style === "revealpop") && i < wt.length && tau + 1e-9 < wt[i].start) un.a = 0;
      if (i === active) {
        un.hi = hl.style === "color" || hl.style === "pop" || hl.style === "revealpop" ? 1 : 0;
        if (hl.style === "pop" || hl.style === "revealpop") un.s *= 1 + 0.08 * ease("punch", clamp((tau - wt[i].start) / 0.12));
      }
    });
  }
  return { block: blk, kind, units };
}

export function textMatrix(w: number, h: number, W: number, H: number, t: TransformValues): Mat {
  const s = t.scale;
  const fx = t.flipH ? -1 : 1, fy = t.flipV ? -1 : 1;
  const th = (t.rotation * Math.PI) / 180;
  const co = Math.cos(th), si = Math.sin(th);
  const a = s * fx * co, b = s * fx * si, c = -s * fy * si, d = s * fy * co;
  const cx = w / 2, cy = h / 2, Cx = W / 2 + t.x, Cy = H / 2 + t.y;
  return [a, b, c, d, Cx - (a * cx + c * cy), Cy - (b * cx + d * cy)];
}

export function mul(m1: Mat, m2: Mat): Mat {
  const [a1, b1, c1, d1, e1, f1] = m1;
  const [a2, b2, c2, d2, e2, f2] = m2;
  return [a1 * a2 + c1 * b2, b1 * a2 + d1 * b2, a1 * c2 + c1 * d2, b1 * c2 + d1 * d2, a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1];
}

export interface TextOp { img: "base" | "hi" | "box"; tile: [number, number, number, number]; m: Mat; alpha: number }

/** Draw operations for one frame (same as text.text_ops). */
export function textOps(lay: TextLayout, clip: Clip, tau: number, W: number, H: number): TextOp[] {
  const D = clipDuration(clip);
  const st = state(clip.text, lay.chars.length, lay.words.length, tau, D);
  const t0 = transformAt(clip, tau);
  const b = st.block;
  const tv = { ...t0, x: t0.x + b.dx, y: t0.y + b.dy, scale: t0.scale * b.s };
  const op = clamp(tv.opacity) * b.op;
  if (op <= 1e-4) return [];
  const mb = textMatrix(lay.w, lay.h, W, H, tv);
  const m = lay.m;
  const full: [number, number, number, number] = [-m, -m, lay.w + m, lay.h + m];
  const ops: TextOp[] = [];
  if (lay.images.box) ops.push({ img: "box", tile: full, m: mb, alpha: op });
  if (st.kind === null) {
    ops.push({ img: "base", tile: full, m: mb, alpha: op });
    return ops;
  }
  const items: Unit[] = st.kind === "chars" ? lay.chars : lay.words;
  const pairs = items.map((it, i) => [it, st.units[i]] as const).filter(([, u]) => u);
  // highlighted (possibly enlarged) units go last (stable sort, like Python's sorted)
  pairs.sort((p, q) => Number(p[1].hi > 0) - Number(q[1].hi > 0));
  for (const [it, u] of pairs) {
    const a = op * u.a;
    if (a <= 1e-4) continue;
    const [ax, ay] = it.anchor;
    const mu = mul(mb, [u.s, 0, 0, u.s, ax - u.s * ax, ay + u.dy - u.s * ay]);
    if (u.hi < 1 || !lay.images.hi) ops.push({ img: "base", tile: it.tile, m: mu, alpha: a });
    if (u.hi > 0 && lay.images.hi) ops.push({ img: "hi", tile: it.tile, m: mu, alpha: a * u.hi });
  }
  return ops;
}

/** The text block's corners on the canvas at rest (no in/out animation), for handles and checks. */
export function textBox(lay: { w: number; h: number }, clip: Clip, tau: number, W: number, H: number): [number, number][] {
  const m = textMatrix(lay.w, lay.h, W, H, transformAt(clip, tau));
  return [[0, 0], [lay.w, 0], [lay.w, lay.h], [0, lay.h]].map(([x, y]) => [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]]);
}

/** Keys of the spec that change the rendered pixels (same as text.STYLE_KEYS). */
export const STYLE_KEYS = ["content", "font", "size", "color", "outline", "shadow", "box", "letterSpacing", "lineHeight", "align", "maxWidth", "uppercase", "highlight"] as const;
export function lookOf(spec: unknown): string {
  const s = normalize(spec) as unknown as Obj;
  const look: Obj = {};
  for (const k of STYLE_KEYS) look[k] = s[k];
  const hl = (s.highlight as TextSpec["highlight"]);
  look.highlight = hl.style === "color" || hl.style === "pop" || hl.style === "revealpop" ? hl.color : null;
  return JSON.stringify(look);
}
