// Properties > Motion tracking (video clips): track a face or any object from the playhead (forwards,
// backwards or both), see how sure the tracker was, fix a frame and re-track, attach text / a sticker /
// a blur or pixelate that follows it, and auto-reframe the clip onto it.
import { useEffect, useState } from "react";
import { api } from "../api";
import { toastError, useApp } from "../store/app";
import { useEditor } from "../store/editor";
import { hostOf, retrack, trackClicks, trackFace } from "../tracking/actions";
import { running } from "../tracking/api";
import { addFollower, deleteTrack, renameTrack, tracksOf, type FollowerKind } from "../tracking/edit";
import { LOW_CONF } from "../tracking/follow";
import type { MotionTrack, Reframe } from "../tracking/types";
import * as ops from "../timeline/ops";
import type { Clip, MediaMeta } from "../types";
import { ProgressBar } from "./Common";
import { Icon } from "./Icons";

export function TSlider({ label, value, min, max, step, fmt, onChange, title }: {
  label: string; value: number; min: number; max: number; step: number; fmt?: (v: number) => string;
  onChange: (v: number, final: boolean) => void; title?: string;
}) {
  const st = useEditor.getState;
  return (
    <div className="fl-row" title={title}>
      <label>{label}</label>
      <input type="range" min={min} max={max} step={step} value={value}
        onPointerDown={() => st().beginGesture()} onPointerUp={() => st().endGesture()}
        onChange={(e) => onChange(+e.target.value, false)} />
      <span className="mono faint">{fmt ? fmt(value) : value}</span>
    </div>
  );
}

export function trackSummary(t: MotionTrack): string {
  const n = t.data.length || 1;
  const lost = t.data.filter((e) => e[5] <= 0).length;
  const low = t.data.filter((e) => e[5] > 0 && e[5] < LOW_CONF).length;
  const parts = [`${n} frames`];
  if (low) parts.push(`${Math.round((low / n) * 100)}% uncertain`);
  if (lost) parts.push(`${Math.round((lost / n) * 100)}% lost`);
  return parts.join(" · ");
}

interface Sticker { id: string; name: string; url: string }
let stickerCache: Sticker[] | null = null;

export default function TrackingPanel({ clip, m }: { clip: Clip; m: MediaMeta }) {
  const project = useEditor((s) => s.project)!;
  const ui = useEditor((s) => s.trackUi);
  const jobs = useApp((s) => s.jobs);
  const [stickers, setStickers] = useState<Sticker[] | null>(stickerCache);
  const [showStickers, setShowStickers] = useState(false);
  const setUi = useEditor.getState().setTrackUi;
  const commit = useEditor.getState().commit;
  const tracks = tracksOf(project, clip.mediaId);
  const sel = tracks.find((t) => t.id === ui.trackId) ?? (ui.clipId === clip.id ? undefined : undefined);
  const jobId = clip.mediaId ? running.get(clip.mediaId) : undefined;
  const job = jobId ? jobs[jobId] : undefined;
  const busy = job && (job.status === "queued" || job.status === "running");
  const dir = ui.dir ?? "both";
  const tool = ui.clipId === clip.id ? ui.tool : "none";

  useEffect(() => {
    // show this clip's first track on the preview when the clip is selected
    if (ui.clipId !== clip.id) setUi({ clipId: clip.id, trackId: tracks[0]?.id ?? null, tool: "none", faces: null, fix: null, points: [] });
  }, [clip.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!showStickers || stickers) return;
    fetch("/api/media/stickers/library").then((r) => r.json()).then((j) => { stickerCache = j.stickers; setStickers(j.stickers); })
      .catch((e) => toastError("Stickers", e));
  }, [showStickers, stickers]);

  if (m.type !== "video") return null;
  const attach = async (kind: FollowerKind, sticker?: Sticker) => {
    if (!sel) return;
    try {
      let sm: MediaMeta | undefined;
      if (kind === "sticker" && sticker) {
        sm = Object.values(useEditor.getState().media).find((x) => (x as MediaMeta & { sticker?: string }).sticker === sticker.id && x.status !== "error");
        if (!sm) {
          const r = await fetch(`/api/media/stickers/${sticker.id}/import`, { method: "POST" });
          if (!r.ok) throw new Error(`${r.status}`);
          sm = (await r.json()).media as MediaMeta;
          useEditor.setState({ media: { ...useEditor.getState().media, [sm.id]: sm } });
        }
      }
      let id = "";
      commit((d) => {
        if (sm && !d.media.includes(sm.id)) d.media.push(sm.id);
        const host = ops.findClip(d, clip.id)?.clip;
        if (host) id = addFollower(d, host, sel.id, kind, { sticker: sm });
      });
      if (id) useEditor.getState().select({ kind: "clip", clipId: id });
      setShowStickers(false);
    } catch (e) {
      toastError("Could not add it", e);
    }
  };
  const rf: Reframe = clip.reframe ?? {};
  const setReframe = (patch: Partial<Reframe>, final = true) =>
    commit((d) => {
      const c = ops.findClip(d, clip.id)?.clip;
      if (!c) return;
      c.reframe = { third: "center", smoothing: 0.6, ...(c.reframe ?? {}), ...patch };
      if (patch.enabled && c.fit !== "fill") c.fit = "fill";
    }, final);
  const aspectOff = Math.abs((m.width ?? 1) / (m.height ?? 1) - project.settings.width / project.settings.height) > 0.1;

  return (
    <div className="prop-group tracking">
      <div className="label">Motion tracking</div>
      {tracks.length > 0 && (
        <div className="trk-list">
          {tracks.map((t) => (
            <div key={t.id} className={`trk-row ${sel?.id === t.id ? "on" : ""}`} onClick={() => setUi({ clipId: clip.id, trackId: t.id, fix: null })}>
              <Icon name={t.kind === "face" ? "user" : "target"} size={13} />
              <input className="input trk-name" value={t.name} onClick={(e) => e.stopPropagation()}
                onChange={(e) => commit((d) => renameTrack(d, t.id, e.target.value), false)} />
              <span className="faint trk-sum" title="How sure the tracker was (uncertain and lost frames are marked on the timeline)">{trackSummary(t)}</span>
              <button className="btn ghost xs" title="Delete this track (things that follow it stay where they are)"
                onClick={(e) => { e.stopPropagation(); commit((d) => deleteTrack(d, t.id)); }}>✕</button>
            </div>
          ))}
        </div>
      )}
      <div className="mo-row">
        <label>Track</label>
        <div className="seg" style={{ gridColumn: "span 3" }}>
          <button disabled={!!busy} title="Find the face(s) on this frame and follow one" onClick={() => trackFace(clip)}>Face</button>
          <button disabled={!!busy} className={tool === "draw" ? "on" : ""} title="Draw a box around the object on the preview"
            onClick={() => setUi({ clipId: clip.id, tool: tool === "draw" ? "none" : "draw", fix: null })}>Draw a box</button>
          <button disabled={!!busy} className={tool === "click" ? "on" : ""} title="Click the object on the preview (AI, SAM 2.1): best for objects that change shape"
            onClick={() => setUi({ clipId: clip.id, tool: tool === "click" ? "none" : "click", points: [], fix: null })}>Click (AI)</button>
        </div>
      </div>
      <div className="mo-row">
        <label>From here</label>
        <div className="seg" style={{ gridColumn: "span 3" }}>
          {(["both", "forward", "backward"] as const).map((d) => (
            <button key={d} className={dir === d ? "on" : ""} onClick={() => setUi({ dir: d })}>{d === "both" ? "Both ways" : d === "forward" ? "Forward" : "Backward"}</button>
          ))}
        </div>
      </div>
      {tool === "draw" && <div className="faint" style={{ fontSize: 11 }}>Drag a box around the object on the preview. Tracking starts from the frame at the playhead.</div>}
      {tool === "face" && <div className="faint" style={{ fontSize: 11 }}>Several faces: click the one to follow on the preview.</div>}
      {tool === "click" && (
        <div className="row" style={{ gap: 6, fontSize: 11 }}>
          <span className="faint">Click the object on the preview (Shift+click: not this). {ui.points.length} click(s).</span>
          <span className="spacer" />
          <button className="btn xs" disabled={!ui.points.some((q) => q.label === 1)} onClick={() => trackClicks(clip, ui.points)}>Track it</button>
        </div>
      )}
      {busy && (
        <div className="cut-status">
          <div className="row" style={{ gap: 6 }}>
            <Icon name="loader" size={12} className="spin" />
            <span style={{ fontSize: 12 }}>{job!.message || "Tracking…"} {Math.round((job!.progress ?? 0) * 100)}%</span>
            <span className="spacer" />
            <button className="btn ghost xs" onClick={() => api.cancelJob(job!.id).catch((e) => toastError("Cancel failed", e))}>Cancel</button>
          </div>
          <ProgressBar value={job!.progress ?? 0} />
        </div>
      )}
      {sel && (
        <>
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            <span className="faint" style={{ fontSize: 11 }}>{ui.fix ? "Box moved:" : "Wrong on a frame? Drag the box on the preview, then"}</span>
            <button className="btn xs" disabled={!ui.fix || !!busy} onClick={() => retrack(clip, sel.id, "forward")}>Re-track forward</button>
            <button className="btn xs" disabled={!ui.fix || !!busy} onClick={() => retrack(clip, sel.id, "backward")}>Re-track backward</button>
          </div>
          <div className="mo-row">
            <label>Add</label>
            <div className="seg" style={{ gridColumn: "span 3" }}>
              <button title="Text that follows it" onClick={() => attach("text")}>Text</button>
              <button className={showStickers ? "on" : ""} title="A sticker pinned to it" onClick={() => setShowStickers(!showStickers)}>Sticker</button>
              <button title="Blur that follows it (hide a face or a number plate)" onClick={() => attach("blur")}>Blur</button>
              <button title="Pixelate that follows it" onClick={() => attach("pixelate")}>Pixelate</button>
            </div>
          </div>
          {showStickers && (
            <div className="stk-grid">
              {(stickers ?? []).map((s) => (
                <button key={s.id} className="stk" title={s.name} onClick={() => attach("sticker", s)}><img src={s.url} alt={s.name} /></button>
              ))}
              {!stickers && <span className="faint">Loading…</span>}
            </div>
          )}
        </>
      )}
      <label className="row fl-check" title="Pan across the whole frame so the tracked subject stays in view (e.g. a landscape clip in a 9:16 reel)">
        <input type="checkbox" checked={!!rf.trackId && rf.enabled !== false} disabled={!tracks.length}
          onChange={(e) => setReframe({ enabled: e.target.checked, trackId: rf.trackId ?? sel?.id ?? tracks[0]?.id })} />
        Auto-reframe on a tracked subject{aspectOff ? "" : " (the clip already matches the frame)"}
      </label>
      {!!rf.trackId && rf.enabled !== false && (
        <div className="fl-body">
          <div className="fl-row">
            <label>Subject</label>
            <select className="input" value={rf.trackId} onChange={(e) => setReframe({ trackId: e.target.value })}>
              {tracks.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
            <span />
          </div>
          <div className="fl-row">
            <label>Keep it</label>
            <div className="seg">
              {(["left", "center", "right"] as const).map((k) => (
                <button key={k} className={(rf.third ?? "center") === k ? "on" : ""} onClick={() => setReframe({ third: k })}>
                  {k === "center" ? "Centre" : k === "left" ? "Left third" : "Right third"}
                </button>
              ))}
            </div>
            <span />
          </div>
          <TSlider label="Smooth camera" value={rf.smoothing ?? 0.6} min={0} max={1} step={0.05} fmt={(v) => `${Math.round(v * 100)}%`}
            title="Higher = a calmer virtual camera that ignores small movements" onChange={(v, f) => setReframe({ smoothing: v }, f)} />
          <label className="row fl-check">
            <input type="checkbox" checked={!!rf.vertical} onChange={(e) => setReframe({ vertical: e.target.checked })} /> Follow up and down too (when zoomed in)
          </label>
        </div>
      )}
      {!tracks.length && !busy && <div className="faint" style={{ fontSize: 11 }}>Track a face or an object, then pin text, a sticker or a blur to it, or reframe the clip around it.</div>}
    </div>
  );
}

/** For hostOf() users that only need to know if a clip can be tracked. */
export const canTrack = (clipId: string | null) => {
  const c = hostOf(clipId);
  const m = c?.mediaId ? useEditor.getState().media[c.mediaId] : undefined;
  return m?.type === "video";
};
