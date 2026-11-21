"use client";

// The frame of the settings console (docs/architecture.md, Section 17.7, M7.10): a left
// navigation of the sections and the section on the right. On a narrow screen the navigation is
// a row of tabs above the section. The console works on the org of the workspace that the
// sidebar chose.

import Link from "next/link";
import { createContext, Fragment, useContext } from "react";
import type { Org } from "@/lib/api";
import { type ConsoleSection, groupedSections } from "@/lib/console";
import { useData } from "../DataProvider";
import { ErrorState, LoadingState } from "../ui";
import styles from "./Console.module.css";

interface ConsoleContextValue {
  org: Org;
  /** The workspace that the sidebar chose. It is a workspace of `org`. */
  workspaceId: string;
}

const ConsoleContext = createContext<ConsoleContextValue | null>(null);

/** The org and the workspace of the console. Only a section inside `ConsoleFrame` can use it. */
export function useConsole(): ConsoleContextValue {
  const value = useContext(ConsoleContext);
  if (value === null) throw new Error("useConsole needs a ConsoleFrame.");
  return value;
}

export function ConsoleFrame({ section, children }: { section: ConsoleSection | null; children: React.ReactNode }) {
  const { me, meError, reloadMe, workspaceId, orgOfWorkspace } = useData();
  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the console" />;
  const org = orgOfWorkspace(workspaceId);

  return (
    <div className={styles.console}>
      <nav className={styles.nav} aria-label="Console sections">
        <p className={styles.brand}>Console</p>
        {groupedSections().map(({ group, sections }) => (
          <div key={group} className={styles.group}>
            <h2 className={styles.groupTitle}>{group}</h2>
            <ul className={styles.links}>
              {sections.map((s) => (
                <li key={s.id}>
                  <Link
                    href={`/console/${s.id}`}
                    className={styles.link}
                    aria-current={section?.id === s.id ? "page" : undefined}
                    title={s.description}
                  >
                    {s.title}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>
      <main className={styles.main}>
        {section === null ? (
          <div className="state">
            <h2>No such section</h2>
            <p>The console has no section with this name.</p>
            <Link href="/console/setup" className="btn">
              Open Setup
            </Link>
          </div>
        ) : org === null || workspaceId === null ? (
          <div className="state">
            <h2>No workspace</h2>
            <p>You are not in a workspace yet. Ask an admin of your org to add you.</p>
          </div>
        ) : !org.region_here ? (
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
        ) : (
          <ConsoleContext.Provider value={{ org, workspaceId }}>
            {/* A new org or workspace mounts the section again, so no draft or secret stays. */}
            <Fragment key={`${org.id}:${workspaceId}`}>{children}</Fragment>
          </ConsoleContext.Provider>
        )}
      </main>
    </div>
  );
}
