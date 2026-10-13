// Cutout settings and the shared maths of the subject effects (mirrors backend/app/cutout.py; both are
// checked against shared/cutout_cases.json).
import type { Filter, MediaMeta, Project } from "../types";

export type CutoutBgType = "keep" | "blur" | "color" | "image" | "video" | "remove";

export interface CutoutRefine { smooth: number; grow: number; feather: number; detail: boolean; motion: number }
export interface CutoutBackground { type: CutoutBgType; color: string; blur: number; mediaId: string | null }
export interface GlowPulse { mode: "none" | "beat" | "rate"; amount: number; rate: number }
export interface CutoutGlow { enabled: boolean; color: string; width: number; softness: number; opacity: number; pulse: GlowPulse }
export interface CutoutShadow { enabled: boolean; color: string; opacity: number; blur: number; x: number; y: number }

export interface Cutout {
  enabled: boolean;
  /** "person": automatic matting; "object": click to select (SAM 2), using `points`. */
  mode: "person" | "object";
  points: { t: number; x: number; y: number; label: 0 | 1 }[];
  /** "auto" = the default person model from Settings. */
  model: string;
  refine: CutoutRefine;
  background: CutoutBackground;
  subjectFilters: Filter[];
  backgroundFilters: Filter[];
  glow: CutoutGlow;
  shadow: CutoutShadow;
}

export const CUTOUT_DEFAULT: Cutout = {
  enabled: false,
  mode: "person",
  points: [],
  model: "auto",
  refine: { smooth: 0.5, grow: 0, feather: 1, detail: true, motion: 0.8 },
  background: { type: "keep", color: "#14141a", blur: 24, mediaId: null },
  subjectFilters: [],
  backgroundFilters: [],
  glow: { enabled: false, color: "#ffd25a", width: 14, softness: 0.5, opacity: 0.9, pulse: { mode: "none", amount: 0.7, rate: 2 } },
  shadow: { enabled: false, color: "#000000", opacity: 0.55, blur: 16, x: 10, y: 16 },
};

export function normalizeCutout(c?: Partial<Cutout> | null): Cutout {
  const d = CUTOUT_DEFAULT;
  const x = (c ?? {}) as Partial<Cutout>;
  return {
    ...d,
    ...x,
    refine: { ...d.refine, ...(x.refine ?? {}) },
    background: { ...d.background, ...(x.background ?? {}) },
    glow: { ...d.glow, ...(x.glow ?? {}), pulse: { ...d.glow.pulse, ...(x.glow?.pulse ?? {}) } },
    shadow: { ...d.shadow, ...(x.shadow ?? {}) },
    points: x.points ?? [],
    subjectFilters: x.subjectFilters ?? [],
    backgroundFilters: x.backgroundFilters ?? [],
  };
}

export const cutoutOn = (c: { cutout?: unknown; mediaId?: string | null }): boolean =>
  !!c.mediaId && !!(c.cutout as Cutout | undefined)?.enabled;

// ---------------------------------------------------------------- glow pulse
export const PULSE_DECAY = 0.22;

/** Beat times on the timeline from the music tracks' songs (same as cutout.timeline_beats). */
export function timelineBeats(p: Pick<Project, "tracks">, media: Record<string, Pick<MediaMeta, "beats">>): number[] {
  const out: number[] = [];
  for (const t of p.tracks) {
    if (t.type !== "music" || t.muted) continue;
    for (const c of t.clips) {
      const m = c.mediaId ? media[c.mediaId] : undefined;
      const sp = c.speed || 1;
      for (const x of m?.beats ?? []) if (x >= c.inPoint - 1e-6 && x < c.outPoint) out.push(c.start + (x - c.inPoint) / sp);
    }
  }
  return out.sort((a, b) => a - b);
}

/** 0..1 pulse envelope at timeline time t. */
export function pulseAt(pulse: Partial<GlowPulse> | undefined, t: number, beats: number[]): number {
  const mode = pulse?.mode ?? "none";
  if (mode === "beat") {
    // bisect_right(beats, t + 1e-6) - 1
    let lo = 0, hi = beats.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (t + 1e-6 < beats[mid]) hi = mid;
      else lo = mid + 1;
    }
    const i = lo - 1;
    return i >= 0 ? Math.exp(-(t - beats[i]) / PULSE_DECAY) : 0;
  }
  if (mode === "rate") return 0.5 - 0.5 * Math.cos(2 * Math.PI * (pulse?.rate ?? 2) * t);
  return 0;
}

/** erfc with ~1e-7 relative error (Numerical Recipes, Chebyshev fit). */
export function erfc(x: number): number {
  const z = Math.abs(x);
  const t = 1 / (1 + 0.5 * z);
  const r = t * Math.exp(-z * z - 1.26551223 + t * (1.00002368 + t * (0.37409196 + t * (0.09678418 + t * (-0.18628806
    + t * (0.27886807 + t * (-1.13520398 + t * (1.48851587 + t * (-0.82215223 + t * 0.17087277)))))))));
  return x >= 0 ? r : 2 - r;
}

/** [sigma px, lo, hi, opacity]: glow alpha = clip((blur(matte, sigma) - lo) / (hi - lo)) * opacity. */
export function glowLevels(glow: Partial<CutoutGlow>, p: number, k = 1): [number, number, number, number] {
  const pulse = glow.pulse;
  const amt = pulse && (pulse.mode ?? "none") !== "none" ? pulse.amount ?? 0.7 : 0;
  const w0 = Math.max(1, glow.width ?? 14);
  const s = Math.max(0, Math.min(1, glow.softness ?? 0.5));
  const sigma = Math.max(0.8, w0 * (0.25 + 0.5 * s));
  const w = w0 * (1 + 0.6 * amt * p);
  const lo = 0.5 * erfc(w / (sigma * Math.SQRT2));
  const hi = lo + (0.5 - lo) * (0.12 + 0.88 * s);
  const op = Math.max(0, Math.min(1, (glow.opacity ?? 0.9) * (1 - 0.5 * amt + 0.5 * amt * p)));
  return [sigma * k, lo, hi, op];
}
