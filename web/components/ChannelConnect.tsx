"use client";

// The WhatsApp and email rows of the chat apps of an org (docs/architecture.md, Section 11.9).
// An org admin picks the WhatsApp provider (Meta, Twilio, or Gupshup) and pastes its
// credentials, or connects an inbound email address with SendGrid. The secrets go only into
// this form. After a connect the page shows the webhook URL and the steps that are left at the
// provider. A link from the agent (`/settings?connect=whatsapp&provider=twilio`) opens the form
// with the provider.

import { useEffect, useState } from "react";
import { type ChannelSetup, type EmailConnect, errorText, type IntegrationSummary, type Org, type WhatsAppConnect } from "@/lib/api";
import {
  emailProblem,
  providerOf,
  TOKEN_LABELS,
  WHATSAPP_FIELDS,
  WHATSAPP_PROVIDERS,
  WHATSAPP_TITLES,
  whatsappBody,
  whatsappProblem,
} from "@/lib/channels";
import { integrationTone, toneBadge } from "@/lib/history";
import { useApi } from "./AuthProvider";
import styles from "./Page.module.css";

type Kind = "whatsapp" | "email";

const EMPTY_WHATSAPP: WhatsAppConnect = { provider: "meta", number: "", api_token: "" };
const EMPTY_EMAIL: EmailConnect = { address: "", api_key: "", public_key: "", sender_name: "sama" };

function statusBadge(status: string | undefined): string {
  return toneBadge(integrationTone(status));
}

/** The `connect` and `provider` values of the page link, read once on the client. */
function linkTarget(): { connect: string | null; provider: string | null } {
  if (typeof window === "undefined") return { connect: null, provider: null };
  const params = new URLSearchParams(window.location.search);
  return { connect: params.get("connect"), provider: params.get("provider") };
}

export function ChannelConnect({ org, kind, onChanged }: { org: Org; kind: Kind; onChanged: () => void }) {
  const api = useApi();
  const current: IntegrationSummary | undefined = org.integrations.find((i) => i.kind === kind);
  const title = kind === "whatsapp" ? "WhatsApp" : "Email";
  const [open, setOpen] = useState(false);
  const [whatsapp, setWhatsapp] = useState<WhatsAppConnect>(EMPTY_WHATSAPP);
  const [email, setEmail] = useState<EmailConnect>(EMPTY_EMAIL);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [setup, setSetup] = useState<ChannelSetup | null>(null);

  useEffect(() => {
    const target = linkTarget();
    if (target.connect !== kind) return;
    setOpen(true);
    if (kind === "whatsapp") setWhatsapp((w) => ({ ...w, provider: providerOf(target.provider) }));
  }, [kind]);

  const finish = (result: ChannelSetup) => {
    // The secrets leave the page state as soon as the API has them.
    setWhatsapp((w) => ({ ...EMPTY_WHATSAPP, provider: w.provider }));
    setEmail(EMPTY_EMAIL);
    setSetup(result);
    setOpen(false);
    onChanged();
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const problem = kind === "whatsapp" ? whatsappProblem(whatsapp) : emailProblem(email);
    if (problem !== null) {
      setError(problem);
      return;
    }
    setBusy("connect");
    setError(null);
    try {
      finish(
        kind === "whatsapp"
          ? await api.connectWhatsApp(org.id, whatsappBody(whatsapp))
          : await api.connectEmail(org.id, {
              ...email,
              address: email.address.trim(),
              api_key: email.api_key.trim(),
              public_key: email.public_key.trim(),
            }),
      );
    } catch (e) {
      setError(errorText(e, "The connect failed."));
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async () => {
    if (!window.confirm(`Disconnect ${title}? People can no longer talk to the agents there.`)) return;
    setBusy("disconnect");
    setError(null);
    try {
      await api.disconnectIntegration(org.id, kind);
      setSetup(null);
      onChanged();
    } catch (e) {
      setError(errorText(e, "The disconnect failed."));
    } finally {
      setBusy(null);
    }
  };

  const input = (
    label: string,
    value: string,
    change: (value: string) => void,
    options: { secret?: boolean; hint?: string; multiline?: boolean } = {},
  ) => (
    <label className="field" key={label}>
      <span className="label">{label}</span>
      {options.multiline ? (
        <textarea className="input" rows={3} value={value} onChange={(e) => change(e.target.value)} />
      ) : (
        <input
          className="input"
          type={options.secret ? "password" : "text"}
          autoComplete="off"
          value={value}
          maxLength={600}
          onChange={(e) => change(e.target.value)}
        />
      )}
      {options.hint ? <span className="hint">{options.hint}</span> : null}
    </label>
  );

  return (
    <div className={styles.section}>
      <div className={styles.row}>
        <div className={styles.name}>
          <span>{title}</span>
          <span className={statusBadge(current?.status)}>{current ? current.status : "Not connected"}</span>
          {current?.name ? <span className={styles.meta}>{current.name}</span> : null}
        </div>
        {org.admin ? (
          <div className={styles.actions}>
            <button type="button" className="btn btn-small" disabled={busy !== null} onClick={() => setOpen((o) => !o)}>
              {open ? "Close" : current ? "Connect again" : "Connect"}
            </button>
            {current ? (
              <button
                type="button"
                className="btn btn-danger btn-small"
                disabled={busy !== null}
                onClick={() => void disconnect()}
              >
                {busy === "disconnect" ? "Disconnecting" : "Disconnect"}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
      {current && current.status !== "active" && current.last_error ? (
        <p className={styles.error}>The connection is not active. The provider gave this error: {current.last_error}</p>
      ) : null}
      {!org.admin && !current ? <p className={styles.meta}>An org admin connects {title}.</p> : null}
      {open && org.admin ? (
        <form className={styles.form} onSubmit={(e) => void submit(e)} aria-label={`Connect ${title}`}>
          {kind === "whatsapp" ? (
            <>
              <label className="field">
                <span className="label">Provider</span>
                <select
                  className="input"
                  value={whatsapp.provider}
                  onChange={(e) => setWhatsapp({ ...whatsapp, provider: providerOf(e.target.value) })}
                >
                  {WHATSAPP_PROVIDERS.map((p) => (
                    <option key={p} value={p}>
                      {WHATSAPP_TITLES[p]}
                    </option>
                  ))}
                </select>
              </label>
              {input("Business number", whatsapp.number, (number) => setWhatsapp({ ...whatsapp, number }), {
                hint: "In international form, for example +15551234567.",
              })}
              {input(
                TOKEN_LABELS[whatsapp.provider],
                whatsapp.api_token,
                (api_token) => setWhatsapp({ ...whatsapp, api_token }),
                { secret: true, hint: "Never paste it in a chat." },
              )}
              {WHATSAPP_FIELDS[whatsapp.provider].map(({ field, label, secret }) =>
                input(label, whatsapp[field] ?? "", (value) => setWhatsapp({ ...whatsapp, [field]: value }), { secret }),
              )}
            </>
          ) : (
            <>
              {input("Inbound address", email.address, (address) => setEmail({ ...email, address }), {
                hint: "An address on a subdomain whose MX record points to SendGrid.",
              })}
              {input("SendGrid API key", email.api_key, (api_key) => setEmail({ ...email, api_key }), {
                secret: true,
                hint: "A key with the Mail Send permission. Never paste it in a chat.",
              })}
              {input("Public key of the parse security policy", email.public_key, (public_key) => setEmail({ ...email, public_key }), {
                multiline: true,
              })}
              {input("Sender name", email.sender_name ?? "", (sender_name) => setEmail({ ...email, sender_name }))}
            </>
          )}
          <div className={styles.actions}>
            <button type="submit" className="btn btn-primary btn-small" disabled={busy !== null}>
              {busy === "connect" ? "Connecting" : "Connect"}
            </button>
          </div>
        </form>
      ) : null}
      {setup ? (
        <div className="alert" role="status">
          <p>
            Give this URL to the provider: <code className="mono">{setup.webhook_url}</code>
          </p>
          {setup.verify_token ? (
            <p>
              Verify token: <code className="mono">{setup.verify_token}</code>. sama shows it only now.
            </p>
          ) : null}
          <ol>
            {setup.steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        </div>
      ) : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
