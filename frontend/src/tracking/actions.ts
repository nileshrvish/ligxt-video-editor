// Starting tracking from the editor: the clip's source span, the frame at the playhead, the chosen tool.
import { clipDuration, srcTime } from "../motion/motion";
import { toastError, useApp } from "../store/app";
import { useEditor } from "../store/editor";
import * as ops from "../timeline/ops";
import type { Clip } from "../types";
import { findFaces, startTracking, type TrackRequest } from "./api";
import { nextName, trackById } from "./edit";
import type { MotionTrack } from "./types";

/** The clip's source span (seconds), whatever its speed, ramp or direction. */
export function sourceRange(clip: Clip): [number, number] {
  const D = clipDuration(clip);
  const a = srcTime(clip, 0).src, b = srcTime(clip, Math.max(0, D - 1e-6)).src;
  return [Math.max(0, Math.min(a, b, clip.inPoint)), Math.max(a, b, clip.outPoint)];
}

/** The source time on screen at the playhead (clamped into the clip). */
export function seedTime(clip: Clip): number {
  const t = useEditor.getState().playhead;
  const tau = Math.min(Math.max(0, t - clip.start), Math.max(0, clipDuration(clip) - 1e-6));
  return srcTime(clip, tau).src;
}

const dirOf = () => useEditor.getState().trackUi.dir ?? "both";

async function run(clip: Clip, req: Omit<TrackRequest, "mediaId" | "range" | "direction"> & { direction?: TrackRequest["direction"] }) {
  if (!clip.mediaId) return;
  const setUi = useEditor.getState().setTrackUi;
  try {
    await startTracking({ mediaId: clip.mediaId, range: sourceRange(clip), direction: req.direction ?? dirOf(), ...req } as TrackRequest,
      (track) => setUi({ clipId: clip.id, trackId: track.id, fix: null })); // the fixed box stays until the new track is in
    setUi({ tool: "none", faces: null, points: [] });
  } catch (e) {
    toastError("Could not start tracking", e);
  }
}

/** Track a face: the only one on the frame, or the one picked (box index), or let the user pick. */
export async function trackFace(clip: Clip, pick?: number) {
  const p = useEditor.getState().project;
  if (!clip.mediaId || !p) return;
  const t = seedTime(clip);
  const setUi = useEditor.getState().setTrackUi;
  let faces = useEditor.getState().trackUi.faces;
  if (pick == null) {
    try {
      faces = (await findFaces(clip.mediaId, t)).faces;
    } catch (e) {
      return toastError("Finding faces failed", e);
    }
    if (!faces.length) {
      useApp.getState().toast({ kind: "warning", title: "No face on this frame", detail: "Move the playhead to a frame where the face is visible, or draw a box around it." });
      return;
    }
    if (faces.length > 1) {
      setUi({ clipId: clip.id, tool: "face", faces });
      useApp.getState().toast({ kind: "info", title: `${faces.length} faces found`, detail: "Click the face to follow on the preview." });
      return;
    }
    pick = 0;
  }
  const f = faces![pick];
  await run(clip, { method: "face", seed: { t, box: f.box }, name: nextName(p, "face") });
}

export async function trackBox(clip: Clip, box: [number, number, number, number]) {
  const p = useEditor.getState().project;
  if (!p) return;
  await run(clip, { method: "box", seed: { t: seedTime(clip), box }, name: nextName(p, "object") });
}

export async function trackClicks(clip: Clip, points: { x: number; y: number; label: 0 | 1 }[]) {
  const p = useEditor.getState().project;
  if (!p || !points.some((q) => q.label === 1)) return;
  await run(clip, { method: "sam", seed: { t: seedTime(clip), points }, name: nextName(p, "object") });
}

/** Re-track from the fixed box at the playhead, forwards or backwards (the rest of the track is kept). */
export async function retrack(clip: Clip, trackId: string, direction: "forward" | "backward") {
  const st = useEditor.getState();
  const track = st.project ? trackById(st.project, trackId) : undefined;
  const fix = st.trackUi.fix;
  if (!track || !fix) return;
  const method = track.method === "face" ? "face" : track.method === "sam" ? "sam" : "box";
  await run(clip, { method, seed: { t: fix.t, box: fix.box }, direction, track: track as MotionTrack, name: track.name });
}

/** The selected clip if it shows a video (tracking works on video clips). */
export function hostOf(clipId: string | null): Clip | null {
  const p = useEditor.getState().project;
  if (!p || !clipId) return null;
  return ops.findClip(p, clipId)?.clip ?? null;
}
