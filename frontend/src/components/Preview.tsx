import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { PreviewEngine } from "../player/engine";
import { REELS_SAFE } from "../player/geometry";
import { useEditor } from "../store/editor";
import type { MediaMeta } from "../types";
import { fmtTime, projectDuration } from "../util";
import { Icon } from "./Icons";

/** URL the browser should play for a media item: proxy when ready, else the original. */
export function playableUrl(m: MediaMeta): string {
  if (m.proxy) return m.proxy;
  return "/files/" + m.file;
}

function useBoxSize(ref: React.RefObject<HTMLElement>) {
  const [size, setSize] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setSize({ w: e.contentRect.width, h: e.contentRect.height }));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, [ref]);
  return size;
}

export default function Preview() {
  const selection = useEditor((s) => s.selection);
  const media = useEditor((s) => s.media);
  const source = selection.kind === "media" ? media[selection.id] : null;
  return (
    <div className="panel">
      <div className="panel-head">
        {source ? "Source" : "Program"}
        {source && <span className="faint" style={{ textTransform: "none", fontWeight: 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{source.name}</span>}
        <span className="spacer" />
        {source && (
          <button className="btn sm" onClick={() => useEditor.getState().select({ kind: "none" })} title="Back to the timeline preview">
            Show timeline
          </button>
        )}
      </div>
      {source ? <SourceMonitor m={source} /> : <ProgramMonitor />}
    </div>
  );
}

function SourceMonitor({ m }: { m: MediaMeta }) {
  return (
    <div className="preview">
      <div className="stage">
        {m.type === "image" ? (
          <img src={m.proxy ?? m.thumbnail ?? ""} alt={m.name} style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }} />
        ) : m.type === "audio" ? (
          <div className="preview-empty">
            <Icon name="music" size={40} />
            <audio key={m.id} src={playableUrl(m)} controls style={{ width: 320 }} />
          </div>
        ) : (
          <video key={m.id} src={playableUrl(m)} controls style={{ maxWidth: "100%", maxHeight: "100%", background: "#000" }} />
        )}
        {m.type === "video" && m.proxyStatus !== "ready" && (
          <span className="preview-label">{m.proxyStatus === "error" ? "Proxy failed: playing original" : "Proxy building: playing original"}</span>
        )}
      </div>
      <div className="transport">
        <span className="muted" style={{ fontSize: 12 }}>Double-click the thumbnail or use “Add to timeline” to edit this file.</span>
        <span className="spacer" />
        <button className="btn sm primary" onClick={() => useEditor.getState().addToTimeline(m.id)}>
          <Icon name="plus" size={13} /> Add to timeline
        </button>
      </div>
    </div>
  );
}

function ProgramMonitor() {
  const project = useEditor((s) => s.project!);
  const playhead = useEditor((s) => s.playhead);
  const playing = useEditor((s) => s.playing);
  const rate = useEditor((s) => s.rate);
  const stageRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<PreviewEngine | null>(null);
  const size = useBoxSize(stageRef);
  const [buffering, setBuffering] = useState(false);
  const [safe, setSafe] = useState(() => readFlag("ligxt.safeAreas", false));
  const { width: W, height: H, fps, aspect } = project.settings;
  const duration = projectDuration(project);
  const empty = project.tracks.every((t) => t.clips.length === 0);

  // Frame size that fits the stage at the project's aspect ratio.
  const pad = 24;
  const scale = Math.max(0, Math.min((size.w - pad) / W, (size.h - pad) / H));
  const fw = Math.floor(W * scale);
  const fh = Math.floor(H * scale);

  useEffect(() => {
    if (!canvasRef.current) return;
    const engine = new PreviewEngine(canvasRef.current);
    engine.onBuffering = (b) => setBuffering(b);
    engineRef.current = engine;
    (window as unknown as { __ligxtPreview?: PreviewEngine }).__ligxtPreview = engine;
    return () => {
      engine.destroy();
      engineRef.current = null;
    };
  }, [empty]);

  // Backing store: display size x devicePixelRatio, never above the project resolution.
  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv || !fw || !fh) return;
    const dpr = window.devicePixelRatio || 1;
    const k = Math.min(1, (fw * dpr) / W);
    cv.width = Math.max(2, Math.round(W * k));
    cv.height = Math.max(2, Math.round(H * k));
    engineRef.current?.invalidate();
  }, [fw, fh, W, H, empty]);

  const toggleSafe = () => {
    setSafe(!safe);
    writeFlag("ligxt.safeAreas", !safe);
  };

  const toggleFullscreen = () => {
    const el = stageRef.current;
    if (!el) return;
    if (document.fullscreenElement) document.exitFullscreen();
    else el.requestFullscreen().catch(() => void 0);
  };

  const shuttleLabel = playing && rate !== 1 ? `${rate < 0 ? "◀◀ " : "▶▶ "}${Math.abs(rate)}×` : null;
  const step = (dir: number) => {
    const s = useEditor.getState();
    s.setPlaying(false);
    s.setPlayhead(Math.round((s.playhead + dir / fps) * fps) / fps);
  };

  return (
    <div className="preview">
      <div className="stage" ref={stageRef}>
        {empty ? (
          <div className="preview-empty">
            <Icon name="film" size={36} />
            <div>Import media and double-click it (or drag it) onto the timeline.</div>
          </div>
        ) : (
          <div className="canvas-frame" style={{ width: fw, height: fh }}>
            <canvas ref={canvasRef} style={{ width: fw, height: fh, display: "block" }} />
            {safe && <SafeAreas reels={aspect === "9:16"} />}
            {buffering && <span className="preview-label" style={{ top: 8, left: 8 }}>Loading…</span>}
            {shuttleLabel && <span className="shuttle-badge">{shuttleLabel}</span>}
          </div>
        )}
      </div>
      <input
        className="scrubber"
        type="range"
        min={0}
        max={Math.max(0.001, duration)}
        step={1 / fps}
        value={Math.min(playhead, duration)}
        disabled={empty}
        onPointerDown={() => useEditor.getState().setPlaying(false)}
        onChange={(e) => useEditor.getState().setPlayhead(+e.target.value)}
        title="Scrub"
      />
      <div className="transport">
        <button className="btn ghost icon" title="Previous frame (←)" onClick={() => step(-1)}>
          <Icon name="stepBack" />
        </button>
        <button className="btn icon primary" title="Play / pause (Space). J / K / L to shuttle" onClick={() => useEditor.getState().setPlaying(!playing)} disabled={empty}>
          <Icon name={playing ? "pause" : "play"} />
        </button>
        <button className="btn ghost icon" title="Next frame (→)" onClick={() => step(1)}>
          <Icon name="stepFwd" />
        </button>
        <span className="timecode">
          {fmtTime(playhead, fps)} <span className="faint">/ {fmtTime(duration, fps)}</span>
        </span>
        <span className="spacer" />
        <button className={`btn ghost sm toggle ${safe ? "on" : ""}`} onClick={toggleSafe} title="Show the areas Instagram's buttons and captions cover">
          Safe areas
        </button>
        <span className="faint" style={{ fontSize: 11 }}>{W}×{H}</span>
        <button className="btn ghost icon" title="Fullscreen" onClick={toggleFullscreen}>
          <Icon name="fullscreen" />
        </button>
      </div>
    </div>
  );
}

function SafeAreas({ reels }: { reels: boolean }) {
  if (!reels) {
    return (
      <div className="safe-layer">
        <div className="safe-box" style={{ inset: "5%" }} />
      </div>
    );
  }
  const S = REELS_SAFE;
  return (
    <div className="safe-layer">
      <div className="safe-zone" style={{ top: 0, left: 0, right: 0, height: `${S.top * 100}%` }}>
        <span>Reels header</span>
      </div>
      <div className="safe-zone" style={{ bottom: 0, left: 0, right: 0, height: `${S.bottom * 100}%` }}>
        <span>Caption · username · audio</span>
      </div>
      <div className="safe-zone" style={{ top: `${S.rightFrom * 100}%`, bottom: `${S.bottom * 100}%`, right: 0, width: `${S.right * 100}%` }}>
        <span style={{ writingMode: "vertical-rl" }}>Buttons</span>
      </div>
      <div className="safe-box" style={{ top: `${S.top * 100}%`, bottom: `${S.bottom * 100}%`, left: "4%", right: `${S.right * 100}%` }} />
    </div>
  );
}

function readFlag(key: string, dflt: boolean): boolean {
  try {
    const v = localStorage.getItem(key);
    return v == null ? dflt : v === "1";
  } catch {
    return dflt;
  }
}

function writeFlag(key: string, v: boolean) {
  try {
    localStorage.setItem(key, v ? "1" : "0");
  } catch {
    /* storage unavailable */
  }
}
