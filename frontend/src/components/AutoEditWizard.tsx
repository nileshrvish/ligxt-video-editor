// Home > Auto-edit: 1 footage (a folder, or the clips of a project), 2 song + style + text, 3 analysis (background
// job, cached), 4 review (shots with thumbnails, scores and reasons; swap from ranked alternatives, lock, exclude a
// clip, regenerate, switch style), 5 create a normal project + report.
import { useEffect, useMemo, useRef, useState } from "react";
import { api, uploadMedia } from "../api";
import { excludeClip, fmtScore, locksOf, STYLES, swap, toggleLock, type Draft, type Locks, type StyleId } from "../autoedit/model";
import { toastError, useApp } from "../store/app";
import type { MediaMeta, ProjectSummary, TemplateSummary } from "../types";
import { navigate } from "../router";
import { Modal, ProgressBar } from "./Common";
import { Icon } from "./Icons";

async function post<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? `${r.status}`);
  return r.json() as Promise<T>;
}

const isClip = (m: MediaMeta) => m.type === "video" || m.type === "image";

export default function AutoEditWizard({ onClose }: { onClose: () => void }) {
  const [step, setStep] = useState(0);
  const [mediaIds, setMediaIds] = useState<string[]>([]);
  const [media, setMedia] = useState<Record<string, MediaMeta>>({});
  const [uploads, setUploads] = useState<Record<string, number>>({});
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [templates, setTemplates] = useState<TemplateSummary[]>([]);
  const [songId, setSongId] = useState<string | null>(null);
  const [style, setStyle] = useState<StyleId>("energetic");
  const [template, setTemplate] = useState<string>("");
  const [hook, setHook] = useState("");
  const [outro, setOutro] = useState("");
  const [name, setName] = useState("Auto edit");
  const [beatJob, setBeatJob] = useState<string | null>(null);
  const [anJob, setAnJob] = useState<string | null>(null);
  const [createJob, setCreateJob] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const jobs = useApp((s) => s.jobs);
  const folderRef = useRef<HTMLInputElement>(null);
  const songRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.listProjects().then((r) => setProjects(r.projects), () => undefined);
    api.templates().then((r) => setTemplates(r.templates), () => undefined);
  }, []);
  // keep the media metadata fresh (imports finishing, beats found)
  const allIds = useMemo(() => [...mediaIds, ...(songId ? [songId] : [])], [mediaIds, songId]);
  useEffect(() => {
    if (!allIds.length) return;
    let alive = true;
    const tick = () => api.getMedia(allIds).then((r) => alive && setMedia(Object.fromEntries(r.media.map((m) => [m.id, m]))), () => undefined);
    tick();
    const h = setInterval(tick, 1500);
    return () => { alive = false; clearInterval(h); };
  }, [allIds.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  const upload = (files: FileList | File[], asSong = false) => {
    for (const f of Array.from(files)) {
      const audio = f.type.startsWith("audio/") || /\.(mp3|wav|m4a|aac|flac|ogg)$/i.test(f.name);
      const visual = f.type.startsWith("video/") || f.type.startsWith("image/") || /\.(mp4|mov|mkv|webm|jpg|jpeg|png|heic|webp)$/i.test(f.name);
      if (!asSong && !audio && !visual) continue;
      const key = `${f.name}-${Math.random()}`;
      const u = uploadMedia(f, (p) => setUploads((x) => ({ ...x, [key]: p })));
      u.promise.then((r) => {
        setUploads((x) => { const y = { ...x }; delete y[key]; return y; });
        if (asSong || audio) setSongId((s) => s ?? r.media.id);
        else setMediaIds((ids) => [...ids, r.media.id]);
      }, (e) => { setUploads((x) => { const y = { ...x }; delete y[key]; return y; }); toastError(`Import failed: ${f.name}`, e); });
    }
  };
  const fromProject = async (pid: string) => {
    if (!pid) return;
    try {
      const p = await api.getProject(pid);
      const r = await api.getMedia(p.media);
      setMediaIds(r.media.filter(isClip).map((m) => m.id));
      const song = r.media.find((m) => m.type === "audio" && !m.sfx && (m.beats?.length ?? 0) > 0) ?? r.media.find((m) => m.type === "audio" && !m.sfx);
      if (song) setSongId(song.id);
      setName(`${p.name} auto edit`);
      if (!hook) setHook(p.name);
    } catch (e) {
      toastError("Could not read the project", e);
    }
  };

  const song = songId ? media[songId] : undefined;
  useEffect(() => {
    if (song && song.status !== "processing" && !song.beats && !beatJob) api.detectBeats(song.id).then((r) => setBeatJob(r.jobId), (e) => toastError("Beat detection failed", e));
  }, [song?.status, song?.beats, beatJob]); // eslint-disable-line react-hooks/exhaustive-deps
  const clips = mediaIds.map((id) => media[id]).filter(Boolean);
  const ready = clips.length > 0 && clips.every((m) => m.status !== "processing") && !Object.keys(uploads).length;
  const songOk = !!song?.beats?.length;

  const makeDraft = async (opts: { locks?: Locks; exclude?: string[]; style?: StyleId } = {}) => {
    if (!songId) return;
    setBusy(true);
    try {
      const d = await post<Draft>("/api/autoedit/draft", {
        media: mediaIds, music: songId, style: opts.style ?? style, template: template || null, locks: opts.locks ?? (draft ? locksOf(draft) : {}),
        exclude: opts.exclude ?? draft?.exclude ?? [], texts: { hook, outro },
      });
      setDraft(d);
    } catch (e) {
      toastError("Could not build the draft", e);
    } finally {
      setBusy(false);
    }
  };
  const analyse = async () => {
    try {
      const r = await post<{ jobId: string }>("/api/autoedit/analyze", { media: mediaIds });
      setAnJob(r.jobId);
      setStep(2);
    } catch (e) {
      toastError("Could not start the analysis", e);
    }
  };
  const an = anJob ? jobs[anJob] : undefined;
  useEffect(() => {
    if (an?.status === "done" && step === 2) {
      void makeDraft().then(() => setStep(3));
    }
  }, [an?.status]); // eslint-disable-line react-hooks/exhaustive-deps
  const cj = createJob ? jobs[createJob] : undefined;
  const create = async () => {
    if (!draft) return;
    try {
      const r = await post<{ jobId: string }>("/api/autoedit/create", { draft: { ...draft, texts: { hook, outro } }, name });
      setCreateJob(r.jobId);
      setStep(4);
    } catch (e) {
      toastError("Could not create the project", e);
    }
  };

  const steps = ["Footage", "Song & style", "Analyse", "Review", "Create"];
  const footer = (
    <>
      <button className="btn" onClick={() => (step && step !== 2 && step !== 4 ? setStep(step === 3 ? 1 : step - 1) : onClose())}>{step && step !== 2 && step !== 4 ? "Back" : "Close"}</button>
      {step === 0 && <button className="btn primary" disabled={!ready} onClick={() => setStep(1)}>Next</button>}
      {step === 1 && <button className="btn primary" disabled={!songOk} onClick={analyse}><Icon name="wand" size={14} /> Analyse footage</button>}
      {step === 3 && <button className="btn primary" disabled={!draft || busy} onClick={create}><Icon name="sparkles" size={14} /> Create project</button>}
      {step === 4 && cj?.status === "done" && <button className="btn primary" onClick={() => { onClose(); navigate(`/p/${cj.result.projectId}`); }}>Open project</button>}
    </>
  );

  return (
    <Modal title="Auto-edit from raw footage" onClose={onClose} wide footer={footer}>
      <div className="wiz-steps">{steps.map((s, i) => <span key={s} className={i === step ? "on" : i < step ? "done" : ""}>{i + 1}. {s}</span>)}</div>
      {step === 0 && (
        <div className="wiz-body ae">
          <p className="faint">Pick a folder of clips and photos (a song in it is used as the music), or use the clips of a project.</p>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <button className="btn" onClick={() => folderRef.current?.click()}><Icon name="folder" size={14} /> Choose a folder…</button>
            <input ref={folderRef} data-testid="ae-folder" type="file" multiple hidden
              {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
              onChange={(e) => { if (e.target.files) upload(e.target.files); e.target.value = ""; }} />
            <select className="input" data-testid="ae-project" defaultValue="" onChange={(e) => fromProject(e.target.value)} style={{ maxWidth: 280 }}>
              <option value="">…or use the clips of a project</option>
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </div>
          {Object.values(uploads).map((p, i) => <ProgressBar key={i} value={p} />)}
          <div className="ae-clips">
            {clips.map((m) => (
              <div key={m.id} className="ae-clip" title={m.name}>
                {m.thumbnail ? <img src={m.thumbnail} alt="" /> : <div className="ph" />}
                <small>{m.name.slice(0, 18)}{m.type === "video" ? ` · ${m.duration.toFixed(1)} s` : " · photo"}</small>
              </div>
            ))}
          </div>
          <div className="faint" style={{ fontSize: 12 }}>{clips.length} clip(s){song ? ` · song: ${song.name}` : ""}</div>
        </div>
      )}
      {step === 1 && (
        <div className="wiz-body ae">
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <b>Song</b>
            <span className="faint">{song ? song.name : "none yet"}</span>
            {song && !song.beats && <><Icon name="loader" size={13} className="spin" /> <span className="faint">finding the beats…</span></>}
            <button className="btn sm" onClick={() => songRef.current?.click()}>Choose a song…</button>
            <input ref={songRef} type="file" accept="audio/*" hidden onChange={(e) => { if (e.target.files) { setSongId(null); upload(e.target.files, true); } e.target.value = ""; }} />
          </div>
          <div className="ae-styles">
            {STYLES.map((s) => (
              <button key={s.id} className={`choice ${style === s.id ? "on" : ""}`} onClick={() => setStyle(s.id)} data-testid={`ae-style-${s.id}`}>
                <b>{s.label}</b><span>{s.blurb}</span>
              </button>
            ))}
          </div>
          <div className="fl-row">
            <label>Structure</label>
            <select className="input" value={template} onChange={(e) => setTemplate(e.target.value)}>
              <option value="">No template: beat-sync to the song</option>
              {templates.map((t) => <option key={t.id} value={t.id}>Template: {t.name}</option>)}
            </select>
            <span />
          </div>
          <div className="fl-row"><label>Title</label><input className="input" value={hook} placeholder="optional, e.g. TRAVEL DUMP" onChange={(e) => setHook(e.target.value)} /><span /></div>
          <div className="fl-row"><label>Closing line</label><input className="input" value={outro} placeholder="optional" onChange={(e) => setOutro(e.target.value)} /><span /></div>
          <div className="fl-row"><label>Project name</label><input className="input" value={name} onChange={(e) => setName(e.target.value)} /><span /></div>
          <div className="faint" style={{ fontSize: 11 }}>No sound effects: the music carries the edit. Text is kept to a title and an optional closing line.</div>
        </div>
      )}
      {step === 2 && (
        <div className="wiz-body ae">
          <p>Analysing the footage: shots, sharpness, exposure, faces, motion, shake, composition, near-duplicates. It runs once per clip (cached).</p>
          <div className="row" style={{ gap: 8 }}>
            <Icon name="loader" size={14} className="spin" />
            <span>{an?.message || "Starting…"} {Math.round((an?.progress ?? 0) * 100)}%</span>
            <span className="spacer" />
            {an && (an.status === "running" || an.status === "queued") && <button className="btn ghost xs" onClick={() => api.cancelJob(an.id)}>Cancel</button>}
          </div>
          <ProgressBar value={an?.progress ?? 0} />
          {an?.status === "error" && <div style={{ color: "var(--danger)" }}>{an.error}</div>}
          {an?.status === "cancelled" && <div className="faint">Cancelled. <button className="btn xs" onClick={analyse}>Start again</button></div>}
        </div>
      )}
      {step === 3 && draft && (
        <div className="wiz-body ae">
          <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <div className="seg">
              {STYLES.map((s) => (
                <button key={s.id} className={draft.style === s.id ? "on" : ""} disabled={busy}
                  onClick={() => { setStyle(s.id); void makeDraft({ style: s.id, locks: {} }); }}>{s.label}</button>
              ))}
            </div>
            <button className="btn sm" disabled={busy} onClick={() => makeDraft()} title="Pick the unlocked shots again (locked ones stay)">
              <Icon name="refresh" size={13} /> Regenerate
            </button>
            <span className="faint" style={{ fontSize: 12 }}>
              {draft.slots.length} shots · {draft.length.toFixed(1)} s · drop at {draft.dropAt.toFixed(1)} s · {draft.shotsUsed} of {draft.shotsAvailable} usable shots
            </span>
          </div>
          <div className="ae-review" data-testid="ae-review">
            {draft.slots.map((s) => (
              <div key={s.i} className={`ae-slot ${s.pick?.locked ? "locked" : ""} sec-${s.section}`}>
                <div className="ae-time">{s.start.toFixed(2)} s<small>{s.section}{s.speed !== 1 ? ` · ${s.speed.toFixed(2)}x` : ""}</small></div>
                {s.pick ? (
                  <>
                    <img src={s.pick.thumb} alt="" />
                    <div className="ae-info">
                      <b title={s.pick.name}>{s.pick.name.slice(0, 26)}</b>
                      <span className="faint">{s.pick.in.toFixed(1)}-{s.pick.out.toFixed(1)} s · score {fmtScore(s.pick.score)}</span>
                      <span className="ae-reasons">{s.pick.reasons.join(", ")}</span>
                    </div>
                    <div className="ae-acts">
                      <button className={`btn xs ${s.pick.locked ? "primary" : ""}`} title="Keep this shot when regenerating" onClick={() => setDraft(toggleLock(draft, s.i))}>
                        {s.pick.locked ? "Locked" : "Lock"}
                      </button>
                      <button className="btn xs" onClick={() => setOpen(open === s.i ? null : s.i)}>Swap</button>
                      <button className="btn xs ghost" title="Leave this clip out of the whole edit" onClick={() => { const x = excludeClip(draft, s.pick!.mediaId); void makeDraft(x); }}>Exclude clip</button>
                    </div>
                  </>
                ) : <span className="faint">no shot fits this slot</span>}
                {open === s.i && (
                  <div className="ae-alts">
                    {s.alternatives.map((a) => (
                      <button key={`${a.mediaId}-${a.shot}`} className="ae-alt" title={a.reasons.join(", ")} onClick={() => { setDraft(swap(draft, s.i, a)); setOpen(null); }}>
                        <img src={a.thumb} alt="" />
                        <small>{a.name.slice(0, 14)} · {fmtScore(a.score)}</small>
                      </button>
                    ))}
                    {!s.alternatives.length && <span className="faint">No other shot fits.</span>}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
      {step === 4 && (
        <div className="wiz-body ae">
          {cj?.status !== "done" ? (
            <>
              <div className="row" style={{ gap: 8 }}><Icon name="loader" size={14} className="spin" /> {cj?.message || "Building the project…"}</div>
              <ProgressBar value={cj?.progress ?? 0} />
              {cj?.status === "error" && <div style={{ color: "var(--danger)" }}>{cj.error}</div>}
            </>
          ) : (
            <>
              <b>Project created. What was chosen and why:</b>
              <pre className="ae-report" data-testid="ae-report">{(cj.result.report as string[]).join("\n")}</pre>
            </>
          )}
        </div>
      )}
    </Modal>
  );
}
