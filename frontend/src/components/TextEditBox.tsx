// Typing on the canvas: a text box over the selected text, styled like it (font, size, colour,
// alignment, rotation). The rendered text is hidden while it is open; every keystroke updates the
// clip (one undo step for the whole edit). Esc or a click outside closes it; Ctrl+Enter too.
import { useEffect, useLayoutEffect, useRef } from "react";
import { transformAt } from "../motion/motion";
import { useEditor } from "../store/editor";
import { normalize, TEXT_FONTS } from "../text/anim";
import { textAsset } from "../text/cache";
import * as TE from "../text/edit";
import * as ops from "../timeline/ops";

const loaded = new Set<string>();
function ensureFont(id: string) {
  if (loaded.has(id)) return;
  const f = TEXT_FONTS.find((x) => x.id === id);
  if (!f) return;
  loaded.add(id);
  const face = new FontFace(`ligxt-${id}`, `url(/files/fonts/${f.file})`);
  face.load().then((ff) => document.fonts.add(ff)).catch(() => loaded.delete(id));
}

export default function TextEditBox({ fw }: { fw: number }) {
  const id = useEditor((s) => s.editingText);
  const project = useEditor((s) => s.project!);
  const playhead = useEditor((s) => s.playhead);
  const ref = useRef<HTMLTextAreaElement>(null);
  const hit = id ? ops.findClip(project, id) : null;
  const clip = hit?.clip;
  const spec = clip ? normalize(clip.text) : null;

  useEffect(() => {
    if (!id) return;
    const st = useEditor.getState();
    st.beginGesture();
    st.setPlaying(false);
    return () => useEditor.getState().endGesture();
  }, [id]);
  useEffect(() => {
    if (spec) ensureFont(spec.font);
  }, [spec?.font]);
  useLayoutEffect(() => {
    const ta = ref.current;
    if (!ta) return;
    ta.focus();
    ta.style.height = "auto";
    ta.style.height = `${ta.scrollHeight}px`;
  });
  if (!clip || !spec || !hit || hit.track.type !== "text") return null;

  const W = project.settings.width, H = project.settings.height;
  const k = fw / W;
  const tau = Math.max(0, Math.min(playhead - clip.start, clip.outPoint - clip.inPoint));
  const tv = transformAt(clip, tau);
  const lay = textAsset(clip.text).layout;
  const bw = Math.max(lay?.w ?? spec.maxWidth ?? 600, 240);
  const close = () => useEditor.getState().setEditingText(null);
  const size = spec.size * tv.scale * k;
  return (
    <textarea
      ref={ref}
      className="tx-onCanvas"
      value={spec.content}
      spellCheck={false}
      onChange={(e) => {
        const v = e.target.value;
        useEditor.getState().commit((p) => {
          const h = ops.findClip(p, clip.id);
          if (h) TE.setContent(h.clip, v);
        }, false);
      }}
      onBlur={close}
      onKeyDown={(e) => {
        e.stopPropagation(); // keep editor shortcuts (space, S, Delete...) out of the way while typing
        if (e.key === "Escape" || (e.key === "Enter" && (e.ctrlKey || e.metaKey))) close();
      }}
      style={{
        left: (W / 2 + tv.x) * k, top: (H / 2 + tv.y) * k, width: bw * tv.scale * k,
        transform: `translate(-50%, -50%) rotate(${tv.rotation}deg)`,
        fontFamily: `ligxt-${spec.font}, sans-serif`, fontSize: size, lineHeight: spec.lineHeight,
        letterSpacing: spec.letterSpacing * tv.scale * k, textAlign: spec.align, color: spec.color,
        textTransform: spec.uppercase ? "uppercase" : "none",
        WebkitTextStroke: spec.outline.width ? `${Math.max(0.5, spec.outline.width * tv.scale * k * 0.6)}px ${spec.outline.color}` : undefined,
      }}
    />
  );
}
