import { describe, expect, it } from "vitest";
import type { Project } from "../types";
import { splitClip } from "../timeline/ops";
import * as T from "./edit";

const project = (): Project => ({
  version: 1, id: "p", name: "p", created: 0, updated: 0,
  settings: { width: 1080, height: 1920, fps: 30, aspect: "9:16", background: { mode: "fill", color: "#000" }, sampleRate: 48000 },
  media: [], tracks: [{ id: "v", type: "video", name: "Main", muted: false, hidden: false, locked: false, volume: 1, clips: [] },
    { id: "m", type: "music", name: "Music", muted: false, hidden: false, locked: false, volume: 1, clips: [] }],
  transitions: [], markers: [],
  export: { preset: "reels", mode: "final", width: 1080, height: 1920, fps: 30, range: null, fileName: "" },
});

describe("text editing", () => {
  it("presets keep the words and timings", () => {
    const c = T.newTextClip(1, 2, { content: "hi there", words: [{ text: "hi", start: 0, end: 0.3 }, { text: "there", start: 0.3, end: 0.8 }] });
    T.applyStyle(c, T.presetById("hook")!.style, T.presetById("hook")!.position);
    expect((c.text as { font: string }).font).toBe("Anton-Regular");
    expect((c.text as { content: string }).content).toBe("hi there");
    expect(c.transform.y).toBe(-420);
  });

  it("editing caption text keeps timings when the word count is unchanged, otherwise re-spreads them", () => {
    const c = T.newTextClip(0, 2, { content: "a b", words: [{ text: "a", start: 0, end: 0.5 }, { text: "b", start: 1, end: 1.5 }] });
    T.setContent(c, "x y");
    expect((c.text as { words: unknown }).words).toEqual([{ text: "x", start: 0, end: 0.5 }, { text: "y", start: 1, end: 1.5 }]);
    T.setContent(c, "one two three");
    const w = (c.text as { words: { start: number; end: number }[] }).words;
    expect(w).toHaveLength(3);
    expect(w[0].start).toBe(0);
    expect(w[2].end).toBeCloseTo(1.5, 3);
  });

  it("splitting a caption splits its words", () => {
    const p = project();
    p.tracks.push({ id: "t", type: "text", name: "Captions", muted: false, hidden: false, locked: false, volume: 1, clips: [
      T.newTextClip(2, 2, { content: "a b c", words: [{ text: "a", start: 0, end: 0.4 }, { text: "b", start: 0.8, end: 1.2 }, { text: "c", start: 1.4, end: 1.8 }],
        animIn: { type: "pop", duration: 0.3, easing: "out" } }),
    ] });
    const left = p.tracks[2].clips[0];
    const rightId = splitClip(p, left.id, 2.6)!;
    const right = p.tracks[2].clips.find((c) => c.id === rightId)!;
    expect((left.text as { content: string }).content).toBe("a");
    expect((right.text as { content: string; words: { start: number }[] }).content).toBe("b c");
    expect((right.text as { words: { start: number }[] }).words[0].start).toBeCloseTo(0.2, 3);
    expect((right.text as { animIn: { type: string } }).animIn.type).toBe("none");
  });

  it("caption segments: short, broken at pauses and sentence ends", () => {
    const w = (text: string, start: number, end: number) => ({ text, start, end });
    const segs = T.segmentWords([w("Welcome", 0, 0.4), w("back", 0.4, 0.7), w("to", 0.7, 0.8), w("the", 0.8, 0.9), w("channel.", 0.9, 1.4),
      w("Today", 2.2, 2.5), w("we", 2.5, 2.6)]);
    expect(segs.map((s) => s.map((x) => x.text).join(" "))).toEqual(["Welcome back to the", "channel.", "Today we"]);
  });

  it("adds a caption track with clip-relative word times", () => {
    const p = project();
    const r = T.addCaptions(p, [{ text: "Hello", start: 1, end: 1.4 }, { text: "world", start: 1.4, end: 2 }], {}, { x: 0, y: 380 });
    const t = p.tracks.find((x) => x.id === r.trackId)!;
    expect(t.name).toBe("Captions");
    expect(t.clips[0].start).toBe(1);
    expect((t.clips[0].text as { words: { start: number }[] }).words.map((x) => x.start)).toEqual([0, 0.4]);
  });
});
