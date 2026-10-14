import { describe, expect, it } from "vitest";
import { excludeClip, locksOf, swap, toggleLock, type Draft, type Pick, type Slot } from "./model";

const pk = (m: string, shot = 0, inn = 0): Pick => ({ mediaId: m, shot, in: inn, out: inn + 1, kind: "wide", thumb: "", name: m, score: 1, reasons: [] });
const slot = (i: number, p: Pick, alts: Pick[] = []): Slot => ({ i, section: "peak", b0: i, b1: i + 1, start: i, dur: 1, speed: 1, pick: p, alternatives: alts });
const draft = (slots: Slot[]): Draft => ({
  style: "energetic", styleLabel: "Energetic", template: null, media: ["a", "b", "c"], exclude: [], music: "s", musicStart: 0, length: 3, dropAt: 1,
  slots, aspect: "9:16", texts: {}, shotsUsed: 3, shotsAvailable: 3,
});

describe("auto-edit review edits", () => {
  it("lock, swap and the locks sent when regenerating", () => {
    let d = draft([slot(0, pk("a")), slot(1, pk("b"), [pk("c", 2, 1.5), pk("a", 1)])]);
    expect(locksOf(d)).toEqual({});
    d = toggleLock(d, 0);
    expect(locksOf(d)).toEqual({ "0": { mediaId: "a", shot: 0, in: 0 } });
    d = swap(d, 1, d.slots[1].alternatives[0]);
    expect(d.slots[1].pick).toMatchObject({ mediaId: "c", shot: 2, in: 1.5, locked: true });
    expect(d.slots[1].alternatives.map((a) => a.mediaId)).toEqual(["b", "a"]); // the old pick is an alternative now
    expect(Object.keys(locksOf(d))).toEqual(["0", "1"]);
    d = toggleLock(d, 0);
    expect(Object.keys(locksOf(d))).toEqual(["1"]);
  });
  it("excluding a clip drops its locks", () => {
    let d = draft([slot(0, pk("a")), slot(1, pk("b"))]);
    d = toggleLock(toggleLock(d, 0), 1);
    const x = excludeClip(d, "a");
    expect(x.exclude).toEqual(["a"]);
    expect(Object.values(x.locks).map((l) => l.mediaId)).toEqual(["b"]);
  });
});
