import { describe, expect, it } from "vitest";
import type { Clip } from "../types";
import { fadeGain, layerGeometry } from "./geometry";

const clip = (extra: Partial<Clip> = {}): Clip => ({
  id: "c", mediaId: "m", start: 0, inPoint: 0, outPoint: 1, speed: 1, reverse: false, freezeFrame: null,
  volume: 1, muted: false, fadeIn: 0, fadeOut: 0, fit: null,
  transform: { x: 0, y: 0, scale: 1, rotation: 0, flipH: false, flipV: false, opacity: 1, crop: { left: 0, top: 0, right: 0, bottom: 0 } },
  keyframes: {}, filters: [], text: null, ...extra,
});

describe("layerGeometry (mirrors render.overlay_geometry)", () => {
  it("fits 16:9 into 9:16 with letterbox", () => {
    const g = layerGeometry(1920, 1080, clip(), true, "fit", 1080, 1920);
    expect([g.dw, g.dh, g.cx, g.cy]).toEqual([1080, 607.5, 540, 960]);
  });
  it("fill covers and crops to the canvas", () => {
    const g = layerGeometry(1920, 1080, clip({ fit: "fill" }), true, "fit", 1080, 1920);
    expect(g.dh).toBe(1920);
    expect([g.cw, g.ch]).toEqual([1080, 1920]);
  });
  it("main track ignores transform; blur flag", () => {
    const c = clip({ fit: "blur", transform: { ...clip().transform, scale: 0.5, x: 100 } });
    const g = layerGeometry(1920, 1080, c, true, "fit", 1080, 1920);
    expect([g.dw, g.cx, g.blurBackground]).toEqual([1080, 540, true]);
  });
  it("overlay: 400x400 fitted, scaled 0.5, moved up 200 (same as the export test)", () => {
    const c = clip({ transform: { ...clip().transform, scale: 0.5, y: -200, opacity: 0.5 } });
    const g = layerGeometry(400, 400, c, false, "fit", 1080, 1920);
    expect([g.dw, g.dh, g.cx, g.cy, g.opacity]).toEqual([540, 540, 540, 760, 0.5]);
  });
  it("fill overlay crop is canvas * scale", () => {
    const c = clip({ fit: "fill", transform: { ...clip().transform, scale: 0.5 } });
    const g = layerGeometry(1280, 720, c, false, "fit", 1080, 1920);
    expect([g.cw, g.ch]).toEqual([540, 960]);
  });
  it("overlays default to fit even if the project default is blur", () => {
    expect(layerGeometry(400, 400, clip(), false, "blur", 1080, 1920).blurBackground).toBe(false);
  });
});

describe("fadeGain", () => {
  it("ramps in and out", () => {
    expect(fadeGain(0.5, 10, 1, 2)).toBe(0.5);
    expect(fadeGain(5, 10, 1, 2)).toBe(1);
    expect(fadeGain(9, 10, 1, 2)).toBe(0.5);
    expect(fadeGain(0, 10, 0, 0)).toBe(1);
  });
});
