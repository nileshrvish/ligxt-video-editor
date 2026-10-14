// Motion tracking data (mirrors backend/app/follow.py).

/** A tracked face or object of one media file, in source frames: data[i] is source frame k0 + i:
 *  [cx, cy, w, h] as fractions of the source frame, angle (degrees), confidence (0 = lost, values held). */
export interface MotionTrack {
  id: string;
  mediaId: string;
  name: string;
  kind: "face" | "object";
  method: "face" | "box" | "sam";
  fps: number;
  k0: number;
  data: [number, number, number, number, number, number][];
  /** Boxes the user set (start or fixes), by source frame. */
  keys?: { k: number; box: [number, number, number, number] }[];
}

/** Text, stickers, overlay clips and tracked regions: follow a track. The clip's own x/y is the offset. */
export interface Follow {
  trackId: string;
  clipId?: string;
  enabled?: boolean;
  position?: boolean;
  scale?: boolean;
  rotation?: boolean;
  smoothing?: number;
  safe?: boolean;
}

/** Auto-reframe (video clips): pan across the whole frame so the tracked subject stays on a third. */
export interface Reframe {
  trackId?: string;
  enabled?: boolean;
  third?: "left" | "center" | "right";
  smoothing?: number;
  vertical?: boolean;
}

/** A tracked region on an adjustment clip: blur / pixelate / grade inside an ellipse or box. */
export interface Region {
  effect?: "blur" | "pixelate" | "grade";
  amount?: number;
  shape?: "ellipse" | "rect";
  feather?: number;
  padding?: number;
}

/** Per-frame channels (tau = i / fps, clip-relative): x/y added, s multiplies the scale, r adds degrees. */
export interface AutoMotion {
  fps: number;
  x?: number[] | null;
  y?: number[] | null;
  s?: number[] | null;
  r?: number[] | null;
  wholeSource?: boolean;
}

export interface AutoRegion { fps: number; x: number[]; y: number[]; w: number[]; h: number[] }
