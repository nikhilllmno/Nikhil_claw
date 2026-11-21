// The service worker of the extension. It owns the sign-in, the one WebSocket to the API
// (lib/gateway.ts), and the REST calls of the side panel. The panel talks to it through a
// chrome.runtime port (lib/messages.ts). No token goes to the panel or to a web page.

import { errorMessage, type PublicConfig } from "./lib/api";
import { Auth, AuthError, type SupabaseConfig } from "./lib/auth";
import { chromeStore, TabBindings } from "./lib/bindings";
import { API_URL_KEY, defaultApiUrl, normalizeApiUrl } from "./lib/config";
import { Gateway, GatewayError, type GatewayStatus } from "./lib/gateway";
import {
  type PanelState,
  type PanelToWorker,
  PORT_NAME,
  type Request,
  type WorkerToPanel,
} from "./lib/messages";
import { originPattern } from "./lib/page";
import { socketUrl } from "./lib/protocol";

const session = chromeStore(chrome.storage.session);
const local = chromeStore(chrome.storage.local);
const bindings = new TabBindings(session);

let apiUrl = defaultApiUrl();
let configCache: { url: string; value: SupabaseConfig | null } | null = null;
let status: GatewayStatus = "signed_out";
let detail: string | null = null;
const ports = new Map<chrome.runtime.Port, Set<string>>();

class ApiCallError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function supabaseConfig(): Promise<SupabaseConfig | null> {
  if (configCache?.url === apiUrl) return configCache.value;
  const response = await fetch(`${apiUrl}/config`);
  if (!response.ok) throw new AuthError("The sama API did not answer. Check the API address.");
  const data = (await response.json()) as PublicConfig;
  const value =
    data.supabase_url && data.supabase_anon_key
      ? { url: data.supabase_url.replace(/\/+$/, ""), anonKey: data.supabase_anon_key }
      : null;
  configCache = { url: apiUrl, value };
  return value;
}

const auth = new Auth({
  store: session,
  config: supabaseConfig,
  onChange: () => broadcastState(),
});

const gateway = new Gateway({
  url: () => socketUrl(apiUrl),
  token: () => auth.getToken(),
  refresh: () => auth.refresh(),
  canRefresh: () => auth.current?.refresh_token != null,
  createSocket: (url) => new WebSocket(url),
  onFrame: (frame) => broadcast({ kind: "frame", frame }),
  onStatus: (next, text) => {
    status = next;
    detail = text;
    // The server refused the token and no fresh token came: the sign-in ended.
    if (next === "signed_out" && text !== null && auth.current !== null) void auth.signOut();
    broadcastState();
  },
});

function panelState(): PanelState {
  return {
    apiUrl,
    signedIn: auth.current !== null,
    email: auth.current?.email ?? null,
    status,
    detail,
  };
}

function post(port: chrome.runtime.Port, message: WorkerToPanel): void {
  try {
    port.postMessage(message);
  } catch {
    // The panel closed.
  }
}

function broadcast(message: WorkerToPanel): void {
  for (const port of ports.keys()) post(port, message);
}

/** Stops following a session that no open panel follows. */
function unsubscribeIfUnused(sessionId: string): void {
  if (![...ports.values()].some((ids) => ids.has(sessionId))) gateway.unsubscribe(sessionId);
}

function broadcastState(): void {
  broadcast({ kind: "state", state: panelState() });
}

const ready = (async () => {
  const saved = await local.get(API_URL_KEY);
  if (typeof saved === "string") {
    try {
      apiUrl = normalizeApiUrl(saved);
    } catch {
      apiUrl = defaultApiUrl();
    }
  }
  if ((await auth.load()) !== null) gateway.start();
})();

/** A REST call with the access token. A 401 gives one refresh and one more try. */
async function callApi(method: "GET" | "POST", path: string, body?: unknown): Promise<unknown> {
  if (!path.startsWith("/") || path.startsWith("//")) throw new ApiCallError(400, "Bad path.");
  for (let retried = false; ; retried = true) {
    const token = retried ? await auth.refresh() : await auth.getToken();
    if (token === null) throw new ApiCallError(401, "Sign in first.");
    const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(`${apiUrl}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status === 401 && !retried && auth.current?.refresh_token) continue;
    const data: unknown = response.status === 204 ? null : await response.json().catch(() => null);
    if (response.status === 401) {
      await auth.signOut();
      gateway.stop();
    }
    if (!response.ok) throw new ApiCallError(response.status, errorMessage(response.status, data));
    return data;
  }
}

async function signedIn(): Promise<void> {
  await bindings.clear();
  gateway.restart();
}

async function handle(port: chrome.runtime.Port, request: Request): Promise<unknown> {
  await ready;
  switch (request.call) {
    case "state":
      return panelState();
    case "signIn":
      await auth.signInWithPassword(request.email, request.password);
      await signedIn();
      return panelState();
    case "useToken":
      await auth.useAccessToken(request.token);
      await signedIn();
      return panelState();
    case "signOut":
      gateway.stop();
      await auth.signOut();
      await bindings.clear();
      return panelState();
    case "setApiUrl": {
      const next = normalizeApiUrl(request.url);
      const granted = await chrome.permissions.contains({ origins: [originPattern(next)] });
      if (!granted) throw new Error("Give the extension access to this address first.");
      gateway.stop();
      await auth.signOut();
      await bindings.clear();
      apiUrl = next;
      configCache = null;
      await local.set(API_URL_KEY, next);
      broadcastState();
      return panelState();
    }
    case "api":
      return callApi(request.method, request.path, request.body);
    case "subscribe":
      ports.get(port)?.add(request.sessionId);
      gateway.subscribe(request.sessionId, request.after);
      return null;
    case "unsubscribe":
      ports.get(port)?.delete(request.sessionId);
      unsubscribeIfUnused(request.sessionId);
      return null;
    case "send":
      return gateway.send(request.sessionId, request.text, request.page);
  }
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== PORT_NAME || port.sender?.id !== chrome.runtime.id) {
    port.disconnect();
    return;
  }
  ports.set(port, new Set());
  port.onMessage.addListener((message: PanelToWorker) => {
    const { id, ...request } = message;
    handle(port, request as Request).then(
      (value) => post(port, { kind: "reply", id, ok: true, value: value ?? null }),
      (error: unknown) =>
        post(port, {
          kind: "reply",
          id,
          ok: false,
          error: error instanceof Error ? error.message : "Something went wrong.",
          code: error instanceof GatewayError ? error.code : null,
          status: error instanceof ApiCallError ? error.status : null,
        }),
    );
  });
  port.onDisconnect.addListener(() => {
    const ids = ports.get(port) ?? new Set<string>();
    ports.delete(port);
    for (const sessionId of ids) unsubscribeIfUnused(sessionId);
  });
  void ready.then(() => {
    if (auth.current !== null) gateway.start();
    post(port, { kind: "state", state: panelState() });
  });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  void bindings.unbind(tabId);
});

function openOnClick(): void {
  void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => undefined);
}

chrome.runtime.onInstalled.addListener(openOnClick);
chrome.runtime.onStartup.addListener(openOnClick);
