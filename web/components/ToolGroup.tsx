import type { ToolCall } from "@/lib/chat";
import { groupSubagents, placeSubagents, type SubagentEvent } from "@/lib/subagentProgress";
import { SubagentGroup } from "./SubagentGroup";
import styles from "./ToolGroup.module.css";

const MAX_PREVIEW = 2_000;

/**
 * The tool calls of one run, collapsed by default. The progress of a sub-agent shows under the
 * tool call that started it. Progress without a matching call shows as its own row, at the
 * place of its first event.
 */
export function ToolGroup({
  calls,
  subagents = [],
  runEnded = false,
}: {
  calls: ToolCall[];
  subagents?: SubagentEvent[];
  runEnded?: boolean;
}) {
  const { nested, own } = placeSubagents(
    groupSubagents(subagents, runEnded),
    calls.map((call) => call.callId),
  );
  if (calls.length === 0) {
    if (own.length === 0) return null;
    return (
      <ul className={styles.standalone} aria-label="Sub-agent progress">
        {own.map((group) => (
          <li key={group.key}>
            <SubagentGroup group={group} />
          </li>
        ))}
      </ul>
    );
  }
  // The calls and the stand-alone groups, in the order of their first events.
  const rows = [
    ...calls.map((call) => ({ seq: call.seq, call, group: null })),
    ...own.map((group) => ({ seq: group.firstSeq, call: null, group })),
  ].sort((a, b) => a.seq - b.seq);
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
        {rows.map((row) =>
          row.group !== null ? (
            <li key={row.group.key}>
              <SubagentGroup group={row.group} />
            </li>
          ) : (
            <li key={row.call.callId}>
              <ToolCallRow call={row.call} />
              {(nested.get(row.call.callId) ?? []).map((group) => (
                <div key={group.key} className={styles.nested}>
                  <SubagentGroup group={group} />
                </div>
              ))}
            </li>
          ),
        )}
      </ul>
    </details>
  );
}

function ToolCallRow({ call }: { call: ToolCall }) {
  return (
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
          {call.preview.length > MAX_PREVIEW ? `${call.preview.slice(0, MAX_PREVIEW)}…` : call.preview}
        </pre>
      ) : null}
    </details>
  );
}
