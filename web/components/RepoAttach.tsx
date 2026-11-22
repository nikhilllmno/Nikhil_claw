"use client";

// The repos of a session: the list, the form that attaches one, and Detach (M7.9). The parent
// owns the data and reloads it in `onChanged`.

import { useId, useState } from "react";
import { errorText, type RepoOut } from "@/lib/api";
import { MAX_REPOS, optionalText, repoDetail, repoStatusLabel, repoTone } from "@/lib/coding";
import { toneBadge } from "@/lib/history";
import { useApi } from "./AuthProvider";
import styles from "./Coding.module.css";
import page from "./Page.module.css";

export function RepoAttach({
  sessionId,
  repos,
  onChanged,
}: {
  sessionId: string;
  repos: RepoOut[];
  /** Called after an attach or a detach, so that the parent loads the repos again. */
  onChanged: () => void;
}) {
  const api = useApi();
  const ids = useId();
  const [repo, setRepo] = useState("");
  const [ref, setRef] = useState("");
  const [folder, setFolder] = useState("");
  const [busy, setBusy] = useState(false);
  const [detaching, setDetaching] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The repo that the person attached last, until the list shows it.
  const [attached, setAttached] = useState<RepoOut | null>(null);
  const full = repos.length >= MAX_REPOS;

  const attach = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy || repo.trim() === "") return;
    setBusy(true);
    setError(null);
    try {
      const added = await api.attachRepo(sessionId, {
        repo: repo.trim(),
        ref: optionalText(ref),
        folder: optionalText(folder),
      });
      setAttached(added);
      setRepo("");
      setRef("");
      setFolder("");
      onChanged();
    } catch (e) {
      setError(errorText(e, "The repo was not attached."));
    } finally {
      setBusy(false);
    }
  };

  const detach = async (item: RepoOut) => {
    if (!window.confirm(`Detach ${item.repo}? The folder stays in the sandbox.`)) return;
    setDetaching(item.id);
    setError(null);
    try {
      await api.detachRepo(sessionId, item.id);
      onChanged();
    } catch (e) {
      setError(errorText(e, "The repo was not detached."));
    } finally {
      setDetaching(null);
    }
  };

  return (
    <div className={styles.panel}>
      <p className={styles.help}>
        Attach a GitHub repo to this session. Use the URL of a public repo, or a repo that your org
        allows. The repo clones into the sandbox when the next run starts. A session holds up to{" "}
        {MAX_REPOS} repos.
      </p>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      {attached !== null && !repos.some((r) => r.id === attached.id) ? (
        <p className="alert alert-success" role="status">
          Attached {attached.repo}. It clones into the sandbox when the next run starts.
        </p>
      ) : null}
      {repos.length === 0 && attached === null ? (
        <p className="alert">No repo is attached yet.</p>
      ) : (
        <ul className={page.list} aria-label="Attached repos">
          {repos.map((item) => (
            <li key={item.id}>
              <section className={`card ${page.card}`} aria-label={item.repo}>
                <div className={page.cardHead}>
                  <div className={page.name}>
                    <span className="mono">{item.repo}</span>
                    <span className={toneBadge(repoTone(item.status))}>{repoStatusLabel(item)}</span>
                  </div>
                  <button
                    type="button"
                    className="btn btn-small btn-danger"
                    disabled={detaching !== null}
                    aria-label={`Detach ${item.repo}`}
                    onClick={() => void detach(item)}
                  >
                    {detaching === item.id ? "Detaching…" : "Detach"}
                  </button>
                </div>
                <p className={page.meta}>
                  {[repoDetail(item), item.path ?? `Folder: ${item.folder}`].filter(Boolean).join(" · ")}
                </p>
                {item.status === "failed" && item.error ? (
                  <p className={page.error}>{item.error}</p>
                ) : null}
              </section>
            </li>
          ))}
        </ul>
      )}
      <form className={`card ${page.card}`} onSubmit={(e) => void attach(e)} aria-label="Attach a repo">
        <h2 className={page.subheading}>Attach a repo</h2>
        <div className={styles.fields}>
          <div className="field">
            <label className="label" htmlFor={`${ids}-repo`}>
              Repo
            </label>
            <input
              id={`${ids}-repo`}
              className="input"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="https://github.com/owner/name"
              autoComplete="off"
              spellCheck={false}
              disabled={busy || full}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor={`${ids}-ref`}>
              Branch or tag (optional)
            </label>
            <input
              id={`${ids}-ref`}
              className="input"
              value={ref}
              onChange={(e) => setRef(e.target.value)}
              placeholder="main"
              autoComplete="off"
              spellCheck={false}
              disabled={busy || full}
            />
          </div>
          <div className="field">
            <label className="label" htmlFor={`${ids}-folder`}>
              Folder (optional)
            </label>
            <input
              id={`${ids}-folder`}
              className="input"
              value={folder}
              onChange={(e) => setFolder(e.target.value)}
              placeholder="The name of the repo"
              autoComplete="off"
              spellCheck={false}
              disabled={busy || full}
            />
          </div>
        </div>
        {full ? <p className={page.meta}>This session has {MAX_REPOS} repos. Detach one to attach another.</p> : null}
        <div className={page.actions}>
          <button type="submit" className="btn btn-primary" disabled={busy || full || repo.trim() === ""}>
            {busy ? "Attaching…" : "Attach"}
          </button>
        </div>
      </form>
    </div>
  );
}
