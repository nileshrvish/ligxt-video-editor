import { CLIENT_ID } from "./session";
import type { Job, MediaMeta, Project, ProjectSummary, TemplateSummary } from "./types";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function parseError(res: Response): Promise<ApiError> {
  let msg = `${res.status} ${res.statusText}`;
  try {
    const body = await res.json();
    if (body?.detail) msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    /* not JSON */
  }
  return new ApiError(msg, res.status);
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: { ...(body !== undefined ? { "Content-Type": "application/json" } : {}), "X-Ligxt-Client": CLIENT_ID },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError("Cannot reach the Ligxt backend. Is start.ps1 still running?", 0);
  }
  if (!res.ok) throw await parseError(res);
  return res.json() as Promise<T>;
}

export const api = {
  health: () => req<{ ok: boolean }>("GET", "/api/health"),
  system: () => req<{ ffmpeg: string; nvenc: boolean; root: string; exportsDir: string }>("GET", "/api/system"),

  listProjects: () => req<{ projects: ProjectSummary[] }>("GET", "/api/projects"),
  createProject: (name: string, aspect: string) => req<Project>("POST", "/api/projects", { name, aspect }),
  getProject: (id: string) => req<Project>("GET", `/api/projects/${id}`),
  saveProject: (p: Project) => req<{ ok: boolean; updated: number }>("PUT", `/api/projects/${p.id}`, p),
  autosave: (p: Project) => req<{ ok: boolean; updated: number }>("POST", `/api/projects/${p.id}/autosave`, p),
  listAutosaves: (id: string) => req<{ autosaves: { name: string; time: number }[] }>("GET", `/api/projects/${id}/autosaves`),
  restoreAutosave: (id: string, name: string) => req<Project>("POST", `/api/projects/${id}/autosaves/${name}/restore`),
  duplicateProject: (id: string) => req<Project>("POST", `/api/projects/${id}/duplicate`),
  deleteProject: (id: string) => req<{ ok: boolean }>("DELETE", `/api/projects/${id}`),

  getMedia: (ids: string[]) =>
    req<{ media: MediaMeta[]; missing: string[] }>("GET", `/api/media?ids=${ids.join(",")}`),
  getMediaOne: (id: string) => req<MediaMeta>("GET", `/api/media/${id}`),
  reprocessMedia: (id: string) => req<{ jobId: string }>("POST", `/api/media/${id}/reprocess`),
  detectBeats: (id: string) => req<{ jobId: string }>("POST", `/api/media/${id}/beats`),
  textStyles: () => req<{ styles: { id: string; name: string; style: Record<string, unknown> }[] }>("GET", "/api/text-styles"),
  saveTextStyle: (name: string, style: Record<string, unknown>) => req<{ id: string; name: string }>("POST", "/api/text-styles", { name, style }),
  deleteTextStyle: (id: string) => req<{ ok: boolean }>("DELETE", `/api/text-styles/${id}`),
  lyrics: (p: Project, body: { clipId: string | null; playhead: number; mode: "auto" | "align" | "lrc"; model: string; language: string | null; text?: string;
    lrclibId?: number | null; lrc?: string }) =>
    req<{ jobId: string }>("POST", `/api/projects/${p.id}/lyrics`, { project: p, ...body }),
  lyricsTags: (mediaId: string) => req<{ artist: string; title: string; album: string; duration: number | null }>("GET", `/api/lyrics/tags/${mediaId}`),
  lyricsSearch: (q: { title: string; artist?: string; album?: string }) =>
    req<{ results: { id: number; artist: string; title: string; album: string; duration: number; synced: boolean; instrumental: boolean }[] }>(
      "GET", `/api/lyrics/search?${new URLSearchParams({ title: q.title, artist: q.artist ?? "", album: q.album ?? "" })}`),
  lyricsTranslate: (body: { lines: string[]; source: string | null; target: string; model: "good" | "best"; script?: "native" | "roman" }) =>
    req<{ jobId: string }>("POST", "/api/lyrics/translate", body),
  lyricsRomanize: (lines: string[], diacritics = false) => req<{ lines: string[] }>("POST", "/api/lyrics/romanize", { lines, diacritics }),
  lyricsLanguages: () => req<{ languages: Record<string, string>; models: Record<"good" | "best", { id: string; name: string; installed: boolean; downloadBytes: number }> }>(
    "GET", "/api/lyrics/languages"),
  clearStemsCache: () => req<{ freed: number }>("DELETE", "/api/ai/stems-cache"),
  captions: (p: Project, model: string, language: string | null) =>
    req<{ jobId: string }>("POST", `/api/projects/${p.id}/captions`, { project: p, model, language }),
  estimateSize: (p: Project) => req<{ jobId: string }>("POST", `/api/projects/${p.id}/export/estimate`, { project: p, mode: "final" }),
  exportCover: (p: Project, time: number, text: Record<string, unknown> | null, hideText: boolean) =>
    req<{ jobId: string }>("POST", `/api/projects/${p.id}/cover`, { project: p, time, text, hideText }),
  detectFaces: (id: string) => req<{ jobId: string }>("POST", `/api/media/${id}/faces`),
  templates: (q = "", category = "") =>
    req<{ templates: TemplateSummary[]; categories: string[] }>("GET", `/api/templates?q=${encodeURIComponent(q)}&category=${encodeURIComponent(category)}`),
  template: (id: string) => req<TemplateSummary & { template: Record<string, any> }>("GET", `/api/templates/${id}`),
  saveTemplate: (body: { project: Project; name: string; category: string; tags: string[]; description: string; exportPreset: string | null; previewStart: number | null }) =>
    req<{ template: TemplateSummary; notes: string[]; previewJobId: string }>("POST", "/api/templates", body),
  patchTemplate: (id: string, body: { name?: string; category?: string; tags?: string[]; description?: string }) => req<TemplateSummary>("PATCH", `/api/templates/${id}`, body),
  duplicateTemplate: (id: string) => req<TemplateSummary>("POST", `/api/templates/${id}/duplicate`),
  deleteTemplate: (id: string) => req<{ ok: boolean }>("DELETE", `/api/templates/${id}`),
  templatePreview: (id: string, body: { source: "placeholder" | "project"; projectId?: string; start?: number }) =>
    req<{ jobId: string }>("POST", `/api/templates/${id}/preview`, body),
  importTemplate: async (file: File) => {
    const r = await fetch("/api/templates/import", { method: "POST", headers: { "Content-Type": "application/zip" }, body: file });
    if (!r.ok) throw await parseError(r);
    return (await r.json()) as { template: TemplateSummary; previewJobId: string | null };
  },
  templateAccent: (id: string, media: string[]) => req<any>("POST", `/api/templates/${id}/accent`, { media }),
  generateFromTemplate: (id: string, body: { media: string[]; music: string | null; musicStart: number; texts: Record<string, string>; name: string; accent?: "auto" | "keep" | number }) =>
    req<{ jobId: string }>("POST", `/api/templates/${id}/generate`, body),
  sfxLibrary: () => req<{ sfx: { id: string; name: string; peak: number }[] }>("GET", "/api/media/sfx/library"),
  importSfx: (id: string) => req<{ media: MediaMeta; jobId: string }>("POST", `/api/media/sfx/${id}/import`),

  exportProject: (p: Project, mode: string, rangeStart?: number, rangeEnd?: number) =>
    req<{ jobId: string }>("POST", `/api/projects/${p.id}/export`, { project: p, mode, rangeStart, rangeEnd }),
  listLuts: () => req<{ luts: { id: string; name: string; kind: "builtin" | "user" }[] }>("GET", "/api/luts"),
  importLut: async (file: File) => {
    let res: Response;
    try {
      res = await fetch(`/api/luts/import?filename=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
    } catch {
      throw new ApiError("Cannot reach the Ligxt backend. Is start.ps1 still running?", 0);
    }
    if (!res.ok) throw await parseError(res);
    return (await res.json()) as { id: string; name: string };
  },
  listPresets: () => req<{ presets: { id: string; name: string; filters: import("./types").Filter[] }[] }>("GET", "/api/presets"),
  savePreset: (name: string, filters: import("./types").Filter[]) => req<{ id: string; name: string }>("POST", "/api/presets", { name, filters }),
  deletePreset: (id: string) => req<{ ok: boolean }>("DELETE", `/api/presets/${id}`),
  cancelJob: (id: string) => req<{ ok: boolean }>("POST", `/api/jobs/${id}/cancel`),
  aiStatus: (refresh = false) => req<AiStatus>("GET", `/api/ai/status${refresh ? "?refresh=true" : ""}`),
  aiSettings: (body: Partial<AiStatus["settings"]>) => req<AiStatus["settings"]>("PUT", "/api/ai/settings", body),
  downloadModel: (id: string) => req<{ jobId: string }>("POST", `/api/ai/models/${encodeURIComponent(id)}/download`),
  deleteModel: (id: string) => req<{ ok: boolean }>("DELETE", `/api/ai/models/${encodeURIComponent(id)}`),
  clearCutoutCache: () => req<{ freed: number }>("DELETE", "/api/ai/cutout-cache"),
  cutoutPick: (mediaId: string, t: number, points: { x: number; y: number; label: number }[]) =>
    req<{ jobId: string }>("POST", "/api/cutout/pick", { mediaId, t, points }),
  openFolder: (path?: string) => req<{ ok: boolean }>("POST", "/api/system/open-folder", { path: path ?? null }),
};

/** Upload one file as a raw body so we get progress events and streaming to disk. */
export function uploadMedia(
  file: File,
  onProgress: (fraction: number) => void,
): { promise: Promise<{ media: MediaMeta; jobId: string }>; abort: () => void } {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<{ media: MediaMeta; jobId: string }>((resolve, reject) => {
    xhr.open("POST", `/api/media/import?filename=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let body: any = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* ignore */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new ApiError(body?.detail ?? `Upload failed (${xhr.status})`, xhr.status));
    };
    xhr.onerror = () => reject(new ApiError(`Upload of '${file.name}' failed: backend not reachable.`, 0));
    xhr.onabort = () => reject(new ApiError(`Upload of '${file.name}' was cancelled.`, 0));
    xhr.send(file);
  });
  return { promise, abort: () => xhr.abort() };
}

export type { Job };

export interface AiModel {
  id: string; name: string; task: string; license: string; source: string; about: string;
  downloadBytes?: number; installed?: boolean; diskBytes: number; jobId?: string | null; deletable?: boolean;
}
export interface AiStatus {
  settings: { device: "auto" | "cpu"; budgetGB: number; personModel: string; separator: "mel-roformer" | "off"; dereverb: boolean };
  env: { installed: boolean; error?: string; python?: string; torch?: string | null; cuda?: boolean; device?: string;
    vramTotal?: number; vramFree?: number; cudaVersion?: string };
  models: AiModel[];
  other: AiModel[];
  totalBytes: number;
  cacheBytes: number;
  stemsBytes: number;
}
