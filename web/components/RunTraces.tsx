"use client";

// The run traces of a session (docs/architecture.md, Section 11.4). Each run shows its status,
// its tokens and cost, and on request its trace: the steps, the tool calls, and the model calls.
// The API redacts the text again before it sends it.

import { useCallback, useState } from "react";
import type { RunSummary } from "@/lib/api";
import { sessionTitle } from "@/lib/chat";
import {
  formatCost,
  formatDuration,
  formatTokens,
  runTone,
  runUsage,
  stepSummary,
  stepTitle,
  toneBadge,
  toolTone,
} from "@/lib/history";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { PageHeader } from "./PageHeader";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

export function RunTraces({ sessionId }: { sessionId: string }) {
  const api = useApi();
  const loadSession = useCallback(() => api.getSession(sessionId), [api, sessionId]);
  const loadRuns = useCallback(() => api.listRuns(sessionId), [api, sessionId]);
  const session = useLoad(loadSession, "Could not load the session.");
  const runs = useLoad(loadRuns, "Could not load the runs.");
  const [open, setOpen] = useState<string | null>(null);
  const [now] = useState(() => Date.now());

  const error = session.error ?? runs.error;
  if (error) {
    return (
      <ErrorState
        title="The runs did not load"
        message={error}
        onRetry={() => {
          session.reload();
          runs.reload();
        }}
      />
    );
  }
  if (session.data === null || runs.data === null) return <LoadingState label="Loading the runs" />;

  return (
    <div className={styles.page}>
      <PageHeader
        title="Runs"
        back={{ href: `/s/${sessionId}`, label: "Back to the session" }}
        meta={
          <>
            {sessionTitle(session.data)} · Each run is one answer of the agent. Open a run to see its
            steps, tool calls, and model calls.
          </>
        }
      />
      {runs.data.length === 0 ? (
        <p className="alert">This session has no runs yet.</p>
      ) : (
        <ul className={styles.list} aria-label="Runs">
          {runs.data.map((run) => (
            <li key={run.id}>
              <RunCard
                run={run}
                now={now}
                open={open === run.id}
                onToggle={() => setOpen((o) => (o === run.id ? null : run.id))}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function RunCard({
  run,
  now,
  open,
  onToggle,
}: {
  run: RunSummary;
  now: number;
  open: boolean;
  onToggle: () => void;
}) {
  const duration = formatDuration(run.started_at, run.finished_at);
  return (
    <section className={`card ${styles.card}`} aria-label={`Run ${run.id.slice(0, 8)}`}>
      <div className={styles.cardHead}>
        <div className={styles.name}>
          <span className={toneBadge(runTone(run.status))}>{run.status}</span>
          <span>{run.compact ? "Compaction" : run.kind === "review" ? "Background review" : "Answer"}</span>
        </div>
        <span className={styles.meta}>
          {relativeTime(run.created_at, now)}
          {duration ? ` · ${duration}` : ""}
        </span>
      </div>
      <p className={styles.meta}>
        {runUsage(run)} · {run.model_calls === 1 ? "1 model call" : `${run.model_calls} model calls`} ·{" "}
        {run.runtime}
      </p>
      {run.failure_message || run.failure_code ? (
        <p className={styles.error}>{[run.failure_code, run.failure_message].filter(Boolean).join(": ")}</p>
      ) : null}
      <div className={styles.actions}>
        <button type="button" className="btn btn-small" aria-expanded={open} onClick={onToggle}>
          {open ? "Hide the trace" : "Show the trace"}
        </button>
      </div>
      {open ? <Trace runId={run.id} /> : null}
    </section>
  );
}

function Trace({ runId }: { runId: string }) {
  const api = useApi();
  const load = useCallback(() => api.getRunTrace(runId), [api, runId]);
  const trace = useLoad(load, "Could not load the trace.");
  if (trace.error) return <ErrorState title="The trace did not load" message={trace.error} onRetry={trace.reload} />;
  if (trace.data === null) return <LoadingState label="Loading the trace" />;
  const { steps, tool_calls: tools, model_calls: models, truncated } = trace.data;

  return (
    <>
      {truncated ? (
        <p className="alert alert-warning">The run has more events than the trace can show. The trace shows the first ones.</p>
      ) : null}
      <div className={styles.section}>
        <h3 className={styles.subheading}>Steps</h3>
        {steps.length === 0 ? (
          <p className={styles.meta}>No steps.</p>
        ) : (
          <ol className={styles.list}>
            {steps.map((step) => {
              const summary = stepSummary(step);
              return (
                <li key={step.seq} className={styles.form}>
                  <div className={styles.row}>
                    <span className={styles.name}>{stepTitle(step)}</span>
                    <span className={styles.meta}>#{step.seq}</span>
                  </div>
                  {summary ? (
                    <p className={step.type === "text" ? styles.entry : styles.text}>{summary}</p>
                  ) : null}
                  {step.type !== "text" && Object.keys(step.payload).length > 0 ? (
                    <details>
                      <summary className={styles.meta}>Details</summary>
                      <pre className={styles.pre}>{JSON.stringify(step.payload, null, 2)}</pre>
                    </details>
                  ) : null}
                </li>
              );
            })}
          </ol>
        )}
      </div>
      <div className={styles.section}>
        <h3 className={styles.subheading}>Tool calls</h3>
        {tools.length === 0 ? (
          <p className={styles.meta}>No tool calls.</p>
        ) : (
          <ul className={styles.list}>
            {tools.map((call) => (
              <li key={call.call_id} className={styles.form}>
                <div className={styles.row}>
                  <span className={`mono ${styles.name}`}>{call.tool_name}</span>
                  <span className={styles.name}>
                    {call.decision ? <span className="badge">{call.decision}</span> : null}
                    <span className={toneBadge(toolTone(call.status))}>
                      {call.status}
                    </span>
                  </span>
                </div>
                <details>
                  <summary className={styles.meta}>Arguments and result</summary>
                  <pre className={styles.pre}>{JSON.stringify(call.arguments, null, 2)}</pre>
                  {call.result_preview ? <pre className={styles.pre}>{call.result_preview}</pre> : null}
                </details>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className={styles.section}>
        <h3 className={styles.subheading}>Model calls</h3>
        {models.length === 0 ? (
          <p className={styles.meta}>No model calls.</p>
        ) : (
          <div className={styles.scroll}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Model</th>
                  <th scope="col">Tokens in / out</th>
                  <th scope="col">Cache read / write</th>
                  <th scope="col">Cost</th>
                  <th scope="col">Time</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {models.map((m, i) => (
                  <tr key={i}>
                    <td className="mono">{m.model}</td>
                    <td>
                      {formatTokens(m.input_tokens)} / {formatTokens(m.output_tokens)}
                    </td>
                    <td>
                      {formatTokens(m.cache_read_tokens)} / {formatTokens(m.cache_write_tokens)}
                    </td>
                    <td>{formatCost(m.cost_usd)}</td>
                    <td>{m.duration_ms} ms</td>
                    <td>{m.status_code}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
