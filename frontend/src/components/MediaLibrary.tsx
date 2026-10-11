import { useRef, useState } from "react";
import { useApp } from "../store/app";
import { useEditor } from "../store/editor";
import type { MediaMeta } from "../types";
import { fmtDuration } from "../util";
import { ProgressBar } from "./Common";
import { Icon } from "./Icons";

export const MEDIA_DND = "application/x-ligxt-media";

export default function MediaLibrary() {
  const ids = useEditor((s) => s.project!.media);
  const media = useEditor((s) => s.media);
  const uploads = useEditor((s) => s.uploads);
  const [over, setOver] = useState(false);
  const [filter, setFilter] = useState<"all" | "video" | "image" | "audio">("all");
  const fileRef = useRef<HTMLInputElement>(null);

  const items = ids.map((id) => media[id]).filter((m): m is MediaMeta => !!m && (filter === "all" || m.type === filter));

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setOver(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length) useEditor.getState().importFiles(files);
  };

  return (
    <div className="panel">
      <div className="panel-head">
        Media
        <span className="faint" style={{ textTransform: "none", fontWeight: 400 }}>{ids.length}</span>
        <span className="spacer" />
        <button className="btn sm primary" onClick={() => fileRef.current?.click()} title="Import video, images or audio">
          <Icon name="upload" size={13} /> Import
        </button>
        <input
          ref={fileRef}
          type="file"
          multiple
          accept="video/*,image/*,audio/*,.mkv,.mts,.m2ts"
          hidden
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            e.target.value = "";
            if (files.length) useEditor.getState().importFiles(files);
          }}
        />
      </div>
      {ids.length > 0 && (
        <div style={{ padding: "6px 8px 0" }}>
          <div className="seg" style={{ width: "100%" }}>
            {(["all", "video", "image", "audio"] as const).map((f) => (
              <button key={f} className={filter === f ? "on" : ""} style={{ flex: 1 }} onClick={() => setFilter(f)}>
                {f[0].toUpperCase() + f.slice(1)}
              </button>
            ))}
          </div>
        </div>
      )}
      <div
        className="panel-body"
        onDragOver={(e) => {
          if (e.dataTransfer.types.includes("Files")) {
            e.preventDefault();
            setOver(true);
          }
        }}
        onDragLeave={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node)) setOver(false);
        }}
        onDrop={onDrop}
      >
        <div className={`dropzone ${over ? "over" : ""}`}>
          {uploads.map((u) => (
            <div key={u.key} className="upload-row">
              <div className="row">
                <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>Uploading {u.name}</span>
                <span className="faint mono">{Math.round(u.progress * 100)}%</span>
                <button className="btn ghost icon sm" title="Cancel upload" onClick={u.abort}>
                  <Icon name="x" size={12} />
                </button>
              </div>
              <ProgressBar value={u.progress} />
            </div>
          ))}
          {ids.length === 0 && uploads.length === 0 ? (
            <div className="lib-empty">
              <Icon name="upload" size={28} />
              <div>Drag videos, photos or music here</div>
              <button className="btn" onClick={() => fileRef.current?.click()}>Browse files</button>
              <div style={{ fontSize: 11 }}>MP4, MOV, MKV, WebM · JPG, PNG, WebP · MP3, WAV, M4A</div>
            </div>
          ) : (
            <div className="media-grid">
              {items.map((m) => (
                <MediaItem key={m.id} m={m} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function MediaItem({ m }: { m: MediaMeta }) {
  const selected = useEditor((s) => s.selection.kind === "media" && s.selection.id === m.id);
  const job = useApp((s) =>
    Object.values(s.jobs).find((j) => j.ref.mediaId === m.id && (j.status === "running" || j.status === "queued")),
  );
  const icon = m.type === "video" ? "film" : m.type === "image" ? "image" : "music";
  const dims = m.width && m.height ? `${m.width}×${m.height}` : "";
  const sub =
    m.type === "image" ? dims :
    m.type === "audio" ? `${fmtDuration(m.duration)} · ${m.sampleRate ? `${m.sampleRate / 1000} kHz` : ""}` :
    `${dims} · ${m.fps ?? "?"} fps`;

  return (
    <div
      className={`media-item ${selected ? "sel" : ""}`}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData(MEDIA_DND, m.id);
        e.dataTransfer.effectAllowed = "copy";
      }}
      onClick={() => useEditor.getState().select({ kind: "media", id: m.id })}
      onDoubleClick={() => useEditor.getState().addToTimeline(m.id)}
      title={`${m.name}\nDouble-click or drag to the timeline to add`}
    >
      <div className="mthumb" style={m.thumbnail ? { backgroundImage: `url(${m.thumbnail})` } : undefined}>
        {!m.thumbnail && <Icon name={icon} size={24} />}
        <span className="tbadge">{m.type}</span>
        {m.type !== "image" && <span className="badge">{fmtDuration(m.duration)}</span>}
        <button
          className="btn icon sm add-btn"
          title="Add to timeline"
          onClick={(e) => {
            e.stopPropagation();
            useEditor.getState().addToTimeline(m.id);
          }}
        >
          <Icon name="plus" size={14} />
        </button>
      </div>
      <div className="minfo">
        <b>{m.name}</b>
        <span>{sub}</span>
        {job ? (
          <div style={{ marginTop: 4 }}>
            <ProgressBar value={job.progress} />
            <div className="status-line">
              {job.status === "queued" ? "Queued" : job.message === "Proxy" ? "Building proxy" : job.message || "Processing"}{" "}
              {job.status === "running" && `${Math.round(job.progress * 100)}%`}
            </div>
          </div>
        ) : m.status === "error" ? (
          <div className="status-line error-text" title={m.error ?? ""}>
            <Icon name="alert" size={12} /> Processing failed
          </div>
        ) : m.status === "warning" ? (
          <div className="status-line" style={{ color: "var(--warn)" }} title={m.error ?? ""}>
            <Icon name="alert" size={12} /> Ready with warnings
          </div>
        ) : null}
      </div>
    </div>
  );
}
