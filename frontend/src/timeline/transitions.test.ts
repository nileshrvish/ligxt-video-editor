import { describe, expect, it } from "vitest";
import shared from "../../../shared/transition_cases.json";
import type { Project } from "../types";
import * as ops from "./ops";
import { mainTransitionPlans, planTransition } from "./transitions";

describe("planTransition matches the backend (shared/transition_cases.json)", () => {
  for (const c of shared.cases) {
    it(c.name, () => {
      const a = { id: "A", speed: 1, ...c.a };
      const b = { id: "B", speed: 1, ...c.b };
      const src = (v: number | null) => (v == null ? Infinity : v);
      const plan = planTransition({ id: "t", type: "fade", duration: c.duration }, a, b, src(c.srcA), src(c.srcB));
      if (c.expect == null) return expect(plan).toBeNull();
      expect(plan!.status).toBe(c.expect.status);
      expect(plan!.half).toBeCloseTo(c.expect.half, 6);
      expect([plan!.freezeA, plan!.freezeB]).toEqual([c.expect.freezeA, c.expect.freezeB]);
      expect(plan!.message === "").toBe(c.expect.status === "ok");
    });
  }
});

const clip = (id: string, start: number, dur: number) => ({
  id, mediaId: "img", start, inPoint: 0, outPoint: dur, speed: 1, reverse: false, freezeFrame: null, volume: 1, muted: false,
  fadeIn: 0, fadeOut: 0, fit: null,
  transform: { x: 0, y: 0, scale: 1, rotation: 0, flipH: false, flipV: false, opacity: 1, crop: { left: 0, top: 0, right: 0, bottom: 0 } },
  keyframes: {}, filters: [], text: null,
});
const proj = (): Project => ({
  version: 1, id: "p", name: "p", created: 0, updated: 0,
  settings: { width: 1080, height: 1920, fps: 30, aspect: "9:16", background: { mode: "fit", color: "#000000" }, sampleRate: 48000 },
  media: [], transitions: [], markers: [],
  tracks: [{ id: "main", type: "video", name: "Main", muted: false, hidden: false, locked: false, volume: 1,
    clips: [clip("a", 0, 2), clip("b", 2, 2), clip("c", 4, 1), clip("d", 6, 1)] }],
  export: { preset: "reels", mode: "final", width: 1080, height: 1920, fps: 30, range: null, fileName: "" },
});
const photoDur: ops.MediaDuration = () => Infinity;

describe("transition ops", () => {
  it("sets, updates and removes a transition on a cut", () => {
    const p = proj();
    const id = ops.setTransition(p, "a", "b", { type: "whipleft", duration: 0.4 })!;
    expect(p.transitions).toHaveLength(1);
    ops.setTransition(p, "a", "b", { duration: 5 });
    expect(p.transitions[0]).toMatchObject({ type: "whipleft", duration: 2 });
    expect(ops.setTransition(p, "a", "c", { type: "fade" })).toBeNull(); // not neighbours
    ops.removeTransition(p, id);
    expect(p.transitions).toHaveLength(0);
  });

  it("applies to all cuts (skipping gaps) and plans them", () => {
    const p = proj();
    expect(ops.applyTransitionToAll(p, "main", "fade", 0.6)).toBe(2); // a|b, b|c; c..d has a gap
    const plans = mainTransitionPlans(p, photoDur);
    expect([...plans.keys()]).toEqual(["a", "b"]);
    expect(plans.get("b")!.half).toBeCloseTo(0.3); // 0.6 s fits; c is 1 s long (cap 0.5)
  });

  it("moving a clip away drops its transitions", () => {
    const p = proj();
    ops.applyTransitionToAll(p, "main", "fade", 0.5);
    ops.moveClip(p, "a", 10);
    expect(p.transitions.map((t) => t.fromClipId)).toEqual(["b"]);
  });
});
