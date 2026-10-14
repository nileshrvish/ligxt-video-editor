// Auto-edit drafts (backend/app/autoedit.py): types and the review-screen edits (swap, lock, exclude).

export type StyleId = "energetic" | "cinematic" | "chill";
export const STYLES: { id: StyleId; label: string; blurb: string }[] = [
  { id: "energetic", label: "Energetic", blurb: "Quick cuts, punch-ins, a slow-mo slam on the drop (~18 s)" },
  { id: "cinematic", label: "Cinematic", blurb: "Longer, steadier shots, push-ins, fades, text behind the subject (~24 s)" },
  { id: "chill", label: "Chill", blurb: "Calm shots, soft slow motion, dissolves, golden grade (~24 s)" },
];

export interface Pick {
  mediaId: string; shot: number; in: number; out: number; kind: string; thumb: string; name: string;
  score: number; reasons: string[]; locked?: boolean;
}

export interface Slot {
  i: number; section: string; b0: number; b1: number; start: number; dur: number; speed: number;
  pick: Pick | null; alternatives: Pick[];
}

export interface Draft {
  style: StyleId; styleLabel: string; template: string | null; media: string[]; exclude: string[]; music: string;
  musicStart: number; length: number; dropAt: number; slots: Slot[]; aspect: string; texts: Record<string, string>;
  shotsUsed: number; shotsAvailable: number;
}

export type Locks = Record<string, { mediaId: string; shot: number; in: number }>;

/** The locks to keep when regenerating: every locked pick. */
export function locksOf(d: Draft): Locks {
  const out: Locks = {};
  for (const s of d.slots) if (s.pick?.locked) out[String(s.i)] = { mediaId: s.pick.mediaId, shot: s.pick.shot, in: s.pick.in };
  return out;
}

/** Lock or unlock the shot in slot i. */
export function toggleLock(d: Draft, i: number): Draft {
  return { ...d, slots: d.slots.map((s) => (s.i === i && s.pick ? { ...s, pick: { ...s.pick, locked: !s.pick.locked } } : s)) };
}

/** Put an alternative into slot i (it becomes locked, so regenerating keeps it); the old pick joins the alternatives. */
export function swap(d: Draft, i: number, alt: Pick): Draft {
  return {
    ...d,
    slots: d.slots.map((s) => {
      if (s.i !== i) return s;
      const rest = s.alternatives.filter((a) => !(a.mediaId === alt.mediaId && a.shot === alt.shot));
      return { ...s, pick: { ...alt, locked: true }, alternatives: s.pick ? [{ ...s.pick, locked: false }, ...rest] : rest };
    }),
  };
}

/** Exclude a clip: it disappears from the draft (locks on it are dropped). */
export function excludeClip(d: Draft, mediaId: string): { exclude: string[]; locks: Locks } {
  const locks = locksOf(d);
  for (const k of Object.keys(locks)) if (locks[k].mediaId === mediaId) delete locks[k];
  return { exclude: [...new Set([...d.exclude, mediaId])], locks };
}

export const fmtScore = (v: number) => (Math.round(v * 100) / 100).toFixed(2);
