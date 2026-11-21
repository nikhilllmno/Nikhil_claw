"use client";

// The usage page of the admin area (docs/architecture.md, Section 17.6, M6.9): the model cost
// and the tokens of the org for 7, 30, or 90 UTC days, by day, person, agent, model, and key.

import { useCallback, useState } from "react";
import type { UsageRow } from "@/lib/api";
import { costShare, dayBars, shortDay, USAGE_RANGES, type UsageRange } from "@/lib/admin";
import { formatCost, formatTokens } from "@/lib/history";
import { useAdminOrg, Stat } from "./Admin";
import { useApi } from "./AuthProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";
import own from "./Admin.module.css";

export function Usage() {
  const org = useAdminOrg();
  const api = useApi();
  const [days, setDays] = useState<UsageRange>(30);
  const load = useCallback(() => api.orgUsage(org.id, days), [api, org.id, days]);
  const { data, error, reload } = useLoad(load, "Could not load the usage.");
  // The answer of an older range can still be on screen while the new one loads.
  const usage = data !== null && data.days === days ? data : null;

  return (
    <>
      <div className={styles.row}>
        <p className={styles.meta}>The days are UTC days. The cost counts the org keys and the key of sama.</p>
        <div className={own.segmented} role="group" aria-label="Time range">
          {USAGE_RANGES.map((range) => (
            <button key={range} type="button" aria-pressed={range === days} onClick={() => setDays(range)}>
              {range} days
            </button>
          ))}
        </div>
      </div>
      {error ? (
        <ErrorState title="The usage did not load" message={error} onRetry={reload} />
      ) : usage === null ? (
        <LoadingState label="Loading the usage" />
      ) : (
        <>
          <dl className={own.stats}>
            <Stat label="Model cost" value={formatCost(usage.totals.cost_usd)} />
            <Stat label="On org keys" value={formatCost(usage.totals.org_key_cost_usd)} />
            <Stat label="Model calls" value={String(usage.totals.model_calls)} />
            <Stat label="Tokens in" value={formatTokens(usage.totals.input_tokens)} />
            <Stat label="Tokens out" value={formatTokens(usage.totals.output_tokens)} />
            <Stat label="Runs" value={String(usage.totals.runs)} />
          </dl>
          {usage.totals.model_calls === 0 ? (
            <div className="state">
              <h2>No model use yet</h2>
              <p>
                The agents of {org.name} made no model calls in the last {days} days. Start a session to see the cost
                here.
              </p>
            </div>
          ) : (
            <>
              <section className={`card ${styles.card}`} aria-labelledby="usage-daily">
                <h2 id="usage-daily" className={styles.heading}>
                  Cost each day
                </h2>
                <div className={own.chart} role="img" aria-label={`The model cost of each day for ${days} days`}>
                  {dayBars(usage.by_day).map((bar) => (
                    <div key={bar.day} className={own.bar} title={`${shortDay(bar.day)}: ${formatCost(bar.cost)}`}>
                      <div className={own.barFill} style={{ height: `${bar.percent}%` }} />
                    </div>
                  ))}
                </div>
                <div className={own.axis} aria-hidden="true">
                  <span>{shortDay(usage.by_day[0]?.day ?? "")}</span>
                  <span>{shortDay(usage.by_day.at(-1)?.day ?? "")}</span>
                </div>
                <details>
                  <summary className={`hint ${own.summary}`}>Show the cost of each day</summary>
                  <div className={styles.scroll}>
                    <table className={own.data}>
                      <thead>
                        <tr>
                          <th scope="col">Day (UTC)</th>
                          <th scope="col">Cost</th>
                          <th scope="col">Calls</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...usage.by_day].reverse().map((d) => (
                          <tr key={d.day}>
                            <td>{shortDay(d.day)}</td>
                            <td>{formatCost(d.cost_usd)}</td>
                            <td>{d.model_calls}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              </section>
              <Breakdown id="usage-people" title="People" column="Person" rows={usage.by_user} total={usage.totals.cost_usd} />
              <Breakdown id="usage-agents" title="Agents" column="Agent" rows={usage.by_profile} total={usage.totals.cost_usd} />
              <Breakdown id="usage-models" title="Models" column="Model" rows={usage.by_model} total={usage.totals.cost_usd} />
              <Breakdown id="usage-keys" title="Keys" column="Key" rows={usage.by_key_source} total={usage.totals.cost_usd} />
            </>
          )}
        </>
      )}
    </>
  );
}

function Breakdown({
  id,
  title,
  column,
  rows,
  total,
}: {
  id: string;
  title: string;
  column: string;
  rows: UsageRow[];
  total: string;
}) {
  return (
    <section className={`card ${styles.card}`} aria-labelledby={id}>
      <h2 id={id} className={styles.heading}>
        {title}
      </h2>
      <div className={styles.scroll}>
        <table className={own.data}>
          <thead>
            <tr>
              <th scope="col">{column}</th>
              <th scope="col">Cost</th>
              <th scope="col" className={own.hideNarrow}>
                Calls
              </th>
              <th scope="col" className={own.hideNarrow}>
                Tokens in / out
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const share = costShare(row, total);
              return (
                <tr key={row.key ?? row.label}>
                  <td>
                    <div>{row.label}</div>
                    <span className={own.share} aria-hidden="true">
                      <span className={own.shareFill} style={{ width: `${share}%` }} />
                    </span>
                  </td>
                  <td>
                    {formatCost(row.cost_usd)}
                    <div className="hint">{share}%</div>
                  </td>
                  <td className={own.hideNarrow}>{row.model_calls}</td>
                  <td className={own.hideNarrow}>
                    {formatTokens(row.input_tokens)} / {formatTokens(row.output_tokens)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
