"use client";

// The Connect card of the chat (architecture §10.3.4, M8.1). The button opens the sign-in page of
// the provider in a new tab. After the click, the card checks the connection every few seconds,
// so it shows "connected" when the sign-in finishes. The agent continues the request by itself.

import { useEffect, useState } from "react";
import { useApi } from "./AuthProvider";
import { cardStatus, type ConnectCardData } from "@/lib/connectCard";
import { createPoller } from "@/lib/poller";
import styles from "./ConnectCard.module.css";

const POLL_MS = 4000;
const POLL_FOR_MS = 15 * 60 * 1000;

export function ConnectCard({ card }: { card: ConnectCardData }) {
  const api = useApi();
  const [state, setState] = useState(card.state);
  // Each click starts a new 15-minute window of checks.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (attempt === 0 || !card.connectionId) return;
    // One request at a time (the poller of the Code page), also for the focus refresh.
    const poller = createPoller({
      load: () => api.checkConnection(card.connectionId),
      onData: (checked) => {
        setState(checked.state);
        if (checked.state === "connected") poller.stop();
      },
      onError: () => {
        // The next poll tries again. A connection that the user cannot see stays as it is.
      },
      intervalMs: POLL_MS,
    });
    const deadline = window.setTimeout(() => poller.stop(), POLL_FOR_MS);
    const onFocus = () => poller.refresh();
    window.addEventListener("focus", onFocus);
    poller.start();
    return () => {
      poller.stop();
      window.clearTimeout(deadline);
      window.removeEventListener("focus", onFocus);
    };
  }, [api, card.connectionId, attempt]);

  const connected = state === "connected";
  const label = card.state === "form" ? `Open the ${card.title} form` : `Connect ${card.title}`;
  return (
    <section className={`card ${styles.card}`} aria-label={`Connect ${card.title}`}>
      <div className={styles.row}>
        <span className={styles.title}>{card.title}</span>
        <span className={connected ? "badge badge-accent" : "badge"}>
          {connected ? "Connected" : card.scope === "workspace" ? "Whole workspace" : "Only you"}
        </span>
      </div>
      <p className={styles.text} role="status">
        {cardStatus(connected ? "connected" : card.state === "form" ? "form" : state, card.title)}
      </p>
      {!connected && card.url ? (
        <div>
          <a
            className="btn btn-primary btn-small"
            href={card.url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={() => setAttempt((n) => n + 1)}
          >
            {attempt > 0 ? `Open the ${card.title} sign-in again` : label}
          </a>
        </div>
      ) : null}
    </section>
  );
}
