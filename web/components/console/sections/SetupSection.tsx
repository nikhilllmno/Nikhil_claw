"use client";

// The Setup section: the steps of the setup guide as a checklist. Each step has a status and a
// link to the part of the console, or the page, where the user does it. The progress is the one
// of the guide at /onboarding, kept in this browser for each org.

import Link from "next/link";
import { useState } from "react";
import { loadProgress, type Progress, saveProgress } from "@/lib/onboarding";
import { NEXT_STEPS, SETUP_STATUS_LABELS, type SetupStatus, setupChecklist, setupProgressText } from "@/lib/setup";
import { useConsole } from "../Console";
import { SectionHeader } from "../parts";
import styles from "../../Page.module.css";
import own from "../../Admin.module.css";

const STATUS_BADGE: Record<SetupStatus, string> = {
  done: "badge badge-accent",
  skipped: "badge",
  next: "badge badge-warning",
  todo: "badge",
};

export function SetupSection() {
  const { org } = useConsole();
  const [progress, setProgress] = useState<Progress>(() => loadProgress(window.localStorage, org.id));
  const items = setupChecklist(org, progress);

  const update = (change: Partial<Progress>) => {
    const value = { ...progress, ...change };
    saveProgress(window.localStorage, org.id, value);
    setProgress(value);
  };

  return (
    <>
      <SectionHeader id="setup" meta={`${org.name} · ${setupProgressText(items)}`} />
      <section className={`card ${styles.card}`} aria-labelledby="setup-steps">
        <div className={styles.cardHead}>
          <h2 id="setup-steps" className={styles.heading}>
            Steps
          </h2>
          <Link href="/onboarding" className="btn btn-quiet btn-small">
            Open the setup guide
          </Link>
        </div>
        <ol className={own.steps}>
          {items.map((item, index) => (
            <li
              key={item.id}
              className={`${own.step} ${item.done ? own.stepDone : ""}`}
              aria-current={item.status === "next" ? "step" : undefined}
            >
              <span className={own.stepMark} aria-hidden="true">
                {item.done ? "✓" : index + 1}
              </span>
              <div className={own.stepBody}>
                <div className={styles.row}>
                  <span className={own.stepTitle}>{item.title}</span>
                  <span className={STATUS_BADGE[item.status]}>{SETUP_STATUS_LABELS[item.status]}</span>
                </div>
                <span className="hint">{item.hint}</span>
                <div className={styles.actions}>
                  {item.href !== null ? (
                    <Link href={item.href} className={`btn btn-small ${item.status === "next" ? "btn-primary" : ""}`}>
                      {item.linkLabel}
                    </Link>
                  ) : null}
                  {item.id === "plan" && !item.done ? (
                    <button type="button" className="btn btn-quiet btn-small" onClick={() => update({ plan: "kept" })}>
                      {`Keep the ${org.plan} plan`}
                    </button>
                  ) : null}
                  {item.id === "channel" && !item.done ? (
                    <button
                      type="button"
                      className="btn btn-quiet btn-small"
                      onClick={() => update({ channel: "skipped" })}
                    >
                      Use only the web app
                    </button>
                  ) : null}
                </div>
              </div>
            </li>
          ))}
        </ol>
      </section>

      <section className={`card ${styles.card}`} aria-labelledby="setup-next">
        <h2 id="setup-next" className={styles.heading}>
          Next steps
        </h2>
        <ul className={styles.list}>
          {NEXT_STEPS.map((step) => (
            <li key={step.href} className={styles.row}>
              <div className={styles.name}>
                <span>{step.title}</span>
                <span className={styles.meta}>{step.text}</span>
              </div>
              <Link href={step.href} className="btn btn-small">
                {step.linkLabel}
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
