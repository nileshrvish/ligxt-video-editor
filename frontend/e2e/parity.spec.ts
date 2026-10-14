// Preview vs export for motion tracking: an auto-reframed landscape clip in a 9:16 frame, text that follows the
// tracked object and a pixelate region on it. The preview canvas and the exported frames must line up (block
// averages, with a shift search: the best match must be at zero shift).
import { expect, test, type APIRequestContext } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const FIX = fileURLToPath(new URL(".fixtures", import.meta.url));
const W = 360, H = 640, FPS = 25, BX = 9, BY = 16;

async function waitJob(request: APIRequestContext, id: string, timeout = 120_000) {
  const t0 = Date.now();
  for (;;) {
    const j = await (await request.get(`/api/jobs/${id}`)).json();
    if (["done", "error", "cancelled"].includes(j.status)) return j;
    if (Date.now() - t0 > timeout) throw new Error(`job ${id} timed out`);
    await new Promise((r) => setTimeout(r, 250));
  }
}

/** Mean RGB of BX x BY blocks of an RGB image (w x h), shifted by (dx, dy) pixels. */
function blocks(px: Uint8Array | number[], w: number, h: number, dx = 0, dy = 0): number[] {
  const out: number[] = [];
  const bw = w / BX, bh = h / BY;
  for (let by = 0; by < BY; by++)
    for (let bx = 0; bx < BX; bx++) {
      const s = [0, 0, 0];
      let n = 0;
      for (let y = Math.floor(by * bh); y < Math.floor((by + 1) * bh); y++)
        for (let x = Math.floor(bx * bw); x < Math.floor((bx + 1) * bw); x++) {
          const xx = Math.min(w - 1, Math.max(0, x + dx)), yy = Math.min(h - 1, Math.max(0, y + dy));
          const i = (yy * w + xx) * 3;
          s[0] += px[i]; s[1] += px[i + 1]; s[2] += px[i + 2];
          n++;
        }
      out.push(s[0] / n, s[1] / n, s[2] / n);
    }
  return out;
}

// Playwright's bundled Chromium has no H.264 decoder: play the clip in the system Edge (headless, its own temp profile).
test.use({ channel: "msedge" });

const mae = (a: number[], b: number[]) => a.reduce((t, v, i) => t + Math.abs(v - b[i]), 0) / a.length;

test("tracking: preview and export line up (reframe + following text + pixelate region)", async ({ page, request }) => {
  const r = await request.post("/api/media/import?filename=object.mp4", { data: readFileSync(`${FIX}/object.mp4`), headers: { "Content-Type": "application/octet-stream" } });
  const imp = await r.json();
  expect((await waitJob(request, imp.jobId)).status).toBe("done");
  const mid = imp.media.id as string;
  // track the object from 0.5 s (box around its known position)
  const cx = 120 + 100 * 0.5, cy = 180 + 50 * Math.sin(0.8);
  const tj = await (await request.post("/api/tracking/start", {
    data: { mediaId: mid, method: "box", seed: { t: 0.52, box: [(cx - 30) / 640, (cy - 30) / 360, 60 / 640, 60 / 360] }, range: [0, 4], direction: "both" },
  })).json();
  const done = await waitJob(request, tj.jobId);
  expect(done.status).toBe("done");
  const track = done.result.track;
  const p = await (await request.post("/api/projects", { data: { name: "Parity", aspect: "9:16" } })).json();
  p.settings = { ...p.settings, width: W, height: H, fps: FPS, background: { mode: "fill", color: "#000000" } };
  p.export = { ...p.export, width: W, height: H, fps: FPS, preset: "custom" };
  p.media = [mid];
  p.motionTracks = [track];
  p.tracks[0].clips = [{ id: "v", mediaId: mid, start: 0, inPoint: 0.2, outPoint: 2.2, speed: 1, fit: "fill",
    reframe: { trackId: track.id, third: "center", smoothing: 0.4 } }];
  const text = p.tracks.find((t: { type: string }) => t.type === "text");
  text.clips = [{ id: "tx", mediaId: null, start: 0, inPoint: 0, outPoint: 2.0, transform: { x: 0, y: -110, scale: 1, rotation: 0, opacity: 1, flipH: false, flipV: false, crop: { left: 0, top: 0, right: 0, bottom: 0 } },
    keyframes: {}, filters: [], text: { content: "PINNED", font: "Anton-Regular", size: 56, color: "#ffffff" }, follow: { trackId: track.id, clipId: "v", smoothing: 0.2 } }];
  const ov = p.tracks.find((t: { type: string }) => t.type === "overlay");
  ov.clips = [{ id: "rg", mediaId: null, adjustment: true, start: 0, inPoint: 0, outPoint: 2.0, region: { effect: "pixelate", amount: 60, shape: "ellipse", padding: 0.3, feather: 0.2 },
    follow: { trackId: track.id, clipId: "v", smoothing: 0.15 }, filters: [], keyframes: {}, transform: { x: 0, y: 0, scale: 1, rotation: 0, opacity: 1, flipH: false, flipV: false, crop: { left: 0, top: 0, right: 0, bottom: 0 } } }];
  expect((await request.put(`/api/projects/${p.id}`, { data: p })).ok()).toBeTruthy();
  const ex = await (await request.post(`/api/projects/${p.id}/export`, { data: { mode: "final" } })).json();
  const ej = await waitJob(request, ex.jobId, 180_000);
  expect(ej.status, ej.error).toBe("done");

  page.on("console", (m) => { if (m.type() !== "debug") console.log("[browser]", m.type(), m.text().slice(0, 300)); });
  page.on("pageerror", (e) => console.log("[pageerror]", e.message.slice(0, 400)));
  await page.addInitScript(() => localStorage.setItem("ligxt.tour.done", "1"));
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto(`/#/p/${p.id}`);
  await expect(page.locator(".canvas-frame canvas")).toBeVisible();
  for (const k of [6, 22, 40]) {
    const t = k / FPS;
    await page.evaluate((tt) => (window as unknown as { __ligxtStore: { getState: () => { setPlayhead: (v: number) => void } } }).__ligxtStore.getState().setPlayhead(tt), t);
    await page.waitForFunction((tt) => Math.abs(((window as unknown as { __ligxtPreview: { lastFrameT: number } }).__ligxtPreview.lastFrameT ?? -1) - tt) < 1e-6, t, { timeout: 20_000 }).catch(async (e) => { console.log("state", JSON.stringify(await page.evaluate(() => { const w = window as any; return { last: w.__ligxtPreview.lastFrameT, ph: w.__ligxtStore.getState().playhead, dbg: w.__ligxtPreview.debugState() }; })).slice(0, 1500)); throw e; });
    await page.waitForTimeout(300); // let the frame settle (seek + redraw)
    await page.waitForFunction((tt) => Math.abs(((window as unknown as { __ligxtPreview: { lastFrameT: number } }).__ligxtPreview.lastFrameT ?? -1) - tt) < 1e-6, t);
    const prev = await page.evaluate(([w, h]) => {
      const cv = document.querySelector(".canvas-frame canvas") as HTMLCanvasElement;
      const c2 = document.createElement("canvas");
      c2.width = w; c2.height = h;
      const g = c2.getContext("2d")!;
      g.drawImage(cv, 0, 0, w, h);
      const d = g.getImageData(0, 0, w, h).data;
      const out: number[] = [];
      for (let i = 0; i < d.length; i += 4) out.push(d[i], d[i + 1], d[i + 2]);
      return out;
    }, [W, H]);
    const raw = execFileSync("ffmpeg", ["-v", "error", "-i", ej.result.path, "-vf", `select=eq(n\\,${k})`, "-vsync", "0", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]);
    const exp = new Uint8Array(raw);
    const base = blocks(exp, W, H);
    const errs: [number, number, number][] = [];
    for (const dx of [-6, -3, 0, 3, 6]) for (const dy of [-6, 0, 6]) errs.push([mae(blocks(prev, W, H, dx, dy), base), dx, dy]);
    errs.sort((a, b) => a[0] - b[0]);
    const zero = errs.find((e) => e[1] === 0 && e[2] === 0)![0];
    console.log(`frame ${k}: block MAE ${zero.toFixed(2)}/255 at zero shift; best ${errs[0][0].toFixed(2)} at (${errs[0][1]}, ${errs[0][2]})`);
    expect(errs[0][1] === 0 && errs[0][2] === 0).toBeTruthy();
    expect(zero).toBeLessThan(8);
  }
});
