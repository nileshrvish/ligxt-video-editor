// Slow-mo quality (mirrors backend/app/slowmo.py; checked against shared/slowmo_cases.json).
import { rampSegments } from "../motion/motion";
import type { Clip, MediaMeta } from "../types";

export type SlowQuality = "auto" | "repeat" | "blend" | "ai";
export interface SlowMo { quality: SlowQuality; smooth: boolean }

export const SLOWMO_DEFAULT: SlowMo = { quality: "auto", smooth: false };
export const QUALITY_LABELS: Record<SlowQuality, string> = {
  auto: "Auto (AI smooth when slowed)", repeat: "Frame repeat", blend: "Frame blending", ai: "AI smooth (RIFE)",
};

export function normalizeSlowmo(s?: Partial<SlowMo> | null): SlowMo {
  const q = s?.quality;
  return { quality: q === "repeat" || q === "blend" || q === "ai" || q === "auto" ? q : "auto", smooth: !!s?.smooth };
}

export function minSpeed(c: Clip): number {
  if (c.freezeFrame != null) return 1;
  return Math.min(...rampSegments(c).map((s) => s.speed));
}

/** repeat | blend | ai. Auto: AI smooth when the clip is slowed below 1x, frame repeat otherwise. */
export function qualityOf(c: Clip): "repeat" | "blend" | "ai" {
  const q = normalizeSlowmo(c.slowmo).quality;
  if (q === "auto") return minSpeed(c) < 1 - 1e-6 ? "ai" : "repeat";
  return q;
}

export const slowFactor = (c: Clip) => 1 / Math.max(0.05, minSpeed(c));

/** Does the clip play retimed frames (blending or AI) instead of repeating source frames? */
export function needsRetime(c: Clip, m: Pick<MediaMeta, "type" | "fps"> | undefined, fps: number): boolean {
  if (!m || m.type !== "video") return false;
  if (qualityOf(c) === "repeat") return false;
  const smooth = normalizeSlowmo(c.slowmo).smooth;
  return minSpeed(c) < 1 - 1e-6 || (smooth && Math.abs((m.fps || 30) - fps) > 0.01);
}
