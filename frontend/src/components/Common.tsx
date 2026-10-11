import { useEffect, type ReactNode } from "react";
import { useApp } from "../store/app";
import { Icon } from "./Icons";

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="btn ghost icon sm" onClick={onClose} title="Close (Esc)">
            <Icon name="x" />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function Toasts() {
  const toasts = useApp((s) => s.toasts);
  const dismiss = useApp((s) => s.dismiss);
  return (
    <div className="toasts">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          <div className="row" style={{ alignItems: "flex-start" }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <b>{t.title}</b>
              {t.detail && <p>{t.detail}</p>}
            </div>
            <button className="btn ghost icon sm" onClick={() => dismiss(t.id)} title="Dismiss">
              <Icon name="x" size={14} />
            </button>
          </div>
          {t.action && (
            <div className="row" style={{ marginTop: 8 }}>
              <button
                className="btn sm"
                onClick={() => {
                  t.action!.run();
                  dismiss(t.id);
                }}
              >
                {t.action.label}
              </button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export function ConnectionBanner() {
  const connected = useApp((s) => s.connected);
  if (connected) return null;
  return <div className="banner">Lost connection to the Ligxt backend. Retrying… (is start.ps1 still running?)</div>;
}

export function AspectShape({ aspect, size = 26 }: { aspect: string; size?: number }) {
  const [w, h] = aspect.split(":").map(Number);
  const scale = size / Math.max(w, h);
  return <div className="aspect-shape" style={{ width: w * scale, height: h * scale }} />;
}

export function ProgressBar({ value, big }: { value: number; big?: boolean }) {
  return (
    <div className={`progress ${big ? "big" : ""}`}>
      <div style={{ width: `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%` }} />
    </div>
  );
}
