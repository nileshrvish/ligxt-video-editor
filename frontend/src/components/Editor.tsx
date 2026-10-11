import { useEffect, useRef, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { navigate } from "../router";
import { onJob, useApp } from "../store/app";
import { useEditor } from "../store/editor";
import { projectDuration } from "../util";
import { Modal } from "./Common";
import ExportDialog, { openFolderAfterExport } from "./ExportDialog";
import { api } from "../api";
import { Icon, Logo } from "./Icons";
import MediaLibrary from "./MediaLibrary";
import Preview from "./Preview";
import Properties from "./Properties";
import Timeline from "./Timeline";

const AUTOSAVE_MS = 30_000;

export default function Editor({ projectId }: { projectId: string }) {
  const project = useEditor((s) => s.project);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [showExport, setShowExport] = useState(false);
  const [showHelp, setShowHelp] = useState(false);

  // Load project
  useEffect(() => {
    setLoadError(null);
    useEditor
      .getState()
      .load(projectId)
      .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)));
    return () => useEditor.getState().close();
  }, [projectId]);

  // Refresh media metadata as background jobs progress (thumbnail -> waveform -> proxy).
  useEffect(
    () =>
      onJob((job) => {
        if (job.kind === "export" && job.ref.projectId === useEditor.getState().project?.id) {
          const toast = useApp.getState().toast;
          if (job.status === "done") {
            const r = job.result;
            if (openFolderAfterExport()) api.openFolder(r.path).catch(() => void 0);
            toast({
              kind: r.sync?.ok ? "success" : "warning",
              title: `Export finished: ${r.file}`,
              detail: r.sync?.ok ? undefined : r.sync?.message,
              action: { label: "Show in folder", run: () => api.openFolder(r.path).catch(() => void 0) },
            });
          } else if (job.status === "error") {
            toast({ kind: "error", title: "Export failed", detail: job.error ?? undefined });
          }
          return;
        }
        const mid = job.ref.mediaId;
        if (!mid || !useEditor.getState().media[mid]) return;
        if (job.status !== "running" || job.message === "Waveform" || job.message === "Proxy") {
          useEditor.getState().refreshMedia(mid);
        }
        if (job.status === "error") {
          const name = useEditor.getState().media[mid]?.name ?? "media";
          useApp.getState().toast({ kind: "error", title: `Processing ${name} failed`, detail: job.error ?? undefined });
        }
      }),
    [],
  );

  // Autosave every 30 s when there are unsaved changes.
  useEffect(() => {
    const id = window.setInterval(() => {
      const s = useEditor.getState();
      if (s.project && (s.saveState === "dirty" || s.saveState === "error")) s.save("auto");
    }, AUTOSAVE_MS);
    return () => window.clearInterval(id);
  }, []);

  // Warn before closing the tab with unsaved changes.
  useEffect(() => {
    const on = (e: BeforeUnloadEvent) => {
      if (useEditor.getState().saveState !== "saved") {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", on);
    return () => window.removeEventListener("beforeunload", on);
  }, []);

  // Global keyboard shortcuts.
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
      if (document.querySelector(".modal-backdrop")) return;
      const s = useEditor.getState();
      const ctrl = e.ctrlKey || e.metaKey;
      const fps = s.project?.settings.fps ?? 30;
      const k = e.key;
      if (ctrl && k.toLowerCase() === "s") {
        e.preventDefault();
        s.save("manual");
      } else if (ctrl && k.toLowerCase() === "z" && !e.shiftKey) {
        e.preventDefault();
        s.undo();
      } else if (ctrl && (k.toLowerCase() === "y" || (k.toLowerCase() === "z" && e.shiftKey))) {
        e.preventDefault();
        s.redo();
      } else if (ctrl && k.toLowerCase() === "e") {
        e.preventDefault();
        setShowExport(true);
      } else if (k === " ") {
        e.preventDefault();
        s.setPlaying(!s.playing);
      } else if (k === "ArrowLeft" || k === "ArrowRight") {
        e.preventDefault();
        s.setPlaying(false);
        const step = (e.shiftKey ? 1 : 1 / fps) * (k === "ArrowLeft" ? -1 : 1);
        s.setPlayhead(Math.round((s.playhead + step) * fps) / fps);
      } else if (k === "Home") {
        s.setPlayhead(0);
      } else if (k === "End" && s.project) {
        s.setPlayhead(projectDuration(s.project));
      } else if (k === "Delete" || k === "Backspace") {
        s.deleteSelectedClip();
      } else if (k === "?" || (k === "/" && e.shiftKey)) {
        setShowHelp((v) => !v);
      } else if (k === "=" || k === "+") {
        s.setZoom(s.pxPerSec * 1.25);
      } else if (k === "-") {
        s.setZoom(s.pxPerSec / 1.25);
      } else if (k === "Escape") {
        s.select({ kind: "none" });
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  if (loadError) {
    return (
      <div className="home">
        <div className="home-inner">
          <h2 className="error-text">Could not open this project</h2>
          <p className="muted">{loadError}</p>
          <button className="btn" onClick={() => navigate("/")}>Back to home</button>
        </div>
      </div>
    );
  }
  if (!project) return <div className="lib-empty">Loading project…</div>;

  return (
    <div className="editor">
      <TopBar onExport={() => setShowExport(true)} onHelp={() => setShowHelp(true)} />
      <div className="workspace">
        <PanelGroup direction="vertical" autoSaveId="ligxt-v">
          <Panel defaultSize={64} minSize={30}>
            <PanelGroup direction="horizontal" autoSaveId="ligxt-h">
              <Panel defaultSize={22} minSize={14} maxSize={40}>
                <MediaLibrary />
              </Panel>
              <PanelResizeHandle className="resize-h" />
              <Panel defaultSize={54} minSize={25}>
                <Preview />
              </Panel>
              <PanelResizeHandle className="resize-h" />
              <Panel defaultSize={24} minSize={16} maxSize={40}>
                <Properties />
              </Panel>
            </PanelGroup>
          </Panel>
          <PanelResizeHandle className="resize-v" />
          <Panel defaultSize={36} minSize={18}>
            <Timeline />
          </Panel>
        </PanelGroup>
      </div>
      {showExport && <ExportDialog onClose={() => setShowExport(false)} />}
      {showHelp && <ShortcutsOverlay onClose={() => setShowHelp(false)} />}
    </div>
  );
}

function TopBar({ onExport, onHelp }: { onExport: () => void; onHelp: () => void }) {
  const name = useEditor((s) => s.project!.name);
  const saveState = useEditor((s) => s.saveState);
  const lastSaved = useEditor((s) => s.lastSaved);
  const canUndo = useEditor((s) => s.past.length > 0);
  const canRedo = useEditor((s) => s.future.length > 0);
  const jobs = useApp((s) => s.jobs);
  const running = Object.values(jobs).filter((j) => (j.status === "running" || j.status === "queued") && j.kind !== "export");
  const [draft, setDraft] = useState(name);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => setDraft(name), [name]);

  const commitName = () => {
    const v = draft.trim();
    if (v && v !== name) useEditor.getState().commit((p) => void (p.name = v));
    else setDraft(name);
  };

  const goHome = async () => {
    const s = useEditor.getState();
    if (s.saveState !== "saved" && !(await s.save("manual"))) {
      if (!window.confirm("Saving failed. Leave anyway and lose unsaved changes?")) return;
    }
    navigate("/");
  };

  const stateLabel =
    saveState === "saving" ? "Saving…" :
    saveState === "dirty" ? "Unsaved changes" :
    saveState === "error" ? "Save failed" :
    lastSaved ? `Saved ${new Date(lastSaved * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "Saved";

  return (
    <div className="topbar">
      <button className="btn ghost icon" onClick={goHome} title="Home (saves first)">
        <Logo size={24} />
      </button>
      <input
        ref={inputRef}
        className="name-input"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commitName}
        onKeyDown={(e) => {
          if (e.key === "Enter") inputRef.current?.blur();
          if (e.key === "Escape") {
            setDraft(name);
            setTimeout(() => inputRef.current?.blur());
          }
        }}
        title="Rename project"
      />
      <span className="save-state">
        <span className={`dot ${saveState === "saved" ? "ok" : saveState === "error" ? "err" : "warn"}`} />
        {stateLabel}
      </span>
      <div className="spacer" />
      {running.length > 0 && (
        <span className="jobs-pill" title={running.map((j) => `${j.label}: ${Math.round(j.progress * 100)}%`).join("\n")}>
          <Icon name="loader" size={13} className="spin" /> Processing {running.length} file{running.length > 1 ? "s" : ""}
        </span>
      )}
      <button className="btn ghost icon" disabled={!canUndo} onClick={() => useEditor.getState().undo()} title="Undo (Ctrl+Z)">
        <Icon name="undo" />
      </button>
      <button className="btn ghost icon" disabled={!canRedo} onClick={() => useEditor.getState().redo()} title="Redo (Ctrl+Y)">
        <Icon name="redo" />
      </button>
      <button className="btn ghost icon" onClick={onHelp} title="Keyboard shortcuts (?)">
        <Icon name="keyboard" />
      </button>
      <button
        className="btn"
        onClick={() => useEditor.getState().save("manual").then((ok) => ok && useApp.getState().toast({ kind: "success", title: "Project saved" }))}
        title="Save (Ctrl+S)"
      >
        <Icon name="save" /> Save
      </button>
      <button className="btn primary" onClick={onExport} title="Export (Ctrl+E)">
        <Icon name="export" /> Export
      </button>
    </div>
  );
}

const SHORTCUTS: [string, string, boolean?][] = [
  ["Space", "Play / pause"],
  ["← / →", "Step one frame"],
  ["Shift + ← / →", "Step one second"],
  ["Home / End", "Go to start / end"],
  ["Delete", "Delete selected clip"],
  ["Ctrl + Z", "Undo"],
  ["Ctrl + Y", "Redo"],
  ["Ctrl + S", "Save"],
  ["Ctrl + E", "Export"],
  ["+ / −", "Zoom timeline"],
  ["Ctrl + scroll", "Zoom timeline"],
  ["Esc", "Clear selection"],
  ["?", "This help"],
  ["S", "Split at playhead", true],
  ["J / K / L", "Shuttle", true],
  ["M", "Add marker", true],
];

export function ShortcutsOverlay({ onClose }: { onClose: () => void }) {
  return (
    <Modal title="Keyboard shortcuts" onClose={onClose} wide>
      <div className="shortcut-list">
        {SHORTCUTS.map(([k, d, soon]) => (
          <div key={k} className={soon ? "soon" : ""}>
            <span>{d}{soon ? " (Phase 2)" : ""}</span>
            <span className="kbd">{k}</span>
          </div>
        ))}
      </div>
    </Modal>
  );
}

