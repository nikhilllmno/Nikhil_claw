import { formatTokens } from "@/lib/history";
import { type SubagentGroup as Group, subagentName, subagentStepsLabel } from "@/lib/subagentProgress";
import styles from "./SubagentGroup.module.css";

/**
 * The progress of one sub-agent invocation, collapsed by default. The summary shows the name,
 * the number of steps, the state, and the tokens. While the sub-agent runs, it also shows the
 * latest step. The steps are plain text.
 */
export function SubagentGroup({ group }: { group: Group }) {
  const latest = group.steps[group.steps.length - 1]?.text ?? "";
  const state = group.done ? "done" : "running";
  return (
    <details className={styles.group} role="group" aria-label={`Sub-agent ${subagentName(group.subagent)}, ${state}`}>
      <summary className={styles.summary}>
        {group.done ? <span className={styles.dot} aria-hidden="true" /> : <span className="spinner" aria-hidden="true" />}
        <span className={styles.name}>{subagentName(group.subagent)}</span>
        <span className="badge">{subagentStepsLabel(group.steps.length)}</span>
        <span className={group.done ? "badge badge-accent" : "badge badge-warning"}>{state}</span>
        {group.tokens !== null ? <span className="badge">{formatTokens(group.tokens)} tokens</span> : null}
        {!group.done && latest !== "" ? <span className={styles.preview}>{latest}</span> : null}
      </summary>
      {group.steps.length > 0 ? (
        <ol className={styles.steps} aria-label={`Steps of sub-agent ${subagentName(group.subagent)}`}>
          {group.steps.map((step) => (
            <li key={step.seq} className={styles.step} title={step.text}>
              {step.text}
            </li>
          ))}
        </ol>
      ) : (
        <p className={styles.empty}>No steps yet.</p>
      )}
    </details>
  );
}
