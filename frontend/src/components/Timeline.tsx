import { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useApp } from "../store/app";
import { useEditor } from "../store/editor";
import type { Clip, MediaMeta, Track } from "../types";
import { clipDuration, contentEnd, projectDuration, TRACK_COLORS } from "../util";
import { Icon } from "./Icons";
import { MEDIA_DND } from "./MediaLibrary";

const TRACK_ICONS: Record<string, string> = { video: "film", overlay: "layout", text: "text", audio: "volume", music: "music" };

export default function Timeline() {
  const project = useEditor((s) => s.project!);
  const pps = useEditor((s) => s.pxPerSec);
  const selection = useEditor((s) => s.selection);
  const scrollRef = useRef<HTMLDivElement>(null);
  const headsRef = useRef<HTMLDivElement>(null);
  const [viewW, setViewW] = useState(800);
  const [dropTrack, setDropTrack] = useState<string | null>(null);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setViewW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const contentW = Math.max(viewW, (contentEnd(project) + 10) * pps);

  const timeAtClientX = (clientX: number) => {
    const el = scrollRef.current!;
    const rect = el.getBoundingClientRect();
    return Math.max(0, (clientX - rect.left + el.scrollLeft) / pps);
  };

  const scrub = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    const s = useEditor.getState();
    s.setPlaying(false);
    s.setPlayhead(timeAtClientX(e.clientX));
    const target = e.currentTarget as HTMLElement;
    target.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => useEditor.getState().setPlayhead(timeAtClientX(ev.clientX));
    const up = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
  };

  // Ctrl + wheel zooms around the mouse position.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      const s = useEditor.getState();
      const rect = el.getBoundingClientRect();
      const t = (e.clientX - rect.left + el.scrollLeft) / s.pxPerSec;
      s.setZoom(s.pxPerSec * (e.deltaY < 0 ? 1.15 : 1 / 1.15));
      requestAnimationFrame(() => (el.scrollLeft = t * useEditor.getState().pxPerSec - (e.clientX - rect.left)));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const zoomToFit = () => {
    const d = Math.max(5, projectDuration(useEditor.getState().project!));
    useEditor.getState().setZoom((viewW - 40) / d);
    if (scrollRef.current) scrollRef.current.scrollLeft = 0;
  };

  return (
    <div className="timeline">
      <div className="tl-toolbar">
        <span className="label" style={{ marginRight: 8 }}>Timeline</span>
        <span className="faint" style={{ fontSize: 12 }}>
          Drag media onto a track or double-click it in the library. Trim, split and drag-to-move arrive in Phase 2.
        </span>
        <span className="spacer" />
        <button className="btn ghost icon sm" title="Zoom out (−)" onClick={() => useEditor.getState().setZoom(pps / 1.25)}>
          <Icon name="zoomOut" size={15} />
        </button>
        <input
          type="range" min={8} max={400} value={pps} style={{ width: 110 }}
          onChange={(e) => useEditor.getState().setZoom(+e.target.value)} title="Zoom"
        />
        <button className="btn ghost icon sm" title="Zoom in (+)" onClick={() => useEditor.getState().setZoom(pps * 1.25)}>
          <Icon name="zoomIn" size={15} />
        </button>
        <button className="btn ghost sm" onClick={zoomToFit} title="Fit the whole project">Fit</button>
      </div>
      <div className="tl-body">
        <div className="tl-heads" ref={headsRef}>
          <div className="ruler-spacer" />
          {project.tracks.map((t) => (
            <TrackHead key={t.id} t={t} />
          ))}
        </div>
        <div
          className="tl-scroll"
          ref={scrollRef}
          onScroll={(e) => headsRef.current && (headsRef.current.scrollTop = (e.target as HTMLElement).scrollTop)}
        >
          <div style={{ width: contentW, position: "relative", minHeight: "100%" }}>
            <Ruler width={contentW} pps={pps} onPointerDown={scrub} />
            {project.tracks.map((t) => (
              <div
                key={t.id}
                className={`tl-track ${dropTrack === t.id ? "drop" : ""}`}
                onPointerDown={(e) => {
                  if (e.target === e.currentTarget) {
                    useEditor.getState().select({ kind: "none" });
                    scrub(e);
                  }
                }}
                onDragOver={(e) => {
                  if (e.dataTransfer.types.includes(MEDIA_DND)) {
                    e.preventDefault();
                    e.dataTransfer.dropEffect = "copy";
                    setDropTrack(t.id);
                  }
                }}
                onDragLeave={() => setDropTrack(null)}
                onDrop={(e) => {
                  setDropTrack(null);
                  const id = e.dataTransfer.getData(MEDIA_DND);
                  if (!id) return;
                  e.preventDefault();
                  const s = useEditor.getState();
                  if (t.type === "text") {
                    useApp.getState().toast({ kind: "info", title: "Text clips are added from the Text panel (Phase 6)." });
                    return;
                  }
                  const m = s.media[id];
                  const type = m?.type === "audio" && t.type !== "audio" && t.type !== "music" ? "music" : t.type;
                  s.addToTimeline(id, type, Math.round(timeAtClientX(e.clientX) * 10) / 10);
                }}
              >
                {t.clips.map((c) => (
                  <ClipBlock
                    key={c.id}
                    c={c}
                    track={t}
                    pps={pps}
                    selected={selection.kind === "clip" && selection.clipId === c.id}
                  />
                ))}
              </div>
            ))}
            <Playhead pps={pps} />
            {contentEnd(project) === 0 && <div className="tl-hint">The timeline is empty.</div>}
          </div>
        </div>
      </div>
    </div>
  );
}

function TrackHead({ t }: { t: Track }) {
  const toggle = (key: "muted" | "hidden") =>
    useEditor.getState().commit((p) => {
      const tr = p.tracks.find((x) => x.id === t.id);
      if (tr) tr[key] = !tr[key];
    });
  const audible = t.type !== "text";
  const visual = t.type === "video" || t.type === "overlay" || t.type === "text";
  return (
    <div className="tl-head">
      <span className="swatch" style={{ background: TRACK_COLORS[t.type] }} />
      <Icon name={TRACK_ICONS[t.type]} size={14} style={{ color: "var(--text-3)" }} />
      <span className="tname" title={t.name}>{t.name || t.type}</span>
      {visual && (
        <button className="btn ghost icon sm" title={t.hidden ? "Show track" : "Hide track"} onClick={() => toggle("hidden")} style={{ opacity: t.hidden ? 0.4 : 1 }}>
          <Icon name="eye" size={13} />
        </button>
      )}
      {audible && (
        <button className="btn ghost icon sm" title={t.muted ? "Unmute track" : "Mute track"} onClick={() => toggle("muted")}>
          <Icon name={t.muted ? "mute" : "volume"} size={13} style={{ color: t.muted ? "var(--err)" : undefined }} />
        </button>
      )}
    </div>
  );
}

function Ruler({ width, pps, onPointerDown }: { width: number; pps: number; onPointerDown: (e: React.PointerEvent) => void }) {
  // Pick a label interval that keeps labels ~80px apart.
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
  const major = steps.find((s) => s * pps >= 80) ?? 600;
  const minor = major / 5;
  const ticks = [];
  for (let t = 0; t * pps <= width; t += minor) {
    const isMajor = Math.abs(t / major - Math.round(t / major)) < 1e-6;
    ticks.push(
      <div key={t.toFixed(3)} className={`tick ${isMajor ? "major" : "minor"}`} style={{ left: t * pps }} />,
    );
    if (isMajor) {
      const m = Math.floor(t / 60);
      const s = t - m * 60;
      ticks.push(
        <span key={`l${t.toFixed(3)}`} className="tl-label" style={{ left: t * pps }}>
          {m}:{(major < 1 ? s.toFixed(1) : String(Math.round(s))).padStart(major < 1 ? 4 : 2, "0")}
        </span>,
      );
    }
  }
  return (
    <div className="tl-ruler" style={{ width }} onPointerDown={onPointerDown}>
      {ticks}
    </div>
  );
}

function Playhead({ pps }: { pps: number }) {
  const t = useEditor((s) => s.playhead);
  return <div className="playhead" style={{ left: t * pps - 1 }} />;
}

const ClipBlock = memo(function ClipBlock({ c, track, pps, selected }: { c: Clip; track: Track; pps: number; selected: boolean }) {
  const m = useEditor((s) => (c.mediaId ? s.media[c.mediaId] : undefined));
  const w = Math.max(2, clipDuration(c) * pps);
  const visual = m && m.type !== "audio" && (track.type === "video" || track.type === "overlay");
  const showWave = m?.waveform && (track.type === "audio" || track.type === "music" || (m.hasAudio && track.type === "video"));
  return (
    <div
      className={`tl-clip ${selected ? "sel" : ""}`}
      style={{ left: c.start * pps, width: w, ["--c" as string]: TRACK_COLORS[track.type], opacity: track.hidden ? 0.4 : 1 }}
      onPointerDown={(e) => {
        e.stopPropagation();
        useEditor.getState().select({ kind: "clip", trackId: track.id, clipId: c.id });
      }}
      title={m ? `${m.name}\n${c.inPoint.toFixed(2)}s → ${c.outPoint.toFixed(2)}s` : "Missing media"}
    >
      {visual && m?.thumbnail && <div className="cthumb" style={{ backgroundImage: `url(${m.thumbnail})` }} />}
      {showWave && m && <Waveform m={m} c={c} width={w} muted={c.muted || track.muted} />}
      <div className="clabel">{m?.name ?? "Missing media"}</div>
    </div>
  );
});

const waveCache = new Map<string, Promise<{ peaksPerSecond: number; peaks: number[] }>>();
function loadWave(url: string) {
  if (!waveCache.has(url)) {
    waveCache.set(
      url,
      fetch(url).then((r) => {
        if (!r.ok) throw new Error(`waveform ${r.status}`);
        return r.json();
      }),
    );
  }
  return waveCache.get(url)!;
}

function Waveform({ m, c, width, muted }: { m: MediaMeta; c: Clip; width: number; muted: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    let cancelled = false;
    loadWave(m.waveform!)
      .then((wf) => {
        const cv = ref.current;
        if (cancelled || !cv) return;
        const dpr = window.devicePixelRatio || 1;
        const W = Math.min(16000, Math.round(width * dpr));
        const H = Math.round((cv.clientHeight || 22) * dpr);
        cv.width = W;
        cv.height = H;
        const ctx = cv.getContext("2d")!;
        ctx.clearRect(0, 0, W, H);
        ctx.fillStyle = muted ? "rgba(255,255,255,0.25)" : "rgba(255,255,255,0.75)";
        const pps = wf.peaksPerSecond;
        const a = c.inPoint * pps;
        const b = c.outPoint * pps;
        const per = (b - a) / W;
        for (let x = 0; x < W; x++) {
          const i0 = Math.floor(a + x * per);
          const i1 = Math.max(i0 + 1, Math.floor(a + (x + 1) * per));
          let peak = 0;
          for (let i = i0; i < i1 && i < wf.peaks.length; i++) peak = Math.max(peak, wf.peaks[i]);
          const h = Math.max(1, (peak / 255) * H * Math.min(1.5, c.volume));
          ctx.fillRect(x, H - h, 1, h);
        }
      })
      .catch(() => void 0);
    return () => {
      cancelled = true;
    };
  }, [m.waveform, c.inPoint, c.outPoint, c.volume, width, muted]);
  return <canvas ref={ref} />;
}
