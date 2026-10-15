import { describe, expect, it } from "vitest";
import * as T from "../text/edit";
import type { Project } from "../types";
import { canRun, presenceCells, presenceSummary } from "./model";

const project = (): Project => ({
  version: 1, id: "p", name: "p", created: 0, updated: 0,
  settings: { width: 1080, height: 1920, fps: 30, aspect: "9:16", background: { mode: "fill", color: "#000" }, sampleRate: 48000 },
  media: [], tracks: [{ id: "v", type: "video", name: "Main", muted: false, hidden: false, locked: false, volume: 1, clips: [] }],
  transitions: [], markers: [],
  export: { preset: "reels", mode: "final", width: 1080, height: 1920, fps: 30, range: null, fileName: "" },
} as unknown as Project);

const lines: T.TimedWord[][] = [
  [{ text: "we", start: 1, end: 1.4 }, { text: "were", start: 1.4, end: 1.9 }],
  [{ text: "young", start: 3, end: 3.6 }],
];
const tr: T.LyricsTranslation = { lines: ["nous étions", "jeunes"], from: "en", to: "fr", model: "good" };
const style = { size: 80, lineHeight: 1.1 } as const;
type TextOf = { content: string; size?: number; words?: unknown[]; translation?: { source: string; from: string; to: string }; animIn: { type: string } };

describe("lyrics translation layouts", () => {
  it("two lines: the lyrics with word timings + the translation below, smaller, same line timing", () => {
    const p = project();
    const r = T.addLyrics(p, lines, style, { x: 0, y: 380 }, tr, "two");
    const lyr = p.tracks.find((t) => t.id === r.trackId)!;
    const tl = p.tracks.find((t) => t.id === r.translationTrackId)!;
    expect(lyr.name).toBe("Lyrics");
    expect(tl.name).toBe("Lyrics (translation)");
    expect(lyr.clips.map((c) => (c.text as TextOf).content)).toEqual(["we were", "young"]);
    expect(tl.clips.map((c) => (c.text as TextOf).content)).toEqual(["nous étions", "jeunes"]);
    // same timing per line: start at the first word, end at the last word + 0.25 (never past the next line)
    expect(lyr.clips.map((c) => [c.start, c.outPoint - c.inPoint])).toEqual([[1, 1.15], [3, 0.85]]);
    expect(tl.clips.map((c) => [c.start, c.outPoint - c.inPoint])).toEqual([[1, 1.15], [3, 0.85]]);
    const t0 = tl.clips[0].text as TextOf;
    expect(t0.words).toBeUndefined(); // line-level: no word timings
    expect(t0.animIn.type).toBe("fade");
    expect(t0.size).toBe(Math.round(80 * T.TRANSLATION_SCALE));
    expect(t0.translation).toMatchObject({ source: "we were", from: "en", to: "fr" });
    expect(tl.clips[0].transform.y).toBeGreaterThan(lyr.clips[0].transform.y); // below the lyrics line
    expect(tl.clips[0].transform.y).toBe(T.translationPosition(style, { x: 0, y: 380 }).y);
    expect((lyr.clips[0].text as TextOf).words).toHaveLength(2);
  });

  it("translation only replaces the lyrics on the Lyrics track; original ignores the translation", () => {
    const p = project();
    const r = T.addLyrics(p, lines, style, { x: 0, y: 380 }, tr, "translation");
    expect(r.translationTrackId).toBeUndefined();
    const lyr = p.tracks.find((t) => t.id === r.trackId)!;
    expect(lyr.clips.map((c) => (c.text as TextOf).content)).toEqual(["nous étions", "jeunes"]);
    expect(lyr.clips[1].start).toBe(3);
    const q = project();
    const r2 = T.addLyrics(q, lines, style, { x: 0, y: 380 }, tr, "original");
    expect(q.tracks.find((t) => t.id === r2.trackId)!.clips.map((c) => (c.text as TextOf).content)).toEqual(["we were", "young"]);
    expect(q.tracks.filter((t) => t.type === "text")).toHaveLength(1);
  });

  it("empty lines are dropped together with their translation", () => {
    const p = project();
    const r = T.addLyrics(p, [lines[0], [{ text: " ", start: 2, end: 2.5 }], lines[1]], style, { x: 0, y: 0 },
      { ...tr, lines: ["nous étions", "", "jeunes"] }, "two");
    expect(p.tracks.find((t) => t.id === r.translationTrackId)!.clips.map((c) => (c.text as TextOf).content)).toEqual(["nous étions", "jeunes"]);
  });
});

describe("lyrics dialog: look up + vocal presence", () => {
  it("look up needs a chosen result or LRC text", () => {
    expect(canRun("lyrics", "lookup", { hasClip: true, text: "", taps: 0 }).ok).toBe(false);
    expect(canRun("lyrics", "lookup", { hasClip: true, text: "", taps: 0, lookupId: 12 }).ok).toBe(true);
    expect(canRun("lyrics", "lookup", { hasClip: true, text: "", taps: 0, lrc: "[00:01.00]x" }).ok).toBe(true);
  });
  it("the presence strip maps seconds of the song to the timeline (clip start, speed)", () => {
    const cells = presenceCells([0, 0.2, 0.6, 1], { start: 4, speed: 2 });
    expect(cells.map((c) => [c.t0, c.t1])).toEqual([[4, 4.5], [4.5, 5], [5, 5.5], [5.5, 6]]);
    expect(cells.map((c) => c.level)).toEqual(["none", "low", "mid", "high"]);
    expect(presenceSummary([0, 0.2, 0.6, 1])).toBe("vocals in 2 of 4 s");
  });
});
