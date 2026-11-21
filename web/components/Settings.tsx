"use client";

// The settings page (docs/architecture.md, Sections 11.4, 14.1, 17.4, and 17.6). It shows the
// account, the chat apps of each org, the browser settings of each org, the chat accounts that
// the user linked, the agents, and the data of the user in each org. An org admin connects or
// disconnects the chat apps and creates agents here. The model keys and the region are in the
// admin area (Org keys and region).

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import {
  type AgentProfileSummary,
  type ChannelIdentity,
  type ChatChannel,
  errorText,
  type Org,
  type PairingCode,
} from "@/lib/api";
import { CHANNEL_TITLES, pairingHint } from "@/lib/channels";
import { ROLE_LABELS, roleBadge } from "@/lib/orgs";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { BrowserSettings } from "./BrowserSettings";
import { ChatApps } from "./ChatApps";
import { MyData } from "./DataControls";
import { useData } from "./DataProvider";
import { NewAgentForm } from "./NewAgentForm";
import { PageHeader } from "./PageHeader";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

/** The channels where a user links an account with a code from this page. */
const PAIRING_CHANNELS: ChatChannel[] = ["telegram", "whatsapp", "email"];

export function Settings() {
  const { me, meError, reloadMe, workspaceId, profilesFor, orgOfWorkspace } = useData();
  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the settings" />;
  const org = orgOfWorkspace(workspaceId);
  const profiles = profilesFor(workspaceId);

  return (
    <div className={styles.page}>
      <PageHeader title="Settings" meta="Your account, the chat apps, the agents, and your data." />

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
                  {o.admin && o.region_here ? (
                    <>
                      {" · "}
                      <Link href="/admin">Admin</Link>
                    </>
                  ) : null}
                  {!o.region_here ? (
                    <>
                      {` · Region ${o.region}, served by another deployment`}
                      {o.region_url ? (
                        <>
                          {" · "}
                          <a href={o.region_url} rel="noopener noreferrer">
                            Open the org there
                          </a>
                        </>
                      ) : null}
                    </>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {me.orgs
        .filter((o) => o.region_here)
        .map((o) => (
          <ChatApps key={o.id} org={o} onChanged={reloadMe} />
        ))}

      {me.orgs.map((o) => (
        <BrowserSettings key={o.id} org={o} />
      ))}

      <LinkedAccounts orgs={me.orgs} workspaceId={workspaceId} org={org} />

      <Agents org={org} workspaceId={workspaceId} profiles={profiles} />

      {me.orgs
        .filter((o) => o.region_here)
        .map((o) => (
          <MyData key={o.id} org={o} />
        ))}
    </div>
  );
}

function Agents({
  org,
  workspaceId,
  profiles,
}: {
  org: Org | null;
  workspaceId: string | null;
  profiles: AgentProfileSummary[];
}) {
  const router = useRouter();
  const { reloadMe } = useData();
  const [creating, setCreating] = useState(false);
  return (
    <section className={`card ${styles.card}`} aria-labelledby="settings-agents">
      <div className={styles.cardHead}>
        <h2 id="settings-agents" className={styles.heading}>
          Agents
        </h2>
        {org?.admin && !creating ? (
          <button type="button" className="btn btn-small" onClick={() => setCreating(true)}>
            New agent
          </button>
        ) : null}
      </div>
      {org?.admin && creating ? (
        <NewAgentForm
          org={org}
          workspaceId={workspaceId}
          onCreated={(profile) => {
            reloadMe();
            router.push(`/agents/${profile.id}`);
          }}
          onCancel={() => setCreating(false)}
        />
      ) : null}
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
  );
}

type PairTarget = { workspaceId: string; profileId: string; channel: ChatChannel };
type PairResult = { code: PairingCode | null; error: string | null };

function pairKey(t: PairTarget): string {
  return `${t.channel}:${t.workspaceId}:${t.profileId}`;
}

function samePair(a: PairTarget, b: PairTarget | null): boolean {
  return b !== null && a.workspaceId === b.workspaceId && a.profileId === b.profileId && a.channel === b.channel;
}

/** The chat accounts that the user linked, and the code to link a Telegram account. */
export function LinkedAccounts({
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
    const channels: ChatChannel[] = ["telegram", "slack", "whatsapp", "email"];
    const lists = await Promise.all(channels.map((c) => api.listIdentities(c)));
    return channels.flatMap((channel, index) => (lists[index] ?? []).map((i) => ({ ...i, channel })));
  }, [api]);
  const identities = useLoad(load, "Could not load the linked chat accounts.");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A code belongs to one channel, one workspace, and one agent. It shows only for that target. An
  // answer that comes after the target changed is kept under its own target and does not show for
  // the new one.
  const [pairing, setPairing] = useState<PairTarget | null>(null);
  const [pairResults, setPairResults] = useState<Record<string, PairResult>>({});
  const profiles = profilesFor(workspaceId);
  const [profileId, setProfileId] = useState<string | null>(null);
  const agent = profiles.find((p) => p.id === profileId) ?? profiles[0] ?? null;
  const active = PAIRING_CHANNELS.filter(
    (c) => org?.integrations.some((i) => i.kind === c && i.status === "active") ?? false,
  );
  const [chosen, setChosen] = useState<ChatChannel | null>(null);
  const channel = chosen !== null && active.includes(chosen) ? chosen : (active[0] ?? null);
  const target =
    workspaceId !== null && agent !== null && channel !== null
      ? { workspaceId, profileId: agent.id, channel }
      : null;
  const pairingHere = pairing !== null && samePair(pairing, target);
  const result = target === null ? null : (pairResults[pairKey(target)] ?? null);
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
      const code = await api.createPairingCode(started.channel, started.workspaceId, started.profileId);
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
      {channel !== null && agent !== null ? (
        <div className={styles.section}>
          <h3 className={styles.subheading}>Link a {CHANNEL_TITLES[channel]} account</h3>
          {active.length > 1 ? (
            <label className="field">
              <span className="label">Channel</span>
              <select className="input" value={channel} onChange={(e) => setChosen(e.target.value as ChatChannel)}>
                {active.map((c) => (
                  <option key={c} value={c}>
                    {CHANNEL_TITLES[c]}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
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
                      {pairingHint(channel).open}
                    </a>{" "}
                    or send{" "}
                  </>
                ) : (
                  "Send "
                )}
                <code className="mono">{result.code.command}</code> to {pairingHint(channel).to}. The code stops working{" "}
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
