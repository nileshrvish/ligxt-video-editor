import { describe, expect, it } from "vitest";
import shared from "../../../shared/motion_cases.json";
import type { Clip, Keyframe } from "../types";
import * as M from "./motion";

const C = shared.cases;
const close = (a: number, b: number) => expect(a).toBeCloseTo(b, 9);

describe("motion maths matches backend/app/motion.py (shared/motion_cases.json)", () => {
  it("easings", () => C.ease.forEach((c) => close(M.ease(c.name, c.u), c.v)));
  it("keyframe interpolation", () => C.valueAt.forEach((c) => close(M.valueAt(c.kfs as Keyframe[], c.t, c.default), c.v)));
  it("layer matrices and inverses", () =>
    C.matrix.forEach((c) => {
      const { m, rect } = M.layerMatrix(c.src[0], c.src[1], { left: 0, top: 0, right: 0, bottom: 0, ...c.crop }, c.fill, 1080, 1920, c.t as M.TransformValues);
      m.forEach((v, i) => close(v, c.m[i]));
      rect.forEach((v, i) => close(v, c.rect[i]));
      M.invert(m).forEach((v, i) => close(v, c.inv[i]));
    }));
  it("speed, ramps, reverse, freeze", () =>
    C.time.forEach((c) => {
      const clip = { freezeFrame: null, reverse: false, speedRamp: null, ...c.clip } as unknown as Clip;
      close(M.clipDuration(clip), c.duration);
      c.samples.forEach(([t, src]) => close(M.srcTime(clip, t).src, src));
    }));
  it("ducking regions and gain", () =>
    C.duck.forEach((c) => {
      const regs = M.duckRegions(c.sources as [number, number, number[]][]);
      expect(regs.length).toBe(c.regions.length);
      regs.forEach((r, i) => { close(r[0], c.regions[i][0]); close(r[1], c.regions[i][1]); });
      c.gains.forEach(([t, g]) => close(M.duckGain(regs, t), g));
    }));
});

describe("punch and shake channels layer on top of the base movement", () => {
  it("matches the backend", () =>
    shared.cases.channels.forEach((c) => {
      const v = M.transformAt(c.clip as unknown as Clip, c.tau);
      for (const k of ["x", "y", "scale", "rotation", "opacity"] as const) close(v[k], (c.v as unknown as Record<string, number>)[k]);
    }));
});
