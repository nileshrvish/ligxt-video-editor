// WebGL2 preview of the grading pipeline in backend/app/grade.py (same order, same maths):
//   blur -> sharpen -> colour LUT + chromatic aberration -> VHS chroma bleed -> glow -> light leak
//   -> scanlines -> vignette -> grain.
// The colour LUT is built by math.ts exactly like grade.build_lut and sampled trilinearly (lut3d
// interp=trilinear). Grain and light leaks use the very same textures the export uses
// (/api/fx/grain.png, /api/fx/leak.png). Spatial sizes are reference pixels at a 1080 px short side,
// scaled to the export size and then to the preview size.
import type { Filter } from "../types";
import { adjustParams, buildLut, effects, GRAIN_FRAMES, hasColor, LUT_SIZE, needsGrade, parseCube, refScale, VHS, type Cube } from "./math";

export interface GradeContext {
  /** Size of this layer in the export (pixels), e.g. 1080x1920 for a main-track clip. */
  exportW: number;
  exportH: number;
  /** Short side the reference-pixel sizes scale with (the export's, even for an overlay layer). */
  refW: number;
  refH: number;
  t: number; // timeline time (s)
  fps: number;
}

const VERT = `#version 300 es
in vec2 pos; void main() { gl_Position = vec4(pos, 0.0, 1.0); }`;

const HEAD = `#version 300 es
precision highp float;
precision highp sampler3D;
uniform sampler2D src;
uniform vec2 R;
out vec4 o;
vec4 tx(vec2 p) { return texture(src, p / R); }       // p in pixels, bottom-up, pixel centres at +0.5
`;

// Separable Gaussian (also used for glow). dir = (1,0) or (0,1); sigma in pixels.
const BLUR = HEAD + `
uniform vec2 dir; uniform float sigma;
void main() {
  vec2 p = gl_FragCoord.xy;
  float r = ceil(3.0 * sigma);
  vec4 acc = vec4(0.0); float wsum = 0.0;
  for (float i = -60.0; i <= 60.0; i += 1.0) {
    if (abs(i) > r) continue;
    float w = exp(-0.5 * i * i / (sigma * sigma));
    acc += tx(p + dir * i) * w; wsum += w;
  }
  o = acc / wsum;
}`;

// Unsharp on luma (5x5 box at export scale -> 'k' preview pixels per export pixel).
const SHARPEN = HEAD + `
uniform float amount; uniform float k;
void main() {
  vec2 p = gl_FragCoord.xy;
  vec3 c = tx(p).rgb;
  vec3 W = vec3(0.299, 0.587, 0.114);
  float y = dot(c, W), yb = 0.0;
  for (int j = -2; j <= 2; j++) for (int i = -2; i <= 2; i++) yb += dot(tx(p + vec2(float(i), float(j)) * k).rgb, W);
  yb /= 25.0;
  o = vec4(clamp(c + (y - yb) * amount, 0.0, 1.0), 1.0);
}`;

// Colour LUT (trilinear, like lut3d) + chromatic aberration (rgbashift: red right, blue left).
const LUTCA = HEAD + `
uniform sampler3D lut; uniform float useLut; uniform float shift;
vec3 grade(vec3 c) {
  if (useLut < 0.5) return c;
  float n = ${LUT_SIZE}.0;
  return texture(lut, (clamp(c, 0.0, 1.0) * (n - 1.0) + 0.5) / n).rgb;
}
void main() {
  vec2 p = gl_FragCoord.xy;
  float r = grade(tx(p - vec2(shift, 0.0)).rgb).r;
  float g = grade(tx(p).rgb).g;
  float b = grade(tx(p + vec2(shift, 0.0)).rgb).b;
  o = vec4(r, g, b, 1.0);
}`;

// VHS chroma bleed: blur Cb/Cr horizontally (yuv444p + gblur planes=6 in the export).
const CHROMA = HEAD + `
uniform float sigma;
vec3 toYuv(vec3 c) { float y = dot(c, vec3(0.299, 0.587, 0.114)); return vec3(y, (c.b - y) * 0.564, (c.r - y) * 0.713); }
vec3 toRgb(vec3 v) { float r = v.x + 1.403 * v.z, b = v.x + 1.773 * v.y; return vec3(r, (v.x - 0.299 * r - 0.114 * b) / 0.587, b); }
void main() {
  vec2 p = gl_FragCoord.xy;
  vec3 c = toYuv(tx(p).rgb);
  float r = ceil(3.0 * sigma); vec2 acc = vec2(0.0); float ws = 0.0;
  for (float i = -60.0; i <= 60.0; i += 1.0) {
    if (abs(i) > r) continue;
    float w = exp(-0.5 * i * i / (sigma * sigma));
    acc += toYuv(tx(p + vec2(i, 0.0)).rgb).yz * w; ws += w;
  }
  o = vec4(clamp(toRgb(vec3(c.x, acc / ws)), 0.0, 1.0), 1.0);
}`;

const THRESH = HEAD + `
uniform float th;
void main() { o = vec4(clamp((tx(gl_FragCoord.xy).rgb - th) / (1.0 - th), 0.0, 1.0), 1.0); }`;

// Glow (screen), light leak (screen), scanlines (multiply), vignette (multiply), grain (grain merge).
const FINAL = HEAD + `
uniform sampler2D glow; uniform float glowAmt;
uniform sampler2D leak; uniform float leakAmt; uniform float leakX; uniform vec2 leakSize; uniform vec2 ex;
uniform float scanAmt; uniform float scanPeriod;
uniform float vigAmt; uniform float vigFeather;
uniform sampler2D grain; uniform float grainAmt; uniform float grainTile; uniform float grainFrames;
vec3 screen(vec3 a, vec3 b, float op) { return a + ((1.0 - (1.0 - a) * (1.0 - b)) - a) * op; }
float ss(float e0, float e1, float x) { float t = clamp((x - e0) / (e1 - e0), 0.0, 1.0); return t * t * (3.0 - 2.0 * t); }
void main() {
  vec2 p = gl_FragCoord.xy;
  vec2 uv = vec2(p.x / R.x, 1.0 - p.y / R.y);          // top-origin, like FFmpeg
  vec3 c = tx(p).rgb;
  if (glowAmt > 0.0) c = screen(c, texture(glow, p / R).rgb, glowAmt);
  if (leakAmt > 0.0) {
    vec2 lp = vec2(floor(leakX) + uv.x * ex.x, uv.y * ex.y) / leakSize;   // crop x in export pixels
    c = screen(c, texture(leak, lp).rgb, leakAmt);                       // uploaded top row first
  }
  if (scanAmt > 0.0) {
    // Box-average of 1 - 0.18*a*(0.5 + 0.5*cos(2*pi*y/P)) over the export rows this pixel covers.
    float y0 = uv.y * ex.y - 0.5 * ex.y / R.y, y1 = y0 + ex.y / R.y;
    float w = 6.2831853 / scanPeriod;
    float m = (y1 - y0) < 0.5 ? cos(w * 0.5 * (y0 + y1)) : (sin(w * y1) - sin(w * y0)) / (w * (y1 - y0));
    c *= 1.0 - 0.18 * scanAmt * (0.5 + 0.5 * m);
  }
  if (vigAmt > 0.0) {
    float d = length(uv - 0.5) / 0.70710678;
    float r0 = 0.25 + 0.4 * (1.0 - vigFeather);
    c *= 1.0 - vigAmt * ss(r0, r0 + 0.3 + 0.5 * vigFeather, d);
  }
  if (grainAmt > 0.0) {
    float n = texture(grain, vec2(uv.x, (grainTile + uv.y) / grainFrames)).r;
    c += (n - 128.0 / 255.0) * grainAmt;
  }
  o = vec4(clamp(c, 0.0, 1.0), 1.0);
}`;

type Prog = { p: WebGLProgram; u: Record<string, WebGLUniformLocation | null> };

export class FilterRenderer {
  readonly canvas = document.createElement("canvas");
  private gl: WebGL2RenderingContext | null;
  private progs: Record<string, Prog> = {};
  private fbos: { tex: WebGLTexture; fb: WebGLFramebuffer; w: number; h: number }[] = [];
  private srcTex: WebGLTexture | null = null;
  private lutTex: WebGLTexture | null = null;
  private lutKey = "";
  private images = new Map<string, { img: HTMLImageElement; tex: WebGLTexture | null }>();
  private cubes = new Map<string, Cube | "loading" | "error">();
  failed = false;
  /** Called when an async asset (LUT file, grain/leak texture) finished loading. */
  onAsset: () => void = () => void 0;

  constructor() {
    this.gl = this.canvas.getContext("webgl2", { premultipliedAlpha: false, preserveDrawingBuffer: true, antialias: false });
    if (!this.gl) {
      this.failed = true;
      return;
    }
    try {
      const gl = this.gl;
      const buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      for (const [name, src] of Object.entries({ blur: BLUR, sharpen: SHARPEN, lutca: LUTCA, chroma: CHROMA, thresh: THRESH, final: FINAL })) {
        this.progs[name] = this.compile(src);
      }
    } catch (e) {
      console.error("[ligxt] WebGL2 grading unavailable:", e);
      this.failed = true;
    }
  }

  private compile(fragSrc: string): Prog {
    const gl = this.gl!;
    const sh = (type: number, src: string) => {
      const s = gl.createShader(type)!;
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? "shader");
      return s;
    };
    const p = gl.createProgram()!;
    gl.attachShader(p, sh(gl.VERTEX_SHADER, VERT));
    gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fragSrc));
    gl.bindAttribLocation(p, 0, "pos");
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) ?? "link");
    const u: Prog["u"] = {};
    const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (let i = 0; i < n; i++) {
      const name = gl.getActiveUniform(p, i)!.name;
      u[name] = gl.getUniformLocation(p, name);
    }
    return { p, u };
  }

  private texture(): WebGLTexture {
    const gl = this.gl!;
    const t = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  private fbo(i: number, w: number, h: number) {
    const gl = this.gl!;
    let f = this.fbos[i];
    if (!f || f.w !== w || f.h !== h) {
      if (f) {
        gl.deleteTexture(f.tex);
        gl.deleteFramebuffer(f.fb);
      }
      const tex = this.texture();
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      const fb = gl.createFramebuffer()!;
      gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      f = this.fbos[i] = { tex, fb, w, h };
    }
    return f;
  }

  /** Run one pass from texture `src` into fbo `dst` (-1 = the output canvas). */
  private pass(name: string, src: WebGLTexture, dst: number, w: number, h: number, set: (u: Prog["u"], gl: WebGL2RenderingContext) => void) {
    const gl = this.gl!;
    const pr = this.progs[name];
    gl.useProgram(pr.p);
    gl.bindFramebuffer(gl.FRAMEBUFFER, dst < 0 ? null : this.fbo(dst, w, h).fb);
    gl.viewport(0, 0, w, h);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, src);
    gl.uniform1i(pr.u.src, 0);
    gl.uniform2f(pr.u.R, w, h);
    set(pr.u, gl);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  /** A generated texture from the backend (same file the export uses). Returns null while loading. */
  private image(url: string): WebGLTexture | null {
    let e = this.images.get(url);
    if (!e) {
      const img = new Image();
      e = { img, tex: null };
      this.images.set(url, e);
      img.onload = () => {
        const gl = this.gl!;
        const t = this.texture();
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, img);
        e!.tex = t;
        this.onAsset();
      };
      img.src = url;
    }
    return e.tex;
  }

  private cubesFor(filters: Filter[]): Record<string, Cube> | null {
    const out: Record<string, Cube> = {};
    let ready = true;
    for (const f of filters) {
      if (f.type !== "lut" || f.enabled === false) continue;
      const id = String((f.params as Record<string, unknown>)?.lut ?? "");
      if (!id) continue;
      const c = this.cubes.get(id);
      if (c === undefined) {
        this.cubes.set(id, "loading");
        fetch(`/api/luts/file?id=${encodeURIComponent(id)}`)
          .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
          .then((t) => this.cubes.set(id, parseCube(t)))
          .catch(() => this.cubes.set(id, "error"))
          .finally(() => this.onAsset());
        ready = false;
      } else if (c === "loading") ready = false;
      else if (c !== "error") out[id] = c;
    }
    return ready ? out : null;
  }

  private uploadLut(filters: Filter[], cubes: Record<string, Cube>) {
    const gl = this.gl!;
    const key = JSON.stringify(filters.map((f) => [f.type, f.name, f.intensity, f.params, f.enabled]));
    if (key === this.lutKey && this.lutTex) return;
    const data = buildLut(filters, cubes);
    if (!this.lutTex) {
      this.lutTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_3D, this.lutTex);
      gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      for (const p of [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T, gl.TEXTURE_WRAP_R]) gl.texParameteri(gl.TEXTURE_3D, p, gl.CLAMP_TO_EDGE);
    }
    gl.bindTexture(gl.TEXTURE_3D, this.lutTex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGB16F, LUT_SIZE, LUT_SIZE, LUT_SIZE, 0, gl.RGB, gl.FLOAT, data);
    this.lutKey = key;
  }

  /**
   * Grade `input` (a canvas holding this layer at preview resolution). Returns the graded canvas,
   * or null when an asset is still loading (the caller keeps the previous frame).
   */
  apply(input: HTMLCanvasElement, filters: Filter[], g: GradeContext): HTMLCanvasElement | null {
    if (this.failed || !this.gl || !needsGrade(filters)) return input;
    const gl = this.gl;
    const w = input.width, h = input.height;
    const cubes = this.cubesFor(filters);
    if (!cubes) return null;
    const fx = effects(filters);
    const p = adjustParams(filters);
    const rs = refScale(g.refW, g.refH);
    const k = w / g.exportW; // preview pixels per export pixel
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    // Assets that must be loaded before this frame can be drawn.
    let grainTex: WebGLTexture | null = null, leakTex: WebGLTexture | null = null;
    let grainSize = 1;
    if (fx.grain.amount > 0) {
      grainSize = Math.max(1, fx.grain.size * rs);
      grainTex = this.image(`/api/fx/grain.png?w=${g.exportW}&h=${g.exportH}&s=${grainSize}`);
      if (!grainTex) return null;
    }
    if (fx.lightleak.amount > 0) {
      leakTex = this.image(`/api/fx/leak.png?w=${g.exportW}&h=${g.exportH}`);
      if (!leakTex) return null;
    }

    if (!this.srcTex) this.srcTex = this.texture();
    gl.bindTexture(gl.TEXTURE_2D, this.srcTex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true); // bottom-up like the framebuffers
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, input);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);

    let cur: WebGLTexture = this.srcTex;
    let curSlot = -1; // -1 = the uploaded source texture
    let keep = -1; // a slot that must not be overwritten (the image under the glow)
    const run = (name: string, set: (u: Prog["u"], gl: WebGL2RenderingContext) => void) => {
      let dst = 0;
      while (dst === curSlot || dst === keep) dst++; // never write the input or a kept image
      this.pass(name, cur, dst, w, h, set);
      cur = this.fbos[dst].tex;
      curSlot = dst;
    };

    const blur = (Number(p.blur ?? 0) / 100) * 20 * rs * k;
    if (blur >= 0.3 * k && blur > 0.05) {
      run("blur", (u, gl) => { gl.uniform2f(u.dir, 1, 0); gl.uniform1f(u.sigma, blur); });
      run("blur", (u, gl) => { gl.uniform2f(u.dir, 0, 1); gl.uniform1f(u.sigma, blur); });
    }
    const sharp = (Number(p.sharpen ?? 0) / 100) * 1.5;
    if (sharp > 0) run("sharpen", (u, gl) => { gl.uniform1f(u.amount, sharp); gl.uniform1f(u.k, Math.max(k, 0.0001)); });

    const useLut = hasColor(filters);
    if (useLut) this.uploadLut(filters, cubes);
    const ca = Math.round(fx.chroma.amount * 6 * rs + fx.vhs.amount * VHS.chroma * rs);
    if (useLut || ca >= 1) {
      run("lutca", (u, gl) => {
        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_3D, this.lutTex);
        gl.uniform1i(u.lut, 1);
        gl.uniform1f(u.useLut, useLut ? 1 : 0);
        gl.uniform1f(u.shift, ca >= 1 ? ca * k : 0);
      });
    }
    if (fx.vhs.amount > 0 && 4 * rs * fx.vhs.amount >= 0.3) {
      run("chroma", (u, gl) => gl.uniform1f(u.sigma, Math.max(0.05, 4 * rs * fx.vhs.amount * k)));
    }
    let glowTex: WebGLTexture | null = null;
    if (fx.glow.amount > 0) {
      const base = cur;
      const baseSlot = curSlot;
      keep = baseSlot;
      const sig = Math.max(0.05, Math.max(0.5, fx.glow.radius * rs) * k);
      run("thresh", (u, gl) => gl.uniform1f(u.th, Math.min(0.98, fx.glow.threshold)));
      run("blur", (u, gl) => { gl.uniform2f(u.dir, 1, 0); gl.uniform1f(u.sigma, sig); });
      run("blur", (u, gl) => { gl.uniform2f(u.dir, 0, 1); gl.uniform1f(u.sigma, sig); });
      glowTex = cur;
      cur = base;
      curSlot = baseSlot;
    }
    const W2 = 2 * g.exportW;
    const leakX = (W2 - g.exportW) * (0.5 + 0.5 * Math.sin(g.t * fx.lightleak.speed));
    const tile = ((Math.round(g.t * g.fps) % GRAIN_FRAMES) + GRAIN_FRAMES) % GRAIN_FRAMES;
    this.pass("final", cur, -1, w, h, (u, gl) => {
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, glowTex ?? cur);
      gl.uniform1i(u.glow, 1);
      gl.uniform1f(u.glowAmt, glowTex ? fx.glow.amount : 0);
      gl.activeTexture(gl.TEXTURE2);
      gl.bindTexture(gl.TEXTURE_2D, leakTex ?? cur);
      gl.uniform1i(u.leak, 2);
      gl.uniform1f(u.leakAmt, leakTex ? fx.lightleak.amount : 0);
      gl.uniform1f(u.leakX, leakX);
      gl.uniform2f(u.leakSize, W2, g.exportH);
      gl.uniform2f(u.ex, g.exportW, g.exportH);
      gl.uniform1f(u.scanAmt, fx.vhs.amount);
      gl.uniform1f(u.scanPeriod, 3 * rs);
      gl.uniform1f(u.vigAmt, fx.vignette.amount);
      gl.uniform1f(u.vigFeather, fx.vignette.feather);
      gl.activeTexture(gl.TEXTURE3);
      gl.bindTexture(gl.TEXTURE_2D, grainTex ?? cur);
      gl.uniform1i(u.grain, 3);
      gl.uniform1f(u.grainAmt, grainTex ? fx.grain.amount * 0.5 : 0);
      gl.uniform1f(u.grainTile, tile);
      gl.uniform1f(u.grainFrames, GRAIN_FRAMES);
    });
    return this.canvas;
  }
}
