import { useEffect, useState } from "react";
import { api } from "../api";
import { toastError, useApp } from "../store/app";
import { useEditor, useSelectedClip } from "../store/editor";
import type { Aspect, Clip, FitMode, MediaMeta } from "../types";
import { ASPECTS, clipDuration, fmtBytes, fmtDuration } from "../util";
import { Icon } from "./Icons";

export default function Properties() {
  const sel = useEditor((s) => s.selection);
  const media = useEditor((s) => s.media);
  const selClip = useSelectedClip();
  return (
    <div className="panel">
      <div className="panel-head">Properties</div>
      <div className="panel-body">
        {selClip ? (
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
  const update = (fn: (c: Clip) => void) =>
    useEditor.getState().commit((p) => {
      const c = p.tracks.find((t) => t.id === trackId)?.clips.find((c) => c.id === clip.id);
      if (c) fn(c);
    });
  const maxOut = m && m.type !== "image" ? m.duration : 3600;
  const isVisual = trackType === "video" || trackType === "overlay";

  return (
    <div className="props">
      <div>
        <div className="label">Clip · {trackType} track</div>
        <h3 title={m?.name}>{m?.name ?? "Missing media"}</h3>
        <div className="faint" style={{ fontSize: 12 }}>
          {fmtDuration(clipDuration(clip))} on timeline
        </div>
      </div>

      <div className="prop-group">
        <div className="label">Timing</div>
        <div className="prop-row">
          <label>Start at</label>
          <NumberField value={clip.start} min={0} suffix="s" onCommit={(v) => update((c) => void (c.start = v))} />
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
        </div>
      )}

      {(m?.hasAudio ?? false) && (
        <div className="prop-group">
          <div className="label">Audio</div>
          <div className="prop-row">
            <label>Volume</label>
            <div className="slider-row">
              <input
                type="range" min={0} max={2} step={0.01} value={clip.volume}
                onChange={(e) => {
                  const v = parseFloat(e.target.value);
                  useEditor.getState().commit((p) => {
                    const c = p.tracks.find((t) => t.id === trackId)?.clips.find((c) => c.id === clip.id);
                    if (c) c.volume = v;
                  }, false);
                }}
                onPointerDown={() => useEditor.getState().beginGesture()}
                onPointerUp={() => useEditor.getState().endGesture()}
              />
              <span className="mono faint" style={{ fontSize: 11 }}>{Math.round(clip.volume * 100)}%</span>
            </div>
          </div>
          <div className="prop-row">
            <label>Mute</label>
            <label className="row" style={{ cursor: "pointer" }}>
              <input type="checkbox" checked={clip.muted} onChange={(e) => update((c) => void (c.muted = e.target.checked))} />
              <span className="muted">Mute this clip</span>
            </label>
          </div>
          {(trackType === "audio" || trackType === "music") && (
            <>
              <div className="prop-row">
                <label>Fade in</label>
                <NumberField value={clip.fadeIn} min={0} max={10} step={0.1} suffix="s" onCommit={(v) => update((c) => void (c.fadeIn = v))} />
              </div>
              <div className="prop-row">
                <label>Fade out</label>
                <NumberField value={clip.fadeOut} min={0} max={10} step={0.1} suffix="s" onCommit={(v) => update((c) => void (c.fadeOut = v))} />
              </div>
            </>
          )}
        </div>
      )}

      <div className="prop-group">
        <button className="btn danger" onClick={() => useEditor.getState().deleteSelectedClip()}>
          <Icon name="trash" size={14} /> Delete clip
        </button>
        <div className="faint" style={{ fontSize: 11 }}>Crop, keyframes, speed, filters and transitions arrive in later phases.</div>
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
