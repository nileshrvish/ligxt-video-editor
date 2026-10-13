import { describe, expect, it } from "vitest";
import cases from "../../../shared/text_cases.json";
import type { Clip } from "../types";
import { state, textOps, type TextLayout } from "./anim";

describe("text draw operations match the export (shared/text_cases.json)", () => {
  for (const c of cases.cases) {
    it(c.name, () => {
      for (const f of c.ops) {
        const got = textOps(c.layout as unknown as TextLayout, c.clip as unknown as Clip, f.tau, cases.W, cases.H);
        expect(got.length, `${c.name} @${f.tau}`).toBe(f.ops.length);
        got.forEach((o, i) => {
          const w = f.ops[i];
          expect(o.img).toBe(w.img);
          o.tile.forEach((v, j) => expect(v).toBeCloseTo(w.tile[j], 6));
          o.m.forEach((v, j) => expect(v, `${c.name} @${f.tau} op ${i} m[${j}]`).toBeCloseTo(w.m[j], 6));
          expect(o.alpha).toBeCloseTo(w.alpha, 6);
        });
      }
    });
  }

  it("typewriter reveals characters one by one, then removes them from the end", () => {
    const s = { animIn: { type: "typewriter", duration: 1, easing: "linear" }, animOut: { type: "typewriter", duration: 1, easing: "linear" } };
    const vis = (tau: number) => state(s, 10, 2, tau, 4).units.filter((u) => u.a > 0).length;
    expect([vis(0), vis(0.25), vis(0.5), vis(1), vis(2), vis(3.5), vis(4)]).toEqual([0, 2, 5, 10, 10, 5, 0]);
  });

  it("karaoke: the spoken word is highlighted, the last one stays lit until the next starts", () => {
    const words = [{ text: "a", start: 0, end: 0.2 }, { text: "b", start: 0.5, end: 0.7 }];
    const s = { highlight: { style: "color", color: "#f00" }, words };
    expect(state(s, 3, 2, 0.1, 2).units.map((u) => u.hi)).toEqual([1, 0]);
    expect(state(s, 3, 2, 0.4, 2).units.map((u) => u.hi)).toEqual([1, 0]);
    expect(state(s, 3, 2, 0.6, 2).units.map((u) => u.hi)).toEqual([0, 1]);
  });
});
