// Canvas preview player.
//
// One master clock drives everything. Every clip near the playhead gets its own (detached) media
// element playing the proxy; elements are slaved to the clock (seeked when they drift) and video
// frames are composited onto a 2D canvas: background -> main track -> overlay tracks in track order.
// Audio from every audible clip goes through WebAudio gain nodes (volume > 100%, fades, track mute).
import { useEditor } from "../store/editor";
import type { Clip, MediaMeta, Project, Track } from "../types";
import { clipDuration, clipEnd, projectDuration } from "../util";
import { mainTransitionPlans, type TransitionPlan } from "../timeline/transitions";
import { fadeGain, layerGeometry } from "./geometry";
import { TransitionRenderer } from "./transitions";
import { FilterRenderer, type GradeContext } from "../grade/renderer";
import { needsGrade } from "../grade/math";
import { duckGain, duckRegions, hasMotion, rectMatrix, roundedRect, rnd, srcTime, transformAt, valueAt } from "../motion/motion";
import { loadWave, type Wave } from "./waves";

const PREROLL = 1.5; // seconds: load + seek upcoming clips ahead of time
const KEEP_IDLE_MS = 4000; // drop elements unused for this long
const MAX_AUDIBLE_RATE = 2; // mute audio when shuttling faster than this (or in reverse)

interface Active {
  /** Set on main-track clips taking part in a transition right now (A = outgoing, B = incoming). */
  tr?: { plan: TransitionPlan; side: "A" | "B"; p: number };
  clip: Clip;
  track: Track;
  /** null for adjustment layers (no media; they grade everything below them). */
  media: MediaMeta;
  adjust?: boolean;
  isMain: boolean;
  visual: boolean;
  audible: boolean;
  preroll: boolean;
}

interface Slot {
  el: HTMLVideoElement | HTMLAudioElement;
  url: string;
  gain: GainNode | null;
  lastUsed: number;
}

export function mediaUrl(m: MediaMeta): string {
  return m.proxy && m.type !== "image" ? m.proxy : "/files/" + m.file;
}

export class PreviewEngine {
  private ctx: CanvasRenderingContext2D;
  private slots = new Map<string, Slot>(); // by clip id
  private images = new Map<string, HTMLImageElement>(); // by media id
  private audio: AudioContext | null = null;
  private master: GainNode | null = null;
  private analyser: AnalyserNode | null = null;
  private raf = 0;
  private dirty = true;
  private clock = { t0: 0, wall0: 0, rate: 0 };
  private lastEngineTime = -1;
  private unsub: () => void;
  /** Called with true while a frame is waiting for media to load or seek. */
  onBuffering: (b: boolean) => void = () => void 0;
  private buffering = false;
  private plansFor: { project: Project | null; media: Record<string, MediaMeta> | null; plans: Map<string, TransitionPlan> } =
    { project: null, media: null, plans: new Map() };
  private trRenderer: TransitionRenderer | null = null;
  private layerA = document.createElement("canvas");
  private layerB = document.createElement("canvas");
  private layerO = document.createElement("canvas");
  private layerAdj = document.createElement("canvas");
  private before = document.createElement("canvas");
  private work = document.createElement("canvas");
  /** Timeline time of the last frame that was fully composed and shown (for tests and tooling). */
  lastFrameT = -1;
  private fx = new FilterRenderer();
  /** Before/after split: fraction of the width showing the ungraded frame (null = off). */
  private compare: number | null = null;
  /** Speech regions for auto-ducking music (recomputed when the project or media change). */
  private duckFor: { project: Project | null; media: Record<string, MediaMeta> | null; regions: [number, number][] } =
    { project: null, media: null, regions: [] };
  private waves = new Map<string, Wave>(); // by waveform url, once loaded

  constructor(private canvas: HTMLCanvasElement) {
    this.ctx = canvas.getContext("2d", { alpha: false })!;
    this.unsub = useEditor.subscribe((s, prev) => {
      if (s.playing !== prev.playing || s.rate !== prev.rate) {
        if (s.playing) this.startClock(s.playhead, s.rate);
        else this.silence(); // stop immediately, don't wait for the next animation frame
        this.dirty = true;
      } else if (s.playhead !== prev.playhead && s.playhead !== this.lastEngineTime) {
        // Moved by the user (scrub, arrows, click) rather than by our clock.
        if (s.playing) this.startClock(s.playhead, s.rate);
        this.dirty = true;
      }
      if (s.project !== prev.project || s.media !== prev.media) this.dirty = true;
      if (s.compare !== prev.compare) {
        this.compare = s.compare;
        this.dirty = true;
      }
    });
    this.compare = useEditor.getState().compare;
    this.fx.onAsset = () => (this.dirty = true);
    this.raf = requestAnimationFrame(this.tick);
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.unsub();
    for (const slot of this.slots.values()) this.dispose(slot);
    this.slots.clear();
    this.audio?.close().catch(() => void 0);
  }

  invalidate() {
    this.dirty = true;
  }

  private silence() {
    for (const slot of this.slots.values()) {
      slot.el.pause();
      if (slot.gain && this.audio) {
        slot.gain.gain.cancelScheduledValues(this.audio.currentTime);
        slot.gain.gain.setValueAtTime(0, this.audio.currentTime);
      }
    }
  }

  /** Current output level (RMS, 0..1) of everything the preview is playing. */
  level(): number {
    if (!this.analyser) return 0;
    const buf = new Float32Array(this.analyser.fftSize);
    this.analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (const v of buf) sum += v * v;
    return Math.sqrt(sum / buf.length);
  }

  /** Diagnostics (used by browser tests): one entry per live media element. */
  debugState() {
    return [...this.slots.entries()].map(([clipId, s]) => ({
      clipId,
      paused: s.el.paused,
      time: +s.el.currentTime.toFixed(3),
      rate: s.el.playbackRate,
      gain: s.gain ? +s.gain.gain.value.toFixed(3) : null,
      ready: s.el.readyState,
    }));
  }

  private startClock(t: number, rate: number) {
    // Pressing play at the very end starts again from the beginning.
    const p = useEditor.getState().project;
    if (rate > 0 && p && t >= projectDuration(p) - 1e-3) {
      t = 0;
      this.lastEngineTime = 0;
      useEditor.getState().setPlayhead(0);
    }
    this.clock = { t0: t, wall0: performance.now(), rate };
    if (!this.audio) {
      try {
        this.audio = new AudioContext();
        this.master = this.audio.createGain();
        this.analyser = this.audio.createAnalyser();
        this.analyser.fftSize = 2048;
        this.master.connect(this.analyser).connect(this.audio.destination);
        for (const slot of this.slots.values()) this.connect(slot);
      } catch {
        this.audio = null;
      }
    }
    if (this.audio?.state === "suspended") this.audio.resume().catch(() => void 0);
  }

  private tick = (now: number) => {
    this.raf = requestAnimationFrame(this.tick);
    const s = useEditor.getState();
    const p = s.project;
    if (!p) return;
    const total = projectDuration(p);
    let t = s.playhead;
    if (s.playing) {
      // rAF timestamps can predate startClock(), so never let elapsed go negative.
      const elapsed = Math.max(0, now - this.clock.wall0) / 1000;
      t = this.clock.t0 + elapsed * this.clock.rate;
      const hitEnd = this.clock.rate > 0 && t >= total;
      const hitStart = this.clock.rate < 0 && t <= 0;
      if (hitEnd || hitStart) {
        t = Math.max(0, Math.min(total, t));
        this.lastEngineTime = t;
        s.setPlayhead(t);
        s.setPlaying(false);
      } else {
        this.lastEngineTime = t;
        s.setPlayhead(t);
      }
    }
    const active = this.collect(p, s.media, t);
    const speech = p.tracks.some((tr) => tr.type === "music" && tr.autoDuck?.enabled) ? this.speech(p, s.media) : [];
    this.sync(active, t, s.playing, s.playing ? this.clock.rate : 0, p.settings.fps, now, speech);
    if (s.playing || this.dirty) {
      const ok = this.draw(p, active, t);
      this.dirty = !ok;
      if (this.buffering !== !ok) {
        this.buffering = !ok;
        this.onBuffering(!ok);
      }
    }
    this.gc(now);
  };

  // ------------------------------------------------------------ what plays now
  /** Transition plans for the main track (recomputed only when the project or media change). */
  plans(p: Project, media: Record<string, MediaMeta>): Map<string, TransitionPlan> {
    const c = this.plansFor;
    if (c.project !== p || c.media !== media) {
      const dur = (id: string | null) => {
        const m = id ? media[id] : undefined;
        return !m || m.type === "image" ? Infinity : m.duration;
      };
      this.plansFor = { project: p, media, plans: mainTransitionPlans(p, dur) };
    }
    return this.plansFor.plans;
  }

  private collect(p: Project, media: Record<string, MediaMeta>, t: number): Active[] {
    const out: Active[] = [];
    const main = p.tracks.find((x) => x.type === "video");
    const plans = this.plans(p, media);
    const incoming = new Map<string, TransitionPlan>();
    for (const pl of plans.values()) incoming.set(pl.toId, pl);
    for (const track of p.tracks) {
      if (track.type === "text") continue; // Phase 6
      const isMain = track === main;
      for (const clip of track.clips) {
        if (clip.adjustment && !clip.mediaId) {
          if (track.type === "overlay" && !track.hidden && t >= clip.start && t < clipEnd(clip)) {
            out.push({ clip, track, media: null as unknown as MediaMeta, adjust: true, isMain: false, visual: true, audible: false, preroll: false });
          }
          continue;
        }
        const m = clip.mediaId ? media[clip.mediaId] : undefined;
        if (!m) continue;
        // Main-track clips extend into their transitions (handles beyond the in/out points).
        const inTr = isMain ? incoming.get(clip.id) : undefined;
        const outTr = isMain ? plans.get(clip.id) : undefined;
        const from = clip.start - (inTr?.half ?? 0);
        const to = clipEnd(clip) + (outTr?.half ?? 0);
        const inside = t >= from && t < to;
        const preroll = !inside && t >= from - PREROLL && t < from;
        if (!inside && !preroll) continue;
        const visual = (track.type === "video" || track.type === "overlay") && m.type !== "audio" && !track.hidden;
        const audible = m.hasAudio && m.type !== "image" && !track.muted && !clip.muted && clip.volume > 0;
        if (!visual && !audible) continue;
        const a: Active = { clip, track, media: m, isMain, visual, audible, preroll };
        if (inside && inTr && t < inTr.cut + inTr.half) a.tr = { plan: inTr, side: "B", p: (t - (inTr.cut - inTr.half)) / (2 * inTr.half) };
        if (inside && outTr && t >= outTr.cut - outTr.half) a.tr = { plan: outTr, side: "A", p: (t - (outTr.cut - outTr.half)) / (2 * outTr.half) };
        out.push(a);
      }
    }
    return out;
  }

  /** Speech-like regions on the timeline (main video + audio tracks, from the waveforms), the same
   *  rules as backend render.speech_regions. Waveforms load in the background; until then: none. */
  private speech(p: Project, media: Record<string, MediaMeta>): [number, number][] {
    const c = this.duckFor;
    if (c.project === p && c.media === media) return c.regions;
    const sources: [number, number, number[]][] = [];
    let missing = false;
    for (const tr of p.tracks) {
      if (tr.muted || (tr.type !== "video" && tr.type !== "audio")) continue;
      for (const clip of tr.clips) {
        const m = clip.mediaId ? media[clip.mediaId] : undefined;
        if (!m || !m.hasAudio || !m.waveform || clip.muted || clip.volume <= 0 || m.sfx) continue;
        const url = m.waveform;
        const wf = this.waves.get(url);
        if (!wf) {
          missing = true;
          loadWave(url).then((w) => {
            this.waves.set(url, w);
            this.duckFor = { project: null, media: null, regions: [] };
          }).catch(() => void 0);
          continue;
        }
        const D = clipDuration(clip);
        const vals: number[] = [];
        for (let i = 0; i < Math.floor(D * 100); i++) {
          const j = Math.floor(srcTime(clip, i / 100).src * (wf.peaksPerSecond || 100));
          vals.push(j >= 0 && j < wf.peaks.length ? wf.peaks[j] / 255 : 0);
        }
        sources.push([clip.start, clip.start + D, vals]);
      }
    }
    const regions = duckRegions(sources);
    this.duckFor = missing ? { project: null, media: null, regions } : { project: p, media, regions };
    return regions;
  }

  // ------------------------------------------------------------ media elements
  private slotFor(a: Active, now: number): Slot | null {
    if (a.media.type === "image") return null;
    let slot = this.slots.get(a.clip.id);
    const url = mediaUrl(a.media);
    if (slot && slot.url !== url) {
      this.dispose(slot);
      slot = undefined;
    }
    if (!slot) {
      const el = document.createElement(a.media.type === "audio" ? "audio" : "video") as HTMLVideoElement;
      el.preload = "auto";
      el.playsInline = true;
      el.crossOrigin = "anonymous";
      el.src = url;
      const mark = () => (this.dirty = true);
      el.addEventListener("seeked", mark);
      el.addEventListener("loadeddata", mark);
      el.addEventListener("canplay", mark);
      slot = { el, url, gain: null, lastUsed: now };
      this.connect(slot);
      this.slots.set(a.clip.id, slot);
    }
    slot.lastUsed = now;
    return slot;
  }

  private connect(slot: Slot) {
    if (!this.audio || slot.gain) return;
    try {
      const src = this.audio.createMediaElementSource(slot.el);
      slot.gain = this.audio.createGain();
      slot.gain.gain.value = 0;
      src.connect(slot.gain).connect(this.master ?? this.audio.destination);
    } catch {
      slot.el.muted = true; // could not route through WebAudio: keep it silent rather than wrong
    }
  }

  private dispose(slot: Slot) {
    slot.el.pause();
    slot.el.removeAttribute("src");
    slot.el.load();
    slot.gain?.disconnect();
  }

  private gc(now: number) {
    for (const [id, slot] of this.slots) {
      if (now - slot.lastUsed > KEEP_IDLE_MS) {
        this.dispose(slot);
        this.slots.delete(id);
      } else if (now - slot.lastUsed > 100 && !slot.el.paused) {
        slot.el.pause(); // left the playhead window
        if (slot.gain) slot.gain.gain.value = 0;
      }
    }
  }

  private image(m: MediaMeta): HTMLImageElement {
    let img = this.images.get(m.id);
    const url = m.proxy ?? m.thumbnail ?? "/files/" + m.file;
    if (!img || img.dataset.url !== url) {
      img = new Image();
      img.dataset.url = url;
      img.onload = () => (this.dirty = true);
      img.src = url;
      this.images.set(m.id, img);
    }
    return img;
  }

  private sync(active: Active[], t: number, playing: boolean, rate: number, fps: number, now: number, speech: [number, number][]) {
    for (const a of active) {
      if (a.adjust) continue;
      const slot = this.slotFor(a, now);
      if (!slot) continue;
      const el = slot.el;
      // Source time from the shared math (speed, ramps, reverse, freeze). Preroll: where its
      // (handle) footage starts.
      const tau = a.preroll ? -this.incomingHalf(a) : t - a.clip.start;
      const st = srcTime(a.clip, tau);
      let want = st.src;
      const speed = st.speed;
      // Past the footage (transition freeze): hold the first/last frame.
      if (a.media.type !== "image") want = Math.max(0, Math.min(want, Math.max(0, a.media.duration - 0.04)));
      // Reversed and frozen clips can't be played by a media element: they're stepped frame by
      // frame (seeked) and silent in the preview. The export plays reversed audio.
      const stepped = speed <= 0;
      const forward = playing && rate > 0 && !a.preroll && !stepped;
      if (forward) {
        el.playbackRate = Math.max(0.0625, Math.min(16, speed * rate));
        if (el.paused) {
          if (Math.abs(el.currentTime - want) > 0.05) el.currentTime = want;
          el.play().catch(() => void 0);
        } else if (Math.abs(el.currentTime - want) > 0.25 * Math.max(1, rate)) {
          el.currentTime = want; // drifted: resync to the master clock
        }
      } else {
        if (!el.paused) el.pause();
        if (!el.seeking && Math.abs(el.currentTime - want) > 0.5 / fps) el.currentTime = want;
      }
      if (slot.gain && this.audio) {
        const audible = a.audible && forward && rate <= MAX_AUDIBLE_RATE;
        const inFootage = want > 0.001 && want < a.media.duration - 0.05;
        const xf = a.tr ? (a.tr.side === "A" ? 1 - a.tr.p : a.tr.p) : 1; // linear, like FFmpeg's afade
        // Same as backend layers.gain_expr: keyframed volume x track x fades x ducking.
        const ct = t - a.clip.start;
        const duck = a.track.type === "music" && a.track.autoDuck?.enabled ? duckGain(speech, t, a.track.autoDuck.amount ?? -12) : 1;
        const g = audible && inFootage
          ? valueAt(a.clip.keyframes?.volume, ct, a.clip.volume) * a.track.volume * xf
            * fadeGain(ct, clipDuration(a.clip), a.clip.fadeIn, a.clip.fadeOut) * duck
          : 0;
        slot.gain.gain.setTargetAtTime(g, this.audio.currentTime, 0.015);
      }
    }
  }

  // ------------------------------------------------------------ drawing
  /** Draw the frame at time t. Returns false if a layer wasn't ready (the old frame is kept). */
  private draw(p: Project, active: Active[], t: number): boolean {
    const trA = active.find((a) => a.visual && a.isMain && a.tr?.side === "A");
    const trB = active.find((a) => a.visual && a.isMain && a.tr?.side === "B" && a.tr.plan === trA?.tr?.plan);
    const inTransition = !!(trA && trB);
    const layers = active.filter((a) => {
      if (!a.visual || a.preroll) return false;
      if (a.adjust) return true;
      if (a.isMain) return inTransition ? a === trA || a === trB : !a.tr && t >= a.clip.start && t < clipEnd(a.clip);
      return t >= a.clip.start;
    });
    // Readiness first, so we never flash a half-drawn frame.
    const sources: (CanvasImageSource | null)[] = layers.map((a) => {
      if (a.adjust) return this.layerAdj;
      if (a.media.type === "image") {
        const img = this.image(a.media);
        return img.complete && img.naturalWidth ? img : null;
      }
      const el = this.slots.get(a.clip.id)?.el as HTMLVideoElement | undefined;
      return el && el.readyState >= 2 ? el : null;
    });
    if (sources.some((s) => !s)) return false;
    const frame = { layers, sources, trA, trB, inTransition };
    // Compose off-screen and only show complete frames: if a grading asset is still loading, the
    // previous frame stays on screen instead of a half-drawn (or black) one.
    const cw0 = this.canvas.width, ch0 = this.canvas.height;
    if (this.work.width !== cw0 || this.work.height !== ch0) {
      this.work.width = cw0;
      this.work.height = ch0;
    }
    const wctx = this.work.getContext("2d")!;
    if (!this.compose(wctx, this.work, p, frame, t, false)) return false;
    this.ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.ctx.globalAlpha = 1;
    this.ctx.drawImage(this.work, 0, 0);
    this.lastFrameT = t;
    if (this.compare != null) {
      const cw = this.canvas.width, ch = this.canvas.height;
      if (this.before.width !== cw || this.before.height !== ch) {
        this.before.width = cw;
        this.before.height = ch;
      }
      const bctx = this.before.getContext("2d")!;
      if (!this.compose(bctx, this.before, p, frame, t, true)) return true;
      const x = Math.round(cw * Math.min(1, Math.max(0, this.compare)));
      if (x > 0) this.ctx.drawImage(this.before, 0, 0, x, ch, 0, 0, x, ch);
    }
    return true;
  }

  private gradeCtx(p: Project, t: number, w: number, h: number): GradeContext {
    return { exportW: w, exportH: h, refW: p.settings.width, refH: p.settings.height, t, fps: p.settings.fps };
  }

  /** Composite one frame into `ctx` (the main canvas, or the "before" canvas with `bypass`). */
  private compose(
    ctx: CanvasRenderingContext2D, target: HTMLCanvasElement, p: Project,
    f: { layers: Active[]; sources: (CanvasImageSource | null)[]; trA?: Active; trB?: Active; inTransition: boolean },
    t: number, bypass: boolean,
  ): boolean {
    const { width: W, height: H } = p.settings;
    const k = target.width / W;
    const cw = target.width, ch = target.height;
    const bg = p.settings.background.color || "#000";
    const clear = (c: CanvasRenderingContext2D, w: number, h: number) => {
      c.setTransform(1, 0, 0, 1, 0, 0);
      c.globalAlpha = 1;
      c.filter = "none";
      c.fillStyle = bg;
      c.fillRect(0, 0, w, h);
    };
    const sized = (cv: HTMLCanvasElement, w: number, h: number) => {
      if (cv.width !== w || cv.height !== h) {
        cv.width = w;
        cv.height = h;
      }
      return cv.getContext("2d")!;
    };
    /** A main-track clip as a full graded frame (or null while grading assets load). */
    const mainFrame = (cv: HTMLCanvasElement, a: Active, src: CanvasImageSource, full: boolean): HTMLCanvasElement | null => {
      const w = full ? W : cw, h = full ? H : ch;
      const c2 = sized(cv, w, h);
      clear(c2, w, h);
      this.drawLayer(c2, a, src, p, full ? 1 : k, t);
      if (bypass || !needsGrade(a.clip.filters)) return cv;
      const graded = this.fx.apply(cv, a.clip.filters, this.gradeCtx(p, t, W, H));
      if (!graded) return null;
      if (graded !== cv) {
        c2.setTransform(1, 0, 0, 1, 0, 0);
        // Keyframed grade strength: the graded picture over the ungraded one (export: blend).
        c2.globalAlpha = Math.max(0, Math.min(1, valueAt(a.clip.keyframes?.intensity, t - a.clip.start, 1)));
        c2.drawImage(graded, 0, 0, w, h);
        c2.globalAlpha = 1;
      }
      return cv;
    };

    clear(ctx, cw, ch);
    const { layers, sources } = f;
    if (f.inTransition) {
      const iA = layers.indexOf(f.trA!);
      const iB = layers.indexOf(f.trB!);
      const plan = f.trA!.tr!.plan;
      // zoomin magnifies A down to a single source pixel: render A at full project resolution so the
      // preview samples the same pixel as the export.
      const fullA = plan.type === "zoomin";
      const A = mainFrame(this.layerA, layers[iA], sources[iA]!, fullA);
      const B = mainFrame(this.layerB, layers[iB], sources[iB]!, false);
      if (!A || !B) return false;
      this.trRenderer ??= new TransitionRenderer();
      const out = this.trRenderer.render(plan.type, A, B, t - (plan.cut - plan.half), 2 * plan.half, p.settings.fps);
      ctx.drawImage(out, 0, 0);
    }
    for (let j = 0; j < layers.length; j++) {
      const a = layers[j];
      if (f.inTransition && (a === f.trA || a === f.trB)) continue;
      if (a.adjust) {
        if (bypass || !needsGrade(a.clip.filters)) continue;
        const c2 = sized(this.layerAdj, cw, ch);
        c2.setTransform(1, 0, 0, 1, 0, 0);
        c2.globalAlpha = 1;
        c2.drawImage(target, 0, 0);
        const graded = this.fx.apply(this.layerAdj, a.clip.filters, this.gradeCtx(p, t, W, H));
        if (!graded) return false;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        const strength = valueAt(a.clip.keyframes?.intensity, t - a.clip.start, 1);
        ctx.globalAlpha = Math.max(0, Math.min(1, (a.clip.transform?.opacity ?? 1) * strength));
        ctx.drawImage(graded, 0, 0, cw, ch);
        ctx.globalAlpha = 1;
        continue;
      }
      if (a.isMain) {
        if (bypass || !needsGrade(a.clip.filters)) {
          this.drawLayer(ctx, a, sources[j]!, p, k, t);
        } else {
          const fr = mainFrame(this.layerA, a, sources[j]!, false);
          if (!fr) return false;
          ctx.setTransform(1, 0, 0, 1, 0, 0);
          ctx.drawImage(fr, 0, 0);
        }
        continue;
      }
      // Overlay clip: grade its own picture at its fitted size (scale 1, export: the same) before
      // placing it with its (keyframed) transform.
      if (bypass || !needsGrade(a.clip.filters)) {
        this.drawLayer(ctx, a, sources[j]!, p, k, t);
        continue;
      }
      const src = sources[j]!;
      const [sw, sh] = this.sourceSize(src, a, W, H);
      const fill = (a.clip.fit ?? "fit") === "fill";
      const [x0, y0, rw, rh] = roundedRect(sw, sh, a.clip.transform?.crop, fill, W, H);
      const f0 = Math.min(W / rw, H / rh);
      const gw = Math.max(2, rnd((rw * f0) / 2) * 2), gh = Math.max(2, rnd((rh * f0) / 2) * 2);
      const lw = Math.max(2, Math.round(gw * k)), lh = Math.max(2, Math.round(gh * k));
      const c2 = sized(this.layerO, lw, lh);
      c2.setTransform(1, 0, 0, 1, 0, 0);
      c2.globalAlpha = 1;
      c2.clearRect(0, 0, lw, lh);
      c2.drawImage(src, x0, y0, rw, rh, 0, 0, lw, lh);
      const graded = this.fx.apply(this.layerO, a.clip.filters, this.gradeCtx(p, t, gw, gh));
      if (!graded) return false;
      const tv = transformAt(a.clip, t - a.clip.start);
      const m = rectMatrix([0, 0, gw, gh], W, H, tv);
      ctx.save();
      ctx.setTransform(k * m[0], k * m[1], k * m[2], k * m[3], k * m[4], k * m[5]);
      ctx.globalAlpha = Math.max(0, Math.min(1, tv.opacity));
      ctx.drawImage(graded, 0, 0, lw, lh, 0, 0, gw, gh);
      ctx.restore();
    }
    return true;
  }

  private bd = { small: document.createElement("canvas"), pad: document.createElement("canvas") };

  /** The blurred "cover" backdrop of the blur fit mode, made like the export's blur_backdrop:
   *  cover-crop at 1/8 size, gaussian sigma 6 (edges extended, like gblur), upscaled, then
   *  darkened by 0.06 (eq=brightness=-0.06). */
  private backdrop(src: CanvasImageSource, sw: number, sh: number, W: number, H: number): HTMLCanvasElement {
    const w = Math.floor(W / 8), h = Math.floor(H / 8), m = 24;
    const pad = this.bd.pad;
    if (pad.width !== w + 2 * m || pad.height !== h + 2 * m) { pad.width = w + 2 * m; pad.height = h + 2 * m; }
    const px = pad.getContext("2d")!;
    px.filter = "none";
    const f = Math.max(w / sw, h / sh);
    const cw = w / f, ch = h / f; // source rect shown
    px.drawImage(src, (sw - cw) / 2, (sh - ch) / 2, cw, ch, m, m, w, h);
    // Extend the edges outwards so the blur doesn't pull in transparency.
    px.drawImage(pad, m, m, w, 1, m, 0, w, m);
    px.drawImage(pad, m, m + h - 1, w, 1, m, m + h, w, m);
    px.drawImage(pad, m, 0, 1, h + 2 * m, 0, 0, m, h + 2 * m);
    px.drawImage(pad, m + w - 1, 0, 1, h + 2 * m, m + w, 0, m, h + 2 * m);
    const small = this.bd.small;
    if (small.width !== w || small.height !== h) { small.width = w; small.height = h; }
    const sx = small.getContext("2d")!;
    sx.globalCompositeOperation = "source-over";
    sx.filter = "blur(6px)";
    sx.drawImage(pad, -m, -m);
    sx.filter = "none";
    // Subtract ~0.06 (|c - 15| == c - 15 except in near-black areas).
    sx.globalCompositeOperation = "difference";
    sx.fillStyle = "rgb(15,15,15)";
    sx.fillRect(0, 0, w, h);
    sx.globalCompositeOperation = "source-over";
    return small;
  }

  private sourceSize(src: CanvasImageSource, a: Active, W: number, H: number): [number, number] {
    const sw = (src as HTMLVideoElement).videoWidth || (src as HTMLImageElement).naturalWidth || a.media.width || W;
    const sh = (src as HTMLVideoElement).videoHeight || (src as HTMLImageElement).naturalHeight || a.media.height || H;
    return [sw, sh];
  }

  /** Draw a clip's picture at timeline time t. Main-track clips without any transform use the plain
   *  fit/fill/blur placement (export: fit_chain); everything else goes through the shared layer
   *  matrix with the export's whole-pixel crop rect (export: layers.motion_layer). */
  private drawLayer(ctx: CanvasRenderingContext2D, a: Active, src: CanvasImageSource, p: Project, k: number, t: number) {
    const { width: W, height: H } = p.settings;
    const [sw, sh] = this.sourceSize(src, a, W, H);
    const mode = a.clip.fit ?? (a.isMain ? p.settings.background.mode : "fit");
    if (a.isMain && mode === "blur") {
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.drawImage(this.backdrop(src, sw, sh, W, H), 0, 0, W * k, H * k);
      ctx.restore();
    }
    if (a.isMain && !hasMotion(a.clip)) {
      const g = layerGeometry(sw, sh, a.clip, true, p.settings.background.mode, W, H);
      ctx.save();
      ctx.translate(g.cx * k, g.cy * k);
      if (g.cw < g.dw - 0.5 || g.ch < g.dh - 0.5) {
        ctx.beginPath();
        ctx.rect((-g.cw / 2) * k, (-g.ch / 2) * k, g.cw * k, g.ch * k);
        ctx.clip();
      }
      ctx.drawImage(src, (-g.dw / 2) * k, (-g.dh / 2) * k, g.dw * k, g.dh * k);
      ctx.restore();
      return;
    }
    const tv = transformAt(a.clip, t - a.clip.start);
    const rect = roundedRect(sw, sh, a.clip.transform?.crop, mode === "fill", W, H);
    const m = rectMatrix(rect, W, H, tv);
    const [x0, y0, rw, rh] = rect;
    ctx.save();
    ctx.setTransform(k * m[0], k * m[1], k * m[2], k * m[3], k * m[4], k * m[5]);
    ctx.globalAlpha = Math.max(0, Math.min(1, tv.opacity));
    ctx.drawImage(src, x0, y0, rw, rh, x0, y0, rw, rh);
    ctx.restore();
  }

  private incomingHalf(a: Active): number {
    if (!a.isMain) return 0;
    for (const pl of this.plansFor.plans.values()) if (pl.toId === a.clip.id) return pl.half;
    return 0;
  }
}
