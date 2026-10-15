// Auto-captions: speech on the video/audio tracks (faster-whisper), or song lyrics from a music clip (Phase 13a/13b):
// Auto (Whisper on the separated vocals, hallucination guard), Paste lyrics (aligned with stable-ts), Look up synced
// lyrics online (LRCLIB, opt-in) or Tap to sync (no AI); optional translation (M2M100 / MADLAD-400) in three layouts.
// Lyrics go to a "Lyrics" track in the caption `words` format, so every highlight style works.
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { timelineBeats } from "../cutout/model";
import { canRun, instrumentalAlternatives, musicClips, newTapState, presenceCells, presenceSummary, tap, tapLines, undoTap, type LyricsMode, type Source, type TapState } from "../lyrics/model";
import { clipDuration } from "../motion/motion";
import { toastError, useApp } from "../store/app";
import { useEditor } from "../store/editor";
import { mergeSpec, TEXT_PRESETS, type TextSpec } from "../text/anim";
import { segmentWords, type LyricsLayout, type TimedWord } from "../text/edit";
import { Modal, ProgressBar } from "./Common";
import { Icon } from "./Icons";

const STYLES: { id: TextSpec["highlight"]["style"]; label: string }[] = [
  { id: "pop", label: "Karaoke pop (spoken word grows + colour)" },
  { id: "color", label: "Karaoke (spoken word in colour)" },
  { id: "reveal", label: "Reveal words as they are spoken" },
  { id: "revealpop", label: "Reveal + pop" },
  { id: "none", label: "Plain" },
];
const LANGS: [string, string][] = [["en", "English"], ["hi", "Hindi"], ["es", "Spanish"], ["fr", "French"], ["de", "German"], ["pt", "Portuguese"],
  ["it", "Italian"], ["ru", "Russian"], ["ar", "Arabic"], ["ja", "Japanese"], ["ko", "Korean"], ["zh", "Chinese"], ["bn", "Bengali"], ["ta", "Tamil"],
  ["ur", "Urdu"], ["mr", "Marathi"], ["gu", "Gujarati"], ["tr", "Turkish"], ["id", "Indonesian"], ["he", "Hebrew"], ["th", "Thai"]];
const hasDevanagari = (s: string) => /[ऀ-ॿ]/.test(s);

interface Voice { share: number; perSecond: number[]; method?: "stem" | "vad" }
interface LyricsResult {
  mode: "auto" | "align" | "lrc"; message: string; instrumental?: boolean; words?: (TimedWord & { p?: number })[];
  lines?: (TimedWord & { p?: number; flag?: boolean })[][]; voice?: Voice;
  guard?: { lowProbability: number; unreliableSegments: number; repeatedRuns: number }; flagged?: number;
  language?: string; device?: string; model?: string; seconds?: number; mediaName?: string;
  separation?: { separator: string; dereverb: boolean; cached: boolean; device?: string } | null;
  lrc?: { offset: number; method: string; lines: number; lrcTimed: number; artist?: string; title?: string };
}
interface Found { id: number; artist: string; title: string; album: string; duration: number; synced: boolean; instrumental: boolean }

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
  const [trJobId, setTrJobId] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [report, setReport] = useState<string[] | null>(null);
  const [voice, setVoice] = useState<Voice | null>(null);
  const [offer, setOffer] = useState(false);
  const [taps, setTaps] = useState<TapState | null>(null);
  // look up (LRCLIB)
  const [artist, setArtist] = useState("");
  const [title, setTitle] = useState("");
  const [found, setFound] = useState<Found[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [pick, setPick] = useState<number | null>(null);
  const [lrcText, setLrcText] = useState("");
  // translation + script
  const [target, setTarget] = useState("");
  const [trModel, setTrModel] = useState<"good" | "best">("good");
  const [layout, setLayout] = useState<LyricsLayout>("two");
  const [hindiRoman, setHindiRoman] = useState(false);
  const pending = useRef<{ lines: TimedWord[][]; lang: string | null; report: string[] } | null>(null);
  const job = useApp((s) => (jobId ? s.jobs[jobId] : undefined));
  const trJob = useApp((s) => (trJobId ? s.jobs[trJobId] : undefined));
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

  // prefill the look-up fields from the song's tags (local; nothing is sent until Search)
  useEffect(() => {
    if (mode !== "lookup" || !clip?.mediaId || title) return;
    api.lyricsTags(clip.mediaId).then((t) => { setArtist(t.artist); setTitle(t.title); }, () => undefined);
  }, [mode, clip?.mediaId]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Insert the lyrics (romanized when asked), translated first when a target language is chosen. */
  const finish = async (lines: TimedWord[][], lang: string | null, rep: string[]) => {
    let ls = lines;
    if (hindiRoman && lines.some((l) => l.some((w) => hasDevanagari(w.text)))) {
      const flat = lines.flat();
      const r = await api.lyricsRomanize(flat.map((w) => w.text));
      let k = 0;
      ls = lines.map((l) => l.map((w) => ({ ...w, text: r.lines[k++] ?? w.text })));
      rep = [...rep, "Hindi lyrics written in Latin letters"];
    }
    if (target && target !== (lang ?? "")) {
      pending.current = { lines: ls, lang, report: rep };
      const p = await api.lyricsTranslate({ lines: lines.map((l) => l.map((w) => w.text).join(" ")), source: lang || null, target, model: trModel,
        script: target === "hi" && hindiRoman ? "roman" : "native" });
      setTrJobId(p.jobId);
      return;
    }
    const n = useEditor.getState().insertLyrics(ls, specFor(), preset.position, snapWords ? beats : null, 1);
    setReport([...rep, `${n} caption lines on the Lyrics track${snapWords ? ", snapped to the beats" : ""}`]);
  };

  useEffect(() => {
    if (!trJob || (trJob.status !== "done" && trJob.status !== "error") || !pending.current) return;
    const pend = pending.current;
    pending.current = null;
    if (trJob.status === "error") {
      setResult(`Translation failed: ${trJob.error}. The lyrics were not added; try again or turn translation off.`);
      return;
    }
    const r = trJob.result as { lines: string[]; source: string; target: string; model: string; modelName: string };
    const n = useEditor.getState().insertLyrics(pend.lines, specFor(), preset.position, snapWords ? beats : null, 1,
      { lines: r.lines, from: r.source, to: r.target, model: r.model, script: r.target === "hi" && hindiRoman ? "roman" : "native" }, layout);
    const lay = layout === "two" ? "two lines (translation below, smaller)" : layout === "translation" ? "translation only" : "original only";
    setReport([...pend.report, `Translated ${r.source} -> ${r.target} with ${r.modelName}; layout: ${lay}`,
      `${n} caption lines on the Lyrics track${layout === "two" ? " + the same lines on \"Lyrics (translation)\"" : ""}. Edit a translated line in Properties (Re-translate there too).`]);
  }, [trJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps

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
    setVoice(r.voice ?? null);
    const how = r.voice?.method === "stem" ? "vocal presence (separated vocals)" : "voice activity (estimated on the full mix)";
    const vline = r.voice ? `${how}: ${(r.voice.share * 100).toFixed(0)} %, ${presenceSummary(r.voice.perSecond)}` : "";
    const sep = r.separation ? `Vocals separated with Mel-Band RoFormer${r.separation.dereverb ? " + DeEcho-DeReverb" : ""}${r.separation.cached ? " (cached)" : ""}`
      : "No vocal separation (off in Settings, or the AI tools are missing)";
    if (r.mode === "auto" && (r.instrumental || !(r.words ?? []).length)) {
      setResult(r.message);
      setOffer(true);
      setReport([`Mode: Auto (${r.model ?? lyrModel})`, sep, vline, `Dropped by the guard: ${r.guard?.lowProbability ?? 0} low-probability words, ${r.guard?.unreliableSegments ?? 0} unreliable segments, ${r.guard?.repeatedRuns ?? 0} evenly repeated words`].filter(Boolean));
      return;
    }
    const lines = r.mode === "auto" ? segmentWords(r.words ?? []) : (r.lines ?? []);
    const words = lines.reduce((a, l) => a + l.length, 0);
    const low = r.mode === "auto" ? (r.words ?? []).filter((w) => (w.p ?? 1) < 0.6).length : (r.flagged ?? 0);
    const rep = [
      `Mode: ${r.mode === "auto" ? `Auto (${r.model}, ${r.language ?? "?"}, ${r.device?.toUpperCase()})` : r.mode === "lrc" ? "Synced lyrics (LRCLIB / LRC)" : "Paste lyrics (aligned)"} · ${r.seconds ?? "?"} s`,
      sep, vline, `${words} words in ${lines.length} lines`,
      r.mode === "auto" ? `${low} low-confidence words (below 0.6) kept; dropped: ${r.guard?.lowProbability ?? 0} low-probability, ${r.guard?.repeatedRuns ?? 0} evenly repeated`
        : `${low} words could not be aligned: ${r.mode === "lrc" ? "they keep the lyrics' own line timing" : "spread between their neighbours"} (check them)`,
      r.lrc ? `Placed in the song: the clip starts ${r.lrc.offset >= 0 ? "+" : ""}${r.lrc.offset.toFixed(1)} s into the lyrics' timing (${r.lrc.method})` : "",
    ].filter(Boolean);
    finish(lines, r.language ?? (language || null), rep).catch((e) => toastError("Could not add the lyrics", e));
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
    const rep = [`Mode: Tap to sync${snapTaps ? " (taps snapped to beats within 80 ms)" : ""}`,
      `${lines.reduce((a, l) => a + l.length, 0)} words in ${lines.length} lines; ${taps.lines.length - lines.length} line(s) not tapped (left out)`];
    setTaps(null);
    finish(lines, language || null, rep).catch((e) => toastError("Could not add the lyrics", e));
  };

  const search = () => {
    setSearching(true);
    setFound(null);
    setPick(null);
    api.lyricsSearch({ title, artist }).then((r) => { setFound(r.results); setPick(r.results.find((x) => x.synced)?.id ?? null); },
      (e) => toastError("Search failed", e)).finally(() => setSearching(false));
  };

  const start = () => {
    const p = useEditor.getState().project!;
    setResult(null);
    setReport(null);
    setVoice(null);
    setOffer(false);
    if (source === "speech") {
      api.captions(p, model, language || null).then((r) => setJobId(r.jobId), (e) => toastError("Captions could not start", e));
      return;
    }
    if (mode === "tap") return startTapping();
    const m = mode === "auto" ? "auto" : mode === "paste" ? "align" : "lrc";
    api.lyrics(p, { clipId, playhead: playhead0.current, mode: m, model: lyrModel, language: language || null, text,
      lrclibId: mode === "lookup" && !lrcText.trim() ? pick : null, lrc: mode === "lookup" ? lrcText : undefined })
      .then((r) => setJobId(r.jobId), (e) => toastError("Lyrics could not start", e));
  };
  const running = (job && (job.status === "running" || job.status === "queued")) || (trJob && (trJob.status === "running" || trJob.status === "queued"));
  const activeJob = trJob && (trJob.status === "running" || trJob.status === "queued") ? trJob : job;
  const check = canRun(source, mode, { hasClip: !!clip, text, taps: 1, lookupId: pick, lrc: lrcText });
  const label = source === "speech" ? (running ? "Transcribing…" : "Transcribe speech")
    : mode === "auto" ? (running ? "Listening…" : "Find the lyrics") : mode === "paste" ? (running ? "Aligning…" : "Align my lyrics")
      : mode === "lookup" ? (running ? "Timing…" : "Use these lyrics") : "Start tapping";

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
                {([["auto", "Auto"], ["paste", "Paste lyrics"], ["lookup", "Look up"], ["tap", "Tap to sync"]] as [LyricsMode, string][]).map(([k, l]) => (
                  <button key={k} className={mode === k ? "on" : ""} disabled={!!taps} onClick={() => { setMode(k); setOffer(false); }}>{l}</button>
                ))}
              </div>
            </div>
            {mode === "auto" && (
              <div className="faint" style={{ fontSize: 12 }}>The vocals are separated from the music first (Mel-Band RoFormer; Settings &gt; Vocal separator), then Whisper listens
                to them and keeps only words it is sure about. If it hears no vocals it says so instead of guessing.</div>
            )}
            {mode !== "tap" && (
              <div className="prop-row"><label>Accuracy</label>
                <select className="input" value={lyrModel} onChange={(e) => setLyrModel(e.target.value)} data-testid="lyr-accuracy">
                  {mode === "auto" && <option value="small">Good (small)</option>}
                  <option value="large-v3-turbo">Best (large-v3-turbo; downloads 1.6 GB once)</option>
                  <option value="large-v3">Best for non-English (large-v3; slower, downloads 3.1 GB once)</option>
                </select>
              </div>
            )}
            {(mode === "paste" || mode === "tap") && (
              <textarea className="input" rows={6} placeholder="Paste the lyrics: one line per caption" value={text} disabled={!!taps}
                onChange={(e) => setText(e.target.value)} data-testid="lyr-text" style={{ resize: "vertical", fontFamily: "inherit" }} />
            )}
            {mode === "paste" && <div className="faint" style={{ fontSize: 12 }}>Your exact words and line breaks are kept; they are timed to the separated vocals (stable-ts). Words it can't place are spread between their neighbours and listed.</div>}
            {mode === "lookup" && (
              <div className="cut-status" data-testid="lyr-lookup">
                <div style={{ fontSize: 12 }}><b>Needs internet.</b> Only the artist and title are sent to lrclib.net (a free synced-lyrics database). The lyrics end up only in this project's text clips.</div>
                <div className="row" style={{ gap: 6 }}>
                  <input className="input" placeholder="Artist" value={artist} onChange={(e) => setArtist(e.target.value)} style={{ flex: 1 }} />
                  <input className="input" placeholder="Song title" value={title} onChange={(e) => setTitle(e.target.value)} style={{ flex: 1 }} />
                  <button className="btn sm" disabled={!title.trim() || searching} onClick={search}>{searching ? "Searching…" : "Search"}</button>
                </div>
                {found && !found.length && <div className="faint" style={{ fontSize: 12 }}>Nothing found. Check the spelling, or paste LRC text below.</div>}
                {found && found.length > 0 && (
                  <div style={{ maxHeight: 140, overflow: "auto" }}>
                    {found.map((f) => (
                      <label key={f.id} className="row fl-check" style={{ fontSize: 12, opacity: f.synced ? 1 : 0.55 }}>
                        <input type="radio" name="lrc" checked={pick === f.id} disabled={!f.synced} onChange={() => setPick(f.id)} />
                        {f.artist} – {f.title}{f.album ? ` (${f.album})` : ""} · {Math.round(f.duration)} s{f.synced ? "" : f.instrumental ? " · instrumental" : " · not synced"}
                      </label>
                    ))}
                  </div>
                )}
                <details><summary className="faint" style={{ fontSize: 12 }}>Or paste LRC text ([mm:ss.xx] lines)</summary>
                  <textarea className="input" rows={4} value={lrcText} onChange={(e) => setLrcText(e.target.value)} data-testid="lyr-lrc" style={{ resize: "vertical", width: "100%" }} />
                </details>
                <div className="faint" style={{ fontSize: 12 }}>The clip's place in the song is found from the vocals, then each word is timed to the separated vocals.</div>
              </div>
            )}
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
                  <option value="">Detect automatically</option>
                  {LANGS.map(([k, n]) => <option key={k} value={k}>{n}</option>)}
                </select>
              </div>
            )}
            <label className="row fl-check" title="Hindi lyrics in Latin letters (romanized), e.g. for viewers who don't read Devanagari">
              <input type="checkbox" checked={hindiRoman} onChange={(e) => setHindiRoman(e.target.checked)} data-testid="lyr-roman" /> Write Hindi in Latin letters (romanized)
            </label>
            <div className="prop-row"><label>Translate</label>
              <div className="row" style={{ gap: 6, flex: 1 }}>
                <select className="input" value={target} onChange={(e) => setTarget(e.target.value)} data-testid="lyr-target" style={{ flex: 1 }}>
                  <option value="">No translation</option>
                  {LANGS.map(([k, n]) => <option key={k} value={k}>to {n}</option>)}
                </select>
                {target && (
                  <select className="input" value={trModel} onChange={(e) => setTrModel(e.target.value as "good" | "best")} title="Translation model (runs on this computer)">
                    <option value="good">Good (M2M100 1.2B)</option><option value="best">Best (MADLAD-400 3B)</option>
                  </select>
                )}
              </div>
            </div>
            {target && (
              <div className="prop-row"><label>Show</label>
                <div className="seg" data-testid="lyr-layout">
                  {([["original", "Original"], ["translation", "Translation"], ["two", "Both (two lines)"]] as [LyricsLayout, string][]).map(([k, l]) => (
                    <button key={k} className={layout === k ? "on" : ""} onClick={() => setLayout(k)}>{l}</button>
                  ))}
                </div>
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
        {running && activeJob && (<><ProgressBar value={activeJob.progress} /><div className="faint" style={{ fontSize: 12 }}>{activeJob.message || "Working"}</div></>)}
        {result && <div className="tr-warning">{result}</div>}
        {offer && (
          <div className="row" style={{ gap: 6 }}>
            {instrumentalAlternatives.map((m) => (
              <button key={m} className="btn sm" onClick={() => { setMode(m); setOffer(false); setResult(null); }}>{m === "paste" ? "Paste lyrics instead" : "Tap to sync instead"}</button>
            ))}
          </div>
        )}
        {voice && clip && voice.perSecond.length > 0 && (
          <div data-testid="lyr-presence" title="Where the vocals are (per second of the song clip)">
            <div className="faint" style={{ fontSize: 11 }}>Vocals per second ({voice.method === "stem" ? "separated vocals" : "estimated on the full mix"})</div>
            <div style={{ display: "flex", gap: 1, height: 14 }}>
              {presenceCells(voice.perSecond, clip).map((c, i) => (
                <div key={i} title={`${c.t0.toFixed(1)}-${c.t1.toFixed(1)} s: ${(c.p * 100).toFixed(0)} %`} data-level={c.level}
                  style={{ flex: 1, borderRadius: 2, background: c.level === "none" ? "var(--line, #333)" : `rgba(255, 211, 77, ${0.3 + 0.7 * c.p})` }} />
              ))}
            </div>
          </div>
        )}
        {report && <div className="cut-status" data-testid="lyr-report">{report.map((l, i) => <div key={i} style={{ fontSize: 12 }}>{l}</div>)}</div>}
        {source === "speech" && <div className="faint" style={{ fontSize: 11 }}>The first run downloads the speech model (about 460 MB for "small") into the models/whisper folder.</div>}
      </div>
    </Modal>
  );
}
