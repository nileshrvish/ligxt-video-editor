// Headless UI flow of Home > Auto-edit: footage from a folder or a project, song + style, analysis, review (lock,
// swap, regenerate, style switch), create + report, open the project.
import { expect, test, type APIRequestContext } from "@playwright/test";
import { copyFileSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const FIX = fileURLToPath(new URL(".fixtures", import.meta.url));

async function waitJob(request: APIRequestContext, id: string, timeout = 120_000) {
  const t0 = Date.now();
  for (;;) {
    const j = await (await request.get(`/api/jobs/${id}`)).json();
    if (["done", "error", "cancelled"].includes(j.status)) return j;
    if (Date.now() - t0 > timeout) throw new Error(`job ${id} timed out`);
    await new Promise((r) => setTimeout(r, 250));
  }
}

async function importFile(request: APIRequestContext, file: string) {
  const r = await request.post(`/api/media/import?filename=${file}`, { data: readFileSync(`${FIX}/${file}`), headers: { "Content-Type": "application/octet-stream" } });
  const b = await r.json();
  expect((await waitJob(request, b.jobId)).status).toBe("done");
  return b.media.id as string;
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("ligxt.tour.done", "1"));
});

test("auto-edit: a folder of clips is imported", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("auto-edit").click();
  const dir = mkdtempSync(join(tmpdir(), "ligxt-folder-"));
  for (const f of ["object.mp4", "pattern.mp4", "song.wav"]) copyFileSync(join(FIX, f), join(dir, f));
  await page.getByTestId("ae-folder").setInputFiles(dir);
  await expect(page.locator(".ae-clip")).toHaveCount(2, { timeout: 60_000 });
  await expect(page.getByText(/2 clip\(s\) · song: song.wav/)).toBeVisible();
});

test("auto-edit: analyse, review (lock, swap, regenerate, style), create and open the project", async ({ page, request }) => {
  const a = await importFile(request, "object.mp4");
  const b = await importFile(request, "pattern.mp4");
  const s = await importFile(request, "song.wav");
  expect((await waitJob(request, (await (await request.post(`/api/media/${s}/beats`)).json()).jobId)).status).toBe("done");
  const p = await (await request.post("/api/projects", { data: { name: "Trip", aspect: "9:16" } })).json();
  p.media = [a, b, s];
  expect((await request.put(`/api/projects/${p.id}`, { data: p })).ok()).toBeTruthy();

  await page.goto("/");
  await page.getByTestId("auto-edit").click();
  await page.getByTestId("ae-project").selectOption({ label: "Trip" });
  await expect(page.getByText(/2 clip\(s\) · song: song.wav/)).toBeVisible();
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByTestId("ae-style-cinematic").click();
  await page.getByPlaceholder("optional, e.g. TRAVEL DUMP").fill("trip test");
  await page.getByRole("button", { name: /Analyse footage/ }).click();
  const review = page.getByTestId("ae-review");
  await expect(review).toBeVisible({ timeout: 90_000 });
  const slots = review.locator(".ae-slot");
  expect(await slots.count()).toBeGreaterThanOrEqual(4);
  await expect(page.locator(".seg button.on")).toHaveText("Cinematic");
  // lock the first shot, swap the second for an alternative
  const firstName = await slots.nth(0).locator(".ae-info b").textContent();
  await slots.nth(0).getByRole("button", { name: "Lock" }).click();
  await expect(slots.nth(0).getByRole("button", { name: "Locked" })).toBeVisible();
  await slots.nth(1).getByRole("button", { name: "Swap" }).click();
  const alt = slots.nth(1).locator(".ae-alt").first();
  if (await alt.count()) {
    await alt.click();
    await expect(slots.nth(1)).toHaveClass(/locked/);
  }
  await page.getByRole("button", { name: /Regenerate/ }).click();
  await expect(slots.nth(0).getByRole("button", { name: "Locked" })).toBeVisible();
  await expect(slots.nth(0).locator(".ae-info b")).toHaveText(firstName!);
  // another style re-drafts
  await page.locator(".seg").getByRole("button", { name: "Energetic" }).click();
  await expect(page.locator(".seg button.on")).toHaveText("Energetic");
  await page.getByRole("button", { name: /Create project/ }).click();
  await expect(page.getByTestId("ae-report")).toContainText("Energetic auto-edit", { timeout: 120_000 });
  await page.getByRole("button", { name: "Open project" }).click();
  await expect(page).toHaveURL(/#\/p\//);
  const pid = page.url().split("/p/")[1];
  const proj = await (await request.get(`/api/projects/${pid}`)).json();
  expect(proj.tracks[0].clips.length).toBeGreaterThanOrEqual(4);
  const texts = proj.tracks.flatMap((t: { clips: { text?: { content: string } }[] }) => t.clips).filter((c: { text?: unknown }) => c.text);
  expect(texts.map((c: { text: { content: string } }) => c.text.content)).toContain("TRIP TEST");
});
