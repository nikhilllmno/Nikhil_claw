"use client";

// The "Chrome extension" card of the settings page (M8.3, docs/architecture.md, Section 14.2.4).
// It shows if the sama extension of the person is connected, offers the build of this API as a
// download, and gives the steps to load it in Chrome. The Chrome Web Store item comes later.

import { useCallback, useState } from "react";
import { errorText } from "@/lib/api";
import { saveFile } from "@/lib/history";
import { useApi } from "./AuthProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

export function ChromeExtension() {
  const api = useApi();
  const load = useCallback(() => api.chromeState(), [api]);
  const state = useLoad(load, "Could not load the state of the Chrome extension.");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      saveFile("sama-extension.zip", await api.downloadExtension());
    } catch (e) {
      setError(errorText(e, "The download failed."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section id="chrome-extension" className={`card ${styles.card}`} aria-labelledby="settings-chrome">
      <div className={styles.cardHead}>
        <h2 id="settings-chrome" className={styles.heading}>
          Chrome extension
        </h2>
        {state.data ? (
          <span className={state.data.connected ? "badge badge-accent" : "badge"}>
            {state.data.connected ? "Connected" : "Not connected"}
          </span>
        ) : null}
      </div>
      <p className={styles.meta}>
        With the extension, the agent can work in your own Chrome, with your logins, when you choose
        &quot;Browser: My Chrome&quot; in a chat or ask it to use your browser. You see each step and can stop it.
      </p>
      {state.error ? (
        <ErrorState message={state.error} onRetry={state.reload} />
      ) : state.data === null ? (
        <LoadingState label="Loading the state of the extension" />
      ) : (
        <>
          <ol className={styles.steps}>
            <li>Download the extension and unzip it into a folder that you keep.</li>
            <li>
              Open <code>chrome://extensions</code> in Chrome and turn on Developer mode (top right).
            </li>
            <li>Click &quot;Load unpacked&quot; and choose the folder.</li>
            <li>Click the sama icon in the toolbar, then sign in with your sama email and password.</li>
          </ol>
          <div className={styles.actions}>
            {state.data.download ? (
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void download()}>
                {busy ? "Downloading…" : "Download the extension"}
              </button>
            ) : (
              <p className={styles.meta}>The download is not ready on this server yet.</p>
            )}
            <button type="button" className="btn" onClick={state.reload}>
              Check again
            </button>
          </div>
        </>
      )}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}
