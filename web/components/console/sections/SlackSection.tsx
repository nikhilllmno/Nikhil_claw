"use client";

// The Slack section: the chat apps of the org (Slack, Telegram, WhatsApp with its provider, and
// email; M7.8), a test of each active Slack or Telegram app, and the chat accounts that the user
// linked.

import { useState } from "react";
import { errorText, type ChatChannel, type IntegrationSummary, type Org } from "@/lib/api";
import { activeChatApps, CHAT_APP_TITLES, type CheckNotice, checkNotice } from "@/lib/chatAppCheck";
import { integrationTone, toneBadge } from "@/lib/history";
import { can } from "@/lib/console";
import { useApi } from "../../AuthProvider";
import { ChatApps } from "../../ChatApps";
import { useData } from "../../DataProvider";
import { LinkedAccounts } from "../../Settings";
import { useConsole } from "../Console";
import { SectionHeader } from "../parts";
import styles from "../../Page.module.css";

export function SlackSection() {
  const { org } = useConsole();
  const { me, reloadMe, workspaceId } = useData();
  const manage = org.admin && can(org, "chat_apps.manage");

  return (
    <>
      <SectionHeader
        id="slack"
        meta={manage ? undefined : "An org admin with the right to manage chat apps connects and disconnects them."}
      />
      {manage ? <ChatApps org={org} onChanged={reloadMe} /> : <ChatAppStatus org={org} />}
      {manage ? <ConnectionTest org={org} onChecked={reloadMe} /> : null}
      <LinkedAccounts orgs={me?.orgs ?? [org]} workspaceId={workspaceId} org={org} />
    </>
  );
}

/** The state of the chat apps for a user who cannot change them. */
function ChatAppStatus({ org }: { org: Org }) {
  const apps: { kind: ChatChannel; summary: IntegrationSummary | undefined }[] = (["slack", "telegram", "whatsapp", "email"] as const).map(
    (kind) => ({ kind, summary: org.integrations.find((i) => i.kind === kind) }),
  );
  return (
    <section className={`card ${styles.card}`} aria-label={`Chat apps of ${org.name}`}>
      <div className={styles.cardHead}>
        <h2 className={styles.heading}>Chat apps</h2>
        <span className={styles.meta}>{org.name}</span>
      </div>
      <ul className={styles.list}>
        {apps.map(({ kind, summary }) => (
          <li key={kind} className={styles.row}>
            <div className={styles.name}>
              <span>{CHAT_APP_TITLES[kind]}</span>
              <span className={toneBadge(integrationTone(summary?.status))}>
                {summary ? summary.status : "Not connected"}
              </span>
              {summary?.name ? <span className={styles.meta}>{summary.name}</span> : null}
            </div>
          </li>
        ))}
      </ul>
      <p className={styles.meta}>Only an org admin with the right to manage chat apps changes them.</p>
    </section>
  );
}

/** A test button for each active app. The test calls the app with the stored token. Only for a user who can manage chat apps. */
function ConnectionTest({ org, onChecked }: { org: Org; onChecked: () => void }) {
  const api = useApi();
  const apps = activeChatApps(org.integrations);
  const [busy, setBusy] = useState<ChatChannel | null>(null);
  const [results, setResults] = useState<Partial<Record<ChatChannel, CheckNotice | { error: string }>>>({});

  const test = async (kind: ChatChannel) => {
    setBusy(kind);
    setResults((r) => ({ ...r, [kind]: undefined }));
    try {
      const check = await api.checkIntegration(org.id, kind);
      setResults((r) => ({ ...r, [kind]: checkNotice(kind, check) }));
      onChecked();
    } catch (e) {
      setResults((r) => ({ ...r, [kind]: { error: errorText(e, "The test did not run.") } }));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className={`card ${styles.card}`} aria-labelledby="slack-test">
      <h2 id="slack-test" className={styles.heading}>
        Test the connection
      </h2>
      <p className={styles.text}>The test asks the chat app who it is, with the token that sama keeps for the org.</p>
      {apps.length === 0 ? (
        <p className={styles.meta}>No chat app is active. Connect one first.</p>
      ) : (
        <ul className={styles.list}>
          {apps.map((kind) => {
            const result = results[kind];
            return (
              <li key={kind} className={styles.section}>
                <div className={styles.row}>
                  <span className={styles.name}>{CHAT_APP_TITLES[kind]}</span>
                  <button
                    type="button"
                    className="btn btn-small"
                    disabled={busy !== null}
                    onClick={() => void test(kind)}
                  >
                    {busy === kind ? "Testing…" : "Test the connection"}
                  </button>
                </div>
                {result === undefined ? null : "error" in result ? (
                  <p className="alert alert-error" role="alert">
                    {result.error}
                  </p>
                ) : result.ok ? (
                  <p className="alert alert-success" role="status">
                    {result.text}
                  </p>
                ) : (
                  <p className="alert alert-error" role="alert">
                    {result.text}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
