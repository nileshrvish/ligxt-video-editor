import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { PRESET_ORDER, PRESETS, type EffectKey } from "../grade/math";
import {
  ADJUST_UI, adjustOf, EFFECT_UI, effectOf, lutOf, presetOf, setAdjust, setEffect, setLut, setPreset, withNewIds,
} from "../grade/stack";
import { toastError, useApp } from "../store/app";
import { useEditor } from "../store/editor";
import type { Clip, Filter } from "../types";
import { Icon } from "./Icons";

type Lut = { id: string; name: string; kind: "builtin" | "user" };
type UserPreset = { id: string; name: string; filters: Filter[] };

/** Slider that records one undo step per drag and resets on double-click. */
function Slider({ label, value, min, max, step, onChange, fmt }: {
  label: string; value: number; min: number; max: number; step: number; onChange: (v: number, final: boolean) => void; fmt?: (v: number) => string;
}) {
  const st = useEditor.getState;
  return (
    <div className="fl-row" title="Double-click to reset">
      <label onDoubleClick={() => onChange(min < 0 ? 0 : min, true)}>{label}</label>
      <input
        type="range" min={min} max={max} step={step} value={value}
        onPointerDown={() => st().beginGesture()}
        onPointerUp={() => st().endGesture()}
        onChange={(e) => onChange(+e.target.value, false)}
        onDoubleClick={() => onChange(min < 0 ? 0 : min, true)}
      />
      <span className="mono faint">{fmt ? fmt(value) : Math.round(value * 100) / 100}</span>
    </div>
  );
}

export default function FiltersPanel({ clip, trackId }: { clip: Clip; trackId: string }) {
  const filters = clip.filters ?? [];
  const st = useEditor.getState;
  const toast = useApp((s) => s.toast);
  const clipboard = useEditor((s) => s.filterClipboard);
  const [luts, setLuts] = useState<Lut[]>([]);
  const [mine, setMine] = useState<UserPreset[]>([]);
  const [open, setOpen] = useState<Record<string, boolean>>({ presets: true, adjust: false, effects: false, lut: false });
  const [saving, setSaving] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = () => {
    api.listLuts().then((r) => setLuts(r.luts), () => void 0);
    api.listPresets().then((r) => setMine(r.presets), () => void 0);
  };
  useEffect(refresh, []);

  const set = (next: Filter[], final = true) => st().setFilters(clip.id, next, final);
  const adj = adjustOf(filters);
  const preset = presetOf(filters);
  const lut = lutOf(filters);
  const count = filters.length;

  const applyToAll = () => {
    const p = st().project!;
    const ids = p.tracks
      .filter((t) => (t.type === "video" || t.type === "overlay") && !t.locked)
      .flatMap((t) => t.clips.filter((c) => c.id !== clip.id && (!!c.mediaId || c.adjustment) && !!c.adjustment === !!clip.adjustment).map((c) => c.id));
    st().commit((d) => {
      for (const t of d.tracks) for (const c of t.clips) if (ids.includes(c.id)) c.filters = withNewIds(filters);
    });
    toast({ kind: "success", title: `Applied to ${ids.length} other clip${ids.length === 1 ? "" : "s"}`, detail: "Ctrl+Z to undo." });
  };

  const section = (key: string, title: string, badge?: string) => (
    <button className="fl-head" onClick={() => setOpen({ ...open, [key]: !open[key] })}>
      <span>{open[key] ? "▾" : "▸"} {title}</span>
      {badge && <span className="fl-badge">{badge}</span>}
    </button>
  );

  return (
    <div className="prop-group fl" data-track={trackId}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="label">Colour & effects {count > 0 && <span className="fl-badge">{count}</span>}</div>
        <div className="row" style={{ gap: 2 }}>
          <button className="btn ghost icon xs" title="Copy filters" onClick={() => st().copyFilters(clip.id)} disabled={!count}><Icon name="copy" size={12} /></button>
          <button className="btn ghost xs" title="Paste filters onto this clip" onClick={() => st().pasteFilters([clip.id])} disabled={!clipboard}>Paste</button>
          <button className="btn ghost xs" title="Give every other clip these filters" onClick={applyToAll} disabled={!count}>All</button>
          <button className="btn ghost xs danger" title="Remove all filters from this clip" onClick={() => set([])} disabled={!count}>Reset</button>
        </div>
      </div>

      {section("presets", "Presets", preset ? PRESETS[preset.name]?.label ?? preset.name : undefined)}
      {open.presets && (
        <div className="fl-body">
          <div className="tr-grid">
            <button className={`btn sm ${!preset ? "toggle on" : ""}`} onClick={() => set(setPreset(filters, null))}>None</button>
            {PRESET_ORDER.map((k) => (
              <button key={k} className={`btn sm ${preset?.name === k ? "toggle on" : ""}`} onClick={() => set(setPreset(filters, k))}>
                {PRESETS[k].label}
              </button>
            ))}
          </div>
          {preset && (
            <>
              <Slider label="Intensity" value={preset.intensity} min={0} max={1} step={0.01} fmt={(v) => `${Math.round(v * 100)}%`}
                onChange={(v, fin) => set(setPreset(filters, preset.name, { intensity: v }), fin)} />
              {!PRESETS[preset.name]?.mono && (
                <label className="row fl-check">
                  <input type="checkbox" checked={(preset.params as Record<string, unknown>)?.skinProtect !== false}
                    onChange={(e) => set(setPreset(filters, preset.name, {}, { skinProtect: e.target.checked }))} />
                  Protect skin tones
                </label>
              )}
            </>
          )}
          {mine.length > 0 && <div className="faint" style={{ fontSize: 11, marginTop: 6 }}>My presets</div>}
          <div className="tr-grid">
            {mine.map((p) => (
              <span key={p.id} className="row" style={{ gap: 0 }}>
                <button className="btn sm" onClick={() => set(withNewIds(p.filters))} title="Replace this clip's filters">{p.name}</button>
                <button className="btn ghost icon xs" title="Delete preset"
                  onClick={() => api.deletePreset(p.id).then(refresh, (e) => toastError("Delete failed", e))}><Icon name="x" size={10} /></button>
              </span>
            ))}
          </div>
          {saving === null ? (
            <button className="btn sm ghost" disabled={!count} onClick={() => setSaving("")}>+ Save these filters as my preset</button>
          ) : (
            <div className="row">
              <input className="input" autoFocus placeholder="Preset name" value={saving} style={{ flex: 1, height: 24 }}
                onChange={(e) => setSaving(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setSaving(null);
                  if (e.key === "Enter" && saving.trim())
                    api.savePreset(saving.trim(), filters).then(() => { setSaving(null); refresh(); toast({ kind: "success", title: "Preset saved" }); },
                      (er) => toastError("Could not save preset", er));
                }} />
              <button className="btn sm" onClick={() => setSaving(null)}>Cancel</button>
            </div>
          )}
        </div>
      )}

      {section("lut", "LUT", lut ? lut.name || "LUT" : undefined)}
      {open.lut && (
        <div className="fl-body">
          <div className="row">
            <select className="input" style={{ flex: 1, height: 26 }} value={String((lut?.params as Record<string, unknown>)?.lut ?? "")}
              onChange={(e) => {
                const l = luts.find((x) => x.id === e.target.value);
                set(setLut(filters, l ? l.id : null, l?.name ?? ""));
              }}>
              <option value="">None</option>
              {luts.map((l) => <option key={l.id} value={l.id}>{l.name}{l.kind === "user" ? " (imported)" : ""}</option>)}
            </select>
            <button className="btn sm" onClick={() => fileRef.current?.click()} title="Import a .cube LUT">Import…</button>
            <input ref={fileRef} type="file" accept=".cube" hidden onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) api.importLut(f).then((l) => { refresh(); set(setLut(filters, l.id, l.name)); toast({ kind: "success", title: `Imported ${l.name}` }); },
                (er) => toastError("LUT import failed", er));
            }} />
          </div>
          {lut && (
            <>
              <Slider label="Intensity" value={lut.intensity} min={0} max={1} step={0.01} fmt={(v) => `${Math.round(v * 100)}%`}
                onChange={(v, fin) => set(setLut(filters, String((lut.params as Record<string, unknown>).lut), "", { intensity: v }), fin)} />
              <label className="row fl-check">
                <input type="checkbox" checked={(lut.params as Record<string, unknown>)?.skinProtect !== false}
                  onChange={(e) => set(setLut(filters, String((lut.params as Record<string, unknown>).lut), "", {}, { skinProtect: e.target.checked }))} />
                Protect skin tones
              </label>
            </>
          )}
        </div>
      )}

      {section("adjust", "Adjust", Object.values(adj).some((v) => Number(v)) ? "on" : undefined)}
      {open.adjust && (
        <div className="fl-body">
          {ADJUST_UI.map((g) => (
            <div key={g.group}>
              <div className="faint" style={{ fontSize: 11, margin: "4px 0 2px" }}>{g.group}</div>
              {g.keys.map((k) => (
                <Slider key={k.key} label={k.label} value={Number(adj[k.key] ?? 0)} min={k.min} max={k.max} step={k.step}
                  onChange={(v, fin) => set(setAdjust(filters, k.key, v), fin)} />
              ))}
            </div>
          ))}
        </div>
      )}

      {section("effects", "Effects", EFFECT_UI.filter((e) => effectOf(filters, e.key)).length ? String(EFFECT_UI.filter((e) => effectOf(filters, e.key)).length) : undefined)}
      {open.effects && (
        <div className="fl-body">
          {EFFECT_UI.map((e) => {
            const f = effectOf(filters, e.key as EffectKey);
            const pr = (f?.params ?? {}) as Record<string, number>;
            return (
              <div key={e.key} className="fl-effect">
                <label className="row fl-check">
                  <input type="checkbox" checked={!!f} onChange={(ev) => set(setEffect(filters, e.key, ev.target.checked ? {} : null))} />
                  {e.label}
                </label>
                {f && (
                  <>
                    <Slider label="Amount" value={pr.amount ?? 0} min={0} max={1} step={0.01} fmt={(v) => `${Math.round(v * 100)}%`}
                      onChange={(v, fin) => set(setEffect(filters, e.key, { amount: v }), fin)} />
                    {e.extra?.map((x) => (
                      <Slider key={x.key} label={x.label} value={pr[x.key] ?? 0} min={x.min} max={x.max} step={x.step}
                        onChange={(v, fin) => set(setEffect(filters, e.key, { [x.key]: v }), fin)} />
                    ))}
                  </>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
