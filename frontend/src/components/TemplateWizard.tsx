// "Use template": 1 media (drop files, order = shots), 2 music (beats, start point), 3 text, 4 generate.
import { useEffect, useMemo, useRef, useState } from "react";
import { api, uploadMedia } from "../api";
import { navigate } from "../router";
import { toastError, useApp } from "../store/app";
import type { MediaMeta, TemplateSummary } from "../types";
import { Modal, ProgressBar } from "./Common";
import { Icon } from "./Icons";

type Tpl = {
  slots: { ref: number; section?: string; lengthBeats: number }[]; refs?: number;
  music?: { file?: string | null; license?: string | null; name?: string };
  sections?: { name: string; startBeat: number; endBeat: number }[];
};
type Energy = { rate: number; curve: number[]; drop: number | null; dropBeat: number | null; candidates: { time: number; score: number; beat: number }[] };
type Accent = { selective: boolean; hue?: number | null; name?: string; templateName?: string; templateHue?: number; slot?: number; preview?: string; templatePreview?: string;
  shots?: number[]; hint?: { mediaId: string; name: string } | null };

/** Song energy over time with the detected drop, the template's span and where its drop lands. Click to set the start. */
function EnergyChart({ e, beats, start, span, dropAt, onPick }: { e: Energy; beats: number[]; start: number; span: number; dropAt: number | null; onPick: (t: number) => void }) {
  const dur = e.curve.length / e.rate;
  const W = 600, H = 70;
  const x = (t: number) => (t / dur) * W;
  const pts = e.curve.map((v, i) => `${x(i / e.rate).toFixed(1)},${(H - v * (H - 4)).toFixed(1)}`).join(" ");
  return (
    <svg className="energy" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" onClick={(ev) => {
      const r = (ev.currentTarget as SVGSVGElement).getBoundingClientRect();
      onPick(((ev.clientX - r.left) / r.width) * dur);
    }}>
      <rect x={x(start)} y={0} width={Math.max(1, x(start + span) - x(start))} height={H} className="energy-span" />
      <polyline points={`0,${H} ${pts} ${W},${H}`} className="energy-fill" />
      {beats.filter((_, i) => i % 4 === 0).map((b) => <line key={b} x1={x(b)} x2={x(b)} y1={H - 4} y2={H} className="energy-bar" />)}
      {e.drop != null && <line x1={x(e.drop)} x2={x(e.drop)} y1={0} y2={H} className="energy-drop" />}
      {dropAt != null && <line x1={x(dropAt)} x2={x(dropAt)} y1={0} y2={H} className="energy-tdrop" />}
      <line x1={x(start)} x2={x(start)} y1={0} y2={H} className="energy-start" />
    </svg>
  );
}

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
  const [energy, setEnergy] = useState<Energy | null>(null);
  const [startTouched, setStartTouched] = useState(false);
  const [accent, setAccent] = useState<Accent | null>(null);
  const [accentChoice, setAccentChoice] = useState<"auto" | "keep">("auto");
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
  // Same beat grid as the server (templates.Grid): beats before the first detected one are extrapolated,
  // so a song whose first beat is just after 0 s can start at 0 s (= beat -1).
  const period0 = beats.length > 1 ? (beats[Math.min(8, beats.length - 1)] - beats[0]) / Math.min(8, beats.length - 1) : 0.5;
  const startBeats = useMemo(() => {
    const pre: number[] = [];
    for (let k = 1; beats.length && beats[0] - k * period0 >= -0.1; k++) pre.unshift(Math.max(0, beats[0] - k * period0));
    return [...pre, ...beats];
  }, [beats, period0]);
  const beatPos = (t: number) => {
    if (!beats.length) return 0;
    if (t <= beats[0]) return (t - beats[0]) / period0;
    const i = beats.findIndex((x) => x > t);
    if (i < 0) return beats.length - 1 + (t - beats[beats.length - 1]) / period0;
    return i - 1 + (t - beats[i - 1]) / (beats[i] - beats[i - 1]);
  };
  const beatTime = (b: number) => (b < 0 || !beats.length ? (beats[0] ?? 0) + b * period0 : b < beats.length ? beats[b] : beats[beats.length - 1] + (b - beats.length + 1) * period0);
  const snapStart = (t: number) => (startBeats.length ? startBeats.reduce((b, x) => (Math.abs(x - t) < Math.abs(b - t) ? x : b), startBeats[0]) : t);
  const bpm = song?.bpm ?? 0;
  const needSec = bpm ? (summary.beats * 60) / bpm : summary.duration;
  const songOk = useBundled || (!!song && !!song.beats?.length);
  const mediaReady = mediaIds.length > 0 && mediaIds.every((id) => media[id] && media[id].status !== "processing") && !Object.keys(uploads).length;

  // song energy -> drop -> suggested start (the template's Drop section lands on the song's drop)
  useEffect(() => {
    if (!song?.beats?.length) return;
    setEnergy(null);
    fetch(`/api/media/${song.id}/energy`).then((r) => (r.ok ? r.json() : null)).then((e) => e && setEnergy(e), () => void 0);
  }, [song?.id, song?.beats?.length]);
  const tDrop = tpl?.sections?.find((x) => /drop/i.test(x.name))?.startBeat ?? null;
  const suggested = useMemo(() => {
    if (!energy?.dropBeat || tDrop == null || !beats.length) return null;
    const t = beatTime(energy.dropBeat - Math.round(tDrop));
    return t >= -0.1 && t < beats[beats.length - 1] ? Math.max(0, t) : null;
  }, [energy, tDrop, beats, period0]);
  useEffect(() => { if (suggested != null && !startTouched) setStart(suggested); }, [suggested]);
  const dropAt = tDrop != null && beats.length ? beatTime(Math.round(beatPos(snapStart(start))) + Math.round(tDrop)) : null;
  // accent colour suggestion for templates with selective colour
  useEffect(() => {
    if (step !== 2 || !mediaIds.length) return;
    api.templateAccent(summary.id, mediaIds).then(setAccent, () => setAccent(null));
  }, [step, mediaIds.join(",")]);
  const hueGap = accent?.hue != null && accent.templateHue != null ? Math.abs(accent.hue - accent.templateHue) % 360 : 999;
  const sameAccent = hueGap !== 999 && Math.min(hueGap, 360 - hueGap) < 12;

  const generate = () => {
    api.generateFromTemplate(summary.id, { media: mediaIds, music: useBundled ? null : songId, musicStart: useBundled ? 0 : start, texts, name, accent: accentChoice })
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
                  <div className={`wiz-thumb ${m ? "" : "empty"}`} style={m?.thumbnail ? { backgroundImage: `url(${m.thumbnail})` } : undefined}
                    onClick={m ? undefined : () => fileRef.current?.click()} title={m ? "Drag onto another shot to swap" : "Add photos or videos"}>{!m && <span>{i + 1}</span>}
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
                {energy && energy.curve.length > 0 && (
                  <>
                    <EnergyChart e={energy} beats={beats} start={start} span={needSec} dropAt={dropAt} onPick={(t) => { setStartTouched(true); setStart(snapStart(t)); }} />
                    <div className="row" style={{ gap: 8, fontSize: 12, flexWrap: "wrap" }}>
                      <span className="faint"><span className="lg lg-drop" /> song's drop {energy.drop != null ? `${energy.drop.toFixed(2)} s` : "(none found)"}</span>
                      <span className="faint"><span className="lg lg-tdrop" /> template's drop</span>
                      <span className="faint"><span className="lg lg-span" /> template ({needSec.toFixed(1)} s)</span>
                      {suggested != null && (Math.abs(snapStart(start) - suggested) < 0.02
                        ? <span className="faint" style={{ color: "var(--ok, #6fcf97)" }}>Drops lined up</span>
                        : <button className="btn sm" onClick={() => { setStartTouched(true); setStart(suggested); }}>Line up the drop (start {suggested.toFixed(2)} s)</button>)}
                    </div>
                    {suggested == null && tDrop != null && energy.drop != null && <div className="faint" style={{ fontSize: 11 }}>The song's drop comes too early to line up with the template's drop ({Math.round(tDrop)} beats in); pick a start by ear.</div>}
                  </>
                )}
                <div className="prop-row"><label>Start at</label>
                  <div className="slider-row">
                    <input type="range" min={0} max={Math.max(0, song.duration - 1)} step={0.01} value={start} onChange={(e) => { setStartTouched(true); setStart(snapStart(+e.target.value)); }} />
                    <span className="mono faint">{start.toFixed(2)} s</span>
                  </div>
                </div>
                <audio controls src={`${song.proxy ?? "/files/" + song.file}#t=${start}`} style={{ width: "100%" }} />
                {start + needSec > song.duration + 0.05 && <div className="tr-warning">{needSec > song.duration
                  ? `The song is ${(needSec - song.duration).toFixed(1)} s shorter than the template: the last moment will be silent.`
                  : "The song ends before the template does: choose an earlier start."}</div>}
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
          {accent?.selective && (
            <div className="wiz-accent">
              <div className="label">Selective colour (one colour stays, the rest goes black and white)</div>
              <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
                {accent.hue != null && (
                  <label className={`wiz-acc ${accentChoice === "auto" ? "on" : ""}`}>
                    {accent.preview && <img src={accent.preview} alt="" />}
                    <span><input type="radio" checked={accentChoice === "auto"} onChange={() => setAccentChoice("auto")} /> Suggested: <b>{accent.name}</b>
                      <i className="sw" style={{ background: `hsl(${accent.hue} 80% 50%)` }} /> (from shot {(accent.slot ?? 0) + 1})</span>
                  </label>
                )}
                {!sameAccent && (
                  <label className={`wiz-acc ${accentChoice === "keep" ? "on" : ""}`}>
                    {accent.templatePreview && <img src={accent.templatePreview} alt="" />}
                    <span><input type="radio" checked={accentChoice === "keep" || accent.hue == null} onChange={() => setAccentChoice("keep")} /> Keep the template's <b>{accent.templateName}</b>
                      <i className="sw" style={{ background: `hsl(${accent.templateHue ?? 25} 80% 50%)` }} /></span>
                  </label>
                )}
              </div>
              {sameAccent && <div className="faint" style={{ fontSize: 11 }}>Your shots' most striking colour is the same as the template's, so it stays as it is.</div>}
              {accent.hue == null && <div className="faint" style={{ fontSize: 11 }}>
                No striking colour in {accent.shots?.length ? `shot ${accent.shots.map((i) => i + 1).join(", ")} (the selective-colour part)` : "the selective-colour shots"}
                {accent.hint ? `. Your file '${media[accent.hint.mediaId]?.name ?? "?"}' has a strong ${accent.hint.name}: drag it onto that shot in step 1 to use it.` : "; the template's colour is kept, and you can pick another later with the eyedropper."}
              </div>}
            </div>
          )}
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
