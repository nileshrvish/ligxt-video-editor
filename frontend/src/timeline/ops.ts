// Pure timeline editing operations. Each op mutates a draft Project (callers clone first),
// so they're easy to unit test and every UI action becomes one undo step.
//
// Rules:
// - The main video track is magnetic by default: clips are packed from 0 in order, dragging
//   reorders, trimming/deleting ripples. Turn off with track.magnetic = false.
// - Every other track allows gaps but never overlaps: a clip lands in the nearest free slot.
import type { Clip, Marker, Project, Track, TrackType } from "../types";
import { clipDuration, clipEnd, deepClone, uid } from "../util";
import { splitText } from "../text/edit";

export const MIN_CLIP = 0.1; // seconds
const EPS = 1e-6;

/** Source durations for trimming limits. Images return Infinity. */
export type MediaDuration = (mediaId: string | null) => number;

export function findClip(p: Project, clipId: string): { track: Track; clip: Clip; index: number } | null {
  for (const track of p.tracks) {
    const index = track.clips.findIndex((c) => c.id === clipId);
    if (index >= 0) return { track, clip: track.clips[index], index };
  }
  return null;
}

export function isMagnetic(t: Track): boolean {
  return t.type === "video" && t.magnetic !== false;
}

export function sortClips(t: Track): void {
  t.clips.sort((a, b) => a.start - b.start);
}

/** Lay clips end-to-end from 0. Sorts by start first unless the array order is already the intended order. */
export function pack(t: Track, sortFirst = true): void {
  if (sortFirst) sortClips(t);
  let x = 0;
  for (const c of t.clips) {
    c.start = round(x);
    x += clipDuration(c);
  }
}

const round = (t: number) => Math.round(t * 1e6) / 1e6;

/** Which track types a clip may move between. */
export function compatible(from: TrackType, to: TrackType): boolean {
  const visual: TrackType[] = ["video", "overlay"];
  const audio: TrackType[] = ["audio", "music"];
  return from === to || (visual.includes(from) && visual.includes(to)) || (audio.includes(from) && audio.includes(to));
}

/** Nearest start time >= 0 where a clip of `dur` fits on `t` without overlapping (ignoring `excludeId`). */
export function nearestFreeStart(t: Track, dur: number, desired: number, excludeId?: string): number {
  const others = t.clips.filter((c) => c.id !== excludeId).sort((a, b) => a.start - b.start);
  desired = Math.max(0, desired);
  let best = Infinity;
  let bestDist = Infinity;
  let gapStart = 0;
  const consider = (lo: number, hi: number) => {
    if (hi - lo + EPS < dur) return;
    const pos = Math.min(Math.max(desired, lo), hi - dur);
    const dist = Math.abs(pos - desired);
    if (dist < bestDist) {
      bestDist = dist;
      best = pos;
    }
  };
  for (const c of others) {
    consider(gapStart, c.start);
    gapStart = Math.max(gapStart, clipEnd(c));
  }
  consider(gapStart, Infinity);
  return round(best);
}

/** Index at which a clip centred at `center` would be inserted among a magnetic track's clips. */
function insertionIndex(t: Track, center: number, excludeId?: string): number {
  const others = t.clips.filter((c) => c.id !== excludeId).sort((a, b) => a.start - b.start);
  let i = 0;
  while (i < others.length && others[i].start + clipDuration(others[i]) / 2 < center) i++;
  return i;
}

/** Put `clip` (not currently on any track) onto track `t` near `desiredStart`. */
export function placeClip(t: Track, clip: Clip, desiredStart: number): void {
  if (isMagnetic(t)) {
    sortClips(t);
    const idx = insertionIndex(t, desiredStart + clipDuration(clip) / 2);
    t.clips.splice(idx, 0, clip);
    pack(t, false);
  } else {
    clip.start = nearestFreeStart(t, clipDuration(clip), desiredStart);
    t.clips.push(clip);
    sortClips(t);
  }
}

export function insertClip(p: Project, trackId: string, clip: Clip, desiredStart: number): void {
  const t = p.tracks.find((x) => x.id === trackId);
  if (!t) throw new Error("Track not found");
  placeClip(t, clip, desiredStart);
}

/** Move a clip to `desiredStart` on `toTrackId` (same track if omitted). */
export function moveClip(p: Project, clipId: string, desiredStart: number, toTrackId?: string): void {
  const hit = findClip(p, clipId);
  if (!hit) return;
  const from = hit.track;
  const to = (toTrackId && p.tracks.find((t) => t.id === toTrackId)) || from;
  if (to.locked || from.locked || !compatible(from.type, to.type)) return;
  from.clips.splice(hit.index, 1);
  if (isMagnetic(from) && from !== to) pack(from);
  placeClip(to, hit.clip, desiredStart);
  pruneTransitions(p);
}

/** After a clip's in/out/start was edited directly: repack a magnetic track, or move the clip
 *  to the nearest free slot if it now overlaps a neighbour. Never reorders a magnetic track. */
export function settle(p: Project, clipId: string): void {
  const hit = findClip(p, clipId);
  if (!hit) return;
  const { track: t, clip: c } = hit;
  if (isMagnetic(t)) {
    pack(t, false);
    return;
  }
  c.start = nearestFreeStart(t, clipDuration(c), c.start, c.id);
  sortClips(t);
  pruneTransitions(p);
}

/** Drag the left edge so the clip starts at `newStart` (end stays put on free tracks). */
export function trimStart(p: Project, clipId: string, newStart: number, mediaDur: MediaDuration): void {
  const hit = findClip(p, clipId);
  if (!hit || hit.track.locked) return;
  const { track: t, clip: c } = hit;
  const speed = c.speed || 1;
  const end = clipEnd(c);
  const isImage = !isFinite(mediaDur(c.mediaId));
  // Lower bound: the source can't go before 0 (photos can grow up to 10 min). On free tracks the
  // clip also can't cross t=0 or the previous clip; on magnetic tracks the start is re-packed anyway.
  let lo = isImage ? c.start - 600 : c.start - c.inPoint / speed;
  if (!isMagnetic(t)) {
    const prev = t.clips.filter((x) => x.id !== c.id && clipEnd(x) <= c.start + EPS).reduce((m, x) => Math.max(m, clipEnd(x)), 0);
    lo = Math.max(lo, prev, 0);
  }
  const hi = end - MIN_CLIP;
  const s = round(Math.min(hi, Math.max(lo, newStart)));
  const delta = s - c.start;
  if (isImage) {
    c.outPoint = round(c.outPoint - delta * speed);
  } else {
    c.inPoint = round(Math.max(0, c.inPoint + delta * speed));
  }
  if (isMagnetic(t)) {
    pack(t); // start stays where packing puts it; followers ripple
  } else {
    c.start = s;
  }
  pruneTransitions(p);
}

/** Drag the right edge so the clip ends at `newEnd`. */
export function trimEnd(p: Project, clipId: string, newEnd: number, mediaDur: MediaDuration): void {
  const hit = findClip(p, clipId);
  if (!hit || hit.track.locked) return;
  const { track: t, clip: c } = hit;
  const speed = c.speed || 1;
  const srcMax = mediaDur(c.mediaId);
  let hi = isFinite(srcMax) ? c.start + (srcMax - c.inPoint) / speed : Infinity;
  if (!isMagnetic(t)) {
    const next = t.clips.filter((x) => x.id !== c.id && x.start >= clipEnd(c) - EPS).reduce((m, x) => Math.min(m, x.start), Infinity);
    hi = Math.min(hi, next);
  }
  const lo = c.start + MIN_CLIP;
  const e = Math.min(hi, Math.max(lo, newEnd));
  c.outPoint = round(c.inPoint + (e - c.start) * speed);
  if (isMagnetic(t)) pack(t);
  pruneTransitions(p);
}

/** Split `clipId` at timeline time `t`. Returns the new (right-hand) clip id, or null. */
export function splitClip(p: Project, clipId: string, time: number): string | null {
  const hit = findClip(p, clipId);
  if (!hit || hit.track.locked) return null;
  const c = hit.clip;
  if (time <= c.start + MIN_CLIP / 2 || time >= clipEnd(c) - MIN_CLIP / 2) return null;
  const speed = c.speed || 1;
  const cut = round(c.inPoint + (time - c.start) * speed);
  const right: Clip = deepClone(c);
  right.id = uid();
  right.start = round(time);
  right.inPoint = cut;
  right.fadeIn = 0;
  c.outPoint = cut;
  c.fadeOut = 0;
  // Keyframes are clip-relative: keep each half's own, shifting the right half's times.
  const offset = time - c.start;
  for (const [k, list] of Object.entries(c.keyframes ?? {})) {
    c.keyframes[k] = list.filter((kf) => kf.time <= offset + EPS);
    right.keyframes[k] = list.filter((kf) => kf.time >= offset - EPS).map((kf) => ({ ...kf, time: round(kf.time - offset) }));
  }
  if (c.text) splitText(c, right, offset);
  hit.track.clips.splice(hit.index + 1, 0, right);
  // A transition that left the original clip now leaves the right half.
  for (const tr of p.transitions) if (tr.fromClipId === c.id) tr.fromClipId = right.id;
  return right.id;
}

/** Split every unlocked clip under the playhead. Returns ids of new clips. */
export function splitAll(p: Project, time: number): string[] {
  const ids: string[] = [];
  for (const t of p.tracks) {
    if (t.locked) continue;
    const c = t.clips.find((x) => time > x.start && time < clipEnd(x));
    if (c) {
      const id = splitClip(p, c.id, time);
      if (id) ids.push(id);
    }
  }
  return ids;
}

export function deleteClip(p: Project, clipId: string, ripple = false): void {
  const hit = findClip(p, clipId);
  if (!hit || hit.track.locked) return;
  const { track: t, clip: c } = hit;
  const dur = clipDuration(c);
  t.clips.splice(hit.index, 1);
  if (isMagnetic(t)) pack(t);
  else if (ripple) {
    for (const x of t.clips) if (x.start >= c.start - EPS) x.start = round(Math.max(0, x.start - dur));
  }
  pruneTransitions(p);
}

/** Duplicate a clip right after itself. Returns the new id. */
export function duplicateClip(p: Project, clipId: string): string | null {
  const hit = findClip(p, clipId);
  if (!hit || hit.track.locked) return null;
  const copy: Clip = deepClone(hit.clip);
  copy.id = uid();
  const t = hit.track;
  if (isMagnetic(t)) {
    sortClips(t);
    t.clips.splice(t.clips.indexOf(hit.clip) + 1, 0, copy);
    pack(t, false);
  } else {
    copy.start = nearestFreeStart(t, clipDuration(copy), clipEnd(hit.clip));
    t.clips.push(copy);
    sortClips(t);
  }
  return copy.id;
}

/** Drop transitions whose two clips are no longer neighbours on the same track. */
export function pruneTransitions(p: Project): void {
  p.transitions = p.transitions.filter((tr) => {
    const t = p.tracks.find((x) => x.id === tr.trackId);
    if (!t) return false;
    const sorted = [...t.clips].sort((a, b) => a.start - b.start);
    const i = sorted.findIndex((c) => c.id === tr.fromClipId);
    return i >= 0 && sorted[i + 1]?.id === tr.toClipId && Math.abs(clipEnd(sorted[i]) - sorted[i + 1].start) < 1e-3;
  });
}

// ---------------------------------------------------------------- transitions
/** Add or update the transition on the cut between `fromId` and `toId` (they must be neighbours). */
export function setTransition(p: Project, fromId: string, toId: string, patch: { type?: string; duration?: number }): string | null {
  const a = findClip(p, fromId);
  const b = findClip(p, toId);
  if (!a || !b || a.track !== b.track || Math.abs(clipEnd(a.clip) - b.clip.start) > 1e-3) return null;
  let tr = p.transitions.find((x) => x.fromClipId === fromId && x.toClipId === toId);
  if (!tr) {
    tr = { id: uid(), trackId: a.track.id, fromClipId: fromId, toClipId: toId, type: "fade", duration: 0.5 };
    p.transitions.push(tr);
  }
  if (patch.type) tr.type = patch.type;
  if (patch.duration != null) tr.duration = Math.min(2, Math.max(0.1, patch.duration));
  return tr.id;
}

export function removeTransition(p: Project, id: string): void {
  p.transitions = p.transitions.filter((t) => t.id !== id);
}

/** Put the same transition on every cut of a track. Returns how many cuts got one. */
export function applyTransitionToAll(p: Project, trackId: string, type: string, duration: number): number {
  const t = p.tracks.find((x) => x.id === trackId);
  if (!t) return 0;
  const s = [...t.clips].sort((a, b) => a.start - b.start);
  let n = 0;
  for (let i = 0; i + 1 < s.length; i++) {
    if (Math.abs(clipEnd(s[i]) - s[i + 1].start) < 1e-3 && setTransition(p, s[i].id, s[i + 1].id, { type, duration })) n++;
  }
  return n;
}

// ---------------------------------------------------------------- tracks
const TRACK_NAMES: Record<TrackType, string> = { video: "Main video", overlay: "Overlay", text: "Text", audio: "Audio", music: "Music" };

export function addTrack(p: Project, type: Exclude<TrackType, "video">): string {
  const n = p.tracks.filter((t) => t.type === type).length;
  const track: Track = {
    id: uid(), type, name: n ? `${TRACK_NAMES[type]} ${n + 1}` : TRACK_NAMES[type],
    muted: false, hidden: false, locked: false, volume: 1, clips: [],
  };
  // Keep tracks grouped by type in the standard order.
  const order: TrackType[] = ["video", "overlay", "text", "audio", "music"];
  const rank = order.indexOf(type);
  let idx = p.tracks.length;
  for (let i = p.tracks.length - 1; i >= 0; i--) {
    if (order.indexOf(p.tracks[i].type) <= rank) {
      idx = i + 1;
      break;
    }
    idx = i;
  }
  p.tracks.splice(idx, 0, track);
  return track.id;
}

export function removeTrack(p: Project, trackId: string): boolean {
  const t = p.tracks.find((x) => x.id === trackId);
  if (!t || (t.type === "video" && p.tracks.filter((x) => x.type === "video").length <= 1)) return false;
  p.tracks = p.tracks.filter((x) => x.id !== trackId);
  pruneTransitions(p);
  return true;
}

// ---------------------------------------------------------------- markers
export function addMarker(p: Project, time: number, label = ""): string | null {
  if (p.markers.some((m) => Math.abs(m.time - time) < 0.01)) return null;
  const m: Marker = { id: uid(), time: round(Math.max(0, time)), label, color: "#f5c542" };
  p.markers.push(m);
  p.markers.sort((a, b) => a.time - b.time);
  return m.id;
}

export function moveMarker(p: Project, id: string, time: number): void {
  const m = p.markers.find((x) => x.id === id);
  if (m) m.time = round(Math.max(0, time));
  p.markers.sort((a, b) => a.time - b.time);
}

export function removeMarker(p: Project, id: string): void {
  p.markers = p.markers.filter((m) => m.id !== id);
}

// ---------------------------------------------------------------- snapping
/** Beat times of a clip's media mapped onto the timeline (only beats inside the clip). */
export function clipBeats(c: Clip, beats: number[] | undefined): number[] {
  if (!beats?.length) return [];
  const speed = c.speed || 1;
  return beats.filter((b) => b >= c.inPoint && b < c.outPoint).map((b) => round(c.start + (b - c.inPoint) / speed));
}

/** Snap candidates: every clip edge (except `excludeId`), playhead, markers, 0 and `extra` (e.g. beats). */
export function snapPoints(p: Project, playhead: number, excludeId?: string, extra: number[] = []): number[] {
  const pts = new Set<number>([0, round(playhead), ...extra]);
  for (const t of p.tracks)
    for (const c of t.clips) {
      if (c.id === excludeId) continue;
      pts.add(round(c.start));
      pts.add(round(clipEnd(c)));
    }
  for (const m of p.markers) pts.add(m.time);
  return [...pts];
}

/** Snap one or more edges (offsets from `t`) to the nearest point within `threshold`. */
export function snap(t: number, points: number[], threshold: number, edges: number[] = [0]): { time: number; at: number | null } {
  let best: { time: number; at: number | null; d: number } = { time: t, at: null, d: threshold };
  for (const off of edges)
    for (const pt of points) {
      const d = Math.abs(t + off - pt);
      if (d < best.d) best = { time: pt - off, at: pt, d };
    }
  return { time: best.time, at: best.at };
}

/** Validate the invariants (used by tests and as a debug check). */
export function overlaps(t: Track): boolean {
  const s = [...t.clips].sort((a, b) => a.start - b.start);
  return s.some((c, i) => i > 0 && c.start < clipEnd(s[i - 1]) - 1e-4);
}
