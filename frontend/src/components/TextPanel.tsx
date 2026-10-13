// Properties for text clips: content, presets + my styles, look, animations, captions, transform.
import { useEffect, useState } from "react";
import { api } from "../api";
import { toastError, useApp } from "../store/app";
import { useEditor } from "../store/editor";
import * as ops from "../timeline/ops";
import { EASING_LABELS, EASINGS } from "../motion/motion";
import { TEXT_FONTS, TEXT_PRESETS, type TextSpec } from "../text/anim";
import * as TE from "../text/edit";
import type { Clip } from "../types";
import { clipDuration } from "../util";
import { Icon } from "./Icons";
import { PropSlider, useClipTime, useClipUpdate } from "./MotionPanel";
import { SafetyNote } from "./SafeAreas";

type Patch = Partial<TextSpec> | Record<string, unknown>;

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="fl-row">
      <label>{label}</label>
      {children}
    </div>
  );
}

function Range({ value, min, max, step, onChange, fmt }: { value: number; min: number; max: number; step: number; onChange: (v: number, fin: boolean) => void; fmt?: (v: number) => string }) {
  const st = useEditor.getState;
  return (
    <>
      <input type="range" min={min} max={max} step={step} value={value}
        onPointerDown={() => st().beginGesture()} onPointerUp={() => st().endGesture()}
        onChange={(e) => onChange(+e.target.value, false)} />
      <span className="mono faint">{fmt ? fmt(value) : Math.round(value * 100) / 100}</span>
    </>
  );
}

const ANIMS = [["none", "None"], ["fade", "Fade"], ["slide", "Slide"], ["pop", "Pop / scale"], ["typewriter", "Typewriter"], ["words", "Word by word"]];

function AnimEditor({ label, a, onChange }: { label: string; a: TextSpec["animIn"]; onChange: (p: Partial<TextSpec["animIn"]>, fin?: boolean) => void }) {
  return (
    <div className="tx-anim">
      <div className="row" style={{ gap: 6 }}>
        <span className="faint" style={{ fontSize: 11, width: 28 }}>{label}</span>
        <select className="input" style={{ height: 24, flex: 1 }} value={a.type} onChange={(e) => onChange({ type: e.target.value }, true)}>
          {ANIMS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        {a.type !== "none" && (
          <select className="input" style={{ height: 24, width: 96 }} value={a.easing} onChange={(e) => onChange({ easing: e.target.value }, true)}>
            {EASINGS.map((k) => <option key={k} value={k}>{EASING_LABELS[k]}</option>)}
          </select>
        )}
      </div>
      {a.type !== "none" && (
        <>
          <Row label="Duration"><Range value={a.duration} min={0.1} max={2} step={0.05} fmt={(v) => `${v.toFixed(2)} s`} onChange={(v, f) => onChange({ duration: v }, f)} /></Row>
          {a.type === "slide" && (
            <div className="row" style={{ gap: 4 }}>
              <span className="faint" style={{ fontSize: 11 }}>From</span>
              <div className="seg">
                {([["up", "below"], ["down", "above"], ["right", "left"], ["left", "right"]] as const).map(([d, l]) => (
                  <button key={d} className={a.direction === d ? "on" : ""} onClick={() => onChange({ direction: d }, true)}>{l}</button>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** One word per beat of the music under the clip: kinetic captions that land exactly on the beat. */
function BeatSync({ clip }: { clip: Clip }) {
  const project = useEditor((s) => s.project!);
  const media = useEditor((s) => s.media);
  const [every, setEvery] = useState<0.5 | 1 | 2>(1);
  const beats = project.tracks.filter((t) => (t.type === "music" || t.type === "audio") && !t.muted)
    .flatMap((t) => t.clips.flatMap((c) => (c.mediaId ? ops.clipBeats(c, media[c.mediaId]?.beats) : [])));
  const update = useClipUpdate(clip.id);
  return (
    <div className="row" style={{ gap: 6, marginTop: 4, flexWrap: "wrap" }}>
      <button className="btn sm" disabled={beats.length < 2}
        title={beats.length < 2 ? "Detect beats on the music first (select the music clip > Detect beats)" : "Each word appears exactly on a beat (style: Reveal + pop)"}
        onClick={() => {
          let n = 0;
          update((c) => void (n = TE.syncWordsToBeats(c, beats, every)));
          useApp.getState().toast({ kind: n ? "success" : "info", title: n ? `${n} words synced to the beat` : "No beats under this text" });
        }}>♪ Sync words to the beat</button>
      <select className="input" style={{ height: 24 }} value={every} onChange={(e) => setEvery(+e.target.value as 0.5 | 1 | 2)}>
        <option value={0.5}>every half beat</option><option value={1}>every beat</option><option value={2}>every 2 beats</option>
      </select>
    </div>
  );
}

export default function TextPanel({ clip }: { clip: Clip }) {
  const st = useEditor.getState;
  const update = useClipUpdate(clip.id);
  const tau = useClipTime(clip);
  const s = TE.specOf(clip);
  const toast = useApp((x) => x.toast);
  const [mine, setMine] = useState<{ id: string; name: string; style: Record<string, unknown> }[]>([]);
  const [saving, setSaving] = useState<string | null>(null);
  const [draft, setDraft] = useState(s.content);
  useEffect(() => setDraft(s.content), [s.content, clip.id]);
  const refresh = () => api.textStyles().then((r) => setMine(r.styles), () => void 0);
  useEffect(() => void refresh(), []);

  const patch = (p: Patch, fin = true) => update((c) => TE.patchText(c, p), fin);
  const isCaption = !!s.words?.length;

  return (
    <div className="props">
      <div>
        <div className="label">{isCaption ? "Caption" : "Text"}</div>
        <textarea className="input tx-content" rows={Math.min(5, Math.max(2, draft.split("\n").length))} value={draft}
          onFocus={() => st().beginGesture()}
          onBlur={() => st().endGesture()}
          onChange={(e) => { setDraft(e.target.value); update((c) => TE.setContent(c, e.target.value), false); }} />
        <div className="row" style={{ gap: 6, marginTop: 4 }}>
          <button className="btn sm" onClick={() => st().setEditingText(clip.id)} title="Type directly on the preview (double-click the text there too)">
            <Icon name="text" size={12} /> Edit on canvas
          </button>
          <span className="faint" style={{ fontSize: 11 }}>{clipDuration(clip).toFixed(2)} s on the timeline</span>
        </div>
        <SafetyNote clipId={clip.id} />
      </div>

      <div className="prop-group">
        <div className="label">Style presets</div>
        <div className="tr-grid">
          {TEXT_PRESETS.filter((p) => p.id !== "caption" || isCaption).map((p) => (
            <button key={p.id} className="btn sm" title={isCaption ? "Keeps the caption's words and timing" : undefined}
              onClick={() => update((c) => TE.applyStyle(c, p.style, isCaption ? undefined : p.position))}>{p.label}</button>
          ))}
        </div>
        {mine.length > 0 && <div className="faint" style={{ fontSize: 11 }}>My text styles</div>}
        <div className="tr-grid">
          {mine.map((m) => (
            <span key={m.id} className="row" style={{ gap: 0 }}>
              <button className="btn sm" onClick={() => update((c) => TE.applyStyle(c, m.style as Partial<TextSpec>))}>{m.name}</button>
              <button className="btn ghost icon xs" title="Delete style" onClick={() => api.deleteTextStyle(m.id).then(refresh, (e) => toastError("Delete failed", e))}><Icon name="x" size={10} /></button>
            </span>
          ))}
        </div>
        {saving === null ? (
          <button className="btn sm ghost" onClick={() => setSaving("")}>+ Save my text style</button>
        ) : (
          <div className="row">
            <input className="input" autoFocus placeholder="Style name" value={saving} style={{ flex: 1, height: 24 }}
              onChange={(e) => setSaving(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") setSaving(null);
                if (e.key === "Enter" && saving.trim())
                  api.saveTextStyle(saving.trim(), clip.text ?? {}).then(() => { setSaving(null); refresh(); toast({ kind: "success", title: "Text style saved" }); },
                    (er) => toastError("Could not save the style", er));
              }} />
            <button className="btn sm" onClick={() => setSaving(null)}>Cancel</button>
          </div>
        )}
      </div>

      <div className="prop-group fl">
        <div className="label">Font & look</div>
        <Row label="Font">
          <select className="input" style={{ height: 24, gridColumn: "span 2" }} value={s.font} onChange={(e) => patch({ font: e.target.value })}>
            {TEXT_FONTS.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
          </select>
        </Row>
        <Row label="Size"><Range value={s.size} min={16} max={300} step={1} fmt={(v) => `${Math.round(v)} px`} onChange={(v, f) => patch({ size: v }, f)} /></Row>
        <Row label="Colour">
          <input type="color" value={s.color.slice(0, 7)} onChange={(e) => patch({ color: e.target.value }, false)} onBlur={() => st().endGesture()} onFocus={() => st().beginGesture()} />
          <label className="row fl-check"><input type="checkbox" checked={s.uppercase} onChange={(e) => patch({ uppercase: e.target.checked })} />CAPS</label>
        </Row>
        <div className="row" style={{ gap: 6 }}>
          <div className="seg">
            {(["left", "center", "right"] as const).map((a) => (
              <button key={a} className={s.align === a ? "on" : ""} onClick={() => patch({ align: a })}>{a === "center" ? "Centre" : a[0].toUpperCase() + a.slice(1)}</button>
            ))}
          </div>
        </div>
        <Row label="Spacing"><Range value={s.letterSpacing} min={-10} max={40} step={0.5} fmt={(v) => `${v} px`} onChange={(v, f) => patch({ letterSpacing: v }, f)} /></Row>
        <Row label="Line height"><Range value={s.lineHeight} min={0.7} max={2} step={0.01} fmt={(v) => v.toFixed(2)} onChange={(v, f) => patch({ lineHeight: v }, f)} /></Row>
        <Row label="Wrap at"><Range value={s.maxWidth} min={0} max={1600} step={10} fmt={(v) => (v ? `${v} px` : "off")} onChange={(v, f) => patch({ maxWidth: v }, f)} /></Row>
        <Row label="Outline"><Range value={s.outline.width} min={0} max={20} step={0.5} fmt={(v) => `${v} px`} onChange={(v, f) => patch({ outline: { width: v } }, f)} /></Row>
        {s.outline.width > 0 && <Row label="Outline col."><input type="color" value={s.outline.color.slice(0, 7)} onChange={(e) => patch({ outline: { color: e.target.value } }, false)} /><span /></Row>}
        <label className="row fl-check"><input type="checkbox" checked={s.shadow.enabled} onChange={(e) => patch({ shadow: { enabled: e.target.checked } })} />Shadow / glow</label>
        {s.shadow.enabled && (
          <>
            <Row label="Colour"><input type="color" value={s.shadow.color.slice(0, 7)} onChange={(e) => patch({ shadow: { color: e.target.value } }, false)} /><span /></Row>
            <Row label="Opacity"><Range value={s.shadow.opacity} min={0} max={1} step={0.01} fmt={(v) => `${Math.round(v * 100)}%`} onChange={(v, f) => patch({ shadow: { opacity: v } }, f)} /></Row>
            <Row label="Blur"><Range value={s.shadow.blur} min={0} max={40} step={0.5} onChange={(v, f) => patch({ shadow: { blur: v } }, f)} /></Row>
            <Row label="Offset Y"><Range value={s.shadow.y} min={-30} max={30} step={1} onChange={(v, f) => patch({ shadow: { y: v } }, f)} /></Row>
            <Row label="Offset X"><Range value={s.shadow.x} min={-30} max={30} step={1} onChange={(v, f) => patch({ shadow: { x: v } }, f)} /></Row>
          </>
        )}
        <label className="row fl-check"><input type="checkbox" checked={s.box.enabled} onChange={(e) => patch({ box: { enabled: e.target.checked } })} />Background box</label>
        {s.box.enabled && (
          <>
            <Row label="Colour"><input type="color" value={s.box.color.slice(0, 7)} onChange={(e) => patch({ box: { color: e.target.value } }, false)} />
              <div className="seg">
                {(["lines", "block"] as const).map((m) => <button key={m} className={s.box.mode === m ? "on" : ""} onClick={() => patch({ box: { mode: m } })}>{m === "lines" ? "Per line" : "Block"}</button>)}
              </div>
            </Row>
            <Row label="Opacity"><Range value={s.box.opacity} min={0} max={1} step={0.01} fmt={(v) => `${Math.round(v * 100)}%`} onChange={(v, f) => patch({ box: { opacity: v } }, f)} /></Row>
            <Row label="Padding"><Range value={s.box.padX} min={0} max={80} step={1} onChange={(v, f) => patch({ box: { padX: v, padY: Math.round(v / 2) } }, f)} /></Row>
            <Row label="Corners"><Range value={s.box.radius} min={0} max={60} step={1} onChange={(v, f) => patch({ box: { radius: v } }, f)} /></Row>
          </>
        )}
      </div>

      <div className="prop-group fl">
        <div className="label">Animation</div>
        <AnimEditor label="In" a={s.animIn} onChange={(p, f = true) => patch({ animIn: p }, f)} />
        <AnimEditor label="Out" a={s.animOut} onChange={(p, f = true) => patch({ animOut: p }, f)} />
        <BeatSync clip={clip} />
        {isCaption && (
          <>
            <div className="faint" style={{ fontSize: 11, marginTop: 4 }}>Word highlight (follows the words' timing)</div>
            <div className="row" style={{ gap: 6 }}>
              <select className="input" style={{ height: 24, flex: 1 }} value={s.highlight.style} onChange={(e) => patch({ highlight: { style: e.target.value } })}>
                <option value="none">None</option><option value="color">Karaoke (colour)</option><option value="pop">Karaoke pop</option><option value="reveal">Reveal as spoken</option><option value="revealpop">Reveal + pop</option>
              </select>
              <input type="color" value={s.highlight.color.slice(0, 7)} onChange={(e) => patch({ highlight: { color: e.target.value } }, false)} />
            </div>
          </>
        )}
      </div>

      <div className="prop-group mo">
        <div className="label">Position & keyframes</div>
        <PropSlider clip={clip} prop="x" label="X" tau={tau} update={update} min={-1080} max={1080} step={1} fmt={(v) => `${Math.round(v)} px`} />
        <PropSlider clip={clip} prop="y" label="Y" tau={tau} update={update} min={-1920} max={1920} step={1} fmt={(v) => `${Math.round(v)} px`} />
        <PropSlider clip={clip} prop="scale" label="Scale" tau={tau} update={update} min={10} max={400} step={1} scale={100} fmt={(v) => `${Math.round(v * 100)}%`} />
        <PropSlider clip={clip} prop="rotation" label="Rotate" tau={tau} update={update} min={-180} max={180} step={0.5} fmt={(v) => `${v.toFixed(1)}°`} />
        <PropSlider clip={clip} prop="opacity" label="Opacity" tau={tau} update={update} min={0} max={100} step={1} scale={100} fmt={(v) => `${Math.round(v * 100)}%`} />
      </div>

      <div className="prop-group">
        <div className="prop-row">
          <label>Start at</label>
          <input className="input" type="number" step={0.05} defaultValue={+clip.start.toFixed(3)} key={`s${clip.start}`}
            onBlur={(e) => +e.target.value !== clip.start && st().commit((p) => ops.moveClip(p, clip.id, Math.max(0, +e.target.value)))} />
        </div>
        <div className="prop-row">
          <label>Length</label>
          <input className="input" type="number" step={0.05} min={0.1} defaultValue={+(clip.outPoint - clip.inPoint).toFixed(3)} key={`l${clip.outPoint}`}
            onBlur={(e) => update((c) => void (c.outPoint = c.inPoint + Math.max(0.1, +e.target.value)))} />
        </div>
        <button className="btn danger" onClick={() => st().deleteSelectedClip()}><Icon name="trash" size={14} /> Delete {isCaption ? "caption" : "text"}</button>
      </div>
    </div>
  );
}
