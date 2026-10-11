// Preview renderer for transitions.
// The xfade types are a line-by-line GLSL port of FFmpeg's libavfilter/vf_xfade.c (8.x), so the
// preview matches the export. As in FFmpeg, `P` (progress) runs from 1 at the first frame of the
// transition to 0 at the end, and mix(a, b, m) = a*m + b*(1-m).
// The whip pans are our own transition (no FFmpeg equivalent) and mirror render.whip_chain exactly:
// an eased slide, averaged over 8 sub-frames at 960 fps for motion blur.

const XFADE = [
  "fade", "dissolve", "fadeblack", "fadewhite", "slideleft", "slideright", "slideup", "slidedown",
  "wipeleft", "wiperight", "wipeup", "wipedown", "zoomin", "circleopen", "circleclose", "pixelize", "radial",
];

const VERT = `
attribute vec2 pos;
void main() { gl_Position = vec4(pos, 0.0, 1.0); }`;

const FRAG = `
precision highp float;
uniform sampler2D A;
uniform sampler2D B;
uniform float P;
uniform vec2 R;   // output size in pixels
uniform vec2 RA;  // size of texture A (full project resolution for zoomin, else = R)
uniform int T;

// Sample a frame at integer pixel (x, y) with y = 0 at the top (FFmpeg's convention).
vec4 at(sampler2D s, float x, float y) { return texture2D(s, (vec2(x, y) + 0.5) / R); }
float mixf(float a, float b, float m) { return a * m + b * (1.0 - m); }
vec4 mixv(vec4 a, vec4 b, float m) { return a * m + b * (1.0 - m); }
float sstep(float e0, float e1, float x) { float t = clamp((x - e0) / (e1 - e0), 0.0, 1.0); return t * t * (3.0 - 2.0 * t); }
// FFmpeg uses fract(sin(..) * 43758.5) in float32; GPU sin() loses precision on large arguments and
// gives a banded pattern, so use a sine-free hash with the same uniform distribution.
float frand(float x, float y) {
  vec3 p3 = fract(vec3(x, y, x) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}
float imod(float a, float b) { return a - b * floor(a / b); }

void main() {
  float W = R.x, H = R.y;
  float x = floor(gl_FragCoord.x);
  float y = floor(H - gl_FragCoord.y);
  vec4 a = at(A, x, y), b = at(B, x, y);
  vec4 o = a;
  if (T == 0) { o = mixv(a, b, P); }                                                   // fade
  else if (T == 1) { o = (frand(x, y) * 2.0 + P * 2.0 - 1.5) >= 0.5 ? a : b; }        // dissolve
  else if (T == 2 || T == 3) {                                                          // fadeblack / fadewhite
    vec4 bg = T == 2 ? vec4(0.0, 0.0, 0.0, 1.0) : vec4(1.0);
    o = mixv(mixv(a, bg, sstep(0.8, 1.0, P)), mixv(bg, b, sstep(0.2, 1.0, P)), P);
  }
  else if (T >= 4 && T <= 7) {                                                          // slides
    bool horiz = T <= 5;
    float L = horiz ? W : H;
    float z = (T == 4 || T == 6) ? -P * L : P * L;
    z = z < 0.0 ? ceil(z) : floor(z);                                                   // C int truncation
    float zc = z + (horiz ? x : y);
    float zz = imod(zc, L);
    bool inB = zc >= 0.0 && zc < L;
    o = horiz ? (inB ? at(B, zz, y) : at(A, zz, y)) : (inB ? at(B, x, zz) : at(A, x, zz));
  }
  else if (T == 8) { o = x > floor(W * P) ? b : a; }                                    // wipeleft
  else if (T == 9) { o = x > floor(W * (1.0 - P)) ? a : b; }                            // wiperight
  else if (T == 10) { o = y > floor(H * P) ? b : a; }                                   // wipeup
  else if (T == 11) { o = y > floor(H * (1.0 - P)) ? a : b; }                           // wipedown
  else if (T == 12) {                                                                   // zoomin
    float zf = sstep(0.5, 1.0, P);
    float u = 0.5 + (x / W - 0.5) * zf, v = 0.5 + (y / H - 0.5) * zf;
    // FFmpeg samples A at full resolution: use A's own size so it is the same source pixel. The export
    // pipeline is yuv420p, so that pixel's colour (chroma) is shared with its 2x2 block; approximate it
    // with a box average (BT.601), which matters only because zoomin magnifies a single pixel.
    vec2 sp = vec2(ceil(u * (RA.x - 1.0)), ceil(v * (RA.y - 1.0)));
    vec2 bp = floor(sp / 2.0) * 2.0;
    vec3 c0 = texture2D(A, (sp + 0.5) / RA).rgb;
    vec3 q0 = texture2D(A, (bp + vec2(0.5, 0.5)) / RA).rgb, q1 = texture2D(A, (bp + vec2(1.5, 0.5)) / RA).rgb;
    vec3 q2 = texture2D(A, (bp + vec2(0.5, 1.5)) / RA).rgb, q3 = texture2D(A, (bp + vec2(1.5, 1.5)) / RA).rgb;
    vec3 Wy = vec3(0.299, 0.587, 0.114);
    float Y = dot(c0, Wy);
    vec3 qa = (q0 + q1 + q2 + q3) * 0.25;
    float Ya = dot(qa, Wy);
    float Cb = (qa.b - Ya) * 0.564, Cr = (qa.r - Ya) * 0.713;
    float Rr = Y + 1.403 * Cr, Bb = Y + 1.773 * Cb;
    vec4 zv = vec4(clamp(vec3(Rr, (Y - 0.299 * Rr - 0.114 * Bb) / 0.587, Bb), 0.0, 1.0), 1.0);
    o = mixv(zv, b, sstep(0.0, 0.5, P));
  }
  else if (T == 13 || T == 14) {                                                        // circleopen / close
    float hw = floor(W / 2.0), hh = floor(H / 2.0);
    float z = length(vec2(hw, hh));
    float p = (T == 13 ? P - 0.5 : 0.5 - P) * 3.0;
    float sm = length(vec2(x - hw, y - hh)) / z + p;
    o = T == 13 ? mixv(a, b, sstep(0.0, 1.0, sm)) : mixv(b, a, sstep(0.0, 1.0, sm));
  }
  else if (T == 15) {                                                                   // pixelize
    float d = min(P, 1.0 - P);
    float dist = ceil(d * 50.0) / 50.0;
    float sq = 2.0 * dist * min(W, H) / 20.0;
    float sx = dist > 0.0 ? floor(min((floor(x / sq) + 0.5) * sq, W - 1.0)) : x;
    float sy = dist > 0.0 ? floor(min((floor(y / sq) + 0.5) * sq, H - 1.0)) : y;
    o = mixv(at(A, sx, sy), at(B, sx, sy), P);
  }
  else if (T == 16) {                                                                   // radial
    float sm = atan(x - floor(W / 2.0), y - floor(H / 2.0)) - (P - 0.5) * (3.14159265 * 2.5);
    o = mixv(b, a, sstep(0.0, 1.0, sm));
  }
  gl_FragColor = vec4(o.rgb, 1.0);
}`;

export const WHIP_FPS = 960; // must match render.WHIP_FPS
export const WHIP_SAMPLES = 32; // must match render.WHIP_SAMPLES

export const easeInOutCubic = (u: number) => {
  u = Math.min(1, Math.max(0, u));
  return u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2;
};

/** Offsets (in pixels) of A and B for a whip at eased position e. Mirrors render.whip_chain. */
export function whipOffsets(kind: string, e: number, W: number, H: number) {
  switch (kind) {
    case "whipright": return { ax: W * e, ay: 0, bx: -W + W * e, by: 0 };
    case "whipup": return { ax: 0, ay: -H * e, bx: 0, by: H - H * e };
    case "whipdown": return { ax: 0, ay: H * e, bx: 0, by: -H + H * e };
    default: return { ax: -W * e, ay: 0, bx: W - W * e, by: 0 };
  }
}

export class TransitionRenderer {
  readonly canvas = document.createElement("canvas");
  private gl: WebGLRenderingContext | null;
  private prog: WebGLProgram | null = null;
  private texA: WebGLTexture | null = null;
  private texB: WebGLTexture | null = null;
  private loc: Record<string, WebGLUniformLocation | null> = {};
  private whipCanvas = document.createElement("canvas");
  private fps = 30;
  failed = false;

  constructor() {
    this.gl = this.canvas.getContext("webgl", { premultipliedAlpha: false, preserveDrawingBuffer: true, antialias: false });
    if (!this.gl) {
      this.failed = true;
      return;
    }
    try {
      this.init(this.gl);
    } catch (e) {
      console.error("[ligxt] WebGL transition shader failed:", e);
      this.failed = true;
    }
  }

  private init(gl: WebGLRenderingContext) {
    const sh = (type: number, src: string) => {
      const s = gl.createShader(type)!;
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? "shader error");
      return s;
    };
    const prog = gl.createProgram()!;
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) ?? "link error");
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const pos = gl.getAttribLocation(prog, "pos");
    gl.enableVertexAttribArray(pos);
    gl.vertexAttribPointer(pos, 2, gl.FLOAT, false, 0, 0);
    for (const n of ["A", "B", "P", "R", "RA", "T"]) this.loc[n] = gl.getUniformLocation(prog, n);
    const tex = (unit: number) => {
      const t = gl.createTexture()!;
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      return t;
    };
    this.texA = tex(0);
    this.texB = tex(1);
    gl.uniform1i(this.loc.A, 0);
    gl.uniform1i(this.loc.B, 1);
    this.prog = prog;
  }

  /**
   * Composite two full frames (same size) for transition `type`.
   * `elapsed` = seconds since the transition started, `duration` = its length; `fps` = project fps.
   */
  render(type: string, a: HTMLCanvasElement, b: HTMLCanvasElement, elapsed: number, duration: number, fps: number): HTMLCanvasElement {
    // Output size follows B; A may be larger (full resolution for zoomin).
    const w = b.width;
    const h = b.height;
    this.fps = fps;
    if (type.startsWith("whip")) return this.whip(type, a, b, elapsed, duration, w, h);
    // FFmpeg evaluates progress per output frame: P = 1 - k / nframes.
    const nf = Math.max(1, Math.round(duration * fps));
    const k = Math.min(nf - 1, Math.max(0, Math.floor(elapsed * fps + 1e-6)));
    const P = 1 - k / nf;
    const gl = this.gl;
    const t = XFADE.indexOf(type);
    if (!gl || this.failed || t < 0) return this.fallback(a, b, P, w, h);
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    gl.viewport(0, 0, w, h);
    gl.useProgram(this.prog);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.texA);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, a);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.texB);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, b);
    gl.uniform1f(this.loc.P, P);
    gl.uniform2f(this.loc.R, w, h);
    gl.uniform2f(this.loc.RA, a.width, a.height);
    gl.uniform1i(this.loc.T, t);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    return this.canvas;
  }

  private whip(type: string, a: HTMLCanvasElement, b: HTMLCanvasElement, elapsed: number, duration: number, w: number, h: number) {
    const c = this.whipCanvas;
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
    }
    const ctx = c.getContext("2d")!;
    ctx.globalAlpha = 1;
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, w, h);
    // Same sub-frames as the export: fps=<project fps> keeps the LAST 240 fps sub-frame that rounds
    // to output frame k (n_last = ceil((k + 0.5) * 240 / fps) - 1), and tmix=frames=8 averages
    // sub-frames n_last-7 .. n_last (only those that exist). Measured with ffmpeg 8.1: at 30 fps that
    // is sub-frames 8k-4 .. 8k+3, a window centred on the frame time.
    const k = Math.round(elapsed * this.fps);
    const nLast = Math.ceil(((k + 0.5) * WHIP_FPS) / this.fps) - 1;
    let n = 0;
    for (let j = 0; j < WHIP_SAMPLES; j++) {
      const sub = nLast - j;
      if (sub < 0) break;
      const tj = sub / WHIP_FPS;
      const o = whipOffsets(type, easeInOutCubic(tj / duration), w, h);
      n++;
      ctx.globalAlpha = 1 / n; // running average with equal weights
      if (n === 1) ctx.globalAlpha = 1;
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, w, h);
      ctx.clip();
      // Each sub-frame is A and B side by side on black, drawn as one layer.
      this.subFrame(a, b, o, w, h);
      ctx.drawImage(this.tmp, 0, 0);
      ctx.restore();
    }
    ctx.globalAlpha = 1;
    return c;
  }

  private tmp = document.createElement("canvas");
  private subFrame(a: HTMLCanvasElement, b: HTMLCanvasElement, o: ReturnType<typeof whipOffsets>, w: number, h: number) {
    if (this.tmp.width !== w || this.tmp.height !== h) {
      this.tmp.width = w;
      this.tmp.height = h;
    }
    const t = this.tmp.getContext("2d")!;
    t.fillStyle = "#000";
    t.fillRect(0, 0, w, h);
    t.drawImage(a, Math.round(o.ax), Math.round(o.ay));
    t.drawImage(b, Math.round(o.bx), Math.round(o.by));
  }

  private fallback(a: HTMLCanvasElement, b: HTMLCanvasElement, P: number, w: number, h: number) {
    const c = this.whipCanvas;
    c.width = w;
    c.height = h;
    const ctx = c.getContext("2d")!;
    ctx.globalAlpha = 1;
    ctx.drawImage(a, 0, 0);
    ctx.globalAlpha = 1 - P;
    ctx.drawImage(b, 0, 0);
    ctx.globalAlpha = 1;
    return c;
  }
}
