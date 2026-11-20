import type { ToolCall } from "@/lib/chat";
import styles from "./ToolGroup.module.css";

const MAX_PREVIEW = 2_000;

/** The tool calls of one run, collapsed by default. */
export function ToolGroup({ calls }: { calls: ToolCall[] }) {
  if (calls.length === 0) return null;
  const running = calls.some((c) => c.status === "running");
  const failed = calls.filter((c) => c.status === "error").length;
  const names = [...new Set(calls.map((c) => c.name))];
  return (
    <details className={styles.group}>
      <summary className={styles.summary}>
        {running ? <span className="spinner" aria-hidden="true" /> : <span className={styles.dot} aria-hidden="true" />}
        <span className={styles.summaryText}>
          {running ? "Using tools" : "Used tools"}: {names.join(", ")}
        </span>
        <span className="badge">{calls.length}</span>
        {failed > 0 ? <span className="badge badge-warning">{failed} failed</span> : null}
      </summary>
      <ul className={styles.list}>
        {calls.map((call) => (
          <li key={call.callId}>
            <details className={styles.call}>
              <summary className={styles.callSummary}>
                <span className="mono">{call.name}</span>
                <span className={`${styles.status} ${call.status === "error" ? styles.error : ""}`}>
                  {call.status === "running" ? "running" : call.status === "error" ? "failed" : "done"}
                </span>
              </summary>
              {Object.keys(call.arguments).length > 0 ? (
                <pre className={styles.code}>{JSON.stringify(call.arguments, null, 2)}</pre>
              ) : null}
              {call.preview ? (
                <pre className={styles.code}>
                  {call.preview.length > MAX_PREVIEW
                    ? `${call.preview.slice(0, MAX_PREVIEW)}…`
                    : call.preview}
                </pre>
              ) : null}
            </details>
          </li>
        ))}
      </ul>
    </details>
  );
}
