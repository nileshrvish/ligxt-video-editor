// Photo extension images (GET /api/media/<id>/extended): the same file the export uses.
export interface ExtInfo { url: string; fx: number; fy: number; w: number; h: number }
interface Entry { status: "loading" | "ready" | "error"; info?: ExtInfo; img?: HTMLImageElement }

const cache = new Map<string, Entry>();
const listeners = new Set<() => void>();

export function onExtLoaded(fn: () => void): () => void {
  listeners.add(fn);
  return () => void listeners.delete(fn);
}

export function extension(mediaId: string): Entry {
  let e = cache.get(mediaId);
  if (e) return e;
  e = { status: "loading" };
  cache.set(mediaId, e);
  const entry = e;
  fetch(`/api/media/${mediaId}/extended`)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`extension ${r.status}`))))
    .then((info: ExtInfo) => new Promise<void>((res, rej) => {
      const img = new Image();
      img.onload = () => { entry.info = info; entry.img = img; entry.status = "ready"; res(); };
      img.onerror = () => rej(new Error("extension image"));
      img.src = info.url;
    }))
    .catch(() => {
      entry.status = "error";
      setTimeout(() => cache.get(mediaId) === entry && cache.delete(mediaId), 3000);
    })
    .finally(() => listeners.forEach((fn) => fn()));
  return e;
}
