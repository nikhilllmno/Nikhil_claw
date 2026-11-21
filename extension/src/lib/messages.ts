// The messages between the side panel and the service worker (a chrome.runtime port).
// The panel asks; the service worker answers with the same `id`. The service worker also
// pushes the connection state and the session events.

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
  | { call: "send"; sessionId: string; text: string; page?: PageContext };

export type PanelToWorker = { id: number } & Request;

export type WorkerToPanel =
  | { kind: "reply"; id: number; ok: true; value: unknown }
  | { kind: "reply"; id: number; ok: false; error: string; code: string | null; status: number | null }
  | { kind: "state"; state: PanelState }
  | { kind: "frame"; frame: ServerFrame };
