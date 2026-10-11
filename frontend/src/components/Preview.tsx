import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { clipAtTime, useEditor } from "../store/editor";
import type { Clip, FitMode, MediaMeta } from "../types";
import { clipEnd, fmtTime, projectDuration } from "../util";
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
  const media = useEditor((s) => s.media);
  const playhead = useEditor((s) => s.playhead);
  const playing = useEditor((s) => s.playing);
  const stageRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const size = useBoxSize(stageRef);
  const { width: W, height: H, fps } = project.settings;
  const duration = projectDuration(project);

  const hit = clipAtTime(project, playhead);
  const clip = hit?.clip ?? null;
  const m = clip?.mediaId ? media[clip.mediaId] : undefined;
  const mainTrack = project.tracks.find((t) => t.type === "video");
  const fit: FitMode = clip?.fit ?? project.settings.background.mode;

  // Frame size that fits the stage at the project's aspect ratio.
  const pad = 24;
  const scale = Math.min((size.w - pad) / W, (size.h - pad) / H);
  const fw = Math.max(0, Math.floor(W * scale));
  const fh = Math.max(0, Math.floor(H * scale));

  const srcTime = (c: Clip, t: number) => c.inPoint + (t - c.start) * (c.speed || 1);

  // Load / seek the video element when the clip under the playhead changes or when scrubbing.
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !clip || !m || m.type !== "video") return;
    const url = playableUrl(m);
    if (v.dataset.src !== url) {
      v.dataset.src = url;
      v.src = url;
    }
    const target = srcTime(clip, useEditor.getState().playhead);
    if (!useEditor.getState().playing || Math.abs(v.currentTime - target) > 0.25) v.currentTime = target;
    v.playbackRate = clip.speed || 1;
    v.volume = Math.max(0, Math.min(1, clip.volume * (mainTrack?.volume ?? 1)));
    v.muted = clip.muted || !!mainTrack?.muted;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clip?.id, m?.proxy, clip?.inPoint, clip?.speed, clip?.volume, clip?.muted, playing ? 0 : playhead]);

  // Play/pause the element alongside the transport.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    if (playing && m?.type === "video") v.play().catch(() => void 0);
    else v.pause();
  }, [playing, clip?.id, m?.type]);

  // Playback clock: follow the video element when one is playing, else advance by wall time.
  useEffect(() => {
    if (!playing) return;
    const s0 = useEditor.getState();
    if (s0.project && s0.playhead >= projectDuration(s0.project) - 1e-3) s0.setPlayhead(0);
    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = (now - last) / 1000;
      last = now;
      const s = useEditor.getState();
      if (!s.project) return;
      const total = projectDuration(s.project);
      const h = clipAtTime(s.project, s.playhead);
      const mm = h?.clip.mediaId ? s.media[h.clip.mediaId] : undefined;
      const v = videoRef.current;
      let t = s.playhead;
      if (h && mm?.type === "video" && v) {
        if (v.readyState >= 2 && !v.seeking) {
          t = h.clip.start + (v.currentTime - h.clip.inPoint) / (h.clip.speed || 1);
          if (t < s.playhead - 0.3 || v.ended) t = s.playhead + dt; // element not caught up yet
          if (t >= clipEnd(h.clip) - 0.5 / fps || v.currentTime >= h.clip.outPoint - 0.01) t = clipEnd(h.clip);
        }
      } else {
        t = s.playhead + dt;
      }
      if (t >= total) {
        s.setPlayhead(total);
        s.setPlaying(false);
        return;
      }
      s.setPlayhead(t);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, fps]);

  const toggleFullscreen = () => {
    const el = stageRef.current;
    if (!el) return;
    if (document.fullscreenElement) document.exitFullscreen();
    else el.requestFullscreen().catch(() => void 0);
  };

  const objectFit = fit === "fill" ? "cover" : "contain";
  const empty = !mainTrack || mainTrack.clips.length === 0;

  return (
    <div className="preview">
      <div className="stage" ref={stageRef}>
        {empty ? (
          <div className="preview-empty">
            <Icon name="film" size={36} />
            <div>Import media and double-click it (or drag it) onto the timeline.</div>
          </div>
        ) : (
          <div className="canvas-frame" style={{ width: fw, height: fh, background: project.settings.background.color }}>
            {m && fit === "blur" && m.thumbnail && <img className="blur-bg" src={m.proxy && m.type === "image" ? m.proxy : m.thumbnail} alt="" />}
            <video
              ref={videoRef}
              playsInline
              preload="auto"
              style={{ objectFit, visibility: m?.type === "video" ? "visible" : "hidden" }}
            />
            {m?.type === "image" && <img src={m.proxy ?? m.thumbnail ?? ""} alt={m.name} style={{ objectFit }} />}
          </div>
        )}
      </div>
      <div className="transport">
        <button
          className="btn ghost icon"
          title="Previous frame (←)"
          onClick={() => {
            const s = useEditor.getState();
            s.setPlaying(false);
            s.setPlayhead(s.playhead - 1 / fps);
          }}
        >
          <Icon name="stepBack" />
        </button>
        <button className="btn icon primary" title="Play / pause (Space)" onClick={() => useEditor.getState().setPlaying(!playing)} disabled={empty}>
          <Icon name={playing ? "pause" : "play"} />
        </button>
        <button
          className="btn ghost icon"
          title="Next frame (→)"
          onClick={() => {
            const s = useEditor.getState();
            s.setPlaying(false);
            s.setPlayhead(s.playhead + 1 / fps);
          }}
        >
          <Icon name="stepFwd" />
        </button>
        <span className="timecode">
          {fmtTime(playhead, fps)} <span className="faint">/ {fmtTime(duration, fps)}</span>
        </span>
        <span className="spacer" />
        <span className="faint" style={{ fontSize: 11 }}>{W}×{H} · {fps} fps</span>
        <button className="btn ghost icon" title="Fullscreen" onClick={toggleFullscreen}>
          <Icon name="fullscreen" />
        </button>
      </div>
    </div>
  );
}
