import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { PreviewEngine } from "../player/engine";
import { REELS_SAFE } from "../player/geometry";
import { useEditor } from "../store/editor";
import type { MediaMeta } from "../types";
import { fmtTime, projectDuration } from "../util";
import { Icon } from "./Icons";
import TransformHandles from "./TransformHandles";
import TextEditBox from "./TextEditBox";
import { SafetyBadge, SafetyBoxes } from "./SafeAreas";

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
    <div className="panel" data-tour="preview">
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
  const [scopes, setScopes] = useState(() => readFlag("ligxt.scopes", false));
  const compare = useEditor((s) => s.compare);
  const colorPick = useEditor((s) => s.colorPick);
  const editingText = useEditor((s) => s.editingText);
  useEffect(() => {
    const e = engineRef.current;
    if (e) {
      e.editingTextId = editingText;
      e.invalidate();
    }
  }, [editingText]);
  const pick = (e: React.MouseEvent) => {
    const cv = canvasRef.current;
    const fn = useEditor.getState().colorPick;
    if (!cv || !fn) return;
    const r = cv.getBoundingClientRect();
    const x = Math.floor(((e.clientX - r.left) / r.width) * cv.width), y = Math.floor(((e.clientY - r.top) / r.height) * cv.height);
    // Average a 5x5 patch so noise or grain doesn't decide the colour.
    const d = cv.getContext("2d")!.getImageData(Math.max(0, x - 2), Math.max(0, y - 2), 5, 5).data;
    const rgb: [number, number, number] = [0, 0, 0];
    for (let i = 0; i < d.length; i += 4) for (let c = 0; c < 3; c++) rgb[c] += d[i + c] / (d.length / 4);
    useEditor.getState().setColorPick(null);
    fn(rgb);
  };
  const toggleScopes = () => {
    setScopes(!scopes);
    writeFlag("ligxt.scopes", !scopes);
  };
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
            {safe && <SafetyBoxes fw={fw} />}
            {buffering && <span className="preview-label" style={{ top: 8, left: 8 }}>Loading…</span>}
            {shuttleLabel && <span className="shuttle-badge">{shuttleLabel}</span>}
            {compare != null && <CompareDivider value={compare} width={fw} />}
            {compare == null && !colorPick && !editingText && <TransformHandles fw={fw} fh={fh} />}
            {editingText && <TextEditBox fw={fw} />}
            {colorPick && <div className="pv-pick" title="Click a colour to keep" onClick={pick} />}
          </div>
        )}
      </div>
      {scopes && !empty && <Scopes canvasRef={canvasRef} />}
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
        <button className={`btn ghost sm toggle ${compare != null ? "on" : ""}`} title="Before / after: drag the divider to compare the graded and ungraded picture (\\)"
          onClick={() => useEditor.getState().setCompare(compare == null ? 0.5 : null)}>
          Before/After
        </button>
        <button className={`btn ghost sm toggle ${scopes ? "on" : ""}`} title="Colour scopes: histogram and waveform" onClick={toggleScopes}>
          Scopes
        </button>
        <SafetyBadge onShow={() => { if (!safe) toggleSafe(); }} />
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

function CompareDivider({ value, width }: { value: number; width: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const drag = (e: React.PointerEvent) => {
    e.preventDefault();
    const frame = ref.current?.parentElement;
    if (!frame) return;
    const move = (ev: PointerEvent) => {
      const r = frame.getBoundingClientRect();
      useEditor.getState().setCompare(Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)));
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    move(e.nativeEvent);
  };
  return (
    <div ref={ref} className="cmp" onPointerDown={drag} style={{ left: value * width }}>
      <span className="cmp-label l">Before</span>
      <span className="cmp-label r">After</span>
      <span className="cmp-handle">⇆</span>
    </div>
  );
}

/** Histogram (R, G, B, luma) and luma waveform of the graded preview, refreshed ~6 times a second. */
function Scopes({ canvasRef }: { canvasRef: React.RefObject<HTMLCanvasElement> }) {
  const hRef = useRef<HTMLCanvasElement>(null);
  const wRef = useRef<HTMLCanvasElement>(null);
  const [stats, setStats] = useState({ dark: 0, clip: 0 });
  useEffect(() => {
    const tmp = document.createElement("canvas");
    const id = window.setInterval(() => {
      const src = canvasRef.current, hc = hRef.current, wc = wRef.current;
      if (!src || !hc || !wc || !src.width) return;
      const w = 96, h = Math.max(2, Math.round((96 * src.height) / src.width));
      tmp.width = w;
      tmp.height = h;
      const tx = tmp.getContext("2d", { willReadFrequently: true })!;
      tx.drawImage(src, 0, 0, w, h);
      const d = tx.getImageData(0, 0, w, h).data;
      const bins = [new Float32Array(64), new Float32Array(64), new Float32Array(64), new Float32Array(64)];
      const wave = new Float32Array(w * 64);
      let dark = 0, clip = 0;
      for (let i = 0, px = 0; i < d.length; i += 4, px++) {
        const y = 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
        bins[0][d[i] >> 2]++; bins[1][d[i + 1] >> 2]++; bins[2][d[i + 2] >> 2]++; bins[3][Math.min(63, y >> 2)]++;
        wave[(px % w) * 64 + Math.min(63, y >> 2)]++;
        if (y < 8) dark++;
        if (d[i] > 250 || d[i + 1] > 250 || d[i + 2] > 250) clip++;
      }
      const n = w * h;
      setStats({ dark: dark / n, clip: clip / n });
      const hx = hc.getContext("2d")!;
      hx.clearRect(0, 0, hc.width, hc.height);
      hx.globalCompositeOperation = "lighter";
      const max = Math.max(...bins.slice(0, 3).map((b) => Math.max(...b))) || 1;
      ["rgba(255,70,70,0.55)", "rgba(70,255,90,0.55)", "rgba(80,120,255,0.6)"].forEach((col, c) => {
        hx.fillStyle = col;
        hx.beginPath();
        hx.moveTo(0, hc.height);
        for (let b = 0; b < 64; b++) hx.lineTo((b / 63) * hc.width, hc.height - (bins[c][b] / max) * (hc.height - 2));
        hx.lineTo(hc.width, hc.height);
        hx.fill();
      });
      hx.globalCompositeOperation = "source-over";
      const wx = wc.getContext("2d")!;
      const img = wx.createImageData(w, 64);
      for (let x = 0; x < w; x++) for (let b = 0; b < 64; b++) {
        const v = Math.min(255, wave[x * 64 + b] * 40);
        const o = ((63 - b) * w + x) * 4;
        img.data[o] = v * 0.6; img.data[o + 1] = v; img.data[o + 2] = v * 0.7; img.data[o + 3] = 255;
      }
      wx.putImageData(img, 0, 0);
    }, 160);
    return () => window.clearInterval(id);
  }, [canvasRef]);
  return (
    <div className="scopes">
      <div className="scope"><canvas ref={hRef} width={160} height={56} /><span>Histogram</span></div>
      <div className="scope"><canvas ref={wRef} width={96} height={64} style={{ width: 120, height: 56, imageRendering: "pixelated" }} /><span>Waveform</span></div>
      <div className="scope-stats">
        <span className={stats.dark > 0.2 ? "warn" : ""}>Crushed blacks {Math.round(stats.dark * 100)}%</span>
        <span className={stats.clip > 0.05 ? "warn" : ""}>Clipped highlights {Math.round(stats.clip * 100)}%</span>
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
