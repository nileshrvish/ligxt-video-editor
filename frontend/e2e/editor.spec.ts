// Headless UI flows: the Phase 10 slow-mo controls and the Phase 11 motion-tracking panel, preview layer and
// follow controls, driven like a user (clicks, drags, Ctrl+S) and checked through the saved project.
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const FIX = fileURLToPath(new URL(".fixtures", import.meta.url));

async function waitJob(request: APIRequestContext, id: string, timeout = 60_000) {
  const t0 = Date.now();
  for (;;) {
    const j = await (await request.get(`/api/jobs/${id}`)).json();
    if (["done", "error", "cancelled"].includes(j.status)) return j;
    if (Date.now() - t0 > timeout) throw new Error(`job ${id} timed out`);
    await new Promise((r) => setTimeout(r, 250));
  }
}

async function importClip(request: APIRequestContext, file: string) {
  const r = await request.post(`/api/media/import?filename=${file}`, { data: readFileSync(join(FIX, file)), headers: { "Content-Type": "application/octet-stream" } });
  expect(r.ok()).toBeTruthy();
  const body = await r.json();
  expect((await waitJob(request, body.jobId)).status).toBe("done");
  return (await (await request.get(`/api/media/${body.media.id}`)).json()) as { id: string };
}

async function newProject(request: APIRequestContext, mediaId: string, clip: Record<string, unknown>) {
  const p = await (await request.post("/api/projects", { data: { name: "UI test", aspect: "16:9" } })).json();
  p.media = [mediaId];
  p.tracks[0].clips = [{ id: "v", mediaId, start: 0, inPoint: 0.2, outPoint: 2.2, speed: 1, fit: "fit", ...clip }];
  expect((await request.put(`/api/projects/${p.id}`, { data: p })).ok()).toBeTruthy();
  return p.id as string;
}

async function open(page: Page, pid: string) {
  await page.addInitScript(() => localStorage.setItem("ligxt.tour.done", "1"));
  await page.goto(`/#/p/${pid}`);
  await expect(page.locator(".canvas-frame canvas")).toBeVisible();
  await page.locator('[data-clip-id="v"]').click();
}

async function saved(page: Page, request: APIRequestContext, pid: string) {
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("Control+s");
  await page.waitForTimeout(600);
  return (await request.get(`/api/projects/${pid}`)).json();
}

test("slow-mo controls: quality, smooth motion and the blended frames", async ({ page, request }) => {
  const m = await importClip(request, "object.mp4");
  const pid = await newProject(request, m.id, { speed: 0.5, outPoint: 1.2 });
  await open(page, pid);
  const q = page.locator('select[title^="How in-between frames"]');
  await expect(q).toBeVisible();
  await expect(q).toHaveValue("auto");
  await q.selectOption("blend");
  await expect(page.getByText(/Blending frames|✓ Frame blending/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/✓ Frame blending/)).toBeVisible({ timeout: 60_000 });
  await page.getByLabel(/Smooth motion/).check();
  const p = await saved(page, request, pid);
  expect(p.tracks[0].clips[0].slowmo).toMatchObject({ quality: "blend", smooth: true });
});

test("motion tracking: draw a box, pin text to it, fix a frame and re-track, auto-reframe", async ({ page, request }) => {
  const m = await importClip(request, "object.mp4");
  const pid = await newProject(request, m.id, {});
  await open(page, pid);
  await expect(page.getByText("Motion tracking")).toBeVisible();
  // a face: there is none in this clip
  await page.getByRole("button", { name: "Face", exact: true }).click();
  await expect(page.getByText("No face on this frame")).toBeVisible();
  // draw a box around the object (source frame at the playhead = 0.2 s: centre 140, 195.7 of 640x360)
  await page.getByRole("button", { name: "Draw a box" }).click();
  const fr = (await page.locator(".canvas-frame").boundingBox())!;
  const k = fr.width / 640;
  const cx = fr.x + 140 * k, cy = fr.y + (180 + 50 * Math.sin(0.32)) * k;
  await page.mouse.move(cx - 32 * k, cy - 32 * k);
  await page.mouse.down();
  await page.mouse.move(cx, cy, { steps: 4 });
  await page.mouse.move(cx + 32 * k, cy + 32 * k, { steps: 4 });
  await page.mouse.up();
  await expect(page.locator(".trk-row")).toHaveCount(1, { timeout: 30_000 });
  await expect(page.locator(".trk-name")).toHaveValue("Object 1");
  // the tracked box and its path are on the preview
  await expect(page.locator(".trk-box")).toBeVisible();
  // fix a later frame: step 10 frames on, drag the box a little, then re-track forward
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  for (let i = 0; i < 10; i++) await page.keyboard.press("ArrowRight");
  await expect(page.locator(".trk-box")).toBeVisible();
  const bb = (await page.locator(".trk-box").boundingBox())!;
  await page.mouse.move(bb.x + bb.width / 2, bb.y + bb.height / 2);
  await page.mouse.down();
  await page.mouse.move(bb.x + bb.width / 2 + 6, bb.y + bb.height / 2 + 4, { steps: 3 });
  await page.mouse.up();
  await expect(page.locator(".trk-box.fixed")).toBeVisible();
  await page.getByRole("button", { name: "Re-track forward" }).click();
  await expect(page.locator(".trk-box.fixed")).toHaveCount(0, { timeout: 30_000 });
  // auto-reframe on it
  await page.getByLabel(/Auto-reframe on a tracked subject/).check();
  await expect(page.getByText("Smooth camera")).toBeVisible();
  // text that follows it
  await page.getByTitle("Text that follows it").click();
  await expect(page.locator("select").filter({ hasText: "Object 1" })).toHaveValue(/trk_/);
  await page.getByLabel("Rotation").check();
  const p = await saved(page, request, pid);
  expect(p.motionTracks).toHaveLength(1);
  const tr = p.motionTracks[0];
  expect(tr.keys.length).toBe(2); // the start box + the fix
  const host = p.tracks[0].clips[0];
  expect(host.reframe).toMatchObject({ trackId: tr.id, third: "center" });
  expect(host.fit).toBe("fill");
  const text = p.tracks.flatMap((t: { clips: unknown[] }) => t.clips).find((c: { text?: unknown }) => c.text) as { follow: Record<string, unknown> };
  expect(text.follow).toMatchObject({ trackId: tr.id, clipId: "v", rotation: true });
});
