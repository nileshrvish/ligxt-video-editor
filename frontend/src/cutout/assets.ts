// Cutout mattes for the preview: asks the backend (POST /api/cutout/prepare) for a clip's refined matte,
// follows the analysis job, and tells the preview when something changed. Videos get one proxy with the
// picture on the left and the matte on the right (they can't drift apart); photos get a matte image.
import { onJob } from "../store/app";
import type { Clip, MediaMeta } from "../types";
import { normalizeCutout, type Cutout } from "./model";

export interface CutoutInfo {
  state: "ready"; image: boolean; fps: number; offset: number; frames: number;
  preview: string; matte: string | null; previewWidth?: number | null; previewHeight?: number | null;
  coverage: number; emptyFrames: number; noSubject: boolean; model: string; device?: string | null;
  warning?: string | null; analysisFps?: number | null; peakVram?: number | null;
}

export interface CutoutAsset {
  status: "checking" | "running" | "ready" | "error" | "missing" | "needsPoints";
  info?: CutoutInfo;
  jobId?: string;
  progress?: number;
  message?: string;
  error?: string;
  /** First use of a model: how much it downloads. */
  downloadBytes?: number;
  modelName?: string;
  /** Photos: the matte image (preview size). */
  img?: HTMLImageElement;
}

const byKey = new Map<string, CutoutAsset>();
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((fn) => fn());

export function onCutoutAssets(fn: () => void): () => void {
  listeners.add(fn);
  return () => void listeners.delete(fn);
}

const r3 = (x: number | null | undefined) => (x == null ? null : Math.round(x * 1000) / 1000);

/** What decides the matte: the media, model, analysis detail, refine settings and the source span. */
export function cutoutKey(clip: Clip, cut: Cutout): string {
  const timing = { i: r3(clip.inPoint), o: r3(clip.outPoint), s: r3(clip.speed), r: !!clip.reverse, f: r3(clip.freezeFrame), sr: clip.speedRamp ?? null };
  return JSON.stringify([clip.mediaId, cut.mode, cut.mode === "object" ? cut.points : cut.model, cut.refine, timing]);
}

function timingOf(clip: Clip) {
  return { inPoint: clip.inPoint, outPoint: clip.outPoint, speed: clip.speed, reverse: clip.reverse, freezeFrame: clip.freezeFrame, speedRamp: clip.speedRamp ?? null };
}

function request(key: string, clip: Clip, cut: Cutout, start: boolean) {
  const entry = byKey.get(key)!;
  fetch("/api/cutout/prepare", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mediaId: clip.mediaId, cutout: { model: cut.model, mode: cut.mode, points: cut.points, refine: cut.refine, enabled: true },
      clip: timingOf(clip), start }),
  })
    .then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail ?? `cutout ${r.status}`);
      return body;
    })
    .then((b) => {
      if (b.state === "ready") return ready(entry, b as CutoutInfo);
      entry.status = b.state === "running" ? "running" : b.state === "error" ? "error" : b.state === "needsPoints" ? "needsPoints" : "missing";
      entry.jobId = b.jobId;
      entry.progress = b.progress ?? 0;
      entry.message = b.message;
      entry.error = b.error;
      entry.downloadBytes = b.downloadBytes;
      entry.modelName = b.modelName;
      if (b.jobId) follow(key, b.jobId);
    })
    .catch((e: Error) => {
      entry.status = "error";
      entry.error = e.message;
    })
    .finally(notify);
}

async function ready(entry: CutoutAsset, info: CutoutInfo) {
  if (info.image && info.preview) {
    const img = new Image();
    img.src = info.preview;
    await img.decode().catch(() => void 0);
    entry.img = img;
  }
  entry.info = info;
  entry.status = "ready";
  entry.error = undefined;
  notify();
}

function follow(key: string, jobId: string) {
  const off = onJob((j) => {
    if (j.id !== jobId) return;
    const entry = byKey.get(key);
    if (!entry) return off();
    if (j.status === "running" || j.status === "queued") {
      entry.status = "running";
      entry.progress = j.progress;
      entry.message = j.status === "queued" ? "Waiting for the GPU" : j.message;
      notify();
      return;
    }
    off();
    if (j.status === "done" && j.result?.state === "ready") void ready(entry, j.result as CutoutInfo);
    else {
      entry.status = "error";
      entry.error = j.status === "cancelled" ? "Cancelled" : j.error ?? "Analysis failed";
      notify();
    }
  });
}

/** The cutout asset for a clip; asks the backend on first use (start = run the analysis if needed). */
export function cutoutAsset(clip: Clip, m: MediaMeta | undefined, start = true): CutoutAsset | null {
  if (!m || !clip.mediaId || (m.type !== "video" && m.type !== "image")) return null;
  const cut = normalizeCutout(clip.cutout);
  const key = cutoutKey(clip, cut);
  let a = byKey.get(key);
  if (!a) {
    a = { status: "checking" };
    byKey.set(key, a);
    request(key, clip, cut, start);
  } else if (start && (a.status === "missing")) {
    a.status = "checking";
    request(key, clip, cut, true);
  }
  return a;
}

/** Forget a failed/cancelled analysis so the next use tries again. */
export function retryCutout(clip: Clip) {
  byKey.delete(cutoutKey(clip, normalizeCutout(clip.cutout)));
  notify();
}

/** Drop everything (e.g. after the cache was cleared in Settings). */
export function resetCutoutAssets() {
  byKey.clear();
  notify();
}
