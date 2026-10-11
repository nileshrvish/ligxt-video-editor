// On-canvas transform handles for the selected clip: drag inside to move, a corner to scale, the
// knob above the top edge to rotate (Shift snaps to 15°). Edits go through the same setProp as the
// Properties panel, so with keyframes on they land as keyframes at the playhead.
import * as E from "../motion/edit";
import { apply, rectMatrix, roundedRect, transformAt } from "../motion/motion";
import { useEditor, useSelectedClip } from "../store/editor";
import * as ops from "../timeline/ops";
import type { Clip } from "../types";
import { clipEnd } from "../util";

export default function TransformHandles({ fw, fh }: { fw: number; fh: number }) {
  const sel = useSelectedClip();
  const playhead = useEditor((s) => s.playhead);
  const playing = useEditor((s) => s.playing);
  const settings = useEditor((s) => s.project!.settings);
  const m = useEditor((s) => (sel?.clip.mediaId ? s.media[sel.clip.mediaId] : undefined));
  if (!sel || playing || !m || m.type === "audio" || sel.clip.adjustment) return null;
  if (sel.trackType !== "video" && sel.trackType !== "overlay") return null;
  const clip = sel.clip;
  if (playhead < clip.start || playhead >= clipEnd(clip)) return null;

  const { width: W, height: H } = settings;
  const k = fw / W;
  const tau = playhead - clip.start;
  const isMain = sel.trackType === "video";
  const fill = (clip.fit ?? (isMain ? settings.background.mode : "fit")) === "fill";
  const tv = transformAt(clip, tau);
  const rect = roundedRect(m.width || W, m.height || H, clip.transform.crop, fill, W, H);
  const mat = rectMatrix(rect, W, H, tv);
  const [x0, y0, cw, ch] = rect;
  const corners = [[x0, y0], [x0 + cw, y0], [x0 + cw, y0 + ch], [x0, y0 + ch]].map(([x, y]) => apply(mat, x, y).map((v) => v * k));
  const C = [(W / 2 + tv.x) * k, (H / 2 + tv.y) * k];
  const topMid = [(corners[0][0] + corners[1][0]) / 2, (corners[0][1] + corners[1][1]) / 2];
  const away = Math.hypot(topMid[0] - C[0], topMid[1] - C[1]) || 1;
  const knob = [topMid[0] + ((topMid[0] - C[0]) / away) * 22, topMid[1] + ((topMid[1] - C[1]) / away) * 22];

  const update = (fn: (c: Clip) => void) =>
    useEditor.getState().commit((p) => {
      const hit = ops.findClip(p, clip.id);
      if (hit) fn(hit.clip);
    }, false);

  /** Generic drag: `onMove` gets the pointer in project pixels. */
  const drag = (onMove: (px: number, py: number, ev: PointerEvent) => void) => (e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const frame = (e.currentTarget as Element).closest("svg")!.getBoundingClientRect();
    const st = useEditor.getState();
    st.beginGesture();
    const move = (ev: PointerEvent) => onMove((ev.clientX - frame.left) / k, (ev.clientY - frame.top) / k, ev);
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      useEditor.getState().endGesture();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const cx = W / 2 + tv.x, cy = H / 2 + tv.y;
  const x0v = E.propValue(clip, "x", tau), y0v = E.propValue(clip, "y", tau);
  const s0 = E.propValue(clip, "scale", tau), r0 = E.propValue(clip, "rotation", tau);

  const onMoveStart = (e: React.PointerEvent) => {
    const frame = (e.currentTarget as Element).closest("svg")!.getBoundingClientRect();
    const px0 = (e.clientX - frame.left) / k, py0 = (e.clientY - frame.top) / k;
    drag((px, py, ev) => {
      let nx = x0v + px - px0, ny = y0v + py - py0;
      if (!ev.altKey) { // snap to the centre lines
        if (Math.abs(nx) < 12) nx = 0;
        if (Math.abs(ny) < 12) ny = 0;
      }
      update((c) => { E.setProp(c, "x", tau, Math.round(nx)); E.setProp(c, "y", tau, Math.round(ny)); });
    })(e);
  };
  const onScaleStart = (e: React.PointerEvent) => {
    const frame = (e.currentTarget as Element).closest("svg")!.getBoundingClientRect();
    const d0 = Math.hypot((e.clientX - frame.left) / k - cx, (e.clientY - frame.top) / k - cy) || 1;
    drag((px, py) => {
      const d = Math.hypot(px - cx, py - cy);
      update((c) => E.setProp(c, "scale", tau, Math.max(0.05, Math.min(8, +(s0 * (d / d0)).toFixed(4)))));
    })(e);
  };
  const onRotateStart = (e: React.PointerEvent) => {
    const frame = (e.currentTarget as Element).closest("svg")!.getBoundingClientRect();
    const a0 = Math.atan2((e.clientY - frame.top) / k - cy, (e.clientX - frame.left) / k - cx);
    drag((px, py, ev) => {
      let r = r0 + ((Math.atan2(py - cy, px - cx) - a0) * 180) / Math.PI;
      r = ((r + 540) % 360) - 180;
      if (ev.shiftKey) r = Math.round(r / 15) * 15;
      update((c) => E.setProp(c, "rotation", tau, +r.toFixed(2)));
    })(e);
  };

  return (
    <svg className="pv-handles" width={fw} height={fh} style={{ overflow: "visible" }}>
      <polygon points={corners.map((p) => p.join(",")).join(" ")} className="pv-poly" onPointerDown={onMoveStart}>
        <title>Drag to move (Alt: no centre snapping)</title>
      </polygon>
      <line x1={topMid[0]} y1={topMid[1]} x2={knob[0]} y2={knob[1]} className="pv-stem" />
      {corners.map((p, i) => (
        <rect key={i} x={p[0] - 5} y={p[1] - 5} width={10} height={10} className="pv-corner-r" onPointerDown={onScaleStart}
          style={{ cursor: i % 2 ? "nesw-resize" : "nwse-resize" }} />
      ))}
      <circle cx={knob[0]} cy={knob[1]} r={6} className="pv-knob" onPointerDown={onRotateStart}>
        <title>Drag to rotate (Shift: 15° steps)</title>
      </circle>
    </svg>
  );
}
