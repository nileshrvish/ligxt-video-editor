import { describe, expect, it } from "vitest";
import * as ops from "../timeline/ops";
import type { Clip, MediaMeta, Project, Track, TrackType } from "../types";
import { addFollower, deleteTrack, nextName, upsertTrack, weakRanges } from "./edit";
import { resolve } from "./follow";
import type { MotionTrack } from "./types";

let n = 0;
const clip = (start: number, dur: number, extra: Partial<Clip> = {}): Clip => ({
  id: `c${++n}`, mediaId: "vid", start, inPoint: 1, outPoint: 1 + dur, speed: 1, reverse: false, freezeFrame: null, volume: 1, muted: false,
  fadeIn: 0, fadeOut: 0, fit: "fill",
  transform: { x: 0, y: 0, scale: 1, rotation: 0, flipH: false, flipV: false, opacity: 1, crop: { left: 0, top: 0, right: 0, bottom: 0 } },
  keyframes: {}, filters: [], text: null, ...extra,
});
const track = (type: TrackType, clips: Clip[]): Track => ({ id: `t-${type}`, type, name: type, muted: false, hidden: false, locked: false, volume: 1, clips });
const project = (tracks: Track[]): Project => ({
  version: 1, id: "p", name: "p", created: 0, updated: 0,
  settings: { width: 1080, height: 1920, fps: 30, aspect: "9:16", background: { mode: "fill", color: "#000000" }, sampleRate: 48000 } as Project["settings"],
  media: ["vid"], tracks, transitions: [], markers: [],
  export: { preset: "reels", mode: "final", width: 1080, height: 1920, fps: 30, range: null, fileName: "" } as unknown as Project["export"],
});
const metas = { vid: { id: "vid", type: "video", width: 1920, height: 1080, fps: 25, duration: 10 } as MediaMeta };
const tr = (id: string, lost: number[] = []): MotionTrack => ({
  id, mediaId: "vid", name: "Face 1", kind: "face", method: "face", fps: 25, k0: 0,
  data: Array.from({ length: 250 }, (_, i) => [0.3 + 0.002 * i, 0.5, 0.1, 0.2, 0, lost.includes(i) ? 0 : 0.9] as [number, number, number, number, number, number]),
});

describe("tracking edits", () => {
  it("adds text, a sticker and a blur that follow a track over the host clip", () => {
    const host = clip(2, 3);
    const p = project([track("video", [host]), track("text", [clip(2.5, 1, { mediaId: null, text: { content: "x" } })])]);
    upsertTrack(p, tr("a"));
    expect(nextName(p, "face")).toBe("Face 2");
    const tid = addFollower(p, host, "a", "text");
    const sticker = { id: "stk", type: "image", width: 512, height: 512, duration: 0 } as MediaMeta;
    const sid = addFollower(p, host, "a", "sticker", { sticker });
    const bid = addFollower(p, host, "a", "blur");
    const all = p.tracks.flatMap((t) => t.clips);
    const t = all.find((c) => c.id === tid)!, s = all.find((c) => c.id === sid)!, b = all.find((c) => c.id === bid)!;
    // the text track was busy at that time: a new text track was added, no overlaps anywhere
    expect(p.tracks.filter((x) => x.type === "text").length).toBe(2);
    p.tracks.forEach((x) => expect(ops.overlaps(x)).toBe(false));
    for (const c of [t, s, b]) {
      expect(c.start).toBe(2);
      expect(c.follow).toMatchObject({ trackId: "a", clipId: host.id });
    }
    expect(b.adjustment).toBe(true);
    expect(b.region?.effect).toBe("blur");
    expect(s.mediaId).toBe("stk");
    // all three resolve to tracking channels
    const r = resolve(p, { ...metas, stk: sticker }, 30, () => [400, 100]);
    expect(r.get(t.id)?.autoMotion?.x?.length).toBeGreaterThan(80);
    expect(r.get(s.id)?.autoMotion).toBeTruthy();
    expect(r.get(b.id)?.autoRegion?.w.length).toBeGreaterThan(80);
  });

  it("deleting a track switches off whatever followed it", () => {
    const host = clip(0, 4, { reframe: { trackId: "a", third: "left" } });
    const p = project([track("video", [host])]);
    upsertTrack(p, tr("a"));
    addFollower(p, host, "a", "pixelate");
    deleteTrack(p, "a");
    expect(p.motionTracks).toEqual([]);
    expect(p.tracks.flatMap((t) => t.clips).every((c) => !c.follow && !c.reframe)).toBe(true);
  });

  it("finds the uncertain and lost stretches of a clip (for the timeline marks)", () => {
    const host = clip(10, 4); // source 1..5 s = frames 25..125
    const lost = Array.from({ length: 13 }, (_, i) => 50 + i); // source 2.0-2.5 s
    const r = weakRanges(tr("a", lost), host, (tau) => host.inPoint + tau, 30);
    expect(r.length).toBe(1);
    expect(r[0].lost).toBe(true);
    expect(r[0].from).toBeCloseTo(11, 1); // timeline = 10 + (2.0 - 1)
    expect(r[0].to).toBeCloseTo(11.52, 1);
  });
});
