// Helpers for editing a clip's filter stack immutably. The UI keeps at most one adjust filter, one
// preset, one LUT and one filter per effect (the data model and the renderers allow more).
import type { Filter } from "../types";
import { uid } from "../util";
import { EFFECT_DEFAULTS, type EffectKey } from "./math";

export const ADJUST_UI: { group: string; keys: { key: string; label: string; min: number; max: number; step: number }[] }[] = [
  { group: "Light", keys: [
    { key: "exposure", label: "Exposure", min: -3, max: 3, step: 0.05 },
    { key: "brightness", label: "Brightness", min: -100, max: 100, step: 1 },
    { key: "contrast", label: "Contrast", min: -100, max: 100, step: 1 },
    { key: "highlights", label: "Highlights", min: -100, max: 100, step: 1 },
    { key: "shadows", label: "Shadows", min: -100, max: 100, step: 1 },
    { key: "whites", label: "Whites", min: -100, max: 100, step: 1 },
    { key: "blacks", label: "Blacks", min: -100, max: 100, step: 1 },
  ] },
  { group: "Colour", keys: [
    { key: "temperature", label: "Temperature", min: -100, max: 100, step: 1 },
    { key: "tint", label: "Tint", min: -100, max: 100, step: 1 },
    { key: "saturation", label: "Saturation", min: -100, max: 100, step: 1 },
    { key: "vibrance", label: "Vibrance", min: -100, max: 100, step: 1 },
  ] },
  { group: "Detail", keys: [
    { key: "sharpen", label: "Sharpen", min: 0, max: 100, step: 1 },
    { key: "blur", label: "Blur", min: 0, max: 100, step: 1 },
  ] },
];

export const EFFECT_UI: { key: EffectKey; label: string; extra?: { key: string; label: string; min: number; max: number; step: number }[] }[] = [
  { key: "grain", label: "Film grain", extra: [{ key: "size", label: "Size", min: 1, max: 4, step: 0.1 }] },
  { key: "vignette", label: "Vignette", extra: [{ key: "feather", label: "Feather", min: 0, max: 1, step: 0.05 }] },
  { key: "lightleak", label: "Light leaks", extra: [{ key: "speed", label: "Speed", min: 0.05, max: 1.5, step: 0.05 }] },
  { key: "vhs", label: "VHS look" },
  { key: "glow", label: "Glow / bloom", extra: [
    { key: "threshold", label: "Threshold", min: 0.3, max: 0.95, step: 0.01 },
    { key: "radius", label: "Radius", min: 2, max: 40, step: 1 },
  ] },
  { key: "chroma", label: "Chromatic aberration" },
];

const clone = (f: Filter[]) => f.map((x) => ({ ...x, params: { ...(x.params ?? {}) } }));

export function adjustOf(filters: Filter[]): Record<string, number> {
  return (filters.find((f) => f.type === "adjust")?.params ?? {}) as Record<string, number>;
}

export function setAdjust(filters: Filter[], key: string, value: number): Filter[] {
  const out = clone(filters);
  let f = out.find((x) => x.type === "adjust");
  if (!f) {
    f = { id: uid(), type: "adjust", name: "", intensity: 1, params: {}, enabled: true };
    out.unshift(f);
  }
  (f.params as Record<string, number>)[key] = value;
  return out;
}

export function presetOf(filters: Filter[]): Filter | undefined {
  return filters.find((f) => f.type === "preset");
}

export function setPreset(filters: Filter[], name: string | null, patch: Partial<Filter> = {}, params: Record<string, unknown> = {}): Filter[] {
  const out = clone(filters);
  const i = out.findIndex((f) => f.type === "preset");
  if (name === null) return i >= 0 ? out.filter((_, j) => j !== i) : out;
  const prev = i >= 0 ? out[i] : undefined;
  const f: Filter = {
    id: prev?.id ?? uid(), type: "preset", name, intensity: prev?.intensity ?? 1,
    params: { skinProtect: true, ...(prev?.params ?? {}), ...params }, enabled: true, ...patch,
  };
  if (i >= 0) out[i] = f;
  else out.push(f);
  return out;
}

export function lutOf(filters: Filter[]): Filter | undefined {
  return filters.find((f) => f.type === "lut");
}

export function setLut(filters: Filter[], lut: string | null, name = "", patch: Partial<Filter> = {}, params: Record<string, unknown> = {}): Filter[] {
  const out = clone(filters);
  const i = out.findIndex((f) => f.type === "lut");
  if (lut === null) return i >= 0 ? out.filter((_, j) => j !== i) : out;
  const prev = i >= 0 ? out[i] : undefined;
  const f: Filter = {
    id: prev?.id ?? uid(), type: "lut", name: name || prev?.name || "", intensity: prev?.intensity ?? 1,
    params: { skinProtect: true, ...(prev?.params ?? {}), lut, ...params }, enabled: true, ...patch,
  };
  if (i >= 0) out[i] = f;
  else out.push(f);
  return out;
}

export function effectOf(filters: Filter[], key: EffectKey): Filter | undefined {
  return filters.find((f) => f.type === "effect" && f.name === key);
}

export function setEffect(filters: Filter[], key: EffectKey, params: Record<string, number> | null): Filter[] {
  const out = clone(filters);
  const i = out.findIndex((f) => f.type === "effect" && f.name === key);
  if (params === null) return i >= 0 ? out.filter((_, j) => j !== i) : out;
  const base = i >= 0 ? (out[i].params as Record<string, number>) : { ...EFFECT_DEFAULTS[key], amount: 0.4 };
  const f: Filter = { id: i >= 0 ? out[i].id : uid(), type: "effect", name: key, intensity: 1, params: { ...base, ...params }, enabled: true };
  if (i >= 0) out[i] = f;
  else out.push(f);
  return out;
}

/** Fresh ids so pasted / preset stacks don't share ids with their source. */
export const withNewIds = (filters: Filter[]) => clone(filters).map((f) => ({ ...f, id: uid() }));

export function selectiveOf(filters: Filter[]): Filter | undefined {
  return filters.find((f) => f.type === "selective");
}

/** Selective colour: keep one hue range, the rest black and white. `params` null removes it. */
export function setSelective(filters: Filter[], params: Record<string, number> | null, patch: Partial<Filter> = {}): Filter[] {
  const out = clone(filters);
  const i = out.findIndex((f) => f.type === "selective");
  if (params === null) return i >= 0 ? out.filter((_, j) => j !== i) : out;
  const prev = i >= 0 ? out[i] : undefined;
  const f: Filter = {
    id: prev?.id ?? uid(), type: "selective", name: "", intensity: prev?.intensity ?? 1,
    params: { hue: 25, range: 20, softness: 12, satLow: 0.08, satHigh: 0.2, ...(prev?.params ?? {}), ...params }, enabled: true, ...patch,
  };
  if (i >= 0) out[i] = f;
  else out.push(f);
  return out;
}
