"use client";

// The audit log of the admin area (docs/architecture.md, Sections 17.1 and 17.5, M6.9): the
// entries of the org, newest first, with a filter by kind and a button for older entries.

import { useCallback, useState } from "react";
import { type AuditEntry, errorText } from "@/lib/api";
import { AUDIT_FILTERS, auditActor, auditDetails, auditLabel } from "@/lib/admin";
import { relativeTime } from "@/lib/time";
import { useAdminOrg } from "./Admin";
import { useApi } from "./AuthProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";
import own from "./Admin.module.css";

const PAGE = 50;

export function AuditLog() {
  const org = useAdminOrg();
  const api = useApi();
  const [filter, setFilter] = useState("");
  const load = useCallback(
    async () => ({ filter, page: await api.orgAudit(org.id, { limit: PAGE, action: filter }) }),
    [api, org.id, filter],
  );
  const first = useLoad(load, "Could not load the audit log.");
  // The older pages that the user loaded for this filter.
  const [older, setOlder] = useState<{ filter: string; entries: AuditEntry[]; next: number | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);
  const [now] = useState(() => Date.now());

  const current = first.data !== null && first.data.filter === filter ? first.data.page : null;
  const extra = older !== null && older.filter === filter ? older : null;
  const entries = current === null ? [] : [...current.entries, ...(extra?.entries ?? [])];
  const next = extra ? extra.next : (current?.next_before ?? null);

  const loadMore = async () => {
    if (next === null) return;
    setBusy(true);
    setMoreError(null);
    try {
      const page = await api.orgAudit(org.id, { limit: PAGE, action: filter, before: next });
      setOlder({ filter, entries: [...(extra?.entries ?? []), ...page.entries], next: page.next_before });
    } catch (e) {
      setMoreError(errorText(e, "Could not load more entries."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className={styles.toolbar}>
        <label className={`field ${styles.grow}`}>
          <span className="label">Show</span>
          <select className="input" value={filter} onChange={(e) => setFilter(e.target.value)}>
            {AUDIT_FILTERS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <section className={`card ${styles.card}`} aria-label="Audit entries" aria-busy={current === null}>
        {first.error ? (
          <ErrorState title="The audit log did not load" message={first.error} onRetry={first.reload} />
        ) : current === null ? (
          <LoadingState label="Loading the audit log" />
        ) : entries.length === 0 ? (
          <p className={styles.meta}>
            {filter === "" ? "The audit log of the org is empty." : "No entries of this kind."}
          </p>
        ) : (
          <ul className={styles.entries}>
            {entries.map((entry) => (
              <li key={entry.id} className={own.event}>
                <div className={own.eventHead}>
                  <span className={own.eventTitle}>
                    {auditActor(entry)} · {auditLabel(entry.action)}
                  </span>
                  <time className="hint" dateTime={entry.created_at} title={new Date(entry.created_at).toLocaleString()}>
                    {relativeTime(entry.created_at, now)}
                  </time>
                </div>
                <div className={own.chips}>
                  <span className={own.chip}>{entry.action}</span>
                  {auditDetails(entry.details).map((part) => (
                    <span key={part} className={own.chip}>
                      {part}
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}
        {next !== null && current !== null ? (
          <div className={styles.actions}>
            <button type="button" className="btn btn-small" disabled={busy} onClick={() => void loadMore()}>
              {busy ? "Loading…" : "Load older entries"}
            </button>
          </div>
        ) : null}
        {moreError ? (
          <p className="alert alert-error" role="alert">
            {moreError}
          </p>
        ) : null}
      </section>
    </>
  );
}
