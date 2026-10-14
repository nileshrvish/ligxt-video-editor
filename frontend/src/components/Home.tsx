import { useEffect, useState } from "react";
import { api } from "../api";
import { navigate } from "../router";
import TemplatesGallery from "./Templates";
import AutoEditWizard from "./AutoEditWizard";
import { MediaCleanupDialog, RecentExports, RecoveryNotice, useVersion } from "./HomeExtras";
import { toastError, useApp } from "../store/app";
import type { ProjectSummary } from "../types";
import { fmtDuration, timeAgo } from "../util";
import { AspectShape, Modal } from "./Common";
import { Icon, Logo } from "./Icons";

const ASPECT_CHOICES = [
  { aspect: "9:16", label: "Reels / Shorts", size: "1080×1920" },
  { aspect: "1:1", label: "Square", size: "1080×1080" },
  { aspect: "4:5", label: "Portrait post", size: "1080×1350" },
  { aspect: "16:9", label: "YouTube", size: "1920×1080" },
];

export function NewProjectDialog({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [aspect, setAspect] = useState("9:16");
  const [busy, setBusy] = useState(false);
  const create = async () => {
    setBusy(true);
    try {
      const p = await api.createProject(name.trim() || `Reel ${new Date().toLocaleDateString()}`, aspect);
      navigate(`/p/${p.id}`);
    } catch (e) {
      toastError("Could not create project", e);
      setBusy(false);
    }
  };
  return (
    <Modal
      title="New project"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" onClick={create} disabled={busy}>Create project</button>
        </>
      }
    >
      <div className="field">
        <label htmlFor="pname">Name</label>
        <input
          id="pname"
          className="input"
          autoFocus
          placeholder="My reel"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && create()}
        />
      </div>
      <div className="field">
        <label>Canvas</label>
        <div className="choice-grid">
          {ASPECT_CHOICES.map((c) => (
            <button key={c.aspect} className={`choice ${aspect === c.aspect ? "on" : ""}`} onClick={() => setAspect(c.aspect)}>
              <div style={{ height: 30, display: "grid", placeItems: "center" }}>
                <AspectShape aspect={c.aspect} />
              </div>
              <span>{c.label}</span>
              <small>{c.aspect} · {c.size}</small>
            </button>
          ))}
        </div>
      </div>
    </Modal>
  );
}

export default function Home() {
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [showAuto, setShowAuto] = useState(false);
  const [cleanup, setCleanup] = useState(false);
  const ver = useVersion();
  const toast = useApp((s) => s.toast);

  const refresh = () =>
    api.listProjects().then(
      (r) => setProjects(r.projects),
      (e) => {
        setProjects([]);
        toastError("Could not load projects", e);
      },
    );
  useEffect(() => {
    refresh();
  }, []);

  const remove = async (p: ProjectSummary) => {
    if (!window.confirm(`Delete "${p.name}"? Its autosaves are deleted too. Media files stay in the library.`)) return;
    try {
      await api.deleteProject(p.id);
      toast({ kind: "info", title: `Deleted "${p.name}"` });
      refresh();
    } catch (e) {
      toastError("Delete failed", e);
    }
  };
  const duplicate = async (p: ProjectSummary) => {
    try {
      await api.duplicateProject(p.id);
      refresh();
    } catch (e) {
      toastError("Duplicate failed", e);
    }
  };

  return (
    <div className="home">
      <div className="home-inner">
        <div className="brand">
          <Logo size={34} />
          <div>
            <h1>Ligxt Video Editor</h1>
            <div className="tag">Local editing for Reels, Shorts and more{ver ? ` · v${ver.version}` : ""}</div>
          </div>
        </div>

        <div className="home-actions">
          <button className="big-card" onClick={() => setShowNew(true)}>
            <div className="ic"><Icon name="plus" size={18} /></div>
            <b>New project</b>
            <span>Start from an empty timeline (9:16, 1:1, 4:5 or 16:9)</span>
          </button>
          <button className="big-card" onClick={() => setShowAuto(true)} data-testid="auto-edit">
            <div className="ic"><Icon name="wand" size={18} /></div>
            <b>Auto-edit</b>
            <span>Pick a folder of clips and a song: a beat-synced first cut, reviewed by you</span>
          </button>
          <button className="big-card" onClick={() => document.getElementById("templates")?.scrollIntoView({ behavior: "smooth" })}>
            <div className="ic"><Icon name="sparkles" size={18} /></div>
            <b>Templates</b>
            <span>Start from a style: drop in photos, pick a song, done</span>
          </button>
        </div>

        <RecoveryNotice projects={projects} />
        <div className="section-title">
          Recent projects
          <span className="spacer" />
          <button className="btn ghost sm" onClick={() => setCleanup(true)} title="Find imported files no project uses, and free the space">
            <Icon name="trash" size={14} /> Media cleanup
          </button>
          <button className="btn ghost sm" onClick={() => api.openFolder().catch((e) => toastError("Could not open folder", e))}>
            <Icon name="folder" size={14} /> Exports folder
          </button>
          <button className="btn ghost sm" onClick={() => navigate("/settings")} title="AI tools, models and disk usage">
            <Icon name="gear" size={14} /> Settings
          </button>
        </div>
        {projects === null ? (
          <div className="faint">Loading…</div>
        ) : projects.length === 0 ? (
          <div className="empty">No projects yet. Click <b>New project</b> to start editing.</div>
        ) : (
          <div className="project-grid">
            {projects.map((p) => (
              <div key={p.id} className="project-card" onClick={() => navigate(`/p/${p.id}`)} title={`Open ${p.name}`}>
                <div className="thumb" style={p.thumbnail ? { backgroundImage: `url(${p.thumbnail})` } : undefined}>
                  {!p.thumbnail && <Icon name="film" size={28} />}
                </div>
                <div className="meta">
                  <b>{p.name}</b>
                  <span>
                    {p.aspect} · {p.clips ?? 0} clips · {fmtDuration(p.duration ?? 0)} · {timeAgo(p.updated)}
                  </span>
                </div>
                <div className="pc-menu" onClick={(e) => e.stopPropagation()}>
                  <button className="btn icon sm" title="Duplicate" onClick={() => duplicate(p)}>
                    <Icon name="copy" size={13} />
                  </button>
                  <button className="btn icon sm danger" title="Delete" onClick={() => remove(p)}>
                    <Icon name="trash" size={13} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
        <TemplatesGallery />
        <RecentExports />
      </div>
      {showNew && <NewProjectDialog onClose={() => setShowNew(false)} />}
      {showAuto && <AutoEditWizard onClose={() => setShowAuto(false)} />}
      {cleanup && <MediaCleanupDialog onClose={() => setCleanup(false)} />}
    </div>
  );
}
