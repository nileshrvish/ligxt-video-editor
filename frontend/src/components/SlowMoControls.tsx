// Speed section > Slow-mo quality: frame repeat / frame blending / AI smooth (RIFE), "smooth motion"
// (frame-rate conversion), and the state of the retimed frames (progress, ready, warnings).
import { useEffect, useReducer } from "react";
import { api } from "../api";
import { onSlowAssets, retrySlow, slowAsset } from "../slowmo/assets";
import { minSpeed, needsRetime, normalizeSlowmo, qualityOf, QUALITY_LABELS, slowFactor, type SlowQuality } from "../slowmo/model";
import { toastError } from "../store/app";
import { useEditor } from "../store/editor";
import { mainTransitionPlans } from "../timeline/transitions";
import type { Clip, MediaMeta } from "../types";
import { ProgressBar } from "./Common";
import { Icon } from "./Icons";

/** Transition handles of a main-track clip (the preview asks for the same retimed frames). */
export function clipHandles(clip: Clip): [number, number] {
  const st = useEditor.getState();
  if (!st.project) return [0, 0];
  const plans = mainTransitionPlans(st.project, (id) => {
    const m = id ? st.media[id] : undefined;
    return !m || m.type === "image" ? Infinity : m.duration;
  });
  let before = 0, after = 0;
  for (const pl of plans.values()) {
    if (pl.toId === clip.id) before = pl.half;
    if (pl.fromId === clip.id) after = pl.half;
  }
  return [before, after];
}

export default function SlowMoControls({ clip, m, update }: { clip: Clip; m: MediaMeta; update: (fn: (c: Clip) => void, final?: boolean) => void }) {
  const [, bump] = useReducer((x: number) => x + 1, 0);
  useEffect(() => onSlowAssets(bump), []);
  const fps = useEditor((s) => s.project?.settings.fps ?? 30);
  const sm = normalizeSlowmo(clip.slowmo);
  const q = qualityOf(clip);
  const slowed = minSpeed(clip) < 1 - 1e-6;
  const retime = needsRetime(clip, m, fps);
  const [before, after] = clipHandles(clip);
  const asset = retime ? slowAsset(clip, m, fps, before, after, "preview") : null;
  const set = (patch: Partial<typeof sm>) => update((c) => void (c.slowmo = { ...normalizeSlowmo(c.slowmo), ...patch }));
  const fpsNote = m.fps && Math.abs(m.fps - fps) > 0.01 ? `${+m.fps.toFixed(3)} fps clip on a ${fps} fps timeline` : null;
  return (
    <div className="slowmo">
      <div className="mo-row">
        <label>Slow-mo</label>
        <select className="input" style={{ height: 24, gridColumn: "span 3" }} value={sm.quality}
          title="How in-between frames are made when the clip is slowed down" onChange={(e) => set({ quality: e.target.value as SlowQuality })}>
          {(Object.keys(QUALITY_LABELS) as SlowQuality[]).map((k) => <option key={k} value={k}>{QUALITY_LABELS[k]}</option>)}
        </select>
      </div>
      <label className="row fl-check" title="Make in-between frames at normal speed too: smooth frame-rate conversion (e.g. 24/25 fps to 30, or any clip to a 60 fps export)">
        <input type="checkbox" checked={sm.smooth} onChange={(e) => set({ smooth: e.target.checked })} />
        Smooth motion{fpsNote ? ` (${fpsNote})` : " (frame-rate conversion)"}
      </label>
      {!retime && (
        <div className="faint" style={{ fontSize: 11 }}>
          {q === "repeat" ? (slowed ? "Frame repeat: each frame is held longer (stutters when slowed a lot)." : "Normal speed: frames are shown as they are.")
            : "Nothing to interpolate at this speed."}
        </div>
      )}
      {retime && asset && (asset.status === "checking" || asset.status === "running") && (
        <div className="cut-status">
          <div className="row" style={{ gap: 6 }}>
            <Icon name="loader" size={12} className="spin" />
            <span style={{ fontSize: 12 }}>{q === "ai" ? "AI smooth" : "Blending frames"}… {Math.round((asset.progress ?? 0) * 100)}%</span>
            <span className="spacer" />
            {asset.jobId && <button className="btn ghost xs" onClick={() => api.cancelJob(asset.jobId!).catch((e) => toastError("Cancel failed", e))}>Cancel</button>}
          </div>
          <ProgressBar value={asset.progress ?? 0} />
          <div className="faint" style={{ fontSize: 11 }}>The preview shows frame repeat until it's ready. The export uses full resolution.</div>
        </div>
      )}
      {retime && asset?.status === "ready" && asset.info && (
        <div className="cut-status">
          <div className="faint" style={{ fontSize: 11 }}>
            ✓ {q === "ai" ? "AI smooth" : "Frame blending"} · {asset.info.interpolated ?? 0} in-between frames
            {slowed ? ` · ${slowFactor(clip).toFixed(slowFactor(clip) % 1 ? 1 : 0)}x slow motion` : ""}
            {asset.info.aiFps ? ` · ${asset.info.aiFps} fps` : ""}
          </div>
          {asset.info.warning && <div style={{ fontSize: 11, color: "var(--warn)" }}>{asset.info.warning}</div>}
        </div>
      )}
      {retime && asset?.status === "error" && (
        <div className="cut-status">
          <div style={{ fontSize: 12, color: "var(--danger)" }}>{asset.error === "Cancelled" ? "Cancelled." : `Slow motion failed: ${asset.error}`}</div>
          <button className="btn sm" onClick={() => retrySlow(clip, fps, before, after)}><Icon name="refresh" size={12} /> Try again</button>
        </div>
      )}
    </div>
  );
}
