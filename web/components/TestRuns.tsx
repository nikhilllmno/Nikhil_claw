"use client";

// The commands that the agent ran in the sandbox, and the test runs among them (M7.9).

import { useCallback, useState } from "react";
import type { CommandOut } from "@/lib/api";
import { commandCounts, commandStatusLabel, commandTone, listView } from "@/lib/coding";
import { toneBadge } from "@/lib/history";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import styles from "./Coding.module.css";
import page from "./Page.module.css";
import { ErrorState, LoadingState } from "./ui";
import { useNow, usePolled } from "./usePolled";

export function TestRuns({ sessionId }: { sessionId: string }) {
  const [testsOnly, setTestsOnly] = useState(false);
  return (
    <div className={styles.panel}>
      <label className={styles.check}>
        <input type="checkbox" checked={testsOnly} onChange={(e) => setTestsOnly(e.target.checked)} />
        Tests only
      </label>
      <CommandList key={String(testsOnly)} sessionId={sessionId} testsOnly={testsOnly} />
    </div>
  );
}

function CommandList({ sessionId, testsOnly }: { sessionId: string; testsOnly: boolean }) {
  const api = useApi();
  const now = useNow();
  const load = useCallback(() => api.listCommands(sessionId, testsOnly), [api, sessionId, testsOnly]);
  const commands = usePolled(load, "Could not load the commands.");

  const view = listView(commands.data, commands.error);
  if (commands.data === null || view.kind === "loading" || view.kind === "error") {
    if (view.kind === "error" && commands.error) {
      return <ErrorState title="The commands did not load" message={commands.error} onRetry={commands.reload} />;
    }
    return <LoadingState label="Loading the commands" />;
  }
  const warning = view.warning ? (
    <p className="alert alert-warning" role="status">
      {view.warning}{" "}
      <button type="button" className="btn btn-small" onClick={commands.reload}>
        Try again
      </button>
    </p>
  ) : null;
  if (view.kind === "empty") {
    return (
      <div className={styles.stack}>
        {warning}
        <p className="alert">
          {testsOnly
            ? "The agent ran no tests in this session yet."
            : "The agent ran no commands in this session yet."}
        </p>
      </div>
    );
  }
  return (
    <div className={styles.stack}>
      {warning}
      <p className={page.meta}>{commandCounts(commands.data)}, newest first.</p>
      <ul className={page.list} aria-label={testsOnly ? "Test runs" : "Commands"}>
        {commands.data.map((command) => (
          <li key={`${command.run_id}:${command.call_id}`}>
            <CommandCard command={command} now={now} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function CommandCard({ command, now }: { command: CommandOut; now: number }) {
  const label = commandStatusLabel(command);
  return (
    <section className={`card ${page.card}`} aria-label={`${command.test ? "Test run" : "Command"}: ${command.command}`}>
      <div className={page.cardHead}>
        <div className={page.name}>
          <span className={toneBadge(commandTone(command.status))}>{label}</span>
          {command.test ? <span className="badge">Test</span> : null}
          <span className={`mono ${page.meta}`}>{command.tool}</span>
        </div>
        <span className={page.meta}>{relativeTime(command.started_at, now)}</span>
      </div>
      <pre className={styles.command}>{command.command}</pre>
      {command.summary ? <p className={page.text}>{command.summary}</p> : null}
      {command.output ? (
        <details>
          <summary className={styles.summary}>Output</summary>
          <pre className={page.pre}>{command.output}</pre>
        </details>
      ) : null}
    </section>
  );
}
