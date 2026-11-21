"use client";

// The settings page (docs/architecture.md, Sections 11.4 and 17.3). It shows the account, the
// chat apps of each org, the model keys and the region of each org, the browser settings of each
// org (Section 14.1), the chat accounts that the user linked, and the agents. An org admin connects or disconnects the Telegram bot and sets
// the model keys of the org here. A bot token or a model key goes only into the form of this
// page.

import Link from "next/link";
import { useCallback, useState } from "react";
import {
  type ChannelIdentity,
  type ChatChannel,
  errorText,
  type Org,
  type IntegrationSummary,
  type ModelKeyStatus,
  type ModelProvider,
  type PairingCode,
} from "@/lib/api";
import { integrationTone, toneBadge } from "@/lib/history";
import { keyProblem, keyUse, MAX_KEY_CHARS, PROVIDER_TITLES, regionText } from "@/lib/orgkeys";
import { ROLE_LABELS, roleBadge } from "@/lib/orgs";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { BrowserSettings } from "./BrowserSettings";
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
          <p className={styles.meta}>
            You are not in an org yet. <Link href="/">Join or create an org</Link>.
          </p>
        ) : (
          <ul className={styles.list}>
            {me.orgs.map((o) => (
              <li key={o.id} className={styles.row}>
                <div className={styles.name}>
                  <span>{o.name}</span>
                  <span className={roleBadge(o.role)}>{ROLE_LABELS[o.role]}</span>
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

      {me.orgs.map((o) => (
        <OrgKeys key={o.id} org={o} />
      ))}

      {me.orgs.map((o) => (
        <BrowserSettings key={o.id} org={o} />
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
  return toneBadge(integrationTone(status));
}

/** The text of a Telegram connect: a success only when the bot is active. */
function connectResult(view: IntegrationSummary): string | { failure: string } {
  if (view.status === "active") return `The Telegram bot${view.name ? ` @${view.name}` : ""} is connected.`;
  const why = view.last_error ? ` Telegram gave this error: ${view.last_error}` : "";
  return { failure: `The Telegram bot is not active.${why} Check the token and connect again.` };
}

function ChatApps({ org, onChanged }: { org: Org; onChanged: () => void }) {
  const api = useApi();
  const telegram = org.integrations.find((i) => i.kind === "telegram");
  const slack = org.integrations.find((i) => i.kind === "slack");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // An action gives the text of its success, or `{ failure }` when the API said yes but the
  // change did not work.
  const act = async (label: string, action: () => Promise<string | { failure: string }>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      const done = await action();
      if (typeof done === "string") setNotice(done);
      else setError(done.failure);
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
      return connectResult(view);
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
        {telegram && telegram.status !== "active" ? (
          <p className={styles.error}>
            {telegram.last_error
              ? `The bot is not active. Telegram gave this error: ${telegram.last_error}`
              : `The bot is not active. Its status is ${telegram.status}.`}
            {org.admin ? " Connect it again with the token of the bot." : " An org admin must connect it again."}
          </p>
        ) : null}
        {org.admin && telegram?.status !== "active" ? (
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
                {busy === "connect" ? "Connecting" : telegram ? "Connect again" : "Connect"}
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

function OrgKeys({ org }: { org: Org }) {
  const api = useApi();
  const load = useCallback(() => api.modelKeys(org.id), [api, org.id]);
  // Only an admin of an org that this deployment serves can read the keys.
  const keys = useLoad(org.admin && org.region_here ? load : null, "Could not load the model keys.");
  const [drafts, setDrafts] = useState<Record<ModelProvider, string>>({ anthropic: "", openai: "" });
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [now] = useState(() => Date.now());

  const act = async (label: string, action: () => Promise<ModelKeyStatus[]>, done: string) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      keys.setData(await action());
      setNotice(done);
    } catch (e) {
      setError(errorText(e, "The change failed."));
    } finally {
      setBusy(null);
    }
  };

  const save = (event: React.FormEvent, provider: ModelProvider) => {
    event.preventDefault();
    const problem = keyProblem(provider, drafts[provider]);
    if (problem !== null) {
      setNotice(null);
      setError(problem);
      return;
    }
    const value = drafts[provider].trim();
    // The secret leaves the page state before the call.
    setDrafts((all) => ({ ...all, [provider]: "" }));
    void act(
      `set-${provider}`,
      async () => {
        const status = await api.setModelKey(org.id, provider, value);
        return (keys.data ?? []).map((row) => (row.provider === provider ? status : row));
      },
      `The ${PROVIDER_TITLES[provider]} key of the org is set. The agents use it from now on.`,
    );
  };

  const remove = (provider: ModelProvider) => {
    const title = PROVIDER_TITLES[provider];
    if (!window.confirm(`Remove the ${title} key of the org? The agents then use the key of sama, if there is one.`))
      return;
    void act(
      `remove-${provider}`,
      async () => {
        await api.removeModelKey(org.id, provider);
        return await api.modelKeys(org.id);
      },
      `The ${title} key of the org is removed.`,
    );
  };

  return (
    <section className={`card ${styles.card}`} aria-label={`Org keys and region of ${org.name}`}>
      <div className={styles.cardHead}>
        <h2 className={styles.heading}>Org keys and region</h2>
        <span className={styles.meta}>{org.name}</span>
      </div>
      <div className={styles.row}>
        <div className={styles.name}>
          <span>Region</span>
          <span className="badge mono">{org.region}</span>
        </div>
      </div>
      <p className={styles.text}>
        {regionText(org)}{" "}
        {org.region_here ? (
          "An operator moves an org to another region."
        ) : org.region_url ? (
          <a href={org.region_url} rel="noopener noreferrer">
            Open the org there
          </a>
        ) : (
          "Ask the operator for its address."
        )}
      </p>
      {!org.region_here ? null : !org.admin ? (
        <p className={styles.meta}>An org admin manages the model keys of the org.</p>
      ) : (
        <>
          <p className={styles.text}>
            The agents use the key of the org for a provider when the org has one. Else they use the key of sama.
            The cost shows the same way for both keys.
          </p>
          {notice ? (
            <p className="alert alert-success" role="status">
              {notice}
            </p>
          ) : null}
          {keys.error ? (
            <ErrorState title="The model keys did not load" message={keys.error} onRetry={keys.reload} />
          ) : keys.data === null ? (
            <LoadingState label="Loading the model keys" />
          ) : (
            keys.data.map((status) => {
              const title = PROVIDER_TITLES[status.provider];
              const use = keyUse(status);
              return (
                <div key={status.provider} className={styles.section}>
                  <div className={styles.row}>
                    <div className={styles.name}>
                      <span>{title}</span>
                      <span className={status.org_key ? "badge badge-accent" : "badge"}>{use.badge}</span>
                      <span className={styles.meta}>
                        {use.text}
                        {status.org_key && status.updated_at ? ` Set ${relativeTime(status.updated_at, now)}.` : ""}
                      </span>
                    </div>
                    {status.org_key ? (
                      <button
                        type="button"
                        className="btn btn-danger btn-small"
                        disabled={busy !== null}
                        onClick={() => remove(status.provider)}
                      >
                        {busy === `remove-${status.provider}` ? "Removing" : "Remove"}
                      </button>
                    ) : null}
                  </div>
                  <form
                    className={styles.form}
                    onSubmit={(event) => save(event, status.provider)}
                    aria-label={`Set the ${title} key of the org`}
                  >
                    <label className="field">
                      <span className="label">{status.org_key ? `New ${title} key` : `${title} key`}</span>
                      <input
                        className="input"
                        type="password"
                        autoComplete="off"
                        value={drafts[status.provider]}
                        maxLength={MAX_KEY_CHARS}
                        onChange={(e) => {
                          const value = e.target.value;
                          setDrafts((all) => ({ ...all, [status.provider]: value }));
                        }}
                      />
                      <span className="hint">
                        Sama checks the key with {title} before it keeps it. Never paste a key in a chat.
                      </span>
                    </label>
                    <div className={styles.actions}>
                      <button type="submit" className="btn btn-primary btn-small" disabled={busy !== null}>
                        {busy === `set-${status.provider}` ? "Checking" : status.org_key ? "Replace" : "Set"}
                      </button>
                    </div>
                  </form>
                </div>
              );
            })
          )}
          {error ? (
            <p className="alert alert-error" role="alert">
              {error}
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}

type PairTarget = { workspaceId: string; profileId: string };
type PairResult = { code: PairingCode | null; error: string | null };

function pairKey(t: PairTarget): string {
  return `${t.workspaceId}:${t.profileId}`;
}

function samePair(a: PairTarget, b: PairTarget | null): boolean {
  return b !== null && a.workspaceId === b.workspaceId && a.profileId === b.profileId;
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
  // A code belongs to one workspace and one agent. It shows only for that pair. An answer that
  // comes after the pair changed is kept under its own pair and does not show for the new pair.
  const [pairing, setPairing] = useState<PairTarget | null>(null);
  const [pairResults, setPairResults] = useState<Record<string, PairResult>>({});
  const profiles = profilesFor(workspaceId);
  const [profileId, setProfileId] = useState<string | null>(null);
  const agent = profiles.find((p) => p.id === profileId) ?? profiles[0] ?? null;
  const target = workspaceId !== null && agent !== null ? { workspaceId, profileId: agent.id } : null;
  const pairingHere = pairing !== null && samePair(pairing, target);
  const result = target === null ? null : (pairResults[pairKey(target)] ?? null);
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
    if (target === null) return;
    const started = { ...target };
    setPairing(started);
    const key = pairKey(started);
    const keep = (result: PairResult) => setPairResults((all) => ({ ...all, [key]: result }));
    setPairResults((all) => {
      const next = { ...all };
      delete next[key];
      return next;
    });
    try {
      const code = await api.createPairingCode("telegram", started.workspaceId, started.profileId);
      keep({ code, error: null });
    } catch (e) {
      keep({ code: null, error: errorText(e, "No code was made.") });
    } finally {
      setPairing((p) => (p === started ? null : p));
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
            <button type="button" className="btn btn-small" disabled={pairingHere} onClick={() => void pair()}>
              {pairingHere ? "Making a code" : "Get a link code"}
            </button>
          </div>
          {result?.error ? (
            <p className="alert alert-error" role="alert">
              {result.error}
            </p>
          ) : null}
          {result?.code ? (
            <div className="alert" role="status">
              <p>
                {result.code.link ? (
                  <>
                    <a href={result.code.link} target="_blank" rel="noopener noreferrer">
                      Open the bot in Telegram
                    </a>{" "}
                    or send{" "}
                  </>
                ) : (
                  "Send "
                )}
                <code className="mono">{result.code.command}</code> to the bot. The code stops working{" "}
                {new Date(result.code.expires_at).toLocaleTimeString()}.
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
