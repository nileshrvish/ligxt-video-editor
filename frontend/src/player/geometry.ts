// Where a clip's picture lands on the canvas. Must match backend/app/render.py:
// - main track: fit (contain), fill (cover) or blur (contain over a blurred cover); no transform yet (Phase 5)
// - overlay tracks: fit/fill, then transform.scale about the centre, offset by transform.x/y from the
//   canvas centre, plus rotation / flips / opacity. Fill overlays are cropped to (W*scale x H*scale).
import type { Clip, FitMode } from "../types";

export interface LayerGeometry {
  /** Drawn size of the full (uncropped) picture, in project pixels. */
  dw: number;
  dh: number;
  /** Visible size after cropping (equal to dw/dh unless fill-cropped). */
  cw: number;
  ch: number;
  /** Centre of the layer in project pixels. */
  cx: number;
  cy: number;
  rotation: number; // radians
  flipH: boolean;
  flipV: boolean;
  opacity: number;
  blurBackground: boolean;
}

export function layerGeometry(
  srcW: number, srcH: number, clip: Clip, isMain: boolean, defaultFit: FitMode, W: number, H: number,
): LayerGeometry {
  const sw = Math.max(1, srcW || W);
  const sh = Math.max(1, srcH || H);
  const fit: FitMode = clip.fit ?? (isMain ? defaultFit : "fit");
  const mode = fit === "fill" ? "fill" : "fit";
  const f = mode === "fill" ? Math.max(W / sw, H / sh) : Math.min(W / sw, H / sh);
  const tr = clip.transform;
  const k = isMain ? 1 : Math.max(0.01, tr?.scale ?? 1);
  const dw = sw * f * k;
  const dh = sh * f * k;
  return {
    dw,
    dh,
    cw: mode === "fill" ? Math.min(dw, W * k) : dw,
    ch: mode === "fill" ? Math.min(dh, H * k) : dh,
    cx: W / 2 + (isMain ? 0 : tr?.x ?? 0),
    cy: H / 2 + (isMain ? 0 : tr?.y ?? 0),
    rotation: isMain ? 0 : ((tr?.rotation ?? 0) * Math.PI) / 180,
    flipH: !isMain && !!tr?.flipH,
    flipV: !isMain && !!tr?.flipV,
    opacity: isMain ? 1 : Math.max(0, Math.min(1, tr?.opacity ?? 1)),
    blurBackground: isMain && fit === "blur",
  };
}

/** Linear fade gain for audio at clip-relative time `t` (seconds into the clip). */
export function fadeGain(t: number, dur: number, fadeIn: number, fadeOut: number): number {
  let g = 1;
  if (fadeIn > 0 && t < fadeIn) g = Math.min(g, Math.max(0, t / fadeIn));
  if (fadeOut > 0 && t > dur - fadeOut) g = Math.min(g, Math.max(0, (dur - t) / fadeOut));
  return g;
}

/** Instagram Reels UI zones on a 1080x1920 canvas, as fractions (top, bottom, right column). */
export const REELS_SAFE = { top: 220 / 1920, bottom: 420 / 1920, right: 140 / 1080, rightFrom: 0.42 };
