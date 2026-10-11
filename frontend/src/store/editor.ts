import { create } from "zustand";
import { api, uploadMedia } from "../api";
import type { Clip, MediaMeta, Project, TrackType } from "../types";
import * as ops from "../timeline/ops";
import { cuts as cutPairs } from "../timeline/transitions";
import { clipEnd, newClip, trackEnd } from "../util";
import { toastError, useApp } from "./app";

export type Selection =
  | { kind: "none" }
  | { kind: "media"; id: string }
  | { kind: "clip"; clipId: string }
  | { kind: "marker"; id: string }
  | { kind: "transition"; id: string };

export interface Upload {
  key: string;
  name: string;
  progress: number;
  error?: string;
  abort: () => void;
}

type SaveState = "saved" | "dirty" | "saving" | "error";

interface EditorState {
  project: Project | null;
  media: Record<string, MediaMeta>;
  selection: Selection;
  playhead: number;
  playing: boolean;
  /** Playback rate while playing: 1 = normal; J/L shuttle uses ±1, ±2, ±4, ±8. */
  rate: number;
  pxPerSec: number;
  uploads: Upload[];
  saveState: SaveState;
  lastSaved: number | null;
  past: Project[];
  future: Project[];
  gestureStart: Project | null;

  load: (id: string) => Promise<void>;
  close: () => void;
  /** Apply a change to the project. Pushes an undo step unless `undoable` is false. */
  commit: (fn: (draft: Project) => void, undoable?: boolean) => void;
  /** Continuous edits (drags, sliders): call beginGesture, commit(fn, false) repeatedly, then endGesture
   *  to record the whole gesture as a single undo step. */
  beginGesture: () => void;
  endGesture: () => void;
  undo: () => void;
  redo: () => void;
  select: (s: Selection) => void;
  setPlayhead: (t: number) => void;
  setPlaying: (p: boolean) => void;
  /** J / K / L shuttle. */
  shuttle: (key: "j" | "k" | "l") => void;
  setZoom: (pxPerSec: number) => void;
  importFiles: (files: File[]) => Promise<void>;
  refreshMedia: (id: string) => Promise<void>;
  removeMedia: (id: string) => void;
  /** Add media to a track (default: main video, or music for audio files) at `at` (default: end). */
  addToTimeline: (mediaId: string, trackId?: string, at?: number) => void;
  deleteSelectedClip: (ripple?: boolean) => void;
  splitAtPlayhead: () => void;
  /** Add (or select) a transition on the cut between two clips, using the last-used type/length. */
  addTransition: (fromId: string, toId: string) => void;
  /** T key: add a transition at the cut nearest the playhead on the main track. */
  addTransitionNearPlayhead: () => void;
  lastTransition: { type: string; duration: number };
  setLastTransition: (t: { type: string; duration: number }) => void;
  duplicateSelected: () => void;
  addMarkerAtPlayhead: () => void;
  snapping: boolean;
  setSnapping: (on: boolean) => void;
  /** During a gesture: replace the project with fn(copy of the gesture's starting state). */
  fromBase: (fn: (draft: Project) => void) => void;
  mediaDuration: ops.MediaDuration;
  save: (kind?: "manual" | "auto") => Promise<boolean>;
}

const HISTORY_LIMIT = 100;

export const useEditor = create<EditorState>((set, get) => ({
  project: null,
  media: {},
  selection: { kind: "none" },
  playhead: 0,
  playing: false,
  rate: 1,
  pxPerSec: 60,
  uploads: [],
  saveState: "saved",
  lastSaved: null,
  past: [],
  future: [],
  gestureStart: null,
  snapping: readSnapping(),
  lastTransition: { type: "fade", duration: 0.5 },

  load: async (id) => {
    const project = await api.getProject(id);
    const { media, missing } = project.media.length ? await api.getMedia(project.media) : { media: [], missing: [] };
    if (missing.length) {
      useApp.getState().toast({
        kind: "warning",
        title: `${missing.length} media file(s) are missing`,
        detail: "They were deleted from the media folder. Clips that use them cannot be exported.",
      });
    }
    set({
      project,
      media: Object.fromEntries(media.map((m) => [m.id, m])),
      selection: { kind: "none" },
      playhead: 0,
      playing: false,
      saveState: "saved",
      lastSaved: project.updated,
      past: [],
      future: [],
      uploads: [],
    });
  },

  close: () => set({ project: null, media: {}, past: [], future: [], selection: { kind: "none" }, playing: false }),

  commit: (fn, undoable = true) => {
    const cur = get().project;
    if (!cur) return;
    const next = structuredClone(cur);
    fn(next);
    set({
      project: next,
      saveState: "dirty",
      ...(undoable ? { past: [...get().past, cur].slice(-HISTORY_LIMIT), future: [] } : {}),
    });
  },

  beginGesture: () => set({ gestureStart: get().project }),

  endGesture: () => {
    const start = get().gestureStart;
    if (start && start !== get().project) {
      set({ past: [...get().past, start].slice(-HISTORY_LIMIT), future: [] });
    }
    set({ gestureStart: null });
  },

  undo: () => {
    const { past, project, future } = get();
    if (!past.length || !project) return;
    set({ project: past[past.length - 1], past: past.slice(0, -1), future: [project, ...future], saveState: "dirty" });
    get().select(validSelection(get()));
  },

  redo: () => {
    const { past, project, future } = get();
    if (!future.length || !project) return;
    set({ project: future[0], future: future.slice(1), past: [...past, project], saveState: "dirty" });
    get().select(validSelection(get()));
  },

  select: (selection) => set({ selection }),
  setPlayhead: (t) => set({ playhead: Math.max(0, t) }),
  setPlaying: (playing) => set(playing ? { playing } : { playing, rate: 1 }),

  shuttle: (key) => {
    const { playing, rate } = get();
    if (key === "k") return set({ playing: false, rate: 1 });
    const dir = key === "l" ? 1 : -1;
    const next = playing && Math.sign(rate) === dir ? Math.min(8, Math.abs(rate) * 2) * dir : dir;
    set({ playing: true, rate: next });
  },
  setZoom: (pxPerSec) => set({ pxPerSec: Math.min(400, Math.max(8, pxPerSec)) }),

  importFiles: async (files) => {
    const run = async (file: File) => {
      const key = `${file.name}-${file.size}-${Math.random()}`;
      const { promise, abort } = uploadMedia(file, (p) =>
        set({ uploads: get().uploads.map((u) => (u.key === key ? { ...u, progress: p } : u)) }),
      );
      set({ uploads: [...get().uploads, { key, name: file.name, progress: 0, abort }] });
      try {
        const { media } = await promise;
        set({ media: { ...get().media, [media.id]: media } });
        get().commit((p) => {
          if (!p.media.includes(media.id)) p.media.push(media.id);
        }, false);
        set({ uploads: get().uploads.filter((u) => u.key !== key) });
      } catch (e) {
        set({ uploads: get().uploads.filter((u) => u.key !== key) });
        toastError(`Could not import ${file.name}`, e);
      }
    };
    // Two uploads at a time keeps the disk busy without starving the proxy jobs.
    const queue = [...files];
    const worker = async () => {
      for (let f = queue.shift(); f; f = queue.shift()) await run(f);
    };
    await Promise.all([worker(), worker()]);
  },

  refreshMedia: async (id) => {
    try {
      const m = await api.getMediaOne(id);
      if (get().project?.media.includes(id)) set({ media: { ...get().media, [id]: m } });
    } catch {
      /* media deleted meanwhile */
    }
  },

  removeMedia: (id) => {
    const p = get().project;
    if (!p) return;
    const used = p.tracks.some((t) => t.clips.some((c) => c.mediaId === id));
    get().commit((d) => {
      d.media = d.media.filter((m) => m !== id);
      for (const t of d.tracks) t.clips = t.clips.filter((c) => c.mediaId !== id);
    });
    const sel = get().selection;
    if (sel.kind === "media" && sel.id === id) set({ selection: { kind: "none" } });
    if (used) useApp.getState().toast({ kind: "info", title: "Removed media and its clips", detail: "Ctrl+Z to undo." });
  },

  addToTimeline: (mediaId, trackId, at) => {
    const m = get().media[mediaId];
    const p = get().project;
    if (!m || !p) return;
    const warn = (title: string) => useApp.getState().toast({ kind: "warning", title });
    const track = trackId
      ? p.tracks.find((t) => t.id === trackId)
      : p.tracks.find((t) => t.type === (m.type === "audio" ? "music" : "video"));
    if (!track) return warn(`There is no ${m.type === "audio" ? "music" : "main video"} track. Add one first.`);
    if (track.locked) return warn(`“${track.name}” is locked. Unlock it first.`);
    if (track.type === "text") return warn("Text clips are added from the Text panel (Phase 6).");
    if ((track.type === "video" || track.type === "overlay") && m.type === "audio") return warn("Audio files go on an Audio or Music track.");
    if ((track.type === "audio" || track.type === "music") && !m.hasAudio) return warn(`${m.name} has no audio.`);
    const clip: Clip = newClip(m, 0);
    get().commit((d) => ops.insertClip(d, track.id, clip, at ?? trackEnd(track)));
    set({ selection: { kind: "clip", clipId: clip.id } });
  },

  deleteSelectedClip: (ripple = false) => {
    const s = get().selection;
    if (s.kind === "transition") {
      get().commit((d) => ops.removeTransition(d, s.id));
      set({ selection: { kind: "none" } });
      return;
    }
    if (s.kind === "marker") {
      get().commit((d) => ops.removeMarker(d, s.id));
      set({ selection: { kind: "none" } });
      return;
    }
    if (s.kind !== "clip") return;
    const hit = get().project && ops.findClip(get().project!, s.clipId);
    if (hit?.track.locked) return void useApp.getState().toast({ kind: "warning", title: `“${hit.track.name}” is locked.` });
    get().commit((d) => ops.deleteClip(d, s.clipId, ripple));
    set({ selection: { kind: "none" } });
  },

  splitAtPlayhead: () => {
    const { project, playhead, selection } = get();
    if (!project) return;
    let made: string[] = [];
    const sel = selection.kind === "clip" ? ops.findClip(project, selection.clipId) : null;
    const inSel = sel && playhead > sel.clip.start && playhead < clipEnd(sel.clip);
    get().commit((d) => {
      made = inSel ? [ops.splitClip(d, sel!.clip.id, playhead)].filter((x): x is string => !!x) : ops.splitAll(d, playhead);
    });
    if (!made.length) {
      // Nothing changed: drop the empty undo step.
      set({ past: get().past.slice(0, -1) });
      useApp.getState().toast({ kind: "info", title: "Nothing to split here", detail: "Move the playhead over a clip (not at its edge)." });
    }
  },

  addTransition: (fromId, toId) => {
    const existing = get().project?.transitions.find((t) => t.fromClipId === fromId && t.toClipId === toId);
    if (existing) return set({ selection: { kind: "transition", id: existing.id } });
    const { type, duration } = get().lastTransition;
    let id: string | null = null;
    get().commit((d) => void (id = ops.setTransition(d, fromId, toId, { type, duration })));
    if (id) set({ selection: { kind: "transition", id } });
    else set({ past: get().past.slice(0, -1) });
  },

  addTransitionNearPlayhead: () => {
    const { project, playhead } = get();
    const main = project?.tracks.find((t) => t.type === "video");
    if (!main) return;
    const pairs = cutPairs(main.clips);
    if (!pairs.length) {
      useApp.getState().toast({ kind: "info", title: "No cuts to put a transition on", detail: "Place two clips next to each other on the main track." });
      return;
    }
    const [a, b] = pairs.reduce((best, pr) => (Math.abs(pr[1].start - playhead) < Math.abs(best[1].start - playhead) ? pr : best));
    get().addTransition(a.id, b.id);
  },

  setLastTransition: (lastTransition) => set({ lastTransition }),

  duplicateSelected: () => {
    const s = get().selection;
    if (s.kind !== "clip") return;
    let id: string | null = null;
    get().commit((d) => void (id = ops.duplicateClip(d, s.clipId)));
    if (id) set({ selection: { kind: "clip", clipId: id } });
  },

  addMarkerAtPlayhead: () => {
    let id: string | null = null;
    get().commit((d) => void (id = ops.addMarker(d, get().playhead)));
    if (id) set({ selection: { kind: "marker", id } });
    else set({ past: get().past.slice(0, -1) });
  },

  setSnapping: (snapping) => {
    try {
      localStorage.setItem("ligxt.snapping", snapping ? "1" : "0");
    } catch {
      /* storage unavailable */
    }
    set({ snapping });
  },

  fromBase: (fn) => {
    const base = get().gestureStart ?? get().project;
    if (!base) return;
    const next = structuredClone(base);
    fn(next);
    set({ project: next, saveState: "dirty" });
  },

  mediaDuration: (id) => {
    const m = id ? get().media[id] : undefined;
    return !m || m.type === "image" ? Infinity : m.duration;
  },

  save: async (kind = "manual") => {
    const p = get().project;
    if (!p) return false;
    set({ saveState: "saving" });
    try {
      const r = kind === "auto" ? await api.autosave(p) : await api.saveProject(p);
      // Only mark clean if nothing changed while the request was in flight.
      set({ saveState: get().project === p ? "saved" : "dirty", lastSaved: r.updated });
      return true;
    } catch (e) {
      set({ saveState: "error" });
      toastError(kind === "auto" ? "Autosave failed" : "Save failed", e);
      return false;
    }
  },
}));

function readSnapping(): boolean {
  try {
    return localStorage.getItem("ligxt.snapping") !== "0";
  } catch {
    return true;
  }
}

function validSelection(s: EditorState): Selection {
  const sel = s.selection;
  if (sel.kind === "clip" && !(s.project && ops.findClip(s.project, sel.clipId))) return { kind: "none" };
  if (sel.kind === "marker" && !s.project?.markers.some((m) => m.id === sel.id)) return { kind: "none" };
  if (sel.kind === "transition" && !s.project?.transitions.some((t) => t.id === sel.id)) return { kind: "none" };
  if (sel.kind === "media" && !s.project?.media.includes(sel.id)) return { kind: "none" };
  return sel;
}

/** The currently selected timeline clip (or null). */
export function useSelectedClip(): { clip: Clip; trackId: string; trackType: TrackType } | null {
  const sel = useEditor((s) => s.selection);
  const project = useEditor((s) => s.project);
  if (sel.kind !== "clip" || !project) return null;
  const hit = ops.findClip(project, sel.clipId);
  return hit ? { clip: hit.clip, trackId: hit.track.id, trackType: hit.track.type } : null;
}

export function clipAtTime(p: Project, time: number): { clip: Clip; trackId: string } | null {
  const main = p.tracks.find((t) => t.type === "video");
  if (!main) return null;
  const c = main.clips.find((c) => time >= c.start && time < clipEnd(c));
  return c ? { clip: c, trackId: main.id } : null;
}
