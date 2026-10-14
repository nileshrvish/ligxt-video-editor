// Tracking jobs: start one on the backend (/api/tracking/start) and put the finished track into the open
// project (even if the panel was closed meanwhile).
import { ApiError } from "../api";
import { CLIENT_ID } from "../session";
import { onJob, toastError, useApp } from "../store/app";
import { useEditor } from "../store/editor";
import { upsertTrack } from "./edit";
import type { MotionTrack } from "./types";

export interface TrackRequest {
  mediaId: string;
  method: "face" | "box" | "sam";
  seed: { t: number; box?: [number, number, number, number]; point?: [number, number]; points?: { x: number; y: number; label: 0 | 1 }[] };
  range: [number, number];
  direction: "both" | "forward" | "backward";
  track?: MotionTrack | null;
  name?: string;
}

async function post<T>(url: string, body: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Ligxt-Client": CLIENT_ID }, body: JSON.stringify(body) });
  } catch {
    throw new ApiError("Cannot reach the Ligxt backend. Is it still running?", 0);
  }
  if (!res.ok) {
    let msg = `${res.status}`;
    try { msg = (await res.json()).detail ?? msg; } catch { /* not JSON */ }
    throw new ApiError(String(msg), res.status);
  }
  return res.json() as Promise<T>;
}

export const findFaces = (mediaId: string, t: number) =>
  post<{ faces: { box: [number, number, number, number]; score: number }[] }>("/api/tracking/faces", { mediaId, t });

/** Running tracking jobs by media id (the panel shows progress and Cancel for them). */
export const running = new Map<string, string>();

/** Start tracking; when the job is done the track is added to (or replaced in) the project. */
export async function startTracking(req: TrackRequest, onDone?: (track: MotionTrack, stats: Record<string, unknown>) => void): Promise<string> {
  const { jobId } = await post<{ jobId: string }>("/api/tracking/start", req);
  const projectId = useEditor.getState().project?.id;
  running.set(req.mediaId, jobId);
  const off = onJob((job) => {
    if (job.id !== jobId || (job.status !== "done" && job.status !== "error" && job.status !== "cancelled")) return;
    off();
    running.delete(req.mediaId);
    if (job.status === "error") return toastError("Tracking failed", new Error(job.error ?? "unknown error"));
    if (job.status !== "done" || !job.result?.track) return;
    const st = useEditor.getState();
    if (st.project?.id !== projectId) return;
    const track = job.result.track as MotionTrack;
    st.commit((d) => upsertTrack(d, track));
    const stats = job.result.stats ?? {};
    const lost = Number(stats.lost ?? 0), low = Number(stats.low ?? 0);
    useApp.getState().toast({
      kind: "info", title: `Tracked: ${track.name}`,
      detail: `${track.data.length} frames${low || lost ? ` · ${low} uncertain, ${lost} lost (marked on the timeline)` : ""}`,
    });
    onDone?.(track, stats);
  });
  return jobId;
}
