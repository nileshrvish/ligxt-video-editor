// Headless UI flow: Captions > Song lyrics > Paste lyrics -> a "Lyrics" track with the user's lines.
// (The e2e server aligns with the worker's numpy test aligner: no speech model needed.)
import { expect, test, type APIRequestContext } from "@playwright/test";
import { readFileSync } from "node:fs";
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

test("lyrics: paste mode adds a Lyrics track with the pasted lines", async ({ page, request }) => {
  const imp = async (f: string) => {
    const b = await (await request.post(`/api/media/import?filename=${f}`, { data: readFileSync(`${FIX}/${f}`), headers: { "Content-Type": "application/octet-stream" } })).json();
    expect((await waitJob(request, b.jobId)).status).toBe("done");
    return b.media.id as string;
  };
  const vid = await imp("pattern.mp4");
  const song = await imp("song.wav");
  const p = await (await request.post("/api/projects", { data: { name: "Lyrics UI", aspect: "9:16" } })).json();
  p.media = [vid, song];
  p.tracks[0].clips = [{ id: "v", mediaId: vid, start: 0, inPoint: 0, outPoint: 5, speed: 1 }];
  const mu = p.tracks.find((t: { type: string }) => t.type === "music");
  mu.clips = [{ id: "song", mediaId: song, start: 0, inPoint: 2, outPoint: 7, speed: 1 }];
  expect((await request.put(`/api/projects/${p.id}`, { data: p })).ok()).toBeTruthy();

  await page.addInitScript(() => localStorage.setItem("ligxt.tour.done", "1"));
  await page.goto(`/#/p/${p.id}`);
  await page.getByTestId("captions-btn").click();
  await page.getByTestId("cap-source").getByRole("button", { name: /Song lyrics/ }).click();
  await expect(page.getByTestId("lyr-clip")).toHaveValue("song");
  await page.getByTestId("lyr-mode").getByRole("button", { name: "Paste lyrics" }).click();
  await page.getByTestId("lyr-text").fill("hello there friend\nsing it back");
  await page.getByRole("button", { name: /Align my lyrics/ }).click();
  await expect(page.getByTestId("lyr-report")).toContainText("6 words in 2 caption lines", { timeout: 60_000 });
  await page.getByRole("button", { name: "Close", exact: true }).last().click();
  await page.keyboard.press("Control+s");
  await page.waitForTimeout(600);
  const saved = await (await request.get(`/api/projects/${p.id}`)).json();
  const lyr = saved.tracks.find((t: { name: string }) => t.name === "Lyrics");
  expect(lyr).toBeTruthy();
  expect(lyr.clips.map((c: { text: { content: string } }) => c.text.content)).toEqual(["hello there friend", "sing it back"]);
  expect(lyr.clips[0].text.words.map((w: { text: string }) => w.text)).toEqual(["hello", "there", "friend"]);
});
