// This tab's identity, the project lock (one editing tab per project) and local drafts (so a crashed
// tab or server never loses more than about a second of work).
import type { Project } from "./types";

function makeId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Survives reloads of the same tab (sessionStorage), differs between tabs. */
export const CLIENT_ID: string = (() => {
  try {
    const k = "ligxt.client";
    const v = sessionStorage.getItem(k) || makeId();
    sessionStorage.setItem(k, v);
    return v;
  } catch {
    return makeId();
  }
})();

export const CLIENT_LABEL = `tab opened ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;

export type LockResult = { ok: true } | { ok: false; owner: string; since: number };

export async function takeLock(pid: string, force = false): Promise<LockResult> {
  const r = await fetch(`/api/projects/${pid}/lock`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client: CLIENT_ID, force, label: CLIENT_LABEL }),
  });
  if (r.ok) return { ok: true };
  if (r.status === 409) {
    try {
      const d = JSON.parse((await r.json()).detail);
      return { ok: false, owner: d.owner, since: d.since };
    } catch {
      return { ok: false, owner: "another tab", since: Date.now() / 1000 };
    }
  }
  throw new Error(`lock ${r.status}`);
}

export function releaseLock(pid: string) {
  const body = JSON.stringify({ client: CLIENT_ID });
  try {
    navigator.sendBeacon(`/api/projects/${pid}/unlock`, new Blob([body], { type: "application/json" }));
  } catch {
    /* best effort */
  }
}

// ---------------------------------------------------------------- local drafts
interface Draft { at: number; project: Project; synced: boolean }
const draftKey = (pid: string) => `ligxt.draft.${pid}`;

export function writeDraft(p: Project, synced: boolean) {
  try {
    localStorage.setItem(draftKey(p.id), JSON.stringify({ at: Date.now(), project: p, synced } satisfies Draft));
  } catch {
    /* storage full or disabled: drafts are a safety net only */
  }
}

export function markDraftSynced(pid: string) {
  try {
    const raw = localStorage.getItem(draftKey(pid));
    if (!raw) return;
    const d = JSON.parse(raw) as Draft;
    d.synced = true;
    localStorage.setItem(draftKey(pid), JSON.stringify(d));
  } catch {
    /* ignore */
  }
}

export function dropDraft(pid: string) {
  try {
    localStorage.removeItem(draftKey(pid));
  } catch {
    /* ignore */
  }
}

const stripVolatile = (p: Project) => JSON.stringify({ ...p, updated: 0 });

/** An unsaved local draft that is newer than (and different from) the saved project, if any. */
export function pendingDraft(saved: Project): Draft | null {
  try {
    const raw = localStorage.getItem(draftKey(saved.id));
    if (!raw) return null;
    const d = JSON.parse(raw) as Draft;
    if (d.synced || d.at <= saved.updated * 1000) return null;
    return stripVolatile(d.project) === stripVolatile(saved) ? null : d;
  } catch {
    return null;
  }
}

/** Projects with unsaved drafts (for the Home screen). */
export function draftProjectIds(): string[] {
  const out: string[] = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)!;
      if (!k.startsWith("ligxt.draft.")) continue;
      const d = JSON.parse(localStorage.getItem(k)!) as Draft;
      if (!d.synced) out.push(k.slice("ligxt.draft.".length));
    }
  } catch {
    /* ignore */
  }
  return out;
}
