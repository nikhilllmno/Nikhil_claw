// The WhatsApp and email channels on the settings page (docs/architecture.md, Section 11.9).
// A token, a key, or a secret goes only into the form of the page. The API never sends it back.

import type { ChatChannel, EmailConnect, WhatsAppConnect, WhatsAppProvider } from "./api";

export const CHANNEL_TITLES: Record<ChatChannel, string> = {
  telegram: "Telegram",
  slack: "Slack",
  whatsapp: "WhatsApp",
  email: "Email",
};

export const WHATSAPP_PROVIDERS: WhatsAppProvider[] = ["meta", "twilio", "gupshup"];

export const WHATSAPP_TITLES: Record<WhatsAppProvider, string> = {
  meta: "Meta WhatsApp Cloud API",
  twilio: "Twilio",
  gupshup: "Gupshup",
};

export type WhatsAppField = "phone_number_id" | "account_sid" | "app_id" | "app_name" | "app_secret";

/** The fields of each provider, after the number and the API token. */
export const WHATSAPP_FIELDS: Record<WhatsAppProvider, { field: WhatsAppField; label: string; secret: boolean }[]> = {
  meta: [
    { field: "phone_number_id", label: "Phone number ID", secret: false },
    { field: "app_secret", label: "App secret", secret: true },
  ],
  twilio: [{ field: "account_sid", label: "Account SID", secret: false }],
  gupshup: [
    { field: "app_id", label: "App ID", secret: false },
    { field: "app_name", label: "App name", secret: false },
  ],
};

export const TOKEN_LABELS: Record<WhatsAppProvider, string> = {
  meta: "Access token",
  twilio: "Auth token",
  gupshup: "API key",
};

/** The provider of a `?provider=` value of the page, else Meta. */
export function providerOf(value: string | null): WhatsAppProvider {
  return WHATSAPP_PROVIDERS.find((p) => p === value) ?? "meta";
}

/** The same checks as the API, so that a wrong paste fails before it leaves the page. */
export function whatsappProblem(form: WhatsAppConnect): string | null {
  if (!/^\+[1-9][0-9]{6,14}$/.test(form.number.trim())) {
    return "Give the number in international form, for example +15551234567.";
  }
  if (form.api_token.trim() === "") return `Give the ${TOKEN_LABELS[form.provider].toLowerCase()}.`;
  for (const { field, label } of WHATSAPP_FIELDS[form.provider]) {
    if ((form[field] ?? "").trim() === "") return `Give the ${label.toLowerCase()}.`;
  }
  if (form.provider === "twilio" && !/^AC[0-9a-f]{32}$/.test((form.account_sid ?? "").trim())) {
    return "The account SID starts with AC and has 34 characters.";
  }
  return null;
}

/** Only the fields of the chosen provider go to the API. */
export function whatsappBody(form: WhatsAppConnect): WhatsAppConnect {
  const body: WhatsAppConnect = {
    provider: form.provider,
    number: form.number.trim(),
    api_token: form.api_token.trim(),
  };
  for (const { field } of WHATSAPP_FIELDS[form.provider]) {
    const value = (form[field] ?? "").trim();
    if (value !== "") body[field] = value;
  }
  return body;
}

export function emailProblem(form: EmailConnect): string | null {
  if (!/^[a-z0-9._-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$/i.test(form.address.trim())) {
    return "Give a plain address without +, for example sama@mail.example.com.";
  }
  if (!form.api_key.trim().startsWith("SG.")) return "A SendGrid API key starts with SG.";
  if (form.public_key.trim().length < 20) return "Paste the public key of the security policy.";
  return null;
}

/** The text that tells a user where the pairing code goes. */
export function pairingHint(channel: ChatChannel): { open: string; to: string } {
  if (channel === "whatsapp") return { open: "Open WhatsApp", to: "the WhatsApp number" };
  if (channel === "email") return { open: "Write the email", to: "the address (as the subject)" };
  return { open: "Open the bot in Telegram", to: "the bot" };
}
