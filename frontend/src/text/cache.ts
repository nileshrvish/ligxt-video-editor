// Text layouts + images from the backend (POST /api/text/layout), cached by look. The preview draws
// these exact images, the export composes the same files.
import { lookOf, type TextLayout } from "./anim";

export interface TextAsset {
  status: "loading" | "ready" | "error";
  layout?: TextLayout;
  imgs?: { base: HTMLImageElement; hi?: HTMLImageElement; box?: HTMLImageElement };
  error?: string;
}

const byLook = new Map<string, TextAsset>();
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((fn) => fn());

export function onTextAssets(fn: () => void): () => void {
  listeners.add(fn);
  return () => void listeners.delete(fn);
}

function loadImg(url: string): Promise<HTMLImageElement> {
  return new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = () => rej(new Error(`could not load ${url}`));
    img.src = url;
  });
}

/** The asset for a spec (starts loading it on first use). */
export function textAsset(spec: unknown): TextAsset {
  const look = lookOf(spec);
  let a = byLook.get(look);
  if (a) return a;
  a = { status: "loading" };
  byLook.set(look, a);
  const entry = a;
  fetch("/api/text/layout", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ spec }) })
    .then(async (r) => {
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? `text layout ${r.status}`);
      return r.json() as Promise<TextLayout & { base: string }>;
    })
    .then(async (lay) => {
      const url = (f: string) => `${lay.base}${f}`;
      const [base, hi, box] = await Promise.all([
        loadImg(url(lay.images.base)),
        lay.images.hi ? loadImg(url(lay.images.hi)) : Promise.resolve(undefined),
        lay.images.box ? loadImg(url(lay.images.box)) : Promise.resolve(undefined),
      ]);
      entry.layout = lay;
      entry.imgs = { base, hi, box };
      entry.status = "ready";
    })
    .catch((e: Error) => {
      entry.status = "error";
      entry.error = e.message;
      // forget failures so a later edit (or a restarted backend) can try again
      setTimeout(() => byLook.get(look) === entry && byLook.delete(look), 3000);
    })
    .finally(notify);
  if (byLook.size > 400) byLook.delete(byLook.keys().next().value!); // keep the cache bounded
  return a;
}

/** Last ready asset per clip, so typing shows the previous version until the new one is in. */
const lastReady = new Map<string, TextAsset>();
export function textAssetFor(clipId: string, spec: unknown): TextAsset | null {
  const a = textAsset(spec);
  if (a.status === "ready") {
    lastReady.set(clipId, a);
    return a;
  }
  return lastReady.get(clipId) ?? null;
}
