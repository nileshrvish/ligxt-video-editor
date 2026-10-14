import { describe, expect, it } from "vitest";
import shared from "../../../shared/follow_cases.json";
import { autoAt } from "../motion/motion";
import type { Clip, MediaMeta, Project } from "../types";
import * as F from "./follow";
import type { MotionTrack } from "./types";

const close = (a: number, b: number, d = 6) => expect(a).toBeCloseTo(b, d);
const arr = (a: number[] | null | undefined, b: number[] | null | undefined, d = 6) => {
  expect(!!a && a.length > 0).toBe(!!b && b.length > 0);
  if (!a || !b) return;
  expect(a.length).toBe(b.length);
  a.forEach((v, i) => close(v, b[i], d));
};

describe("tracking maths matches backend/app/follow.py (shared/follow_cases.json)", () => {
  it("samples a track (interpolated, frame on screen, held at the ends)", () =>
    shared.trackAt.forEach((c) => {
      const v = F.trackAt(shared.trackA as unknown as MotionTrack, c.s, c.interp)!;
      for (const k of ["x", "y", "w", "h", "a", "c"] as const) close(v[k], (c.v as Record<string, number>)[k], 8);
    }));
  it("smoothing strength and the Whittaker smoother", () => {
    shared.lam.forEach((c) => close(F.lam(c.a, c.fps), c.v, 6));
    shared.smooth.forEach((c) => arr(F.smooth(c.y, c.w, c.lmb), c.z, 7));
    arr(F.unwrap(shared.unwrap.a), shared.unwrap.v, 9);
  });
  it("resolves reframe, followers (scale, rotation, safe area, a deleted host clip) and a tracked region", () => {
    const sizes = shared.sizes as unknown as Record<string, [number, number]>;
    const got = F.resolve(shared.project as unknown as Project, shared.metas as unknown as Record<string, MediaMeta>, 30,
      (c: Clip) => sizes[c.id] ?? null, (c: Clip) => c.id !== "m2");
    const want = shared.resolved as unknown as Record<string, { autoMotion?: Record<string, number[] | null>; autoRegion?: Record<string, number[]> }>;
    expect([...got.keys()].sort()).toEqual(Object.keys(want).sort());
    for (const [id, w] of Object.entries(want)) {
      const g = got.get(id)!;
      if (w.autoMotion) {
        for (const k of ["x", "y", "s", "r"]) arr((g.autoMotion as unknown as Record<string, number[] | null>)[k], w.autoMotion[k], 5);
      }
      if (w.autoRegion) for (const k of ["x", "y", "w", "h"]) arr((g.autoRegion as unknown as Record<string, number[]>)[k], w.autoRegion[k], 5);
    }
    shared.autoAt.forEach((c) => {
      const v = autoAt(got.get(c.clip)!.autoMotion!, c.tau)!;
      for (const k of ["x", "y", "s", "r"] as const) close(v[k], (c.v as Record<string, number>)[k], 5);
    });
  });
});
