// WebGL2 compositing of cutout clips in the preview (export: backend/app/cutout_render.py), same order:
//   background (keep | blurred | colour | image | video, then the background-only grade)
//   -> shadow (matte shifted + blurred) -> glow (matte blurred, levels) -> subject (alpha = matte).
// All inputs are canvases at preview resolution; sizes in canvas pixels are scaled by k.

const VERT = `#version 300 es
in vec2 pos; void main() { gl_Position = vec4(pos, 0.0, 1.0); }`;

const HEAD = `#version 300 es
precision highp float;
uniform vec2 R;
out vec4 o;
`;

// Separable Gaussian, sigma in pixels (gblur in the export).
const BLUR = HEAD + `
uniform sampler2D src; uniform vec2 dir; uniform float sigma;
void main() {
  vec2 p = gl_FragCoord.xy;
  if (sigma < 0.3) { o = texture(src, p / R); return; }
  int r = int(min(ceil(3.0 * sigma), 96.0));
  vec4 acc = vec4(0.0); float ws = 0.0;
  for (int i = -r; i <= r; i++) {
    float w = exp(-0.5 * float(i * i) / (sigma * sigma));
    acc += w * texture(src, (p + dir * float(i)) / R);
    ws += w;
  }
  o = acc / ws;
}`;

const COMP = HEAD + `
uniform sampler2D tB, tS, tM, tG, tSh;
uniform int hasB, glowOn, shadowOn, alphaFromM;
uniform vec3 gC, sC;
uniform float gLo, gHi, gOp, sOp;
uniform vec2 sOff;
void main() {
  vec2 uv = gl_FragCoord.xy / R;
  vec4 b = hasB == 1 ? texture(tB, uv) : vec4(0.0);
  vec4 acc = vec4(b.rgb * b.a, b.a);                       // premultiplied "over" from the bottom up
  if (shadowOn == 1) {
    vec2 q = (gl_FragCoord.xy - sOff) / R;
    float a = (q.x < 0.0 || q.y < 0.0 || q.x > 1.0 || q.y > 1.0) ? 0.0 : texture(tSh, q).r * sOp;
    acc = vec4(sC * a, a) + acc * (1.0 - a);
  }
  if (glowOn == 1) {
    float g = texture(tG, uv).r;
    float a = clamp((g - gLo) / max(1e-6, gHi - gLo), 0.0, 1.0) * gOp;
    acc = vec4(gC * a, a) + acc * (1.0 - a);
  }
  vec4 s = texture(tS, uv);
  float m = alphaFromM == 1 ? texture(tM, uv).r : s.a;
  acc = vec4(s.rgb * m, m) + acc * (1.0 - m);
  o = acc;
}`;

const MASK = HEAD + `
uniform sampler2D tP, tM;
void main() { vec2 uv = gl_FragCoord.xy / R; float m = texture(tM, uv).r; o = vec4(texture(tP, uv).rgb * m, m); }`;

type Prog = { p: WebGLProgram; u: Record<string, WebGLUniformLocation | null> };
type Target = { tex: WebGLTexture; fb: WebGLFramebuffer; w: number; h: number };

export interface CompArgs {
  /** Subject picture (straight alpha). */
  S: HTMLCanvasElement;
  /** Background picture, or null for transparent (overlay tracks). */
  B: HTMLCanvasElement | null;
  /** Matte on the canvas (red channel). */
  M: HTMLCanvasElement;
  /** Background blur, sigma in preview pixels (0 = none). */
  bgBlur: number;
  glow: { sigma: number; lo: number; hi: number; op: number; color: string } | null;
  shadow: { sigma: number; dx: number; dy: number; op: number; color: string } | null;
  /** true: the subject's alpha is the matte; false: the subject picture's own alpha (overlay "keep"). */
  alphaFromM: boolean;
}

function rgb(hex: string): [number, number, number] {
  const h = (hex || "#000000").replace("#", "").padEnd(6, "0");
  return [parseInt(h.slice(0, 2), 16) / 255, parseInt(h.slice(2, 4), 16) / 255, parseInt(h.slice(4, 6), 16) / 255];
}

export class CutoutRenderer {
  readonly canvas = document.createElement("canvas");
  private gl: WebGL2RenderingContext | null;
  private progs: Record<string, Prog> = {};
  private inputs = new Map<string, WebGLTexture>();
  private targets = new Map<string, Target>();
  failed = false;

  constructor() {
    this.gl = this.canvas.getContext("webgl2", { premultipliedAlpha: true, preserveDrawingBuffer: true, antialias: false });
    if (!this.gl) {
      this.failed = true;
      return;
    }
    try {
      const gl = this.gl;
      const buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      this.progs.blur = this.compile(BLUR);
      this.progs.comp = this.compile(COMP);
      this.progs.mask = this.compile(MASK);
    } catch (e) {
      console.error("[ligxt] cutout preview unavailable:", e);
      this.failed = true;
    }
  }

  private compile(frag: string): Prog {
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
    gl.attachShader(p, sh(gl.FRAGMENT_SHADER, frag));
    gl.bindAttribLocation(p, 0, "pos");
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) ?? "link");
    const u: Prog["u"] = {};
    const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS) as number;
    for (let i = 0; i < n; i++) {
      const name = gl.getActiveUniform(p, i)!.name;
      u[name] = gl.getUniformLocation(p, name);
    }
    return { p, u };
  }

  private tex(): WebGLTexture {
    const gl = this.gl!;
    const t = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  private upload(name: string, c: HTMLCanvasElement): WebGLTexture {
    const gl = this.gl!;
    let t = this.inputs.get(name);
    if (!t) this.inputs.set(name, (t = this.tex()));
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, c);
    return t;
  }

  private target(name: string, w: number, h: number): Target {
    const gl = this.gl!;
    let t = this.targets.get(name);
    if (!t || t.w !== w || t.h !== h) {
      const tex = t?.tex ?? this.tex();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      const fb = t?.fb ?? gl.createFramebuffer()!;
      gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      t = { tex, fb, w, h };
      this.targets.set(name, t);
    }
    return t;
  }

  private draw(prog: Prog, fb: WebGLFramebuffer | null, w: number, h: number, tex: [string, WebGLTexture][], set: (u: Prog["u"]) => void) {
    const gl = this.gl!;
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.viewport(0, 0, w, h);
    gl.useProgram(prog.p);
    tex.forEach(([name, t], i) => {
      gl.activeTexture(gl.TEXTURE0 + i);
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.uniform1i(prog.u[name], i);
    });
    gl.uniform2f(prog.u.R, w, h);
    set(prog.u);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  /** Gaussian blur of a texture into the target `name` (two passes). */
  private blur(name: string, src: WebGLTexture, w: number, h: number, sigma: number): WebGLTexture {
    const a = this.target(name + "_h", w, h), b = this.target(name, w, h);
    this.draw(this.progs.blur, a.fb, w, h, [["src", src]], (u) => { this.gl!.uniform2f(u.dir, 1, 0); this.gl!.uniform1f(u.sigma, sigma); });
    this.draw(this.progs.blur, b.fb, w, h, [["src", a.tex]], (u) => { this.gl!.uniform2f(u.dir, 0, 1); this.gl!.uniform1f(u.sigma, sigma); });
    return b.tex;
  }

  /** The composed cutout clip at the size of the inputs (premultiplied alpha). */
  compose(a: CompArgs): HTMLCanvasElement | null {
    if (this.failed || !this.gl) return null;
    const gl = this.gl;
    const w = a.S.width, h = a.S.height;
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    const tS = this.upload("S", a.S);
    const tM = this.upload("M", a.M);
    let tB = a.B ? this.upload("B", a.B) : tS;
    if (a.B && a.bgBlur >= 0.3) tB = this.blur("Bb", tB, w, h, a.bgBlur);
    const tG = a.glow ? this.blur("G", tM, w, h, a.glow.sigma) : tM;
    const tSh = a.shadow ? this.blur("Sh", tM, w, h, a.shadow.sigma) : tM;
    this.draw(this.progs.comp, null, w, h, [["tB", tB], ["tS", tS], ["tM", tM], ["tG", tG], ["tSh", tSh]], (u) => {
      gl.uniform1i(u.hasB, a.B ? 1 : 0);
      gl.uniform1i(u.alphaFromM, a.alphaFromM ? 1 : 0);
      gl.uniform1i(u.glowOn, a.glow ? 1 : 0);
      gl.uniform1i(u.shadowOn, a.shadow ? 1 : 0);
      if (a.glow) {
        gl.uniform3f(u.gC, ...rgb(a.glow.color));
        gl.uniform1f(u.gLo, a.glow.lo);
        gl.uniform1f(u.gHi, a.glow.hi);
        gl.uniform1f(u.gOp, a.glow.op);
      }
      if (a.shadow) {
        gl.uniform3f(u.sC, ...rgb(a.shadow.color));
        gl.uniform1f(u.sOp, a.shadow.op);
        gl.uniform2f(u.sOff, a.shadow.dx, -a.shadow.dy); // GL y is up
      }
    });
    return this.canvas;
  }

  /** P cut out by the matte M (premultiplied): the subject, for text behind the subject. */
  mask(P: HTMLCanvasElement, M: HTMLCanvasElement): HTMLCanvasElement | null {
    if (this.failed || !this.gl) return null;
    const w = P.width, h = P.height;
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    this.draw(this.progs.mask, null, w, h, [["tP", this.upload("P", P)], ["tM", this.upload("M", M)]], () => void 0);
    return this.canvas;
  }
}
