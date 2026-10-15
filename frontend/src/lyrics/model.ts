// Song lyrics: the dialog's mode logic and the Tap-to-sync model (no AI). Times are timeline seconds.
import { clipDuration } from "../motion/motion";
import type { TimedWord } from "../text/edit";
import type { Clip, Project } from "../types";

export type LyricsMode = "auto" | "paste" | "lookup" | "tap";
export type Source = "speech" | "lyrics";

export interface MusicClipChoice { clip: Clip; label: string; skip: string | null }

/** The music clips that can carry lyrics, and the default one: the clip under the playhead, else the first. */
export function musicClips(p: Project, names: Record<string, string>, playhead: number): { list: MusicClipChoice[]; defaultId: string | null } {
  const list: MusicClipChoice[] = [];
  for (const t of p.tracks) {
    if (t.type !== "music" || t.muted) continue;
    for (const c of t.clips) {
      if (!c.mediaId) continue;
      const skip = c.reverse || c.freezeFrame != null || c.speedRamp ? "reversed, frozen or speed-ramped: its lyrics can't be timed" : null;
      list.push({ clip: c, label: `${names[c.mediaId] ?? "song"} (${c.start.toFixed(1)}-${(c.start + clipDuration(c)).toFixed(1)} s)`, skip });
    }
  }
  const under = list.find((x) => !x.skip && playhead >= x.clip.start && playhead < x.clip.start + clipDuration(x.clip));
  return { list, defaultId: (under ?? list.find((x) => !x.skip))?.clip.id ?? null };
}

/** What the dialog's main button does / whether it can run. */
export function canRun(source: Source, mode: LyricsMode, opts: { hasClip: boolean; text: string; taps: number; lookupId?: number | null; lrc?: string }): { ok: boolean; why?: string } {
  if (source === "speech") return { ok: true };
  if (!opts.hasClip) return { ok: false, why: "Add the song on a music track first." };
  if (mode === "paste" && !opts.text.trim()) return { ok: false, why: "Paste the lyrics (one line per caption)." };
  if (mode === "lookup" && !opts.lookupId && !(opts.lrc ?? "").trim()) return { ok: false, why: "Search and pick synced lyrics, or paste LRC text." };
  if (mode === "tap" && !opts.taps) return { ok: false, why: "Play the song and tap Space at the start of each line." };
  return { ok: true };
}

/** After an Auto run found no vocals, these are offered instead. */
export const instrumentalAlternatives: LyricsMode[] = ["paste", "tap"];

/** The dialog's vocal-presence strip: one cell per second of the clip's range, on the timeline. */
export interface PresenceCell { t0: number; t1: number; p: number; level: "none" | "low" | "mid" | "high" }

export function presenceCells(perSecond: number[], clip: { start: number; speed?: number }): PresenceCell[] {
  const sp = clip.speed || 1;
  return perSecond.map((p, k) => ({
    t0: +(clip.start + k / sp).toFixed(3), t1: +(clip.start + (k + 1) / sp).toFixed(3), p,
    level: p < 0.1 ? "none" : p < 0.4 ? "low" : p < 0.75 ? "mid" : "high",
  }));
}

/** "vocals heard in 19 of 24 s" style summary. */
export function presenceSummary(perSecond: number[]): string {
  const voiced = perSecond.filter((p) => p >= 0.4).length;
  return `vocals in ${voiced} of ${perSecond.length} s`;
}

export function splitLyrics(text: string): string[][] {
  return text.replace(/\r/g, "").split("\n").map((l) => l.trim().split(/\s+/).filter(Boolean)).filter((l) => l.length);
}

// ---------------------------------------------------------------- tap to sync
export interface Tap { line: number; word: number; t: number }

export interface TapState { lines: string[][]; taps: Tap[] }

export const newTapState = (text: string): TapState => ({ lines: splitLyrics(text), taps: [] });

/** The next tap target: Space = the start of the next line; Shift+Space = the next word of the current line
 *  (or the next line when the current one has no words left). */
export function nextTarget(s: TapState, word: boolean): { line: number; word: number } | null {
  const last = s.taps[s.taps.length - 1];
  if (!last) return s.lines.length ? { line: 0, word: 0 } : null;
  if (word && last.word + 1 < s.lines[last.line].length) return { line: last.line, word: last.word + 1 };
  return last.line + 1 < s.lines.length ? { line: last.line + 1, word: 0 } : null;
}

/** The nearest beat within `tol` seconds (80 ms), else the time itself. */
export function snapToBeat(t: number, beats: number[], tol = 0.08): number {
  let best = t, d = tol + 1e-9;
  for (const b of beats) if (Math.abs(b - t) <= d) { best = b; d = Math.abs(b - t); }
  return best;
}

export function tap(s: TapState, t: number, word: boolean, beats: number[] | null = null): TapState {
  const tg = nextTarget(s, word);
  if (!tg) return s;
  const tt = beats ? snapToBeat(t, beats) : t;
  const prev = s.taps[s.taps.length - 1];
  return { ...s, taps: [...s.taps, { ...tg, t: Math.max(tt, prev ? prev.t + 0.02 : tt) }] };
}

export const undoTap = (s: TapState): TapState => ({ ...s, taps: s.taps.slice(0, -1) });

/** Timed lines from the taps: tapped words start on their tap; the others are spread evenly up to the next tap;
 *  each word lasts until the next one; a line ends where the next line starts (the last at `end`). Lines that were
 *  never tapped are left out. */
export function tapLines(s: TapState, end: number): TimedWord[][] {
  const lineStarts = new Map<number, number>();
  for (const tp of s.taps) if (tp.word === 0) lineStarts.set(tp.line, tp.t);
  const tapped = [...lineStarts.keys()].sort((a, b) => a - b);
  const out: TimedWord[][] = [];
  tapped.forEach((li, k) => {
    const words = s.lines[li];
    const lineEnd = k + 1 < tapped.length ? lineStarts.get(tapped[k + 1])! : Math.max(end, lineStarts.get(li)! + 0.3 * words.length);
    const fixed = new Map<number, number>();
    for (const tp of s.taps) if (tp.line === li) fixed.set(tp.word, tp.t);
    const starts: number[] = new Array(words.length);
    const anchors = [...fixed.keys()].sort((a, b) => a - b);
    for (let a = 0; a < anchors.length; a++) {
      const w0 = anchors[a], t0 = fixed.get(w0)!;
      const w1 = a + 1 < anchors.length ? anchors[a + 1] : words.length;
      const t1 = a + 1 < anchors.length ? fixed.get(w1)! : lineEnd;
      for (let w = w0; w < w1; w++) starts[w] = t0 + ((t1 - t0) * (w - w0)) / (w1 - w0);
    }
    out.push(words.map((text, w) => ({
      text, start: +starts[w].toFixed(3), end: +(w + 1 < words.length ? starts[w + 1] : lineEnd).toFixed(3),
    })));
  });
  return out;
}
