"use client";

// The live browser of a session next to the chat (M8.3), as Claude for Chrome and the browser
// view of Codex show it: the step of the agent on top, the tab title and the address, the live
// view of the cloud browser, "Take control" and "Give back", and Stop. At a wide width it is a
// column next to the chat; at a narrow width it covers the chat as a dialog. The live view URL
// lives 10 minutes, and only members who can see the session get it.

import { useEffect, useRef, useState } from "react";
import type { LiveBrowser } from "@/lib/api";
import { shortUrl, VIEW_REFRESH_MS } from "@/lib/liveBrowser";
import panelStyles from "./CodePanel.module.css";
import styles from "./BrowserPanel.module.css";
import { useDrawerFocus } from "./useDrawerFocus";

export function BrowserPanel({
  live,
  step,
  running,
  modal,
  busy,
  error,
  onControl,
  onStop,
  onClose,
}: {
  /** The live browser, or `null` when no open run has one. */
  live: LiveBrowser | null;
  /** The current step of the agent in the browser. */
  step: string | null;
  /** True while a run of the session is open. */
  running: boolean;
  /** True at a narrow width: the panel covers the chat. */
  modal: boolean;
  busy: boolean;
  error: string | null;
  onControl: (action: "take" | "give_back") => void;
  onStop: () => void;
  onClose: () => void;
}) {
  const panel = useRef<HTMLElement>(null);
  useDrawerFocus(modal, panel, onClose);
  // The URL in the frame. A new URL of the same tab only renews its time to live, so the frame
  // keeps the old one and does not reload. Another tab loads at once; the same tab loads a fresh
  // URL before the old one ends.
  const [frame, setFrame] = useState<{ url: string; pageId: string | null } | null>(null);
  if (live !== null && live.view_url !== null && (frame === null || frame.pageId !== (live.page_id ?? null))) {
    setFrame({ url: live.view_url, pageId: live.page_id ?? null });
  }
  const latest = useRef(live);
  useEffect(() => {
    latest.current = live;
  });
  useEffect(() => {
    const timer = setInterval(() => {
      const now = latest.current;
      if (now?.view_url) setFrame({ url: now.view_url, pageId: now.page_id ?? null });
    }, VIEW_REFRESH_MS);
    return () => clearInterval(timer);
  }, []);

  const userControl = live?.control === "user";
  // The browser session ends with the run, so the frame shows only while the run is open.
  const shown = running && live !== null && live.view_url !== null && frame !== null;
  return (
    <aside
      ref={panel}
      className={`${panelStyles.panel} ${modal ? panelStyles.modal : ""}`}
      aria-label="Live browser"
      role={modal ? "dialog" : undefined}
      aria-modal={modal ? true : undefined}
    >
      <header className={panelStyles.head}>
        <div className={panelStyles.headText}>
          <h2 className={panelStyles.title}>
            {live === null ? "Browser" : live.title || shortUrl(live.page_url) || "Live browser"}
          </h2>
          {live?.page_url ? (
            <p className={panelStyles.meta} title={live.page_url}>
              {shortUrl(live.page_url)}
              {live.tabs && live.tabs > 1 ? ` · ${live.tabs} tabs` : ""}
            </p>
          ) : null}
        </div>
        {running && userControl && live?.view_url ? (
          <a className="btn btn-quiet btn-small" href={live.view_url} target="_blank" rel="noopener noreferrer">
            Open in new tab
          </a>
        ) : null}
        <button type="button" className="btn btn-quiet btn-small" onClick={onClose} aria-label="Close the browser panel">
          Close
        </button>
      </header>

      {live !== null ? (
        <div className={`${styles.bar} ${userControl ? styles.barUser : ""}`} role="status">
          {live.control === "pending" ? (
            <span className={styles.barText}>
              <span className="spinner" aria-hidden="true" />
              Taking control: the agent finishes its step.
            </span>
          ) : userControl ? (
            <span className={styles.barText}>
              You have control. The agent waits. Close any live view tab before you give back.
            </span>
          ) : (
            <span className={styles.barText}>
              {running ? <span className="spinner" aria-hidden="true" /> : null}
              {step ?? (running ? "The agent works in the browser" : "The agent is done")}
            </span>
          )}
          <span className={styles.barActions}>
            {running && live.view_url ? (
              userControl ? (
                <button type="button" className="btn btn-primary btn-small" disabled={busy} onClick={() => onControl("give_back")}>
                  Give back
                </button>
              ) : (
                <button type="button" className="btn btn-small" disabled={busy || !running || live.control === "pending"} onClick={() => onControl("take")}>
                  Take control
                </button>
              )
            ) : null}
            {running ? (
              <button type="button" className="btn btn-danger btn-small" onClick={onStop}>
                Stop
              </button>
            ) : null}
          </span>
        </div>
      ) : null}
      {error ? (
        <p className={`alert alert-error ${styles.error}`} role="alert">
          {error}
        </p>
      ) : null}

      <div className={styles.view}>
        {shown && frame !== null ? (
          // While the agent has control, `inert` keeps the pointer and the keyboard out of the
          // live view; the person only watches.
          <div className={styles.frameBox} inert={!userControl}>
            <iframe
              key={frame.url}
              src={frame.url}
              title={`Live view: ${live?.title || shortUrl(live?.page_url) || "browser"}`}
              className={`${styles.frame} ${userControl ? "" : styles.frameReadOnly}`}
              tabIndex={userControl ? 0 : -1}
              sandbox="allow-same-origin allow-scripts"
              allow="clipboard-read; clipboard-write"
              referrerPolicy="no-referrer"
            />
          </div>
        ) : (
          <p className={styles.empty}>
            {live !== null && live.target === "my_chrome"
              ? "The agent works in a tab of your own Chrome. Look at that tab."
              : running
                ? "The browser shows here when the agent opens a page."
                : live !== null
                  ? `The browser closed when the run ended. Last page: ${shortUrl(live.page_url) || "none"}.`
                  : "The browser of the agent shows here while it works."}
          </p>
        )}
      </div>
    </aside>
  );
}
