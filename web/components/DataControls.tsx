"use client";

// The data controls (docs/architecture.md, Section 17.4) in the web app (Section 17.6, M6.9).
// `OrgData` is the admin page: the retention of the org, the export of the org (an owner), and
// the delete of the org (an owner, who types the slug). `MyData` is on the settings page: each
// user exports or deletes the own data in an org. A delete cannot be undone, so each one asks
// for a confirmation first.

import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import { type DataSettings, errorText, type Org } from "@/lib/api";
import { confirmsSlug, daysText, retentionChoice, retentionChoices, retentionSummary, saveFile } from "@/lib/admin";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";
import own from "./Admin.module.css";

export function OrgData({ org }: { org: Org }) {
  return (
    <>
      <Retention org={org} />
      <OrgExport org={org} />
      <DeleteOrg org={org} />
    </>
  );
}

function Retention({ org }: { org: Org }) {
  const api = useApi();
  const load = useCallback(() => api.dataSettings(org.id), [api, org.id]);
  const settings = useLoad(load, "Could not load the data settings.");
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const save = async (data: DataSettings, value: string) => {
    const days = value === "" ? null : Number(value);
    if (days !== null && days < (data.effective_days ?? Infinity)) {
      const text = `Delete the sessions with no activity for ${daysText(days)}? Sama deletes them in the next hour. This cannot be undone.`;
      if (!window.confirm(text)) return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      settings.setData(await api.setRetention(org.id, days));
      setDraft(null);
      setNotice("The retention is saved.");
    } catch (e) {
      setError(errorText(e, "The retention was not saved."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={`card ${styles.card}`} aria-labelledby="data-retention">
      <h2 id="data-retention" className={styles.heading}>
        Retention
      </h2>
      {settings.error ? (
        <ErrorState title="The data settings did not load" message={settings.error} onRetry={settings.reload} />
      ) : settings.data === null ? (
        <LoadingState label="Loading the data settings" />
      ) : (
        <>
          <p className={styles.text}>{retentionSummary(settings.data)}</p>
          <p className={styles.meta}>
            The retention applies to sessions. Memory, skills, and the audit log stay.
            {settings.data.plan_max_days !== null
              ? ` The ${settings.data.plan} plan keeps a session for at most ${daysText(settings.data.plan_max_days)}.`
              : ""}
          </p>
          {notice ? (
            <p className="alert alert-success" role="status">
              {notice}
            </p>
          ) : null}
          {org.admin ? (
            <form
              className={styles.toolbar}
              onSubmit={(event) => {
                event.preventDefault();
                if (settings.data) void save(settings.data, draft ?? String(settings.data.retention_days ?? ""));
              }}
            >
              <label className={`field ${styles.grow}`}>
                <span className="label">Delete a session after</span>
                <select
                  className="input"
                  value={draft ?? String(settings.data.retention_days ?? "")}
                  onChange={(e) => setDraft(e.target.value)}
                  disabled={busy}
                >
                  {retentionChoices(settings.data).map((days) => (
                    <option key={days ?? "default"} value={days ?? ""}>
                      {retentionChoice(days, settings.data!)}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={busy || draft === null || draft === String(settings.data.retention_days ?? "")}
              >
                {busy ? "Saving…" : "Save"}
              </button>
            </form>
          ) : (
            <p className={styles.meta}>An org admin changes the retention.</p>
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

function useDownload() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const run = async (load: () => Promise<{ name: string; blob: Blob }>) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const file = await load();
      saveFile(file.name, file.blob);
      setNotice(`The download of ${file.name} started.`);
    } catch (e) {
      setError(errorText(e, "The export failed."));
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, notice, run };
}

function OrgExport({ org }: { org: Org }) {
  const api = useApi();
  const download = useDownload();
  const owner = org.role === "owner";
  return (
    <section className={`card ${styles.card}`} aria-labelledby="data-export">
      <h2 id="data-export" className={styles.heading}>
        Export the org
      </h2>
      <p className={styles.text}>
        A ZIP file with all the data of the org: sessions, messages, memory, skills, jobs, and the audit log. It has
        no secrets. A large org can take a few minutes.
      </p>
      {owner ? (
        <div className={styles.actions}>
          <button
            type="button"
            className="btn"
            disabled={download.busy}
            onClick={() => void download.run(() => api.exportOrg(org.id))}
          >
            {download.busy ? "Preparing the export…" : "Download the export"}
          </button>
        </div>
      ) : (
        <p className={styles.meta}>Only an org owner exports the org.</p>
      )}
      <div aria-live="polite">
        {download.notice ? <p className="alert alert-success">{download.notice}</p> : null}
        {download.error ? <p className="alert alert-error">{download.error}</p> : null}
      </div>
    </section>
  );
}

function DeleteOrg({ org }: { org: Org }) {
  const api = useApi();
  const router = useRouter();
  const { reloadMe, reloadSessions } = useData();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (org.role !== "owner") return null;

  const remove = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!confirmsSlug(typed, org.slug)) {
      setError(`Type ${org.slug} to confirm.`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.deleteOrg(org.id, typed.trim());
      reloadMe();
      reloadSessions();
      router.replace("/");
    } catch (e) {
      setError(errorText(e, "The org was not deleted."));
      setBusy(false);
    }
  };

  return (
    <section className={`card ${styles.card} ${own.danger}`} aria-labelledby="data-delete-org">
      <h2 id="data-delete-org" className={styles.heading}>
        Delete the org
      </h2>
      <p className={styles.text}>
        Sama deletes the org and all its data: members, sessions, agents, memory, skills, keys, chat apps, and the
        audit log. This cannot be undone. Export the org first if you need the data.
      </p>
      {!open ? (
        <div className={styles.actions}>
          <button type="button" className="btn btn-danger" onClick={() => setOpen(true)}>
            Delete the org…
          </button>
        </div>
      ) : (
        <form className={styles.form} onSubmit={remove} aria-label="Confirm the delete of the org">
          <label className="field">
            <span className="label">
              Type <span className="mono">{org.slug}</span> to confirm
            </span>
            <input
              className="input"
              value={typed}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setTyped(e.target.value)}
              disabled={busy}
            />
          </label>
          <div className={styles.actions}>
            <button type="submit" className="btn btn-danger" disabled={busy || !confirmsSlug(typed, org.slug)}>
              {busy ? "Deleting…" : "Delete the org forever"}
            </button>
            <button
              type="button"
              className="btn btn-quiet"
              disabled={busy}
              onClick={() => {
                setOpen(false);
                setTyped("");
                setError(null);
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}

/** The data of the user in one org: export and delete. */
export function MyData({ org }: { org: Org }) {
  const api = useApi();
  const router = useRouter();
  const { reloadMe, reloadSessions } = useData();
  const download = useDownload();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remove = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.deleteUser(org.id, org.user_id);
      reloadMe();
      reloadSessions();
      router.replace("/");
    } catch (e) {
      setError(errorText(e, "Your data was not deleted."));
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <section className={`card ${styles.card}`} aria-label={`Your data in ${org.name}`}>
      <div className={styles.cardHead}>
        <h2 className={styles.heading}>Your data</h2>
        <span className={styles.meta}>{org.name}</span>
      </div>
      <p className={styles.text}>
        Download a ZIP of your data in this org: your private sessions, your notes, your connections, your jobs, and
        the messages that you wrote in shared sessions. It has no secrets.
      </p>
      <div className={styles.actions}>
        <button
          type="button"
          className="btn btn-small"
          disabled={download.busy}
          onClick={() => void download.run(() => api.exportUser(org.id, org.user_id))}
        >
          {download.busy ? "Preparing…" : "Download my data"}
        </button>
        {!confirming ? (
          <button type="button" className="btn btn-danger btn-small" disabled={busy} onClick={() => setConfirming(true)}>
            Delete my data…
          </button>
        ) : null}
      </div>
      {confirming ? (
        <div className="alert alert-warning" role="alert">
          <p>
            Sama deletes your private sessions, your notes, your connections, and your jobs in {org.name}, and removes
            you from the org. Shared content stays without your name. This cannot be undone.
          </p>
          <div className={styles.actions}>
            <button type="button" className="btn btn-danger btn-small" disabled={busy} onClick={() => void remove()}>
              {busy ? "Deleting…" : "Delete my data forever"}
            </button>
            <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      <div aria-live="polite">
        {download.notice ? <p className="alert alert-success">{download.notice}</p> : null}
        {download.error ? <p className="alert alert-error">{download.error}</p> : null}
        {error ? <p className="alert alert-error">{error}</p> : null}
      </div>
    </section>
  );
}
