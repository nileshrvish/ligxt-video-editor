// "Save as template" from the editor: structure + style of this project, no media.
import { useState } from "react";
import { api } from "../api";
import { toastError, useApp } from "../store/app";
import { useEditor } from "../store/editor";
import { Modal } from "./Common";
import { EXPORT_PRESETS } from "./ExportDialog";

export default function SaveTemplateDialog({ onClose }: { onClose: () => void }) {
  const project = useEditor((s) => s.project!);
  const [name, setName] = useState(project.name);
  const [category, setCategory] = useState("Beat sync");
  const [tags, setTags] = useState("");
  const [description, setDescription] = useState("");
  const [preset, setPreset] = useState(project.export.preset);
  const [preview, setPreview] = useState<"project" | "placeholder">("project");
  const [start, setStart] = useState(0);
  const [busy, setBusy] = useState(false);
  const [notes, setNotes] = useState<string[] | null>(null);
  const save = async () => {
    setBusy(true);
    try {
      await useEditor.getState().save("manual");
      const r = await api.saveTemplate({ project: useEditor.getState().project!, name, category, tags: tags.split(","), description,
        exportPreset: preset, previewStart: preview === "project" ? start : null });
      setNotes(r.notes);
      useApp.getState().toast({ kind: "success", title: `Template "${r.template.name}" saved`, detail: "Its preview is rendering; find it on the Home screen under Templates." });
    } catch (e) {
      toastError("Could not save the template", e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title="Save as template" onClose={onClose}
      footer={notes ? <button className="btn primary" onClick={onClose}>Done</button> : <><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy || !name.trim()} onClick={save}>{busy ? "Saving…" : "Save template"}</button></>}>
      {notes ? (
        <div className="props" style={{ gap: 8 }}>
          <b>Saved.</b>
          {notes.length > 0 && <ul className="wiz-list">{notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}
        </div>
      ) : (
        <div className="props" style={{ gap: 10 }}>
          <div className="muted" style={{ fontSize: 13 }}>Stores the beat structure, sections (from markers), transitions, motion, grade, text styles with placeholder text, sound effects and the export preset. Your photos, videos and (unless license-safe) song are not stored.</div>
          <div className="prop-row"><label>Name</label><input className="input" autoFocus value={name} onChange={(e) => setName(e.target.value)} /></div>
          <div className="prop-row"><label>Category</label><input className="input" value={category} onChange={(e) => setCategory(e.target.value)} /></div>
          <div className="prop-row"><label>Tags</label><input className="input" value={tags} placeholder="travel, beat sync, golden hour" onChange={(e) => setTags(e.target.value)} /></div>
          <div className="prop-row"><label>About</label><input className="input" value={description} onChange={(e) => setDescription(e.target.value)} /></div>
          <div className="prop-row"><label>Export</label>
            <select className="input" value={preset} onChange={(e) => setPreset(e.target.value)}>{EXPORT_PRESETS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}</select>
          </div>
          <div className="prop-row"><label>Preview</label>
            <div className="row" style={{ gap: 6 }}>
              <select className="input" value={preview} onChange={(e) => setPreview(e.target.value as "project" | "placeholder")}>
                <option value="project">From this project</option><option value="placeholder">Placeholder cards</option>
              </select>
              {preview === "project" && <label className="row" style={{ gap: 4, fontSize: 12 }}>from <input className="input" type="number" min={0} step={0.5} style={{ width: 70 }} value={start} onChange={(e) => setStart(+e.target.value)} /> s</label>}
            </div>
          </div>
          <div className="faint" style={{ fontSize: 11 }}>Tip: add markers named Breakdown, Drop, Peak, Outro to name the template's sections.</div>
        </div>
      )}
    </Modal>
  );
}
