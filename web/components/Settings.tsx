"use client";

// The settings page (docs/architecture.md, Section 11.4). It shows the account, the chat apps
// of each org, the chat accounts that the user linked, and the agents. An org admin connects or
// disconnects the Telegram bot here. The bot token goes only into the form of this page.

import Link from "next/link";
import { useCallback, useState } from "react";
import {
  type ChannelIdentity,
  type ChatChannel,
  errorText,
  type Org,
  type PairingCode,
} from "@/lib/api";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { PageHeader } from "./PageHeader";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

const CHANNEL_TITLES: Record<ChatChannel, string> = { telegram: "Telegram", slack: "Slack" };

export function Settings() {
  const { me, meError, reloadMe, workspaceId, profilesFor, orgOfWorkspace } = useData();
  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the settings" />;
  const org = orgOfWorkspace(workspaceId);
  const profiles = profilesFor(workspaceId);

  return (
    <div className={styles.page}>
      <PageHeader title="Settings" meta="Your account, the chat apps, and the agents." />

      <section className={`card ${styles.card}`} aria-labelledby="settings-account">
        <h2 id="settings-account" className={styles.heading}>
          Account
        </h2>
        {me.orgs.length === 0 ? (
          <p className={styles.meta}>You are not in an org yet. Ask an admin of your org to add you.</p>
        ) : (
          <ul className={styles.list}>
            {me.orgs.map((o) => (
              <li key={o.id} className={styles.row}>
                <div className={styles.name}>
                  <span>{o.name}</span>
                  <span className={o.admin ? "badge badge-accent" : "badge"}>{o.admin ? "Admin" : "Member"}</span>
                </div>
                <span className={styles.meta}>
                  {o.display_name ? `${o.display_name} · ` : ""}
                  {o.workspaces.length === 1 ? "1 workspace" : `${o.workspaces.length} workspaces`}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {me.orgs.map((o) => (
        <ChatApps key={o.id} org={o} onChanged={reloadMe} />
      ))}

      <LinkedAccounts orgs={me.orgs} workspaceId={workspaceId} org={org} />

      <section className={`card ${styles.card}`} aria-labelledby="settings-agents">
        <h2 id="settings-agents" className={styles.heading}>
          Agents
        </h2>
        {profiles.length === 0 ? (
          <p className={styles.meta}>This workspace has no agents.</p>
        ) : (
          <ul className={styles.list}>
            {profiles.map((p) => (
              <li key={p.id} className={styles.row}>
                <Link href={`/agents/${p.id}`}>{p.name}</Link>
                <span className={styles.meta}>
                  {p.model} · {p.workspace_id === null ? "Whole org" : "This workspace"}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className={styles.meta}>
          Change the tools on the <Link href="/connections">Connections</Link> page, the notes on
          the <Link href="/memory">Memory</Link> page, and the guides on the{" "}
          <Link href="/skills">Skills</Link> page.
        </p>
      </section>
    </div>
  );
}

function statusBadge(status: string | undefined): string {
  if (status === "active") return "badge badge-accent";
  if (status === undefined) return "badge";
  return `badge ${styles.badgeError}`;
}

function ChatApps({ org, onChanged }: { org: Org; onChanged: () => void }) {
  const api = useApi();
  const telegram = org.integrations.find((i) => i.kind === "telegram");
  const slack = org.integrations.find((i) => i.kind === "slack");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const act = async (label: string, action: () => Promise<string>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      setNotice(await action());
      onChanged();
    } catch (e) {
      setError(errorText(e, "The change failed."));
    } finally {
      setBusy(null);
    }
  };

  const connect = (event: React.FormEvent) => {
    event.preventDefault();
    const value = token.trim();
    if (value === "") {
      setError("Give the token of the bot.");
      return;
    }
    void act("connect", async () => {
      const view = await api.connectTelegram(org.id, value);
      // The secret leaves the page state as soon as the API has it.
      setToken("");
      return `The Telegram bot${view.name ? ` @${view.name}` : ""} is connected.`;
    });
  };

  return (
    <section className={`card ${styles.card}`} aria-label={`Chat apps of ${org.name}`}>
      <div className={styles.cardHead}>
        <h2 className={styles.heading}>Chat apps</h2>
        <span className={styles.meta}>{org.name}</span>
      </div>
      <p className={styles.text}>
        People talk to the agents of the org in these apps. Each org has its own bot.
      </p>
      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      <div className={styles.section}>
        <div className={styles.row}>
          <div className={styles.name}>
            <span>Telegram</span>
            <span className={statusBadge(telegram?.status)}>{telegram ? telegram.status : "Not connected"}</span>
            {telegram?.name ? <span className={styles.meta}>@{telegram.name}</span> : null}
          </div>
          {org.admin && telegram ? (
            <button
              type="button"
              className="btn btn-danger btn-small"
              disabled={busy !== null}
              onClick={() => {
                if (!window.confirm("Disconnect the Telegram bot? People can no longer talk to the agents there.")) return;
                void act("disconnect", async () => {
                  await api.disconnectIntegration(org.id, "telegram");
                  return "The Telegram bot is disconnected.";
                });
              }}
            >
              {busy === "disconnect" ? "Disconnecting" : "Disconnect"}
            </button>
          ) : null}
        </div>
        {org.admin && !telegram ? (
          <form className={styles.form} onSubmit={connect} aria-label="Connect a Telegram bot">
            <label className="field">
              <span className="label">Bot token</span>
              <input
                className="input"
                type="password"
                autoComplete="off"
                value={token}
                maxLength={200}
                onChange={(e) => setToken(e.target.value)}
              />
              <span className="hint">Make a bot with @BotFather in Telegram and paste its token here. Never paste it in a chat.</span>
            </label>
            <div className={styles.actions}>
              <button type="submit" className="btn btn-primary btn-small" disabled={busy !== null}>
                {busy === "connect" ? "Connecting" : "Connect"}
              </button>
            </div>
          </form>
        ) : null}
        {!org.admin && !telegram ? <p className={styles.meta}>An org admin connects the Telegram bot.</p> : null}
      </div>
      <div className={styles.section}>
        <div className={styles.row}>
          <div className={styles.name}>
            <span>Slack</span>
            <span className={statusBadge(slack?.status)}>{slack ? slack.status : "Not connected"}</span>
            {slack?.name ? <span className={styles.meta}>{slack.name}</span> : null}
          </div>
        </div>
      </div>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}

function LinkedAccounts({
  orgs,
  workspaceId,
  org,
}: {
  orgs: Org[];
  workspaceId: string | null;
  org: Org | null;
}) {
  const api = useApi();
  const { profilesFor } = useData();
  const load = useCallback(async () => {
    const [telegram, slack] = await Promise.all([api.listIdentities("telegram"), api.listIdentities("slack")]);
    return [
      ...telegram.map((i) => ({ ...i, channel: "telegram" as const })),
      ...slack.map((i) => ({ ...i, channel: "slack" as const })),
    ];
  }, [api]);
  const identities = useLoad(load, "Could not load the linked chat accounts.");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState<PairingCode | null>(null);
  const profiles = profilesFor(workspaceId);
  const [profileId, setProfileId] = useState<string | null>(null);
  const agent = profiles.find((p) => p.id === profileId) ?? profiles[0] ?? null;
  const telegramOn = org?.integrations.some((i) => i.kind === "telegram" && i.status === "active") ?? false;
  const [now] = useState(() => Date.now());

  const where = (identity: ChannelIdentity) => {
    const o = orgs.find((x) => x.id === identity.org_id);
    const workspace = o?.workspaces.find((w) => w.id === identity.workspace_id);
    const profile = o?.agent_profiles.find((p) => p.id === identity.agent_profile_id);
    return [workspace?.name, profile?.name].filter(Boolean).join(" · ");
  };

  const unlink = async (channel: ChatChannel, id: string) => {
    if (!window.confirm(`Unlink this ${CHANNEL_TITLES[channel]} account? Its messages no longer reach the agents.`)) return;
    setBusy(id);
    setError(null);
    try {
      await api.unlinkIdentity(channel, id);
      identities.setData((rows) => rows && rows.filter((r) => r.id !== id));
    } catch (e) {
      setError(errorText(e, "The account was not unlinked."));
    } finally {
      setBusy(null);
    }
  };

  const pair = async () => {
    if (workspaceId === null || agent === null) return;
    setBusy("pair");
    setError(null);
    try {
      setCode(await api.createPairingCode("telegram", workspaceId, agent.id));
    } catch (e) {
      setError(errorText(e, "No code was made."));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className={`card ${styles.card}`} aria-labelledby="settings-linked">
      <h2 id="settings-linked" className={styles.heading}>
        Linked chat accounts
      </h2>
      <p className={styles.text}>A linked account talks to one agent of one workspace.</p>
      {identities.error ? (
        <ErrorState title="The accounts did not load" message={identities.error} onRetry={identities.reload} />
      ) : identities.data === null ? (
        <LoadingState label="Loading the linked accounts" />
      ) : identities.data.length === 0 ? (
        <p className={styles.meta}>No linked chat accounts.</p>
      ) : (
        <ul className={styles.list}>
          {identities.data.map((identity) => (
            <li key={identity.id} className={styles.row}>
              <div className={styles.name}>
                <span>{CHANNEL_TITLES[identity.channel]}</span>
                <span className={styles.meta}>
                  {where(identity)} · linked {relativeTime(identity.created_at, now)}
                </span>
              </div>
              <button
                type="button"
                className="btn btn-quiet btn-small"
                disabled={busy !== null}
                onClick={() => void unlink(identity.channel, identity.id)}
              >
                {busy === identity.id ? "Unlinking" : "Unlink"}
              </button>
            </li>
          ))}
        </ul>
      )}
      {telegramOn && agent !== null ? (
        <div className={styles.section}>
          <h3 className={styles.subheading}>Link a Telegram account</h3>
          {profiles.length > 1 ? (
            <label className="field">
              <span className="label">Agent</span>
              <select className="input" value={agent.id} onChange={(e) => setProfileId(e.target.value)}>
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <div className={styles.actions}>
            <button type="button" className="btn btn-small" disabled={busy !== null} onClick={() => void pair()}>
              {busy === "pair" ? "Making a code" : "Get a link code"}
            </button>
          </div>
          {code ? (
            <div className="alert" role="status">
              <p>
                {code.link ? (
                  <>
                    <a href={code.link} target="_blank" rel="noopener noreferrer">
                      Open the bot in Telegram
                    </a>{" "}
                    or send{" "}
                  </>
                ) : (
                  "Send "
                )}
                <code className="mono">{code.command}</code> to the bot. The code stops working{" "}
                {new Date(code.expires_at).toLocaleTimeString()}.
              </p>
            </div>
          ) : null}
        </div>
      ) : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}
