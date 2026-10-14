// Tracked regions in the preview: blur / pixelate / grade inside a feathered ellipse or box that follows a
// face or object (export: backend/app/track_render.py, same sizes and the same smoothstep edge).
import type { Region } from "../tracking/types";

export const BLUR_MAX = 40; // gaussian sigma at amount 100 (reference px, 1080 short side)
export const PIXEL_MIN = 4, PIXEL_MAX = 60;

export function regionParams(reg: Region | null | undefined) {
  return {
    effect: reg?.effect ?? "blur",
    amount: Math.max(0, Math.min(100, reg?.amount ?? 70)),
    shape: reg?.shape ?? "ellipse",
    feather: Math.max(0, Math.min(1, reg?.feather ?? 0.2)),
    padding: Math.max(0, reg?.padding ?? 0.25),
  };
}

export const blurSigma = (amount: number, rs: number) => (amount / 100) * BLUR_MAX * rs;
export const pixelBlock = (amount: number, rs: number) => Math.max(2, Math.round((PIXEL_MIN + ((PIXEL_MAX - PIXEL_MIN) * amount) / 100) * rs));

const canvases = { eff: document.createElement("canvas"), small: document.createElement("canvas"), mask: document.createElement("canvas") };

function sized(cv: HTMLCanvasElement, w: number, h: number) {
  if (cv.width !== w || cv.height !== h) {
    cv.width = w;
    cv.height = h;
  }
  return cv.getContext("2d", { willReadFrequently: cv === canvases.mask })!;
}

/** The region's alpha (0..255) at preview scale k: smoothstep on the normalised distance, like mask_frame(). */
function drawMask(w: number, h: number, k: number, box: [number, number, number, number], shape: string, feather: number) {
  const [x, y, rw, rh] = box;
  const mc = sized(canvases.mask, w, h);
  mc.clearRect(0, 0, w, h);
  const X = x * k, Y = y * k, RW = rw * k, RH = rh * k;
  const x0 = Math.max(0, Math.floor(X - RW / 2 - 2)), x1 = Math.min(w, Math.ceil(X + RW / 2 + 3));
  const y0 = Math.max(0, Math.floor(Y - RH / 2 - 2)), y1 = Math.min(h, Math.ceil(Y + RH / 2 + 3));
  if (x1 <= x0 || y1 <= y0) return;
  const img = mc.createImageData(x1 - x0, y1 - y0);
  const f = Math.max(1e-3, feather);
  for (let yy = y0; yy < y1; yy++) {
    const ny = Math.abs(yy + 0.5 - Y) / Math.max(1, RH / 2);
    for (let xx = x0; xx < x1; xx++) {
      const nx = Math.abs(xx + 0.5 - X) / Math.max(1, RW / 2);
      const d = shape === "ellipse" ? Math.sqrt(nx * nx + ny * ny) : Math.max(nx, ny);
      const u = Math.min(1, Math.max(0, (1 - d) / f));
      img.data[((yy - y0) * (x1 - x0) + (xx - x0)) * 4 + 3] = Math.round(u * u * (3 - 2 * u) * 255);
    }
  }
  mc.putImageData(img, x0, y0);
}

/** Draw the region effect over `ctx` (the canvas composed so far is `target`). `grade` grades a canvas
 *  (the clip's filters) for effect "grade"; returns false while grading assets load. */
export function drawRegion(ctx: CanvasRenderingContext2D, target: HTMLCanvasElement, reg: Region | null | undefined,
  box: [number, number, number, number], k: number, rs: number, grade: (cv: HTMLCanvasElement) => HTMLCanvasElement | null): boolean {
  const p = regionParams(reg);
  const w = target.width, h = target.height;
  const ec = sized(canvases.eff, w, h);
  ec.setTransform(1, 0, 0, 1, 0, 0);
  ec.globalAlpha = 1;
  ec.globalCompositeOperation = "source-over";
  ec.clearRect(0, 0, w, h);
  if (p.effect === "pixelate") {
    // the export's grid: the whole canvas scaled down by the block size (area), then up (nearest)
    const b = pixelBlock(p.amount, rs);
    const W = w / k, H = h / k;
    const sw = Math.max(1, Math.round(W / b)), sh = Math.max(1, Math.round(H / b));
    const sc = sized(canvases.small, sw, sh);
    sc.imageSmoothingEnabled = true;
    sc.imageSmoothingQuality = "high";
    sc.drawImage(target, 0, 0, sw, sh);
    ec.imageSmoothingEnabled = false;
    ec.drawImage(canvases.small, 0, 0, w, h);
    ec.imageSmoothingEnabled = true;
  } else if (p.effect === "grade") {
    ec.drawImage(target, 0, 0);
    const g = grade(canvases.eff);
    if (!g) return false;
    if (g !== canvases.eff) {
      ec.clearRect(0, 0, w, h);
      ec.drawImage(g, 0, 0, w, h);
    }
  } else {
    ec.filter = `blur(${Math.max(0.3, blurSigma(p.amount, rs) * k)}px)`;
    ec.drawImage(target, 0, 0);
    ec.filter = "none";
  }
  drawMask(w, h, k, box, p.shape, p.feather);
  ec.globalCompositeOperation = "destination-in";
  ec.drawImage(canvases.mask, 0, 0);
  ec.globalCompositeOperation = "source-over";
  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.globalAlpha = 1;
  ctx.drawImage(canvases.eff, 0, 0);
  ctx.restore();
  return true;
}
