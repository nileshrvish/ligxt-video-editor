// Keeps the open project safe: holds its lock (warns when another tab edits it), writes local drafts
// on every change, and offers to restore a draft that the tab or server never got to save.
import { useEffect, useRef, useState } from "react";
import { navigate } from "../router";
import { useApp } from "../store/app";
import { useEditor } from "../store/editor";
import { dropDraft, markDraftSynced, pendingDraft, releaseLock, takeLock, writeDraft } from "../session";
import type { Project } from "../types";
import { Modal } from "./Common";

const HEARTBEAT_MS = 10_000;

export default function ProjectGuard({ projectId }: { projectId: string }) {
  const project = useEditor((s) => s.project);
  const saveState = useEditor((s) => s.saveState);
  const readOnly = useEditor((s) => s.readOnly);
  const [blocked, setBlocked] = useState<{ owner: string; since: number } | null>(null);
  const [lost, setLost] = useState(false);
  const [draft, setDraft] = useState<{ at: number; project: Project } | null>(null);
  const checked = useRef(false);
  const loadedId = project?.id;

  // lock + heartbeat
  useEffect(() => {
    if (!loadedId) return;
    let alive = true;
    const beat = async (force = false) => {
      try {
        const r = await takeLock(projectId, force);
        if (!alive) return;
        if (r.ok) {
          setBlocked(null);
          if (useEditor.getState().readOnly && force) useEditor.getState().setReadOnly(false);
        } else if (!useEditor.getState().readOnly) {
          if (checked.current) setLost(true);
          else setBlocked({ owner: r.owner, since: r.since });
          useEditor.getState().setReadOnly(true);
        }
      } catch {
        /* server down: drafts keep the work safe; try again on the next beat */
      }
      checked.current = true;
    };
    beat();
    const h = setInterval(() => !useEditor.getState().readOnly && beat(), HEARTBEAT_MS);
    const bye = () => releaseLock(projectId);
    window.addEventListener("pagehide", bye);
    (window as unknown as { __ligxtTakeOver?: () => void }).__ligxtTakeOver = () => beat(true);
    return () => {
      alive = false;
      clearInterval(h);
      window.removeEventListener("pagehide", bye);
      if (!useEditor.getState().readOnly) releaseLock(projectId);
    };
  }, [projectId, loadedId]);

  // recovery: a newer unsaved local draft than the saved project?
  useEffect(() => {
    if (!project || draft) return;
    const d = pendingDraft(project);
    if (d) setDraft({ at: d.at, project: d.project });
  }, [loadedId]);

  // draft on every change (debounced); synced once saved
  useEffect(() => {
    if (!project || readOnly || draft) return;
    const h = setTimeout(() => writeDraft(project, useEditor.getState().saveState === "saved"), 800);
    return () => clearTimeout(h);
  }, [project]);
  useEffect(() => {
    if (project && saveState === "saved") markDraftSynced(project.id);
  }, [saveState]);

  const takeOver = async () => {
    const r = await takeLock(projectId, true);
    if (r.ok) {
      useEditor.getState().setReadOnly(false);
      setBlocked(null);
      setLost(false);
      // the other tab may have saved changes since this tab loaded the project
      await useEditor.getState().load(projectId);
    }
  };

  return (
    <>
      {blocked && (
        <Modal title="This project is open somewhere else" onClose={() => setBlocked(null)}
          footer={<>
            <button className="btn" onClick={() => navigate("/")}>Back to home</button>
            <button className="btn" onClick={() => setBlocked(null)}>View read-only</button>
            <button className="btn primary" onClick={takeOver}>Edit here instead</button>
          </>}>
          <p>Another browser tab or window ({blocked.owner}) has been editing it since {new Date(blocked.since * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}. Editing it in two places would overwrite one tab's changes with the other's.</p>
          <p className="faint" style={{ fontSize: 12 }}>"Edit here instead" makes the other tab read-only and loads the latest saved version here.</p>
        </Modal>
      )}
      {lost && (
        <Modal title="Another tab took over this project" onClose={() => setLost(false)}
          footer={<><button className="btn" onClick={() => setLost(false)}>Stay read-only</button><button className="btn primary" onClick={takeOver}>Edit here again</button></>}>
          <p>This tab is now read-only, so it can't overwrite the other tab's work. Your last changes here were saved up to the moment the other tab took over.</p>
        </Modal>
      )}
      {draft && project && (
        <Modal title="Restore unsaved changes?" onClose={() => { dropDraft(project.id); setDraft(null); }}
          footer={<>
            <button className="btn" onClick={() => { dropDraft(project.id); setDraft(null); }}>Discard them</button>
            <button className="btn primary" onClick={() => {
              const d = draft.project;
              useEditor.getState().commit((p) => { Object.assign(p, { ...d, id: p.id }); });
              useEditor.getState().save("manual").then((ok) => ok && useApp.getState().toast({ kind: "success", title: "Changes restored and saved" }));
              setDraft(null);
            }}>Restore</button>
          </>}>
          <p>This project has changes from {new Date(draft.at).toLocaleString()} that were never saved: the tab or the Ligxt server closed before the next autosave.</p>
          <p className="faint" style={{ fontSize: 12 }}>Restore puts them back (you can still undo). Discard keeps the last saved version.</p>
        </Modal>
      )}
      {readOnly && !blocked && !lost && (
        <div className="ro-banner">Read-only: this project is open in another tab. <button className="btn xs" onClick={takeOver}>Edit here</button></div>
      )}
    </>
  );
}
