// Phase 5 clip editing: transform (with keyframes), motion presets, speed and grade strength.
import { useRef } from "react";
import * as E from "../motion/edit";
import { clipDuration as motionDuration, EASING_LABELS, EASINGS, RAMP_PRESETS, rampPoints, type Prop, type RampPoint } from "../motion/motion";
import { useEditor } from "../store/editor";
import SlowMoControls from "./SlowMoControls";
import * as ops from "../timeline/ops";
import type { Clip, MediaMeta } from "../types";
import { clipDuration } from "../util";

type Update = (fn: (c: Clip) => void, undoable?: boolean) => void;

export function useClipUpdate(clipId: string): Update {
  return (fn, undoable = true) =>
    useEditor.getState().commit((p) => {
      const hit = ops.findClip(p, clipId);
      if (!hit) return;
      fn(hit.clip);
      ops.settle(p, clipId);
    }, undoable);
}

/** Clip-relative time of the playhead, clamped to the clip. */
export function useClipTime(clip: Clip): number {
  const playhead = useEditor((s) => s.playhead);
  return Math.max(0, Math.min(clipDuration(clip), playhead - clip.start));
}

/** ◆ add/remove a keyframe at the playhead, ◀ ▶ jump between this property's keyframes. */
export function KeyControls({ clip, prop, tau, update }: { clip: Clip; prop: Prop; tau: number; update: Update }) {
  const has = E.hasKeys(clip, prop);
  const on = E.keyAt(clip, prop, tau) >= 0;
  const jump = (dir: 1 | -1) => {
    const t = E.jumpKey(clip, tau, dir, prop);
    if (t != null) useEditor.getState().setPlayhead(clip.start + t);
  };
  return (
    <span className="kf-ctl">
      <button className="kf-jump" disabled={!has} onClick={() => jump(-1)} title="Previous keyframe">◀</button>
      <button
        className={`kf-diamond ${on ? "on" : has ? "some" : ""}`}
        title={on ? "Remove the keyframe at the playhead" : "Add a keyframe at the playhead (animates this property)"}
        onClick={() => update((c) => void E.toggleKey(c, prop, tau))}
      >◆</button>
      <button className="kf-jump" disabled={!has} onClick={() => jump(1)} title="Next keyframe">▶</button>
    </span>
  );
}

const RESET: Partial<Record<Prop, number>> = { x: 0, y: 0, scale: 1, rotation: 0, opacity: 1, intensity: 1, volume: 1 };

/** Slider + value for an animatable property. Dragging records one undo step. */
export function PropSlider({ clip, prop, label, tau, update, min, max, step, scale = 1, fmt }: {
  clip: Clip; prop: Prop; label: string; tau: number; update: Update; min: number; max: number; step: number; scale?: number; fmt: (v: number) => string;
}) {
  const st = useEditor.getState;
  const v = E.propValue(clip, prop, tau);
  return (
    <div className="mo-row">
      <label onDoubleClick={() => update((c) => E.setProp(c, prop, tau, RESET[prop] ?? 0))} title="Double-click to reset">{label}</label>
      <input type="range" min={min} max={max} step={step} value={v * scale}
        onPointerDown={() => st().beginGesture()} onPointerUp={() => st().endGesture()}
        onChange={(e) => update((c) => E.setProp(c, prop, tau, +e.target.value / scale), false)} />
      <span className="mono faint">{fmt(v)}</span>
      <KeyControls clip={clip} prop={prop} tau={tau} update={update} />
    </div>
  );
}

export default function MotionPanel({ clip, trackType, m }: { clip: Clip; trackType: string; m: MediaMeta | undefined }) {
  const update = useClipUpdate(clip.id);
  const tau = useClipTime(clip);
  const settings = useEditor((s) => s.project!.settings);
  const st = useEditor.getState;
  const isMain = trackType === "video";
  const fill = (clip.fit ?? (isMain ? settings.background.mode : "fit")) === "fill";
  const ctx = { W: settings.width, H: settings.height, fps: settings.fps, fill, media: m };
  const keysHere = E.keyTimes(clip).find((k) => Math.abs(k.time - tau) < E.KEY_EPS);
  const easingHere = keysHere ? clip.keyframes[keysHere.props[0]]?.find((k) => Math.abs(k.time - tau) < E.KEY_EPS)?.easing ?? "linear" : null;
  const isVideo = m?.type === "video";
  const D = clipDuration(clip);
  const cr = clip.transform.crop;
  const preset = (p: E.MotionPreset) => update((c) => E.applyPreset(c, p, ctx));
  const hasMotion = ["x", "y", "scale", "rotation", "punch", "shakeX"].some((p) => E.hasKeys(clip, p as Prop));

  return (
    <>
      <div className="prop-group mo">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="label">Transform</div>
          <div className="row" style={{ gap: 2 }}>
            <button className="btn ghost xs" title="Previous keyframe (any property)" onClick={() => { const t = E.jumpKey(clip, tau, -1); if (t != null) st().setPlayhead(clip.start + t); }}>◀ key</button>
            <button className="btn ghost xs" title="Next keyframe (any property)" onClick={() => { const t = E.jumpKey(clip, tau, 1); if (t != null) st().setPlayhead(clip.start + t); }}>key ▶</button>
            <button className="btn ghost xs danger" title="Reset position, scale, rotation, opacity, crop and their keyframes"
              onClick={() => update((c) => {
                c.transform = { ...c.transform, x: 0, y: 0, scale: 1, rotation: 0, opacity: 1, flipH: false, flipV: false, crop: { left: 0, top: 0, right: 0, bottom: 0 } };
                for (const p of ["x", "y", "scale", "rotation", "opacity", "punch", "shakeX", "shakeY", "shakeR"]) delete c.keyframes?.[p];
              })}>Reset</button>
          </div>
        </div>
        <PropSlider clip={clip} prop="x" label="X" tau={tau} update={update} min={-settings.width} max={settings.width} step={1} fmt={(v) => `${Math.round(v)} px`} />
        <PropSlider clip={clip} prop="y" label="Y" tau={tau} update={update} min={-settings.height} max={settings.height} step={1} fmt={(v) => `${Math.round(v)} px`} />
        <PropSlider clip={clip} prop="scale" label="Scale" tau={tau} update={update} min={10} max={400} step={1} scale={100} fmt={(v) => `${Math.round(v * 100)}%`} />
        <PropSlider clip={clip} prop="rotation" label="Rotate" tau={tau} update={update} min={-180} max={180} step={0.5} fmt={(v) => `${v.toFixed(1)}°`} />
        <PropSlider clip={clip} prop="opacity" label="Opacity" tau={tau} update={update} min={0} max={100} step={1} scale={100} fmt={(v) => `${Math.round(v * 100)}%`} />
        {clip.filters?.length > 0 && (
          <PropSlider clip={clip} prop="intensity" label="Grade" tau={tau} update={update} min={0} max={100} step={1} scale={100} fmt={(v) => `${Math.round(v * 100)}%`} />
        )}
        {keysHere && (
          <div className="mo-row">
            <label>Easing</label>
            <select className="input" style={{ height: 24, gridColumn: "span 2" }} value={easingHere ?? "linear"}
              title="How the move from this keyframe to the next one accelerates"
              onChange={(e) => update((c) => { for (const p of keysHere.props) E.setKeyEasing(c, p as Prop, tau, e.target.value); })}>
              {EASINGS.map((k) => <option key={k} value={k}>{EASING_LABELS[k]}</option>)}
            </select>
            <span className="faint" style={{ fontSize: 10 }}>{keysHere.props.length} key{keysHere.props.length > 1 ? "s" : ""}</span>
          </div>
        )}
        <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
          <button className={`btn xs ${clip.transform.flipH ? "toggle on" : ""}`} onClick={() => update((c) => void (c.transform.flipH = !c.transform.flipH))}>Flip ↔</button>
          <button className={`btn xs ${clip.transform.flipV ? "toggle on" : ""}`} onClick={() => update((c) => void (c.transform.flipV = !c.transform.flipV))}>Flip ↕</button>
          <button className="btn xs" onClick={() => update((c) => void E.setProp(c, "rotation", tau, (Math.round(E.propValue(c, "rotation", tau) / 90) * 90 + 90 + 180) % 360 - 180))}>Rotate 90°</button>
        </div>
        <div className="faint" style={{ fontSize: 11 }}>Crop (part of the picture to keep)</div>
        {(["left", "right", "top", "bottom"] as const).map((side) => (
          <div className="mo-row" key={side}>
            <label>{side[0].toUpperCase() + side.slice(1)}</label>
            <input type="range" min={0} max={45} step={0.5} value={cr[side] * 100}
              onPointerDown={() => st().beginGesture()} onPointerUp={() => st().endGesture()}
              onChange={(e) => update((c) => void (c.transform.crop = { ...c.transform.crop, [side]: +e.target.value / 100 }), false)} />
            <span className="mono faint">{Math.round(cr[side] * 100)}%</span>
            <span />
          </div>
        ))}
        <div className="faint" style={{ fontSize: 11 }}>Tip: drag the picture in the preview to move it, its corners to scale, the top knob to rotate. With a ◆ on, changes become keyframes at the playhead.</div>
      </div>

      <div className="prop-group">
        <div className="label">Motion presets</div>
        <div className="tr-grid">
          <button className="btn sm" title="Slow eased zoom in (Ken Burns)" onClick={() => preset({ kind: "zoom", dir: "in" })}>Zoom in</button>
          <button className="btn sm" title="Slow eased zoom out (Ken Burns)" onClick={() => preset({ kind: "zoom", dir: "out" })}>Zoom out</button>
          <button className="btn sm" onClick={() => preset({ kind: "pan", dir: "left" })}>Pan ←</button>
          <button className="btn sm" onClick={() => preset({ kind: "pan", dir: "right" })}>Pan →</button>
          <button className="btn sm" onClick={() => preset({ kind: "pan", dir: "up" })}>Pan ↑</button>
          <button className="btn sm" onClick={() => preset({ kind: "pan", dir: "down" })}>Pan ↓</button>
          <button className="btn sm" title="Snappy scale-up at the playhead (put it on a beat)" onClick={() => preset({ kind: "punch", at: tau })}>Punch-in here</button>
          <ShakeButton onShake={(amount, duration) => preset({ kind: "shake", at: tau, amount, duration })} />
          <button className="btn sm ghost danger" disabled={!hasMotion} onClick={() => update((c) => E.clearMotion(c))}>Clear motion</button>
        </div>
      </div>

      {isVideo && <SpeedSection clip={clip} update={update} D={D} isMain={isMain} m={m} />}
    </>
  );
}

function ShakeButton({ onShake }: { onShake: (amount: number, duration: number) => void }) {
  const amount = useRef(14), dur = useRef(0.35);
  return (
    <span className="row" style={{ gap: 2 }}>
      <button className="btn sm" title="Camera shake from the playhead" onClick={() => onShake(amount.current, dur.current)}>Shake</button>
      <select className="input" style={{ height: 24, width: 64 }} defaultValue="14" title="Shake amount" onChange={(e) => (amount.current = +e.target.value)}>
        <option value="6">Light</option><option value="14">Medium</option><option value="28">Strong</option>
      </select>
      <select className="input" style={{ height: 24, width: 58 }} defaultValue="0.35" title="Shake duration" onChange={(e) => (dur.current = +e.target.value)}>
        <option value="0.2">0.2 s</option><option value="0.35">0.35 s</option><option value="0.6">0.6 s</option><option value="1">1 s</option>
      </select>
    </span>
  );
}

const SPEED_STOPS = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4];

function SpeedSection({ clip, update, D, isMain, m }: { clip: Clip; update: Update; D: number; isMain: boolean; m: MediaMeta }) {
  const st = useEditor.getState;
  const pts = rampPoints(clip);
  const ramp = clip.speedRamp as { points: RampPoint[]; preset?: string } | null | undefined;
  const frozen = clip.freezeFrame != null;
  // log2 slider: -2..2 => 0.25x..4x
  const lv = Math.log2(clip.speed || 1);
  return (
    <div className="prop-group">
      <div className="label">Speed</div>
      {frozen ? (
        <div className="muted" style={{ fontSize: 12 }}>
          Freeze frame (holds {clip.freezeFrame!.toFixed(2)} s of the source for {D.toFixed(2)} s).
          <button className="btn xs ghost" onClick={() => update((c) => void (c.freezeFrame = null))}>Unfreeze</button>
        </div>
      ) : (
        <>
          <div className="mo-row">
            <label>Constant</label>
            <input type="range" min={-2} max={2} step={0.01} value={lv} disabled={!!pts}
              onPointerDown={() => st().beginGesture()} onPointerUp={() => st().endGesture()}
              onChange={(e) => {
                let s = 2 ** +e.target.value;
                const near = SPEED_STOPS.find((x) => Math.abs(Math.log2(x) - Math.log2(s)) < 0.04);
                if (near) s = near;
                update((c) => E.setSpeed(c, +s.toFixed(3)), false);
              }} />
            <span className="mono faint">{(clip.speed || 1).toFixed(2)}×</span>
            <span />
          </div>
          <div className="faint" style={{ fontSize: 11 }}>Audio keeps its pitch. Length on the timeline: {D.toFixed(2)} s.</div>
          <div className="faint" style={{ fontSize: 11, marginTop: 4 }}>Speed ramp</div>
          <div className="tr-grid">
            <button className={`btn sm ${!pts ? "toggle on" : ""}`} onClick={() => update((c) => E.setRamp(c, null))}>None</button>
            {RAMP_PRESETS.map((r) => (
              <button key={r.id} className={`btn sm ${ramp?.preset === r.id ? "toggle on" : ""}`} onClick={() => update((c) => E.setRamp(c, r.points.map((p) => ({ ...p })), r.id))}>
                {r.label}
              </button>
            ))}
          </div>
          {pts && <RampEditor points={ramp!.points} onChange={(p, fin) => update((c) => E.setRamp(c, p, undefined), fin)} />}
        </>
      )}
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        <label className="row fl-check">
          <input type="checkbox" checked={!!clip.reverse} disabled={frozen} onChange={(e) => update((c) => void (c.reverse = e.target.checked))} />
          Reverse
        </label>
        {isMain && !frozen && (
          <button className="btn sm" title="Split at the playhead and hold that frame for 1 second" onClick={() => st().freezeFrameAtPlayhead(1)}>Freeze frame here</button>
        )}
      </div>
      {clip.reverse && <div className="faint" style={{ fontSize: 11 }}>The preview plays reversed clips silently; the export plays the sound backwards.</div>}
      <div className="faint" style={{ fontSize: 11 }}>Source used: {(clip.outPoint - clip.inPoint).toFixed(2)} s (avg {((clip.outPoint - clip.inPoint) / Math.max(1e-6, motionDuration(clip))).toFixed(2)}×).</div>
      {!frozen && <SlowMoControls clip={clip} m={m} update={update} />}
    </div>
  );
}

/** Speed curve: x = position in the clip, y = speed (log scale 0.1x..4x). Drag points; double-click
 *  the curve to add one; double-click a point to remove it. */
function RampEditor({ points, onChange }: { points: RampPoint[]; onChange: (p: RampPoint[], final: boolean) => void }) {
  const W = 240, H = 90;
  const ref = useRef<SVGSVGElement>(null);
  const yOf = (s: number) => H - ((Math.log2(s) + Math.log2(10)) / (Math.log2(4) + Math.log2(10))) * H;
  const sOf = (y: number) => Math.min(4, Math.max(0.1, 2 ** ((1 - y / H) * (Math.log2(4) + Math.log2(10)) - Math.log2(10))));
  const sorted = [...points].sort((a, b) => a.u - b.u);
  const local = (e: { clientX: number; clientY: number }) => {
    const r = ref.current!.getBoundingClientRect();
    return { u: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(H, Math.max(0, ((e.clientY - r.top) / r.height) * H)) };
  };
  const drag = (i: number) => (e: React.PointerEvent) => {
    e.stopPropagation();
    useEditor.getState().beginGesture();
    const move = (ev: PointerEvent) => {
      const { u, y } = local(ev);
      const next = sorted.map((p, j) => (j === i ? { u: i === 0 ? 0 : i === sorted.length - 1 ? 1 : u, speed: +sOf(y).toFixed(3) } : p));
      onChange(next, false);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      useEditor.getState().endGesture();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  return (
    <svg ref={ref} className="ramp-ed" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
      onDoubleClick={(e) => { const { u, y } = local(e); onChange([...sorted, { u, speed: +sOf(y).toFixed(3) }].sort((a, b) => a.u - b.u), true); }}>
      {[0.25, 0.5, 1, 2, 4].map((s) => (
        <g key={s}>
          <line x1={0} x2={W} y1={yOf(s)} y2={yOf(s)} className={s === 1 ? "ramp-one" : "ramp-grid"} />
          <text x={2} y={yOf(s) - 2} className="ramp-lbl">{s}×</text>
        </g>
      ))}
      <polyline className="ramp-line" points={sorted.map((p) => `${p.u * W},${yOf(p.speed)}`).join(" ")} />
      {sorted.map((p, i) => (
        <circle key={i} cx={p.u * W} cy={yOf(p.speed)} r={5} className="ramp-pt" onPointerDown={drag(i)}
          onDoubleClick={(e) => { e.stopPropagation(); if (sorted.length > 2 && i > 0 && i < sorted.length - 1) onChange(sorted.filter((_, j) => j !== i), true); }}>
          <title>{p.speed.toFixed(2)}× at {Math.round(p.u * 100)}%</title>
        </circle>
      ))}
    </svg>
  );
}
