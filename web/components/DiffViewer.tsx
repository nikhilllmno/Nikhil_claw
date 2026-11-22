"use client";

// The file changes of the sandbox as diffs (M7.9). The diffs come from the checkpoints. "All
// changes" compares the first checkpoint with the current files. One step compares the files
// before a tool call with the files after it.

import { memo, useCallback, useId, useMemo, useState } from "react";
import type { DiffFile } from "@/lib/api";
import {
  type DiffChoice,
  type DiffLine,
  diffChoiceLabel,
  diffRange,
  diffSummary,
  diffTotals,
  fileCounts,
  fileStatusLabel,
  fileStatusTone,
  fileTitle,
  parsePatch,
  listView,
  startsCollapsed,
} from "@/lib/coding";
import { toneBadge } from "@/lib/history";
import { useApi } from "./AuthProvider";
import styles from "./Coding.module.css";
import page from "./Page.module.css";
import { ErrorState, LoadingState } from "./ui";
import { POLL_MS, usePolled } from "./usePolled";

/** The diff is the heaviest call, so it loads less often than the lists. */
const DIFF_POLL_MS = POLL_MS * 3;

export function DiffViewer({ sessionId }: { sessionId: string }) {
  const api = useApi();
  const selectId = useId();
  const loadCheckpoints = useCallback(() => api.listCheckpoints(sessionId), [api, sessionId]);
  const checkpoints = usePolled(loadCheckpoints, "Could not load the checkpoints.");
  const [choice, setChoice] = useState<DiffChoice>("all");

  const view = listView(checkpoints.data, checkpoints.error);
  if (checkpoints.data === null || view.kind === "loading" || view.kind === "error") {
    if (view.kind === "error" && checkpoints.error) {
      return <ErrorState title="The checkpoints did not load" message={checkpoints.error} onRetry={checkpoints.reload} />;
    }
    return <LoadingState label="Reading the changes from the sandbox. It can take a few seconds while the sandbox wakes up" />;
  }
  const warning = view.warning ? (
    <p className="alert alert-warning" role="status">
      {view.warning}{" "}
      <button type="button" className="btn btn-small" onClick={checkpoints.reload}>
        Try again
      </button>
    </p>
  ) : null;
  if (view.kind === "empty") {
    return (
      <div className={styles.panel}>
        {warning}
        <p className="alert">
          The session has no checkpoints yet. The agent makes a checkpoint before each tool call. Its
          file changes show here after that.
        </p>
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      {warning}
      <div className="field">
        <label className="label" htmlFor={selectId}>
          Show
        </label>
        <select
          id={selectId}
          className="input"
          value={String(choice)}
          onChange={(e) => setChoice(e.target.value === "all" ? "all" : Number(e.target.value))}
        >
          <option value="all">All changes</option>
          {checkpoints.data.map((item) => (
            <option key={item.number} value={item.number}>
              {diffChoiceLabel(item)}
            </option>
          ))}
        </select>
      </div>
      <DiffResult key={String(choice)} sessionId={sessionId} choice={choice} />
    </div>
  );
}

function DiffResult({ sessionId, choice }: { sessionId: string; choice: DiffChoice }) {
  const api = useApi();
  const load = useCallback(() => {
    const { base, head } = diffRange(choice);
    return api.getDiff(sessionId, base, head);
  }, [api, sessionId, choice]);
  const diff = usePolled(load, "Could not load the changes.", DIFF_POLL_MS);
  // The files that the user opened or closed. Other files follow the size rule.
  const [open, setOpen] = useState<Record<string, boolean>>({});

  if (diff.data === null) {
    if (diff.error) return <ErrorState title="The changes did not load" message={diff.error} onRetry={diff.reload} />;
    return <LoadingState label="Reading the changes from the sandbox. It can take a few seconds while the sandbox wakes up" />;
  }
  const { files, truncated, base, head } = diff.data;
  const totals = diffTotals(files);

  return (
    <div className={styles.stack}>
      {diff.error ? (
        <p className="alert alert-warning" role="status">
          {diff.error}{" "}
          <button type="button" className="btn btn-small" onClick={diff.reload}>
            Try again
          </button>
        </p>
      ) : null}
      <p className={page.meta}>
        {diffSummary(base, head)}{" "}
        {files.length === 0
          ? "No file changes."
          : `${files.length} ${files.length === 1 ? "file" : "files"} changed, +${totals.additions} -${totals.deletions}.`}
      </p>
      {truncated ? (
        <p className="alert alert-warning">
          The diff is too large. It shows only a part of the files or of the text.
        </p>
      ) : null}
      {files.length > 0 ? (
        <ul className={page.list} aria-label="Changed files">
          {files.map((file) => {
            const expanded = open[file.path] ?? !startsCollapsed(file);
            return (
              <li key={`${file.old_path ?? ""}>${file.path}`}>
                <FileDiff
                  file={file}
                  expanded={expanded}
                  onToggle={() => setOpen((o) => ({ ...o, [file.path]: !expanded }))}
                />
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}

const FileDiff = memo(function FileDiff({
  file,
  expanded,
  onToggle,
}: {
  file: DiffFile;
  expanded: boolean;
  onToggle: () => void;
}) {
  const bodyId = useId();
  // The rows are only built for a file that is open.
  const rows = useMemo(() => (expanded ? parsePatch(file.patch) : []), [expanded, file.patch]);
  const title = fileTitle(file);
  return (
    <section className={`card ${styles.file}`} aria-label={title}>
      <button type="button" className={styles.fileHead} aria-expanded={expanded} aria-controls={bodyId} onClick={onToggle}>
        <span className={page.name}>
          <span className={toneBadge(fileStatusTone(file.status))}>{fileStatusLabel(file.status)}</span>
          <span className={styles.filePath}>{title}</span>
        </span>
        <span className={styles.counts}>
          {file.binary || file.additions === null || file.deletions === null ? (
            <span>{fileCounts(file)}</span>
          ) : (
            <>
              <span className={styles.added}>+{file.additions}</span>
              <span className={styles.removed}>-{file.deletions}</span>
            </>
          )}
        </span>
      </button>
      <div id={bodyId} hidden={!expanded}>
        {expanded ? <FileBody file={file} rows={rows} /> : null}
      </div>
    </section>
  );
});

function FileBody({ file, rows }: { file: DiffFile; rows: DiffLine[] }) {
  return (
    <>
      {file.binary ? <p className={`${page.meta} ${styles.help}`}>A binary file. No text diff.</p> : null}
      {!file.binary && rows.length === 0 ? (
        <p className={page.meta}>No text changes. Only the mode or the name of the file changed.</p>
      ) : null}
      {rows.length > 0 ? (
        <div className={styles.scrollX}>
          <table className={styles.diff} aria-label={`Diff of ${file.path}`}>
            <tbody>
              {rows.map((row, i) => (
                <DiffRow key={i} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {file.truncated ? (
        <p className="alert alert-warning">The patch of this file is cut. It shows the first part only.</p>
      ) : null}
    </>
  );
}

const SIGNS = { add: "+", del: "-", context: " ", note: "", hunk: "" } as const;
const ROW_CLASS = {
  add: styles.rowAdd,
  del: styles.rowDel,
  hunk: styles.rowHunk,
  note: styles.rowNote,
  context: "",
} as const;

function DiffRow({ row }: { row: DiffLine }) {
  if (row.kind === "hunk") {
    return (
      <tr className={ROW_CLASS.hunk}>
        <td colSpan={3} className={styles.code}>
          {row.text}
        </td>
      </tr>
    );
  }
  return (
    <tr className={ROW_CLASS[row.kind]}>
      <td className={styles.num}>{row.oldNo ?? ""}</td>
      <td className={styles.num}>{row.newNo ?? ""}</td>
      <td className={styles.code}>
        {SIGNS[row.kind]}
        {row.text}
      </td>
    </tr>
  );
}
