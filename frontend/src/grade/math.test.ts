import { describe, expect, it } from "vitest";
import shared from "../../../shared/grade_cases.json";
import type { Filter } from "../types";
import tealOrange from "../../../luts/teal-orange.cube?raw";
import { buildLut, effects, gradeColor, needsGrade, parseCube, type Cube, type RGB } from "./math";

const cubes: Record<string, Cube> = {
  "builtin/teal-orange": parseCube(tealOrange),
};

describe("gradeColor matches backend/app/grade.py (shared/grade_cases.json)", () => {
  for (const c of shared.cases) {
    it(c.name, () => {
      c.colors.forEach((col, i) => {
        const out = gradeColor(col as RGB, c.filters as unknown as Filter[], cubes);
        out.forEach((v, ch) => expect(v).toBeCloseTo(c.expect[i][ch], 4));
      });
    });
  }
});

describe("grade helpers", () => {
  it("LUT is the identity with no filters, in .cube order", () => {
    const lut = buildLut([], {}, 5);
    expect([...lut.slice(3, 6)]).toEqual([0.25, 0, 0]); // second entry: red changes fastest
    expect(lut[(4 + 4 * 5 + 4 * 25) * 3 + 2]).toBe(1);
  });
  it("preset effects scale with intensity and add to explicit effects", () => {
    const fx = effects([
      { type: "preset", name: "vintage70s", intensity: 0.5, params: {}, enabled: true },
      { type: "effect", name: "grain", intensity: 1, params: { amount: 0.1, size: 3 }, enabled: true },
    ] as Filter[]);
    expect(fx.grain.amount).toBeCloseTo(0.25);
    expect(fx.grain.size).toBe(3);
    expect(fx.vignette.amount).toBeCloseTo(0.125);
  });
  it("needsGrade ignores disabled and zero filters", () => {
    expect(needsGrade([{ type: "adjust", params: { contrast: 0 }, enabled: true } as unknown as Filter])).toBe(false);
    expect(needsGrade([{ type: "preset", name: "bw", intensity: 1, params: {}, enabled: false } as unknown as Filter])).toBe(false);
    expect(needsGrade([{ type: "adjust", params: { blur: 10 }, enabled: true } as unknown as Filter])).toBe(true);
  });
});
