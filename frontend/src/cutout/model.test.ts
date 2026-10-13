import { describe, expect, it } from "vitest";
import cases from "../../../shared/cutout_cases.json";
import { CUTOUT_DEFAULT, glowLevels, normalizeCutout, pulseAt, timelineBeats, type GlowPulse } from "./model";
import type { Project } from "../types";

describe("cutout maths match the export (shared/cutout_cases.json)", () => {
  it("pulse envelope", () => {
    for (const c of cases.pulse) expect(pulseAt(c.pulse as GlowPulse, c.t, c.beats)).toBeCloseTo(c.value, 9);
  });
  it("glow levels", () => {
    for (const c of cases.glow) {
      const v = glowLevels(c.glow as never, c.p, c.k);
      v.forEach((x, i) => expect(x).toBeCloseTo(c.value[i], 6));
    }
  });
  it("beats on the timeline", () => {
    expect(timelineBeats(cases.beats.project as unknown as Project, cases.beats.media)).toEqual(cases.beats.value);
  });
  it("defaults are the backend's", () => {
    expect(JSON.parse(JSON.stringify(CUTOUT_DEFAULT))).toEqual(cases.defaults);
  });
  it("normalize fills nested defaults", () => {
    const n = normalizeCutout({ enabled: true, glow: { enabled: true } as never });
    expect(n.glow.width).toBe(14);
    expect(n.glow.pulse.mode).toBe("none");
    expect(n.refine.smooth).toBe(0.5);
  });
});
