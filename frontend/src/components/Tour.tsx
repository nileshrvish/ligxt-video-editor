// First-run tour of the editor's main panels (skippable; replay from the shortcuts dialog).
import { useLayoutEffect, useState } from "react";

const KEY = "ligxt.tour.done";
const STEPS: { sel: string; title: string; text: string }[] = [
  { sel: '[data-tour="media"]', title: "Media", text: "Drop videos, photos and music here (or click Import). Double-click a file, or drag it, to put it on the timeline." },
  { sel: '[data-tour="preview"]', title: "Preview", text: "Plays your edit exactly as it will export. Space plays/pauses, J/K/L shuttle. Drag a selected clip or text here to move it; Safe areas shows the Reels UI." },
  { sel: '[data-tour="timeline"]', title: "Timeline", text: "Clips on tracks: drag to move, drag edges to trim, S splits at the playhead. + Text, CC Captions, + SFX and transitions (the + between clips) live here." },
  { sel: '[data-tour="properties"]', title: "Properties", text: "Everything about the selected clip: motion and keyframes, speed, colour and effects, audio, text style. With nothing selected: the project's aspect ratio." },
  { sel: '[data-tour="export"]', title: "Export", text: "Presets for Instagram, Shorts, TikTok and more, with a size estimate and a cover image. Save as template (next to it) reuses this edit's style." },
];

export function resetTour() {
  try { localStorage.removeItem(KEY); } catch { /* ignore */ }
}

export default function Tour() {
  const [i, setI] = useState<number>(() => {
    try { return localStorage.getItem(KEY) ? -1 : 0; } catch { return -1; }
  });
  const [rect, setRect] = useState<DOMRect | null>(null);
  useLayoutEffect(() => {
    if (i < 0) return;
    const find = () => setRect(document.querySelector(STEPS[i].sel)?.getBoundingClientRect() ?? null);
    find();
    const t = setTimeout(find, 300);
    window.addEventListener("resize", find);
    return () => { clearTimeout(t); window.removeEventListener("resize", find); };
  }, [i]);
  if (i < 0) return null;
  const done = () => { try { localStorage.setItem(KEY, "1"); } catch { /* ignore */ } setI(-1); };
  const s = STEPS[i];
  const r = rect;
  const W = window.innerWidth, H = window.innerHeight;
  const cardW = 320;
  let left = r ? r.left + r.width / 2 - cardW / 2 : W / 2 - cardW / 2;
  let top = r ? (r.bottom + 170 < H ? r.bottom + 10 : Math.max(10, r.top - 170)) : H / 2 - 80;
  if (r && r.height > H * 0.5) { top = r.top + 40; left = r.right + cardW + 20 < W ? r.right + 10 : r.left - cardW - 10; }
  left = Math.max(10, Math.min(W - cardW - 10, left));
  return (
    <div className="tour">
      {r && <div className="tour-spot" style={{ left: r.left - 4, top: r.top - 4, width: r.width + 8, height: r.height + 8 }} />}
      <div className="tour-card" style={{ left, top, width: cardW }}>
        <div className="faint" style={{ fontSize: 11 }}>{i + 1} / {STEPS.length}</div>
        <b>{s.title}</b>
        <p>{s.text}</p>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn ghost sm" onClick={done}>Skip tour</button>
          <span className="spacer" />
          {i > 0 && <button className="btn sm" onClick={() => setI(i - 1)}>Back</button>}
          <button className="btn primary sm" onClick={() => (i + 1 < STEPS.length ? setI(i + 1) : done())}>{i + 1 < STEPS.length ? "Next" : "Start editing"}</button>
        </div>
      </div>
    </div>
  );
}

/** Start the tour again (from the shortcuts dialog). */
export function replayTour() {
  resetTour();
  window.location.reload();
}
