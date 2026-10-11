import { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useApp } from "../store/app";
import { useEditor } from "../store/editor";
import * as ops from "../timeline/ops";
import { cuts as cutPairs, mainTransitionPlans, transitionLabel } from "../timeline/transitions";
import type { Clip, MediaMeta, Track, TrackType } from "../types";
import { clipDuration, clipEnd, contentEnd, TRACK_COLORS } from "../util";
import { Icon } from "./Icons";
import { MEDIA_DND } from "./MediaLibrary";

const TRACK_ICONS: Record<string, string> = { video: "film", overlay: "layout", text: "text", audio: "volume", music: "music" };
const SNAP_PX = 8;
const DRAG_THRESHOLD_PX = 3;
const EDGE_PX = 7;

type DragKind = "move" | "trim-start" | "trim-end";
interface Ghost { trackId: string; start: number; dur: number }

export default function Timeline() {
  const project = useEditor((s) => s.project!);
  const pps = useEditor((s) => s.pxPerSec);
  const selection = useEditor((s) => s.selection);
  const snapping = useEditor((s) => s.snapping);
  const scrollRef = useRef<HTMLDivElement>(null);
  const headsRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const [viewW, setViewW] = useState(800);
  const [dropTrack, setDropTrack] = useState<string | null>(null);
  const [snapAt, setSnapAt] = useState<number | null>(null);
  const [ghost, setGhost] = useState<Ghost | null>(null);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setViewW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const contentW = Math.max(viewW, (contentEnd(project) + 10) * pps);
  const main = project.tracks.find((t) => t.type === "video");

  const timeAtClientX = (clientX: number) => {
    const el = scrollRef.current!;
    const rect = el.getBoundingClientRect();
    return Math.max(0, (clientX - rect.left + el.scrollLeft) / useEditor.getState().pxPerSec);
  };

  const trackAtClientY = (clientY: number): string | null => {
    for (const [id, el] of rowRefs.current) {
      const r = el.getBoundingClientRect();
      if (clientY >= r.top && clientY < r.bottom) return id;
    }
    return null;
  };

  /** Drag helper: calls onMove until the pointer is released. Listens on window (not pointer capture),
   *  because a clip moved to another track is re-mounted and its old element never sees pointerup. */
  const follow = (e: React.PointerEvent, onMove: (ev: PointerEvent) => void, onUp?: (ev: PointerEvent) => void) => {
    const id = e.pointerId;
    const move = (ev: PointerEvent) => ev.pointerId === id && onMove(ev);
    const up = (ev: PointerEvent) => {
      if (ev.pointerId !== id) return;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      window.removeEventListener("blur", cancel);
      onUp?.(ev);
    };
    const cancel = () => up(new PointerEvent("pointercancel", { pointerId: id }));
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    window.addEventListener("blur", cancel);
  };

  const scrub = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    const s = useEditor.getState();
    s.setPlaying(false);
    s.setPlayhead(timeAtClientX(e.clientX));
    follow(e, (ev) => useEditor.getState().setPlayhead(timeAtClientX(ev.clientX)));
  };

  /** Move / trim a clip. The whole drag is a single undo step. */
  const startClipDrag = (e: React.PointerEvent, c: Clip, t: Track) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    const s = useEditor.getState();
    s.select({ kind: "clip", clipId: c.id });
    if (t.locked) return;
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const x = e.clientX - rect.left;
    const edges = rect.width > 3 * EDGE_PX;
    const kind: DragKind = edges && x < EDGE_PX ? "trim-start" : edges && x > rect.width - EDGE_PX ? "trim-end" : "move";
    const x0 = e.clientX;
    const base = s.project!;
    const points = ops.snapPoints(base, s.playhead, c.id, beatPoints(base, s.media, c.id));
    const dur = clipDuration(c);
    let active = false;

    follow(
      e,
      (ev) => {
        const st = useEditor.getState();
        if (!active) {
          if (Math.abs(ev.clientX - x0) < DRAG_THRESHOLD_PX && (kind !== "move" || trackAtClientY(ev.clientY) === t.id)) return;
          active = true;
          st.beginGesture();
        }
        const dt = (ev.clientX - x0) / st.pxPerSec;
        const threshold = st.snapping !== ev.altKey ? SNAP_PX / st.pxPerSec : 0; // Alt inverts snapping
        if (kind === "move") {
          const sn = ops.snap(Math.max(0, c.start + dt), points, threshold, [0, dur]);
          const over = trackAtClientY(ev.clientY);
          const overTrack = base.tracks.find((x) => x.id === over);
          const to = overTrack && !overTrack.locked && ops.compatible(t.type, overTrack.type) ? overTrack.id : t.id;
          setSnapAt(sn.at);
          setGhost({ trackId: to, start: Math.max(0, sn.time), dur });
          st.fromBase((d) => ops.moveClip(d, c.id, Math.max(0, sn.time), to));
        } else if (kind === "trim-start") {
          const sn = ops.snap(c.start + dt, points, threshold);
          setSnapAt(sn.at);
          st.fromBase((d) => ops.trimStart(d, c.id, sn.time, st.mediaDuration));
        } else {
          const sn = ops.snap(clipEnd(c) + dt, points, threshold);
          setSnapAt(sn.at);
          st.fromBase((d) => ops.trimEnd(d, c.id, sn.time, st.mediaDuration));
        }
      },
      () => {
        setSnapAt(null);
        setGhost(null);
        if (active) useEditor.getState().endGesture();
      },
    );
  };

  const startMarkerDrag = (e: React.PointerEvent, id: string, time: number) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    const s = useEditor.getState();
    s.select({ kind: "marker", id });
    s.setPlaying(false);
    s.setPlayhead(time);
    const x0 = e.clientX;
    const points = ops.snapPoints(s.project!, -1, undefined, beatPoints(s.project!, s.media)).filter((p) => p >= 0 && Math.abs(p - time) > 1e-6);
    let active = false;
    follow(
      e,
      (ev) => {
        const st = useEditor.getState();
        if (!active) {
          if (Math.abs(ev.clientX - x0) < DRAG_THRESHOLD_PX) return;
          active = true;
          st.beginGesture();
        }
        const threshold = st.snapping !== ev.altKey ? SNAP_PX / st.pxPerSec : 0;
        const sn = ops.snap(Math.max(0, time + (ev.clientX - x0) / st.pxPerSec), points, threshold);
        setSnapAt(sn.at);
        st.fromBase((d) => ops.moveMarker(d, id, sn.time));
        st.setPlayhead(sn.time);
      },
      () => {
        setSnapAt(null);
        if (active) useEditor.getState().endGesture();
      },
    );
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
    const d = Math.max(5, contentEnd(useEditor.getState().project!));
    useEditor.getState().setZoom((viewW - 40) / d);
    if (scrollRef.current) scrollRef.current.scrollLeft = 0;
  };

  const st = useEditor.getState;
  const hasClipSel = selection.kind === "clip";

  return (
    <div className="timeline">
      <div className="tl-toolbar">
        <button className="btn ghost sm" onClick={() => st().splitAtPlayhead()} title="Split at playhead (S)">
          <Icon name="scissors" size={14} /> Split
        </button>
        <button className="btn ghost icon sm" disabled={!hasClipSel} onClick={() => st().duplicateSelected()} title="Duplicate (Ctrl+D)">
          <Icon name="copy" size={14} />
        </button>
        <button className="btn ghost icon sm" disabled={selection.kind !== "clip" && selection.kind !== "marker"} onClick={() => st().deleteSelectedClip()} title="Delete (Delete)">
          <Icon name="trash" size={14} />
        </button>
        <button className="btn ghost sm" disabled={!hasClipSel} onClick={() => st().deleteSelectedClip(true)} title="Ripple delete: remove and close the gap (Shift+Delete)">
          Ripple delete
        </button>
        <span className="tl-sep" />
        <button className="btn ghost sm" onClick={() => st().addMarkerAtPlayhead()} title="Add marker at playhead (M)">
          <Icon name="flag" size={14} /> Marker
        </button>
        <span className="tl-sep" />
        <button className={`btn ghost sm toggle ${snapping ? "on" : ""}`} onClick={() => st().setSnapping(!snapping)} title="Snapping (N). Hold Alt while dragging to invert.">
          <Icon name="magnet" size={14} /> Snap
        </button>
        {main && (
          <button
            className={`btn ghost sm toggle ${main.magnetic !== false ? "on" : ""}`}
            onClick={() =>
              st().commit((p) => {
                const m = p.tracks.find((t) => t.id === main.id)!;
                m.magnetic = m.magnetic === false;
                if (m.magnetic) ops.pack(m);
              })
            }
            title="Magnetic main track: clips stay packed with no gaps, and dragging reorders them"
          >
            <Icon name="link" size={14} /> Magnetic
          </button>
        )}
        <button className="btn ghost sm" onClick={() => st().addAdjustmentLayer()} title="Add an adjustment layer at the playhead: its filters grade everything below it">
          <Icon name="sparkles" size={14} /> Adjustment
        </button>
        <span className="tl-sep" />
        <AddTrackMenu />
        <span className="spacer" />
        <button className="btn ghost icon sm" title="Zoom out (−)" onClick={() => st().setZoom(pps / 1.25)}>
          <Icon name="zoomOut" size={15} />
        </button>
        <input type="range" min={8} max={400} value={pps} style={{ width: 90 }} onChange={(e) => st().setZoom(+e.target.value)} title="Zoom" />
        <button className="btn ghost icon sm" title="Zoom in (+)" onClick={() => st().setZoom(pps * 1.25)}>
          <Icon name="zoomIn" size={15} />
        </button>
        <button className="btn ghost sm" onClick={zoomToFit} title="Fit the whole project">Fit</button>
      </div>
      <div className="tl-body">
        <div className="tl-heads" ref={headsRef}>
          <div className="ruler-spacer" />
          {project.tracks.map((t) => (
            <TrackHead key={t.id} t={t} canRemove={!(t.type === "video" && project.tracks.filter((x) => x.type === "video").length <= 1)} />
          ))}
        </div>
        <div className="tl-scroll" ref={scrollRef} onScroll={(e) => headsRef.current && (headsRef.current.scrollTop = (e.target as HTMLElement).scrollTop)}>
          <div style={{ width: contentW, position: "relative", minHeight: "100%" }}>
            <Ruler width={contentW} pps={pps} onPointerDown={scrub} onMarkerDown={startMarkerDrag} />
            {project.tracks.map((t) => (
              <div
                key={t.id}
                ref={(el) => void (el ? rowRefs.current.set(t.id, el) : rowRefs.current.delete(t.id))}
                className={`tl-track ${dropTrack === t.id ? "drop" : ""} ${t.locked ? "locked" : ""}`}
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
                  const m = s.media[id];
                  // Audio dropped on a visual track goes to the first music track instead.
                  const target =
                    m?.type === "audio" && t.type !== "audio" && t.type !== "music"
                      ? s.project!.tracks.find((x) => x.type === "music")?.id
                      : t.id;
                  s.addToTimeline(id, target, Math.round(timeAtClientX(e.clientX) * 10) / 10);
                }}
              >
                {t.clips.map((c) => (
                  <ClipBlock
                    key={c.id}
                    c={c}
                    track={t}
                    pps={pps}
                    selected={selection.kind === "clip" && selection.clipId === c.id}
                    onPointerDown={startClipDrag}
                  />
                ))}
                {ghost && ghost.trackId === t.id && <div className="tl-ghost" style={{ left: ghost.start * pps, width: ghost.dur * pps }} />}
                {t.type === "video" && <TransitionBlocks track={t} pps={pps} />}
              </div>
            ))}
            {project.markers.map((m) => (
              <div key={m.id} className="marker-line" style={{ left: m.time * pps, background: m.color }} />
            ))}
            {snapAt != null && <div className="snap-line" style={{ left: snapAt * pps }} />}
            <Playhead pps={pps} />
            {contentEnd(project) === 0 && <div className="tl-hint">The timeline is empty. Drag media here or double-click it in the library.</div>}
          </div>
        </div>
      </div>
    </div>
  );
}

/** Beats of every clip on audio/music tracks, as timeline times (snap targets). */
function beatPoints(p: { tracks: Track[] }, media: Record<string, MediaMeta>, excludeId?: string): number[] {
  const out: number[] = [];
  for (const t of p.tracks) {
    if (t.type !== "audio" && t.type !== "music") continue;
    for (const c of t.clips) if (c.id !== excludeId && c.mediaId) out.push(...ops.clipBeats(c, media[c.mediaId]?.beats));
  }
  return out;
}

/** Transition blocks centred on each cut of the main track, plus "+" buttons on bare cuts. */
function TransitionBlocks({ track, pps }: { track: Track; pps: number }) {
  const project = useEditor((s) => s.project!);
  const media = useEditor((s) => s.media);
  const selected = useEditor((s) => (s.selection.kind === "transition" ? s.selection.id : null));
  const plans = mainTransitionPlans(project, (id) => {
    const m = id ? media[id] : undefined;
    return !m || m.type === "image" ? Infinity : m.duration;
  });
  if (track.locked) return null;
  return (
    <>
      {cutPairs(track.clips).map(([a, b]) => {
        const plan = plans.get(a.id);
        const tr = project.transitions.find((x) => x.fromClipId === a.id && x.toClipId === b.id);
        if (!plan || !tr) {
          return (
            <button
              key={`add-${a.id}`}
              className="tl-addtr"
              style={{ left: b.start * pps - 9 }}
              title="Add a transition here (T)"
              onPointerDown={(e) => e.stopPropagation()}
              onClick={() => useEditor.getState().addTransition(a.id, b.id)}
            >
              +
            </button>
          );
        }
        const w = Math.max(plan.status === "ok" ? 14 : 26, 2 * plan.half * pps);
        return (
          <div
            key={tr.id}
            data-transition-id={tr.id}
            className={`tl-trans ${selected === tr.id ? "sel" : ""} ${plan.status !== "ok" ? "warn" : ""}`}
            style={{ left: plan.cut * pps - w / 2, width: w }}
            title={`${transitionLabel(plan.type)} · ${(2 * plan.half).toFixed(2)} s${plan.message ? `\n⚠ ${plan.message}` : ""}`}
            onPointerDown={(e) => {
              e.stopPropagation();
              useEditor.getState().select({ kind: "transition", id: tr.id });
            }}
          >
            <span>⋈</span>
            {plan.status !== "ok" && <span className="tr-warn">⚠</span>}
          </div>
        );
      })}
    </>
  );
}

function AddTrackMenu() {
  return (
    <select
      className="input"
      style={{ height: 24, fontSize: 12, width: 92 }}
      value=""
      title="Add a track"
      onChange={(e) => {
        const type = e.target.value as Exclude<TrackType, "video">;
        if (type) useEditor.getState().commit((p) => void ops.addTrack(p, type));
      }}
    >
      <option value="">+ Track</option>
      <option value="overlay">Overlay</option>
      <option value="text">Text</option>
      <option value="audio">Audio</option>
      <option value="music">Music</option>
    </select>
  );
}

function TrackHead({ t, canRemove }: { t: Track; canRemove: boolean }) {
  const [editing, setEditing] = useState(false);
  const commit = useEditor((s) => s.commit);
  const toggle = (key: "muted" | "hidden" | "locked") =>
    commit((p) => {
      const tr = p.tracks.find((x) => x.id === t.id);
      if (tr) tr[key] = !tr[key];
    });
  const audible = t.type !== "text";
  const visual = t.type === "video" || t.type === "overlay" || t.type === "text";
  const remove = () => {
    commit((p) => void ops.removeTrack(p, t.id));
    useApp.getState().toast({
      kind: "info",
      title: `Removed track “${t.name}”`,
      detail: t.clips.length ? `${t.clips.length} clip(s) removed with it. Ctrl+Z to undo.` : "Ctrl+Z to undo.",
    });
  };
  return (
    <div className="tl-head">
      <span className="swatch" style={{ background: TRACK_COLORS[t.type] }} />
      <Icon name={TRACK_ICONS[t.type]} size={13} style={{ color: "var(--text-3)", flex: "none" }} />
      {editing ? (
        <input
          className="input"
          autoFocus
          defaultValue={t.name}
          style={{ height: 22, flex: 1, fontSize: 12, minWidth: 0 }}
          onBlur={(e) => {
            const v = e.target.value.trim();
            setEditing(false);
            if (v && v !== t.name) commit((p) => void (p.tracks.find((x) => x.id === t.id)!.name = v));
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            if (e.key === "Escape") setEditing(false);
          }}
        />
      ) : (
        <span className="tname" title={`${t.name} (double-click to rename)`} onDoubleClick={() => setEditing(true)}>
          {t.name || t.type}
        </span>
      )}
      <div className="head-btns">
        {visual && (
          <button className="btn ghost icon xs" title={t.hidden ? "Show track" : "Hide track"} onClick={() => toggle("hidden")} style={{ opacity: t.hidden ? 0.4 : 1 }}>
            <Icon name="eye" size={12} />
          </button>
        )}
        {audible && (
          <button className="btn ghost icon xs" title={t.muted ? "Unmute track" : "Mute track"} onClick={() => toggle("muted")}>
            <Icon name={t.muted ? "mute" : "volume"} size={12} style={{ color: t.muted ? "var(--err)" : undefined }} />
          </button>
        )}
        <button className="btn ghost icon xs" title={t.locked ? "Unlock track" : "Lock track"} onClick={() => toggle("locked")}>
          <Icon name={t.locked ? "lock" : "unlock"} size={12} style={{ color: t.locked ? "var(--warn)" : undefined }} />
        </button>
        {canRemove && (
          <button className="btn ghost icon xs danger" title="Remove track" onClick={remove}>
            <Icon name="x" size={12} />
          </button>
        )}
      </div>
    </div>
  );
}

function Ruler({
  width, pps, onPointerDown, onMarkerDown,
}: { width: number; pps: number; onPointerDown: (e: React.PointerEvent) => void; onMarkerDown: (e: React.PointerEvent, id: string, t: number) => void }) {
  const markers = useEditor((s) => s.project!.markers);
  const selected = useEditor((s) => (s.selection.kind === "marker" ? s.selection.id : null));
  // Pick a label interval that keeps labels ~80px apart.
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
  const major = steps.find((s) => s * pps >= 80) ?? 600;
  const minor = major / 5;
  const ticks = [];
  for (let t = 0; t * pps <= width; t += minor) {
    const isMajor = Math.abs(t / major - Math.round(t / major)) < 1e-6;
    ticks.push(<div key={t.toFixed(3)} className={`tick ${isMajor ? "major" : "minor"}`} style={{ left: t * pps }} />);
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
      {markers.map((m) => (
        <div
          key={m.id}
          className={`marker-flag ${selected === m.id ? "sel" : ""}`}
          data-marker-id={m.id}
          style={{ left: m.time * pps, ["--mc" as string]: m.color }}
          title={`${m.label || "Marker"} at ${m.time.toFixed(2)}s. Drag to move.`}
          onPointerDown={(e) => onMarkerDown(e, m.id, m.time)}
        >
          {m.label && <span>{m.label}</span>}
        </div>
      ))}
    </div>
  );
}

function Playhead({ pps }: { pps: number }) {
  const t = useEditor((s) => s.playhead);
  return <div className="playhead" style={{ left: t * pps - 1 }} />;
}

const ClipBlock = memo(function ClipBlock({
  c, track, pps, selected, onPointerDown,
}: { c: Clip; track: Track; pps: number; selected: boolean; onPointerDown: (e: React.PointerEvent, c: Clip, t: Track) => void }) {
  const m = useEditor((s) => (c.mediaId ? s.media[c.mediaId] : undefined));
  const w = Math.max(2, clipDuration(c) * pps);
  const visual = m && m.type !== "audio" && (track.type === "video" || track.type === "overlay");
  const showWave = m?.waveform && (track.type === "audio" || track.type === "music" || (m.hasAudio && track.type === "video"));
  return (
    <div
      className={`tl-clip ${selected ? "sel" : ""} ${track.locked ? "locked" : ""} ${c.adjustment ? "adj" : ""}`}
      data-clip-id={c.id}
      style={{ left: c.start * pps, width: w, ["--c" as string]: TRACK_COLORS[track.type], opacity: track.hidden ? 0.4 : 1 }}
      onPointerDown={(e) => onPointerDown(e, c, track)}
      title={m ? `${m.name}\n${c.inPoint.toFixed(2)}s → ${c.outPoint.toFixed(2)}s\nDrag to move · drag edges to trim` : "Missing media"}
    >
      {visual && m?.thumbnail && <div className="cthumb" style={{ backgroundImage: `url(${m.thumbnail})` }} />}
      {c.filters?.length > 0 && !c.adjustment && <span className="fx-dot" title={`${c.filters.length} filter(s)`}>fx</span>}
      {showWave && m && <Waveform m={m} c={c} width={w} muted={c.muted || track.muted} />}
      {m?.beats && (track.type === "audio" || track.type === "music") && pps * (60 / (m.bpm || 120)) >= 4 &&
        ops.clipBeats(c, m.beats).map((b) => <div key={b} className="beat" style={{ left: (b - c.start) * pps }} />)}
      <div className="clabel">{c.adjustment ? "Adjustment layer" : m?.name ?? "Missing media"}</div>
      {!track.locked && w > 3 * EDGE_PX && (
        <>
          <div className="edge left" />
          <div className="edge right" />
        </>
      )}
    </div>
  );
});

const waveCache = new Map<string, Promise<{ peaksPerSecond: number; peaks: number[] }>>();
export function loadWave(url: string) {
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
        const H = Math.round((cv.clientHeight || 20) * dpr);
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
