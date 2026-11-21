"use client";

// The admin area (docs/architecture.md, Section 17.6, M6.9). Only an org owner or admin sees
// it. The frame chooses the org, shows the tabs, and gives the org to each admin page.

import Link from "next/link";
import { usePathname } from "next/navigation";
import { createContext, Fragment, useCallback, useContext, useState } from "react";
import type { Org } from "@/lib/api";
import { formatCost } from "@/lib/history";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { PageHeader } from "./PageHeader";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";
import own from "./Admin.module.css";

export const ADMIN_TABS = [
  { href: "/admin", label: "Overview" },
  { href: "/admin/usage", label: "Usage" },
  { href: "/admin/audit", label: "Audit log" },
  { href: "/admin/changes", label: "Agent changes" },
  { href: "/admin/curator", label: "Curator reports" },
  { href: "/admin/keys", label: "Org keys and region" },
  { href: "/admin/api-keys", label: "API keys" },
  { href: "/admin/data", label: "Data controls" },
] as const;

const OrgContext = createContext<Org | null>(null);

/** The org of the admin page. Only a page inside `AdminFrame` can use it. */
export function useAdminOrg(): Org {
  const org = useContext(OrgContext);
  if (org === null) throw new Error("useAdminOrg needs an AdminFrame.");
  return org;
}

export function AdminFrame({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { me, meError, reloadMe, workspaceId, orgOfWorkspace } = useData();
  const [chosen, setChosen] = useState<string | null>(null);
  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the admin area" />;
  const admins = me.orgs.filter((o) => o.admin);
  const current = orgOfWorkspace(workspaceId);
  const org =
    admins.find((o) => o.id === chosen) ?? (current?.admin ? current : null) ?? admins[0] ?? null;
  const tab = ADMIN_TABS.find((t) => t.href === pathname);

  return (
    <div className={styles.page}>
      <PageHeader
        title={tab && tab.href !== "/admin" ? tab.label : "Admin"}
        meta={org ? `Manage ${org.name}: people, usage, keys, and data.` : "Manage your org."}
        back={tab && tab.href !== "/admin" ? { href: "/admin", label: "Back to Admin" } : undefined}
      />
      {org === null ? (
        <div className="state">
          <h2>Only for org admins</h2>
          <p>The admin area is for the owners and the admins of an org. Ask an owner of your org for the admin role.</p>
          <Link href="/" className="btn">
            Back to sessions
          </Link>
        </div>
      ) : (
        <>
          {admins.length > 1 ? (
            <label className="field">
              <span className="label">Org</span>
              <select className="input" value={org.id} onChange={(e) => setChosen(e.target.value)}>
                {admins.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <nav aria-label="Admin">
            <ul className={own.tabs}>
              {ADMIN_TABS.map((t) => (
                <li key={t.href}>
                  <Link href={t.href} className={own.tab} aria-current={pathname === t.href ? "page" : undefined}>
                    {t.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
          {org.region_here ? (
            <OrgContext.Provider value={org}>
              {/* A new org mounts the page again, so no data, draft, or secret of the old org stays. */}
              <Fragment key={org.id}>{children}</Fragment>
            </OrgContext.Provider>
          ) : (
            <div className="state">
              <h2>This org is in another region</h2>
              <p>
                {org.name} is in region {org.region}. Another deployment of sama serves it.
              </p>
              {org.region_url ? (
                <a className="btn" href={org.region_url} rel="noopener noreferrer">
                  Open the org there
                </a>
              ) : null}
            </div>
          )}
        </>
      )}
    </div>
  );
}

const TILES = [
  { href: "/members", title: "Members", text: "Invite people, change roles, and choose their workspaces." },
  { href: "/billing", title: "Billing", text: "The plan of the org, the cost against its quotas, and the payment." },
  { href: "/admin/usage", title: "Usage", text: "The model cost and the tokens of each day, person, agent, and model." },
  { href: "/admin/audit", title: "Audit log", text: "Who changed what in the org, and when." },
  { href: "/admin/changes", title: "Agent changes", text: "The history of the soul, the settings, and the tools of each agent." },
  { href: "/admin/curator", title: "Curator reports", text: "The weekly reports about stale skills and full memory." },
  { href: "/admin/keys", title: "Org keys and region", text: "Use your own Anthropic and OpenAI keys. See the region of the org." },
  { href: "/admin/api-keys", title: "API keys", text: "Connect other agents and apps to sama with the OpenAI API or MCP." },
  { href: "/admin/data", title: "Data controls", text: "Retention, the export of the org, and the delete of the org." },
  { href: "/help/admin", title: "Admin help", text: "How each admin page works." },
] as const;

export function AdminOverview() {
  const org = useAdminOrg();
  const api = useApi();
  const load = useCallback(() => api.orgUsage(org.id, 7), [api, org.id]);
  const usage = useLoad(load, "Could not load the usage.");

  return (
    <>
      <section aria-labelledby="admin-week" className={styles.card}>
        <div className={styles.cardHead}>
          <h2 id="admin-week" className={styles.heading}>
            Last 7 days
          </h2>
          <span className="badge">Plan: {org.plan}</span>
        </div>
        {usage.error ? (
          <p className={styles.error}>
            {usage.error}{" "}
            <button type="button" className="btn btn-small" onClick={usage.reload}>
              Try again
            </button>
          </p>
        ) : (
          <dl className={own.stats} aria-busy={usage.data === null}>
            <Stat label="Model cost" value={usage.data ? formatCost(usage.data.totals.cost_usd) : null} />
            <Stat label="Sessions with activity" value={usage.data ? String(usage.data.totals.sessions) : null} />
            <Stat label="Runs" value={usage.data ? String(usage.data.totals.runs) : null} />
            <Stat label="Agents" value={String(org.agent_profiles.length)} />
          </dl>
        )}
      </section>
      <ul className={own.grid}>
        {TILES.map((tile) => (
          <li key={tile.href}>
            <Link href={tile.href} className={own.tile}>
              <span className={own.tileTitle}>{tile.title}</span>
              <span className={own.tileText}>{tile.text}</span>
            </Link>
          </li>
        ))}
      </ul>
    </>
  );
}

export function Stat({ label, value }: { label: string; value: string | null }) {
  return (
    <div className={own.stat}>
      <dt>{label}</dt>
      <dd>{value ?? <span className={`skeleton ${own.statSkeleton}`} />}</dd>
    </div>
  );
}
