// Click-to-select: canvas <-> clip source coordinates through the clip's layer matrix (the same geometry
// the preview and the export draw with). Points are stored normalised (0..1 of the source picture) at a
// source time, so they don't depend on the preview size or the proxy's resolution.
import { invert, rectMatrix, roundedRect, srcTime, transformAt } from "../motion/motion";
import type { Clip, MediaMeta, Project } from "../types";

export interface PickPoint { t: number; x: number; y: number; label: 0 | 1 }

function matrix(clip: Clip, m: MediaMeta, p: Project, t: number, isMain: boolean) {
  const { width: W, height: H } = p.settings;
  const sw = m.width || W, sh = m.height || H;
  const mode = clip.fit ?? (isMain ? p.settings.background.mode : "fit");
  const rect = roundedRect(sw, sh, clip.transform?.crop, mode === "fill", W, H);
  return { mat: rectMatrix(rect, W, H, transformAt(clip, t - clip.start)), sw, sh, rect };
}

/** Project-pixel point -> normalised source point of the clip (null outside its picture). */
export function canvasToClip(clip: Clip, m: MediaMeta, p: Project, t: number, isMain: boolean, X: number, Y: number) {
  const { mat, sw, sh, rect } = matrix(clip, m, p, t, isMain);
  const [a, b, c, d, e, f] = invert(mat);
  const sx = a * X + c * Y + e, sy = b * X + d * Y + f;
  const [x0, y0, rw, rh] = rect;
  if (sx < x0 || sy < y0 || sx > x0 + rw || sy > y0 + rh) return null;
  return { x: sx / sw, y: sy / sh };
}

/** Normalised source point -> project pixels. */
export function clipToCanvas(clip: Clip, m: MediaMeta, p: Project, t: number, isMain: boolean, x: number, y: number) {
  const { mat, sw, sh } = matrix(clip, m, p, t, isMain);
  const [a, b, c, d, e, f] = mat;
  const sx = x * sw, sy = y * sh;
  return { X: a * sx + c * sy + e, Y: b * sx + d * sy + f };
}

/** The source time shown at timeline time t (where a click lands in the footage). */
export const sourceTimeAt = (clip: Clip, t: number) => srcTime(clip, t - clip.start).src;
