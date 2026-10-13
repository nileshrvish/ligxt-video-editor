import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { toastError, useApp } from "../store/app";
import { useEditor } from "../store/editor";
import { fmtBytes, fmtEta, fmtTime, projectDuration } from "../util";
import { AspectShape, Modal, ProgressBar } from "./Common";
import presetsJson from "../../../shared/export_presets.json";
import { TEXT_PRESETS } from "../text/anim";
import { Icon } from "./Icons";

export interface ExportPreset {
  id: string; label: string; sub: string; w: number; h: number; note: string; audioKbps: number; reduceGrain: boolean;
  video: { kind: "crf" | "bitrate"; crf?: number; kbps?: number; maxKbps?: number; profile?: string; level?: string; preset?: string; gop?: number };
}
export const EXPORT_PRESETS = presetsJson.presets as ExportPreset[];

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
  const est = useEstimate(project, duration, useRange ? range : null);
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
                      e.video = null;
                      e.audioKbps = null;
                      e.reduceGrain = null;
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
                <Icon name="alert" size={12} /> The project canvas is {projAspect} but this export is {outAspect}: the video is letterboxed (bars in the background colour). To reframe instead, change the project's aspect ratio (Properties, with nothing selected).
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
              <span className="faint" style={{ fontSize: 11, maxWidth: 300 }}>
                {ex.mode === "final"
                  ? preset.note
                  : nvenc === false
                    ? "GPU encoder not available: uses fast CPU encoding."
                    : "GPU (NVENC) encoding: several times faster, for quick checks."}
              </span>
              {ex.mode === "final" && preset.id === "custom" && <CustomQuality />}
              <label className="row fl-check" title="Instagram and TikTok turn heavy film grain into blocky, boiling noise. This exports grain at 35 % (the look is kept, the noise is not).">
                <input type="checkbox" checked={ex.reduceGrain ?? preset.reduceGrain}
                  onChange={(e) => set((x) => void (x.reduceGrain = e.target.checked))} />
                Reduce grain for upload
              </label>
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
            <label>Estimated file size</label>
            <div className="row" style={{ fontSize: 13 }}>
              {ex.mode === "draft" ? <span className="faint">Draft exports are quick previews; size not estimated.</span>
                : est.bytes != null ? <><b>{fmtBytes(est.bytes)}</b><span className="faint">{est.method === "bitrate" ? "(from the bitrate)" : "(from three 1-second test renders)"}</span></>
                  : est.busy ? <><Icon name="loader" size={13} className="spin" /><span className="faint">Estimating (renders three 1-second samples)…</span></>
                    : <span className="faint">{est.error ?? "…"}</span>}
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
          <CoverPanel playhead={playhead} />
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

function CustomQuality() {
  const ex = useEditor((s) => s.project!.export);
  const set = (fn: (e: typeof ex) => void) => useEditor.getState().commit((p) => fn(p.export), false);
  const v = ex.video ?? { kind: "crf" as const, crf: 18 };
  return (
    <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
      <div className="seg">
        <button className={v.kind === "crf" ? "on" : ""} onClick={() => set((e) => void (e.video = { kind: "crf", crf: 18, profile: "high", preset: "slow" }))}>Constant quality</button>
        <button className={v.kind === "bitrate" ? "on" : ""} onClick={() => set((e) => void (e.video = { kind: "bitrate", kbps: 12000, maxKbps: 16000, profile: "high", preset: "slow" }))}>Bitrate</button>
      </div>
      {v.kind === "crf" ? (
        <label className="row" style={{ fontSize: 12, gap: 4 }}>CRF
          <input className="input" type="number" min={12} max={30} style={{ width: 60 }} value={v.crf ?? 18}
            onChange={(e) => set((x) => void (x.video = { ...v, crf: Math.max(12, Math.min(30, +e.target.value)) }))} />
          <span className="faint">(lower = better, bigger)</span></label>
      ) : (
        <label className="row" style={{ fontSize: 12, gap: 4 }}>
          <input className="input" type="number" min={1000} max={80000} step={500} style={{ width: 80 }} value={v.kbps ?? 12000}
            onChange={(e) => set((x) => void (x.video = { ...v, kbps: +e.target.value, maxKbps: Math.round(+e.target.value * 4 / 3) }))} /> kbps</label>
      )}
    </div>
  );
}

/** Expected size: instant for bitrate presets, else three short sample renders (debounced). */
function useEstimate(project: ReturnType<typeof useEditor.getState>["project"] & object, duration: number, range: { start: number; end: number } | null) {
  const ex = project.export;
  const preset = EXPORT_PRESETS.find((p) => p.id === ex.preset) ?? EXPORT_PRESETS[EXPORT_PRESETS.length - 1];
  const v = { ...preset.video, ...(ex.video ?? {}) };
  const dur = range ? Math.max(0, range.end - range.start) : duration;
  const [state, setState] = useState<{ bytes: number | null; method?: string; busy: boolean; error?: string }>({ bytes: null, busy: false });
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useApp((s) => (jobId ? s.jobs[jobId] : undefined));
  const timer = useRef<number>();
  const key = JSON.stringify([ex.preset, ex.video, ex.reduceGrain, ex.mode, range, project.tracks, project.settings]);
  useEffect(() => {
    if (ex.mode !== "final") return;
    if (v.kind === "bitrate") {
      setState({ bytes: ((v.kbps ?? 12000) + (ex.audioKbps ?? preset.audioKbps)) * 1000 / 8 * dur * 1.01, method: "bitrate", busy: false });
      return;
    }
    setState({ bytes: null, busy: true });
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      const p = { ...useEditor.getState().project!, export: { ...useEditor.getState().project!.export, range } };
      api.estimateSize(p).then((r) => setJobId(r.jobId), () => setState({ bytes: null, busy: false, error: "Could not estimate." }));
    }, 900);
    return () => window.clearTimeout(timer.current);
  }, [key]);
  useEffect(() => {
    if (!job) return;
    if (job.status === "done") setState({ bytes: job.result.bytes, method: job.result.method, busy: false });
    else if (job.status === "error") setState({ bytes: null, busy: false, error: "Could not estimate (see Jobs)." });
  }, [job?.status]);
  return state;
}

/** Cover / thumbnail: a frame (the playhead) with optional text, saved as a JPG in exports. */
function CoverPanel({ playhead }: { playhead: number }) {
  const project = useEditor((s) => s.project!);
  const texts = project.tracks.filter((t) => t.type === "text").flatMap((t) => t.clips.filter((c) => c.text));
  const [mode, setMode] = useState<string>(texts.length ? `clip:${texts[0].id}` : "none");
  const [custom, setCustom] = useState("");
  const [presetId, setPresetId] = useState("hook");
  const [hideText, setHideText] = useState(true);
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useApp((s) => (jobId ? s.jobs[jobId] : undefined));
  const busy = job && (job.status === "queued" || job.status === "running");
  const go = () => {
    let text: Record<string, unknown> | null = null;
    if (mode.startsWith("clip:")) {
      const c = texts.find((x) => `clip:${x.id}` === mode);
      if (c) text = { text: c.text, transform: { x: c.transform.x, y: c.transform.y, scale: c.transform.scale, rotation: c.transform.rotation } };
    } else if (mode === "custom" && custom.trim()) {
      const pr = TEXT_PRESETS.find((p) => p.id === presetId)!;
      text = { text: { ...pr.style, content: custom.trim() }, transform: pr.position };
    }
    api.exportCover(useEditor.getState().project!, playhead, text, hideText).then((r) => setJobId(r.jobId), (e) => toastError("Cover export failed", e));
  };
  return (
    <div className="field cover-panel">
      <label>Cover image (JPG)</label>
      <div className="row" style={{ gap: 6, flexWrap: "wrap", fontSize: 12 }}>
        <span>Frame at the playhead ({fmtTime(playhead)})</span>
        <select className="input" style={{ height: 26 }} value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="none">No text</option>
          {texts.map((c) => <option key={c.id} value={`clip:${c.id}`}>Text: {String((c.text as { content?: string }).content ?? "").slice(0, 30)}</option>)}
          <option value="custom">Custom text…</option>
        </select>
        {mode === "custom" && (
          <>
            <input className="input" style={{ width: 180 }} placeholder="Cover title" value={custom} onChange={(e) => setCustom(e.target.value)} />
            <select className="input" style={{ height: 26 }} value={presetId} onChange={(e) => setPresetId(e.target.value)}>
              {TEXT_PRESETS.filter((p) => p.id !== "caption").map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select>
          </>
        )}
        <label className="row fl-check"><input type="checkbox" checked={hideText} onChange={(e) => setHideText(e.target.checked)} />Hide the timeline's text in the frame</label>
        <button className="btn sm" onClick={go} disabled={!!busy}>{busy ? "Saving…" : "Save cover"}</button>
        {job?.status === "done" && (
          <a className="btn sm ghost" href={job.result.url} target="_blank" rel="noreferrer"><Icon name="image" size={12} /> {job.result.file}</a>
        )}
        {job?.status === "error" && <span className="error-text">{job.error}</span>}
      </div>
    </div>
  );
}
