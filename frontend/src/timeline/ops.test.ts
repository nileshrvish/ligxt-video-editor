import { describe, expect, it } from "vitest";
import type { Clip, Project, Track, TrackType } from "../types";
import { clipEnd } from "../util";
import * as ops from "./ops";

let n = 0;
function clip(start: number, dur: number, extra: Partial<Clip> = {}): Clip {
  return {
    id: `c${++n}`, mediaId: extra.mediaId ?? "vid", start, inPoint: 0, outPoint: dur, speed: 1, reverse: false,
    freezeFrame: null, volume: 1, muted: false, fadeIn: 0, fadeOut: 0, fit: null,
    transform: { x: 0, y: 0, scale: 1, rotation: 0, flipH: false, flipV: false, opacity: 1, crop: { left: 0, top: 0, right: 0, bottom: 0 } },
    keyframes: {}, filters: [], text: null, ...extra,
  };
}
function track(type: TrackType, clips: Clip[], extra: Partial<Track> = {}): Track {
  return { id: `t-${type}`, type, name: type, muted: false, hidden: false, locked: false, volume: 1, clips, ...extra };
}
function project(tracks: Track[]): Project {
  return {
    version: 1, id: "p", name: "p", created: 0, updated: 0,
    settings: { width: 1080, height: 1920, fps: 30, aspect: "9:16", background: { mode: "fit", color: "#000000" }, sampleRate: 48000 },
    media: [], tracks, transitions: [], markers: [],
    export: { preset: "reels", mode: "final", width: 1080, height: 1920, fps: 30, range: null, fileName: "" },
  };
}
// "vid" is 10 s long, "img" is a photo.
const mediaDur: ops.MediaDuration = (id) => (id === "img" ? Infinity : 10);
const order = (t: Track) => [...t.clips].sort((a, b) => a.start - b.start).map((c) => c.id);
const starts = (t: Track) => [...t.clips].sort((a, b) => a.start - b.start).map((c) => c.start);

describe("magnetic main track", () => {
  it("reorders by drag and stays packed", () => {
    const a = clip(0, 2), b = clip(2, 3), c = clip(5, 1);
    const p = project([track("video", [a, b, c])]);
    ops.moveClip(p, c.id, 0); // drag C to the front
    expect(order(p.tracks[0])).toEqual([c.id, a.id, b.id]);
    expect(starts(p.tracks[0])).toEqual([0, 1, 3]);
    ops.moveClip(p, c.id, 5.5); // drag it to the end
    expect(order(p.tracks[0])).toEqual([a.id, b.id, c.id]);
    expect(ops.overlaps(p.tracks[0])).toBe(false);
  });

  it("trims ripple the following clips", () => {
    const a = clip(0, 4), b = clip(4, 2);
    const p = project([track("video", [a, b])]);
    ops.trimEnd(p, a.id, 3, mediaDur);
    expect(a.outPoint).toBe(3);
    expect(b.start).toBe(3);
    ops.trimStart(p, a.id, 1, mediaDur); // cut 1 s off the head
    expect(a.inPoint).toBe(1);
    expect(a.start).toBe(0);
    expect(b.start).toBe(2);
  });

  it("cannot trim past the source media", () => {
    const a = clip(0, 4, { inPoint: 1, outPoint: 5 });
    const p = project([track("video", [a])]);
    ops.trimEnd(p, a.id, 50, mediaDur);
    expect(a.outPoint).toBe(10);
    ops.trimStart(p, a.id, -5, mediaDur);
    expect(a.inPoint).toBe(0);
  });

  it("delete is always a ripple delete", () => {
    const a = clip(0, 2), b = clip(2, 3), c = clip(5, 1);
    const p = project([track("video", [a, b, c])]);
    ops.deleteClip(p, b.id);
    expect(starts(p.tracks[0])).toEqual([0, 2]);
  });

  it("can be switched off for free placement with gaps", () => {
    const a = clip(0, 2), b = clip(2, 3);
    const p = project([track("video", [a, b], { magnetic: false })]);
    ops.moveClip(p, a.id, 8);
    expect(a.start).toBe(8);
    expect(b.start).toBe(2);
  });
});

describe("free tracks (overlay / audio / music)", () => {
  it("drops into the nearest free slot instead of overlapping", () => {
    const a = clip(0, 2), b = clip(5, 2), m = clip(10, 2);
    const p = project([track("overlay", [a, b, m])]);
    ops.moveClip(p, m.id, 1); // overlaps a -> nearest slot is the 2..5 gap
    expect(m.start).toBe(2);
    expect(ops.overlaps(p.tracks[0])).toBe(false);
    ops.moveClip(p, m.id, 5.5); // overlaps b, gap 2..5 is where it already is -> stays nearest
    expect(ops.overlaps(p.tracks[0])).toBe(false);
  });

  it("trimming stops at neighbours", () => {
    const a = clip(0, 2), b = clip(3, 2);
    const p = project([track("audio", [a, b])]);
    ops.trimEnd(p, a.id, 4.5, mediaDur);
    expect(clipEnd(a)).toBe(3);
    ops.trimStart(p, b.id, 1, mediaDur);
    expect(b.start).toBe(3); // a ends at 3; also b's source starts at 0
  });

  it("ripple delete shifts later clips left", () => {
    const a = clip(0, 2), b = clip(4, 2), c = clip(7, 1);
    const p = project([track("music", [a, b, c])]);
    ops.deleteClip(p, b.id, true);
    expect(starts(p.tracks[0])).toEqual([0, 5]);
    ops.deleteClip(p, a.id, false);
    expect(starts(p.tracks[0])).toEqual([5]);
  });

  it("moves only between compatible tracks", () => {
    const v = clip(0, 2), au = clip(0, 2);
    const p = project([track("video", [v]), track("overlay", []), track("music", [au])]);
    ops.moveClip(p, v.id, 1, "t-music");
    expect(p.tracks[2].clips.map((c) => c.id)).toEqual([au.id]);
    ops.moveClip(p, v.id, 1, "t-overlay");
    expect(p.tracks[1].clips[0].id).toBe(v.id);
    expect(p.tracks[1].clips[0].start).toBe(1);
    expect(p.tracks[0].clips).toHaveLength(0);
  });

  it("locked tracks don't change", () => {
    const a = clip(0, 2);
    const p = project([track("overlay", [a], { locked: true })]);
    ops.moveClip(p, a.id, 5);
    ops.trimEnd(p, a.id, 1, mediaDur);
    ops.deleteClip(p, a.id);
    expect(a.start).toBe(0);
    expect(p.tracks[0].clips).toHaveLength(1);
  });
});

describe("settle after typed edits", () => {
  it("lengthening a magnetic clip ripples without reordering", () => {
    const a = clip(0, 1), b = clip(1, 1), c = clip(2, 1);
    const p = project([track("video", [a, b, c])]);
    b.outPoint = 8;
    ops.settle(p, b.id);
    expect(order(p.tracks[0])).toEqual([a.id, b.id, c.id]);
    expect(starts(p.tracks[0])).toEqual([0, 1, 9]);
  });
  it("an overlapping free clip moves to the nearest gap", () => {
    const a = clip(0, 2), b = clip(3, 2);
    const p = project([track("overlay", [a, b])]);
    a.outPoint = 4; // now overlaps b
    ops.settle(p, a.id);
    expect(ops.overlaps(p.tracks[0])).toBe(false);
  });
});

describe("split / duplicate", () => {
  it("splits at the playhead with correct source points", () => {
    const a = clip(0, 4, { inPoint: 2, outPoint: 6, fadeIn: 0.5, fadeOut: 0.5 });
    const p = project([track("video", [a])]);
    const id = ops.splitClip(p, a.id, 1.5)!;
    const r = p.tracks[0].clips.find((c) => c.id === id)!;
    expect([a.inPoint, a.outPoint, a.fadeOut]).toEqual([2, 3.5, 0]);
    expect([r.start, r.inPoint, r.outPoint, r.fadeIn, r.fadeOut]).toEqual([1.5, 3.5, 6, 0, 0.5]);
  });

  it("respects speed and splits keyframes", () => {
    const a = clip(0, 2, { inPoint: 0, outPoint: 4, speed: 2, keyframes: { scale: [{ time: 0, value: 1, easing: "linear" }, { time: 1.5, value: 2, easing: "linear" }] } });
    const p = project([track("video", [a])]);
    const id = ops.splitClip(p, a.id, 1)!;
    const r = p.tracks[0].clips.find((c) => c.id === id)!;
    expect(a.outPoint).toBe(2);
    expect(r.inPoint).toBe(2);
    expect(a.keyframes.scale.map((k) => k.time)).toEqual([0]);
    expect(r.keyframes.scale.map((k) => k.time)).toEqual([0.5]);
  });

  it("does not split at an edge", () => {
    const a = clip(0, 4);
    const p = project([track("video", [a])]);
    expect(ops.splitClip(p, a.id, 0)).toBeNull();
    expect(ops.splitClip(p, a.id, 4)).toBeNull();
  });

  it("splitAll cuts every track under the playhead", () => {
    const p = project([track("video", [clip(0, 4)]), track("music", [clip(0, 10)]), track("overlay", [clip(5, 1)])]);
    expect(ops.splitAll(p, 2)).toHaveLength(2);
  });

  it("duplicate lands right after the original", () => {
    const a = clip(0, 2), b = clip(2, 1);
    const p = project([track("video", [a, b]), track("overlay", [clip(0, 2), clip(2, 1)])]);
    const id = ops.duplicateClip(p, a.id)!;
    expect(order(p.tracks[0])).toEqual([a.id, id, b.id]);
    expect(starts(p.tracks[0])).toEqual([0, 2, 4]);
    const ov = p.tracks[1];
    const id2 = ops.duplicateClip(p, ov.clips[0].id)!;
    expect(ov.clips.find((c) => c.id === id2)!.start).toBe(3);
    expect(ops.overlaps(ov)).toBe(false);
  });

  it("photos can be extended freely", () => {
    const a = clip(0, 3, { mediaId: "img" });
    const p = project([track("video", [a])]);
    ops.trimEnd(p, a.id, 20, mediaDur);
    expect(a.outPoint).toBe(20);
  });
});

describe("tracks, markers, snapping, transitions", () => {
  it("adds tracks grouped by type and protects the main track", () => {
    const p = project([track("video", []), track("overlay", []), track("text", []), track("audio", []), track("music", [])]);
    const id = ops.addTrack(p, "overlay");
    expect(p.tracks.map((t) => t.type)).toEqual(["video", "overlay", "overlay", "text", "audio", "music"]);
    expect(p.tracks[2].id).toBe(id);
    expect(p.tracks[2].name).toBe("Overlay 2");
    expect(ops.removeTrack(p, "t-video")).toBe(false);
    expect(ops.removeTrack(p, id)).toBe(true);
  });

  it("markers are sorted and de-duplicated", () => {
    const p = project([]);
    ops.addMarker(p, 5);
    ops.addMarker(p, 2);
    expect(ops.addMarker(p, 2.001)).toBeNull();
    expect(p.markers.map((m) => m.time)).toEqual([2, 5]);
    ops.moveMarker(p, p.markers[1].id, 1);
    expect(p.markers.map((m) => m.time)).toEqual([1, 2]);
  });

  it("snaps start or end edge to the nearest point", () => {
    const pts = [0, 5, 10];
    expect(ops.snap(4.9, pts, 0.2).time).toBe(5);
    expect(ops.snap(4.5, pts, 0.2).at).toBeNull();
    // 2 s clip whose end (start+2) lands near 10
    expect(ops.snap(7.95, pts, 0.2, [0, 2]).time).toBe(8);
  });

  it("maps beats through in-point and speed, and adds them as snap points", () => {
    const c = clip(10, 4, { inPoint: 2, outPoint: 10, speed: 2 });
    expect(ops.clipBeats(c, [1, 2, 3, 9.5, 10])).toEqual([10, 10.5, 13.75]);
    const p = project([track("music", [c])]);
    expect(ops.snapPoints(p, 0, undefined, [10.5])).toContain(10.5);
  });

  it("removes transitions when clips stop being neighbours", () => {
    const a = clip(0, 2), b = clip(2, 2), c = clip(4, 2);
    const p = project([track("video", [a, b, c])]);
    p.transitions = [{ id: "x", trackId: "t-video", fromClipId: a.id, toClipId: b.id, type: "fade", duration: 0.5 }];
    ops.moveClip(p, c.id, 1); // c goes between a and b
    expect(p.transitions).toHaveLength(0);
  });
});
