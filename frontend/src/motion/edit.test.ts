import { describe, expect, it } from "vitest";
import type { Clip, MediaMeta, Project } from "../types";
import { clipDuration } from "../util";
import * as E from "./edit";
import { valueAt } from "./motion";

const clip = (over: Partial<Clip> = {}): Clip => ({
  id: "c1", mediaId: "m1", start: 2, inPoint: 0, outPoint: 4, speed: 1, reverse: false, freezeFrame: null,
  volume: 1, muted: false, fadeIn: 0, fadeOut: 0, fit: null,
  transform: { x: 0, y: 0, scale: 1, rotation: 0, flipH: false, flipV: false, opacity: 1, crop: { left: 0, top: 0, right: 0, bottom: 0 } },
  keyframes: {}, filters: [], text: null, ...over,
});

describe("keyframe editing", () => {
  it("diamond adds a key with the current value, then edits update it; removing the last keeps the value", () => {
    const c = clip();
    c.transform.scale = 1.2;
    expect(E.toggleKey(c, "scale", 0)).toBe("added");
    expect(c.keyframes.scale).toEqual([{ time: 0, value: 1.2, easing: "inout" }]);
    E.setProp(c, "scale", 2, 1.5); // new key at 2 s
    E.setProp(c, "scale", 0, 1.1); // updates the key at 0
    expect(c.keyframes.scale.map((k) => [k.time, k.value])).toEqual([[0, 1.1], [2, 1.5]]);
    expect(E.propValue(c, "scale", 1)).toBeCloseTo(1.3, 6); // in-out midpoint
    E.toggleKey(c, "scale", 2);
    expect(E.toggleKey(c, "scale", 0)).toBe("removed");
    expect(c.keyframes.scale).toBeUndefined();
    expect(c.transform.scale).toBe(1.1);
  });

  it("without keyframes, setting changes the base value", () => {
    const c = clip();
    E.setProp(c, "x", 1, 40);
    E.setProp(c, "volume", 1, 0.5);
    expect(c.transform.x).toBe(40);
    expect(c.volume).toBe(0.5);
  });

  it("retime moves every property's key at that time, and jumping finds neighbours", () => {
    const c = clip({ keyframes: {
      x: [{ time: 1, value: 0, easing: "linear" }, { time: 3, value: 10, easing: "linear" }],
      opacity: [{ time: 1, value: 1, easing: "linear" }],
    } });
    expect(E.keyTimes(c)).toEqual([{ time: 1, props: ["x", "opacity"] }, { time: 3, props: ["x"] }]);
    E.retimeKeys(c, 1, 0.5);
    expect(c.keyframes.x[0].time).toBe(0.5);
    expect(c.keyframes.opacity[0].time).toBe(0.5);
    E.retimeKeys(c, 3, 99);
    expect(c.keyframes.x[1].time).toBe(4); // clamped to the clip
    expect(E.jumpKey(c, 0.5, 1)).toBe(4);
    expect(E.jumpKey(c, 4, -1)).toBe(0.5);
    expect(E.jumpKey(c, 4, 1)).toBeNull();
    expect(E.setKeyEasing(c, "x", 0.5, "punch")).toBe(true);
    expect(c.keyframes.x[0].easing).toBe("punch");
  });

  it("presets: zoom/pan replace the base move, punch and shake layer on their own channels", () => {
    const c = clip();
    const ctx = { W: 1080, H: 1920, fps: 30, fill: true };
    E.applyPreset(c, { kind: "zoom", dir: "in" }, ctx);
    expect(valueAt(c.keyframes.scale, 4, 1)).toBeCloseTo(1.12, 6);
    E.applyPreset(c, { kind: "punch", at: 1 }, ctx);
    E.applyPreset(c, { kind: "shake", at: 2 }, ctx);
    expect(c.keyframes.scale).toHaveLength(2);
    expect(c.keyframes.punch.length).toBe(3);
    expect(c.keyframes.shakeX.length).toBeGreaterThan(5);
    E.applyPreset(c, { kind: "pan", dir: "left" }, ctx);
    expect(c.keyframes.x[0].value).toBeGreaterThanOrEqual(c.keyframes.x[1].value);
    expect(c.keyframes.punch.length).toBe(3); // kept
  });

  it("speed changes keep keyframes on the same footage", () => {
    const c = clip({ keyframes: { x: [{ time: 2, value: 5, easing: "linear" }] } });
    E.setSpeed(c, 2);
    expect(clipDuration(c)).toBe(2);
    expect(c.keyframes.x[0].time).toBe(1);
    E.setRamp(c, [{ u: 0, speed: 1 }, { u: 1, speed: 3 }], "x");
    expect(clipDuration(c)).toBeCloseTo(2, 6); // average speed 2
  });
});

describe("audio editing", () => {
  const project = (): Project => ({
    version: 1, id: "p", name: "p", created: 0, updated: 0,
    settings: { width: 1080, height: 1920, fps: 30, aspect: "9:16", background: { mode: "fill", color: "#000" }, sampleRate: 48000 },
    media: ["m1"],
    tracks: [
      { id: "v", type: "video", name: "Main", muted: false, hidden: false, locked: false, volume: 1, clips: [clip()] },
      { id: "m", type: "music", name: "Music", muted: false, hidden: false, locked: false, volume: 1, clips: [] },
    ],
    transitions: [], markers: [],
    export: { preset: "reels", mode: "final", width: 1080, height: 1920, fps: 30, range: null, fileName: "" },
  });

  it("detach audio: copy on an audio track, video muted", () => {
    const p = project();
    const id = E.detachAudio(p, "c1")!;
    const audio = p.tracks.find((t) => t.type === "audio")!;
    expect(p.tracks.indexOf(audio)).toBe(1); // before music
    expect(audio.clips[0].id).toBe(id);
    expect(audio.clips[0].start).toBe(2);
    expect(p.tracks[0].clips[0].muted).toBe(true);
  });

  it("sound effects land their peak on the given time, on an SFX track", () => {
    const p = project();
    const m = { id: "s1", duration: 0.8, sfxPeak: 0.45 } as MediaMeta;
    E.placeSfx(p, m, 5);
    E.placeSfx(p, m, 5.2); // overlaps: goes to a second SFX track
    E.placeSfx(p, m, 0.2); // before 0: trimmed so the peak still lands
    const sfx = p.tracks.filter((t) => t.name.startsWith("SFX"));
    expect(sfx.map((t) => t.name)).toEqual(["SFX", "SFX 2"]);
    expect(sfx[0].clips[1].start).toBeCloseTo(4.55, 6);
    const early = sfx[0].clips[0];
    expect(early.start).toBe(0);
    expect(early.inPoint).toBeCloseTo(0.25, 6);
    expect(p.media).toContain("s1");
  });
});
