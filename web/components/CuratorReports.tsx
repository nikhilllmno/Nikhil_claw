"use client";

// The curator reports of the admin area (docs/architecture.md, Sections 9.7 and 17.5, M6.3 and
// M6.9): the weekly curator job of each workspace and its newest reports.

import Link from "next/link";
import { useCallback, useState } from "react";
import { reportTitle } from "@/lib/admin";
import { relativeTime, timeLeft } from "@/lib/time";
import { useAdminOrg } from "./Admin";
import { useApi } from "./AuthProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";
import own from "./Admin.module.css";

export function CuratorReports() {
  const org = useAdminOrg();
  const api = useApi();
  const load = useCallback(() => api.curatorReports(org.id, 20), [api, org.id]);
  const { data, error, reload } = useLoad(load, "Could not load the curator reports.");
  const [now] = useState(() => Date.now());

  if (error) return <ErrorState title="The curator reports did not load" message={error} onRetry={reload} />;
  if (data === null) return <LoadingState label="Loading the curator reports" />;

  return (
    <>
      <section className={`card ${styles.card}`} aria-labelledby="curator-jobs">
        <h2 id="curator-jobs" className={styles.heading}>
          Curator jobs
        </h2>
        <p className={styles.text}>
          Each Monday the curator checks the skills and the memory of each workspace. It marks unused skills as stale,
          archives them later, and never deletes one. Pin a skill on the <Link href="/skills">Skills</Link> page to keep
          it.
        </p>
        {data.jobs.length === 0 ? (
          <p className={styles.meta}>
            No curator job yet. Sama adds one to each workspace that has an owner and an agent, at the next scheduler
            tick.
          </p>
        ) : (
          <ul className={styles.list}>
            {data.jobs.map((job) => (
              <li key={job.id} className={styles.row}>
                <div className={styles.name}>
                  <span>{job.workspace_name}</span>
                  <span className={job.status === "active" ? "badge badge-accent" : "badge"}>{job.status}</span>
                </div>
                <span className={styles.meta}>
                  {job.next_fire_at && job.status === "active"
                    ? timeLeft(job.next_fire_at, now) === "ended"
                      ? "The next report is due now"
                      : `Next report ${timeLeft(job.next_fire_at, now)}`
                    : job.last_fire_at
                      ? `Last report ${relativeTime(job.last_fire_at, now)}`
                      : "No report yet"}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className={styles.meta}>
          Change the schedule or where the report goes on the <Link href="/jobs">Jobs</Link> page.
        </p>
      </section>
      <section className={`card ${styles.card}`} aria-labelledby="curator-reports">
        <h2 id="curator-reports" className={styles.heading}>
          Reports
        </h2>
        {data.reports.length === 0 ? (
          <p className={styles.meta}>No reports yet. The first report comes after the first Monday.</p>
        ) : (
          <ul className={styles.entries}>
            {data.reports.map((report, index) => (
              <li key={report.id} className={own.event}>
                <details open={index === 0}>
                  <summary className={own.summary}>
                    <span className={own.eventHead}>
                      <span className={own.eventTitle}>
                        {report.workspace_name} ·{" "}
                        {report.status === "failed"
                          ? "The report failed"
                          : reportTitle(report.output, "Curator report")}
                      </span>
                      <time className="hint" dateTime={report.scheduled_for}>
                        {new Date(report.finished_at ?? report.scheduled_for).toLocaleDateString()}
                        {report.manual ? " · run by hand" : ""}
                      </time>
                    </span>
                  </summary>
                  {report.status === "failed" ? (
                    <p className="alert alert-error">{report.error ?? "The curator did not finish."}</p>
                  ) : (
                    <pre className={own.report}>{report.output ?? "The report is empty."}</pre>
                  )}
                  {report.truncated ? <p className="hint">The report is long. Sama shows only its start.</p> : null}
                </details>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
