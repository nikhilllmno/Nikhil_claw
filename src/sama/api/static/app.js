// The minimal sama web page (docs/architecture.md, Section 11.4).
// No build step and no third-party code. Sign-in uses the Supabase Auth REST API.
// The event stream uses fetch, because EventSource cannot send the Authorization header.
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  config: null,
  auth: null, // {access_token, refresh_token, expires_at}
  orgs: [],
  sessionId: null,
  stream: null, // AbortController of the event stream
  openRuns: new Set(),
  signOuts: 0, // grows at each sign-out, so that late answers for the old user are dropped
};
const AUTH_KEY = "sama.auth";
// The sign-in link uses PKCE. The page that asks for the link keeps the verifier, so only this
// browser can use the code in the link. A link from another person cannot sign this browser in.
const PKCE_KEY = "sama.pkce";
const PKCE_MAX_AGE_MS = 60 * 60 * 1000;
const OPEN_STATUSES = new Set(["queued", "running", "paused"]);

function showError(message) {
  $("error").textContent = message || "";
}

// -- sign-in ----------------------------------------------------------------------------------

function saveAuth(data) {
  const expiresIn = Number(data.expires_in || 3600);
  state.auth = {
    access_token: data.access_token,
    refresh_token: data.refresh_token || null,
    expires_at: Date.now() + expiresIn * 1000,
  };
  sessionStorage.setItem(AUTH_KEY, JSON.stringify(state.auth));
}

function clearAuth() {
  state.auth = null;
  sessionStorage.removeItem(AUTH_KEY);
}

async function supabaseAuth(path, body) {
  const { supabase_url: url, supabase_anon_key: key } = state.config;
  if (!url || !key) throw new Error("Supabase sign-in is not configured. Use an access token.");
  const response = await fetch(`${url}/auth/v1/${path}`, {
    method: "POST",
    headers: { apikey: key, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error_description || data.msg || data.message || "Sign-in failed.");
  }
  return data;
}

async function freshToken() {
  const auth = state.auth;
  if (!auth) throw new Error("Sign in first.");
  if (auth.refresh_token && Date.now() > auth.expires_at - 60_000) {
    try {
      saveAuth(await supabaseAuth("token?grant_type=refresh_token", {
        refresh_token: auth.refresh_token,
      }));
    } catch (error) {
      signOut();
      throw error;
    }
  }
  return state.auth.access_token;
}

async function api(path, options = {}) {
  const headers = { Authorization: `Bearer ${await freshToken()}` };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  });
  if (response.status === 401) {
    signOut();
    throw new Error("Your sign-in ended. Sign in again.");
  }
  if (options.raw) return response;
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `The request failed (${response.status}).`);
  }
  return data;
}

function signOut() {
  stopStream();
  clearAuth();
  state.sessionId = null;
  // A pairing code is a secret of this user: the next user must not see it.
  state.signOuts += 1;
  for (const prefix of Object.values(CHANNELS)) {
    $(`${prefix}-code`).replaceChildren();
    $(`${prefix}-links`).replaceChildren();
  }
  $("int-orgs").replaceChildren();
  slackSetups.clear();
  render();
}

// -- sessions and messages ----------------------------------------------------------------------

function fill(select, items) {
  select.replaceChildren(...items.map((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.name;
    return option;
  }));
}

function fillProfiles() {
  const workspace = $("workspace").value;
  const profiles = [];
  for (const org of state.orgs) {
    if (!org.workspaces.some((w) => w.id === workspace)) continue;
    for (const p of org.agent_profiles) {
      if (p.workspace_id === null || p.workspace_id === workspace) profiles.push(p);
    }
  }
  fill($("profile"), profiles);
}

async function loadMe() {
  const me = await api("/me");
  state.orgs = me.orgs;
  const names = state.orgs.map((o) => `${o.display_name || ""} (${o.name})`);
  $("who").textContent = names.join(", ");
  const workspaces = state.orgs.flatMap((o) =>
    o.workspaces.map((w) => ({ id: w.id, name: state.orgs.length > 1 ? `${o.name} / ${w.name}` : w.name })));
  fill($("workspace"), workspaces);
  fillProfiles();
  showPairing();
}

// The org of the workspace that is selected.
function selectedOrg() {
  const workspace = $("workspace").value;
  return state.orgs.find((o) => o.workspaces.some((w) => w.id === workspace)) || null;
}

// The chat channels that have a pairing panel (element id = channel) and the id prefix of its
// parts. The panel shows when the org of the selected workspace has an active integration of
// the channel.
const CHANNELS = { telegram: "tg", slack: "sl" };

function showPairing() {
  const org = selectedOrg();
  for (const [kind, prefix] of Object.entries(CHANNELS)) {
    $(kind).hidden =
      !org || !(org.integrations || []).some((i) => i.kind === kind && i.status === "active");
    $(`${prefix}-code`).replaceChildren();
  }
}

async function loadSessions() {
  const sessions = await api("/sessions");
  $("sessions").replaceChildren(...sessions.map((s) => {
    const item = document.createElement("li");
    item.textContent = s.title || `Session ${s.id.slice(0, 8)}`;
    item.setAttribute("aria-current", String(s.id === state.sessionId));
    item.addEventListener("click", () => openSession(s.id).catch((e) => showError(e.message)));
    return item;
  }));
}

function messageItem(role, text) {
  const item = document.createElement("li");
  item.className = `msg ${role}`;
  item.textContent = text;
  return item;
}

async function loadMessages() {
  const messages = await api(`/sessions/${state.sessionId}/messages`);
  $("messages").replaceChildren(...messages.map((m) => messageItem(m.role, m.text)));
  $("live").textContent = "";
  $("live").hidden = true;
}

async function loadApprovals() {
  const approvals = await api(`/sessions/${state.sessionId}/approvals`);
  $("approvals").replaceChildren(...approvals.map(approvalCard));
}

function approvalCard(approval) {
  const card = document.createElement("div");
  card.className = "card";
  const title = document.createElement("strong");
  title.textContent = `The agent wants to use ${approval.tool_name}`;
  const reason = document.createElement("p");
  reason.textContent = approval.reason || "";
  const args = document.createElement("pre");
  args.textContent = JSON.stringify(approval.arguments, null, 2);
  const row = document.createElement("div");
  row.className = "row";
  const decide = (action, body) => async () => {
    row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try {
      await api(`/approvals/${approval.id}/${action}`, { method: "POST", body });
      card.remove();
    } catch (error) {
      showError(error.message);
      await loadApprovals();
    }
  };
  for (const [label, action, body] of [
    ["Approve", "approve", { always_allow: false }],
    ["Always allow", "approve", { always_allow: true }],
    ["Deny", "deny", {}],
  ]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", decide(action, body));
    row.append(button);
  }
  card.append(title, reason, args, row);
  return card;
}

async function loadQuestions() {
  const requests = await api(`/sessions/${state.sessionId}/questions`);
  $("questions").replaceChildren(...requests.map(questionCard));
}

function questionCard(request) {
  const form = document.createElement("form");
  form.className = "card";
  const inputs = request.questions.map((q) => {
    const label = document.createElement("label");
    label.textContent = q.question;
    let input;
    if (q.choices.length) {
      input = document.createElement("select");
      for (const choice of q.choices) {
        const option = document.createElement("option");
        option.textContent = choice;
        input.append(option);
      }
    } else {
      input = document.createElement("input");
      input.required = true;
    }
    label.append(input);
    form.append(label);
    return input;
  });
  const button = document.createElement("button");
  button.type = "submit";
  button.textContent = "Answer";
  form.append(button);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api(`/sessions/${state.sessionId}/questions/${encodeURIComponent(request.id)}/answer`, {
        method: "POST",
        body: { answers: inputs.map((i) => i.value) },
      });
      form.remove();
    } catch (error) {
      showError(error.message);
    }
  });
  return form;
}

async function openSession(id) {
  stopStream();
  state.sessionId = id;
  state.openRuns.clear();
  const detail = await api(`/sessions/${id}`);
  $("session-title").textContent = detail.title || `Session ${id.slice(0, 8)}`;
  for (const run of detail.runs) {
    if (OPEN_STATUSES.has(run.status)) state.openRuns.add(run.id);
  }
  render();
  await Promise.all([loadMessages(), loadApprovals(), loadQuestions(), loadSessions()]);
  startStream(detail.last_event_id);
}

// -- the event stream --------------------------------------------------------------------------

function stopStream() {
  if (state.stream) state.stream.abort();
  state.stream = null;
}

function startStream(after) {
  const controller = new AbortController();
  state.stream = controller;
  const sessionId = state.sessionId;
  let cursor = after;
  (async () => {
    while (!controller.signal.aborted && state.sessionId === sessionId) {
      try {
        const response = await api(`/sessions/${sessionId}/events?after=${cursor}`, {
          raw: true,
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`The event stream failed (${response.status}).`);
        const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += value;
          let end;
          while ((end = buffer.indexOf("\n\n")) >= 0) {
            const block = buffer.slice(0, end);
            buffer = buffer.slice(end + 2);
            const event = parseEvent(block);
            if (event.id !== null) cursor = event.id;
            if (event.name === "end") return;
            if (event.data !== null) handleEvent(event.name, event.data);
          }
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        showError(error.message);
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  })();
}

function parseEvent(block) {
  const event = { id: null, name: "message", data: null };
  const data = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue;
    const at = line.indexOf(":");
    const field = at < 0 ? line : line.slice(0, at);
    const value = at < 0 ? "" : line.slice(at + 1).replace(/^ /, "");
    if (field === "id") event.id = Number(value);
    else if (field === "event") event.name = value;
    else if (field === "data") data.push(value);
  }
  if (data.length) event.data = JSON.parse(data.join("\n"));
  return event;
}

function applyRun(data) {
  const open = OPEN_STATUSES.has(data.status);
  const wasOpen = state.openRuns.has(data.run_id);
  if (open) state.openRuns.add(data.run_id);
  else state.openRuns.delete(data.run_id);
  if (data.status === "paused") {
    $("run-status").textContent = "The run waits for a decision.";
    loadApprovals().catch((e) => showError(e.message));
  } else if (open) {
    $("run-status").textContent = "";
  } else if (wasOpen) {
    // Show the end of a run only once: the end of a run that the page did not show as open
    // is old news.
    $("run-status").textContent = data.failure_message
      ? `The run ${data.status}: ${data.failure_message}`
      : "";
    loadMessages().catch((e) => showError(e.message));
  }
}

function handleEvent(name, data) {
  if (name === "runs") {
    // The first event of each stream. A run that the page shows as open but that is not open
    // here ended while the page was not connected.
    const known = new Map(data.runs.map((run) => [run.run_id, run]));
    for (const runId of [...state.openRuns]) {
      if (!known.has(runId)) applyRun({ run_id: runId, status: "ended" });
    }
    for (const run of data.runs) applyRun(run);
    $("cancel").hidden = state.openRuns.size === 0;
    return;
  }
  if (name === "run") {
    applyRun(data);
    $("cancel").hidden = state.openRuns.size === 0;
    return;
  }
  if (name !== "run_event") return;
  const live = $("live");
  const payload = data.payload || {};
  switch (data.type) {
    case "text_delta":
      live.hidden = false;
      live.textContent += payload.text || "";
      break;
    case "tool_call_started":
      $("messages").append(messageItem("tool", `Tool: ${payload.name}`));
      break;
    case "approval_required":
      loadApprovals().catch((e) => showError(e.message));
      break;
    case "run_completed":
    case "run_failed":
      loadMessages().catch((e) => showError(e.message));
      break;
    default:
      break;
  }
}

// -- page --------------------------------------------------------------------------------------

function render() {
  const signedIn = state.auth !== null;
  $("login").hidden = signedIn;
  $("app").hidden = !signedIn;
  $("sign-out").hidden = !signedIn;
  if (!signedIn) $("who").textContent = "";
  $("empty").hidden = state.sessionId !== null;
  $("session").hidden = state.sessionId === null;
  $("cancel").hidden = state.openRuns.size === 0;
}

async function start() {
  showError("");
  render();
  if (!state.auth) return;
  await loadMe();
  await loadSessions();
  await loadLinks();
  await loadIntegrations();
}

// -- integrations -------------------------------------------------------------------------------

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function loadIntegrations() {
  const panel = $("integrations");
  panel.hidden = !state.config.integrations;
  if (panel.hidden) return;
  const signOut0 = state.signOuts;
  const blocks = [];
  for (const org of state.orgs) {
    // Admins read the full list, which has the last error. The others use /me.
    const items = org.admin ? await api(`/orgs/${org.id}/integrations`) : org.integrations || [];
    if (signOut0 !== state.signOuts) return;
    blocks.push(orgIntegrations(org, items));
  }
  $("int-orgs").replaceChildren(...blocks);
}

function orgIntegrations(org, items) {
  const block = element("div", undefined, "integration");
  block.append(element("h3", org.name));
  const telegram = items.find((i) => i.kind === "telegram") || null;
  if (telegram === null) {
    block.append(element("p", "Telegram: not connected."));
  } else if (telegram.status === "active") {
    block.append(element("p", `Telegram: @${telegram.name} is connected.`));
  } else {
    const error = telegram.last_error ? ` ${telegram.last_error}` : "";
    block.append(element("p", `Telegram: @${telegram.name || "bot"} is ${telegram.status}.${error}`,
      telegram.status === "failed" ? "failed" : undefined));
  }
  const slack = items.find((i) => i.kind === "slack") || null;
  if (slack === null) {
    block.append(element("p", "Slack: not connected."));
  } else if (slack.status === "active") {
    block.append(element("p", `Slack: the workspace ${slack.name} is connected.`));
  } else {
    block.append(element("p", "Slack: the setup is not complete."));
  }
  if (!org.admin) {
    block.append(element("p", "Ask an admin of the org to connect integrations.", "hint"));
    return block;
  }
  const telegramPart = element("div");
  telegramPart.append(element("h4", "Telegram"), telegramForm(org, telegram !== null));
  if (telegram !== null) {
    telegramPart.append(disconnectButton(org, "telegram", "Disconnect Telegram",
      `Disconnect @${telegram.name || "bot"}? All Telegram links of the org stop.`));
  }
  const slackPart = element("div");
  slackPart.append(element("h4", "Slack"), slackSetup(org, slack));
  if (slack !== null) {
    slackPart.append(slack.status === "pending"
      ? disconnectButton(org, "slack", "Remove the Slack setup",
        "Remove the Slack setup? The request URLs of this setup stop.")
      : disconnectButton(org, "slack", "Disconnect Slack",
        "Disconnect the Slack app? sama revokes its token, and all Slack links of the org stop."));
  }
  block.append(telegramPart, slackPart);
  return block;
}

function disconnectButton(org, kind, label, question) {
  const remove = element("button", label);
  remove.type = "button";
  remove.addEventListener("click", async () => {
    if (!confirm(question)) return;
    try {
      await api(`/orgs/${org.id}/integrations/${kind}`, { method: "DELETE" });
      // The URLs of the setup belong to the removed integration.
      if (kind === "slack") slackSetups.delete(org.id);
      await refreshIntegrations();
    } catch (error) {
      showError(error.message);
    }
  });
  return remove;
}

// Step 1 makes the Slack app from a manifest with the request URLs of the org. Step 2 takes
// the bot token and the signing secret of the installed app.
// The last Slack setup of each org, so that the steps stay after the panel renders again.
const slackSetups = new Map();

function slackSetup(org, slack) {
  const part = element("div");
  const urls = element("div");
  if (slackSetups.has(org.id)) urls.replaceChildren(slackSteps(slackSetups.get(org.id)));
  const start = element("button", slack === null ? "Start Slack setup" : "Show the app setup");
  start.type = "button";
  start.addEventListener("click", async () => {
    start.disabled = true;
    try {
      const signOut0 = state.signOuts;
      const setup = await api(`/orgs/${org.id}/integrations/slack/setup`, { method: "POST" });
      if (signOut0 !== state.signOuts) return;
      slackSetups.set(org.id, setup);
      urls.replaceChildren(slackSteps(setup));
      if (slack === null) await refreshIntegrations();
    } catch (error) {
      showError(error.message);
    } finally {
      start.disabled = false;
    }
  });
  part.append(start, urls);
  if (slack !== null) part.append(slackForm(org, slack.status === "active"));
  return part;
}

function slackSteps(setup) {
  const steps = element("ol");
  const create = element("li", "Open ");
  const link = element("a", "this link to make the Slack app");
  link.href = setup.manifest_link;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  create.append(link, element("span",
    ". Pick your Slack workspace. The manifest has the sama request URLs."));
  steps.append(create);
  for (const step of [
    "Click Install to Workspace and allow the app.",
    "In OAuth & Permissions, copy the Bot User OAuth Token (xoxb-...).",
    "In Basic Information, copy the Signing Secret.",
    "Paste both values below and click Connect Slack.",
  ]) steps.append(element("li", step));
  const list = element("ul", undefined, "hint");
  for (const [name, url] of [
    ["Events", setup.events_url],
    ["Interactivity", setup.interactions_url],
    ["/sama command", setup.commands_url],
  ]) list.append(element("li", `${name}: ${url}`));
  const box = element("div");
  box.append(steps, element("p", "The request URLs, if you make the app by hand:", "hint"), list);
  return box;
}

function slackForm(org, connected) {
  const form = element("form");
  const fields = [];
  for (const [name, text, hint] of [
    ["bot_token", connected ? "New bot token " : "Bot token ", "xoxb-..."],
    ["signing_secret", "Signing secret ", "32 characters"],
  ]) {
    const label = element("label", text);
    const input = element("input");
    input.type = "password";
    input.autocomplete = "off";
    input.required = true;
    input.placeholder = hint;
    input.name = name;
    label.append(input);
    fields.push([name, input]);
    form.append(label);
  }
  const submit = element("button", connected ? "Replace Slack app" : "Connect Slack");
  submit.type = "submit";
  const status = element("p", "", "hint");
  status.setAttribute("role", "status");
  form.append(submit, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (connected && !confirm("Replace the Slack app? If it is a different Slack workspace, all"
      + " Slack links of the org stop, and users must pair again.")) return;
    submit.disabled = true;
    status.textContent = "Connecting...";
    try {
      const body = Object.fromEntries(fields.map(([name, input]) => [name, input.value.trim()]));
      const result = await api(`/orgs/${org.id}/integrations/slack`, { method: "POST", body });
      for (const [, input] of fields) input.value = "";
      status.textContent = `The workspace ${result.name} is connected.`;
      await refreshIntegrations();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  });
  return form;
}

function telegramForm(org, connected) {
  const form = element("form");
  const steps = element("ol");
  for (const step of [
    "In Telegram, open @BotFather.",
    "Send /newbot and follow the steps.",
    "Copy the token that @BotFather gives you and paste it here.",
  ]) steps.append(element("li", step));
  const label = element("label", connected ? "New bot token " : "Bot token ");
  const input = element("input");
  input.type = "password";
  input.autocomplete = "off";
  input.required = true;
  input.placeholder = "123456789:AA...";
  label.append(input);
  const submit = element("button", connected ? "Replace bot" : "Connect Telegram");
  submit.type = "submit";
  const status = element("p", "", "hint");
  status.setAttribute("role", "status");
  form.append(steps, label, submit, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (connected && !confirm("Replace the bot? If it is a different bot, all Telegram links of"
      + " the org stop, and users must pair again.")) return;
    submit.disabled = true;
    status.textContent = "Connecting...";
    try {
      const result = await api(`/orgs/${org.id}/integrations/telegram`, {
        method: "POST",
        body: { bot_token: input.value.trim() },
      });
      input.value = "";
      status.textContent = result.status === "active"
        ? `@${result.name} is connected.`
        : `@${result.name} is ${result.status}. ${result.last_error || ""}`;
      await refreshIntegrations();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  });
  return form;
}

// /me has the integrations of each org, which show or hide the pairing panel.
async function refreshIntegrations() {
  const workspace = $("workspace").value;
  await loadMe();
  if ([...$("workspace").options].some((o) => o.value === workspace)) {
    $("workspace").value = workspace;
    fillProfiles();
    showPairing();
  }
  await loadLinks();
  await loadIntegrations();
}

// -- pairing ------------------------------------------------------------------------------------

async function loadLinks() {
  for (const kind of Object.keys(CHANNELS)) await loadChannelLinks(kind);
}

async function loadChannelLinks(kind) {
  const signOut0 = state.signOuts;
  const links = await api(`/channels/${kind}/identities`);
  if (signOut0 !== state.signOuts) return;
  $(`${CHANNELS[kind]}-links`).replaceChildren(...links.map((link) => {
    const item = document.createElement("li");
    const org = state.orgs.find((o) => o.id === link.org_id);
    item.textContent = `Linked account (${org ? org.name : "org"}) `;
    const unlink = document.createElement("button");
    unlink.type = "button";
    unlink.textContent = "Unlink";
    unlink.addEventListener("click", async () => {
      try {
        await api(`/channels/${kind}/identities/${link.id}`, { method: "DELETE" });
        await loadChannelLinks(kind);
      } catch (error) {
        showError(error.message);
      }
    });
    item.append(unlink);
    return item;
  }));
}

function pairingText(kind, codeKind, result) {
  if (kind === "slack") {
    return codeKind === "user"
      ? `Open the sama app in Slack and send ${result.code} in a direct message`
        + ` (or ${result.command}). `
      : `Invite the sama app to the channel and send ${result.command} in the channel. `;
  }
  return codeKind === "user"
    ? `Open the link, or send /start ${result.code} to @${result.bot_username}. `
    : `Open the link and pick the group, or add @${result.bot_username} to the group and send`
      + ` /pair ${result.code} in the group. `;
}

async function pairingCode(kind, codeKind) {
  const signOut0 = state.signOuts;
  try {
    const result = await api(`/channels/${kind}/pairing-codes`, {
      method: "POST",
      body: {
        workspace_id: $("workspace").value,
        agent_profile_id: $("profile").value,
        kind: codeKind,
      },
    });
    // The user signed out while the request ran: do not show the code.
    if (signOut0 !== state.signOuts) return;
    const until = new Date(result.expires_at).toLocaleTimeString();
    const parts = [element("span", pairingText(kind, codeKind, result))];
    if (result.link) {
      const link = element("a", result.link);
      link.href = result.link;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      parts.push(link);
    }
    parts.push(element("span", ` The code expires at ${until}.`));
    $(`${CHANNELS[kind]}-code`).replaceChildren(...parts);
  } catch (error) {
    showError(error.message);
  }
}

function base64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

async function newPkce() {
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  // localStorage, because the sign-in link can open in a new tab.
  localStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, created: Date.now() }));
  return base64url(digest);
}

async function readSignInLink() {
  // Never use tokens from the URL fragment: anyone can put tokens into a link.
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  const code = new URLSearchParams(location.search).get("code");
  if (!code) return false;
  history.replaceState(null, "", location.pathname);
  const saved = JSON.parse(localStorage.getItem(PKCE_KEY) || "null");
  localStorage.removeItem(PKCE_KEY);
  if (!saved || Date.now() - saved.created > PKCE_MAX_AGE_MS) {
    $("login-status").textContent =
      "This sign-in link is not from this browser, or it is too old. Ask for a new link.";
    return false;
  }
  saveAuth(await supabaseAuth("token?grant_type=pkce", {
    auth_code: code,
    code_verifier: saved.verifier,
  }));
  return true;
}

function bind() {
  $("password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      saveAuth(await supabaseAuth("token?grant_type=password", {
        email: $("email").value,
        password: $("password").value,
      }));
      $("password").value = "";
      await start();
    } catch (error) {
      $("login-status").textContent = error.message;
    }
  });
  $("magic-link").addEventListener("click", async () => {
    try {
      const redirect = encodeURIComponent(location.origin + location.pathname);
      await supabaseAuth(`otp?redirect_to=${redirect}`, {
        email: $("email").value,
        create_user: false,
        code_challenge: await newPkce(),
        code_challenge_method: "s256",
      });
      $("login-status").textContent = "Look in your email for the sign-in link.";
    } catch (error) {
      $("login-status").textContent = error.message;
    }
  });
  $("token-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    saveAuth({ access_token: $("token").value.trim(), expires_in: 3600 });
    $("token").value = "";
    await start().catch((e) => showError(e.message));
  });
  $("sign-out").addEventListener("click", signOut);
  for (const [kind, prefix] of Object.entries(CHANNELS)) {
    for (const codeKind of ["user", "group"]) {
      $(`${prefix}-${codeKind}`).addEventListener("click", () => pairingCode(kind, codeKind));
    }
  }
  $("workspace").addEventListener("change", () => {
    fillProfiles();
    showPairing();
  });
  $("new-session").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const session = await api("/sessions", {
        method: "POST",
        body: {
          workspace_id: $("workspace").value,
          agent_profile_id: $("profile").value,
          audience: $("audience").value,
          title: $("title").value.trim() || null,
        },
      });
      $("title").value = "";
      await openSession(session.id);
    } catch (error) {
      showError(error.message);
    }
  });
  $("send").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("text").value;
    if (!text.trim()) return;
    try {
      const { run_id: runId } = await api(`/sessions/${state.sessionId}/messages`, {
        method: "POST",
        body: { text },
      });
      $("text").value = "";
      state.openRuns.add(runId);
      $("messages").append(messageItem("user", text));
      $("live").textContent = "";
      render();
    } catch (error) {
      showError(error.message);
    }
  });
  $("cancel").addEventListener("click", async () => {
    for (const runId of [...state.openRuns]) {
      await api(`/runs/${runId}/cancel`, { method: "POST" }).catch((e) => showError(e.message));
    }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  bind();
  try {
    state.config = await (await fetch("/config")).json();
    const fromLink = await readSignInLink().catch((error) => {
      $("login-status").textContent = error.message;
      return false;
    });
    if (!fromLink) {
      const saved = sessionStorage.getItem(AUTH_KEY);
      state.auth = saved ? JSON.parse(saved) : null;
    }
    await start();
  } catch (error) {
    showError(error.message);
  }
});
