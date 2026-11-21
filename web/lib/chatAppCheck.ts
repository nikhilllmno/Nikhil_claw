// The test of a chat app in the Slack section of the settings console (M7.10): the text that the
// screen shows after `POST /orgs/{org_id}/integrations/{kind}/check`.

import type { ChatChannel, IntegrationCheck, IntegrationSummary } from "./api";

export const CHAT_APP_TITLES: Record<ChatChannel, string> = {
  telegram: "Telegram",
  slack: "Slack",
  whatsapp: "WhatsApp",
  email: "Email",
};

/** The chat apps of the org that work, so the screen offers a test for each. */
export function activeChatApps(integrations: readonly IntegrationSummary[]): ChatChannel[] {
  const kinds: ChatChannel[] = [];
  for (const i of integrations) {
    if ((i.kind === "telegram" || i.kind === "slack") && i.status === "active" && !kinds.includes(i.kind)) {
      kinds.push(i.kind);
    }
  }
  return kinds;
}

export interface CheckNotice {
  ok: boolean;
  text: string;
}

/** The text after a test. A failure gives the detail of the app when it has one. */
export function checkNotice(kind: ChatChannel, check: Pick<IntegrationCheck, "ok" | "detail">): CheckNotice {
  const app = CHAT_APP_TITLES[kind];
  const detail = check.detail ? ` ${check.detail}` : "";
  return check.ok
    ? { ok: true, text: `The ${app} app answers.${detail}` }
    : { ok: false, text: `The ${app} app does not answer.${detail}` };
}
