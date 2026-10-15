// Auto-captions: speech on the video/audio tracks (faster-whisper), or song lyrics from a music clip (Phase 13a):
// Auto (Whisper on the song, hallucination guard), Paste lyrics (aligned with stable-ts) or Tap to sync (no AI).
// Lyrics go to a "Lyrics" track in the caption `words` format, so every highlight style works.
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { timelineBeats } from "../cutout/model";
import { canRun, instrumentalAlternatives, musicClips, newTapState, tap, tapLines, undoTap, type LyricsMode, type Source, type TapState } from "../lyrics/model";
import { clipDuration } from "../motion/motion";
import { toastError, useApp } from "../store/app";
import { useEditor } from "../store/editor";
import { mergeSpec, TEXT_PRESETS, type TextSpec } from "../text/anim";
import { segmentWords, type TimedWord } from "../text/edit";
import { Modal, ProgressBar } from "./Common";
import { Icon } from "./Icons";

const STYLES: { id: TextSpec["highlight"]["style"]; label: string }[] = [
  { id: "pop", label: "Karaoke pop (spoken word grows + colour)" },
  { id: "color", label: "Karaoke (spoken word in colour)" },
  { id: "reveal", label: "Reveal words as they are spoken" },
  { id: "revealpop", label: "Reveal + pop" },
  { id: "none", label: "Plain" },
];

interface LyricsResult {
  mode: "auto" | "align"; message: string; instrumental?: boolean; words?: (TimedWord & { p?: number })[];
  lines?: (TimedWord & { p?: number; flag?: boolean })[][]; voice?: { share: number; perSecond: number[] };
  guard?: { lowProbability: number; unreliableSegments: number; repeatedRuns: number }; flagged?: number;
  language?: string; device?: string; model?: string; seconds?: number; mediaName?: string;
}

export default function CaptionsDialog({ onClose }: { onClose: () => void }) {
  const [source, setSource] = useState<Source>("speech");
  const [mode, setMode] = useState<LyricsMode>("auto");
  const [model, setModel] = useState("small");
  const [lyrModel, setLyrModel] = useState("large-v3-turbo");
  const [language, setLanguage] = useState("");
  const [style, setStyle] = useState<TextSpec["highlight"]["style"]>("pop");
  const [color, setColor] = useState("#ffd34d");
  const [text, setText] = useState("");
  const [snapWords, setSnapWords] = useState(false);
  const [snapTaps, setSnapTaps] = useState(true);
  const [jobId, setJobId] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [report, setReport] = useState<string[] | null>(null);
  const [offer, setOffer] = useState(false);
  const [taps, setTaps] = useState<TapState | null>(null);
  const job = useApp((s) => (jobId ? s.jobs[jobId] : undefined));
  const project = useEditor((s) => s.project)!;
  const media = useEditor((s) => s.media);
  const playhead0 = useRef(useEditor.getState().playhead);
  const names = useMemo(() => Object.fromEntries(Object.values(media).map((m) => [m.id, m.name])), [media]);
  const { list, defaultId } = useMemo(() => musicClips(project, names, playhead0.current), [project, names]);
  const [clipId, setClipId] = useState<string | null>(defaultId);
  const clip = list.find((x) => x.clip.id === clipId)?.clip ?? null;
  const beats = useMemo(() => timelineBeats(project, media), [project, media]);

  const preset = TEXT_PRESETS.find((p) => p.id === "caption")!;
  const specFor = () => mergeSpec(preset.style, { highlight: { style, color } }) as Partial<TextSpec>;
  const insert = (lines: TimedWord[][]) =>
    useEditor.getState().insertLyrics(lines, specFor(), preset.position, snapWords ? beats : null, 1);

  useEffect(() => {
    if (!job || (job.status !== "done" && job.status !== "error")) return;
    if (job.status === "error") {
      setResult(`${source === "speech" ? "Captions" : "Lyrics"} failed: ${job.error}`);
      return;
    }
    if (source === "speech") {
      const r = job.result as { words: TimedWord[]; message: string; notes?: string[] };
      if (!r.words.length) {
        setResult(r.message + (r.notes?.length ? ` ${r.notes.join(" ")}` : ""));
        return;
      }
      const n = useEditor.getState().insertCaptions(r.words, specFor(), preset.position);
      useApp.getState().toast({ kind: "success", title: `Added ${n} caption${n === 1 ? "" : "s"}`, detail: `${r.message}${r.notes?.length ? " " + r.notes.join(" ") : ""} Edit any caption's text in Properties; Ctrl+Z removes them.` });
      onClose();
      return;
    }
    const r = job.result as LyricsResult;
    const voice = r.voice ? `voice activity ${(r.voice.share * 100).toFixed(0)} % of the range (estimated on the full mix)` : "";
    if (r.mode === "auto" && r.instrumental) {
      setResult(r.message);
      setOffer(true);
      setReport([`Mode: Auto (${r.model ?? lyrModel}) · ${voice}`, `Dropped by the guard: ${r.guard?.lowProbability ?? 0} low-probability words, ${r.guard?.unreliableSegments ?? 0} unreliable segments, ${r.guard?.repeatedRuns ?? 0} evenly repeated words`]);
      return;
    }
    const lines = r.mode === "auto" ? segmentWords(r.words ?? []) : (r.lines ?? []);
    const n = insert(lines);
    const words = lines.reduce((a, l) => a + l.length, 0);
    const low = r.mode === "auto" ? (r.words ?? []).filter((w) => (w.p ?? 1) < 0.6).length : (r.flagged ?? 0);
    setReport([
      `Mode: ${r.mode === "auto" ? `Auto (${r.model}, ${r.language ?? "?"}, ${r.device?.toUpperCase()})` : "Paste lyrics (aligned)"} · ${r.seconds ?? "?"} s`,
      `${words} words in ${n} caption lines on the Lyrics track${snapWords ? ", snapped to the beats" : ""}`,
      voice,
      r.mode === "auto" ? `${low} low-confidence words (below 0.6) kept; dropped: ${r.guard?.lowProbability ?? 0} low-probability, ${r.guard?.repeatedRuns ?? 0} evenly repeated` : `${low} words could not be aligned: spread between their neighbours (check them)`,
    ].filter(Boolean));
    setResult(null);
  }, [job?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  // tap to sync: Space = next line, Shift+Space = next word, Backspace = undo
  useEffect(() => {
    if (!taps) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === " " || e.code === "Space") {
        e.preventDefault();
        e.stopPropagation();
        const t = useEditor.getState().playhead;
        setTaps((s) => (s ? tap(s, t, e.shiftKey, snapTaps ? beats : null) : s));
      } else if (e.key === "Backspace") {
        e.preventDefault();
        setTaps((s) => (s ? undoTap(s) : s));
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [!!taps, snapTaps, beats]); // eslint-disable-line react-hooks/exhaustive-deps

  const startTapping = () => {
    if (!clip) return;
    setTaps(newTapState(text));
    const st = useEditor.getState();
    st.setPlayhead(clip.start);
    st.setPlaying(true);
  };
  const finishTapping = () => {
    if (!taps || !clip) return;
    useEditor.getState().setPlaying(false);
    const lines = tapLines(taps, clip.start + clipDuration(clip));
    const n = insert(lines);
    setReport([`Mode: Tap to sync${snapTaps ? " (taps snapped to beats within 80 ms)" : ""}`,
      `${lines.reduce((a, l) => a + l.length, 0)} words in ${n} lines on the Lyrics track${snapWords ? ", snapped to the beats" : ""}`,
      `${taps.lines.length - lines.length} line(s) not tapped (left out)`]);
    setTaps(null);
  };

  const start = () => {
    const p = useEditor.getState().project!;
    setResult(null);
    setReport(null);
    setOffer(false);
    if (source === "speech") {
      api.captions(p, model, language || null).then((r) => setJobId(r.jobId), (e) => toastError("Captions could not start", e));
      return;
    }
    if (mode === "tap") return startTapping();
    api.lyrics(p, { clipId, playhead: playhead0.current, mode: mode === "auto" ? "auto" : "align", model: lyrModel, language: language || null, text })
      .then((r) => setJobId(r.jobId), (e) => toastError("Lyrics could not start", e));
  };
  const running = job && (job.status === "running" || job.status === "queued");
  const check = canRun(source, mode, { hasClip: !!clip, text, taps: 1 });
  const label = source === "speech" ? (running ? "Transcribing…" : "Transcribe speech")
    : mode === "auto" ? (running ? "Listening…" : "Find the lyrics") : mode === "paste" ? (running ? "Aligning…" : "Align my lyrics") : "Start tapping";

  return (
    <Modal title="Auto-captions" onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>{running ? "Hide" : "Close"}</button>
          {taps ? <button className="btn primary" onClick={finishTapping}>Done: add the lyrics</button>
            : <button className="btn primary" onClick={start} disabled={!!running || !check.ok} title={check.why}><Icon name="text" size={14} /> {label}</button>}
        </>
      }>
      <div className="props" style={{ gap: 12 }}>
        <div className="prop-row"><label>Source</label>
          <div className="seg" data-testid="cap-source">
            <button className={source === "speech" ? "on" : ""} onClick={() => setSource("speech")}>Speech (video/audio tracks)</button>
            <button className={source === "lyrics" ? "on" : ""} onClick={() => setSource("lyrics")}>Song lyrics (music track)</button>
          </div>
        </div>
        {source === "speech" ? (
          <>
            <div className="muted" style={{ fontSize: 13 }}>
              Speech on the video and audio tracks is transcribed on this computer with faster-whisper (GPU when available, free). Music tracks and sound effects are not used.
              Captions are placed above the Reels caption area and below faces in the middle of the frame.
            </div>
            <div className="prop-row"><label>Accuracy</label>
              <select className="input" value={model} onChange={(e) => setModel(e.target.value)}>
                <option value="tiny">Fastest (tiny)</option><option value="base">Fast (base)</option>
                <option value="small">Good (small, recommended)</option><option value="medium">Best (medium, slower; downloads 1.5 GB)</option>
              </select>
            </div>
          </>
        ) : (
          <>
            <div className="prop-row"><label>Song</label>
              <select className="input" value={clipId ?? ""} onChange={(e) => setClipId(e.target.value || null)} data-testid="lyr-clip">
                {!list.length && <option value="">No song on a music track</option>}
                {list.map((x) => <option key={x.clip.id} value={x.clip.id} disabled={!!x.skip} title={x.skip ?? ""}>{x.label}{x.skip ? " (can't be timed)" : ""}</option>)}
              </select>
            </div>
            <div className="prop-row"><label>Mode</label>
              <div className="seg" data-testid="lyr-mode">
                {([["auto", "Auto"], ["paste", "Paste lyrics"], ["tap", "Tap to sync"]] as [LyricsMode, string][]).map(([k, l]) => (
                  <button key={k} className={mode === k ? "on" : ""} disabled={!!taps} onClick={() => { setMode(k); setOffer(false); }}>{l}</button>
                ))}
              </div>
            </div>
            {mode === "auto" && (
              <>
                <div className="faint" style={{ fontSize: 12 }}>Whisper listens to the song itself (no vocal separation) and keeps only words it is sure about.
                  If it hears no vocals it says so instead of guessing.</div>
                <div className="prop-row"><label>Accuracy</label>
                  <select className="input" value={lyrModel} onChange={(e) => setLyrModel(e.target.value)}>
                    <option value="small">Good (small)</option>
                    <option value="large-v3-turbo">Best (large-v3-turbo; downloads 1.6 GB once)</option>
                  </select>
                </div>
              </>
            )}
            {(mode === "paste" || mode === "tap") && (
              <textarea className="input" rows={6} placeholder="Paste the lyrics: one line per caption" value={text} disabled={!!taps}
                onChange={(e) => setText(e.target.value)} data-testid="lyr-text" style={{ resize: "vertical", fontFamily: "inherit" }} />
            )}
            {mode === "paste" && <div className="faint" style={{ fontSize: 12 }}>Your exact words and line breaks are kept; they are timed to the song (stable-ts, large-v3-turbo). Words it can't place are spread between their neighbours and listed.</div>}
            {mode === "tap" && (
              <>
                <label className="row fl-check"><input type="checkbox" checked={snapTaps} onChange={(e) => setSnapTaps(e.target.checked)} /> Snap each tap to the nearest beat (within 80 ms)</label>
                {taps ? (
                  <div className="cut-status">
                    <b>Tapping: Space = next line, Shift+Space = next word, Backspace = undo.</b>
                    <div style={{ fontSize: 13 }}>{(() => {
                      const last = taps.taps[taps.taps.length - 1];
                      const li = last ? last.line : -1;
                      return <>Now: <i>{li >= 0 ? taps.lines[li].join(" ") : "(tap at the first line)"}</i>{li + 1 < taps.lines.length ? <> · next: {taps.lines[li + 1].join(" ")}</> : null}</>;
                    })()}</div>
                    <div className="faint">{taps.taps.length} taps</div>
                  </div>
                ) : <div className="faint" style={{ fontSize: 12 }}>Start plays the song from the clip's start; tap Space as each line begins. No AI needed.</div>}
              </>
            )}
            {mode !== "tap" && (
              <div className="prop-row"><label>Language</label>
                <select className="input" value={language} onChange={(e) => setLanguage(e.target.value)}>
                  <option value="">Detect automatically</option><option value="en">English</option><option value="hi">Hindi</option>
                  <option value="es">Spanish</option><option value="fr">French</option><option value="de">German</option><option value="pt">Portuguese</option>
                </select>
              </div>
            )}
            <label className="row fl-check" title={beats.length < 2 ? "Detect beats on the music first" : "Each line's words land on consecutive beats (Reveal + pop)"}>
              <input type="checkbox" checked={snapWords} disabled={beats.length < 2} onChange={(e) => setSnapWords(e.target.checked)} /> Snap words to beats
            </label>
          </>
        )}
        <div className="prop-row"><label>Highlight</label>
          <div className="row" style={{ gap: 6 }}>
            <select className="input" style={{ flex: 1 }} value={style} onChange={(e) => setStyle(e.target.value as TextSpec["highlight"]["style"])}>
              {STYLES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
            </select>
            <input type="color" value={color} onChange={(e) => setColor(e.target.value)} />
          </div>
        </div>
        {running && (<><ProgressBar value={job!.progress} /><div className="faint" style={{ fontSize: 12 }}>{job!.message || "Working"}</div></>)}
        {result && <div className="tr-warning">{result}</div>}
        {offer && (
          <div className="row" style={{ gap: 6 }}>
            {instrumentalAlternatives.map((m) => (
              <button key={m} className="btn sm" onClick={() => { setMode(m); setOffer(false); setResult(null); }}>{m === "paste" ? "Paste lyrics instead" : "Tap to sync instead"}</button>
            ))}
          </div>
        )}
        {report && <div className="cut-status" data-testid="lyr-report">{report.map((l, i) => <div key={i} style={{ fontSize: 12 }}>{l}</div>)}</div>}
        {source === "speech" && <div className="faint" style={{ fontSize: 11 }}>The first run downloads the speech model (about 460 MB for "small") into the models/whisper folder.</div>}
      </div>
    </Modal>
  );
}
