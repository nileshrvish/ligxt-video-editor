// Editing helpers for keyframes, motion presets, speed and audio. Pure functions on a (draft) clip or
// project, so the store, the Properties panel, the timeline and the preview handles share them.
import type { Clip, Keyframe, MediaMeta, Project } from "../types";
import { addTrack } from "../timeline/ops";
import { clipDuration, uid } from "../util";
import { valueAt, type Prop } from "./motion";
import * as P from "./presets";

/** Two keyframes closer than this (seconds) are the same keyframe. */
export const KEY_EPS = 1 / 120;

/** The base (un-keyframed) value of a property. */
export function baseValue(c: Clip, prop: Prop): number {
  switch (prop) {
    case "volume": return c.volume;
    case "intensity": return 1;
    case "punch": return 1;
    case "shakeX": case "shakeY": case "shakeR": return 0;
    default: return c.transform[prop];
  }
}

export function hasKeys(c: Clip, prop: Prop): boolean {
  return (c.keyframes?.[prop]?.length ?? 0) > 0;
}

/** The value shown at clip time tau (keyframed or the base value). */
export function propValue(c: Clip, prop: Prop, tau: number): number {
  return valueAt(c.keyframes?.[prop], tau, baseValue(c, prop));
}

export function keyAt(c: Clip, prop: Prop, tau: number): number {
  return (c.keyframes?.[prop] ?? []).findIndex((k) => Math.abs(k.time - tau) < KEY_EPS);
}

const sortKeys = (ks: Keyframe[]) => ks.sort((a, b) => a.time - b.time);

/** Set a property at clip time tau: animated properties get a keyframe there (added or updated),
 *  others change their base value. */
export function setProp(c: Clip, prop: Prop, tau: number, value: number): void {
  if (hasKeys(c, prop)) {
    const ks = c.keyframes[prop];
    const i = keyAt(c, prop, tau);
    if (i >= 0) ks[i].value = value;
    else ks.push({ time: +tau.toFixed(4), value, easing: easingNear(ks, tau) });
    sortKeys(ks);
    return;
  }
  if (prop === "volume") c.volume = value;
  else if (prop === "intensity" || prop === "punch" || prop.startsWith("shake")) return; // keyframe-only channels
  else c.transform[prop as "x"] = value;
}

/** A new keyframe inherits the easing of the keyframe before it (ease in-out by default). */
function easingNear(ks: Keyframe[], tau: number): string {
  const prev = [...ks].reverse().find((k) => k.time <= tau);
  return prev?.easing ?? "inout";
}

/** The diamond button: add a keyframe with the current value, or remove the one at tau. */
export function toggleKey(c: Clip, prop: Prop, tau: number): "added" | "removed" {
  c.keyframes ??= {};
  const ks = (c.keyframes[prop] ??= []);
  const i = keyAt(c, prop, tau);
  if (i >= 0) {
    const removed = ks.splice(i, 1)[0];
    // Removing the last keyframe keeps the value it had as the property's base value.
    if (!ks.length) {
      delete c.keyframes[prop];
      if (prop !== "intensity" && prop !== "punch" && !prop.startsWith("shake")) setProp(c, prop, tau, removed.value);
    }
    return "removed";
  }
  ks.push({ time: +tau.toFixed(4), value: propValue(c, prop, tau), easing: easingNear(ks, tau) });
  sortKeys(ks);
  return "added";
}

export function setKeyEasing(c: Clip, prop: Prop, tau: number, easing: string): boolean {
  const i = keyAt(c, prop, tau);
  if (i < 0) return false;
  c.keyframes[prop][i].easing = easing;
  return true;
}

/** Every keyframe time on the clip (for the timeline diamonds and jumping), with its properties. */
export function keyTimes(c: Clip): { time: number; props: string[] }[] {
  const out: { time: number; props: string[] }[] = [];
  for (const [prop, ks] of Object.entries(c.keyframes ?? {})) {
    for (const k of ks ?? []) {
      const hit = out.find((o) => Math.abs(o.time - k.time) < KEY_EPS);
      if (hit) hit.props.includes(prop) || hit.props.push(prop);
      else out.push({ time: k.time, props: [prop] });
    }
  }
  return out.sort((a, b) => a.time - b.time);
}

/** Move every keyframe at `from` (any property) to `to`, clamped to the clip. */
export function retimeKeys(c: Clip, from: number, to: number): void {
  const D = clipDuration(c);
  const t = Math.max(0, Math.min(D, to));
  for (const ks of Object.values(c.keyframes ?? {})) {
    for (const k of ks ?? []) if (Math.abs(k.time - from) < KEY_EPS) k.time = +t.toFixed(4);
    ks && sortKeys(ks);
  }
}

/** Next/previous keyframe time from tau (for one property, or any). */
export function jumpKey(c: Clip, tau: number, dir: 1 | -1, prop?: Prop): number | null {
  const times = prop ? (c.keyframes?.[prop] ?? []).map((k) => k.time) : keyTimes(c).map((k) => k.time);
  const cand = times.filter((t) => (dir > 0 ? t > tau + KEY_EPS : t < tau - KEY_EPS)).sort((a, b) => a - b);
  if (!cand.length) return null;
  return dir > 0 ? cand[0] : cand[cand.length - 1];
}

export function clearMotion(c: Clip): void {
  for (const p of ["x", "y", "scale", "rotation", "punch", "shakeX", "shakeY", "shakeR"]) delete c.keyframes?.[p];
}

// ---------------------------------------------------------------- presets
export type MotionPreset =
  | { kind: "zoom"; dir: "in" | "out"; amount?: number; focus?: P.Focus }
  | { kind: "pan"; dir: "left" | "right" | "up" | "down"; zoom?: number }
  | { kind: "punch"; at: number; amount?: number }
  | { kind: "shake"; at: number; amount?: number; duration?: number };

/** The picture's size on the canvas at scale 1 (fit or fill), for pans. */
export function pictureSize(c: Clip, m: MediaMeta | undefined, W: number, H: number, fill: boolean): [number, number] {
  const sw = m?.width || W, sh = m?.height || H;
  const cr = c.transform.crop;
  const cw = sw * (1 - cr.left - cr.right), ch = sh * (1 - cr.top - cr.bottom);
  if (fill) return cw / ch > W / H ? [(cw * H) / ch, H] : [W, (ch * W) / cw];
  const f = Math.min(W / cw, H / ch);
  return [cw * f, ch * f];
}

export function applyPreset(c: Clip, preset: MotionPreset, ctx: { W: number; H: number; fps: number; fill: boolean; media?: MediaMeta }): void {
  c.keyframes ??= {};
  const D = clipDuration(c);
  if (preset.kind === "zoom" || preset.kind === "pan") {
    for (const p of ["x", "y", "scale"]) delete c.keyframes[p];
    const k = preset.kind === "zoom"
      ? P.kenBurns(D, preset.dir, preset.amount ?? 0.12, preset.focus)
      : P.pan(D, preset.dir, ctx.W, ctx.H, ...pictureSize(c, ctx.media, ctx.W, ctx.H, ctx.fill), preset.zoom ?? 1.12);
    for (const [p, ks] of Object.entries(k)) if (ks.length) c.keyframes[p] = ks;
  } else if (preset.kind === "punch") {
    c.keyframes.punch = P.punchIn(c.keyframes.punch, preset.at, preset.amount ?? 0.1);
  } else {
    const s = P.shake({ x: c.keyframes.shakeX, y: c.keyframes.shakeY, r: c.keyframes.shakeR }, preset.at, preset.amount ?? 14, preset.duration ?? 0.35, ctx.fps);
    Object.assign(c.keyframes, s);
  }
}

// ---------------------------------------------------------------- speed
/** Change constant speed, keeping keyframes at the same place in the footage. */
export function setSpeed(c: Clip, speed: number): void {
  const old = clipDuration(c);
  c.speed = Math.max(0.25, Math.min(4, speed));
  rescaleKeys(c, old, clipDuration(c));
}

export function setRamp(c: Clip, points: { u: number; speed: number }[] | null, preset?: string): void {
  const old = clipDuration(c);
  c.speedRamp = points ? { points, preset } : null;
  rescaleKeys(c, old, clipDuration(c));
}

function rescaleKeys(c: Clip, oldD: number, newD: number): void {
  if (oldD <= 0 || Math.abs(oldD - newD) < 1e-9) return;
  for (const ks of Object.values(c.keyframes ?? {})) for (const k of ks ?? []) k.time = +((k.time * newD) / oldD).toFixed(4);
}

// ---------------------------------------------------------------- audio
/** Detach a video clip's sound onto an audio track (a linked copy playing only its audio); the
 *  video clip is muted. Returns the new clip id. */
export function detachAudio(p: Project, clipId: string): string | null {
  const track = p.tracks.find((t) => t.clips.some((c) => c.id === clipId));
  const c = track?.clips.find((x) => x.id === clipId);
  if (!track || !c || track.type === "audio" || track.type === "music") return null;
  let audio = p.tracks.find((t) => t.type === "audio" && !t.locked && fits(t.clips, c.start, clipDuration(c)));
  if (!audio) {
    const id = addTrack(p, "audio");
    audio = p.tracks.find((t) => t.id === id)!;
  }
  const copy: Clip = JSON.parse(JSON.stringify(c));
  copy.id = uid();
  copy.filters = [];
  copy.transform = { ...copy.transform, x: 0, y: 0, scale: 1, rotation: 0, opacity: 1, flipH: false, flipV: false };
  for (const k of ["x", "y", "scale", "rotation", "opacity", "punch", "shakeX", "shakeY", "shakeR", "intensity"]) delete copy.keyframes?.[k];
  copy.detachedFrom = c.id;
  audio.clips.push(copy);
  audio.clips.sort((a, b) => a.start - b.start);
  c.muted = true;
  return copy.id;
}

const fits = (clips: Clip[], start: number, dur: number) =>
  clips.every((x) => x.start + clipDuration(x) <= start + 1e-6 || x.start >= start + dur - 1e-6);

/** Place a sound effect so its peak lands on `at` (a cut or a beat), on a free audio track. */
export function placeSfx(p: Project, m: MediaMeta, at: number, volume = 0.35): string {
  const dur = m.duration;
  const peak = m.sfxPeak ?? 0;
  let start = at - peak, inPoint = 0;
  if (start < 0) { inPoint = -start; start = 0; }
  const len = dur - inPoint;
  let track = p.tracks.find((t) => t.type === "audio" && !t.locked && t.name.startsWith("SFX") && fits(t.clips, start, len));
  if (!track) {
    const id = addTrack(p, "audio");
    track = p.tracks.find((t) => t.id === id)!;
    const n = p.tracks.filter((t) => t.name.startsWith("SFX")).length;
    track.name = n ? `SFX ${n + 1}` : "SFX";
  }
  const clip: Clip = {
    id: uid(), mediaId: m.id, start: +start.toFixed(4), inPoint: +inPoint.toFixed(4), outPoint: dur, speed: 1, reverse: false,
    freezeFrame: null, volume, muted: false, fadeIn: 0, fadeOut: 0, fit: null,
    transform: { x: 0, y: 0, scale: 1, rotation: 0, flipH: false, flipV: false, opacity: 1, crop: { left: 0, top: 0, right: 0, bottom: 0 } },
    keyframes: {}, filters: [], text: null,
  };
  track.clips.push(clip);
  track.clips.sort((a, b) => a.start - b.start);
  if (!p.media.includes(m.id)) p.media.push(m.id);
  return clip.id;
}
