import { create } from "zustand";
import type { Job } from "../types";

export interface Toast {
  id: number;
  kind: "info" | "success" | "error" | "warning";
  title: string;
  detail?: string;
  action?: { label: string; run: () => void };
  sticky?: boolean;
}

interface AppState {
  toasts: Toast[];
  jobs: Record<string, Job>;
  connected: boolean;
  toast: (t: Omit<Toast, "id">) => number;
  dismiss: (id: number) => void;
  setJobs: (jobs: Job[]) => void;
  upsertJob: (job: Job) => void;
  setConnected: (c: boolean) => void;
}

let toastId = 1;

export const useApp = create<AppState>((set, get) => ({
  toasts: [],
  jobs: {},
  connected: true,
  toast: (t) => {
    const id = toastId++;
    set({ toasts: [...get().toasts, { ...t, id }].slice(-6) });
    if (!t.sticky) {
      const ms = t.kind === "error" ? 12000 : 5000;
      setTimeout(() => get().dismiss(id), ms);
    }
    return id;
  },
  dismiss: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
  setJobs: (jobs) => set({ jobs: Object.fromEntries(jobs.map((j) => [j.id, j])) }),
  upsertJob: (job) => set({ jobs: { ...get().jobs, [job.id]: job } }),
  setConnected: (connected) => set({ connected }),
}));

export const toastError = (title: string, err: unknown) =>
  useApp.getState().toast({ kind: "error", title, detail: err instanceof Error ? err.message : String(err) });

type JobListener = (job: Job) => void;
const listeners = new Set<JobListener>();
export function onJob(fn: JobListener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Subscribe to the backend's server-sent job events (reconnects automatically). */
export function connectEvents(): () => void {
  let es: EventSource | null = null;
  let closed = false;
  let retry: number | undefined;

  const open = () => {
    es = new EventSource("/api/events");
    es.addEventListener("snapshot", (e) => {
      useApp.getState().setConnected(true);
      useApp.getState().setJobs(JSON.parse((e as MessageEvent).data).jobs);
    });
    es.addEventListener("job", (e) => {
      const job: Job = JSON.parse((e as MessageEvent).data);
      useApp.getState().upsertJob(job);
      listeners.forEach((l) => l(job));
    });
    es.onerror = () => {
      useApp.getState().setConnected(false);
      es?.close();
      if (!closed) retry = window.setTimeout(open, 2000);
    };
  };
  open();
  return () => {
    closed = true;
    clearTimeout(retry);
    es?.close();
  };
}
