"use client";

// Small pieces that every section of the console uses: the head of a section, the choice of an
// agent, and the card of a part that another milestone adds.

import { useEffect, useRef } from "react";
import type { AgentProfileSummary } from "@/lib/api";
import { sectionById, type SectionId } from "@/lib/console";
import styles from "../Page.module.css";

/** The head of a section: the title, one line of help, and optional actions on the right. */
export function SectionHeader({
  id,
  actions,
  meta,
}: {
  id: SectionId;
  actions?: React.ReactNode;
  /** A line under the description, for example who can change the section. */
  meta?: React.ReactNode;
}) {
  const section = sectionById(id);
  const ref = useRef<HTMLHeadingElement>(null);
  // The focus goes to the title when the section opens, so a screen reader reads the new section.
  useEffect(() => {
    ref.current?.focus({ preventScroll: true });
  }, []);
  return (
    <header className={styles.header}>
      <div className={styles.row}>
        <h1 ref={ref} tabIndex={-1} className={styles.title}>
          {section?.title ?? id}
        </h1>
        {actions ? <div className={styles.actions}>{actions}</div> : null}
      </div>
      <p className={styles.meta}>{section?.description}</p>
      {meta ? <p className={styles.meta}>{meta}</p> : null}
    </header>
  );
}

/** A select of the agents of the org that the user sees. */
export function ProfilePicker({
  profiles,
  value,
  onChange,
}: {
  profiles: AgentProfileSummary[];
  value: string | null;
  onChange: (profileId: string) => void;
}) {
  return (
    <label className="field">
      <span className="label">Agent</span>
      <select className="input" value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        {profiles.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
    </label>
  );
}

/**
 * The card of a part of a section that another roadmap item adds. It says what comes and
 * where the user can do the work today.
 */
export function ComingCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className={`card ${styles.card}`} aria-label={title}>
      <div className={styles.cardHead}>
        <h2 className={styles.subheading}>{title}</h2>
        <span className="badge">Not available yet</span>
      </div>
      <p className={styles.text}>{children}</p>
    </section>
  );
}
