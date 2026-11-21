"use client";

// The first screen of a user without an org (docs/architecture.md, Section 17.2): the open
// invites for the email of the user, and, when sign-up is open, the form for a new org.

import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import { errorText, type MyInvite } from "@/lib/api";
import { checkOrgName, ROLE_LABELS } from "@/lib/orgs";
import { timeLeft } from "@/lib/time";
import { useApi, useAuth } from "./AuthProvider";
import { useData } from "./DataProvider";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

export function Welcome() {
  const api = useApi();
  const { signup } = useAuth();
  const router = useRouter();
  const { reloadMe, reloadSessions } = useData();
  const loadInvites = useCallback(() => api.myInvites(), [api]);
  const { data: invites, error: invitesError, reload } = useLoad(loadInvites, "Could not load your invites.");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now] = useState(() => Date.now());

  const done = () => {
    reloadMe();
    reloadSessions();
  };

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    const checked = checkOrgName(name);
    if ("error" in checked) {
      setError(checked.error);
      return;
    }
    setBusy("create");
    setError(null);
    try {
      await api.createOrg(checked.name, null);
      done();
      // A new owner continues with the setup guide (Section 17.5).
      router.push("/onboarding");
    } catch (e) {
      setError(errorText(e, "Could not create the org."));
      setBusy(null);
    }
  };

  const join = async (invite: MyInvite) => {
    setBusy(invite.id);
    setError(null);
    try {
      await api.acceptInvite({ invite_id: invite.id });
      done();
    } catch (e) {
      setError(errorText(e, "Could not join the org."));
      setBusy(null);
      reload();
    }
  };

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>Welcome to sama</h1>
        <p className={styles.meta}>Join the org that invited you, or create your own org.</p>
      </header>

      <section className={`card ${styles.card}`} aria-labelledby="welcome-invites">
        <h2 id="welcome-invites" className={styles.heading}>
          Your invites
        </h2>
        {invitesError ? (
          <div className={styles.row}>
            <p className={styles.error}>{invitesError}</p>
            <button type="button" className="btn btn-small" onClick={reload}>
              Try again
            </button>
          </div>
        ) : invites === null ? (
          <p className={styles.meta}>Loading…</p>
        ) : invites.length === 0 ? (
          <p className={styles.meta}>
            You have no invites. An admin can invite the email address of this account.
          </p>
        ) : (
          <ul className={styles.list}>
            {invites.map((invite) => (
              <li key={invite.id} className={styles.row}>
                <div className={styles.name}>
                  <span>{invite.org_name}</span>
                  <span className="badge">{ROLE_LABELS[invite.role]}</span>
                </div>
                <div className={styles.actions}>
                  <span className={styles.meta}>
                    {invite.invited_by ? `From ${invite.invited_by} · ` : ""}
                    {invite.expires_at ? `ends ${timeLeft(invite.expires_at, now)}` : ""}
                  </span>
                  <button
                    type="button"
                    className="btn btn-primary btn-small"
                    onClick={() => join(invite)}
                    disabled={busy !== null}
                  >
                    {busy === invite.id ? "Joining…" : "Join"}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className={`card ${styles.card}`} aria-labelledby="welcome-create">
        <h2 id="welcome-create" className={styles.heading}>
          Create an org
        </h2>
        {signup === "open" ? (
          <form className={styles.form} onSubmit={create}>
            <p className={styles.meta}>
              You become the owner. The org gets a workspace and an agent, and starts on the free
              plan.
            </p>
            <label className="field">
              <span className="label">Org name</span>
              <input
                className="input"
                value={name}
                maxLength={100}
                required
                onChange={(e) => setName(e.target.value)}
                disabled={busy !== null}
              />
            </label>
            <div className={styles.actions}>
              <button type="submit" className="btn btn-primary" disabled={busy !== null}>
                {busy === "create" ? "Creating…" : "Create the org"}
              </button>
            </div>
          </form>
        ) : (
          <p className={styles.meta}>Sign-up is closed. Ask an admin of an org to invite you.</p>
        )}
      </section>

      <div aria-live="polite">{error ? <p className="alert alert-error">{error}</p> : null}</div>
    </div>
  );
}
