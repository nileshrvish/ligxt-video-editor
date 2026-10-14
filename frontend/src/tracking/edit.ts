// Project edits for motion tracking (pure functions on a draft project; used by the Tracking panel).
import { clipDuration } from "../motion/motion";
import * as TE from "../text/edit";
import * as ops from "../timeline/ops";
import type { Clip, MediaMeta, Project, Track, TrackType } from "../types";
import { newClip } from "../util";
import { LOW_CONF } from "./follow";
import type { Follow, MotionTrack, Region } from "./types";

export const tracksOf = (p: Project, mediaId: string | null | undefined): MotionTrack[] =>
  (p.motionTracks ?? []).filter((t) => t.mediaId === mediaId);

export function trackById(p: Project, id: string | null | undefined): MotionTrack | undefined {
  return (p.motionTracks ?? []).find((t) => t.id === id);
}

/** Add or replace (same id) a track. */
export function upsertTrack(p: Project, track: MotionTrack): void {
  const list = p.motionTracks ?? (p.motionTracks = []);
  const i = list.findIndex((t) => t.id === track.id);
  if (i >= 0) list[i] = track;
  else list.push(track);
}

/** Remove a track; clips that followed it stay where they are (follow / reframe switched off). */
export function deleteTrack(p: Project, id: string): void {
  p.motionTracks = (p.motionTracks ?? []).filter((t) => t.id !== id);
  for (const t of p.tracks)
    for (const c of t.clips) {
      if (c.follow?.trackId === id) c.follow = null;
      if (c.reframe?.trackId === id) c.reframe = null;
    }
}

export function renameTrack(p: Project, id: string, name: string): void {
  const t = trackById(p, id);
  if (t) t.name = name.trim() || t.name;
}

/** A unique, friendly name: "Face 1", "Face 2", "Object 1"… */
export function nextName(p: Project, kind: "face" | "object"): string {
  const base = kind === "face" ? "Face" : "Object";
  const used = new Set((p.motionTracks ?? []).map((t) => t.name));
  for (let i = 1; ; i++) if (!used.has(`${base} ${i}`)) return `${base} ${i}`;
}

/** A track of `type` where [start, end) is free (an unlocked one, or a new one). */
function freeTrack(p: Project, type: Exclude<TrackType, "video">, start: number, end: number, avoidName?: string): Track {
  for (const t of p.tracks) {
    if (t.type !== type || t.locked || (avoidName && t.name === avoidName)) continue;
    if (t.clips.every((c) => c.start >= end - 1e-6 || c.start + clipDuration(c) <= start + 1e-6)) return t;
  }
  const id = ops.addTrack(p, type);
  return p.tracks.find((t) => t.id === id)!;
}

export type FollowerKind = "text" | "sticker" | "blur" | "pixelate";

/** A new clip spanning the host clip that follows the track: text, a sticker (media), or a blur /
 *  pixelate region. Returns its id. */
export function addFollower(p: Project, host: Clip, trackId: string, kind: FollowerKind, opts: { sticker?: MediaMeta; content?: string } = {}): string {
  const start = host.start, D = clipDuration(host), end = start + D;
  const follow: Follow = { trackId, clipId: host.id, position: true, scale: false, rotation: false, smoothing: kind === "blur" || kind === "pixelate" ? 0.05 : 0.3, safe: kind === "text" };
  let c: Clip;
  let t: Track;
  if (kind === "text") {
    const preset = TE.presetById("hook") ?? TE.presetById("caption");
    c = TE.newTextClip(start, D, { ...structuredClone(preset?.style ?? {}), content: opts.content ?? "Your text", size: 90 }, { x: 0, y: -170 });
    t = freeTrack(p, "text", start, end, "Captions");
  } else if (kind === "sticker") {
    if (!opts.sticker) throw new Error("No sticker chosen");
    c = newClip(opts.sticker, start);
    c.outPoint = D;
    c.fit = "fit";
    const sw = opts.sticker.width || 512;
    c.transform = { ...c.transform, scale: 200 / Math.min(p.settings.width, (sw * p.settings.height) / (opts.sticker.height || 512)), x: 0, y: -150 };
    t = freeTrack(p, "overlay", start, end);
  } else {
    c = { ...newClip({ id: "", type: "image", duration: 0 } as unknown as MediaMeta, start), mediaId: null, adjustment: true, outPoint: D };
    // generous padding + little smoothing: a face that moves fast must never peek out (measured on the Tracking Demo)
    c.region = { effect: kind, amount: kind === "blur" ? 90 : 70, shape: "ellipse", feather: 0.25, padding: 0.6 } satisfies Region;
    t = freeTrack(p, "overlay", start, end);
  }
  c.start = start;
  c.follow = follow;
  t.clips.push(c);
  ops.sortClips(t);
  return c.id;
}

/** Ranges (timeline seconds) where a clip shows low-confidence or lost frames of a track, for the timeline marks. */
export function weakRanges(track: MotionTrack, clip: Clip, srcAt: (tau: number) => number, fps: number): { from: number; to: number; lost: boolean }[] {
  const D = clipDuration(clip);
  const out: { from: number; to: number; lost: boolean }[] = [];
  const step = 1 / Math.max(1, fps);
  for (let tau = 0; tau < D; tau += step) {
    const k = Math.floor(srcAt(tau) * track.fps + 1e-6) - track.k0;
    const e = track.data[Math.min(Math.max(k, 0), track.data.length - 1)];
    const inRange = k >= 0 && k < track.data.length;
    const weak = !inRange || !e || e[5] < LOW_CONF;
    if (!weak) continue;
    const lost = !inRange || !e || e[5] <= 0;
    const last = out[out.length - 1];
    const t = clip.start + tau;
    if (last && Math.abs(last.to - t) < step * 1.5 && last.lost === lost) last.to = t + step;
    else out.push({ from: t, to: t + step, lost });
  }
  return out;
}
