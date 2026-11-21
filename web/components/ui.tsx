// Small shared pieces for the states that every screen has: loading, error with retry, and the
// screen of a sign-in status that has no content yet.

import type { Tone } from "@/lib/jobs";
import type { Status } from "./AuthProvider";

const TONE_CLASS: Record<Tone, string> = {
  accent: "badge badge-accent",
  warning: "badge badge-warning",
  danger: "badge badge-danger",
  neutral: "badge",
};

/** A small status label. The tone gives the color. */
export function ToneBadge({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return <span className={TONE_CLASS[tone]}>{children}</span>;
}

function Spinner({ label }: { label?: string }) {
  return (
    <span role="status" style={{ display: "inline-flex" }}>
      <span className="spinner" aria-hidden="true" />
      <span className="sr-only">{label ?? "Loading"}</span>
    </span>
  );
}

export function LoadingState({ label = "Loading" }: { label?: string }) {
  return (
    <div className="state">
      <Spinner label={label} />
      <p aria-hidden="true">{label}</p>
    </div>
  );
}

export function ErrorState({
  title = "Something went wrong",
  message,
  onRetry,
}: {
  title?: string;
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="state" role="alert">
      <h2>{title}</h2>
      <p>{message}</p>
      {onRetry ? (
        <button type="button" className="btn" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

/**
 * The screen for a sign-in status without content (connecting, not set up, API not
 * reachable). For any other status it shows `children`.
 */
export function AuthStatusScreen({
  status,
  onRetry,
  loadingLabel,
  children,
}: {
  status: Status;
  onRetry: () => void;
  loadingLabel: string;
  children: React.ReactNode;
}) {
  switch (status.kind) {
    case "loading":
      return <LoadingState label={loadingLabel} />;
    case "unconfigured":
      return (
        <ErrorState
          title="The app is not set up"
          message="NEXT_PUBLIC_SAMA_API_URL is not set. Set it to the address of the sama API and build the app again."
        />
      );
    case "unreachable":
      return <ErrorState title="Cannot reach the sama API" message={status.message} onRetry={onRetry} />;
    default:
      return children;
  }
}
