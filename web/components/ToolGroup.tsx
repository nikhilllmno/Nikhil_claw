import type { ReactNode } from "react";
import type { Segment, ToolCall } from "@/lib/chat";
import { groupSubagents, placeSubagents, type SubagentEvent, type SubagentGroup as Group } from "@/lib/subagentProgress";
import { outputPreview, todoList, toolLabel, toolSubject, type TodoItem } from "@/lib/toolView";
import { Markdown } from "./Markdown";
import { SubagentGroup } from "./SubagentGroup";
import styles from "./ToolGroup.module.css";

/** The sub-agent progress of a run, placed under the call that started it. */
function usePlacement(calls: ToolCall[], subagents: SubagentEvent[], runEnded: boolean) {
  return placeSubagents(
    groupSubagents(subagents, runEnded),
    calls.map((call) => call.callId),
  );
}

function Nested({ groups }: { groups: Group[] | undefined }) {
  if (!groups || groups.length === 0) return null;
  return (
    <div className={styles.nested}>
      {groups.map((group) => (
        <SubagentGroup key={group.key} group={group} />
      ))}
    </div>
  );
}

/**
 * The tool calls of a run that ended, collapsed into one line ("Worked: 4 steps"), as Codex
 * shows them. Open, each call is one compact row. The progress of a sub-agent shows under the
 * tool call that started it.
 */
export function ToolTrail({
  calls,
  subagents = [],
  runEnded = true,
}: {
  calls: ToolCall[];
  subagents?: SubagentEvent[];
  runEnded?: boolean;
}) {
  const { nested, own } = usePlacement(calls, subagents, runEnded);
  if (calls.length === 0 && own.length === 0) return null;
  const rows = [
    ...calls.map((call) => ({ seq: call.seq, call, group: null })),
    ...own.map((group) => ({ seq: group.firstSeq, call: null, group })),
  ].sort((a, b) => a.seq - b.seq);
  const failed = calls.filter((c) => c.status === "error").length;
  const running = calls.some((c) => c.status === "running");
  const labels = [...new Set(calls.map((c) => toolLabel(c.name)))];
  const steps = calls.length + own.length;
  return (
    <details className={styles.trail}>
      <summary className={styles.trailSummary}>
        <span className={styles.chevron} aria-hidden="true" />
        <span className={styles.trailTitle}>
          {running ? "Working" : "Worked"}: {steps} {steps === 1 ? "step" : "steps"}
        </span>
        <span className={styles.trailNames}>{labels.join(", ")}</span>
        {failed > 0 ? <span className="badge badge-warning">{failed} failed</span> : null}
      </summary>
      <ol className={styles.rows}>
        {rows.map((row) =>
          row.group !== null ? (
            <li key={row.group.key}>
              <SubagentGroup group={row.group} />
            </li>
          ) : (
            <li key={row.call.callId}>
              <ToolCallRow call={row.call} />
              <Nested groups={nested.get(row.call.callId)} />
            </li>
          ),
        )}
      </ol>
    </details>
  );
}

/** Kept for the callers of the old name: the trail of a run. */
export const ToolGroup = ToolTrail;

/**
 * The reply of a run that is open: the text and the tool calls in the order of the events, as
 * Claude Code shows them. Calls that the view knows without a place in the text (they started
 * before the page opened) come first.
 */
export function LiveRun({
  segments,
  calls,
  subagents,
  label,
  footer,
  afterCall,
}: {
  segments: Segment[];
  calls: ToolCall[];
  subagents: SubagentEvent[];
  /** For example "Run 2" when runs work at the same time. */
  label?: string | null;
  /** The working line under the reply. */
  footer?: ReactNode;
  /** What shows under the row of a call, for example the Connect card of M8.1. */
  afterCall?: (call: ToolCall) => ReactNode;
}) {
  const { nested, own } = usePlacement(calls, subagents, false);
  const byId = new Map(calls.map((call) => [call.callId, call]));
  const placed = new Set(segments.flatMap((s) => (s.kind === "tool" ? [s.callId] : [])));
  const earlier = calls.filter((call) => !placed.has(call.callId));
  const last = segments[segments.length - 1];
  return (
    <article className={styles.live} aria-label={label ? `Assistant, working in ${label}` : "Assistant, working"}>
      {label ? <p className={styles.runLabel}>{label}</p> : null}
      {earlier.length > 0 || own.length > 0 ? (
        <ol className={styles.rows}>
          {earlier.map((call) => (
            <li key={call.callId}>
              <ToolCallRow call={call} />
              <Nested groups={nested.get(call.callId)} />
              {afterCall?.(call)}
            </li>
          ))}
          {own.map((group) => (
            <li key={group.key}>
              <SubagentGroup group={group} />
            </li>
          ))}
        </ol>
      ) : null}
      {segments.map((segment, index) => {
        if (segment.kind === "text") {
          return (
            <div key={`t-${segment.seq}`} className={styles.text}>
              <Markdown text={segment.text} />
              {index === segments.length - 1 ? <span className={styles.caret} aria-hidden="true" /> : null}
            </div>
          );
        }
        const call = byId.get(segment.callId);
        if (!call) return null;
        return (
          <div key={`c-${segment.callId}`}>
            <ToolCallRow call={call} />
            <Nested groups={nested.get(call.callId)} />
            {afterCall?.(call)}
          </div>
        );
      })}
      {last?.kind !== "text" ? footer : null}
      {last?.kind === "text" && footer ? <div className={styles.footerQuiet}>{footer}</div> : null}
    </article>
  );
}

/** One tool call in one line: a status mark, a verb, and the main input. Open, the input and the output. */
export function ToolCallRow({ call }: { call: ToolCall }) {
  const subject = toolSubject(call.name, call.arguments);
  const output = outputPreview(call.preview);
  const args = Object.keys(call.arguments).length > 0 ? JSON.stringify(call.arguments, null, 2) : null;
  const todos = todoList(call.name, call.arguments);
  const status = call.status === "running" ? "running" : call.status === "error" ? "failed" : "done";
  return (
    <details className={styles.call} data-status={call.status}>
      <summary className={styles.callSummary} aria-label={`${toolLabel(call.name)} ${subject}, ${status}`}>
        {call.status === "running" ? (
          <span className="spinner" aria-hidden="true" />
        ) : (
          <span className={`${styles.mark} ${call.status === "error" ? styles.markError : ""}`} aria-hidden="true" />
        )}
        <span className={styles.verb}>{toolLabel(call.name)}</span>
        {subject ? <span className={styles.subject}>{subject}</span> : null}
        {call.status === "error" ? <span className={styles.failed}>failed</span> : null}
      </summary>
      <div className={styles.callBody}>
        {todos !== null ? <TodoList items={todos} /> : null}
        {args !== null && todos === null ? (
          <pre className={styles.code} aria-label="Input">
            {args}
          </pre>
        ) : null}
        {output !== null ? (
          <pre className={`${styles.code} ${call.status === "error" ? styles.codeError : ""}`} aria-label="Output">
            {output}
          </pre>
        ) : call.status === "running" ? (
          <p className={styles.waiting}>Waiting for the result…</p>
        ) : null}
      </div>
    </details>
  );
}

/** The to-do list of the agent, as Claude Code and Codex show it. */
export function TodoList({ items, title }: { items: TodoItem[]; title?: string }) {
  if (items.length === 0) return null;
  const done = items.filter((item) => item.status === "completed").length;
  return (
    <section className={styles.todos} aria-label={title ?? "To-do list"}>
      {title ? (
        <p className={styles.todoTitle}>
          {title} <span className={styles.todoCount}>{done} of {items.length} done</span>
        </p>
      ) : null}
      <ul className={styles.todoList}>
        {items.map((item, index) => (
          <li key={index} className={styles.todo} data-status={item.status}>
            <span className={styles.todoMark} aria-hidden="true" />
            <span className={styles.todoText}>{item.text}</span>
            <span className="sr-only">
              {item.status === "completed" ? "done" : item.status === "in_progress" ? "in progress" : "to do"}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
