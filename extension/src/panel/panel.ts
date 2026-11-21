// The side panel: the chat of the active tab. It talks only to the service worker
// (lib/messages.ts); the service worker has the tokens and the socket. The panel reads the
// page of the tab itself, because the site access request needs the click of the user.

import {
  type Approval,
  type ChatMessage,
  chatHome,
  type ChatHome,
  chatTitle,
  type Me,
  type SessionDetail,
  type SessionOut,
} from "../lib/api";
import { chromeStore, TabBindings } from "../lib/bindings";
import { applyEvent, type Effect, initialLive, isPaused, type LiveState, openRunIds } from "../lib/chat";
import { normalizeApiUrl } from "../lib/config";
import type { PanelState, PanelToWorker, Request, WorkerToPanel } from "../lib/messages";
import { PORT_NAME } from "../lib/messages";
import {
  MAX_PAGE_TEXT,
  MAX_QUESTION,
  originPattern,
  pageContext,
  pageProblem,
  pageQuestion,
  type PageRead,
  readPage,
  splitPageMessage,
} from "../lib/page";
import type { ServerFrame } from "../lib/protocol";

// -- DOM ----------------------------------------------------------------------------------------

function el<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (node === null) throw new Error(`#${id} is missing`);
  return node as T;
}

const ui = {
  status: el("status"),
  statusText: el("status-text"),
  signOut: el<HTMLButtonElement>("sign-out"),
  signedOut: el("signed-out"),
  signedIn: el("signed-in"),
  passwordForm: el<HTMLFormElement>("password-form"),
  email: el<HTMLInputElement>("email"),
  password: el<HTMLInputElement>("password"),
  tokenForm: el<HTMLFormElement>("token-form"),
  token: el<HTMLInputElement>("token"),
  apiForm: el<HTMLFormElement>("api-form"),
  apiUrl: el<HTMLInputElement>("api-url"),
  authError: el("auth-error"),
  tabTitle: el("tab-title"),
  messages: el("messages"),
  approvals: el("approvals"),
  chatError: el("chat-error"),
  chatNotice: el("chat-notice"),
  composer: el<HTMLFormElement>("composer"),
  text: el<HTMLTextAreaElement>("text"),
  ask: el<HTMLButtonElement>("ask"),
  screenshot: el<HTMLInputElement>("screenshot"),
  stop: el<HTMLButtonElement>("stop"),
  send: el<HTMLButtonElement>("send"),
};

/** Makes an element with text. Page and agent text is untrusted: never use innerHTML. */
function node(tag: string, className: string, text?: string): HTMLElement {
  const element = document.createElement(tag);
  element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function show(element: HTMLElement, text: string | null): void {
  element.textContent = text ?? "";
  element.hidden = !text;
}

// -- the port to the service worker ---------------------------------------------------------------

class WorkerError extends Error {
  constructor(
    message: string,
    readonly code: string | null,
    readonly status: number | null,
  ) {
    super(message);
  }
}

let port: chrome.runtime.Port;
let nextId = 1;
const waiting = new Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void }>();

function connect(): void {
  port = chrome.runtime.connect({ name: PORT_NAME });
  port.onMessage.addListener((message: WorkerToPanel) => {
    if (message.kind === "reply") {
      const entry = waiting.get(message.id);
      waiting.delete(message.id);
      if (!entry) return;
      if (message.ok) entry.resolve(message.value);
      else entry.reject(new WorkerError(message.error, message.code, message.status));
    } else if (message.kind === "state") {
      void onState(message.state);
    } else {
      onFrame(message.frame);
    }
  });
  port.onDisconnect.addListener(() => {
    for (const entry of waiting.values()) entry.reject(new Error("The extension restarted. Try again."));
    waiting.clear();
    // The service worker stopped (an update or a crash): connect again; it starts again.
    setTimeout(() => {
      connect();
      if (view.sessionId) void rpc({ call: "subscribe", sessionId: view.sessionId });
    }, 500);
  });
}

function rpc<T = unknown>(request: Request): Promise<T> {
  const id = nextId++;
  return new Promise<T>((resolve, reject) => {
    waiting.set(id, { resolve: resolve as (value: unknown) => void, reject });
    port.postMessage({ id, ...request } as PanelToWorker);
  });
}

const api = <T>(path: string, method: "GET" | "POST" = "GET", body?: unknown) =>
  rpc<T>({ call: "api", method, path, body });

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

const bindings = new TabBindings(chromeStore(chrome.storage.session));

const ALL_URLS = "<all_urls>";

// -- state ----------------------------------------------------------------------------------------

interface View {
  worker: PanelState | null;
  me: Me | null;
  home: ChatHome | null;
  windowId: number | null;
  tab: chrome.tabs.Tab | null;
  sessionId: string | null;
  messages: ChatMessage[];
  approvals: Approval[];
  live: LiveState;
  busy: boolean;
  loadingChat: boolean;
}

const view: View = {
  worker: null,
  me: null,
  home: null,
  windowId: null,
  tab: null,
  sessionId: null,
  messages: [],
  approvals: [],
  live: initialLive,
  busy: false,
  loadingChat: false,
};

/** Grows at each tab change, so that a late answer for the old tab is dropped. */
let tabEpoch = 0;

const STATUS_TEXT: Record<string, string> = {
  signed_out: "Signed out",
  connecting: "Connecting",
  open: "Connected",
  reconnecting: "Reconnecting",
  refused: "Refused",
};

async function onState(state: PanelState): Promise<void> {
  const wasSignedIn = view.worker?.signedIn ?? false;
  view.worker = state;
  ui.status.dataset.status = state.status;
  ui.statusText.textContent = state.signedIn ? (STATUS_TEXT[state.status] ?? state.status) : "Signed out";
  ui.signOut.hidden = !state.signedIn;
  ui.signedOut.hidden = state.signedIn;
  ui.signedIn.hidden = !state.signedIn;
  if (document.activeElement !== ui.apiUrl) ui.apiUrl.value = state.apiUrl;
  if (!state.signedIn) {
    if (state.detail) show(ui.authError, state.detail);
    view.me = null;
    view.home = null;
    await bindTab(null);
    return;
  }
  if (state.status === "refused") show(ui.chatError, state.detail);
  if (!wasSignedIn || view.me === null) {
    show(ui.authError, null);
    await loadMe();
    await refreshTab();
  }
  renderComposer();
}

async function loadMe(): Promise<void> {
  try {
    view.me = await api<Me>("/me");
    view.home = chatHome(view.me);
    show(ui.chatError, view.home ? null : "You are not in a workspace with an agent. Ask an admin of your org.");
  } catch (error) {
    show(ui.chatError, errorText(error, "Could not load your account."));
  }
}

// -- the tab --------------------------------------------------------------------------------------

async function refreshTab(): Promise<void> {
  const [tab] = await chrome.tabs.query({ active: true, windowId: view.windowId ?? undefined });
  await bindTab(tab ?? null);
}

async function bindTab(tab: chrome.tabs.Tab | null): Promise<void> {
  view.tab = tab;
  ui.tabTitle.textContent = tab?.title || tab?.url || "No tab";
  ui.tabTitle.title = tab?.url ?? "";
  const sessionId =
    tab?.id !== undefined && view.worker?.signedIn ? await bindings.get(tab.id) : null;
  if (sessionId === view.sessionId && view.tab?.id === tab?.id) {
    renderComposer();
    return;
  }
  await openChat(sessionId);
}

async function openChat(sessionId: string | null): Promise<void> {
  const epoch = ++tabEpoch;
  if (view.sessionId) void rpc({ call: "unsubscribe", sessionId: view.sessionId }).catch(() => undefined);
  view.sessionId = sessionId;
  view.messages = [];
  view.approvals = [];
  view.live = initialLive;
  show(ui.chatError, null);
  show(ui.chatNotice, null);
  if (sessionId === null) {
    render();
    return;
  }
  view.loadingChat = true;
  render();
  try {
    const [detail, messages, approvals] = await Promise.all([
      api<SessionDetail>(`/sessions/${sessionId}`),
      api<ChatMessage[]>(`/sessions/${sessionId}/messages?after=0&limit=500`),
      api<Approval[]>(`/sessions/${sessionId}/approvals`),
    ]);
    if (epoch !== tabEpoch) return;
    view.messages = messages;
    view.approvals = approvals;
    await rpc({ call: "subscribe", sessionId, after: detail.last_event_id });
    // The open runs come with the first `runs` event of the socket.
  } catch (error) {
    if (epoch !== tabEpoch) return;
    if (error instanceof WorkerError && error.status === 404) {
      // The session is gone (removed in the web app): start a new chat for the tab.
      view.sessionId = null;
      if (view.tab?.id !== undefined) await bindings.unbind(view.tab.id);
    } else {
      show(ui.chatError, errorText(error, "Could not load the chat."));
    }
  } finally {
    if (epoch === tabEpoch) {
      view.loadingChat = false;
      render();
    }
  }
}

/** The session of the tab. A tab without one gets a new private session. */
async function ensureChat(): Promise<string> {
  if (view.sessionId) return view.sessionId;
  const tab = view.tab;
  if (!view.home || tab?.id === undefined) {
    throw new Error(view.home ? "sama cannot see this tab." : "You are not in a workspace with an agent.");
  }
  const session = await api<SessionOut>("/sessions", "POST", {
    workspace_id: view.home.workspaceId,
    agent_profile_id: view.home.agentProfileId,
    audience: "private",
    title: chatTitle(tab.title),
  });
  await bindings.bind(tab.id, session.id);
  tabEpoch += 1;
  view.sessionId = session.id;
  await rpc({ call: "subscribe", sessionId: session.id, after: 0 });
  return session.id;
}

// -- events ---------------------------------------------------------------------------------------

function onFrame(frame: ServerFrame): void {
  if (!("session_id" in frame) || frame.session_id !== view.sessionId) return;
  if (frame.type === "runs" || frame.type === "run" || frame.type === "run_event") {
    const result = applyEvent(view.live, frame.type, frame.data);
    view.live = result.state;
    void runEffects(result.effects);
    render();
  } else if (frame.type === "error" && frame.id === undefined) {
    show(ui.chatError, frame.message);
  }
}

async function runEffects(effects: Effect[]): Promise<void> {
  const sessionId = view.sessionId;
  if (!sessionId) return;
  const epoch = tabEpoch;
  try {
    if (effects.includes("reload_messages")) {
      const messages = await api<ChatMessage[]>(`/sessions/${sessionId}/messages?after=0&limit=500`);
      if (epoch === tabEpoch) view.messages = messages;
    }
    if (effects.includes("reload_approvals")) {
      const approvals = await api<Approval[]>(`/sessions/${sessionId}/approvals`);
      if (epoch === tabEpoch) view.approvals = approvals;
    }
  } catch (error) {
    if (epoch === tabEpoch) show(ui.chatError, errorText(error, "Could not load the chat."));
  }
  if (epoch === tabEpoch) {
    if (effects.includes("reload_messages")) show(ui.chatError, view.live.failure);
    render();
  }
}

// -- render ---------------------------------------------------------------------------------------

function renderMessage(message: ChatMessage): HTMLElement | null {
  if (message.role === "user") {
    const { question, url } = splitPageMessage(message.text);
    const wrap = node("div", "message user", question);
    if (url !== null) {
      wrap.append(node("div", "message page", `Shared the page ${url}`));
    }
    return wrap;
  }
  if (message.role === "assistant") return node("div", "message assistant", message.text);
  return null;
}

function render(): void {
  const list = ui.messages;
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  const children: HTMLElement[] = [];
  for (const message of view.messages) {
    const item = renderMessage(message);
    if (item) children.push(item);
  }
  for (const runId of openRunIds(view.live)) {
    for (const call of view.live.tools[runId] ?? []) {
      const mark = call.status === "running" ? "..." : call.status === "error" ? "failed" : "done";
      children.push(node("div", "tool", `${call.name} ${mark}`));
    }
    const text = view.live.text[runId];
    children.push(node("div", "message live", text || "sama is working..."));
  }
  if (children.length === 0) {
    children.push(
      node(
        "p",
        "empty",
        view.loadingChat
          ? "Loading the chat..."
          : "No chat for this tab yet. Type a message, or press Ask about this page.",
      ),
    );
  }
  list.replaceChildren(...children);
  if (atBottom) list.scrollTop = list.scrollHeight;
  renderApprovals();
  renderComposer();
}

function renderApprovals(): void {
  ui.approvals.replaceChildren(
    ...view.approvals.map((approval) => {
      const card = node("div", "approval");
      card.append(node("strong", "", `sama asks to use ${approval.tool_name}`));
      if (approval.reason) card.append(node("div", "", approval.reason));
      card.append(node("pre", "", JSON.stringify(approval.arguments, null, 2)));
      const row = node("div", "row");
      const approve = node("button", "primary", "Approve") as HTMLButtonElement;
      const deny = node("button", "", "Deny") as HTMLButtonElement;
      approve.type = "button";
      deny.type = "button";
      approve.addEventListener("click", () => void decide(approval.id, "approve"));
      deny.addEventListener("click", () => void decide(approval.id, "deny"));
      row.append(approve, deny);
      card.append(row);
      return card;
    }),
  );
}

function renderComposer(): void {
  const open = openRunIds(view.live).length > 0;
  const canSend = Boolean(view.worker?.signedIn && view.home && !view.busy);
  ui.send.disabled = !canSend;
  ui.ask.disabled = !canSend;
  ui.stop.hidden = !open;
  ui.send.textContent = open && !isPaused(view.live) ? "Queue" : "Send";
  const problem = pageProblem(view.tab?.url);
  ui.ask.title = problem ?? "Send the text of this page to sama with your question";
}

// -- actions --------------------------------------------------------------------------------------

async function withBusy(action: () => Promise<void>): Promise<void> {
  if (view.busy) return;
  view.busy = true;
  renderComposer();
  try {
    await action();
  } finally {
    view.busy = false;
    renderComposer();
  }
}

function sent(result: { queued: boolean }, note: string | null = null): void {
  const queued = result.queued ? "Queued. sama answers after the current run." : null;
  show(ui.chatNotice, [note, queued].filter(Boolean).join(" ") || null);
}

async function sendText(): Promise<void> {
  const text = ui.text.value.trim();
  if (!text) return;
  await withBusy(async () => {
    show(ui.chatError, null);
    try {
      const sessionId = await ensureChat();
      const result = await rpc<{ queued: boolean }>({ call: "send", sessionId, text });
      ui.text.value = "";
      sent(result);
      await runEffects(["reload_messages"]);
    } catch (error) {
      show(ui.chatError, errorText(error, "Could not send the message."));
    }
  });
}

async function askAboutPage(): Promise<void> {
  const tab = view.tab;
  const problem = pageProblem(tab?.url);
  if (problem || tab?.id === undefined || !tab.url) {
    show(ui.chatError, problem ?? "sama cannot see this tab.");
    return;
  }
  const question = pageQuestion(ui.text.value);
  if (question.length > MAX_QUESTION) {
    show(ui.chatError, `A question about a page can have at most ${MAX_QUESTION} characters.`);
    return;
  }
  const withScreenshot = ui.screenshot.checked;
  // The request must come first, in the click. Without a screenshot, Chrome asks the user for
  // this site only. Chrome takes a screenshot of a tab only with "<all_urls>" (or an
  // `activeTab` grant from a click on the toolbar icon), so a screenshot asks for it too.
  const origins = [originPattern(tab.url)];
  if (withScreenshot) origins.push(ALL_URLS);
  const granted = await chrome.permissions.request({ origins }).catch(() => false);
  if (!granted) {
    show(ui.chatError, "sama did not get access to this site, so it cannot read the page.");
    return;
  }
  await withBusy(async () => {
    show(ui.chatError, null);
    try {
      const [injection] = await chrome.scripting.executeScript({
        target: { tabId: tab.id as number },
        func: readPage,
        args: [MAX_PAGE_TEXT],
      });
      const read = injection?.result as PageRead | undefined;
      if (!read) throw new Error("sama could not read this page.");
      let screenshot: string | null = null;
      if (withScreenshot) {
        screenshot = await chrome.tabs
          .captureVisibleTab(tab.windowId, { format: "jpeg", quality: 70 })
          .catch(() => null);
      }
      const sessionId = await ensureChat();
      const page = pageContext(read, screenshot);
      const result = await rpc<{ queued: boolean }>({ call: "send", sessionId, text: question, page });
      ui.text.value = "";
      const lost = withScreenshot && page.screenshot === null;
      sent(result, lost ? "Chrome did not give a screenshot. sama got the text only." : null);
      await runEffects(["reload_messages"]);
    } catch (error) {
      show(ui.chatError, errorText(error, "Could not send the page."));
    }
  });
}

async function decide(approvalId: string, action: "approve" | "deny"): Promise<void> {
  try {
    await api(`/approvals/${approvalId}/${action}`, "POST", action === "approve" ? { always_allow: false } : {});
    view.approvals = view.approvals.filter((approval) => approval.id !== approvalId);
    render();
  } catch (error) {
    show(ui.chatError, errorText(error, "Could not answer the approval."));
  }
}

async function stopRuns(): Promise<void> {
  for (const runId of openRunIds(view.live)) {
    try {
      await api(`/runs/${runId}/cancel`, "POST");
    } catch (error) {
      show(ui.chatError, errorText(error, "Could not stop the run."));
    }
  }
}

async function authAction(button: HTMLButtonElement, action: () => Promise<unknown>): Promise<void> {
  button.disabled = true;
  show(ui.authError, null);
  try {
    await action();
  } catch (error) {
    show(ui.authError, errorText(error, "Sign-in failed."));
  } finally {
    button.disabled = false;
  }
}

// -- wiring ---------------------------------------------------------------------------------------

ui.passwordForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void authAction(el("password-submit"), async () => {
    await rpc({ call: "signIn", email: ui.email.value, password: ui.password.value });
    ui.password.value = "";
  });
});

ui.tokenForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void authAction(el("token-submit"), async () => {
    await rpc({ call: "useToken", token: ui.token.value });
    ui.token.value = "";
  });
});

ui.apiForm.addEventListener("submit", (event) => {
  event.preventDefault();
  let url: string;
  try {
    url = normalizeApiUrl(ui.apiUrl.value);
  } catch (error) {
    show(ui.authError, errorText(error, "The address is not valid."));
    return;
  }
  // The request must come first, in the click.
  void authAction(el("api-submit"), async () => {
    const granted = await chrome.permissions.request({ origins: [`${new URL(url).origin}/*`] });
    if (!granted) throw new Error("The extension needs access to the API address.");
    await rpc({ call: "setApiUrl", url });
  });
});

ui.signOut.addEventListener("click", () => void rpc({ call: "signOut" }));
ui.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  void sendText();
});
ui.text.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void sendText();
  }
});
ui.ask.addEventListener("click", () => void askAboutPage());
ui.stop.addEventListener("click", () => void stopRuns());

chrome.tabs.onActivated.addListener((info) => {
  if (info.windowId === view.windowId) void refreshTab();
});
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (tabId === view.tab?.id && (change.title !== undefined || change.url !== undefined)) {
    void chrome.tabs.get(tabId).then((tab) => {
      view.tab = tab;
      ui.tabTitle.textContent = tab.title || tab.url || "";
      ui.tabTitle.title = tab.url ?? "";
      renderComposer();
    });
  }
});

void chrome.windows.getCurrent().then((window) => {
  view.windowId = window.id ?? null;
  connect();
  render();
});
