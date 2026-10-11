import { create } from "zustand";
import { api, uploadMedia } from "../api";
import type { Clip, MediaMeta, Project, TrackType } from "../types";
import { clipEnd, newClip, trackEnd } from "../util";
import { toastError, useApp } from "./app";

export type Selection =
  | { kind: "none" }
  | { kind: "media"; id: string }
  | { kind: "clip"; trackId: string; clipId: string };

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
  setZoom: (pxPerSec: number) => void;
  importFiles: (files: File[]) => Promise<void>;
  refreshMedia: (id: string) => Promise<void>;
  removeMedia: (id: string) => void;
  addToTimeline: (mediaId: string, trackType?: TrackType, at?: number) => void;
  deleteSelectedClip: () => void;
  save: (kind?: "manual" | "auto") => Promise<boolean>;
}

const HISTORY_LIMIT = 100;

export const useEditor = create<EditorState>((set, get) => ({
  project: null,
  media: {},
  selection: { kind: "none" },
  playhead: 0,
  playing: false,
  pxPerSec: 60,
  uploads: [],
  saveState: "saved",
  lastSaved: null,
  past: [],
  future: [],
  gestureStart: null,

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
  setPlaying: (playing) => set({ playing }),
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

  addToTimeline: (mediaId, trackType, at) => {
    const m = get().media[mediaId];
    const p = get().project;
    if (!m || !p) return;
    const type: TrackType = trackType ?? (m.type === "audio" ? "music" : "video");
    if (type === "video" && m.type === "audio") {
      useApp.getState().toast({ kind: "warning", title: "Audio files go on the Audio or Music track." });
      return;
    }
    if ((type === "audio" || type === "music") && !m.hasAudio) {
      useApp.getState().toast({ kind: "warning", title: `${m.name} has no audio.` });
      return;
    }
    const track = p.tracks.find((t) => t.type === type);
    if (!track) {
      useApp.getState().toast({ kind: "warning", title: `There is no ${type} track. Add one first.` });
      return;
    }
    const clip: Clip = newClip(m, at ?? trackEnd(track));
    get().commit((d) => {
      d.tracks.find((t) => t.id === track.id)!.clips.push(clip);
    });
    set({ selection: { kind: "clip", trackId: track.id, clipId: clip.id } });
  },

  deleteSelectedClip: () => {
    const s = get().selection;
    if (s.kind !== "clip") return;
    get().commit((d) => {
      const t = d.tracks.find((t) => t.id === s.trackId);
      if (t) t.clips = t.clips.filter((c) => c.id !== s.clipId);
      d.transitions = d.transitions.filter((x) => x.fromClipId !== s.clipId && x.toClipId !== s.clipId);
    });
    set({ selection: { kind: "none" } });
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

function validSelection(s: EditorState): Selection {
  const sel = s.selection;
  if (sel.kind === "clip") {
    const t = s.project?.tracks.find((t) => t.id === sel.trackId);
    if (!t?.clips.some((c) => c.id === sel.clipId)) return { kind: "none" };
  }
  if (sel.kind === "media" && !s.project?.media.includes(sel.id)) return { kind: "none" };
  return sel;
}

/** The currently selected timeline clip (or null). */
export function useSelectedClip(): { clip: Clip; trackId: string; trackType: TrackType } | null {
  const sel = useEditor((s) => s.selection);
  const project = useEditor((s) => s.project);
  if (sel.kind !== "clip" || !project) return null;
  const t = project.tracks.find((t) => t.id === sel.trackId);
  const clip = t?.clips.find((c) => c.id === sel.clipId);
  return t && clip ? { clip, trackId: t.id, trackType: t.type } : null;
}

export function clipAtTime(p: Project, time: number): { clip: Clip; trackId: string } | null {
  const main = p.tracks.find((t) => t.type === "video");
  if (!main) return null;
  const c = main.clips.find((c) => time >= c.start && time < clipEnd(c));
  return c ? { clip: c, trackId: main.id } : null;
}
