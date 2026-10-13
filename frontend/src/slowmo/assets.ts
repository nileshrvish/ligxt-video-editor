// Retimed slow-motion videos for the preview (POST /api/slowmo/prepare): low-res for playback, and the
// full-resolution version when Cutout needs the retimed frames. Follows the job and tells the preview.
import { onJob } from "../store/app";
import type { Clip, MediaMeta } from "../types";

export interface SlowInfo {
  state: "ready"; url: string; fps: number; offset: number; frames: number; quality: string;
  interpolated?: number | null; cutPairs?: number[]; motionP95?: number | null; aiFps?: number | null;
  peakVram?: number | null; device?: string | null; warning?: string | null; mediaId?: string;
}
export interface SlowAsset {
  status: "checking" | "running" | "ready" | "error" | "notNeeded";
  info?: SlowInfo; jobId?: string; progress?: number; message?: string; error?: string;
}

const byKey = new Map<string, SlowAsset>();
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((fn) => fn());
export function onSlowAssets(fn: () => void): () => void {
  listeners.add(fn);
  return () => void listeners.delete(fn);
}

function timing(c: Clip) {
  return { start: c.start, inPoint: c.inPoint, outPoint: c.outPoint, speed: c.speed, speedRamp: c.speedRamp ?? null,
    reverse: c.reverse, freezeFrame: c.freezeFrame, slowmo: c.slowmo ?? null };
}

export function slowKey(c: Clip, fps: number, before: number, after: number, res: string): string {
  return JSON.stringify([c.mediaId, timing(c), fps, +before.toFixed(4), +after.toFixed(4), res]);
}

/** The retimed video for a clip (starts making it on first use). */
export function slowAsset(c: Clip, m: MediaMeta | undefined, fps: number, before = 0, after = 0, res: "preview" | "full" = "preview"): SlowAsset | null {
  if (!m || m.type !== "video" || !c.mediaId) return null;
  const key = slowKey(c, fps, before, after, res);
  let a = byKey.get(key);
  if (a) return a;
  a = { status: "checking" };
  byKey.set(key, a);
  const entry = a;
  fetch("/api/slowmo/prepare", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mediaId: c.mediaId, clip: timing(c), fps, before, after, res, start: true }),
  })
    .then(async (r) => {
      const b = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(b.detail ?? `slow motion ${r.status}`);
      return b;
    })
    .then((b) => {
      if (b.state === "ready") Object.assign(entry, { status: "ready", info: b });
      else if (b.state === "notNeeded") entry.status = "notNeeded";
      else {
        Object.assign(entry, { status: "running", jobId: b.jobId, progress: b.progress ?? 0, message: b.message });
        const off = onJob((j) => {
          if (j.id !== b.jobId) return;
          if (j.status === "running" || j.status === "queued") {
            Object.assign(entry, { status: "running", progress: j.progress, message: j.status === "queued" ? "Waiting for the GPU" : j.message });
            return notify();
          }
          off();
          if (j.status === "done" && j.result?.state === "ready") Object.assign(entry, { status: "ready", info: j.result });
          else Object.assign(entry, { status: "error", error: j.status === "cancelled" ? "Cancelled" : j.error ?? "Failed" });
          notify();
        });
      }
    })
    .catch((e: Error) => Object.assign(entry, { status: "error", error: e.message }))
    .finally(notify);
  return a;
}

export function retrySlow(c: Clip, fps: number, before = 0, after = 0, res: "preview" | "full" = "preview") {
  byKey.delete(slowKey(c, fps, before, after, res));
  notify();
}
