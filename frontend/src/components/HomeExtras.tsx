// Home screen extras: version + crash notice, recent exports (open / re-export), unused media cleanup.
import { useEffect, useState } from "react";
import { api } from "../api";
import { draftProjectIds } from "../session";
import { toastError, useApp } from "../store/app";
import type { ProjectSummary } from "../types";
import { fmtBytes, timeAgo } from "../util";
import { Modal } from "./Common";
import { Icon } from "./Icons";
import { navigate } from "../router";

export function useVersion() {
  const [v, setV] = useState<{ version: string; recoveredFromCrash: boolean } | null>(null);
  useEffect(() => { fetch("/api/version").then((r) => r.json()).then(setV, () => void 0); }, []);
  return v;
}

export function RecoveryNotice({ projects }: { projects: ProjectSummary[] | null }) {
  const v = useVersion();
  const drafts = draftProjectIds();
  const withDrafts = (projects ?? []).filter((p) => drafts.includes(p.id));
  if (!v?.recoveredFromCrash && !withDrafts.length) return null;
  return (
    <div className="tr-warning" style={{ margin: "0 0 14px" }}>
      {v?.recoveredFromCrash && <div><b>Ligxt didn't close cleanly last time.</b> Projects are autosaved every 30 seconds and every change is also kept in this browser, so nothing should be lost.</div>}
      {withDrafts.length > 0 && (
        <div style={{ marginTop: 4 }}>Unsaved changes found in: {withDrafts.map((p, i) => (
          <span key={p.id}>{i ? ", " : ""}<a href="#" onClick={(e) => { e.preventDefault(); navigate(`/p/${p.id}`); }}>{p.name}</a></span>
        ))}. Open the project to restore them.</div>
      )}
    </div>
  );
}

interface ExportItem { file: string; path: string; url: string; size: number; time: number; projectName?: string; preset?: string; mode?: string; canReexport: boolean }

export function RecentExports() {
  const [items, setItems] = useState<ExportItem[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const jobs = useApp((s) => s.jobs);
  const doneCount = Object.values(jobs).filter((j) => j.kind === "export" && j.status === "done").length;
  useEffect(() => { fetch("/api/exports?limit=12").then((r) => r.json()).then((r) => setItems(r.exports), () => setItems([])); }, [doneCount]);
  if (!items?.length) return null;
  const reexport = async (x: ExportItem) => {
    setBusy(x.file);
    try {
      const r = await fetch(`/api/exports/${encodeURIComponent(x.file)}/reexport`, { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail);
      useApp.getState().toast({ kind: "info", title: `Exporting again: ${x.projectName}`, detail: "Same settings, the project's current saved state. Progress is in the jobs list." });
    } catch (e) {
      toastError("Re-export failed", e);
    } finally {
      setBusy(null);
    }
  };
  return (
    <>
      <div className="section-title" style={{ marginTop: 28 }}>Recent exports</div>
      <div className="exports-list">
        {items.map((x) => (
          <div key={x.file} className="export-row">
            <Icon name="film" size={14} />
            <span className="ex-name" title={x.path}>{x.file}</span>
            <span className="faint">{fmtBytes(x.size)} · {timeAgo(x.time)}{x.preset ? ` · ${x.preset}` : ""}</span>
            <span className="spacer" />
            <a className="btn ghost xs" href={x.url} target="_blank" rel="noreferrer">Play</a>
            <button className="btn ghost xs" onClick={() => api.openFolder(x.path).catch((e) => toastError("Could not open folder", e))}>Show in folder</button>
            <button className="btn ghost xs" disabled={!x.canReexport || busy === x.file} title={x.canReexport ? "Export this project again with the same settings" : "Made before v1.0: settings weren't recorded"}
              onClick={() => reexport(x)}>Re-export</button>
          </div>
        ))}
      </div>
    </>
  );
}

interface Unused { id: string; name: string; type: string; size: number; imported: number; thumbnail: string | null }

export function MediaCleanupDialog({ onClose }: { onClose: () => void }) {
  const [items, setItems] = useState<Unused[] | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const load = () => fetch("/api/media-cleanup").then((r) => r.json()).then((r) => { setItems(r.media); setSel(new Set(r.media.map((m: Unused) => m.id))); });
  useEffect(() => void load(), []);
  const total = (items ?? []).filter((m) => sel.has(m.id)).reduce((n, m) => n + m.size, 0);
  const del = async () => {
    if (!window.confirm(`Delete ${sel.size} unused media file(s) (${fmtBytes(total)})? They are not used by any project. This can't be undone.`)) return;
    const r = await fetch("/api/media-cleanup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids: [...sel] }) }).then((x) => x.json());
    useApp.getState().toast({ kind: "success", title: `Deleted ${r.removed} file(s)`, detail: `${fmtBytes(r.freed)} freed.` });
    load();
  };
  return (
    <Modal title="Media cleanup" onClose={onClose} wide
      footer={<><button className="btn" onClick={onClose}>Close</button><button className="btn danger" disabled={!sel.size} onClick={del}><Icon name="trash" size={13} /> Delete selected ({fmtBytes(total)})</button></>}>
      {items === null ? <div className="faint">Looking for unused media…</div> : items.length === 0 ? (
        <div className="empty">Every imported file is used by a project.</div>
      ) : (
        <div className="props" style={{ gap: 6 }}>
          <div className="muted" style={{ fontSize: 13 }}>{items.length} imported file(s) are not used by any project. Deleting removes the copy in Ligxt's media folder and its thumbnails/proxies, never the file you imported from.</div>
          <label className="row fl-check"><input type="checkbox" checked={sel.size === items.length} onChange={(e) => setSel(new Set(e.target.checked ? items.map((m) => m.id) : []))} /> Select all</label>
          <div className="cleanup-list">
            {items.map((m) => (
              <label key={m.id} className="cleanup-row">
                <input type="checkbox" checked={sel.has(m.id)} onChange={(e) => { const s = new Set(sel); if (e.target.checked) s.add(m.id); else s.delete(m.id); setSel(s); }} />
                <span className="cthumb2" style={m.thumbnail ? { backgroundImage: `url(${m.thumbnail})` } : undefined} />
                <span className="ex-name">{m.name}</span>
                <span className="faint">{m.type} · {fmtBytes(m.size)}{m.imported ? ` · ${timeAgo(m.imported)}` : ""}</span>
              </label>
            ))}
          </div>
        </div>
      )}
    </Modal>
  );
}
