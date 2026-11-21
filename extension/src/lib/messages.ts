// The messages between the side panel and the service worker (a chrome.runtime port).
// The panel asks; the service worker answers with the same `id`. The service worker also
// pushes the connection state and the session events.

import type { AgentTab } from "./actions";
import type { SiteSetting } from "./consent";
import type { GatewayStatus } from "./gateway";
import type { PageContext, ServerFrame } from "./protocol";

export const PORT_NAME = "sama.panel";

export interface PanelState {
  apiUrl: string;
  signedIn: boolean;
  email: string | null;
  status: GatewayStatus;
  detail: string | null;
}

export type Request =
  | { call: "state" }
  | { call: "signIn"; email: string; password: string }
  | { call: "useToken"; token: string }
  | { call: "signOut" }
  | { call: "setApiUrl"; url: string }
  | { call: "api"; method: "GET" | "POST"; path: string; body?: unknown }
  | { call: "subscribe"; sessionId: string; after?: number }
  | { call: "unsubscribe"; sessionId: string }
  | { call: "send"; sessionId: string; text: string; page?: PageContext }
  | { call: "answerPrompt"; promptId: string; choice: PromptChoice }
  | { call: "agent"; action: "stop" | "resume"; tabId: number }
  | { call: "sites" }
  | { call: "removeSite"; site: string };

/** A question of the extension to the user (M5.5): a site to allow, or a sensitive action. */
export interface Prompt {
  id: string;
  kind: "site" | "action";
  runId: string;
  tabId: number;
  site: string;
  /** For an action: what the action does ("submit a form"). */
  reason: string | null;
  /** For an action: the name of the element (text of the page). */
  label: string | null;
  /** Epoch milliseconds. */
  expiresAt: number;
}

export type PromptChoice = "allow" | "once" | "deny" | "never";

export type PanelToWorker = { id: number } & Request;

export type WorkerToPanel =
  | { kind: "reply"; id: number; ok: true; value: unknown }
  | { kind: "reply"; id: number; ok: false; error: string; code: string | null; status: number | null }
  | { kind: "state"; state: PanelState }
  | { kind: "frame"; frame: ServerFrame }
  | { kind: "prompts"; prompts: Prompt[] }
  | { kind: "agent"; tabs: AgentTab[]; sites: SiteSetting[] };
