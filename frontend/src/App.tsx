import { Component, useEffect, useState, type ReactNode } from "react";
import { ConnectionBanner, Toasts } from "./components/Common";
import Editor from "./components/Editor";
import Home from "./components/Home";
import Settings from "./components/Settings";
import { connectEvents } from "./store/app";

/** Hash routes: "#/" home, "#/p/<projectId>" editor, "#/settings" settings. */
function useRoute(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const on = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="home">
        <div className="home-inner">
          <h2 className="error-text">Something went wrong in the editor</h2>
          <pre className="log">{this.state.error.stack ?? this.state.error.message}</pre>
          <p className="muted">Your last autosave is safe. Reload the page to continue.</p>
          <button className="btn primary" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </div>
    );
  }
}

export default function App() {
  const route = useRoute();
  useEffect(() => connectEvents(), []);
  const m = route.match(/^#\/p\/([a-f0-9]{12})/);
  return (
    <ErrorBoundary>
      <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
        <ConnectionBanner />
        <div style={{ flex: 1, minHeight: 0 }}>{m ? <Editor key={m[1]} projectId={m[1]} /> : route.startsWith("#/settings") ? <Settings /> : <Home />}</div>
      </div>
      <Toasts />
    </ErrorBoundary>
  );
}
