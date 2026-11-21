"use client";

// The chat apps of an org (docs/architecture.md, Sections 11.4 and 11.6): Telegram and Slack.
// An org admin connects or disconnects the bot of the org here. A bot token or a signing secret
// goes only into the form of this card, and leaves the page state as soon as the API has it.
// The settings page and the setup guide both show this card.

import { useState } from "react";
import { errorText, type IntegrationSummary, type Org, type SlackSetup } from "@/lib/api";
import { integrationTone, toneBadge } from "@/lib/history";
import { useApi } from "./AuthProvider";
import styles from "./Page.module.css";

function statusBadge(status: string | undefined): string {
  return toneBadge(integrationTone(status));
}

/** The text of a connect: a success only when the app is active. */
function connectResult(app: "Telegram" | "Slack", view: IntegrationSummary): string | { failure: string } {
  if (view.status === "active") {
    const name = view.name ? (app === "Telegram" ? ` @${view.name}` : ` ${view.name}`) : "";
    return `The ${app} app${name} is connected.`;
  }
  const why = view.last_error ? ` ${app} gave this error: ${view.last_error}` : "";
  return { failure: `The ${app} app is not active.${why} Check the values and connect again.` };
}

type Result = string | { failure: string };

export function ChatApps({
  org,
  onChanged,
  onConnected,
  title = "Chat apps",
}: {
  org: Org;
  onChanged: () => void;
  /** Called after a connect that made an app active. */
  onConnected?: (kind: "telegram" | "slack") => void;
  title?: string;
}) {
  const api = useApi();
  const telegram = org.integrations.find((i) => i.kind === "telegram");
  const slack = org.integrations.find((i) => i.kind === "slack");
  const [token, setToken] = useState("");
  const [slackToken, setSlackToken] = useState("");
  const [signingSecret, setSigningSecret] = useState("");
  const [setup, setSetup] = useState<SlackSetup | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // An action gives the text of its success, or `{ failure }` when the API said yes but the
  // change did not work.
  const act = async (label: string, action: () => Promise<Result>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      const done = await action();
      if (typeof done === "string") setNotice(done);
      else setError(done.failure);
      onChanged();
    } catch (e) {
      setError(errorText(e, "The change failed."));
    } finally {
      setBusy(null);
    }
  };

  const connectTelegram = (event: React.FormEvent) => {
    event.preventDefault();
    const value = token.trim();
    if (value === "") {
      setError("Give the token of the bot.");
      return;
    }
    // The secret leaves the page state before the call.
    setToken("");
    void act("connect-telegram", async () => {
      const view = await api.connectTelegram(org.id, value);
      if (view.status === "active") onConnected?.("telegram");
      return connectResult("Telegram", view);
    });
  };

  const startSlack = () => {
    setBusy("setup-slack");
    setError(null);
    setNotice(null);
    api.setupSlack(org.id).then(
      (value) => {
        setSetup(value);
        setBusy(null);
      },
      (e: unknown) => {
        setError(errorText(e, "Slack could not be set up."));
        setBusy(null);
      },
    );
  };

  const connectSlack = (event: React.FormEvent) => {
    event.preventDefault();
    const bot = slackToken.trim();
    const secret = signingSecret.trim();
    if (!bot.startsWith("xoxb-")) {
      setError("Give the Bot User OAuth Token of the Slack app. It starts with xoxb-.");
      return;
    }
    if (secret === "") {
      setError("Give the Signing Secret of the Slack app.");
      return;
    }
    setSlackToken("");
    setSigningSecret("");
    void act("connect-slack", async () => {
      const view = await api.connectSlack(org.id, bot, secret);
      if (view.status === "active") {
        setSetup(null);
        onConnected?.("slack");
      }
      return connectResult("Slack", view);
    });
  };

  const disconnect = (kind: "telegram" | "slack") => {
    const app = kind === "telegram" ? "Telegram" : "Slack";
    if (!window.confirm(`Disconnect the ${app} app? People can no longer talk to the agents there.`)) return;
    void act(`disconnect-${kind}`, async () => {
      await api.disconnectIntegration(org.id, kind);
      return `The ${app} app is disconnected.`;
    });
  };

  return (
    <section className={`card ${styles.card}`} aria-label={`Chat apps of ${org.name}`}>
      <div className={styles.cardHead}>
        <h2 className={styles.heading}>{title}</h2>
        <span className={styles.meta}>{org.name}</span>
      </div>
      <p className={styles.text}>People talk to the agents of the org in these apps. Each org has its own bot.</p>
      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}

      <div className={styles.section}>
        <div className={styles.row}>
          <div className={styles.name}>
            <span>Telegram</span>
            <span className={statusBadge(telegram?.status)}>{telegram ? telegram.status : "Not connected"}</span>
            {telegram?.name ? <span className={styles.meta}>@{telegram.name}</span> : null}
          </div>
          {org.admin && telegram ? (
            <button
              type="button"
              className="btn btn-danger btn-small"
              disabled={busy !== null}
              onClick={() => disconnect("telegram")}
            >
              {busy === "disconnect-telegram" ? "Disconnecting" : "Disconnect"}
            </button>
          ) : null}
        </div>
        {telegram && telegram.status !== "active" ? (
          <p className={styles.error}>
            {telegram.last_error
              ? `The bot is not active. Telegram gave this error: ${telegram.last_error}`
              : `The bot is not active. Its status is ${telegram.status}.`}
            {org.admin ? " Connect it again with the token of the bot." : " An org admin must connect it again."}
          </p>
        ) : null}
        {org.admin && telegram?.status !== "active" ? (
          <form className={styles.form} onSubmit={connectTelegram} aria-label="Connect a Telegram bot">
            <label className="field">
              <span className="label">Bot token</span>
              <input
                className="input"
                type="password"
                autoComplete="off"
                value={token}
                maxLength={200}
                onChange={(e) => setToken(e.target.value)}
              />
              <span className="hint">
                Make a bot with @BotFather in Telegram and paste its token here. Never paste it in a chat.
              </span>
            </label>
            <div className={styles.actions}>
              <button type="submit" className="btn btn-primary btn-small" disabled={busy !== null}>
                {busy === "connect-telegram" ? "Connecting" : telegram ? "Connect again" : "Connect"}
              </button>
            </div>
          </form>
        ) : null}
        {!org.admin && !telegram ? <p className={styles.meta}>An org admin connects the Telegram bot.</p> : null}
      </div>

      <div className={styles.section}>
        <div className={styles.row}>
          <div className={styles.name}>
            <span>Slack</span>
            <span className={statusBadge(slack?.status)}>{slack ? slack.status : "Not connected"}</span>
            {slack?.name ? <span className={styles.meta}>{slack.name}</span> : null}
          </div>
          {org.admin && slack ? (
            <button
              type="button"
              className="btn btn-danger btn-small"
              disabled={busy !== null}
              onClick={() => disconnect("slack")}
            >
              {busy === "disconnect-slack" ? "Disconnecting" : "Disconnect"}
            </button>
          ) : null}
        </div>
        {!org.admin ? (
          slack ? null : (
            <p className={styles.meta}>An org admin connects the Slack app.</p>
          )
        ) : slack?.status === "active" ? null : setup === null ? (
          <div className={styles.actions}>
            <button type="button" className="btn btn-small" disabled={busy !== null} onClick={startSlack}>
              {busy === "setup-slack" ? "Setting up" : slack ? "Finish the Slack setup" : "Set up Slack"}
            </button>
          </div>
        ) : (
          <form className={styles.form} onSubmit={connectSlack} aria-label="Connect the Slack app">
            <ol className={styles.text}>
              <li>
                <a href={setup.manifest_link} target="_blank" rel="noopener noreferrer">
                  Create the Slack app
                </a>{" "}
                from the manifest that sama made for your org, and install it in your Slack workspace.
              </li>
              <li>Copy the Bot User OAuth Token (OAuth and Permissions) and the Signing Secret (Basic Information).</li>
              <li>Paste them here. Never paste them in a chat.</li>
            </ol>
            <label className="field">
              <span className="label">Bot User OAuth Token</span>
              <input
                className="input"
                type="password"
                autoComplete="off"
                value={slackToken}
                maxLength={300}
                onChange={(e) => setSlackToken(e.target.value)}
              />
            </label>
            <label className="field">
              <span className="label">Signing Secret</span>
              <input
                className="input"
                type="password"
                autoComplete="off"
                value={signingSecret}
                maxLength={100}
                onChange={(e) => setSigningSecret(e.target.value)}
              />
            </label>
            <div className={styles.actions}>
              <button type="submit" className="btn btn-primary btn-small" disabled={busy !== null}>
                {busy === "connect-slack" ? "Connecting" : "Connect"}
              </button>
              <button type="button" className="btn btn-quiet btn-small" disabled={busy !== null} onClick={() => {
                  // The secrets leave the page state when the form closes.
                  setSlackToken("");
                  setSigningSecret("");
                  setSetup(null);
                }}
              >
                Cancel
              </button>
            </div>
          </form>
        )}
      </div>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}
