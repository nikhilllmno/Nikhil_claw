"use client";

// The model keys and the region of an org (docs/architecture.md, Section 17.3). An org admin
// sets the keys here. A key goes only into the form of this card. The API never sends it back.
// The admin area shows this card on the Org keys page.

import { useCallback, useState } from "react";
import { errorText, type ModelKeyStatus, type ModelProvider, type Org } from "@/lib/api";
import { keyProblem, keyUse, MAX_KEY_CHARS, PROVIDER_TITLES, regionText } from "@/lib/orgkeys";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

export function OrgKeys({ org }: { org: Org }) {
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

