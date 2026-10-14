// Motion tracking on the preview: the track's path around the playhead (uncertain / lost frames in red),
// the tracked box on this frame (drag it, or a corner, to fix a bad frame), and the tools: draw a box,
// pick one of several faces, click the object for the AI tracker.
import { useRef, useState } from "react";
import { clipDuration, invert, srcTime } from "../motion/motion";
import { useEditor } from "../store/editor";
import { seedTime, trackBox, trackFace } from "../tracking/actions";
import { trackById } from "../tracking/edit";
import { hostMatrix, LOW_CONF, trackAt } from "../tracking/follow";
import type { Clip, MediaMeta, Project } from "../types";

type Box = [number, number, number, number];

function context(p: Project, media: Record<string, MediaMeta>, selId: string | null, ui: ReturnType<typeof useEditor.getState>["trackUi"], playhead: number) {
  if (!selId) return null;
  const main = p.tracks.find((t) => t.type === "video");
  const all = p.tracks.filter((t) => t.type === "video" || t.type === "overlay").flatMap((t) => t.clips.map((c) => ({ c, isMain: t === main })));
  const sel = p.tracks.flatMap((t) => t.clips).find((c) => c.id === selId);
  if (!sel) return null;
  let host: { c: Clip; isMain: boolean } | undefined;
  let trackId: string | null = null;
  if (sel.mediaId && media[sel.mediaId]?.type === "video") {
    host = all.find((h) => h.c.id === sel.id);
    trackId = ui.clipId === sel.id ? ui.trackId : null;
  } else if (sel.follow?.trackId) {
    const tr = trackById(p, sel.follow.trackId);
    trackId = sel.follow.trackId;
    const covers = (c: Clip) => playhead >= c.start && playhead < c.start + clipDuration(c);
    host = all.find((h) => h.c.id === sel.follow!.clipId && covers(h.c)) ?? all.find((h) => h.c.mediaId === tr?.mediaId && covers(h.c));
  }
  if (!host) return null;
  const m = media[host.c.mediaId ?? ""];
  if (!m) return null;
  return { host: host.c, isMain: host.isMain, m, track: trackId ? trackById(p, trackId) : undefined, sel };
}

export default function TrackLayer({ fw }: { fw: number; fh?: number }) {
  const p = useEditor((s) => s.project);
  const media = useEditor((s) => s.media);
  const selection = useEditor((s) => s.selection);
  const ui = useEditor((s) => s.trackUi);
  const playhead = useEditor((s) => s.playhead);
  const playing = useEditor((s) => s.playing);
  const [drag, setDrag] = useState<{ kind: "draw" | "move" | "corner"; x0: number; y0: number; x1: number; y1: number; box?: Box; corner?: number } | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  if (!p) return null;
  const ctx = context(p, media, selection.kind === "clip" ? selection.clipId : null, ui, playhead);
  if (!ctx) return null;
  const { host, isMain, m, track } = ctx;
  const W = p.settings.width, H = p.settings.height;
  const k = fw / W;
  const D = clipDuration(host);
  const inside = playhead >= host.start && playhead < host.start + D;
  const tool = ui.clipId === host.id ? ui.tool : "none";
  if (!inside && tool === "none") return null;
  const defFit = p.settings.background.mode;
  const tauAt = (t: number) => Math.min(Math.max(0, t - host.start), Math.max(0, D - 1e-6));
  const mat = (t: number) => hostMatrix(host, m, isMain, defFit, W, H, tauAt(t));
  const now = mat(playhead);
  const toCanvas = (x: number, y: number, mm = now) => {
    const sx = x * mm.sw, sy = y * mm.sh;
    return [(mm.m[0] * sx + mm.m[2] * sy + mm.m[4]) * k, (mm.m[1] * sx + mm.m[3] * sy + mm.m[5]) * k] as [number, number];
  };
  const toSource = (X: number, Y: number): [number, number] => {
    const iv = invert(now.m);
    const x = X / k, y = Y / k;
    return [(iv[0] * x + iv[2] * y + iv[4]) / now.sw, (iv[1] * x + iv[3] * y + iv[5]) / now.sh];
  };
  const local = (e: React.PointerEvent): [number, number] => {
    const r = ref.current!.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  };
  const fps = p.settings.fps || 30;
  const setUi = useEditor.getState().setTrackUi;

  // the path around the playhead
  const path: { X: number; Y: number; c: number }[] = [];
  if (track && inside && !playing) {
    for (let t = Math.max(host.start, playhead - 1.5); t < Math.min(host.start + D, playhead + 1.5); t += 1 / fps) {
      const e = trackAt(track, srcTime(host, tauAt(t)).src, false);
      if (!e) continue;
      const [X, Y] = toCanvas(e.x, e.y, mat(t));
      path.push({ X, Y, c: e.c });
    }
  }
  const eNow = track && inside ? trackAt(track, srcTime(host, tauAt(playhead)).src, false) : null;
  const fixHere = ui.fix && ui.clipId === host.id && Math.abs(ui.fix.t - seedTime(host)) < 0.5 / (m.fps || 30) ? ui.fix.box : null;
  let box: Box | null = fixHere ?? (eNow ? [eNow.x - eNow.w / 2, eNow.y - eNow.h / 2, eNow.w, eNow.h] : null);
  if (drag && (drag.kind === "move" || drag.kind === "corner") && drag.box) {
    const [a0, b0] = toSource(drag.x0, drag.y0), [a1, b1] = toSource(drag.x1, drag.y1);
    const [x, y, w, h] = drag.box;
    if (drag.kind === "move") box = [x + a1 - a0, y + b1 - b0, w, h];
    else {
      const ax = drag.corner! % 2 === 0 ? x + w : x, ay = drag.corner! < 2 ? y + h : y; // the fixed corner
      box = [Math.min(ax, a1), Math.min(ay, b1), Math.max(0.005, Math.abs(a1 - ax)), Math.max(0.005, Math.abs(b1 - ay))];
    }
  }
  const corners = box ? [toCanvas(box[0], box[1]), toCanvas(box[0] + box[2], box[1]), toCanvas(box[0], box[1] + box[3]), toCanvas(box[0] + box[2], box[1] + box[3])] : [];
  const poly = corners.length ? [corners[0], corners[1], corners[3], corners[2]].map((q) => q.join(",")).join(" ") : "";
  const weak = !!eNow && !fixHere && eNow.c < LOW_CONF;

  const endDrag = () => {
    if (!drag) return;
    if (drag.kind === "draw") {
      const [a0, b0] = toSource(Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1));
      const [a1, b1] = toSource(Math.max(drag.x0, drag.x1), Math.max(drag.y0, drag.y1));
      const x0 = Math.max(0, Math.min(a0, a1)), y0 = Math.max(0, Math.min(b0, b1)), x1 = Math.min(1, Math.max(a0, a1)), y1 = Math.min(1, Math.max(b0, b1));
      setDrag(null);
      if (x1 - x0 > 0.01 && y1 - y0 > 0.01) void trackBox(host, [x0, y0, x1 - x0, y1 - y0]);
      return;
    }
    if (box) setUi({ clipId: host.id, fix: { t: seedTime(host), box: box.map((v) => +v.toFixed(5)) as Box } });
    setDrag(null);
  };

  return (
    <div ref={ref} className={`trk-layer ${tool !== "none" ? "tool" : ""}`}
      onPointerDown={(e) => {
        if (tool === "draw") {
          const [x, y] = local(e);
          (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
          setDrag({ kind: "draw", x0: x, y0: y, x1: x, y1: y });
        } else if (tool === "click") {
          const [x, y] = local(e);
          const [sx, sy] = toSource(x, y);
          if (sx < 0 || sy < 0 || sx > 1 || sy > 1) return;
          setUi({ points: [...ui.points, { x: +sx.toFixed(5), y: +sy.toFixed(5), label: e.shiftKey || e.altKey ? 0 : 1 }] });
        }
      }}
      onPointerMove={(e) => drag && setDrag({ ...drag, x1: local(e)[0], y1: local(e)[1] })}
      onPointerUp={endDrag}>
      <svg width="100%" height="100%">
        {path.length > 1 && <polyline points={path.map((q) => `${q.X},${q.Y}`).join(" ")} className="trk-path" />}
        {path.filter((q) => q.c < LOW_CONF).map((q, i) => <circle key={i} cx={q.X} cy={q.Y} r={2.5} className={q.c <= 0 ? "trk-lost" : "trk-weak"} />)}
        {box && tool === "none" && (
          <>
            <polygon points={poly} className={`trk-box ${weak ? "weak" : ""} ${fixHere ? "fixed" : ""}`}
              onPointerDown={(e) => { e.stopPropagation(); (e.target as Element).setPointerCapture?.(e.pointerId); const [x, y] = local(e); setDrag({ kind: "move", x0: x, y0: y, x1: x, y1: y, box: box! }); }} />
            {corners.map(([X, Y], i) => (
              <rect key={i} x={X - 5} y={Y - 5} width={10} height={10} className="trk-handle"
                onPointerDown={(e) => { e.stopPropagation(); (e.target as Element).setPointerCapture?.(e.pointerId); const [x, y] = local(e); setDrag({ kind: "corner", corner: i, x0: x, y0: y, x1: x, y1: y, box: box! }); }} />
            ))}
          </>
        )}
        {drag?.kind === "draw" && (
          <rect x={Math.min(drag.x0, drag.x1)} y={Math.min(drag.y0, drag.y1)} width={Math.abs(drag.x1 - drag.x0)} height={Math.abs(drag.y1 - drag.y0)} className="trk-box drawing" />
        )}
        {tool === "face" && (ui.faces ?? []).map((f, i) => {
          const a = toCanvas(f.box[0], f.box[1]), b = toCanvas(f.box[0] + f.box[2], f.box[1] + f.box[3]);
          return (
            <g key={i} className="trk-face" onPointerDown={(e) => { e.stopPropagation(); void trackFace(host, i); }}>
              <rect x={Math.min(a[0], b[0])} y={Math.min(a[1], b[1])} width={Math.abs(b[0] - a[0])} height={Math.abs(b[1] - a[1])} />
              <text x={Math.min(a[0], b[0]) + 4} y={Math.min(a[1], b[1]) - 4}>{i + 1}</text>
            </g>
          );
        })}
        {tool === "click" && ui.points.map((q, i) => {
          const [X, Y] = toCanvas(q.x, q.y);
          return <circle key={i} cx={X} cy={Y} r={6} className={q.label ? "trk-pt on" : "trk-pt off"} />;
        })}
      </svg>
      {tool === "draw" && !drag && <span className="preview-label" style={{ top: 8, left: 8 }}>Drag a box around the object</span>}
      {weak && tool === "none" && <span className="preview-label" style={{ top: 8, left: 8 }}>{eNow!.c <= 0 ? "Lost here: drag the box onto it, then Re-track" : "Uncertain here"}</span>}
    </div>
  );
}
