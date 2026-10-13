// Auto-captions: transcribe the project's speech with faster-whisper (local, GPU) and add caption
// clips with word timings on a "Captions" track.
import { useEffect, useState } from "react";
import { api } from "../api";
import { toastError, useApp } from "../store/app";
import { useEditor } from "../store/editor";
import { mergeSpec, TEXT_PRESETS, type TextSpec } from "../text/anim";
import { Modal, ProgressBar } from "./Common";
import { Icon } from "./Icons";

const STYLES: { id: TextSpec["highlight"]["style"]; label: string }[] = [
  { id: "pop", label: "Karaoke pop (spoken word grows + colour)" },
  { id: "color", label: "Karaoke (spoken word in colour)" },
  { id: "reveal", label: "Reveal words as they are spoken" },
  { id: "none", label: "Plain" },
];

export default function CaptionsDialog({ onClose }: { onClose: () => void }) {
  const [model, setModel] = useState("small");
  const [language, setLanguage] = useState("");
  const [style, setStyle] = useState<TextSpec["highlight"]["style"]>("pop");
  const [color, setColor] = useState("#ffd34d");
  const [jobId, setJobId] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const job = useApp((s) => (jobId ? s.jobs[jobId] : undefined));

  useEffect(() => {
    if (!job || (job.status !== "done" && job.status !== "error")) return;
    if (job.status === "error") {
      setResult(`Captions failed: ${job.error}`);
      return;
    }
    const r = job.result as { words: { text: string; start: number; end: number }[]; message: string; notes?: string[] };
    if (!r.words.length) {
      setResult(r.message + (r.notes?.length ? ` ${r.notes.join(" ")}` : ""));
      return;
    }
    const preset = TEXT_PRESETS.find((p) => p.id === "caption")!;
    const s = mergeSpec(preset.style, { highlight: { style, color } }) as Partial<TextSpec>;
    const n = useEditor.getState().insertCaptions(r.words, s, preset.position);
    useApp.getState().toast({ kind: "success", title: `Added ${n} caption${n === 1 ? "" : "s"}`, detail: `${r.message}${r.notes?.length ? " " + r.notes.join(" ") : ""} Edit any caption's text in Properties; Ctrl+Z removes them.` });
    onClose();
  }, [job?.status]);

  const start = () => {
    const p = useEditor.getState().project!;
    setResult(null);
    api.captions(p, model, language || null).then((r) => setJobId(r.jobId), (e) => toastError("Captions could not start", e));
  };
  const running = job && (job.status === "running" || job.status === "queued");

  return (
    <Modal title="Auto-captions" onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>{running ? "Hide" : "Close"}</button>
          <button className="btn primary" onClick={start} disabled={!!running}><Icon name="text" size={14} /> {running ? "Transcribing…" : "Transcribe speech"}</button>
        </>
      }>
      <div className="props" style={{ gap: 12 }}>
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
        <div className="prop-row"><label>Language</label>
          <select className="input" value={language} onChange={(e) => setLanguage(e.target.value)}>
            <option value="">Detect automatically</option><option value="en">English</option><option value="hi">Hindi</option>
            <option value="es">Spanish</option><option value="fr">French</option><option value="de">German</option><option value="pt">Portuguese</option>
          </select>
        </div>
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
        <div className="faint" style={{ fontSize: 11 }}>The first run downloads the speech model (about 460 MB for "small") into the models/whisper folder.</div>
      </div>
    </Modal>
  );
}
