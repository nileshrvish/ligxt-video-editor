// Properties > Follow a tracked face or object (text, stickers, overlay clips, adjustment layers): which
// track, follow position / scale / rotation, smoothing, offset, keep inside the Reels safe area; for an
// adjustment layer, a tracked region (blur / pixelate / grade inside an ellipse or box).
import { useEditor } from "../store/editor";
import * as ops from "../timeline/ops";
import type { Follow, Region } from "../tracking/types";
import type { Clip } from "../types";
import { TSlider } from "./TrackingPanel";

export default function FollowPanel({ clip }: { clip: Clip }) {
  const project = useEditor((s) => s.project)!;
  const media = useEditor((s) => s.media);
  const tracks = project.motionTracks ?? [];
  const commit = useEditor.getState().commit;
  const upd = (fn: (c: Clip) => void, final = true) =>
    commit((d) => {
      const c = ops.findClip(d, clip.id)?.clip;
      if (c) fn(c);
    }, final);
  const fo = clip.follow;
  const isRegion = !!clip.adjustment;
  if (!tracks.length) {
    return (
      <div className="prop-group">
        <div className="label">Follow a face or object</div>
        <div className="faint" style={{ fontSize: 11 }}>Select a video clip and use Motion tracking to track a face or an object first.</div>
      </div>
    );
  }
  // the clips that show each track's media (to name the host)
  const hosts = project.tracks.filter((t) => t.type === "video" || t.type === "overlay").flatMap((t) => t.clips);
  const hostFor = (trackId: string) => {
    const tr = tracks.find((t) => t.id === trackId);
    const cands = hosts.filter((c) => c.mediaId === tr?.mediaId);
    // the one that overlaps this clip most
    const ov = (c: Clip) => Math.max(0, Math.min(c.start + (c.outPoint - c.inPoint) / (c.speed || 1), clip.start + (clip.outPoint - clip.inPoint)) - Math.max(c.start, clip.start));
    return cands.sort((a, b) => ov(b) - ov(a))[0];
  };
  const setFollow = (patch: Partial<Follow>, final = true) =>
    upd((c) => {
      const base: Follow = c.follow ?? { trackId: "", position: true, smoothing: isRegion ? 0.15 : 0.3, safe: !isRegion && !!c.text };
      c.follow = { ...base, ...patch };
    }, final);
  const choose = (trackId: string) => {
    if (!trackId) return upd((c) => void (c.follow = null));
    const h = hostFor(trackId);
    setFollow({ trackId, clipId: h?.id, enabled: true });
    if (isRegion) upd((c) => void (c.region = { effect: "blur", amount: 70, shape: "ellipse", feather: 0.25, padding: 0.25, ...(c.region ?? {}) }));
  };
  const reg: Region = clip.region ?? {};
  const setReg = (patch: Partial<Region>, final = true) => upd((c) => void (c.region = { ...(c.region ?? {}), ...patch }), final);
  const label = (t: (typeof tracks)[number]) => `${t.name} · ${media[t.mediaId]?.name ?? "missing clip"}`;

  return (
    <div className="prop-group">
      <div className="label">{isRegion ? "Tracked region (blur / pixelate)" : "Follow a face or object"}</div>
      <div className="fl-row">
        <label>{isRegion ? "Region on" : "Follow"}</label>
        <select className="input" value={fo?.trackId ?? ""} onChange={(e) => choose(e.target.value)}>
          <option value="">{isRegion ? "Whole frame (no region)" : "Not following"}</option>
          {tracks.map((t) => <option key={t.id} value={t.id}>{label(t)}</option>)}
        </select>
        <span />
      </div>
      {fo?.trackId && (
        <div className="fl-body">
          {!isRegion && (
            <div className="row" style={{ gap: 10, fontSize: 12 }}>
              <label className="row fl-check"><input type="checkbox" checked={fo.position !== false} onChange={(e) => setFollow({ position: e.target.checked })} /> Position</label>
              <label className="row fl-check"><input type="checkbox" checked={!!fo.scale} onChange={(e) => setFollow({ scale: e.target.checked })} /> Scale</label>
              <label className="row fl-check"><input type="checkbox" checked={!!fo.rotation} onChange={(e) => setFollow({ rotation: e.target.checked })} /> Rotation</label>
            </div>
          )}
          <TSlider label="Smoothing" value={fo.smoothing ?? 0.3} min={0} max={1} step={0.05} fmt={(v) => `${Math.round(v * 100)}%`}
            title="Higher = steadier, ignores small jitter (very high values round off quick moves)" onChange={(v, f) => setFollow({ smoothing: v }, f)} />
          {!isRegion && (
            <>
              <TSlider label="Offset X" value={clip.transform?.x ?? 0} min={-800} max={800} step={1} fmt={(v) => `${Math.round(v)} px`}
                onChange={(v, f) => upd((c) => void (c.transform = { ...c.transform, x: v }), f)} />
              <TSlider label="Offset Y" value={clip.transform?.y ?? 0} min={-1200} max={1200} step={1} fmt={(v) => `${Math.round(v)} px`}
                onChange={(v, f) => upd((c) => void (c.transform = { ...c.transform, y: v }), f)} />
              <label className="row fl-check" title="Push it back inside when the target moves under the Reels header, caption area or buttons">
                <input type="checkbox" checked={!!fo.safe} onChange={(e) => setFollow({ safe: e.target.checked })} /> Keep inside the safe areas
              </label>
            </>
          )}
          {isRegion && (
            <>
              <div className="fl-row">
                <label>Effect</label>
                <div className="seg">
                  {(["blur", "pixelate", "grade"] as const).map((k) => (
                    <button key={k} className={(reg.effect ?? "blur") === k ? "on" : ""} onClick={() => setReg({ effect: k })}
                      title={k === "grade" ? "This layer's colour & effects, only inside the region" : undefined}>
                      {k === "blur" ? "Blur" : k === "pixelate" ? "Pixelate" : "Colour"}
                    </button>
                  ))}
                </div>
                <span />
              </div>
              {(reg.effect ?? "blur") !== "grade" && (
                <TSlider label="Strength" value={reg.amount ?? 70} min={0} max={100} step={1} fmt={(v) => `${Math.round(v)}%`} onChange={(v, f) => setReg({ amount: v }, f)} />
              )}
              <div className="fl-row">
                <label>Shape</label>
                <div className="seg">
                  {(["ellipse", "rect"] as const).map((k) => (
                    <button key={k} className={(reg.shape ?? "ellipse") === k ? "on" : ""} onClick={() => setReg({ shape: k })}>{k === "ellipse" ? "Ellipse" : "Box"}</button>
                  ))}
                </div>
                <span />
              </div>
              <TSlider label="Padding" value={reg.padding ?? 0.25} min={0} max={1} step={0.05} fmt={(v) => `${Math.round(v * 100)}%`}
                title="How much bigger than the tracked box" onChange={(v, f) => setReg({ padding: v }, f)} />
              <TSlider label="Soft edge" value={reg.feather ?? 0.25} min={0} max={1} step={0.05} fmt={(v) => `${Math.round(v * 100)}%`} onChange={(v, f) => setReg({ feather: v }, f)} />
            </>
          )}
        </div>
      )}
    </div>
  );
}
