"use client";

// The help pages (docs/architecture.md, Section 17.5, M6.9). They need no sign-in, so a person
// who cannot sign in can read them too. The text is in `lib/help.ts`.

import Link from "next/link";
import { useState } from "react";
import { HELP_TOPICS, type HelpTopic, searchHelp } from "@/lib/help";
import styles from "./Page.module.css";
import own from "./Admin.module.css";
import shell from "./Shell.module.css";

export function HelpFrame({ children }: { children: React.ReactNode }) {
  return (
    <div className={shell.helpShell}>
      <header className={shell.helpBar}>
        <Link href="/" className={shell.topbarBrand}>
          sama
        </Link>
        <nav aria-label="Help" className={styles.actions}>
          <Link href="/help" className="btn btn-quiet btn-small">
            Help
          </Link>
          <Link href="/" className="btn btn-small">
            Open the app
          </Link>
        </nav>
      </header>
      <main className={styles.page}>{children}</main>
    </div>
  );
}

export function HelpIndex() {
  const [text, setText] = useState("");
  const found = searchHelp(text);
  const groups = [
    { title: "Use sama", topics: found.filter((t) => t.audience === "everyone") },
    { title: "Manage your org", topics: found.filter((t) => t.audience === "admin") },
  ].filter((g) => g.topics.length > 0);

  return (
    <>
      <header className={styles.header}>
        <h1 className={styles.title}>Help</h1>
        <p className={styles.meta}>How to set up sama, use the agents, and manage your org.</p>
      </header>
      <label className="field">
        <span className="label">Search the help</span>
        <input
          className="input"
          type="search"
          value={text}
          placeholder="For example: invite, Slack, export"
          onChange={(e) => setText(e.target.value)}
        />
      </label>
      {groups.length === 0 ? (
        <div className="state">
          <h2>No help topic found</h2>
          <p>Try other words, or read Getting started.</p>
          <Link href="/help/getting-started" className="btn">
            Getting started
          </Link>
        </div>
      ) : (
        groups.map((group) => (
          <section key={group.title} className={styles.card} aria-label={group.title}>
            <h2 className={styles.heading}>{group.title}</h2>
            <ul className={own.grid}>
              {group.topics.map((topic) => (
                <li key={topic.id}>
                  <Link href={`/help/${topic.id}`} className={own.tile}>
                    <span className={own.tileTitle}>{topic.title}</span>
                    <span className={own.tileText}>{topic.summary}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        ))
      )}
    </>
  );
}

export function HelpArticle({ topic }: { topic: HelpTopic }) {
  const index = HELP_TOPICS.findIndex((t) => t.id === topic.id);
  const next = HELP_TOPICS[index + 1] ?? null;
  return (
    <article className={own.article}>
      <header className={styles.header}>
        <Link href="/help" className={styles.back}>
          All help topics
        </Link>
        <h1 className={styles.title}>{topic.title}</h1>
        <p className={styles.meta}>{topic.summary}</p>
      </header>
      {topic.sections.map((section) => (
        <section key={section.heading} className={`card ${styles.card}`}>
          <h2 className={styles.heading}>{section.heading}</h2>
          <div className={own.prose}>
            {section.paragraphs?.map((p) => <p key={p}>{p}</p>)}
            {section.steps ? (
              <ol>
                {section.steps.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ol>
            ) : null}
            {section.items ? (
              <ul>
                {section.items.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            ) : null}
          </div>
        </section>
      ))}
      <nav aria-label="Related pages" className={styles.actions}>
        {topic.links.map((link) => (
          <Link key={link.href} href={link.href} className="btn btn-small">
            {link.label}
          </Link>
        ))}
        {next ? (
          <Link href={`/help/${next.id}`} className="btn btn-quiet btn-small">
            Next: {next.title}
          </Link>
        ) : null}
      </nav>
    </article>
  );
}
