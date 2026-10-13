import type { Clip, MediaMeta, Project, Track } from "./types";
import { clipDuration as motionClipDuration } from "./motion/motion";
import { current, isDraft } from "immer";

/** Deep copy that also works on immer drafts (structuredClone can't copy a draft proxy). */
export function deepClone<T>(x: T): T {
  return structuredClone(isDraft(x) ? current(x) : x);
}

export const uid = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) => b.toString(16).padStart(2, "0")).join("");

/** Timeline length of a clip (speed, ramps and freeze frames included). */
export function clipDuration(c: Clip): number {
  return Math.max(0, motionClipDuration(c));
}

export function clipEnd(c: Clip): number {
  return c.start + clipDuration(c);
}

export function trackEnd(t: Track): number {
  return t.clips.reduce((m, c) => Math.max(m, clipEnd(c)), 0);
}

/** The main video track defines the video length (music running longer is cut on export). */
export function projectDuration(p: Project): number {
  const main = p.tracks.find((t) => t.type === "video");
  const mainEnd = main ? trackEnd(main) : 0;
  return mainEnd > 0 ? mainEnd : p.tracks.reduce((m, t) => Math.max(m, trackEnd(t)), 0);
}

/** Furthest end of any clip (for sizing the timeline). */
export function contentEnd(p: Project): number {
  return p.tracks.reduce((m, t) => Math.max(m, trackEnd(t)), 0);
}

/** 75.4 -> "1:15.4" ; with frames=true -> "00:01:15:12" style timecode */
export function fmtTime(sec: number, fps?: number): string {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  if (fps) {
    const whole = Math.floor(s);
    const f = Math.floor((s - whole) * fps + 1e-6);
    return `${String(m).padStart(2, "0")}:${String(whole).padStart(2, "0")}:${String(f).padStart(2, "0")}`;
  }
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

export function fmtDuration(sec: number): string {
  if (!sec) return "—";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  return `${m}:${String(Math.round(sec - m * 60)).padStart(2, "0")}`;
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export function fmtEta(sec: number | null): string {
  if (sec == null || !isFinite(sec)) return "estimating…";
  if (sec < 60) return `${Math.ceil(sec)}s left`;
  return `${Math.floor(sec / 60)}m ${Math.ceil(sec % 60)}s left`;
}

export function timeAgo(ts: number): string {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

export function defaultDurationFor(m: MediaMeta): number {
  return m.type === "image" ? 3 : m.duration;
}

export function newClip(m: MediaMeta, start: number): Clip {
  return {
    id: uid(),
    mediaId: m.id,
    start,
    inPoint: 0,
    outPoint: defaultDurationFor(m),
    speed: 1,
    reverse: false,
    freezeFrame: null,
    volume: 1,
    muted: false,
    fadeIn: 0,
    fadeOut: 0,
    fit: null,
    transform: { x: 0, y: 0, scale: 1, rotation: 0, flipH: false, flipV: false, opacity: 1, crop: { left: 0, top: 0, right: 0, bottom: 0 } },
    keyframes: {},
    filters: [],
    text: null,
  };
}

export const ASPECTS: Record<string, [number, number]> = {
  "9:16": [1080, 1920],
  "1:1": [1080, 1080],
  "4:5": [1080, 1350],
  "16:9": [1920, 1080],
};

export const TRACK_COLORS: Record<string, string> = {
  video: "var(--clip-video)",
  overlay: "var(--clip-overlay)",
  text: "var(--clip-text)",
  audio: "var(--clip-audio)",
  music: "var(--clip-music)",
};
