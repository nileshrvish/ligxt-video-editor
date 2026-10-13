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
import { textOps } from "../text/anim";
import { onTextAssets, textAsset, textAssetFor, type TextAsset } from "../text/cache";
import { extension, onExtLoaded } from "./extend";
import { cutoutAsset, onCutoutAssets, type CutoutAsset } from "../cutout/assets";
import { cutoutOn, glowLevels, normalizeCutout, pulseAt, timelineBeats, type Cutout } from "../cutout/model";
import { CutoutRenderer } from "./cutout";
import { onSlowAssets, slowAsset, type SlowInfo } from "../slowmo/assets";
import { needsRetime } from "../slowmo/model";

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
  /** Text clips (no media): drawn from the backend-rendered text images. */
  text?: boolean;
  isMain: boolean;
  visual: boolean;
  audible: boolean;
  preroll: boolean;
  /** Cutout on and its matte ready: the clip plays the picture|matte preview proxy. */
  cut?: { asset: CutoutAsset; cfg: Cutout };
  /** Slow motion with blending / AI frames: the retimed video (plays at 1x; the original element keeps
   *  only the sound). With Cutout too, `cut` is the cutout of the full-resolution retimed video. */
  slow?: { preview: SlowInfo; full?: SlowInfo };
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
    this.unsubAssets = [onTextAssets(() => (this.dirty = true)), onExtLoaded(() => (this.dirty = true)), onCutoutAssets(() => (this.dirty = true)),
      onSlowAssets(() => (this.dirty = true))];
    this.raf = requestAnimationFrame(this.tick);
  }

  private unsubAssets: (() => void)[] = [];
  /** Text clip being edited on the canvas (hidden while its editor box is open). */
  editingTextId: string | null = null;

  destroy() {
    cancelAnimationFrame(this.raf);
    this.unsub();
    this.unsubAssets.forEach((f) => f());
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
      if (track.type === "text") {
        if (track.hidden) continue;
        for (const clip of track.clips) {
          if (!clip.text || t < clip.start || t >= clipEnd(clip) || clip.id === this.editingTextId) continue;
          out.push({ clip, track, media: null as unknown as MediaMeta, text: true, isMain: false, visual: true, audible: false, preroll: false });
        }
        continue;
      }
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
        const fps = p.settings.fps;
        if (visual && needsRetime(clip, m, fps)) {
          const before = inTr?.half ?? 0, after = outTr?.half ?? 0;
          const sa = slowAsset(clip, m, fps, before, after, "preview");
          if (sa?.status === "ready" && sa.info) a.slow = { preview: sa.info };
          if (a.slow && cutoutOn(clip)) {
            // Cutout on a slowed clip: the matte is made from the retimed (full-resolution) frames
            const fa = slowAsset(clip, m, fps, before, after, "full");
            if (fa?.status === "ready" && fa.info?.mediaId) {
              a.slow.full = fa.info;
              const D = clipDuration(clip);
              const rc = { ...clip, mediaId: fa.info.mediaId, inPoint: fa.info.offset, outPoint: fa.info.offset + D, speed: 1,
                speedRamp: null, reverse: false, freezeFrame: null } as Clip;
              const pm = { ...m, id: fa.info.mediaId, fps, duration: fa.info.frames / fps, hasAudio: false } as MediaMeta;
              const asset = cutoutAsset(rc, pm);
              if (asset?.status === "ready" && asset.info) a.cut = { asset, cfg: normalizeCutout(clip.cutout) };
            }
          }
        } else if (visual && cutoutOn(clip) && (track.type === "video" || track.type === "overlay")) {
          const asset = cutoutAsset(clip, m);
          if (asset?.status === "ready" && asset.info) a.cut = { asset, cfg: normalizeCutout(clip.cutout) };
        }
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
  /** The element showing a clip's picture: the retimed video for slowed clips, else the clip's own. */
  private videoEl(a: Active): HTMLVideoElement | undefined {
    return this.slots.get(a.slow ? "v:" + a.clip.id : a.clip.id)?.el as HTMLVideoElement | undefined;
  }

  private slotFor(a: Active, now: number, retimed = false): Slot | null {
    if (a.media.type === "image") return null;
    const sk = retimed ? "v:" + a.clip.id : a.clip.id;
    let slot = this.slots.get(sk);
    const url = retimed ? (a.cut && !a.cut.asset.info!.image ? a.cut.asset.info!.preview : a.slow!.preview.url)
      : a.cut && !a.slow && !a.cut.asset.info!.image ? a.cut.asset.info!.preview : mediaUrl(a.media);
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
      if (retimed) el.muted = true; // pictures only: the sound comes from the clip's own element
      else this.connect(slot);
      this.slots.set(sk, slot);
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
    for (const a of active) if (a.cut) this.syncCutBg(a, t, playing, rate, fps, now);
    for (const a of active) {
      if (a.adjust || a.text) continue;
      if (a.slow) {
        // the retimed video: clip time + its offset, at 1x (speed, ramps, reverse and freeze are baked in)
        const vs = this.slotFor(a, now, true);
        if (vs) {
          const tau = a.preroll ? -this.incomingHalf(a) : t - a.clip.start;
          const sl = a.slow;
          let want = tau + (sl.full && a.cut ? sl.full.offset : sl.preview.offset);
          const ci = a.cut?.asset.info;
          if (ci && sl.full) want = Math.max(0, Math.min(want - ci.offset, ci.frames / ci.fps - 0.04));
          else want = Math.max(0, Math.min(want, sl.preview.frames / sl.preview.fps - 0.04));
          this.drive(vs.el, want, 1, playing && rate > 0 && !a.preroll, rate, fps);
        }
        if (!a.audible) continue; // the original element is only needed for the sound
      }
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
      const ci = a.slow ? undefined : a.cut?.asset.info;
      if (ci && !ci.image) want = Math.max(0, Math.min(want - ci.offset, ci.frames / ci.fps - 0.04)); // window file
      // Reversed and frozen clips can't be played by a media element: they're stepped frame by
      // frame (seeked) and silent in the preview. The export plays reversed audio.
      const stepped = speed <= 0;
      const forward = playing && rate > 0 && !a.preroll && !stepped;
      this.drive(el, want, speed, forward, rate, fps);
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

  /** Slave a media element to the clock: play at speed x rate, resync when it drifts; seek when paused. */
  private drive(el: HTMLVideoElement | HTMLAudioElement, want: number, speed: number, forward: boolean, rate: number, fps: number) {
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
    const texts = new Map<Active, TextAsset>();
    const sources: (CanvasImageSource | null)[] = layers.map((a) => {
      if (a.adjust) return this.layerAdj;
      if (a.text) {
        const asset = textAssetFor(a.clip.id, a.clip.text);
        if (!asset) return textAsset(a.clip.text).status === "error" ? this.layerAdj : null; // failed: draw nothing
        texts.set(a, asset);
        return asset.imgs!.base;
      }
      if (a.clip.extend && a.media.type === "image" && extension(a.media.id).status === "loading") return null;
      if (a.cut) {
        const info = a.cut.asset.info!;
        if (info.image && !(a.cut.asset.img?.complete && a.cut.asset.img.naturalWidth)) return null;
        const bg = a.cut.cfg.background;
        if (bg.type === "image" && bg.mediaId) {
          const bm = useEditor.getState().media[bg.mediaId];
          const bi = bm ? this.image(bm) : null;
          if (bi && !(bi.complete && bi.naturalWidth)) return null;
        }
        if (bg.type === "video" && bg.mediaId) {
          const be = this.slots.get("bg:" + a.clip.id)?.el as HTMLVideoElement | undefined;
          if (be && be.readyState < 2) return null;
        }
      }
      if (a.media.type === "image") {
        const img = this.image(a.media);
        return img.complete && img.naturalWidth ? img : null;
      }
      const el = this.videoEl(a);
      return el && el.readyState >= 2 ? el : null;
    });
    if (sources.some((s) => !s)) return false;
    const frame = { layers, sources, trA, trB, inTransition, texts };
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
    f: { layers: Active[]; sources: (CanvasImageSource | null)[]; trA?: Active; trB?: Active; inTransition: boolean; texts: Map<Active, TextAsset> },
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
    const useCut = (a: Active) => !!a.cut && !bypass && !this.cutFx.failed;
    /** A main-track clip as a full graded frame (or null while grading assets load). With a cutout, the
     *  canvas matte of the clip is left in this.mattes[side] ("A"/"B"/"M"). */
    const mainFrame = (cv: HTMLCanvasElement, a: Active, src: CanvasImageSource, full: boolean, side = "M"): HTMLCanvasElement | null => {
      const w = full ? W : cw, h = full ? H : ch;
      const kk = full ? 1 : k;
      const c2 = sized(cv, w, h);
      clear(c2, w, h);
      const parts = useCut(a) ? this.cutParts(a, src) : null;
      this.drawLayer(c2, a, parts ? parts.pic : src, p, kk, t);
      if (!bypass && needsGrade(a.clip.filters)) {
        const graded = this.fx.apply(cv, a.clip.filters, this.gradeCtx(p, t, W, H));
        if (!graded) return null;
        if (graded !== cv) {
          c2.setTransform(1, 0, 0, 1, 0, 0);
          // Keyframed grade strength: the graded picture over the ungraded one (export: blend).
          c2.globalAlpha = Math.max(0, Math.min(1, valueAt(a.clip.keyframes?.intensity, t - a.clip.start, 1)));
          c2.drawImage(graded, 0, 0, w, h);
          c2.globalAlpha = 1;
        }
      }
      if (!parts) return cv;
      // canvas matte: the matte through the same placement as the picture, over black
      const mc = sized(this.matteCanvas(side), w, h);
      mc.setTransform(1, 0, 0, 1, 0, 0);
      mc.globalAlpha = 1;
      mc.fillStyle = "#000";
      mc.fillRect(0, 0, w, h);
      this.drawLayer(mc, a, parts.matte, p, kk, t, true);
      return this.cutComposite(cv, this.matteCanvas(side), a, p, t, kk, W, H);
    };

    clear(ctx, cw, ch);
    const { layers, sources } = f;
    // Text tracks with text behind the subject (export: render.py's subject pass), and the main track's
    // canvas matte at this frame (none = nothing is on top of the text).
    const behind = new Set<Track>(bypass ? [] : layers.filter((a) => a.text && (a.clip.text as { behindSubject?: boolean })?.behindSubject).map((a) => a.track));
    let mainMatte: HTMLCanvasElement | null = null;
    const behindDone = new Set<Track>();
    if (f.inTransition) {
      const iA = layers.indexOf(f.trA!);
      const iB = layers.indexOf(f.trB!);
      const plan = f.trA!.tr!.plan;
      // zoomin magnifies A down to a single source pixel: render A at full project resolution so the
      // preview samples the same pixel as the export.
      const fullA = plan.type === "zoomin";
      const A = mainFrame(this.layerA, layers[iA], sources[iA]!, fullA, "A");
      const B = mainFrame(this.layerB, layers[iB], sources[iB]!, false, "B");
      if (!A || !B) return false;
      this.trRenderer ??= new TransitionRenderer();
      const out = this.trRenderer.render(plan.type, A, B, t - (plan.cut - plan.half), 2 * plan.half, p.settings.fps);
      ctx.drawImage(out, 0, 0);
      if (behind.size && (useCut(layers[iA]) || useCut(layers[iB]))) {
        // the subject matte goes through the same transition (export: the same xfade/whip on the mattes)
        const blackOr = (a: Active, side: string, full: boolean) => {
          if (useCut(a)) return this.matteCanvas(side);
          const c3 = sized(this.matteCanvas(side), full ? W : cw, full ? H : ch);
          c3.setTransform(1, 0, 0, 1, 0, 0);
          c3.fillStyle = "#000";
          c3.fillRect(0, 0, full ? W : cw, full ? H : ch);
          return this.matteCanvas(side);
        };
        const mo = this.trRenderer.render(plan.type, blackOr(layers[iA], "A", fullA), blackOr(layers[iB], "B", false),
          t - (plan.cut - plan.half), 2 * plan.half, p.settings.fps);
        const mm = sized(this.matteCanvas("main"), cw, ch);
        mm.setTransform(1, 0, 0, 1, 0, 0);
        mm.drawImage(mo, 0, 0, cw, ch);
        mainMatte = this.matteCanvas("main");
      }
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
      if (a.text) {
        if (behind.has(a.track) && !behindDone.has(a.track)) {
          // The text behind the subject: the picture so far, the behind texts, then the picture cut out by
          // the subject matte on top again.
          behindDone.add(a.track);
          const pc = sized(this.behindPic, cw, ch);
          pc.setTransform(1, 0, 0, 1, 0, 0);
          pc.globalAlpha = 1;
          pc.drawImage(target, 0, 0);
          for (let i = j; i < layers.length; i++) {
            const b = layers[i];
            if (b.track !== a.track || !b.text || !(b.clip.text as { behindSubject?: boolean })?.behindSubject) continue;
            const asset = f.texts.get(b);
            if (asset) this.drawText(ctx, asset, b.clip, t, W, H, k);
          }
          if (mainMatte) {
            const sub = this.cutFx.mask(this.behindPic, mainMatte);
            if (sub) {
              ctx.setTransform(1, 0, 0, 1, 0, 0);
              ctx.globalAlpha = 1;
              ctx.drawImage(sub, 0, 0, cw, ch);
            }
          }
        }
        if (behind.has(a.track) && (a.clip.text as { behindSubject?: boolean })?.behindSubject) continue; // drawn above
        const asset = f.texts.get(a);
        if (asset) this.drawText(ctx, asset, a.clip, t, W, H, k);
        continue;
      }
      if (a.isMain) {
        if (useCut(a)) {
          const fr = mainFrame(this.layerA, a, sources[j]!, false);
          if (!fr) return false;
          ctx.setTransform(1, 0, 0, 1, 0, 0);
          ctx.drawImage(fr, 0, 0);
          if (behind.size) mainMatte = this.matteCanvas("M");
        } else if (bypass || !needsGrade(a.clip.filters)) {
          this.drawLayer(ctx, a, sources[j]!, p, k, t);
        } else {
          const fr = mainFrame(this.layerA, a, sources[j]!, false);
          if (!fr) return false;
          ctx.setTransform(1, 0, 0, 1, 0, 0);
          ctx.drawImage(fr, 0, 0);
        }
        continue;
      }
      if (useCut(a)) {
        // Overlay cutout: the layer on its own transparent canvas + its canvas matte, then glow/shadow
        // under it and (unless "keep") the background removed.
        const lc = sized(this.cutLayer, cw, ch);
        lc.setTransform(1, 0, 0, 1, 0, 0);
        lc.clearRect(0, 0, cw, ch);
        const parts = this.cutParts(a, sources[j]!);
        if (!this.drawOverlay(lc, a, parts.pic, p, k, t, bypass)) return false;
        const mc = sized(this.matteCanvas("O"), cw, ch);
        mc.setTransform(1, 0, 0, 1, 0, 0);
        mc.globalAlpha = 1;
        mc.fillStyle = "#000";
        mc.fillRect(0, 0, cw, ch);
        this.drawOverlay(mc, a, parts.matte, p, k, t, true, true);
        const out = this.cutFx.compose({
          S: this.cutLayer, B: null, M: this.matteCanvas("O"), bgBlur: 0, alphaFromM: a.cut!.cfg.background.type !== "keep",
          ...this.cutGlowShadow(a, p, t, k),
        });
        if (out) {
          ctx.setTransform(1, 0, 0, 1, 0, 0);
          ctx.globalAlpha = 1;
          ctx.drawImage(out, 0, 0, cw, ch);
        }
        continue;
      }
      if (!this.drawOverlay(ctx, a, sources[j]!, p, k, t, bypass)) return false;
    }
    return true;
  }

  /** An overlay clip: its own picture graded at its fitted size (scale 1, export: the same) before
   *  placing it with its (keyframed) transform. `matte`: draw a matte the same way (no grade, opaque). */
  private drawOverlay(ctx: CanvasRenderingContext2D, a: Active, src: CanvasImageSource, p: Project, k: number, t: number,
    bypass: boolean, matte = false): boolean {
    const { width: W, height: H } = p.settings;
    if (bypass || !needsGrade(a.clip.filters)) {
      this.drawLayer(ctx, a, src, p, k, t, matte);
      return true;
    }
    const [sw, sh] = this.sourceSize(src, a, W, H);
    const fill = (a.clip.fit ?? "fit") === "fill";
    const [x0, y0, rw, rh] = roundedRect(sw, sh, a.clip.transform?.crop, fill, W, H);
    const f0 = Math.min(W / rw, H / rh);
    const gw = Math.max(2, rnd((rw * f0) / 2) * 2), gh = Math.max(2, rnd((rh * f0) / 2) * 2);
    const lw = Math.max(2, Math.round(gw * k)), lh = Math.max(2, Math.round(gh * k));
    const lo = matte ? this.layerOM : this.layerO;
    if (lo.width !== lw || lo.height !== lh) { lo.width = lw; lo.height = lh; }
    const c2 = lo.getContext("2d")!;
    c2.setTransform(1, 0, 0, 1, 0, 0);
    c2.globalAlpha = 1;
    c2.clearRect(0, 0, lw, lh);
    c2.drawImage(src, x0, y0, rw, rh, 0, 0, lw, lh);
    const graded = matte ? lo : this.fx.apply(lo, a.clip.filters, this.gradeCtx(p, t, gw, gh));
    if (!graded) return false;
    const tv = transformAt(a.clip, t - a.clip.start);
    const m = rectMatrix([0, 0, gw, gh], W, H, tv);
    ctx.save();
    ctx.setTransform(k * m[0], k * m[1], k * m[2], k * m[3], k * m[4], k * m[5]);
    ctx.globalAlpha = Math.max(0, Math.min(1, tv.opacity));
    ctx.drawImage(graded, 0, 0, lw, lh, 0, 0, gw, gh);
    ctx.restore();
    return true;
  }

  // ------------------------------------------------------------ cutout
  private cutFx = new CutoutRenderer();
  private cutLayer = document.createElement("canvas");
  private behindPic = document.createElement("canvas");
  private layerOM = document.createElement("canvas");
  private cutCanvases = new Map<string, HTMLCanvasElement>();
  private beatsFor: { project: Project | null; media: Record<string, MediaMeta> | null; beats: number[] } = { project: null, media: null, beats: [] };

  private matteCanvas(name: string): HTMLCanvasElement {
    let c = this.cutCanvases.get("m:" + name);
    if (!c) this.cutCanvases.set("m:" + name, (c = document.createElement("canvas")));
    return c;
  }

  private scratch(name: string, w: number, h: number): [HTMLCanvasElement, CanvasRenderingContext2D] {
    let c = this.cutCanvases.get(name);
    if (!c) this.cutCanvases.set(name, (c = document.createElement("canvas")));
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    const x = c.getContext("2d")!;
    x.setTransform(1, 0, 0, 1, 0, 0);
    x.globalAlpha = 1;
    x.filter = "none";
    return [c, x];
  }

  /** The picture and matte of a cutout clip's current frame (videos: the two halves of the proxy). */
  private cutParts(a: Active, src: CanvasImageSource): { pic: CanvasImageSource; matte: CanvasImageSource } {
    const info = a.cut!.asset.info!;
    if (info.image) return { pic: src, matte: a.cut!.asset.img! };
    const v = src as HTMLVideoElement;
    const w = Math.floor(v.videoWidth / 2), h = v.videoHeight;
    const [pc, px] = this.scratch("pic:" + a.clip.id, w, h);
    px.drawImage(v, 0, 0, w, h, 0, 0, w, h);
    const [mc, mx] = this.scratch("mat:" + a.clip.id, w, h);
    mx.drawImage(v, w, 0, w, h, 0, 0, w, h);
    return { pic: pc, matte: mc };
  }

  private beats(p: Project): number[] {
    const media = useEditor.getState().media;
    const c = this.beatsFor;
    if (c.project !== p || c.media !== media) this.beatsFor = { project: p, media, beats: timelineBeats(p, media) };
    return this.beatsFor.beats;
  }

  private cutGlowShadow(a: Active, p: Project, t: number, k: number) {
    const cfg = a.cut!.cfg;
    let glow = null, shadow = null;
    if (cfg.glow.enabled) {
      const [sigma, lo, hi, op] = glowLevels(cfg.glow, pulseAt(cfg.glow.pulse, t, this.beats(p)), k);
      glow = { sigma, lo, hi, op, color: cfg.glow.color };
    }
    if (cfg.shadow.enabled) {
      const sh = cfg.shadow;
      shadow = { sigma: Math.max(0, sh.blur) * k, dx: Math.round(sh.x) * k, dy: Math.round(sh.y) * k,
        op: Math.max(0, Math.min(1, sh.opacity)), color: sh.color };
    }
    return { glow, shadow };
  }

  /** The cutout composite of a main-track clip (export: cutout_render.composite). F = the clip's frame. */
  private cutComposite(F: HTMLCanvasElement, M: HTMLCanvasElement, a: Active, p: Project, t: number, k: number, W: number, H: number): HTMLCanvasElement | null {
    const cfg = a.cut!.cfg;
    const w = F.width, h = F.height;
    const g = this.gradeCtx(p, t, W, H);
    // subject: F with the subject-only grade
    const [S, sx] = this.scratch("S", w, h);
    sx.drawImage(F, 0, 0);
    if (needsGrade(cfg.subjectFilters)) {
      const gr = this.fx.apply(S, cfg.subjectFilters, g);
      if (!gr) return null;
      sx.drawImage(gr, 0, 0, w, h);
    }
    // background
    const [B, bx] = this.scratch("B", w, h);
    const bg = cfg.background;
    let bgBlur = 0;
    const media = useEditor.getState().media;
    const cover = (src: CanvasImageSource, sw: number, sh: number) => {
      const f = Math.max(w / sw, h / sh);
      bx.drawImage(src, (w - sw * f) / 2, (h - sh * f) / 2, sw * f, sh * f);
    };
    if (bg.type === "color") {
      bx.fillStyle = bg.color;
      bx.fillRect(0, 0, w, h);
    } else if (bg.type === "image" && bg.mediaId && media[bg.mediaId]) {
      const img = this.image(media[bg.mediaId]);
      bx.fillStyle = "#000";
      bx.fillRect(0, 0, w, h);
      cover(img, img.naturalWidth || 1, img.naturalHeight || 1);
    } else if (bg.type === "video" && bg.mediaId) {
      const el = this.slots.get("bg:" + a.clip.id)?.el as HTMLVideoElement | undefined;
      bx.fillStyle = "#000";
      bx.fillRect(0, 0, w, h);
      if (el && el.readyState >= 2) cover(el, el.videoWidth || 1, el.videoHeight || 1);
    } else {
      bx.drawImage(F, 0, 0);
      if (bg.type === "blur") bgBlur = Math.max(0.5, bg.blur) * k;
    }
    if (needsGrade(cfg.backgroundFilters)) {
      const gr = this.fx.apply(B, cfg.backgroundFilters, g);
      if (!gr) return null;
      bx.drawImage(gr, 0, 0, w, h);
    }
    const out = this.cutFx.compose({ S, B, M, bgBlur, alphaFromM: true, ...this.cutGlowShadow(a, p, t, k) });
    if (!out) return F;
    const fx = F.getContext("2d")!;
    fx.setTransform(1, 0, 0, 1, 0, 0);
    fx.globalAlpha = 1;
    fx.drawImage(out, 0, 0, w, h);
    return F;
  }

  /** A cutout's background video: its own muted element, playing from the clip's start. */
  private syncCutBg(a: Active, t: number, playing: boolean, rate: number, fps: number, now: number) {
    const bg = a.cut!.cfg.background;
    if (bg.type !== "video" || !bg.mediaId) return;
    const m = useEditor.getState().media[bg.mediaId];
    if (!m || m.type !== "video") return;
    const key = "bg:" + a.clip.id;
    let slot = this.slots.get(key);
    const url = mediaUrl(m);
    if (slot && slot.url !== url) {
      this.dispose(slot);
      slot = undefined;
    }
    if (!slot) {
      const el = document.createElement("video");
      el.preload = "auto";
      el.muted = true;
      el.playsInline = true;
      el.crossOrigin = "anonymous";
      el.src = url;
      const mark = () => (this.dirty = true);
      el.addEventListener("seeked", mark);
      el.addEventListener("loadeddata", mark);
      slot = { el, url, gain: null, lastUsed: now };
      this.slots.set(key, slot);
    }
    slot.lastUsed = now;
    const el = slot.el;
    const want = Math.max(0, Math.min(t - a.clip.start, m.duration - 0.04));
    if (playing && rate > 0 && !a.preroll) {
      el.playbackRate = Math.max(0.0625, Math.min(16, rate));
      if (el.paused) {
        if (Math.abs(el.currentTime - want) > 0.05) el.currentTime = want;
        el.play().catch(() => void 0);
      } else if (Math.abs(el.currentTime - want) > 0.25 * Math.max(1, rate)) el.currentTime = want;
    } else {
      if (!el.paused) el.pause();
      if (!el.seeking && Math.abs(el.currentTime - want) > 0.5 / fps) el.currentTime = want;
    }
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
    if (src instanceof HTMLCanvasElement) return [src.width, src.height];
    const sw = (src as HTMLVideoElement).videoWidth || (src as HTMLImageElement).naturalWidth || a.media.width || W;
    const sh = (src as HTMLVideoElement).videoHeight || (src as HTMLImageElement).naturalHeight || a.media.height || H;
    return [sw, sh];
  }

  /** Draw a clip's picture at timeline time t. Main-track clips without any transform use the plain
   *  fit/fill/blur placement (export: fit_chain); everything else goes through the shared layer
   *  matrix with the export's whole-pixel crop rect (export: layers.motion_layer). */
  private drawLayer(ctx: CanvasRenderingContext2D, a: Active, src: CanvasImageSource, p: Project, k: number, t: number, matte = false) {
    const { width: W, height: H } = p.settings;
    const [sw, sh] = this.sourceSize(src, a, W, H);
    const mode = a.clip.fit ?? (a.isMain ? p.settings.background.mode : "fit");
    if (a.isMain && mode === "blur" && !matte) {
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.drawImage(this.backdrop(src, sw, sh, W, H), 0, 0, W * k, H * k);
      ctx.restore();
    }
    const ext = a.clip.extend && a.media.type === "image" ? extension(a.media.id) : null;
    if (a.isMain && !hasMotion(a.clip) && !ext && !a.cut) {
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
    if (ext?.status === "ready" && !matte) {
      // Photo extension (export: render.ext_layers): the fill image, then the whole photo on top.
      const { fx, fy } = ext.info!;
      ctx.drawImage(ext.img!, 0, 0, ext.img!.naturalWidth, ext.img!.naturalHeight, -fx * sw, -fy * sh, sw * (1 + 2 * fx), sh * (1 + 2 * fy));
      ctx.drawImage(src, 0, 0, sw, sh, 0, 0, sw, sh);
    } else {
      ctx.drawImage(src, x0, y0, rw, rh, x0, y0, rw, rh);
    }
    ctx.restore();
  }

  /** A text clip from its rendered images (export: text.Composer with the same operations). */
  private drawText(ctx: CanvasRenderingContext2D, asset: TextAsset, clip: Clip, t: number, W: number, H: number, k: number) {
    const lay = asset.layout!;
    const R = lay.res, m = lay.m;
    ctx.save();
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    for (const op of textOps(lay, clip, t - clip.start, W, H)) {
      const img = asset.imgs![op.img];
      if (!img) continue;
      const [x0, y0, x1, y1] = op.tile;
      const M = op.m;
      ctx.setTransform(k * M[0], k * M[1], k * M[2], k * M[3], k * M[4], k * M[5]);
      ctx.globalAlpha = Math.max(0, Math.min(1, op.alpha));
      ctx.drawImage(img, (x0 + m) * R, (y0 + m) * R, (x1 - x0) * R, (y1 - y0) * R, x0, y0, x1 - x0, y1 - y0);
    }
    ctx.restore();
  }

  private incomingHalf(a: Active): number {
    if (!a.isMain) return 0;
    for (const pl of this.plansFor.plans.values()) if (pl.toId === a.clip.id) return pl.half;
    return 0;
  }
}
