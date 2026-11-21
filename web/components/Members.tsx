"use client";

// The members page (docs/architecture.md, Section 17.2). Every member sees the people of the
// org. An owner or admin changes roles and workspaces, removes members, and makes invite links.
// sama does not send email: the admin copies the link and sends it.

import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import {
  type CreatedInvite,
  errorText,
  type InviteRole,
  type Org,
  type OrgInvite,
  type OrgMember,
  type OrgRole,
} from "@/lib/api";
import {
  canRemove,
  checkInviteEmail,
  INVITE_ROLES,
  inviteLink,
  isAdmin,
  ROLE_LABELS,
  roleBadge,
  replaceMember,
  roleChoices,
  toggleWorkspace,
  withoutMember,
} from "@/lib/orgs";
import { relativeTime, timeLeft } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { PageHeader } from "./PageHeader";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";
import own from "./Members.module.css";

export function Members() {
  const { me, meError, reloadMe, workspaceId, orgOfWorkspace } = useData();
  const [chosen, setChosen] = useState<string | null>(null);
  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the members" />;
  const org =
    me.orgs.find((o) => o.id === chosen) ?? orgOfWorkspace(workspaceId) ?? me.orgs[0] ?? null;

  return (
    <div className={styles.page}>
      <PageHeader title="Members" meta="The people of your org, their roles, and the invites." />
      {org === null ? (
        <p className={styles.meta}>You are not in an org yet.</p>
      ) : (
        <>
          {me.orgs.length > 1 ? (
            <label className="field">
              <span className="label">Org</span>
              <select className="input" value={org.id} onChange={(e) => setChosen(e.target.value)}>
                {me.orgs.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <OrgMembers key={org.id} org={org} />
        </>
      )}
    </div>
  );
}

function OrgMembers({ org }: { org: Org }) {
  const api = useApi();
  const loadMembers = useCallback(() => api.listMembers(org.id), [api, org.id]);
  const members = useLoad(loadMembers, "Could not load the members.");
  const admin = isAdmin(org.role);
  const loadInvites = useCallback(() => api.listInvites(org.id), [api, org.id]);
  const invites = useLoad(admin ? loadInvites : null, "Could not load the invites.");

  return (
    <>
      <section className={`card ${styles.card}`} aria-labelledby="members-list">
        <div className={styles.cardHead}>
          <h2 id="members-list" className={styles.heading}>
            {org.name}
          </h2>
          <span className={styles.meta}>
            {ROLE_LABELS[org.role]} · {org.plan} plan
          </span>
        </div>
        {members.error ? (
          <div className={styles.row}>
            <p className={styles.error}>{members.error}</p>
            <button type="button" className="btn btn-small" onClick={members.reload}>
              Try again
            </button>
          </div>
        ) : members.data === null ? (
          <p className={styles.meta}>Loading…</p>
        ) : (
          <ul className={styles.list}>
            {members.data.map((m) => (
              <MemberRow
                key={m.user_id}
                org={org}
                member={m}
                all={members.data ?? []}
                onUpdated={(updated) =>
                  members.setData((list) => (list ? replaceMember(list, updated) : list))
                }
                onRemoved={(userId) =>
                  members.setData((list) => (list ? withoutMember(list, userId) : list))
                }
              />
            ))}
          </ul>
        )}
      </section>

      {admin ? (
        <>
          <InviteForm
            org={org}
            onCreated={() => {
              invites.reload();
            }}
          />
          <section className={`card ${styles.card}`} aria-labelledby="members-invites">
            <h2 id="members-invites" className={styles.heading}>
              Open invites
            </h2>
            {invites.error ? (
              <div className={styles.row}>
                <p className={styles.error}>{invites.error}</p>
                <button type="button" className="btn btn-small" onClick={invites.reload}>
                  Try again
                </button>
              </div>
            ) : invites.data === null ? (
              <p className={styles.meta}>Loading…</p>
            ) : invites.data.length === 0 ? (
              <p className={styles.meta}>No open invites.</p>
            ) : (
              <ul className={styles.list}>
                {invites.data.map((invite) => (
                  <InviteRow key={invite.id} org={org} invite={invite} onChange={invites.reload} />
                ))}
              </ul>
            )}
          </section>
        </>
      ) : (
        <p className={styles.meta}>Only an owner or admin can invite people and change roles.</p>
      )}
    </>
  );
}

function MemberRow({
  org,
  member,
  all,
  onUpdated,
  onRemoved,
}: {
  org: Org;
  member: OrgMember;
  all: OrgMember[];
  onUpdated: (member: OrgMember) => void;
  onRemoved: (userId: string) => void;
}) {
  const api = useApi();
  const router = useRouter();
  const { reloadMe } = useData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now] = useState(() => Date.now());
  const self = member.user_id === org.user_id;
  const roles = roleChoices(org.role, member, all);
  const removable = canRemove(org.role, org.user_id, member, all);
  // An owner or admin sees every workspace. Only a member has a set of workspaces.
  const editSpaces = isAdmin(org.role) && member.role === "member";

  const run = async <T,>(action: () => Promise<T>, fallback: string, after: (result: T) => void) => {
    setBusy(true);
    setError(null);
    try {
      after(await action());
    } catch (e) {
      setError(errorText(e, fallback));
    } finally {
      setBusy(false);
    }
  };

  const changeRole = (role: OrgRole) =>
    run(
      () => api.changeMember(org.id, member.user_id, { role }),
      "Could not change the role.",
      (updated) => {
        onUpdated(updated);
        if (self) reloadMe();
      },
    );

  // Each change applies its own answer to the list (the PATCH returns the member). The page does
  // not load the full list again after a change, so an older list cannot overwrite a newer change.
  const toggleSpace = (id: string, on: boolean) => {
    void run(
      () =>
        api.changeMember(org.id, member.user_id, {
          workspace_ids: toggleWorkspace(member.workspace_ids, id, on),
        }),
      "Could not change the workspaces.",
      onUpdated,
    );
  };

  const remove = () => {
    const text = self
      ? `Leave ${org.name}? You lose access to its workspaces and sessions.`
      : `Remove ${member.display_name || member.email || "this member"} from ${org.name}?`;
    if (!window.confirm(text)) return;
    void run(
      () => api.removeMember(org.id, member.user_id),
      self ? "Could not leave the org." : "Could not remove the member.",
      () => {
        if (self) {
          reloadMe();
          router.push("/");
        } else {
          onRemoved(member.user_id);
        }
      },
    );
  };

  const name = member.display_name || member.email || "Unnamed member";
  return (
    <li className={own.member}>
      <div className={styles.row}>
        <div className={own.who}>
          <div className={styles.name}>
            <span>{name}</span>
            {self ? <span className="badge">You</span> : null}
            {roles.length === 0 ? <span className={roleBadge(member.role)}>{ROLE_LABELS[member.role]}</span> : null}
          </div>
          <span className={styles.meta}>
            {member.email && member.email !== name ? `${member.email} · ` : ""}
            {member.joined_at ? `joined ${relativeTime(member.joined_at, now)}` : ""}
          </span>
        </div>
        <div className={styles.actions}>
          {roles.length > 0 ? (
            <select
              className="input"
              aria-label={`Role of ${name}`}
              value={member.role}
              onChange={(e) => void changeRole(e.target.value as OrgRole)}
              disabled={busy}
            >
              {roles.map((r) => (
                <option key={r} value={r}>
                  {ROLE_LABELS[r]}
                </option>
              ))}
            </select>
          ) : null}
          {removable ? (
            <button type="button" className="btn btn-small btn-danger" onClick={remove} disabled={busy}>
              {self ? "Leave" : "Remove"}
            </button>
          ) : null}
        </div>
      </div>
      {editSpaces && org.workspaces.length > 0 ? (
        <fieldset className={own.spaces} disabled={busy}>
          <legend>Workspaces</legend>
          {org.workspaces.map((w) => (
            <label key={w.id} className={own.check}>
              <input
                type="checkbox"
                checked={member.workspace_ids.includes(w.id)}
                onChange={(e) => toggleSpace(w.id, e.target.checked)}
              />
              {w.name}
            </label>
          ))}
        </fieldset>
      ) : null}
      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}
    </li>
  );
}

function InviteForm({ org, onCreated }: { org: Org; onCreated: () => void }) {
  const api = useApi();
  const { workspaceId } = useData();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<InviteRole>("member");
  const inOrg = org.workspaces.some((w) => w.id === workspaceId);
  const [space, setSpace] = useState(inOrg && workspaceId ? workspaceId : (org.workspaces[0]?.id ?? ""));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedInvite | null>(null);
  const [copied, setCopied] = useState(false);
  const [now] = useState(() => Date.now());

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const checked = checkInviteEmail(email);
    if ("error" in checked) {
      setError(checked.error);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const invite = await api.createInvite(org.id, {
        email: checked.email,
        role,
        workspace_id: role === "member" && space ? space : null,
      });
      // The earlier link stays on the page until a new invite exists.
      setCreated(invite);
      setCopied(false);
      setEmail("");
      onCreated();
    } catch (e) {
      setError(errorText(e, "Could not make the invite."));
    } finally {
      setBusy(false);
    }
  };

  const link = created ? inviteLink(window.location.origin, created.token) : "";
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      setError("Could not copy the link. Select it and copy it.");
    }
  };

  return (
    <section className={`card ${styles.card}`} aria-labelledby="members-invite">
      <h2 id="members-invite" className={styles.heading}>
        Invite a person
      </h2>
      <form className={styles.form} onSubmit={submit}>
        <div className={own.fields}>
          <label className="field">
            <span className="label">Email</span>
            <input
              className="input"
              type="email"
              autoComplete="off"
              maxLength={320}
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={busy}
            />
          </label>
          <label className="field">
            <span className="label">Role</span>
            <select
              className="input"
              value={role}
              onChange={(e) => setRole(e.target.value as InviteRole)}
              disabled={busy}
            >
              {INVITE_ROLES.map((r) => (
                <option key={r} value={r}>
                  {ROLE_LABELS[r]}
                </option>
              ))}
            </select>
          </label>
          {role === "member" ? (
            <label className="field">
              <span className="label">Workspace</span>
              <select className="input" value={space} onChange={(e) => setSpace(e.target.value)} disabled={busy}>
                <option value="">No workspace</option>
                {org.workspaces.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>
        <span className="hint">
          {role === "admin"
            ? "An admin sees every workspace and manages the members."
            : "A member sees only the workspaces that you give."}
        </span>
        <div className={styles.actions}>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "Making the link…" : "Make an invite link"}
          </button>
        </div>
      </form>
      <div aria-live="polite">
        {error ? <p className="alert alert-error">{error}</p> : null}
        {created ? (
          <div className={styles.form}>
            <p className="alert alert-success">
              Send this link to {created.email}. sama does not send email. You see the link only
              now.
              {created.expires_at ? ` The link ends ${timeLeft(created.expires_at, now)}.` : ""}
            </p>
            <div className={own.link}>
              <input className="input" readOnly value={link} aria-label="Invite link" onFocus={(e) => e.target.select()} />
              <button type="button" className="btn" onClick={copy}>
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}

function InviteRow({ org, invite, onChange }: { org: Org; invite: OrgInvite; onChange: () => void }) {
  const api = useApi();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now] = useState(() => Date.now());
  const space = org.workspaces.find((w) => w.id === invite.workspace_id);

  const revoke = async () => {
    if (!window.confirm(`Revoke the invite for ${invite.email}? The link stops working.`)) return;
    setBusy(true);
    setError(null);
    try {
      await api.revokeInvite(org.id, invite.id);
      onChange();
    } catch (e) {
      setError(errorText(e, "Could not revoke the invite."));
      setBusy(false);
    }
  };

  return (
    <li className={styles.row}>
      <div className={own.who}>
        <div className={styles.name}>
          <span>{invite.email}</span>
          <span className={roleBadge(invite.role)}>{ROLE_LABELS[invite.role]}</span>
          {invite.expired ? <span className="badge badge-warning">Ended</span> : null}
        </div>
        <span className={styles.meta}>
          {space ? `${space.name} · ` : ""}
          {invite.invited_by ? `from ${invite.invited_by} · ` : ""}
          {invite.expires_at && !invite.expired ? `ends ${timeLeft(invite.expires_at, now)}` : ""}
        </span>
        {error ? (
          <p className={styles.error} role="alert">
            {error}
          </p>
        ) : null}
      </div>
      <button type="button" className="btn btn-small btn-danger" onClick={revoke} disabled={busy}>
        {busy ? "Revoking…" : "Revoke"}
      </button>
    </li>
  );
}
