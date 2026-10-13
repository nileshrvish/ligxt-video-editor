import { useEffect, useState } from "react";
import { api } from "../api";
import { toastError, useApp } from "../store/app";
import { useEditor, useSelectedClip } from "../store/editor";
import * as ops from "../timeline/ops";
import { mainTransitionPlans, TRANSITION_GROUPS, transitionLabel, UI_MIN } from "../timeline/transitions";
import type { Aspect, Clip, FitMode, MediaMeta } from "../types";
import { ASPECTS, clipDuration, fmtBytes, fmtDuration } from "../util";
import FiltersPanel from "./FiltersPanel";
import TextPanel from "./TextPanel";
import { SafetyNote } from "./SafeAreas";
import MotionPanel, { PropSlider, useClipTime, useClipUpdate } from "./MotionPanel";
import { Icon } from "./Icons";

export default function Properties() {
  const sel = useEditor((s) => s.selection);
  const media = useEditor((s) => s.media);
  const selClip = useSelectedClip();
  return (
    <div className="panel">
      <div className="panel-head">Properties</div>
      <div className="panel-body">
        {sel.kind === "transition" ? (
          <TransitionProps id={sel.id} />
        ) : sel.kind === "marker" ? (
          <MarkerProps id={sel.id} />
        ) : selClip ? (
          <ClipProps clip={selClip.clip} trackId={selClip.trackId} trackType={selClip.trackType} />
        ) : sel.kind === "media" && media[sel.id] ? (
          <MediaProps m={media[sel.id]} />
        ) : (
          <ProjectProps />
        )}
      </div>
    </div>
  );
}

/** Number input that commits on blur/Enter, so typing doesn't create an undo step per keystroke. */
function NumberField({
  value, onCommit, min, max, step = 0.01, suffix,
}: { value: number; onCommit: (v: number) => void; min?: number; max?: number; step?: number; suffix?: string }) {
  const [text, setText] = useState(String(+value.toFixed(3)));
  useEffect(() => setText(String(+value.toFixed(3))), [value]);
  const commit = () => {
    let v = parseFloat(text);
    if (!isFinite(v)) return setText(String(+value.toFixed(3)));
    if (min != null) v = Math.max(min, v);
    if (max != null) v = Math.min(max, v);
    if (Math.abs(v - value) > 1e-6) onCommit(v);
    else setText(String(+value.toFixed(3)));
  };
  return (
    <div className="row" style={{ gap: 4 }}>
      <input
        className="input"
        type="number"
        step={step}
        min={min}
        max={max}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      />
      {suffix && <span className="faint" style={{ fontSize: 11 }}>{suffix}</span>}
    </div>
  );
}

function ClipProps({ clip, trackId, trackType }: { clip: Clip; trackId: string; trackType: string }) {
  const m = useEditor((s) => (clip.mediaId ? s.media[clip.mediaId] : undefined));
  const locked = useEditor((s) => !!s.project!.tracks.find((t) => t.id === trackId)?.locked);
  const update = (fn: (c: Clip) => void) =>
    useEditor.getState().commit((p) => {
      const c = p.tracks.find((t) => t.id === trackId)?.clips.find((c) => c.id === clip.id);
      if (!c) return;
      fn(c);
      // Typed values follow the same rules as dragging (no overlaps, magnetic packing).
      ops.settle(p, c.id);
    });
  const maxOut = m && m.type !== "image" ? m.duration : 3600;
  const isVisual = trackType === "video" || trackType === "overlay";
  if (clip.adjustment) return <AdjustmentProps clip={clip} trackId={trackId} />;
  if (clip.text && trackType === "text") return <TextPanel clip={clip} />;

  return (
    <div className="props">
      <div>
        <div className="label">Clip · {trackType} track</div>
        <h3 title={m?.name}>{m?.name ?? "Missing media"}</h3>
        <div className="faint" style={{ fontSize: 12 }}>
          {fmtDuration(clipDuration(clip))} on timeline
        </div>
        {locked && <div style={{ color: "var(--warn)", fontSize: 12 }}>This track is locked. Unlock it to edit.</div>}
      </div>

      <div className="prop-group">
        <div className="label">Timing</div>
        <div className="prop-row">
          <label>Start at</label>
          <NumberField
            value={clip.start}
            min={0}
            suffix="s"
            onCommit={(v) => useEditor.getState().commit((p) => ops.moveClip(p, clip.id, v))}
          />
        </div>
        <div className="prop-row">
          <label>In point</label>
          <NumberField value={clip.inPoint} min={0} max={clip.outPoint - 0.05} suffix="s" onCommit={(v) => update((c) => void (c.inPoint = v))} />
        </div>
        <div className="prop-row">
          <label>Out point</label>
          <NumberField
            value={clip.outPoint}
            min={clip.inPoint + 0.05}
            max={maxOut}
            suffix="s"
            onCommit={(v) => update((c) => void (c.outPoint = v))}
          />
        </div>
        {m?.type === "image" && <div className="faint" style={{ fontSize: 11 }}>Photos: the out point sets how long the photo shows.</div>}
      </div>

      {(trackType === "audio" || trackType === "music") && m && <BeatsSection clip={clip} m={m} />}

      {isVisual && (
        <div className="prop-group">
          <div className="label">Frame</div>
          <div className="prop-row">
            <label>Fit</label>
            <div className="seg">
              {([null, "fit", "fill", "blur"] as (FitMode | null)[]).map((f) => (
                <button key={String(f)} className={clip.fit === f ? "on" : ""} onClick={() => update((c) => void (c.fit = f))}>
                  {f === null ? "Default" : f === "blur" ? "Blur bg" : f[0].toUpperCase() + f.slice(1)}
                </button>
              ))}
            </div>
          </div>
          {m?.type === "image" && (
            <label className="row fl-check" title="Fill around the photo with a blurred, colour-matched extension, so it can be moved or zoomed out without showing its edges">
              <input type="checkbox" checked={!!clip.extend} onChange={(e) => update((c) => void (c.extend = e.target.checked))} />
              Extend canvas (blurred, colour-matched fill around the photo)
            </label>
          )}
          <SafetyNote clipId={clip.id} />
        </div>
      )}

      {isVisual && m && m.type !== "audio" && <MotionPanel clip={clip} trackType={trackType} m={m} />}

      {(m?.hasAudio ?? false) && <AudioSection clip={clip} trackType={trackType} m={m!} />}

      {isVisual && m && m.type !== "audio" && <FiltersPanel clip={clip} trackId={trackId} />}

      <div className="prop-group">
        <button className="btn danger" onClick={() => useEditor.getState().deleteSelectedClip()}>
          <Icon name="trash" size={14} /> Delete clip
        </button>
      </div>
    </div>
  );
}

function AudioSection({ clip, trackType, m }: { clip: Clip; trackType: string; m: MediaMeta }) {
  const update = useClipUpdate(clip.id);
  const tau = useClipTime(clip);
  const project = useEditor((s) => s.project!);
  const track = project.tracks.find((t) => t.clips.some((c) => c.id === clip.id));
  const detached = project.tracks.some((t) => t.clips.some((c) => c.detachedFrom === clip.id));
  const canDetach = (trackType === "video" || trackType === "overlay") && m.type === "video" && !detached;
  return (
    <div className="prop-group mo">
      <div className="label">Audio{m.sfx ? " · sound effect" : ""}</div>
      <PropSlider clip={clip} prop="volume" label="Volume" tau={tau} update={update} min={0} max={200} step={1} scale={100} fmt={(v) => `${Math.round(v * 100)}%`} />
      <div className="prop-row">
        <label>Mute</label>
        <label className="row" style={{ cursor: "pointer" }}>
          <input type="checkbox" checked={clip.muted} onChange={(e) => update((c) => void (c.muted = e.target.checked))} />
          <span className="muted">Mute this clip</span>
        </label>
      </div>
      <div className="prop-row">
        <label>Fade in</label>
        <NumberField value={clip.fadeIn} min={0} max={10} step={0.1} suffix="s" onCommit={(v) => update((c) => void (c.fadeIn = v))} />
      </div>
      <div className="prop-row">
        <label>Fade out</label>
        <NumberField value={clip.fadeOut} min={0} max={10} step={0.1} suffix="s" onCommit={(v) => update((c) => void (c.fadeOut = v))} />
      </div>
      {canDetach && (
        <button className="btn sm" title="Move the sound to its own clip on an audio track (the video clip is muted)"
          onClick={() => useEditor.getState().detachAudio(clip.id)}>Detach audio</button>
      )}
      {detached && <div className="faint" style={{ fontSize: 11 }}>Audio detached to an audio track.</div>}
      {track?.type === "music" && (
        <label className="row fl-check" title="Lower the music while there is speech on the video and audio tracks">
          <input type="checkbox" checked={!!track.autoDuck?.enabled}
            onChange={(e) => useEditor.getState().commit((p) => {
              const t = p.tracks.find((x) => x.id === track.id)!;
              t.autoDuck = { enabled: e.target.checked, amount: t.autoDuck?.amount ?? -12 };
            })} />
          Auto-duck under speech ({track.autoDuck?.amount ?? -12} dB)
        </label>
      )}
    </div>
  );
}

function BeatsSection({ clip, m }: { clip: Clip; m: MediaMeta }) {
  const job = useApp((s) =>
    Object.values(s.jobs).find((j) => j.kind === "beats" && j.ref.mediaId === m.id && (j.status === "running" || j.status === "queued")),
  );
  const detect = () =>
    api.detectBeats(m.id).catch((e) => toastError("Beat detection could not start", e));
  const beats = ops.clipBeats(clip, m.beats);
  const addMarkers = (every: number) => {
    const picks = beats.filter((_, i) => i % every === 0);
    let added = 0;
    useEditor.getState().commit((p) => {
      for (const b of picks) if (ops.addMarker(p, b, every === 4 ? "Bar" : "")) added++;
    });
    useApp.getState().toast({ kind: "success", title: `Added ${added} marker${added === 1 ? "" : "s"}` });
  };
  return (
    <div className="prop-group">
      <div className="label">Beats</div>
      {m.beats ? (
        <>
          <div className="muted" style={{ fontSize: 12 }}>
            {m.bpm} BPM · {beats.length} beats in this clip. Clip edges, markers and the playhead snap to beats.
          </div>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            <span className="faint" style={{ fontSize: 12 }}>Markers on:</span>
            <button className="btn sm" onClick={() => addMarkers(1)}>Every beat</button>
            <button className="btn sm" onClick={() => addMarkers(2)}>Every 2nd</button>
            <button className="btn sm" onClick={() => addMarkers(4)}>Every bar (4)</button>
          </div>
          <button className="btn sm ghost" onClick={detect} disabled={!!job}>Detect again</button>
        </>
      ) : (
        <button className="btn" onClick={detect} disabled={!!job}>
          {job ? <><Icon name="loader" size={14} className="spin" /> Detecting beats…</> : <><Icon name="music" size={14} /> Detect beats</>}
        </button>
      )}
    </div>
  );
}

function AdjustmentProps({ clip, trackId }: { clip: Clip; trackId: string }) {
  const st = useEditor.getState;
  const update2 = useClipUpdate(clip.id);
  const tau = useClipTime(clip);
  const update = (fn: (c: Clip) => void, undoable = true) =>
    st().commit((p) => {
      const c = p.tracks.find((t) => t.id === trackId)?.clips.find((x) => x.id === clip.id);
      if (!c) return;
      fn(c);
      ops.settle(p, c.id);
    }, undoable);
  return (
    <div className="props">
      <div>
        <div className="label">Adjustment layer</div>
        <h3>Grades everything below it</h3>
        <div className="faint" style={{ fontSize: 12 }}>Its filters apply to the main track and the overlays under this track for as long as it lasts. Stretch it across many clips to grade them all at once.</div>
      </div>
      <div className="prop-group">
        <div className="prop-row">
          <label>Start at</label>
          <NumberField value={clip.start} min={0} suffix="s" onCommit={(v) => st().commit((p) => ops.moveClip(p, clip.id, v))} />
        </div>
        <div className="prop-row">
          <label>Length</label>
          <NumberField value={clip.outPoint - clip.inPoint} min={0.1} suffix="s" onCommit={(v) => update((c) => void (c.outPoint = c.inPoint + v))} />
        </div>
        <div className="prop-row">
          <label>Strength</label>
          <div className="slider-row">
            <input type="range" min={0} max={1} step={0.01} value={clip.transform.opacity}
              onPointerDown={() => st().beginGesture()} onPointerUp={() => st().endGesture()}
              onChange={(e) => update((c) => void (c.transform.opacity = +e.target.value), false)} />
            <span className="mono faint" style={{ fontSize: 11 }}>{Math.round(clip.transform.opacity * 100)}%</span>
          </div>
        </div>
        <div className="mo">
          <PropSlider clip={clip} prop="intensity" label="Animate" tau={tau} update={update2} min={0} max={100} step={1} scale={100} fmt={(v) => `${Math.round(v * 100)}%`} />
          <div className="faint" style={{ fontSize: 11 }}>Set ◆ keyframes here to fade the grade in or out over time (multiplies the strength).</div>
        </div>
      </div>
      <FiltersPanel clip={clip} trackId={trackId} />
      <div className="prop-group">
        <button className="btn danger" onClick={() => st().deleteSelectedClip()}><Icon name="trash" size={14} /> Delete adjustment layer</button>
      </div>
    </div>
  );
}

function TransitionProps({ id }: { id: string }) {
  const project = useEditor((s) => s.project!);
  const media = useEditor((s) => s.media);
  const tr = project.transitions.find((t) => t.id === id);
  if (!tr) return null;
  const plan = mainTransitionPlans(project, (mid) => {
    const m = mid ? media[mid] : undefined;
    return !m || m.type === "image" ? Infinity : m.duration;
  }).get(tr.fromClipId);
  const st = useEditor.getState;
  const set = (patch: { type?: string; duration?: number }, undoable = true) => {
    st().commit((p) => void ops.setTransition(p, tr.fromClipId, tr.toClipId, patch), undoable);
    const cur = st().project!.transitions.find((t) => t.id === id);
    if (cur) st().setLastTransition({ type: cur.type, duration: cur.duration });
  };
  const spare = (v: number, side: "after its out point" | "before its in point") =>
    !isFinite(v) ? "unlimited (photo)" : `${v.toFixed(2)} s spare ${side}`;
  const preview = () => {
    if (!plan) return;
    st().setPlaying(false);
    st().setPlayhead(Math.max(0, plan.cut - plan.half - 0.6));
    setTimeout(() => st().setPlaying(true), 50);
  };
  return (
    <div className="props">
      <div>
        <div className="label">Transition</div>
        <h3>{transitionLabel(tr.type)}</h3>
        <div className="faint" style={{ fontSize: 12 }}>Centred on the cut; uses footage beyond each clip's in/out points, so the timeline length doesn't change.</div>
      </div>
      <div className="prop-group">
        {TRANSITION_GROUPS.map((g) => (
          <div key={g.label}>
            <div className="faint" style={{ fontSize: 11, marginBottom: 4 }}>{g.label}</div>
            <div className="tr-grid">
              {g.types.map((t) => (
                <button key={t.id} className={`btn sm ${tr.type === t.id ? "toggle on" : ""}`} onClick={() => set({ type: t.id })}>
                  {t.label}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div className="prop-group">
        <div className="prop-row">
          <label>Duration</label>
          <div className="slider-row">
            <input
              type="range" min={UI_MIN} max={2} step={0.05} value={tr.duration}
              onPointerDown={() => st().beginGesture()}
              onPointerUp={() => st().endGesture()}
              onChange={(e) => set({ duration: +e.target.value }, false)}
            />
            <span className="mono faint" style={{ fontSize: 11 }}>{tr.duration.toFixed(2)} s</span>
          </div>
        </div>
        {plan && (
          <dl className="kv">
            <dt>Actual length</dt><dd>{(2 * plan.half).toFixed(2)} s</dd>
            <dt>Left clip</dt><dd>{spare(plan.spareA, "after its out point")}</dd>
            <dt>Right clip</dt><dd>{spare(plan.spareB, "before its in point")}</dd>
          </dl>
        )}
        {plan?.message && (
          <div className="tr-warning">
            <Icon name="alert" size={13} /> {plan.message}
          </div>
        )}
        {!plan && <div className="error-text" style={{ fontSize: 12 }}>These clips are no longer next to each other, so this transition is inactive.</div>}
      </div>
      <div className="prop-group">
        <button className="btn" onClick={preview} disabled={!plan}><Icon name="play" size={13} /> Preview</button>
        <button
          className="btn"
          onClick={() => {
            let n = 0;
            st().commit((p) => void (n = ops.applyTransitionToAll(p, tr.trackId, tr.type, tr.duration)));
            useApp.getState().toast({ kind: "success", title: `${transitionLabel(tr.type)} applied to ${n} cut${n === 1 ? "" : "s"}` });
          }}
        >
          Apply to all cuts
        </button>
        <button className="btn danger" onClick={() => st().deleteSelectedClip()}>
          <Icon name="trash" size={14} /> Remove transition
        </button>
      </div>
    </div>
  );
}

function MarkerProps({ id }: { id: string }) {
  const marker = useEditor((s) => s.project!.markers.find((m) => m.id === id));
  const commit = useEditor((s) => s.commit);
  if (!marker) return null;
  const COLORS = ["#f5c542", "#ff5c8a", "#3ccf91", "#4ab3ff", "#a597ff"];
  return (
    <div className="props">
      <div>
        <div className="label">Marker</div>
        <h3>{marker.label || "Untitled marker"}</h3>
      </div>
      <div className="prop-group">
        <div className="prop-row">
          <label>Label</label>
          <input
            className="input"
            defaultValue={marker.label}
            key={marker.id}
            placeholder="e.g. Drop, Beat 1"
            onBlur={(e) => e.target.value !== marker.label && commit((p) => void (p.markers.find((m) => m.id === id)!.label = e.target.value))}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          />
        </div>
        <div className="prop-row">
          <label>Time</label>
          <NumberField value={marker.time} min={0} suffix="s" onCommit={(v) => commit((p) => ops.moveMarker(p, id, v))} />
        </div>
        <div className="prop-row">
          <label>Color</label>
          <div className="row" style={{ gap: 6 }}>
            {COLORS.map((c) => (
              <button
                key={c}
                title={c}
                onClick={() => commit((p) => void (p.markers.find((m) => m.id === id)!.color = c))}
                style={{ width: 20, height: 20, borderRadius: 10, background: c, border: marker.color === c ? "2px solid #fff" : "1px solid var(--line-strong)", cursor: "pointer" }}
              />
            ))}
          </div>
        </div>
      </div>
      <div className="prop-group">
        <button className="btn" onClick={() => useEditor.getState().setPlayhead(marker.time)}>Go to marker</button>
        <button className="btn danger" onClick={() => useEditor.getState().deleteSelectedClip()}>
          <Icon name="trash" size={14} /> Delete marker
        </button>
        <div className="faint" style={{ fontSize: 11 }}>Clip edges snap to markers while you drag.</div>
      </div>
    </div>
  );
}

function MediaProps({ m }: { m: MediaMeta }) {
  const toast = useApp((s) => s.toast);
  const used = useEditor((s) => s.project!.tracks.reduce((n, t) => n + t.clips.filter((c) => c.mediaId === m.id).length, 0));
  return (
    <div className="props">
      <div>
        <div className="label">{m.type}</div>
        <h3 title={m.name}>{m.name}</h3>
      </div>
      {m.thumbnail && <img src={m.thumbnail} alt="" style={{ width: "100%", borderRadius: 6, background: "#000", maxHeight: 160, objectFit: "contain" }} />}
      <dl className="kv">
        {m.type !== "image" && (<><dt>Duration</dt><dd>{m.duration.toFixed(2)} s</dd></>)}
        {m.width != null && (<><dt>Resolution</dt><dd>{m.width}×{m.height}{m.rotation ? ` (rotated ${m.rotation}°)` : ""}</dd></>)}
        {m.type === "video" && (<><dt>Frame rate</dt><dd>{m.fps} fps</dd></>)}
        {m.videoCodec && m.type === "video" && (<><dt>Video codec</dt><dd>{m.videoCodec}{m.hdr ? " · HDR (tone-mapped)" : ""}</dd></>)}
        {m.hasAudio && (<><dt>Audio</dt><dd>{m.audioCodec} · {m.sampleRate ? `${m.sampleRate / 1000} kHz` : ""} · {m.channels} ch</dd></>)}
        {m.type === "video" && !m.hasAudio && (<><dt>Audio</dt><dd className="faint">none</dd></>)}
        <dt>File size</dt><dd>{fmtBytes(m.size)}</dd>
        <dt>Proxy</dt>
        <dd>
          {m.proxyStatus === "ready" ? "Ready" : m.proxyStatus === "notNeeded" ? "Not needed" : m.proxyStatus === "error" ? <span className="error-text">Failed</span> : "Building…"}
        </dd>
        <dt>Used</dt><dd>{used} clip{used === 1 ? "" : "s"}</dd>
      </dl>
      {m.error && <div className="error-text" style={{ fontSize: 12 }}>{m.error}</div>}
      <div className="prop-group">
        <button className="btn primary" onClick={() => useEditor.getState().addToTimeline(m.id)}>
          <Icon name="plus" size={14} /> Add to timeline
        </button>
        <button
          className="btn"
          onClick={() =>
            api.reprocessMedia(m.id).then(
              () => toast({ kind: "info", title: `Rebuilding thumbnail, waveform and proxy for ${m.name}` }),
              (e) => toastError("Could not reprocess", e),
            )
          }
        >
          <Icon name="refresh" size={14} /> Rebuild proxy
        </button>
        <button
          className="btn danger"
          onClick={() => {
            if (used && !window.confirm(`${m.name} is used by ${used} clip(s). Remove it and those clips from the project?`)) return;
            useEditor.getState().removeMedia(m.id);
          }}
        >
          <Icon name="trash" size={14} /> Remove from project
        </button>
      </div>
    </div>
  );
}

function ProjectProps() {
  const settings = useEditor((s) => s.project!.settings);
  const commit = useEditor((s) => s.commit);
  const setAspect = (a: Aspect) => {
    const [w, h] = ASPECTS[a];
    commit((p) => {
      p.settings.aspect = a;
      p.settings.width = w;
      p.settings.height = h;
      p.export.width = w;
      p.export.height = h;
      p.export.preset = { "9:16": "reels", "1:1": "square", "16:9": "youtube", "4:5": "portrait" }[a];
    });
  };
  return (
    <div className="props">
      <div>
        <div className="label">Project</div>
        <h3>Canvas & defaults</h3>
        <div className="faint" style={{ fontSize: 12 }}>Select a clip or a media file to see its properties.</div>
      </div>
      <div className="prop-group">
        <div className="label">Aspect ratio</div>
        <div className="seg" style={{ width: "100%" }}>
          {(Object.keys(ASPECTS) as Aspect[]).map((a) => (
            <button key={a} className={settings.aspect === a ? "on" : ""} style={{ flex: 1 }} onClick={() => setAspect(a)}>
              {a}
            </button>
          ))}
        </div>
        <div className="faint" style={{ fontSize: 12 }}>{settings.width}×{settings.height}</div>
      </div>
      <div className="prop-group">
        <div className="label">Default fit</div>
        <div className="seg" style={{ width: "100%" }}>
          {(["fit", "fill", "blur"] as FitMode[]).map((f) => (
            <button key={f} className={settings.background.mode === f ? "on" : ""} style={{ flex: 1 }} onClick={() => commit((p) => void (p.settings.background.mode = f))}>
              {f === "fit" ? "Fit" : f === "fill" ? "Fill (crop)" : "Blurred bg"}
            </button>
          ))}
        </div>
        <div className="prop-row">
          <label>Background</label>
          <div className="row">
            <input type="color" value={settings.background.color} onChange={(e) => commit((p) => void (p.settings.background.color = e.target.value))} />
            <span className="mono faint" style={{ fontSize: 11 }}>{settings.background.color}</span>
          </div>
        </div>
      </div>
      <div className="prop-group">
        <div className="label">Timeline frame rate</div>
        <div className="seg" style={{ width: "100%" }}>
          {[24, 25, 30, 60].map((f) => (
            <button key={f} className={settings.fps === f ? "on" : ""} style={{ flex: 1 }} onClick={() => commit((p) => void (p.settings.fps = f))}>
              {f}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
