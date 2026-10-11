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

const PREROLL = 1.5; // seconds: load + seek upcoming clips ahead of time
const KEEP_IDLE_MS = 4000; // drop elements unused for this long
const MAX_AUDIBLE_RATE = 2; // mute audio when shuttling faster than this (or in reverse)

interface Active {
  /** Set on main-track clips taking part in a transition right now (A = outgoing, B = incoming). */
  tr?: { plan: TransitionPlan; side: "A" | "B"; p: number };
  clip: Clip;
  track: Track;
  media: MediaMeta;
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
    });
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
    this.sync(active, t, s.playing, s.playing ? this.clock.rate : 0, p.settings.fps, now);
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

  private sync(active: Active[], t: number, playing: boolean, rate: number, fps: number, now: number) {
    for (const a of active) {
      const slot = this.slotFor(a, now);
      if (!slot) continue;
      const el = slot.el;
      const speed = a.clip.speed || 1;
      let want = a.clip.inPoint + (t - a.clip.start) * speed;
      if (a.preroll) want = a.clip.inPoint - this.incomingHalf(a) * speed; // where its (handle) footage starts
      // Past the footage (transition freeze): hold the first/last frame.
      if (a.media.type !== "image") want = Math.max(0, Math.min(want, Math.max(0, a.media.duration - 0.04)));
      const forward = playing && rate > 0 && !a.preroll;
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
        const g = audible && inFootage
          ? a.clip.volume * a.track.volume * xf * fadeGain(t - a.clip.start, clipDuration(a.clip), a.clip.fadeIn, a.clip.fadeOut)
          : 0;
        slot.gain.gain.setTargetAtTime(g, this.audio.currentTime, 0.015);
      }
    }
  }

  // ------------------------------------------------------------ drawing
  /** Draw the frame at time t. Returns false if a layer wasn't ready (the old frame is kept). */
  private draw(p: Project, active: Active[], t: number): boolean {
    const W = p.settings.width;
    const trA = active.find((a) => a.visual && a.isMain && a.tr?.side === "A");
    const trB = active.find((a) => a.visual && a.isMain && a.tr?.side === "B" && a.tr.plan === trA?.tr?.plan);
    const inTransition = !!(trA && trB);
    const layers = active.filter((a) => {
      if (!a.visual || a.preroll) return false;
      if (a.isMain) return inTransition ? a === trA || a === trB : !a.tr && t >= a.clip.start && t < clipEnd(a.clip);
      return t >= a.clip.start;
    });
    // Readiness first, so we never flash a half-drawn frame.
    const sources: (CanvasImageSource | null)[] = layers.map((a) => {
      if (a.media.type === "image") {
        const img = this.image(a.media);
        return img.complete && img.naturalWidth ? img : null;
      }
      const el = this.slots.get(a.clip.id)?.el as HTMLVideoElement | undefined;
      return el && el.readyState >= 2 ? el : null;
    });
    if (sources.some((s) => !s)) return false;

    const ctx = this.ctx;
    const k = this.canvas.width / W;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.filter = "none";
    ctx.fillStyle = p.settings.background.color || "#000";
    ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

    if (inTransition) {
      // Render both sides as full frames, then composite them with the transition shader.
      const iA = layers.indexOf(trA!);
      const iB = layers.indexOf(trB!);
      const plan = trA!.tr!.plan;
      // zoomin magnifies A down to a single source pixel: render A at full project resolution so the
      // preview samples the same pixel as the export.
      const fullA = plan.type === "zoomin";
      for (const [cv, idx] of [[this.layerA, iA], [this.layerB, iB]] as const) {
        const full = fullA && cv === this.layerA;
        const cw = full ? W : this.canvas.width;
        const ch = full ? p.settings.height : this.canvas.height;
        if (cv.width !== cw || cv.height !== ch) {
          cv.width = cw;
          cv.height = ch;
        }
        const c2 = cv.getContext("2d")!;
        c2.setTransform(1, 0, 0, 1, 0, 0);
        c2.globalAlpha = 1;
        c2.filter = "none";
        c2.fillStyle = p.settings.background.color || "#000";
        c2.fillRect(0, 0, cw, ch);
        this.drawLayer(c2, layers[idx], sources[idx]!, p, full ? 1 : k);
      }
      this.trRenderer ??= new TransitionRenderer();
      const elapsed = t - (plan.cut - plan.half);
      const out = this.trRenderer.render(plan.type, this.layerA, this.layerB, elapsed, 2 * plan.half, p.settings.fps);
      ctx.drawImage(out, 0, 0);
    }
    layers.forEach((a, j) => {
      if (inTransition && (a === trA || a === trB)) return; // already composited above
      this.drawLayer(ctx, a, sources[j]!, p, k);
    });
    return true;
  }

  private drawLayer(ctx: CanvasRenderingContext2D, a: Active, src: CanvasImageSource, p: Project, k: number) {
    const { width: W, height: H } = p.settings;
    {
      const sw = (src as HTMLVideoElement).videoWidth || (src as HTMLImageElement).naturalWidth || a.media.width || W;
      const sh = (src as HTMLVideoElement).videoHeight || (src as HTMLImageElement).naturalHeight || a.media.height || H;
      const g = layerGeometry(sw, sh, a.clip, a.isMain, p.settings.background.mode, W, H);
      if (g.blurBackground) {
        const f = Math.max(W / sw, H / sh);
        ctx.save();
        ctx.filter = `blur(${Math.max(4, 40 * k)}px) brightness(0.88)`;
        ctx.drawImage(src, (W - sw * f) / 2 * k, (H - sh * f) / 2 * k, sw * f * k, sh * f * k);
        ctx.restore();
      }
      ctx.save();
      ctx.translate(g.cx * k, g.cy * k);
      if (g.rotation) ctx.rotate(g.rotation);
      if (g.flipH || g.flipV) ctx.scale(g.flipH ? -1 : 1, g.flipV ? -1 : 1);
      ctx.globalAlpha = g.opacity;
      if (g.cw < g.dw - 0.5 || g.ch < g.dh - 0.5) {
        ctx.beginPath();
        ctx.rect((-g.cw / 2) * k, (-g.ch / 2) * k, g.cw * k, g.ch * k);
        ctx.clip();
      }
      ctx.drawImage(src, (-g.dw / 2) * k, (-g.dh / 2) * k, g.dw * k, g.dh * k);
      ctx.restore();
    }
  }

  private incomingHalf(a: Active): number {
    if (!a.isMain) return 0;
    for (const pl of this.plansFor.plans.values()) if (pl.toId === a.clip.id) return pl.half;
    return 0;
  }
}
