import { useEffect, useState } from "react";
import { api } from "../api";
import { toastError, useApp } from "../store/app";
import { useEditor } from "../store/editor";
import { fmtBytes, fmtEta, fmtTime, projectDuration } from "../util";
import { AspectShape, Modal, ProgressBar } from "./Common";
import { Icon } from "./Icons";

export const EXPORT_PRESETS = [
  { id: "reels", label: "Instagram Reels", sub: "Shorts / TikTok", w: 1080, h: 1920 },
  { id: "square", label: "Square", sub: "Feed post", w: 1080, h: 1080 },
  { id: "portrait", label: "Portrait 4:5", sub: "Feed post", w: 1080, h: 1350 },
  { id: "youtube", label: "YouTube", sub: "16:9 HD", w: 1920, h: 1080 },
  { id: "custom", label: "Custom", sub: "Any size", w: 0, h: 0 },
];

export const OPEN_FOLDER_KEY = "ligxt.openFolderAfterExport";
export const openFolderAfterExport = () => localStorage.getItem(OPEN_FOLDER_KEY) !== "0";

function aspectOf(w: number, h: number) {
  const g = (a: number, b: number): number => (b ? g(b, a % b) : a);
  const d = g(w, h) || 1;
  return `${w / d}:${h / d}`;
}

export default function ExportDialog({ onClose }: { onClose: () => void }) {
  const project = useEditor((s) => s.project!);
  const playhead = useEditor((s) => s.playhead);
  const ex = project.export;
  const duration = projectDuration(project);
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useApp((s) => (jobId ? s.jobs[jobId] : undefined));
  const [nvenc, setNvenc] = useState<boolean | null>(null);
  const [useRange, setUseRange] = useState(!!ex.range);
  const [range, setRange] = useState(ex.range ?? { start: 0, end: duration });
  const [openAfter, setOpenAfter] = useState(openFolderAfterExport());
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    api.system().then((s) => setNvenc(s.nvenc), () => setNvenc(null));
  }, []);

  const set = (fn: (e: typeof ex) => void) => useEditor.getState().commit((p) => fn(p.export), false);
  const preset = EXPORT_PRESETS.find((p) => p.id === ex.preset) ?? EXPORT_PRESETS[EXPORT_PRESETS.length - 1];
  const projAspect = aspectOf(project.settings.width, project.settings.height);
  const outAspect = ex.width && ex.height ? aspectOf(ex.width, ex.height) : projAspect;

  const start = async () => {
    if (duration <= 0) {
      useApp.getState().toast({ kind: "warning", title: "Nothing to export", detail: "Add clips to the main video track first." });
      return;
    }
    if (ex.width < 16 || ex.height < 16 || ex.width > 7680 || ex.height > 7680) {
      useApp.getState().toast({ kind: "warning", title: "Invalid size", detail: "Width and height must be between 16 and 7680." });
      return;
    }
    const r = useRange ? { start: Math.max(0, range.start), end: Math.min(duration, range.end) } : null;
    if (r && r.end - r.start < 0.1) {
      useApp.getState().toast({ kind: "warning", title: "Range too short", detail: "The end must be after the start." });
      return;
    }
    localStorage.setItem(OPEN_FOLDER_KEY, openAfter ? "1" : "0");
    set((e) => void (e.range = r));
    setStarting(true);
    try {
      const p = useEditor.getState().project!;
      const res = await api.exportProject(p, p.export.mode, r?.start, r?.end);
      setJobId(res.jobId);
      useEditor.getState().save("manual");
    } catch (e) {
      toastError("Export could not start", e);
    } finally {
      setStarting(false);
    }
  };

  const running = job && (job.status === "running" || job.status === "queued");
  const elapsed = job?.started ? ((job.finished ?? Date.now() / 1000) - job.started) : 0;

  return (
    <Modal
      title="Export video"
      onClose={onClose}
      wide
      footer={
        job?.status === "done" ? (
          <>
            <button className="btn" onClick={() => setJobId(null)}>Export again</button>
            <button className="btn primary" onClick={onClose}>Done</button>
          </>
        ) : running ? (
          <>
            <span className="faint" style={{ marginRight: "auto", fontSize: 12 }}>You can close this window; the export keeps running.</span>
            <button className="btn danger" onClick={() => api.cancelJob(job.id).catch((e) => toastError("Cancel failed", e))}>Cancel export</button>
            <button className="btn" onClick={onClose}>Hide</button>
          </>
        ) : (
          <>
            <button className="btn" onClick={onClose}>Close</button>
            <button className="btn primary" onClick={start} disabled={starting || duration <= 0}>
              <Icon name="export" size={14} /> {job ? "Try again" : "Export"}
            </button>
          </>
        )
      }
    >
      {job ? (
        <JobView job={job} elapsed={elapsed} />
      ) : (
        <>
          <div className="field">
            <label>Preset</label>
            <div className="choice-grid">
              {EXPORT_PRESETS.map((p) => (
                <button
                  key={p.id}
                  className={`choice ${preset.id === p.id ? "on" : ""}`}
                  onClick={() =>
                    set((e) => {
                      e.preset = p.id;
                      if (p.w) {
                        e.width = p.w;
                        e.height = p.h;
                      }
                    })
                  }
                >
                  <div style={{ height: 28, display: "grid", placeItems: "center" }}>
                    {p.w ? <AspectShape aspect={aspectOf(p.w, p.h)} size={24} /> : <Icon name="layout" size={20} />}
                  </div>
                  <span>{p.label}</span>
                  <small>{p.w ? `${p.w}×${p.h}` : p.sub}</small>
                </button>
              ))}
            </div>
            {preset.id === "custom" && (
              <div className="row">
                <input className="input" type="number" style={{ width: 90 }} value={ex.width} onChange={(e) => set((x) => void (x.width = Math.round(+e.target.value)))} />
                <span className="faint">×</span>
                <input className="input" type="number" style={{ width: 90 }} value={ex.height} onChange={(e) => set((x) => void (x.height = Math.round(+e.target.value)))} />
                <span className="faint">px</span>
              </div>
            )}
            {outAspect !== projAspect && (
              <div style={{ color: "var(--warn)", fontSize: 12 }}>
                <Icon name="alert" size={12} /> The project canvas is {projAspect} but this export is {outAspect}. Clips will be re-fitted using each clip's fit mode.
              </div>
            )}
          </div>

          <div className="row" style={{ gap: 20, alignItems: "flex-start", flexWrap: "wrap" }}>
            <div className="field">
              <label>Quality</label>
              <div className="seg">
                <button className={ex.mode === "final" ? "on" : ""} onClick={() => set((e) => void (e.mode = "final"))}>Final</button>
                <button className={ex.mode === "draft" ? "on" : ""} onClick={() => set((e) => void (e.mode = "draft"))}>Draft (fast)</button>
              </div>
              <span className="faint" style={{ fontSize: 11, maxWidth: 260 }}>
                {ex.mode === "final"
                  ? "H.264 (x264) CRF 18, preset slow, AAC 192k. Best quality for upload."
                  : nvenc === false
                    ? "GPU encoder not available: uses fast CPU encoding."
                    : "GPU (NVENC) encoding: several times faster, for quick checks."}
              </span>
            </div>
            <div className="field">
              <label>Frame rate</label>
              <div className="seg">
                {([24, 25, 30, 60, "source"] as const).map((f) => (
                  <button key={f} className={ex.fps === f ? "on" : ""} onClick={() => set((e) => void (e.fps = f))}>
                    {f === "source" ? "Source" : f}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="field">
            <label>Range</label>
            <div className="row" style={{ flexWrap: "wrap" }}>
              <div className="seg">
                <button className={!useRange ? "on" : ""} onClick={() => setUseRange(false)}>Whole project ({fmtTime(duration)})</button>
                <button className={useRange ? "on" : ""} onClick={() => setUseRange(true)}>Selected range</button>
              </div>
              {useRange && (
                <>
                  <input className="input" type="number" step={0.1} min={0} style={{ width: 80 }} value={+range.start.toFixed(2)} onChange={(e) => setRange({ ...range, start: +e.target.value })} />
                  <button className="btn sm ghost" onClick={() => setRange({ ...range, start: playhead })} title="Use playhead">⟵ playhead</button>
                  <span className="faint">to</span>
                  <input className="input" type="number" step={0.1} min={0} style={{ width: 80 }} value={+range.end.toFixed(2)} onChange={(e) => setRange({ ...range, end: +e.target.value })} />
                  <button className="btn sm ghost" onClick={() => setRange({ ...range, end: playhead })} title="Use playhead">⟵ playhead</button>
                </>
              )}
            </div>
          </div>

          <div className="field">
            <label>File name</label>
            <div className="row">
              <input className="input" style={{ flex: 1 }} placeholder={project.name} value={ex.fileName} onChange={(e) => set((x) => void (x.fileName = e.target.value))} />
              <span className="faint" style={{ fontSize: 12 }}>_date.mp4 → exports\</span>
            </div>
            <label className="row" style={{ cursor: "pointer", fontSize: 12, textTransform: "none", letterSpacing: 0, fontWeight: 400, color: "var(--text-2)" }}>
              <input type="checkbox" checked={openAfter} onChange={(e) => setOpenAfter(e.target.checked)} /> Open the exports folder when done
            </label>
          </div>
        </>
      )}
    </Modal>
  );
}

function JobView({ job, elapsed }: { job: NonNullable<ReturnType<typeof useApp.getState>["jobs"][string]>; elapsed: number }) {
  if (job.status === "queued" || job.status === "running") {
    return (
      <div className="field" style={{ gap: 10 }}>
        <div className="row">
          <Icon name="loader" className="spin" />
          <b>{job.status === "queued" ? "Waiting for the previous export to finish…" : job.message || "Rendering"}</b>
          <span className="spacer" />
          <span className="mono">{Math.round(job.progress * 100)}%</span>
        </div>
        <ProgressBar value={job.progress} big />
        <div className="row faint" style={{ fontSize: 12 }}>
          <span>Elapsed {fmtTime(elapsed)}</span>
          <span className="spacer" />
          <span>{job.status === "running" ? fmtEta(job.eta) : ""}</span>
        </div>
      </div>
    );
  }
  if (job.status === "done") {
    const r = job.result;
    return (
      <div className="field" style={{ gap: 10 }}>
        <div className={`result-card ${r.sync?.ok ? "" : "warn"}`}>
          <Icon name={r.sync?.ok ? "check" : "alert"} size={22} style={{ color: r.sync?.ok ? "var(--ok)" : "var(--warn)" }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <b style={{ wordBreak: "break-all" }}>{r.file}</b>
            <div className="muted" style={{ fontSize: 12 }}>
              {fmtTime(r.duration)} · {fmtBytes(r.size)} · rendered in {fmtTime(elapsed)}
            </div>
            <div style={{ fontSize: 12, color: r.sync?.ok ? "var(--text-2)" : "var(--warn)" }}>Sync check: {r.sync?.message}</div>
          </div>
        </div>
        <div className="row">
          <button className="btn" onClick={() => api.openFolder(r.path).catch((e) => toastError("Could not open folder", e))}>
            <Icon name="folder" size={14} /> Show in folder
          </button>
          <a className="btn" href={r.url} target="_blank" rel="noreferrer">
            <Icon name="play" size={14} /> Play
          </a>
        </div>
      </div>
    );
  }
  return (
    <div className="field" style={{ gap: 10 }}>
      <div className="row" style={{ color: job.status === "cancelled" ? "var(--text-2)" : "var(--err)" }}>
        <Icon name="alert" /> <b>{job.status === "cancelled" ? "Export cancelled" : "Export failed"}</b>
      </div>
      {job.error && <div style={{ wordBreak: "break-word" }}>{job.error}</div>}
      {job.log && (
        <details>
          <summary className="faint" style={{ cursor: "pointer" }}>Technical details (ffmpeg log)</summary>
          <pre className="log">{job.log}</pre>
        </details>
      )}
    </div>
  );
}
