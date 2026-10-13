// Safe-area warnings: a note in Properties, a badge + list under the preview, and boxes on the canvas.
import { useEffect, useMemo, useState } from "react";
import { safetyWarnings, faceBoxes, textBoxAt, type SafetyWarning } from "../player/safety";
import { useEditor } from "../store/editor";
import { onTextAssets } from "../text/cache";
import { clipEnd } from "../util";
import { Icon } from "./Icons";

/** Warnings for the whole project (recomputed when it changes or a text layout arrives). */
export function useSafety(): SafetyWarning[] {
  const project = useEditor((s) => s.project);
  const media = useEditor((s) => s.media);
  const [tick, setTick] = useState(0);
  useEffect(() => onTextAssets(() => setTick((n) => n + 1)), []);
  return useMemo(() => (project ? safetyWarnings(project, media) : []), [project, media, tick]);
}

export function SafetyNote({ clipId }: { clipId: string }) {
  const w = useSafety().filter((x) => x.clipId === clipId);
  if (!w.length) return null;
  return (
    <div className="tr-warning" style={{ marginTop: 6 }}>
      {w.map((x, i) => (
        <div key={i} className="row" style={{ gap: 6 }}>
          <Icon name="alert" size={12} />
          <span style={{ flex: 1 }}>{x.label} (at {x.time.toFixed(2)} s)</span>
          <button className="btn ghost xs" onClick={() => useEditor.getState().setPlayhead(x.time)}>Show</button>
        </div>
      ))}
    </div>
  );
}

export function SafetyBadge({ onShow }: { onShow: () => void }) {
  const w = useSafety();
  const [open, setOpen] = useState(false);
  if (!w.length) return null;
  return (
    <span style={{ position: "relative" }}>
      <button className="btn ghost sm safety-badge" onClick={() => setOpen(!open)} title="Text or faces under the Reels interface">
        <Icon name="alert" size={13} /> {w.length} safe-area {w.length === 1 ? "warning" : "warnings"}
      </button>
      {open && (
        <div className="safety-list">
          {w.map((x, i) => (
            <button key={i} className="safety-item" onClick={() => {
              const st = useEditor.getState();
              st.setPlaying(false);
              st.setPlayhead(x.time);
              st.select({ kind: "clip", clipId: x.clipId });
              onShow();
              setOpen(false);
            }}>
              <span className="mono faint">{x.time.toFixed(2)}s</span> {x.label}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}

/** Face and text boxes at the playhead (shown with the safe-area guides); red when in a zone. */
export function SafetyBoxes({ fw }: { fw: number }) {
  const project = useEditor((s) => s.project!);
  const media = useEditor((s) => s.media);
  const t = useEditor((s) => s.playhead);
  const warn = useSafety();
  const k = fw / project.settings.width;
  const main = project.tracks.find((x) => x.type === "video");
  const boxes: { b: [number, number, number, number]; bad: boolean; kind: string }[] = [];
  for (const tr of project.tracks) {
    if (tr.hidden) continue;
    for (const c of tr.clips) {
      if (t < c.start || t >= clipEnd(c)) continue;
      const bad = warn.some((w) => w.clipId === c.id);
      if (tr.type === "text" && c.text) {
        const b = textBoxAt(c, project, t - c.start);
        if (b) boxes.push({ b, bad, kind: "text" });
      } else if ((tr.type === "video" || tr.type === "overlay") && c.mediaId && media[c.mediaId]) {
        for (const b of faceBoxes(c, media[c.mediaId], tr === main, project, t - c.start)) boxes.push({ b, bad, kind: "face" });
      }
    }
  }
  return (
    <>
      {boxes.map((x, i) => (
        <div key={i} className={`safety-box ${x.kind} ${x.bad ? "bad" : ""}`}
          style={{ left: x.b[0] * k, top: x.b[1] * k, width: (x.b[2] - x.b[0]) * k, height: (x.b[3] - x.b[1]) * k }} />
      ))}
    </>
  );
}
