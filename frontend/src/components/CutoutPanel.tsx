// Properties > Cutout: AI subject cutout for a video/photo clip and the effects built on it
// (background replace, glow/outline with beat pulse, drop shadow). Grades for only the subject or only
// the background live in Colour & effects ("Apply to").
import { useEffect, useReducer, useState } from "react";
import { canvasToClip, clipToCanvas, sourceTimeAt, type PickPoint } from "../cutout/pick";
import { onJob } from "../store/app";
import { api, type AiModel } from "../api";
import { cutoutAsset, onCutoutAssets, retryCutout, type CutoutAsset } from "../cutout/assets";
import { normalizeCutout, type Cutout, type CutoutBgType } from "../cutout/model";
import { toastError } from "../store/app";
import { useEditor } from "../store/editor";
import type { Clip, MediaMeta } from "../types";
import { ProgressBar } from "./Common";
import { Icon } from "./Icons";

function Slider({ label, value, min, max, step, fmt, onChange, title }: {
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
      <span className="mono faint">{fmt ? fmt(value) : Math.round(value * 100) / 100}</span>
    </div>
  );
}

const mb = (n?: number) => (n ? `${Math.round(n / 1048576)} MB` : "");

/** Re-render when cutout assets change (analysis progress, ready). */
function useCutoutAsset(clip: Clip, m: MediaMeta | undefined, on: boolean): CutoutAsset | null {
  const [, bump] = useReducer((x: number) => x + 1, 0);
  useEffect(() => onCutoutAssets(bump), []);
  return on ? cutoutAsset(clip, m) : null;
}

export default function CutoutPanel({ clip, trackType, m }: { clip: Clip; trackType: string; m: MediaMeta }) {
  const cut = normalizeCutout(clip.cutout);
  const asset = useCutoutAsset(clip, m, cut.enabled);
  const [open, setOpen] = useState<Record<string, boolean>>({ edges: false, bg: true, glow: false, shadow: false });
  const [models, setModels] = useState<AiModel[]>([]);
  const [defModel, setDefModel] = useState("");
  const media = useEditor((s) => s.media);
  const projectMedia = useEditor((s) => s.project?.media ?? []);
  useEffect(() => {
    api.aiStatus().then((r) => {
      setModels(r.models.filter((x) => x.task === "person"));
      setDefModel(r.settings.personModel);
    }, () => void 0);
  }, [asset?.status]);
  const set = (fn: (c: Cutout) => void, final = true) => useEditor.getState().setCutout(clip.id, fn, final);
  const isMain = trackType === "video";
  const section = (key: string, title: string, badge?: string) => (
    <button className="fl-head" onClick={() => setOpen({ ...open, [key]: !open[key] })}>
      <span>{open[key] ? "▾" : "▸"} {title}</span>
      {badge && <span className="fl-badge">{badge}</span>}
    </button>
  );
  const model = models.find((x) => x.id === (cut.model === "auto" ? defModel : cut.model));
  const bgChoices: [CutoutBgType, string][] = isMain
    ? [["keep", "Keep"], ["blur", "Blur"], ["color", "Colour"], ["image", "Photo"], ["video", "Video"]]
    : [["keep", "Keep"], ["remove", "Remove"]];
  const bgMedia = projectMedia.map((id) => media[id]).filter((x): x is MediaMeta =>
    !!x && x.id !== m.id && (cut.background.type === "video" ? x.type === "video" : x.type === "image"));

  return (
    <div className="prop-group fl cutout">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="label">Cutout (AI) {cut.enabled && asset?.status === "ready" && <span className="fl-badge" title="Analysed and cached">✓ cached</span>}</div>
      </div>
      <label className="row fl-check" title="Separates the person (or main subject) from the background. No green screen needed.">
        <input type="checkbox" checked={cut.enabled} onChange={(e) => set((c) => void (c.enabled = e.target.checked))} />
        Cut out the subject
      </label>
      {!cut.enabled && model && !model.installed && (
        <div className="faint" style={{ fontSize: 11 }}>First use downloads the {model.name} model ({mb(model.downloadBytes)}).</div>
      )}
      {cut.enabled && (
        <div className="fl-row">
          <label>Subject</label>
          <div className="seg" style={{ gridColumn: "span 2" }}>
            <button className={cut.mode === "person" ? "on" : ""} onClick={() => set((c) => void (c.mode = "person"))} title="Finds the people automatically">Person (auto)</button>
            <button className={cut.mode === "object" ? "on" : ""} onClick={() => set((c) => void (c.mode = "object"))} title="Click any object on the preview">Object (click)</button>
          </div>
        </div>
      )}
      {cut.enabled && cut.mode === "object" && <ObjectPicker clip={clip} m={m} committed={cut.points} />}
      {cut.enabled && <CutoutStatus clip={clip} asset={asset} />}
      {cut.enabled && (
        <>
          <div className="fl-row" style={{ display: cut.mode === "object" ? "none" : undefined }}>
            <label>Model</label>
            <select className="input" style={{ height: 24, gridColumn: "span 2" }} value={cut.model} onChange={(e) => set((c) => void (c.model = e.target.value))}>
              <option value="auto">Default ({models.find((x) => x.id === defModel)?.name ?? "Settings"})</option>
              {models.map((x) => <option key={x.id} value={x.id}>{x.name}{x.installed ? "" : ` (downloads ${mb(x.downloadBytes)})`}</option>)}
            </select>
          </div>
          <a className="faint" style={{ fontSize: 11, display: cut.mode === "object" ? "none" : undefined }} href="#/settings" target="_blank" rel="noreferrer"
            title="Models, disk usage, GPU memory limit (opens in a new tab)">Models and GPU settings…</a>

          {section("edges", "Edges", cut.refine.grow || cut.refine.feather !== 1 ? "edited" : undefined)}
          {open.edges && (
            <div className="fl-body">
              <Slider label="Smooth" value={cut.refine.smooth} min={0} max={1} step={0.05} fmt={(v) => `${Math.round(v * 100)}%`}
                title="Steadies the edge between frames (less flicker). Higher = steadier, but fast motion can lag a little."
                onChange={(v, f) => set((c) => void (c.refine.smooth = v), f)} />
              <Slider label="Shrink/grow" value={cut.refine.grow} min={-12} max={12} step={1} fmt={(v) => `${v > 0 ? "+" : ""}${v} px`}
                title="Negative pulls the edge in (removes background fringe), positive pushes it out"
                onChange={(v, f) => set((c) => void (c.refine.grow = v), f)} />
              <Slider label="Feather" value={cut.refine.feather} min={0} max={8} step={0.5} fmt={(v) => `${v} px`}
                title="Softens the edge" onChange={(v, f) => set((c) => void (c.refine.feather = v), f)} />
              <Slider label="Motion blur" value={cut.refine.motion} min={0} max={1} step={0.05} fmt={(v) => (v ? `${Math.round(v * 100)}%` : "off")}
                title="Fast-moving arms and hands are blurred and come out see-through, so text behind shows through them. This makes them solid (only where the picture moves fast)."
                onChange={(v, f) => set((c) => void (c.refine.motion = v), f)} />
              <label className="row fl-check" title="Edges follow the full-resolution picture (hair, fingers). Changing it analyses again.">
                <input type="checkbox" checked={cut.refine.detail} onChange={(e) => set((c) => void (c.refine.detail = e.target.checked))} />
                Hair detail
              </label>
              <div className="faint" style={{ fontSize: 11 }}>Edge changes are applied in a few seconds; the analysis is reused.</div>
            </div>
          )}

          {section("bg", "Background", bgChoices.find(([k]) => k === cut.background.type)?.[1])}
          {open.bg && (
            <div className="fl-body">
              <div className="seg" style={{ flexWrap: "wrap" }}>
                {bgChoices.map(([k, l]) => (
                  <button key={k} className={cut.background.type === k ? "on" : ""} onClick={() => set((c) => void (c.background.type = k))}>{l}</button>
                ))}
              </div>
              {cut.background.type === "blur" && (
                <Slider label="Blur" value={cut.background.blur} min={2} max={80} step={1} fmt={(v) => `${v}`}
                  onChange={(v, f) => set((c) => void (c.background.blur = v), f)} />
              )}
              {cut.background.type === "color" && (
                <div className="fl-row">
                  <label>Colour</label>
                  <input type="color" value={cut.background.color.slice(0, 7)} onFocus={() => useEditor.getState().beginGesture()}
                    onBlur={() => useEditor.getState().endGesture()} onChange={(e) => set((c) => void (c.background.color = e.target.value), false)} />
                  <span />
                </div>
              )}
              {(cut.background.type === "image" || cut.background.type === "video") && (
                <div className="fl-row">
                  <label>{cut.background.type === "image" ? "Photo" : "Video"}</label>
                  <select className="input" style={{ height: 24, gridColumn: "span 2" }} value={cut.background.mediaId ?? ""}
                    onChange={(e) => set((c) => void (c.background.mediaId = e.target.value || null))}>
                    <option value="">{bgMedia.length ? "Choose…" : `Import a ${cut.background.type === "image" ? "photo" : "video"} first`}</option>
                    {bgMedia.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
                  </select>
                </div>
              )}
              {!isMain && <div className="faint" style={{ fontSize: 11 }}>Remove: only the subject shows over the tracks below.</div>}
              {isMain && <div className="faint" style={{ fontSize: 11 }}>Grade only the subject or only the background: Colour &amp; effects &gt; Apply to.</div>}
            </div>
          )}

          {section("glow", "Glow / outline", cut.glow.enabled ? (cut.glow.pulse.mode === "beat" ? "on the beat" : "on") : undefined)}
          {open.glow && (
            <div className="fl-body">
              <label className="row fl-check">
                <input type="checkbox" checked={cut.glow.enabled} onChange={(e) => set((c) => void (c.glow.enabled = e.target.checked))} />
                Glow around the subject
              </label>
              {cut.glow.enabled && (
                <>
                  <div className="fl-row">
                    <label>Colour</label>
                    <input type="color" value={cut.glow.color.slice(0, 7)} onFocus={() => useEditor.getState().beginGesture()}
                      onBlur={() => useEditor.getState().endGesture()} onChange={(e) => set((c) => void (c.glow.color = e.target.value), false)} />
                    <span />
                  </div>
                  <Slider label="Width" value={cut.glow.width} min={2} max={80} step={1} fmt={(v) => `${v} px`} onChange={(v, f) => set((c) => void (c.glow.width = v), f)} />
                  <Slider label="Softness" value={cut.glow.softness} min={0} max={1} step={0.05} fmt={(v) => (v < 0.15 ? "outline" : `${Math.round(v * 100)}%`)}
                    title="0 = crisp outline, 100% = soft halo" onChange={(v, f) => set((c) => void (c.glow.softness = v), f)} />
                  <Slider label="Opacity" value={cut.glow.opacity} min={0} max={1} step={0.05} fmt={(v) => `${Math.round(v * 100)}%`} onChange={(v, f) => set((c) => void (c.glow.opacity = v), f)} />
                  <div className="fl-row">
                    <label>Pulse</label>
                    <div className="seg" style={{ gridColumn: "span 2" }}>
                      {([["none", "Off"], ["beat", "On the beat"], ["rate", "Steady"]] as const).map(([k, l]) => (
                        <button key={k} className={cut.glow.pulse.mode === k ? "on" : ""} onClick={() => set((c) => void (c.glow.pulse.mode = k))}>{l}</button>
                      ))}
                    </div>
                  </div>
                  {cut.glow.pulse.mode !== "none" && (
                    <Slider label="Strength" value={cut.glow.pulse.amount} min={0.1} max={1} step={0.05} fmt={(v) => `${Math.round(v * 100)}%`} onChange={(v, f) => set((c) => void (c.glow.pulse.amount = v), f)} />
                  )}
                  {cut.glow.pulse.mode === "rate" && (
                    <Slider label="Speed" value={cut.glow.pulse.rate} min={0.25} max={6} step={0.25} fmt={(v) => `${v}/s`} onChange={(v, f) => set((c) => void (c.glow.pulse.rate = v), f)} />
                  )}
                  {cut.glow.pulse.mode === "beat" && <BeatHint />}
                </>
              )}
            </div>
          )}

          {section("shadow", "Drop shadow", cut.shadow.enabled ? "on" : undefined)}
          {open.shadow && (
            <div className="fl-body">
              <label className="row fl-check">
                <input type="checkbox" checked={cut.shadow.enabled} onChange={(e) => set((c) => void (c.shadow.enabled = e.target.checked))} />
                Shadow behind the subject
              </label>
              {cut.shadow.enabled && (
                <>
                  <div className="fl-row">
                    <label>Colour</label>
                    <input type="color" value={cut.shadow.color.slice(0, 7)} onChange={(e) => set((c) => void (c.shadow.color = e.target.value), false)} />
                    <span />
                  </div>
                  <Slider label="Opacity" value={cut.shadow.opacity} min={0} max={1} step={0.05} fmt={(v) => `${Math.round(v * 100)}%`} onChange={(v, f) => set((c) => void (c.shadow.opacity = v), f)} />
                  <Slider label="Blur" value={cut.shadow.blur} min={0} max={60} step={1} onChange={(v, f) => set((c) => void (c.shadow.blur = v), f)} />
                  <Slider label="Offset X" value={cut.shadow.x} min={-80} max={80} step={1} onChange={(v, f) => set((c) => void (c.shadow.x = v), f)} />
                  <Slider label="Offset Y" value={cut.shadow.y} min={-80} max={80} step={1} onChange={(v, f) => set((c) => void (c.shadow.y = v), f)} />
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function BeatHint() {
  const has = useEditor((s) => !!s.project?.tracks.some((t) => t.type === "music" && !t.muted && t.clips.some((c) => (c.mediaId && s.media[c.mediaId]?.beats?.length))));
  return has ? null : (
    <div className="faint" style={{ fontSize: 11, color: "var(--warn)" }}>No beats yet: add a song on a music track and run Detect beats on it.</div>
  );
}

function CutoutStatus({ clip, asset }: { clip: Clip; asset: CutoutAsset | null }) {
  if (asset?.status === "needsPoints") return null; // the object picker explains what to do
  if (!asset || asset.status === "checking") return <div className="faint" style={{ fontSize: 12 }}>Checking…</div>;
  if (asset.status === "running" || asset.status === "missing") {
    const pct = Math.round((asset.progress ?? 0) * 100);
    const msg = asset.message?.startsWith("Downloading") ? asset.message
      : asset.message?.startsWith("Refining") || asset.message?.startsWith("Smoothing") ? `Refining edges… ${pct}%` : `Analyzing subject… ${pct}%`;
    return (
      <div className="cut-status">
        <div className="row" style={{ gap: 6 }}>
          <Icon name="loader" size={12} className="spin" />
          <span style={{ fontSize: 12 }}>{msg}</span>
          <span className="spacer" />
          {asset.jobId && <button className="btn ghost xs" onClick={() => api.cancelJob(asset.jobId!).catch((e) => toastError("Cancel failed", e))}>Cancel</button>}
        </div>
        <ProgressBar value={asset.progress ?? 0} />
        {!asset.message?.startsWith("Downloading") && asset.message && asset.message.includes("frames") && <div className="faint" style={{ fontSize: 11 }}>{asset.message}</div>}
        <div className="faint" style={{ fontSize: 11 }}>The preview shows the clip as it is until the analysis is done. It is cached, so it only runs once.</div>
      </div>
    );
  }
  if (asset.status === "error") {
    return (
      <div className="cut-status">
        <div style={{ fontSize: 12, color: "var(--danger)" }}>{asset.error === "Cancelled" ? "Analysis cancelled." : `Cutout failed: ${asset.error}`}</div>
        <button className="btn sm" onClick={() => retryCutout(clip)}><Icon name="refresh" size={12} /> Try again</button>
      </div>
    );
  }
  const info = asset.info!;
  return (
    <div className="cut-status">
      {info.noSubject ? (
        <div style={{ fontSize: 12, color: "var(--warn)" }}>
          No person found in this clip. Cutout works best on people and clear main subjects; try another clip or turn it off.
        </div>
      ) : (
        <div className="faint" style={{ fontSize: 11 }}>
          ✓ Ready{info.emptyFrames > 0.15 ? ` · no subject in ${Math.round(info.emptyFrames * 100)}% of the frames` : ""}
          {info.device ? ` · analysed on ${info.device}` : ""}{info.analysisFps ? ` (${info.analysisFps} fps)` : ""}
        </div>
      )}
      {info.warning && <div style={{ fontSize: 11, color: "var(--warn)" }}>{info.warning}</div>}
    </div>
  );
}

// ---------------------------------------------------------------- click to select (object mode)
function ObjectPicker({ clip, m, committed }: { clip: Clip; m: MediaMeta; committed: PickPoint[] }) {
  const pick = useEditor((s) => (s.cutoutPick?.clipId === clip.id ? s.cutoutPick : null));
  const [preview, setPreview] = useState<{ url?: string; busy: boolean; msg?: string; error?: string }>({ busy: false });
  const st = useEditor.getState;
  const points = pick ? pick.points : committed;
  const start = () => st().setCutoutPick({ clipId: clip.id, points: [...committed] });
  const stop = () => st().setCutoutPick(null);
  const dirty = !!pick && JSON.stringify(pick.points) !== JSON.stringify(committed);
  const commit = () => {
    if (!pick) return;
    st().setCutout(clip.id, (c) => void (c.points = pick.points));
    stop();
  };
  const showPreview = () => {
    if (!points.some((p) => p.label === 1)) return;
    const t = points[points.length - 1].t;
    const same = points.filter((p) => Math.abs(p.t - t) < 0.02);
    setPreview({ busy: true, msg: "Waiting for the GPU…" });
    api.cutoutPick(m.id, t, same).then(({ jobId }) => {
      const off = onJob((j) => {
        if (j.id !== jobId) return;
        if (j.status === "running" || j.status === "queued") return setPreview((x) => ({ ...x, busy: true, msg: j.message || "Finding the object…" }));
        off();
        if (j.status === "done") setPreview({ busy: false, url: j.result?.url + `?v=${Date.now()}` });
        else setPreview({ busy: false, error: j.error ?? "Failed" });
      });
    }, (e) => setPreview({ busy: false, error: e.message }));
  };
  return (
    <div className="cut-status">
      <div style={{ fontSize: 12 }}>
        {pick ? <>Click the object on the preview. <b>Shift+click</b> marks what is <i>not</i> part of it. Move to another frame to add clicks there.</>
          : <>Pause on a frame where the object is clear, then pick it.</>}
      </div>
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        {!pick ? <button className="btn sm" onClick={start}><Icon name="wand" size={12} /> {committed.length ? "Edit selection" : "Pick on preview"}</button>
          : <button className="btn sm" onClick={stop}>Stop picking</button>}
        <span className="faint" style={{ fontSize: 11 }}>{points.filter((p) => p.label).length} on · {points.filter((p) => !p.label).length} off</span>
        {pick && points.length > 0 && <button className="btn ghost xs" onClick={() => st().setCutoutPick({ clipId: clip.id, points: [] })}>Clear</button>}
        {points.some((p) => p.label) && <button className="btn ghost sm" disabled={preview.busy} onClick={showPreview}>Preview selection</button>}
        {dirty && points.some((p) => p.label) && <button className="btn primary sm" onClick={commit}>Cut out this object</button>}
      </div>
      {preview.busy && <div className="faint" style={{ fontSize: 11 }}><Icon name="loader" size={11} className="spin" /> {preview.msg}</div>}
      {preview.error && <div style={{ fontSize: 11, color: "var(--danger)" }}>{preview.error}</div>}
      {preview.url && !preview.busy && <img src={preview.url} alt="" style={{ width: "100%", maxHeight: 220, objectFit: "contain", borderRadius: 6, background: "#000" }} title="Violet = selected; the clip is tracked from this" />}
      {dirty && <div className="faint" style={{ fontSize: 11 }}>"Cut out this object" tracks it through the clip (cached).</div>}
    </div>
  );
}

/** Over the preview while picking: clicks add points; the points on this frame are shown. */
export function CutoutPickLayer({ fw }: { fw: number; fh?: number }) {
  const pick = useEditor((s) => s.cutoutPick);
  const project = useEditor((s) => s.project);
  const media = useEditor((s) => s.media);
  const playhead = useEditor((s) => s.playhead);
  if (!pick || !project) return null;
  const track = project.tracks.find((t) => t.clips.some((c) => c.id === pick.clipId));
  const clip = track?.clips.find((c) => c.id === pick.clipId);
  const m = clip?.mediaId ? media[clip.mediaId] : undefined;
  if (!clip || !m || !track) return null;
  const isMain = track === project.tracks.find((t) => t.type === "video");
  const k = fw / project.settings.width;
  const inside = playhead >= clip.start && playhead < clip.start + (clip.outPoint - clip.inPoint) / (clip.speed || 1) + 1e-6;
  const tSrc = sourceTimeAt(clip, playhead);
  const onClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!inside) return;
    const r = e.currentTarget.getBoundingClientRect();
    const X = (e.clientX - r.left) / k, Y = (e.clientY - r.top) / k;
    const q = canvasToClip(clip, m, project, playhead, isMain, X, Y);
    if (!q) return;
    const label: 0 | 1 = e.shiftKey || e.altKey ? 0 : 1;
    useEditor.getState().setCutoutPick({ clipId: clip.id, points: [...pick.points, { t: +tSrc.toFixed(4), x: +q.x.toFixed(5), y: +q.y.toFixed(5), label }] });
  };
  const here = pick.points.filter((p) => Math.abs(p.t - tSrc) < 0.5 / (m.fps || 30));
  return (
    <div className="pv-pick" onClick={onClick} title={inside ? "Click the object · Shift+click: not this" : "Move the playhead into this clip"}
      onContextMenu={(e) => e.preventDefault()}>
      {here.map((p, i) => {
        const { X, Y } = clipToCanvas(clip, m, project, playhead, isMain, p.x, p.y);
        return <span key={i} className={`pick-dot ${p.label ? "on" : "off"}`} style={{ left: X * k, top: Y * k }} />;
      })}
      {!inside && <span className="preview-label" style={{ top: 8, left: 8 }}>Move the playhead into the clip to pick</span>}
    </div>
  );
}
