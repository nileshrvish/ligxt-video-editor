// Safe-area checks for Reels (9:16): does a text block or a detected face sit under the header,
// the caption area or the right-hand buttons? Uses the same geometry as the renderers.
import type { Clip, MediaMeta, Project } from "../types";
import { clipEnd } from "../util";
import { apply, hasMotion, rectMatrix, roundedRect, transformAt, type Mat } from "../motion/motion";
import { textBox } from "../text/anim";
import { textAsset } from "../text/cache";
import { REELS_SAFE } from "./geometry";

export type Zone = "header" | "caption" | "buttons";
export const ZONE_LABEL: Record<Zone, string> = { header: "the Reels header", caption: "the caption area", buttons: "the buttons on the right" };
export interface SafetyWarning { clipId: string; kind: "text" | "face"; zone: Zone; time: number; label: string }
export const FACE_MIN_SCORE = 0.85;

export function zones(W: number, H: number): Record<Zone, [number, number, number, number]> {
  return {
    header: [0, 0, W, REELS_SAFE.top * H],
    caption: [0, H - REELS_SAFE.bottom * H, W, H],
    buttons: [W - REELS_SAFE.right * W, REELS_SAFE.rightFrom * H, W, H],
  };
}

type Box = [number, number, number, number];
const aabb = (pts: [number, number][]): Box => [Math.min(...pts.map((p) => p[0])), Math.min(...pts.map((p) => p[1])), Math.max(...pts.map((p) => p[0])), Math.max(...pts.map((p) => p[1]))];
const overlap = (a: Box, b: Box) => Math.max(0, Math.min(a[2], b[2]) - Math.max(a[0], b[0])) * Math.max(0, Math.min(a[3], b[3]) - Math.max(a[1], b[1]));

/** Sample times inside a clip: start, middle, end and every keyframe (clip-relative). */
function sampleTimes(c: Clip): number[] {
  const D = clipEnd(c) - c.start;
  const ts = new Set([0.02, D / 2, Math.max(0, D - 0.02)]);
  for (const ks of Object.values(c.keyframes ?? {})) for (const k of ks ?? []) if (k.time >= 0 && k.time <= D) ts.add(k.time);
  return [...ts].sort((a, b) => a - b);
}

/** The picture's layer matrix at clip time tau (source px -> canvas), for face boxes. */
export function layerMatrixAt(c: Clip, m: MediaMeta, isMain: boolean, defaultFit: string, W: number, H: number, tau: number): Mat {
  const sw = m.width || W, sh = m.height || H;
  const fill = (c.fit ?? (isMain ? defaultFit : "fit")) === "fill";
  const tv = isMain && !hasMotion(c) ? transformAt({ ...c, keyframes: {}, transform: { ...c.transform, x: 0, y: 0, scale: 1, rotation: 0 } }, 0) : transformAt(c, tau);
  return rectMatrix(roundedRect(sw, sh, c.transform?.crop, fill, W, H), W, H, tv);
}

/** Face boxes (canvas px) of a clip at clip time tau. */
export function faceBoxes(c: Clip, m: MediaMeta, isMain: boolean, p: Project, tau: number): Box[] {
  const fr = m.faces?.frames ?? [];
  if (!fr.length) return [];
  const src = m.type === "video" ? fr.reduce((b, f) => (Math.abs(f.t - (c.inPoint + tau)) < Math.abs(b.t - (c.inPoint + tau)) ? f : b)) : fr[0];
  const { width: W, height: H } = p.settings;
  const mat = layerMatrixAt(c, m, isMain, p.settings.background.mode, W, H, tau);
  const sw = m.width || W, sh = m.height || H;
  return src.boxes.filter((b) => b[4] >= FACE_MIN_SCORE).map(([x, y, w, h]) =>
    aabb([[x, y], [x + w, y], [x, y + h], [x + w, y + h]].map(([u, v]) => apply(mat, u * sw, v * sh))));
}

export function textBoxAt(c: Clip, p: Project, tau: number): Box | null {
  const a = textAsset(c.text);
  if (a.status !== "ready" || !a.layout) return null;
  return aabb(textBox(a.layout, c, tau, p.settings.width, p.settings.height));
}

export function safetyWarnings(p: Project, media: Record<string, MediaMeta>): SafetyWarning[] {
  if (p.settings.aspect !== "9:16") return [];
  const { width: W, height: H } = p.settings;
  const Z = zones(W, H);
  const out: SafetyWarning[] = [];
  const main = p.tracks.find((t) => t.type === "video");
  for (const t of p.tracks) {
    if (t.hidden || (t.type !== "video" && t.type !== "overlay" && t.type !== "text")) continue;
    for (const c of t.clips) {
      const seen = new Set<string>();
      for (const tau of sampleTimes(c)) {
        let boxes: { kind: "text" | "face"; b: Box }[] = [];
        if (t.type === "text") {
          const b = c.text ? textBoxAt(c, p, tau) : null;
          if (b) boxes = [{ kind: "text", b }];
        } else if (c.mediaId && media[c.mediaId]) {
          boxes = faceBoxes(c, media[c.mediaId], t === main, p, tau).map((b) => ({ kind: "face" as const, b }));
        }
        for (const { kind, b } of boxes) {
          const area = Math.max(1, (b[2] - b[0]) * (b[3] - b[1]));
          if (b[2] < 0 || b[0] > W || b[3] < 0 || b[1] > H) continue; // off screen
          for (const z of Object.keys(Z) as Zone[]) {
            const ov = overlap(b, Z[z]);
            // text must be fully clear; a face counts when a fifth of it is covered
            if (kind === "text" ? ov > 1 : ov / area > 0.2) {
              const key = `${kind}:${z}`;
              if (seen.has(key)) continue;
              seen.add(key);
              const name = kind === "text" ? `“${String((c.text as { content?: string })?.content ?? "").slice(0, 24)}”` : (media[c.mediaId!]?.name ?? "clip");
              out.push({ clipId: c.id, kind, zone: z, time: c.start + tau, label: `${kind === "text" ? "Text" : "Face in"} ${name} is under ${ZONE_LABEL[z]}` });
            }
          }
        }
      }
    }
  }
  return out.sort((a, b) => a.time - b.time);
}
