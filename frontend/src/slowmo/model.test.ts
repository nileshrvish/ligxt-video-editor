import { describe, expect, it } from "vitest";
import data from "../../../shared/slowmo_cases.json";
import { minSpeed, needsRetime, normalizeSlowmo, qualityOf, SLOWMO_DEFAULT } from "./model";
import type { Clip } from "../types";

describe("slow-mo rules match the backend (shared/slowmo_cases.json)", () => {
  it("quality, slowest speed and retiming", () => {
    for (const c of data.cases) {
      const clip = c.clip as unknown as Clip;
      expect(qualityOf(clip)).toBe(c.quality);
      expect(minSpeed(clip)).toBeCloseTo(c.minSpeed, 6);
      expect(needsRetime(clip, c.media as never, c.fps)).toBe(c.needsRetime);
    }
  });
  it("defaults", () => {
    expect(SLOWMO_DEFAULT).toEqual(data.defaults);
    expect(normalizeSlowmo({ quality: "nope" as never })).toEqual({ quality: "auto", smooth: false });
  });
});
