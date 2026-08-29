import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
}

/**
 * audit M-20: React 19 unmounts the whole tree on an uncaught render error, so
 * one bad API value used to give a bare white screen recoverable only by a
 * reload. This boundary catches it and shows an actionable fallback with a
 * reload affordance, so a render crash is distinguishable from a slow load.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Uncaught render error:", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div role="alert" style={{
          maxWidth: 520, margin: "12vh auto", padding: "2rem",
          fontFamily: "system-ui, sans-serif", lineHeight: 1.5,
        }}>
          <h1 style={{ fontSize: "1.25rem", marginBottom: "0.5rem" }}>Something went wrong</h1>
          <p style={{ color: "#666", marginBottom: "1rem" }}>
            The page hit an unexpected error and can't render right now. Reloading usually fixes it.
          </p>
          <pre style={{
            background: "#f5f5f5", color: "#b00020", padding: "0.75rem",
            borderRadius: 6, overflow: "auto", fontSize: "0.8rem", marginBottom: "1rem",
          }}>{this.state.error.message}</pre>
          <button
            onClick={() => window.location.reload()}
            style={{
              padding: "0.5rem 1rem", borderRadius: 6, border: "none",
              background: "#2563eb", color: "white", cursor: "pointer", fontSize: "0.9rem",
            }}
          >
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
