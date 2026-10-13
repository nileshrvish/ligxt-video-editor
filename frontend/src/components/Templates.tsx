// Template gallery: cards (thumbnail, animated preview on hover, length, media slots, tags), search,
// categories, and management (use, rename, duplicate, delete, export/import .zip, new preview).
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { toastError, useApp } from "../store/app";
import type { TemplateSummary } from "../types";
import { fmtDuration } from "../util";
import { Modal } from "./Common";
import { Icon } from "./Icons";
import TemplateWizard from "./TemplateWizard";

export default function TemplatesGallery() {
  const [items, setItems] = useState<TemplateSummary[] | null>(null);
  const [cats, setCats] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const [cat, setCat] = useState("");
  const [use, setUse] = useState<TemplateSummary | null>(null);
  const [edit, setEdit] = useState<TemplateSummary | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const toast = useApp((s) => s.toast);
  const jobs = useApp((s) => s.jobs);

  const refresh = () => api.templates(q, cat).then((r) => { setItems(r.templates); setCats(r.categories); }, (e) => toastError("Could not load templates", e));
  useEffect(() => { const h = setTimeout(refresh, 200); return () => clearTimeout(h); }, [q, cat]);
  // refresh when a preview render finishes
  const previewDone = Object.values(jobs).filter((j) => j.kind === "template-preview" && j.status === "done").length;
  useEffect(() => void refresh(), [previewDone]);

  const del = async (t: TemplateSummary) => {
    if (!window.confirm(`Delete the template "${t.name}"? Projects made from it are not affected.`)) return;
    await api.deleteTemplate(t.id).then(() => { toast({ kind: "info", title: `Deleted "${t.name}"` }); refresh(); }, (e) => toastError("Delete failed", e));
  };
  const dup = (t: TemplateSummary) => api.duplicateTemplate(t.id).then(() => refresh(), (e) => toastError("Duplicate failed", e));

  return (
    <div className="tpl-gallery" id="templates">
      <div className="section-title">
        Templates
        <span className="spacer" />
        <input className="input" style={{ width: 200, height: 28 }} placeholder="Search templates" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="btn ghost sm" onClick={() => fileRef.current?.click()} title="Import a template .zip">
          <Icon name="upload" size={14} /> Import
        </button>
        <input ref={fileRef} type="file" accept=".zip" hidden onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = "";
          if (f) api.importTemplate(f).then((r) => { toast({ kind: "success", title: `Imported "${r.template.name}"`, detail: r.previewJobId ? "Rendering its preview…" : undefined }); refresh(); },
            (er) => toastError("Import failed", er));
        }} />
      </div>
      <div className="tpl-cats">
        {["", ...cats].map((c) => (
          <button key={c || "all"} className={`chip ${cat === c ? "on" : ""}`} onClick={() => setCat(c)}>{c || "All"}</button>
        ))}
      </div>
      {items === null ? <div className="faint">Loading…</div> : items.length === 0 ? (
        <div className="empty">No templates match. Save one from any project with <b>Save as template</b> in the editor.</div>
      ) : (
        <div className="tpl-grid">
          {items.map((t) => <TemplateCard key={t.id} t={t} onUse={() => setUse(t)} onEdit={() => setEdit(t)} onDuplicate={() => dup(t)} onDelete={() => del(t)} />)}
        </div>
      )}
      {use && <TemplateWizard summary={use} onClose={() => setUse(null)} />}
      {edit && <TemplateEditDialog t={edit} onClose={() => { setEdit(null); refresh(); }} />}
    </div>
  );
}

function TemplateCard({ t, onUse, onEdit, onDuplicate, onDelete }: { t: TemplateSummary; onUse: () => void; onEdit: () => void; onDuplicate: () => void; onDelete: () => void }) {
  const vid = useRef<HTMLVideoElement>(null);
  return (
    <div className="tpl-card" onMouseEnter={() => vid.current?.play().catch(() => void 0)} onMouseLeave={() => { const v = vid.current; if (v) { v.pause(); v.currentTime = 0; } }}>
      <div className="tpl-thumb" style={t.thumb ? { backgroundImage: `url(${t.thumb})` } : undefined} onClick={onUse} title={`Use "${t.name}"`}>
        {t.preview && <video ref={vid} src={t.preview} muted loop playsInline preload="none" />}
        {!t.thumb && <Icon name="sparkles" size={26} />}
        <span className="tpl-len">{fmtDuration(t.duration)}</span>
        {t.builtin && <span className="tpl-badge">Built-in</span>}
      </div>
      <div className="tpl-meta">
        <b title={t.description}>{t.name}</b>
        <span>{t.mediaSlots} media · {t.slots} shots · {Math.round(t.bpm)} BPM{t.music.bundled ? " · song included" : ""}</span>
        <div className="tpl-tags">{t.tags.slice(0, 4).map((x) => <span key={x}>{x}</span>)}</div>
      </div>
      <div className="tpl-actions">
        <button className="btn primary sm" onClick={onUse}>Use</button>
        <a className="btn ghost icon sm" title="Export as .zip (no personal media)" href={`/api/templates/${t.id}/export.zip`} download><Icon name="export" size={13} /></a>
        <button className="btn ghost icon sm" title="Duplicate" onClick={onDuplicate}><Icon name="copy" size={13} /></button>
        {!t.builtin && <button className="btn ghost icon sm" title="Rename, tags, preview" onClick={onEdit}><Icon name="text" size={13} /></button>}
        {!t.builtin && <button className="btn ghost icon sm danger" title="Delete" onClick={onDelete}><Icon name="trash" size={13} /></button>}
      </div>
    </div>
  );
}

function TemplateEditDialog({ t, onClose }: { t: TemplateSummary; onClose: () => void }) {
  const [name, setName] = useState(t.name);
  const [category, setCategory] = useState(t.category);
  const [tags, setTags] = useState(t.tags.join(", "));
  const [description, setDescription] = useState(t.description);
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [src, setSrc] = useState("placeholder");
  const [start, setStart] = useState(0);
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useApp((s) => (jobId ? s.jobs[jobId] : undefined));
  useEffect(() => { api.listProjects().then((r) => setProjects(r.projects)); }, []);
  const saveMeta = () => api.patchTemplate(t.id, { name, category, tags: tags.split(","), description }).then(onClose, (e) => toastError("Could not save", e));
  return (
    <Modal title={`Template: ${t.name}`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Close</button><button className="btn primary" onClick={saveMeta}>Save</button></>}>
      <div className="props" style={{ gap: 10 }}>
        <div className="prop-row"><label>Name</label><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></div>
        <div className="prop-row"><label>Category</label><input className="input" value={category} onChange={(e) => setCategory(e.target.value)} /></div>
        <div className="prop-row"><label>Tags</label><input className="input" value={tags} placeholder="comma, separated" onChange={(e) => setTags(e.target.value)} /></div>
        <div className="prop-row"><label>About</label><input className="input" value={description} onChange={(e) => setDescription(e.target.value)} /></div>
        <div className="label" style={{ marginTop: 8 }}>Thumbnail & animated preview</div>
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <select className="input" style={{ height: 28 }} value={src} onChange={(e) => setSrc(e.target.value)}>
            <option value="placeholder">Placeholder cards (no personal media)</option>
            {projects.map((p) => <option key={p.id} value={p.id}>From project: {p.name}</option>)}
          </select>
          {src !== "placeholder" && <label className="row" style={{ gap: 4, fontSize: 12 }}>from <input className="input" type="number" step={0.5} min={0} style={{ width: 70 }} value={start} onChange={(e) => setStart(+e.target.value)} /> s</label>}
          <button className="btn sm" disabled={job?.status === "running" || job?.status === "queued"}
            onClick={() => api.templatePreview(t.id, src === "placeholder" ? { source: "placeholder" } : { source: "project", projectId: src, start }).then((r) => setJobId(r.jobId), (e) => toastError("Preview failed", e))}>
            {job?.status === "running" || job?.status === "queued" ? "Rendering…" : "Render new preview"}
          </button>
          {job?.status === "done" && <span className="faint">Done.</span>}
        </div>
        <div className="faint" style={{ fontSize: 11 }}>Exported .zip files never contain your media; a preview made from your project is only included if you tick "include preview" when exporting from the API (off by default).</div>
      </div>
    </Modal>
  );
}
