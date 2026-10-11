import { describe, expect, it } from "vitest";
import { valueAt } from "./motion";
import { kenBurns, pan, punchIn, shake } from "./presets";

describe("motion presets", () => {
  it("Ken Burns zooms smoothly and keeps the focal point still", () => {
    const k = kenBurns(4, "in", 0.12, { x: 100, y: -200 });
    expect(valueAt(k.scale, 0, 1)).toBe(1);
    expect(valueAt(k.scale, 4, 1)).toBeCloseTo(1.12);
    for (const t of [0, 1.3, 2, 4]) {
      const s = valueAt(k.scale, t, 1);
      // focal point p maps to s*p + offset; it must stay at p
      expect(s * 100 + valueAt(k.x, t, 0)).toBeCloseTo(100, 6);
      expect(s * -200 + valueAt(k.y, t, 0)).toBeCloseTo(-200, 6);
    }
    expect(valueAt(k.scale, 0.2, 1) - 1).toBeLessThan(0.01); // eased start, no jump
  });
  it("pan never shows past the picture edge", () => {
    const p = pan(3, "left", 1080, 1920, 1080, 1920);
    const z = p.scale[0].value;
    for (const t of [0, 1.5, 3]) expect(Math.abs(valueAt(p.x, t, 0))).toBeLessThanOrEqual((z * 1080 - 1080) / 2 + 1e-6);
    expect(valueAt(p.x, 0, 0)).toBeGreaterThan(0);
    expect(valueAt(p.x, 3, 0)).toBeLessThan(0);
  });
  it("punch-in rises fast, overshoots a little and settles back to 1", () => {
    const k = punchIn([], 1);
    expect(valueAt(k, 0.99, 1)).toBe(1);
    expect(valueAt(k, 1.05, 1)).toBeGreaterThan(1.05);
    expect(valueAt(k, 1.1, 1)).toBeCloseTo(1.1);
    expect(valueAt(k, 1.4, 1)).toBe(1);
    expect(punchIn(punchIn([], 1), 1).length).toBe(3); // re-applying replaces, not duplicates
  });
  it("shake is deterministic and decays to rest", () => {
    const a = shake({}, 2), b = shake({}, 2);
    expect(a).toEqual(b);
    expect(valueAt(a.shakeX, 2, 0)).toBe(0);
    expect(valueAt(a.shakeX, a.shakeX[a.shakeX.length - 1].time, 0)).toBe(0); // ends at rest
    expect(Math.max(...a.shakeX.map((k) => Math.abs(k.value)))).toBeGreaterThan(5);
  });
});
