// Small shared pieces for the states that every screen has: loading, error with retry, empty.

export function Spinner({ label }: { label?: string }) {
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

export function EmptyState({
  title,
  children,
}: {
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="state">
      <h2>{title}</h2>
      {children}
    </div>
  );
}
