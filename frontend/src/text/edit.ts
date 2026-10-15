// Text clip editing: new clips, presets, content changes (keeping caption word timings), captions.
import type { Clip, Project } from "../types";
import { addTrack } from "../timeline/ops";
import { clipDuration, deepClone, uid } from "../util";
import { mergeSpec, normalize, TEXT_PRESETS, type TextPreset, type TextSpec } from "./anim";


export function newTextClip(start: number, duration: number, spec: Partial<TextSpec>, pos = { x: 0, y: 0 }): Clip {
  return {
    id: uid(), mediaId: null, start, inPoint: 0, outPoint: duration, speed: 1, reverse: false, freezeFrame: null,
    volume: 1, muted: false, fadeIn: 0, fadeOut: 0, fit: null,
    transform: { x: pos.x, y: pos.y, scale: 1, rotation: 0, flipH: false, flipV: false, opacity: 1, crop: { left: 0, top: 0, right: 0, bottom: 0 } },
    keyframes: {}, filters: [], text: spec as Record<string, unknown>,
  };
}

export const presetById = (id: string): TextPreset | undefined => TEXT_PRESETS.find((p) => p.id === id);

/** Apply a style (preset or saved) but keep the clip's own words and timings. */
export function applyStyle(c: Clip, style: Partial<TextSpec>, position?: { x: number; y: number }): void {
  const cur = (c.text ?? {}) as Partial<TextSpec>;
  const keep = { content: cur.content ?? "Your text", words: cur.words ?? null };
  c.text = { ...mergeSpec(deepClone(style) as Partial<TextSpec>, {}), ...keep } as Record<string, unknown>;
  if (position) {
    c.transform.x = position.x;
    c.transform.y = position.y;
  }
}

/** Patch the spec (deep merge for nested groups). */
export function patchText(c: Clip, patch: Partial<TextSpec> | Record<string, unknown>): void {
  c.text = mergeSpec((c.text ?? {}) as Record<string, unknown>, patch);
}

const tokens = (s: string) => s.split(/\s+/).filter(Boolean);

/** Change the text; caption word timings follow: the same word count keeps every timing, otherwise
 *  the clip's time is shared out by word length. */
export function setContent(c: Clip, content: string): void {
  const spec = (c.text ?? {}) as Partial<TextSpec>;
  const old = spec.words;
  let words: TextSpec["words"] = old ?? null;
  if (old && old.length) {
    const toks = tokens(content);
    if (toks.length === old.length) words = old.map((w, i) => ({ ...w, text: toks[i] }));
    else if (toks.length) {
      const t0 = old[0].start, t1 = Math.max(old[old.length - 1].end, t0 + 0.1);
      const total = toks.reduce((n, w) => n + w.length + 1, 0);
      let t = t0;
      words = toks.map((w) => {
        const d = ((t1 - t0) * (w.length + 1)) / total;
        const out = { text: w, start: +t.toFixed(3), end: +(t + d).toFixed(3) };
        t += d;
        return out;
      });
    } else words = [];
  }
  c.text = { ...spec, content, words } as Record<string, unknown>;
}

/** After ops.splitClip on a text clip: give each half its own words, and no out/in animation at the cut. */
export function splitText(left: Clip, right: Clip, offset: number): void {
  const ls = (left.text ?? {}) as Partial<TextSpec>;
  const rs = (right.text ?? {}) as Partial<TextSpec>;
  if (ls.words?.length) {
    const lw = ls.words.filter((w) => w.start < offset);
    const rw = ls.words.filter((w) => w.start >= offset).map((w) => ({ ...w, start: +(w.start - offset).toFixed(3), end: +(w.end - offset).toFixed(3) }));
    left.text = { ...ls, words: lw, content: lw.map((w) => w.text).join(" ") };
    right.text = { ...rs, words: rw, content: rw.map((w) => w.text).join(" ") };
  }
  left.text = mergeSpec(left.text as Record<string, unknown>, { animOut: { type: "none" } });
  right.text = mergeSpec(right.text as Record<string, unknown>, { animIn: { type: "none" } });
}

// ---------------------------------------------------------------- captions
export interface TimedWord { text: string; start: number; end: number }

/** Group transcribed words (timeline seconds) into short caption segments: at most `maxWords`
 *  words / `maxChars` characters, broken at sentence ends and pauses. */
export function segmentWords(words: TimedWord[], maxWords = 4, maxChars = 22, maxDur = 2.4, pause = 0.45): TimedWord[][] {
  const segs: TimedWord[][] = [];
  let cur: TimedWord[] = [];
  const chars = (ws: TimedWord[]) => ws.reduce((n, w) => n + w.text.length + 1, -1);
  for (const w of words) {
    const prev = cur[cur.length - 1];
    const brk = prev && (
      cur.length >= maxWords || chars([...cur, w]) > maxChars || w.end - cur[0].start > maxDur
      || w.start - prev.end > pause || /[.!?…]$/.test(prev.text)
    );
    if (brk) { segs.push(cur); cur = []; }
    cur.push(w);
  }
  if (cur.length) segs.push(cur);
  return segs;
}

/** Caption clips for transcribed words on a (new) caption track. Returns the clip ids. */
export function addCaptions(p: Project, words: TimedWord[], style: Partial<TextSpec>, position: { x: number; y: number }): { trackId: string; ids: string[] } {
  const trackId = addTrack(p, "text");
  const track = p.tracks.find((t) => t.id === trackId)!;
  track.name = "Captions";
  const segs = segmentWords(words.map((w) => ({ ...w, text: w.text.trim() })).filter((w) => w.text));
  const ids: string[] = [];
  segs.forEach((seg, i) => {
    const start = seg[0].start;
    const next = segs[i + 1]?.[0].start ?? Infinity;
    const end = Math.max(start + 0.3, Math.min(seg[seg.length - 1].end + 0.25, next));
    const spec: Partial<TextSpec> = {
      ...deepClone(style), content: seg.map((w) => w.text).join(" "),
      words: seg.map((w) => ({ text: w.text, start: +(w.start - start).toFixed(3), end: +(w.end - start).toFixed(3) })),
      animIn: { type: "none", duration: 0.2, easing: "out" }, animOut: { type: "none", duration: 0.2, easing: "in" },
    };
    const c = newTextClip(+start.toFixed(3), +(end - start).toFixed(3), spec, position);
    track.clips.push(c);
    ids.push(c.id);
  });
  return { trackId, ids };
}

export type LyricsLayout = "original" | "translation" | "two";
export interface LyricsTranslation { lines: string[]; from: string; to: string; model: string; script?: "native" | "roman" }
export const TRANSLATION_SCALE = 0.62; // the second (translated) line's size against the lyrics

/** Line timing: from the first word to the last word + 0.25 s, never past the next line, at least 0.3 s. */
export function lineTimes(lines: TimedWord[][]): { start: number; end: number }[] {
  return lines.map((seg, i) => {
    const start = seg[0].start;
    const next = lines[i + 1]?.[0].start ?? Infinity;
    return { start, end: Math.max(start + 0.3, Math.min(seg[seg.length - 1].end + 0.25, next)) };
  });
}

/** Where the translated line sits in the two-line layout: just below the lyrics line. */
export function translationPosition(style: Partial<TextSpec>, position: { x: number; y: number }): { x: number; y: number } {
  const size = Number(style.size ?? 72);
  const lh = Number(style.lineHeight ?? 1.1);
  return { x: position.x, y: Math.round(position.y + size * lh * 0.5 + size * TRANSLATION_SCALE * lh * 0.5 + size * 0.18) };
}

/** Song lyrics: one caption clip per line (timeline seconds) on a new "Lyrics" track, in the caption `words` format,
 *  so every highlight style works. Lines from Auto mode are grouped with segmentWords first.
 *  With a translation (Phase 13b): layout "translation" puts the translated lines on the Lyrics track instead, "two"
 *  adds them on a "Lyrics (translation)" track below the lyrics, smaller. Translated clips use the line's timing (no
 *  word timings: they fade in as a whole line) and remember their source line, so one line can be re-translated.
 *  Returns the lyrics track and every clip id. */
export function addLyrics(p: Project, lines: TimedWord[][], style: Partial<TextSpec>, position: { x: number; y: number },
  tr?: LyricsTranslation | null, layout: LyricsLayout = "original"): { trackId: string; ids: string[]; translationTrackId?: string } {
  const clean = lines.map((l) => l.map((w) => ({ ...w, text: w.text.trim() })).filter((w) => w.text)).filter((l) => l.length);
  const keep = lines.map((l) => l.some((w) => w.text.trim()));  // translations follow the original lines (empty ones dropped)
  const trLines = tr ? tr.lines.filter((_, i) => keep[i] !== false) : [];
  const times = lineTimes(clean);
  const mode: LyricsLayout = tr ? layout : "original";
  const trackId = addTrack(p, "text");
  const track = p.tracks.find((t) => t.id === trackId)!;
  track.name = "Lyrics";
  const ids: string[] = [];
  const translated = (i: number, st: Partial<TextSpec>, pos: { x: number; y: number }) => {
    const { start, end } = times[i];
    const spec: Partial<TextSpec> & { translation?: object } = {
      ...deepClone(st), content: trLines[i] ?? "",
      highlight: { ...(st.highlight ?? {}), style: "none" } as TextSpec["highlight"],
      animIn: { type: "fade", duration: 0.2, easing: "out" }, animOut: { type: "fade", duration: 0.15, easing: "in" },
      translation: { source: clean[i].map((w) => w.text).join(" "), from: tr!.from, to: tr!.to, model: tr!.model, script: tr!.script ?? "native" },
    };
    return newTextClip(+start.toFixed(3), +(end - start).toFixed(3), spec as Partial<TextSpec>, pos);
  };
  clean.forEach((seg, i) => {
    const { start, end } = times[i];
    if (mode === "translation") {
      const c = translated(i, style, position);
      track.clips.push(c);
      ids.push(c.id);
      return;
    }
    const spec: Partial<TextSpec> = {
      ...deepClone(style), content: seg.map((w) => w.text).join(" "),
      words: seg.map((w) => ({ text: w.text, start: +(w.start - start).toFixed(3), end: +(w.end - start).toFixed(3) })),
      animIn: { type: "none", duration: 0.2, easing: "out" }, animOut: { type: "none", duration: 0.2, easing: "in" },
    };
    const c = newTextClip(+start.toFixed(3), +(end - start).toFixed(3), spec, position);
    track.clips.push(c);
    ids.push(c.id);
  });
  let translationTrackId: string | undefined;
  if (mode === "two") {
    translationTrackId = addTrack(p, "text");
    const t2 = p.tracks.find((t) => t.id === translationTrackId)!;
    t2.name = "Lyrics (translation)";
    const small = { ...deepClone(style), size: Math.round(Number(style.size ?? 72) * TRANSLATION_SCALE) };
    const pos2 = translationPosition(style, position);
    clean.forEach((_, i) => {
      const c = translated(i, small, pos2);
      t2.clips.push(c);
      ids.push(c.id);
    });
  }
  return { trackId, ids, translationTrackId };
}

/** Rest-state spec (normalised) for UI reads. */
export const specOf = (c: Clip): TextSpec => normalize(c.text);
export const textDuration = (c: Clip) => clipDuration(c);

/** One word per beat (or every half / second beat) from the first beat at or after the clip's start:
 *  words appear exactly on the music (used with the "reveal + pop" highlight). Returns the word count. */
export function syncWordsToBeats(c: Clip, beats: number[], every: 0.5 | 1 | 2 = 1): number {
  const spec = (c.text ?? {}) as Partial<TextSpec>;
  const toks = tokens(String(spec.content ?? ""));
  if (!toks.length || beats.length < 2) return 0;
  const sorted = [...beats].sort((a, b) => a - b);
  let grid: number[] = sorted;
  if (every === 0.5) grid = sorted.flatMap((b, i) => (i + 1 < sorted.length ? [b, (b + sorted[i + 1]) / 2] : [b]));
  if (every === 2) grid = sorted.filter((_, i) => i % 2 === 0);
  const i0 = grid.findIndex((b) => b >= c.start - 0.02);
  if (i0 < 0) return 0;
  const end = c.start + clipDuration(c);
  const words = toks.map((text, k) => {
    const s = grid[i0 + k] ?? grid[grid.length - 1] + 0.5 * (i0 + k - grid.length + 1);
    const e = grid[i0 + k + 1] ?? Math.max(s + 0.3, end);
    return { text, start: +Math.max(0, s - c.start).toFixed(3), end: +Math.max(0, e - c.start).toFixed(3) };
  });
  const hl = spec.highlight?.style;
  c.text = { ...spec, words, highlight: { color: spec.highlight?.color ?? "#ffd34d", style: hl && hl !== "none" ? hl : "revealpop" } } as Record<string, unknown>;
  return words.length;
}
