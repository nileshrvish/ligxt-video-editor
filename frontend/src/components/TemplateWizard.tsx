// "Use template": 1 media (drop files, order = shots), 2 music (beats, start point), 3 text, 4 generate.
import { useEffect, useMemo, useRef, useState } from "react";
import { api, uploadMedia } from "../api";
import { navigate } from "../router";
import { toastError, useApp } from "../store/app";
import type { MediaMeta, TemplateSummary } from "../types";
import { Modal, ProgressBar } from "./Common";
import { Icon } from "./Icons";

type Tpl = { slots: { ref: number; section?: string; lengthBeats: number }[]; refs?: number; music?: { file?: string | null; license?: string | null; name?: string } };

function useMediaPoll(ids: string[]) {
  const [media, setMedia] = useState<Record<string, MediaMeta>>({});
  const key = ids.join(",");
  useEffect(() => {
    if (!ids.length) return;
    let alive = true;
    const tick = async () => {
      try {
        const r = await api.getMedia(ids);
        if (alive) setMedia(Object.fromEntries(r.media.map((m) => [m.id, m])));
      } catch { /* retry */ }
    };
    tick();
    const h = setInterval(tick, 1200);
    return () => { alive = false; clearInterval(h); };
  }, [key]);
  return media;
}

export default function TemplateWizard({ summary, onClose }: { summary: TemplateSummary; onClose: () => void }) {
  const [step, setStep] = useState(0);
  const [tpl, setTpl] = useState<Tpl | null>(null);
  const [mediaIds, setMediaIds] = useState<string[]>([]);
  const [uploads, setUploads] = useState<Record<string, number>>({});
  const [songId, setSongId] = useState<string | null>(null);
  const [useBundled, setUseBundled] = useState(summary.music.bundled);
  const [start, setStart] = useState(0);
  const [texts, setTexts] = useState<Record<string, string>>(Object.fromEntries(summary.texts.map((t) => [t.key, t.placeholder])));
  const [name, setName] = useState(`${summary.name} project`);
  const [jobId, setJobId] = useState<string | null>(null);
  const [beatJob, setBeatJob] = useState<string | null>(null);
  const job = useApp((s) => (jobId ? s.jobs[jobId] : undefined));

  const fileRef = useRef<HTMLInputElement>(null);
  const songRef = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState<number | null>(null);
  const media = useMediaPoll([...mediaIds, ...(songId ? [songId] : [])]);

  useEffect(() => { api.template(summary.id).then((r) => setTpl(r.template as Tpl), (e) => toastError("Could not load the template", e)); }, [summary.id]);

  const upload = (files: FileList | File[], asSong = false) => {
    for (const f of Array.from(files)) {
      const key = `${f.name}-${Math.random()}`;
      const u = uploadMedia(f, (p) => setUploads((x) => ({ ...x, [key]: p })));
      u.promise.then((r) => {
        setUploads((x) => { const y = { ...x }; delete y[key]; return y; });
        if (asSong) {
          setSongId(r.media.id);
          setUseBundled(false);
        } else setMediaIds((ids) => [...ids, r.media.id]);
      }, (e) => { setUploads((x) => { const y = { ...x }; delete y[key]; return y; }); toastError(`Upload failed: ${f.name}`, e); });
    }
  };

  // shots = distinct media the template shows (refs), with where they appear
  const shots = useMemo(() => {
    if (!tpl) return [];
    const n = tpl.refs ?? tpl.slots.length;
    return Array.from({ length: n }, (_, r) => {
      const uses = tpl.slots.filter((s) => s.ref === r);
      return { ref: r, section: uses[0]?.section ?? "", uses: uses.length, beats: uses.reduce((a, s) => a + s.lengthBeats, 0) };
    });
  }, [tpl]);
  const n = shots.length;
  const plan = !mediaIds.length ? "" : mediaIds.length < n
    ? `${mediaIds.length} files for ${n} shots: files are reused in order (shot ${mediaIds.length + 1} shows file 1 again…).`
    : mediaIds.length > n
      ? `${mediaIds.length} files for ${n} shots: the extra files replace repeated shots; any left over are not used.`
      : `One file per shot.`;

  // music: beats
  const song = songId ? media[songId] : undefined;
  useEffect(() => {
    if (song && song.status !== "processing" && !song.beats && !beatJob) api.detectBeats(song.id).then((r) => setBeatJob(r.jobId), (e) => toastError("Beat detection failed", e));
  }, [song?.status, song?.beats, beatJob]);
  const beats = song?.beats ?? [];
  const snapStart = (t: number) => (beats.length ? beats.reduce((b, x) => (Math.abs(x - t) < Math.abs(b - t) ? x : b), beats[0]) : t);
  const bpm = song?.bpm ?? 0;
  const needSec = bpm ? (summary.beats * 60) / bpm : summary.duration;
  const songOk = useBundled || (!!song && !!song.beats?.length);
  const mediaReady = mediaIds.length > 0 && mediaIds.every((id) => media[id] && media[id].status !== "processing") && !Object.keys(uploads).length;

  const generate = () => {
    api.generateFromTemplate(summary.id, { media: mediaIds, music: useBundled ? null : songId, musicStart: useBundled ? 0 : start, texts, name })
      .then((r) => setJobId(r.jobId), (e) => toastError("Could not start", e));
  };
  const steps = ["Media", "Music", "Text", "Create"];
  const canNext = [mediaReady, songOk, true, false][step];

  return (
    <Modal title={`Use template: ${summary.name}`} onClose={onClose} wide
      footer={job?.status === "done" ? (
        <><button className="btn" onClick={onClose}>Close</button>
          <button className="btn primary" onClick={() => { onClose(); navigate(`/p/${job.result.projectId}`); }}>Open project</button></>
      ) : (
        <><button className="btn" onClick={() => (step ? setStep(step - 1) : onClose())}>{step ? "Back" : "Cancel"}</button>
          {step < 3 ? <button className="btn primary" disabled={!canNext} onClick={() => setStep(step + 1)}>Next</button>
            : <button className="btn primary" disabled={!!job && job.status !== "error"} onClick={generate}><Icon name="sparkles" size={14} /> Create project</button>}</>
      )}>
      <div className="wiz-steps">{steps.map((s, i) => <span key={s} className={i === step ? "on" : i < step ? "done" : ""}>{i + 1}. {s}</span>)}</div>

      {step === 0 && (
        <div className="wiz-body" onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); if (e.dataTransfer.files.length) upload(e.dataTransfer.files); }}>
          <div className="muted" style={{ fontSize: 13 }}>Drop photos or videos here (or <a href="#" onClick={(e) => { e.preventDefault(); fileRef.current?.click(); }}>choose files</a>). Their order fills the template's shots; drag a file onto another to swap them.</div>
          <input ref={fileRef} type="file" multiple accept="image/*,video/*" hidden onChange={(e) => { if (e.target.files) upload(e.target.files); e.target.value = ""; }} />
          <div className="wiz-slots">
            {shots.map((s, i) => {
              const id = mediaIds.length ? mediaIds[i % mediaIds.length] : null;
              const m = id ? media[id] : undefined;
              return (
                <div key={s.ref} className={`wiz-slot ${drag !== null ? "dropzone" : ""}`} draggable={!!id && i < mediaIds.length}
                  onDragStart={() => setDrag(i)} onDragEnd={() => setDrag(null)}
                  onDrop={(e) => { e.stopPropagation(); if (drag !== null && i < mediaIds.length && drag !== i) setMediaIds((ids) => { const x = [...ids]; [x[drag], x[i]] = [x[i], x[drag]]; return x; }); setDrag(null); }}>
                  <div className="wiz-thumb" style={m?.thumbnail ? { backgroundImage: `url(${m.thumbnail})` } : undefined}>{!m && <span>{i + 1}</span>}
                    {m && i >= mediaIds.length && <span className="wiz-reuse">reused</span>}
                    {m?.status === "processing" && <span className="wiz-reuse">preparing…</span>}</div>
                  <small>Shot {i + 1} · {s.section}{s.uses > 1 ? ` · ${s.uses}×` : ""} · {s.beats} beats</small>
                </div>
              );
            })}
          </div>
          {Object.values(uploads).map((p, i) => <ProgressBar key={i} value={p} />)}
          {plan && <div className="tr-warning">{plan}</div>}
          {mediaIds.length > 0 && <button className="btn sm ghost" onClick={() => setMediaIds([])}>Clear files</button>}
        </div>
      )}

      {step === 1 && (
        <div className="wiz-body">
          {summary.music.bundled && (
            <label className="row fl-check"><input type="radio" checked={useBundled} onChange={() => setUseBundled(true)} />
              Use the template's song ({tpl?.music?.name ?? "bundled"}, {summary.music.license})</label>
          )}
          <label className="row fl-check"><input type="radio" checked={!useBundled} onChange={() => setUseBundled(false)} />
            My song: <button className="btn sm" onClick={() => songRef.current?.click()}><Icon name="music" size={12} /> {song ? song.name : "Choose audio file"}</button></label>
          <input ref={songRef} type="file" accept="audio/*,video/*" hidden onChange={(e) => { if (e.target.files) upload(e.target.files, true); e.target.value = ""; }} />
          {!useBundled && song && (
            !song.beats ? <div className="row"><Icon name="loader" className="spin" size={14} /> Finding the beats…</div> : (
              <>
                <div className="muted" style={{ fontSize: 13 }}>{song.bpm} BPM · {song.beats.length} beats. The template needs {summary.beats} beats ≈ {needSec.toFixed(1)} s of this song.</div>
                <div className="prop-row"><label>Start at</label>
                  <div className="slider-row">
                    <input type="range" min={0} max={Math.max(0, song.duration - 1)} step={0.01} value={start} onChange={(e) => setStart(snapStart(+e.target.value))} />
                    <span className="mono faint">{start.toFixed(2)} s</span>
                  </div>
                </div>
                <audio controls src={`${song.proxy ?? "/files/" + song.file}#t=${start}`} style={{ width: "100%" }} />
                {start + needSec > song.duration && <div className="tr-warning">The song ends before the template does: choose an earlier start.</div>}
                <div className="faint" style={{ fontSize: 11 }}>The start snaps to a beat; every cut of the template lands on this song's beats.</div>
              </>
            )
          )}
        </div>
      )}

      {step === 2 && (
        <div className="wiz-body">
          {summary.texts.length === 0 && <div className="faint">This template has no text.</div>}
          {summary.texts.map((t) => (
            <div key={t.key} className="prop-row"><label>{t.role === "hook" ? "Hook title" : t.role === "outro" ? "Closing line" : t.role === "caption" ? "Beat caption" : "Text"}</label>
              <input className="input" value={texts[t.key] ?? ""} onChange={(e) => setTexts({ ...texts, [t.key]: e.target.value })} placeholder={t.placeholder} />
            </div>
          ))}
          <div className="faint" style={{ fontSize: 11 }}>Beat captions show one word per beat; more or fewer words simply continue on the beat. Empty = the template's text.</div>
        </div>
      )}

      {step === 3 && (
        <div className="wiz-body">
          <div className="prop-row"><label>Project name</label><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></div>
          <ul className="wiz-list">
            <li>{mediaIds.length} file(s) into {n} shots ({summary.slots} cuts). {plan}</li>
            <li>Song: {useBundled ? "the template's song" : `${song?.name} from ${start.toFixed(2)} s (${song?.bpm} BPM)`}</li>
            <li>Faces are kept out of the Reels header, caption area and buttons (photos get an extended canvas when needed); shots are colour-matched; text moves off faces.</li>
          </ul>
          {job && (job.status === "running" || job.status === "queued") && <ProgressBar value={job.progress} />}
          {job?.status === "error" && <div className="error-text">{job.error}</div>}
          {job?.status === "done" && (
            <div className="tr-warning" style={{ color: "var(--text)" }}>
              <b>Created. What the template did:</b>
              <ul className="wiz-list">{(job.result.report as string[]).map((r, i) => <li key={i}>{r}</li>)}</ul>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}
