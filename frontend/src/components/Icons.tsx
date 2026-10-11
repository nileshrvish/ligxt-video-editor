// Small inline icon set (stroke icons, 24x24 viewBox).
const paths: Record<string, string> = {
  plus: "M12 5v14M5 12h14",
  film: "M4 4h16v16H4zM8 4v16M16 4v16M4 8h4M4 12h4M4 16h4M16 8h4M16 12h4M16 16h4",
  image: "M4 5h16v14H4zM4 15l4-4 4 4 3-3 5 5M15 9.5a1 1 0 1 0 0-.01",
  music: "M9 18V6l11-2v12M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM20 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
  upload: "M12 16V4M7 9l5-5 5 5M4 16v4h16v-4",
  play: "M7 4l13 8-13 8z",
  pause: "M7 4h4v16H7zM14 4h4v16h-4z",
  stepBack: "M18 5L9 12l9 7zM6 5v14",
  stepFwd: "M6 5l9 7-9 7zM18 5v14",
  export: "M12 3v12M7 10l5 5 5-5M5 19h14",
  save: "M5 4h11l4 4v12H5zM8 4v5h7V4M8 20v-6h8v6",
  undo: "M9 14L4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3",
  redo: "M15 14l5-5-5-5M20 9H9a5 5 0 0 0 0 10h3",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
  x: "M6 6l12 12M18 6L6 18",
  home: "M4 11l8-7 8 7v9h-5v-6H9v6H4z",
  folder: "M3 6h6l2 2h10v11H3z",
  layout: "M4 4h16v16H4zM4 9h16M10 9v11",
  zoomIn: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-5-5M11 8v6M8 11h6",
  zoomOut: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-5-5M8 11h6",
  keyboard: "M3 7h18v11H3zM7 11h.01M11 11h.01M15 11h.01M7 15h10",
  alert: "M12 3l10 18H2zM12 10v5M12 18h.01",
  check: "M5 12l5 5 9-10",
  copy: "M8 8h12v12H8zM4 16V4h12",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  loader: "M12 3a9 9 0 1 0 9 9",
  volume: "M4 9h4l5-4v14l-5-4H4zM16 9a4 4 0 0 1 0 6",
  mute: "M4 9h4l5-4v14l-5-4H4zM17 9l4 6M21 9l-4 6",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  fullscreen: "M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5",
  sparkles: "M12 3l2 5 5 2-5 2-2 5-2-5-5-2 5-2zM19 15l1 2 2 1-2 1-1 2-1-2-2-1 2-1z",
  text: "M5 6V4h14v2M12 4v16M9 20h6",
};

export function Icon({ name, size = 16, className, style }: { name: keyof typeof paths | string; size?: number; className?: string; style?: React.CSSProperties }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.9}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={style}
      aria-hidden
    >
      <path d={paths[name] ?? paths.alert} />
    </svg>
  );
}

export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden>
      <rect width="32" height="32" rx="8" fill="#1b1b24" stroke="#3a3a4a" />
      <path d="M11 8v16h11" fill="none" stroke="#8b7bff" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="22" cy="10" r="3" fill="#ff5c8a" />
    </svg>
  );
}
