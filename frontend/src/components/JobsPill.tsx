// Background work at a glance: imports/proxies, exports, captions, previews... with progress, ETA and cancel.
import { useState } from "react";
import { api } from "../api";
import { toastError, useApp } from "../store/app";
import { fmtEta } from "../util";
import { ProgressBar } from "./Common";
import { Icon } from "./Icons";

const KIND: Record<string, string> = {
  media: "Preparing", export: "Exporting", captions: "Captions", beats: "Beat detection", faces: "Finding faces",
  estimate: "Size estimate", cover: "Cover image", "template-preview": "Template preview", "template-generate": "Template",
};

export default function JobsPill() {
  const jobs = useApp((s) => s.jobs);
  const [open, setOpen] = useState(false);
  const active = Object.values(jobs).filter((j) => j.status === "running" || j.status === "queued").sort((a, b) => a.created - b.created);
  if (!active.length) return null;
  const main = active.find((j) => j.status === "running") ?? active[0];
  const avg = active.reduce((n, j) => n + j.progress, 0) / active.length;
  return (
    <span style={{ position: "relative" }}>
      <button className="jobs-pill" onClick={() => setOpen(!open)} title="Background work: click for details">
        <Icon name="loader" size={13} className="spin" />
        {active.length === 1 ? `${KIND[main.kind] ?? "Working"} ${Math.round(main.progress * 100)}%` : `${active.length} tasks · ${Math.round(avg * 100)}%`}
      </button>
      {open && (
        <div className="jobs-pop">
          {active.map((j) => (
            <div key={j.id} className="job-row">
              <div className="row" style={{ gap: 6 }}>
                <b className="ex-name" title={j.label}>{j.label}</b>
                <span className="spacer" />
                <span className="mono faint">{j.status === "queued" ? "waiting" : `${Math.round(j.progress * 100)}%`}</span>
                <button className="btn ghost icon xs" title="Cancel" onClick={() => api.cancelJob(j.id).catch((e) => toastError("Cancel failed", e))}><Icon name="x" size={11} /></button>
              </div>
              <ProgressBar value={j.progress} />
              <div className="faint" style={{ fontSize: 11 }}>{j.message || (j.status === "queued" ? "Waiting for the previous task" : "Working")}{j.status === "running" && j.eta ? ` · ${fmtEta(j.eta)}` : ""}</div>
            </div>
          ))}
          <div className="faint" style={{ fontSize: 11 }}>Everything keeps running if you close this or keep editing.</div>
        </div>
      )}
    </span>
  );
}
