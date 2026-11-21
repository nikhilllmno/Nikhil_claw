"use client";

// The browser settings of an org on the settings page (docs/architecture.md, Sections 14.1 and
// 16.4). A member adds personal saved logins. An org admin adds org logins and sets the URL
// policy of the org. The user name and the password go only into the form of this page: the API
// never sends them back, and the agent never sees them.

import { useCallback, useState } from "react";
import { type BrowserLogin, type BrowserLoginAdd, type BrowserUrlPolicy, errorText, type Org } from "@/lib/api";
import {
  hostsProblem,
  loginProblem,
  loginScope,
  MAX_NAME_CHARS,
  MAX_PASSWORD_CHARS,
  MAX_SITE_CHARS,
  MAX_USERNAME_CHARS,
  parseHosts,
} from "@/lib/browser";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

const EMPTY: BrowserLoginAdd = { name: "", site: "", username: "", password: "", scope: "personal" };

export function BrowserSettings({ org }: { org: Org }) {
  if (!org.region_here) return null;
  return (
    <section className={`card ${styles.card}`} aria-label={`Browser settings of ${org.name}`}>
      <div className={styles.cardHead}>
        <h2 className={styles.heading}>Browser</h2>
        <span className={styles.meta}>{org.name}</span>
      </div>
      <SavedLogins org={org} />
      {org.admin ? (
        <UrlPolicy org={org} />
      ) : (
        <p className={styles.meta}>An org admin sets the sites that the browser of the agents can open.</p>
      )}
    </section>
  );
}

function SavedLogins({ org }: { org: Org }) {
  const api = useApi();
  const load = useCallback(() => api.browserLogins(org.id), [api, org.id]);
  const logins = useLoad(load, "Could not load the saved logins.");
  const [draft, setDraft] = useState<BrowserLoginAdd>(EMPTY);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [now] = useState(() => Date.now());

  const set = (field: keyof BrowserLoginAdd, value: string) => setDraft((all) => ({ ...all, [field]: value }));

  const add = async (event: React.FormEvent) => {
    event.preventDefault();
    const problem = loginProblem(draft);
    if (problem !== null) {
      setNotice(null);
      setError(problem);
      return;
    }
    const body = { ...draft, name: draft.name.trim(), site: draft.site.trim() };
    // The secret leaves the page state before the call.
    setDraft((all) => ({ ...EMPTY, scope: all.scope }));
    setBusy("add");
    setError(null);
    setNotice(null);
    try {
      const added = await api.addBrowserLogin(org.id, body);
      logins.setData([...(logins.data ?? []), added]);
      setNotice(`The login ${added.name} is saved. Ask the agent to sign in with ${added.name}.`);
    } catch (e) {
      setError(errorText(e, "The login was not saved."));
    } finally {
      setBusy(null);
    }
  };

  const remove = async (login: BrowserLogin) => {
    if (!window.confirm(`Remove the saved login ${login.name}? The agent can then not use it.`)) return;
    setBusy(`remove-${login.id}`);
    setError(null);
    setNotice(null);
    try {
      await api.removeBrowserLogin(org.id, login.id);
      logins.setData((logins.data ?? []).filter((row) => row.id !== login.id));
      setNotice(`The login ${login.name} is removed.`);
    } catch (e) {
      setError(errorText(e, "The login was not removed."));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className={styles.section}>
      <h3 className={styles.heading}>Saved logins</h3>
      <p className={styles.text}>
        The agent signs in to a site with a saved login when you ask it by name. Sama fills the user name and the
        password into the page of that site only. The agent never sees them. Never give a password in a chat.
      </p>
      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      {logins.error ? (
        <ErrorState title="The saved logins did not load" message={logins.error} onRetry={logins.reload} />
      ) : logins.data === null ? (
        <LoadingState label="Loading the saved logins" />
      ) : logins.data.length === 0 ? (
        <p className={styles.meta}>There are no saved logins.</p>
      ) : (
        <ul className={styles.list}>
          {logins.data.map((login) => (
            <li key={login.id} className={styles.row}>
              <div className={styles.name}>
                <span>{login.name}</span>
                <span className={login.scope === "org" ? "badge badge-accent" : "badge"}>{loginScope(login)}</span>
                <span className={`${styles.meta} mono`}>{login.site}</span>
                <span className={styles.meta}>Changed {relativeTime(login.updated_at, now)}.</span>
              </div>
              {login.can_manage ? (
                <button
                  type="button"
                  className="btn btn-danger btn-small"
                  disabled={busy !== null}
                  onClick={() => void remove(login)}
                >
                  {busy === `remove-${login.id}` ? "Removing" : "Remove"}
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      <form className={styles.form} onSubmit={(event) => void add(event)} aria-label="Add a saved login">
        <label className="field">
          <span className="label">Name</span>
          <input
            className="input"
            value={draft.name}
            maxLength={MAX_NAME_CHARS}
            placeholder="GitHub"
            onChange={(e) => set("name", e.target.value)}
          />
          <span className="hint">You and the agent use this name.</span>
        </label>
        <label className="field">
          <span className="label">Site</span>
          <input
            className="input"
            value={draft.site}
            maxLength={MAX_SITE_CHARS}
            placeholder="https://github.com"
            onChange={(e) => set("site", e.target.value)}
          />
          <span className="hint">Sama fills the login only on this site and its subdomains.</span>
        </label>
        <label className="field">
          <span className="label">User name</span>
          <input
            className="input"
            autoComplete="off"
            value={draft.username}
            maxLength={MAX_USERNAME_CHARS}
            onChange={(e) => set("username", e.target.value)}
          />
        </label>
        <label className="field">
          <span className="label">Password</span>
          <input
            className="input"
            type="password"
            autoComplete="new-password"
            value={draft.password}
            maxLength={MAX_PASSWORD_CHARS}
            onChange={(e) => set("password", e.target.value)}
          />
          <span className="hint">The password goes to the vault of sama. No page shows it again.</span>
        </label>
        {org.admin ? (
          <label className="field">
            <span className="label">Who can use it</span>
            <select
              className="input"
              value={draft.scope}
              onChange={(e) => set("scope", e.target.value === "org" ? "org" : "personal")}
            >
              <option value="personal">Only me</option>
              <option value="org">Every member of the org</option>
            </select>
          </label>
        ) : null}
        <div className={styles.actions}>
          <button type="submit" className="btn btn-primary btn-small" disabled={busy !== null}>
            {busy === "add" ? "Saving" : "Save the login"}
          </button>
        </div>
      </form>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function UrlPolicy({ org }: { org: Org }) {
  const api = useApi();
  const load = useCallback(() => api.browserUrlPolicy(org.id), [api, org.id]);
  const policy = useLoad(load, "Could not load the URL policy.");
  return (
    <div className={styles.section}>
      <h3 className={styles.heading}>URL policy</h3>
      <p className={styles.text}>
        The browser never opens private, internal, or cloud metadata addresses. Add hosts to block more sites. With an
        allow list, the browser opens only the listed sites. A host covers its subdomains.
      </p>
      {policy.error ? (
        <ErrorState title="The URL policy did not load" message={policy.error} onRetry={policy.reload} />
      ) : policy.data === null ? (
        <LoadingState label="Loading the URL policy" />
      ) : (
        <PolicyForm
          key={policy.data.updated_at ?? "new"}
          org={org}
          policy={policy.data}
          onSaved={(saved) => policy.setData(saved)}
        />
      )}
    </div>
  );
}

function PolicyForm({
  org,
  policy,
  onSaved,
}: {
  org: Org;
  policy: BrowserUrlPolicy;
  onSaved: (policy: BrowserUrlPolicy) => void;
}) {
  const api = useApi();
  const [allow, setAllow] = useState(policy.allow_hosts.join("\n"));
  const [block, setBlock] = useState(policy.block_hosts.join("\n"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    const allowHosts = parseHosts(allow);
    const blockHosts = parseHosts(block);
    const problem = hostsProblem(allowHosts, blockHosts);
    if (problem !== null) {
      setNotice(null);
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const saved = await api.setBrowserUrlPolicy(org.id, allowHosts, blockHosts);
      onSaved(saved);
      setNotice(
        saved.mode === "allow_list"
          ? `The browser opens only ${saved.allow_hosts.length} listed site(s) now.`
          : "The URL policy is saved.",
      );
    } catch (e) {
      setError(errorText(e, "The URL policy was not saved."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className={styles.form} onSubmit={(event) => void save(event)} aria-label="Set the URL policy">
      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      <label className="field">
        <span className="label">Blocked hosts</span>
        <textarea
          className="input mono"
          rows={4}
          value={block}
          placeholder={"example-ads.com\nsocial.example"}
          onChange={(e) => setBlock(e.target.value)}
        />
        <span className="hint">One host for each line. The browser never opens these sites.</span>
      </label>
      <label className="field">
        <span className="label">Allowed hosts</span>
        <textarea
          className="input mono"
          rows={4}
          value={allow}
          placeholder="Empty: every public site"
          onChange={(e) => setAllow(e.target.value)}
        />
        <span className="hint">Empty: the browser opens every public site that is not blocked.</span>
      </label>
      <div className={styles.actions}>
        <button type="submit" className="btn btn-primary btn-small" disabled={busy}>
          {busy ? "Saving" : "Save the URL policy"}
        </button>
      </div>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </form>
  );
}
