// Mirrors backend/app/models.py (project format v1). Times are seconds.

export type TrackType = "video" | "overlay" | "text" | "audio" | "music";
export type FitMode = "fit" | "fill" | "blur";
export type Aspect = "9:16" | "1:1" | "4:5" | "16:9";

export interface Crop { left: number; top: number; right: number; bottom: number }

export interface Transform {
  x: number; y: number; scale: number; rotation: number;
  flipH: boolean; flipV: boolean; opacity: number; crop: Crop;
}

export interface Keyframe { time: number; value: number; easing: string }

export interface Filter {
  id: string; type: string; name: string; intensity: number;
  params: Record<string, unknown>; enabled: boolean;
}

export interface Clip {
  id: string;
  mediaId: string | null;
  start: number;
  inPoint: number;
  outPoint: number;
  speed: number;
  reverse: boolean;
  freezeFrame: number | null;
  volume: number;
  muted: boolean;
  fadeIn: number;
  fadeOut: number;
  fit: FitMode | null;
  transform: Transform;
  keyframes: Record<string, Keyframe[]>;
  filters: Filter[];
  text: Record<string, unknown> | null;
  [extra: string]: unknown;
}

export interface Track {
  id: string; type: TrackType; name: string;
  muted: boolean; hidden: boolean; locked: boolean; volume: number;
  magnetic?: boolean; // main video track only (default true)
  clips: Clip[];
}

export interface Transition { id: string; trackId: string; fromClipId: string; toClipId: string; type: string; duration: number }
export interface Marker { id: string; time: number; label: string; color: string }

export interface Settings {
  width: number; height: number; fps: number; aspect: Aspect;
  background: { mode: FitMode; color: string };
  sampleRate: number;
}

export interface ExportSettings {
  preset: string;
  mode: "final" | "draft";
  width: number;
  height: number;
  fps: number | "source";
  range: { start: number; end: number } | null;
  fileName: string;
}

export interface Project {
  version: number; id: string; name: string; created: number; updated: number;
  settings: Settings;
  media: string[];
  tracks: Track[];
  transitions: Transition[];
  markers: Marker[];
  export: ExportSettings;
  [extra: string]: unknown;
}

export interface MediaMeta {
  id: string; name: string; type: "video" | "image" | "audio";
  file: string; size: number; imported: number;
  duration: number; hasVideo: boolean; hasAudio: boolean;
  width?: number; height?: number; fps?: number; rotation?: number;
  videoCodec?: string; audioCodec?: string; pixFmt?: string; hdr?: boolean;
  sampleRate?: number; channels?: number; container?: string; bitrate?: number;
  thumbnail: string | null; waveform: string | null; proxy: string | null;
  proxyStatus: "none" | "pending" | "ready" | "error" | "notNeeded";
  status: "processing" | "ready" | "warning" | "error";
  error: string | null;
}

export interface Job {
  id: string; kind: string; label: string; ref: Record<string, string>;
  status: "queued" | "running" | "done" | "error" | "cancelled";
  progress: number; eta: number | null; message: string;
  error: string | null; log: string | null; result: any;
  created: number; started: number | null; finished: number | null;
}

export interface ProjectSummary {
  id: string; name: string; updated: number; created?: number; aspect?: string;
  clips?: number; duration?: number; mediaCount?: number; thumbnail?: string | null; damaged?: boolean;
}
