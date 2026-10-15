// Settings (#/settings): the AI tools (separate ai-venv environment), processing options, and the
// models on disk: license, size, download/delete. Also the cutout cache.
import { useEffect, useState } from "react";
import { api, type AiModel, type AiStatus } from "../api";
import { resetCutoutAssets } from "../cutout/assets";
import { navigate } from "../router";
import { toastError, useApp } from "../store/app";
import { ProgressBar } from "./Common";
import { Icon, Logo } from "./Icons";

const fmt = (n?: number | null) => {
  if (!n) return "0 MB";
  return n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)} GB` : `${Math.round(n / 1024 ** 2)} MB`;
};

export default function Settings() {
  const [st, setSt] = useState<AiStatus | null>(null);
  const [checking, setChecking] = useState(false);
  const jobs = useApp((s) => s.jobs);
  const toast = useApp((s) => s.toast);
  const load = (refresh = false) => {
    if (refresh) setChecking(true);
    return api.aiStatus(refresh).then(setSt, (e) => toastError("Could not load the AI status", e)).finally(() => setChecking(false));
  };
  useEffect(() => void load(), []);
  // refresh when a model download finishes
  const running = Object.values(jobs).filter((j) => j.kind === "model-download" && (j.status === "running" || j.status === "queued"));
  const runKey = running.map((j) => j.id).join(",");
  useEffect(() => {
    if (!runKey) void load();
  }, [runKey]);

  const put = (body: Partial<AiStatus["settings"]>) =>
    api.aiSettings(body).then((s) => setSt((x) => (x ? { ...x, settings: s } : x)), (e) => toastError("Could not save", e));
  const download = (m: AiModel) => api.downloadModel(m.id).then(() => load(), (e) => toastError("Download failed", e));
  const remove = (m: AiModel) => {
    if (!window.confirm(`Delete ${m.name} (${fmt(m.diskBytes)})? It downloads again when a feature needs it.`)) return;
    api.deleteModel(m.id).then(() => { toast({ kind: "success", title: `Deleted ${m.name}` }); return load(); }, (e) => toastError("Delete failed", e));
  };
  const env = st?.env;

  return (
    <div className="home settings">
      <div className="home-inner">
        <div className="brand">
          <Logo size={34} />
          <div>
            <h1>Settings</h1>
            <div className="tag">AI tools and models</div>
          </div>
          <span className="spacer" />
          <button className="btn" onClick={() => navigate("/")}><Icon name="home" size={14} /> Home</button>
        </div>

        <div className="section-title">AI tools <span className="spacer" />
          <button className="btn ghost sm" onClick={() => load(true)} disabled={checking}><Icon name="refresh" size={14} /> {checking ? "Checking…" : "Check again"}</button>
        </div>
        {!st ? <div className="faint">Loading…</div> : (
          <div className="set-card">
            {env?.installed && !env.error ? (
              <>
                <div className="set-row"><b>Environment</b><span>ai-venv · Python {env.python} · PyTorch {env.torch ?? "missing"}</span></div>
                <div className="set-row"><b>Graphics card</b>
                  <span>{env.cuda ? `${env.device} · ${fmt(env.vramTotal)} VRAM (${fmt(env.vramFree)} free now) · CUDA ${env.cudaVersion}` : "No compatible GPU found: AI runs on the CPU (slow)."}</span>
                </div>
              </>
            ) : (
              <div className="set-warn">
                <Icon name="alert" size={14} /> {env?.error ?? "The AI tools are not installed."}
                <div className="faint" style={{ fontSize: 12, marginTop: 4 }}>
                  Everything else in Ligxt works without them. To install: close Ligxt, run <code>setup.ps1</code> (it builds the separate <code>ai-venv</code>), and start Ligxt again.
                </div>
              </div>
            )}
            <div className="set-row">
              <b>Run AI on</b>
              <div className="seg">
                <button className={st.settings.device === "auto" ? "on" : ""} onClick={() => put({ device: "auto" })}>Graphics card (when available)</button>
                <button className={st.settings.device === "cpu" ? "on" : ""} onClick={() => put({ device: "cpu" })}>CPU only</button>
              </div>
            </div>
            <div className="set-row" title="AI work stays under this much graphics memory (batch sizes are tuned to it). Lower it if other apps need the GPU.">
              <b>GPU memory limit</b>
              <span className="row" style={{ gap: 8 }}>
                <input type="range" min={1.5} max={5.5} step={0.25} value={st.settings.budgetGB}
                  onChange={(e) => setSt({ ...st, settings: { ...st.settings, budgetGB: +e.target.value } })}
                  onPointerUp={(e) => put({ budgetGB: +(e.target as HTMLInputElement).value })} />
                <span className="mono">{st.settings.budgetGB.toFixed(2)} GB</span>
              </span>
            </div>
            <div className="set-row">
              <b>Cutout model</b>
              <select className="input" style={{ maxWidth: 360 }} value={st.settings.personModel} onChange={(e) => put({ personModel: e.target.value })}>
                {st.models.filter((m) => m.task === "person").map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
              </select>
            </div>
          </div>
        )}

        <div className="section-title">Vocal separator (song lyrics)</div>
        {st && (() => {
          const roformer = st.models.find((m) => m.id === "mel-roformer-vocals");
          const dereverb = st.models.find((m) => m.id === "uvr-deecho-dereverb");
          return (
            <div className="set-card" data-testid="set-separator">
              <div className="set-row">
                <b>Separator</b>
                <select className="input" style={{ maxWidth: 360 }} value={st.settings.separator} onChange={(e) => put({ separator: e.target.value as "mel-roformer" | "off" })}>
                  <option value="mel-roformer">Mel-Band RoFormer vocals (MIT; recommended)</option>
                  <option value="off">Off: use the full mix (less reliable on songs)</option>
                </select>
              </div>
              <div className="faint" style={{ fontSize: 12 }}>
                Song lyrics first separate the singing from the music, with python-audio-separator (MIT) running {roformer?.name ?? "Mel-Band RoFormer"} (license {roformer?.license ?? "MIT"}).
                {roformer && !roformer.installed ? ` Downloads ${fmt(roformer.downloadBytes ?? 0)} on first use.` : ""} Fast and Best separators are not offered: their models have no stated license or research-only weights.
              </div>
              <label className="row fl-check" title="Runs after the separator, before Whisper">
                <input type="checkbox" checked={st.settings.dereverb} disabled={st.settings.separator === "off"} onChange={(e) => put({ dereverb: e.target.checked })} />
                Remove echo and reverb from the vocals (UVR DeEcho-DeReverb)
              </label>
              <div className="faint" style={{ fontSize: 12 }}>
                License: <b>{dereverb?.license ?? "Not stated"}</b> by its author; enabled at your request. Off by default: on the test song it found 0-1 more words with higher confidence, but whether
                they were more correct couldn't be checked.{dereverb && !dereverb.installed ? ` Downloads ${fmt(dereverb.downloadBytes ?? 0)} on first use.` : ""}
              </div>
              <div className="set-row">
                <span>Separated vocals are kept per song section so repeat runs are instant: <b>{fmt(st.stemsBytes ?? 0)}</b>.</span>
                <button className="btn ghost sm danger" disabled={!st.stemsBytes}
                  onClick={() => api.clearStemsCache().then((r) => { toast({ kind: "success", title: `Freed ${fmt(r.freed)}` }); load(); }, (e) => toastError("Could not clear", e))}>
                  <Icon name="trash" size={12} /> Clear
                </button>
              </div>
            </div>
          );
        })()}

        <div className="section-title">Models <span className="spacer" /><span className="faint" style={{ fontSize: 12 }}>{st ? `${fmt(st.totalBytes)} on disk` : ""}</span></div>
        {st && (
          <div className="set-card">
            {[...st.models, ...st.other].map((m) => {
              const job = m.jobId ? jobs[m.jobId] : running.find((j) => j.ref?.model === m.id);
              const busy = job && (job.status === "running" || job.status === "queued");
              const managed = m.downloadBytes !== undefined;
              return (
                <div key={m.id} className="set-model">
                  <div style={{ minWidth: 0 }}>
                    <b>{m.name}</b>
                    <div className="faint" style={{ fontSize: 12 }}>{m.about}</div>
                    <div className="faint" style={{ fontSize: 11 }}>
                      License: <a href={m.source} target="_blank" rel="noreferrer">{m.license}</a>
                      {managed ? ` · download ${fmt(m.downloadBytes)}` : ""} · {m.diskBytes ? `${fmt(m.diskBytes)} on disk` : "not downloaded"}
                    </div>
                    {busy && <><ProgressBar value={job!.progress} /><div className="faint" style={{ fontSize: 11 }}>{job!.message}</div></>}
                  </div>
                  <div className="row" style={{ gap: 6 }}>
                    {managed && !m.installed && !busy && <button className="btn sm" onClick={() => download(m)}><Icon name="export" size={12} /> Download</button>}
                    {busy && <button className="btn ghost sm" onClick={() => api.cancelJob(job!.id)}>Cancel</button>}
                    {managed && m.installed && <span className="fl-badge" title="Downloaded and checksum-verified">✓ installed</span>}
                    {(m.deletable ?? managed) && m.diskBytes > 0 && !busy && <button className="btn ghost sm danger" onClick={() => remove(m)}><Icon name="trash" size={12} /> Delete</button>}
                    {m.deletable === false && <span className="faint" style={{ fontSize: 11 }}>bundled</span>}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <div className="section-title">Cutout cache</div>
        {st && (
          <div className="set-card">
            <div className="set-row">
              <span>Analysed subjects are kept so previews and exports are instant: <b>{fmt(st.cacheBytes)}</b>.</span>
              <button className="btn ghost sm danger" disabled={!st.cacheBytes}
                onClick={() => api.clearCutoutCache().then((r) => { resetCutoutAssets(); toast({ kind: "success", title: `Freed ${fmt(r.freed)}` }); load(); }, (e) => toastError("Could not clear", e))}>
                <Icon name="trash" size={12} /> Clear
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
