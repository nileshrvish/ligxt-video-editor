import type { Job, MediaMeta, Project, ProjectSummary } from "./types";

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
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
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

  exportProject: (p: Project, mode: string, rangeStart?: number, rangeEnd?: number) =>
    req<{ jobId: string }>("POST", `/api/projects/${p.id}/export`, { project: p, mode, rangeStart, rangeEnd }),
  cancelJob: (id: string) => req<{ ok: boolean }>("POST", `/api/jobs/${id}/cancel`),
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
