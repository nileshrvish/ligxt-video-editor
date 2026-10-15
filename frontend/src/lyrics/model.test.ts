import { describe, expect, it } from "vitest";
import type { Clip, Project, Track } from "../types";
import { canRun, musicClips, newTapState, nextTarget, snapToBeat, splitLyrics, tap, tapLines, undoTap } from "./model";

const clip = (id: string, start: number, dur: number, extra: Partial<Clip> = {}): Clip => ({
  id, mediaId: "song", start, inPoint: 10, outPoint: 10 + dur, speed: 1, reverse: false, freezeFrame: null, volume: 1, muted: false,
  fadeIn: 0, fadeOut: 0, fit: null, transform: { x: 0, y: 0, scale: 1, rotation: 0, flipH: false, flipV: false, opacity: 1, crop: { left: 0, top: 0, right: 0, bottom: 0 } },
  keyframes: {}, filters: [], text: null, ...extra,
});
const proj = (clips: Clip[]): Project => ({
  tracks: [{ id: "m", type: "music", name: "Music", muted: false, hidden: false, locked: false, volume: 1, clips } as Track],
} as unknown as Project);

describe("lyrics dialog logic", () => {
  it("defaults to the music clip under the playhead and flags clips that can't be timed", () => {
    const p = proj([clip("a", 0, 5), clip("b", 5, 5), clip("c", 10, 5, { speedRamp: { points: [{ u: 0, speed: 1 }, { u: 1, speed: 2 }] } })]);
    expect(musicClips(p, { song: "Song" }, 6).defaultId).toBe("b");
    expect(musicClips(p, { song: "Song" }, 12).defaultId).toBe("a"); // under the playhead is ramped: the first usable one
    expect(musicClips(p, {}, 0).list[2].skip).toMatch(/speed-ramped/);
  });
  it("says what's missing before a run", () => {
    expect(canRun("lyrics", "paste", { hasClip: true, text: " ", taps: 0 }).ok).toBe(false);
    expect(canRun("lyrics", "tap", { hasClip: true, text: "a", taps: 0 }).why).toMatch(/tap Space/);
    expect(canRun("lyrics", "auto", { hasClip: false, text: "", taps: 0 }).ok).toBe(false);
    expect(canRun("lyrics", "auto", { hasClip: true, text: "", taps: 0 }).ok).toBe(true);
    expect(splitLyrics("  hello world \n\n  na na\r\n")).toEqual([["hello", "world"], ["na", "na"]]);
  });
});

describe("tap to sync", () => {
  it("Space starts lines, Shift+Space starts words, the rest is spread evenly", () => {
    let s = newTapState("we were young\nand free tonight");
    expect(nextTarget(s, false)).toEqual({ line: 0, word: 0 });
    s = tap(s, 1.0, false);
    s = tap(s, 2.0, true); // "were"
    s = tap(s, 4.0, false); // line 2
    const out = tapLines(s, 7.0);
    expect(out.map((l) => l.map((w) => w.text))).toEqual([["we", "were", "young"], ["and", "free", "tonight"]]);
    expect(out[0].map((w) => w.start)).toEqual([1, 2, 3]); // "young" halfway between "were" and the next line
    expect(out[0][2].end).toBe(4);
    expect(out[1].map((w) => w.start)).toEqual([4, 5, 6]);
    expect(out[1][2].end).toBe(7);
    expect(tapLines(undoTap(s), 7).length).toBe(1);
  });
  it("snaps a tap to a beat within 80 ms only", () => {
    const beats = [1.0, 1.5, 2.0];
    expect(snapToBeat(1.06, beats)).toBe(1.0);
    expect(snapToBeat(1.2, beats)).toBe(1.2);
    let s = newTapState("a b\nc");
    s = tap(s, 0.95, false, beats);
    s = tap(s, 1.52, false, beats);
    expect(s.taps.map((t) => t.t)).toEqual([1.0, 1.5]);
    expect(tap(s, 3, false).taps.length).toBe(2); // no more lines
  });
});
