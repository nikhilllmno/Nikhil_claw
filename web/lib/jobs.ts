// Helpers for the Jobs screen: labels, delivery targets, the form draft and its change to a
// JobInput, and the times. The rules for targets, skills, and models mirror the checks in
// src/sama/scheduler/spec.py. The API checks them again: these checks only give an early answer.

import type { DeliveryStatus, FireStatus, Job, JobInput, JobSchedule, PreapprovedTool } from "./api";

// -- limits (the same as in src/sama/scheduler/spec.py) -----------------------------------------

export const MAX_NAME_CHARS = 200;
export const MAX_PROMPT_CHARS = 20_000;
export const MAX_SCRIPT_CHARS = 20_000;
export const MAX_SKILLS = 20;
export const MAX_PREAPPROVALS = 20;
export const MAX_TARGET_CHARS = 300;
export const MAX_TZ_CHARS = 64;

const SLACK_CHANNEL = /^#?[A-Za-z0-9._-]{1,80}$/;
const TELEGRAM_CHAT = /^-?\d{1,20}$/;
const EMAIL = /^[^@\s]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$/;
const SKILL = /^[a-z0-9][a-z0-9._-]{0,99}$/;
const MODEL = /^[A-Za-z0-9._:/-]{1,100}$/;

// -- labels -------------------------------------------------------------------------------------

export type Tone = "accent" | "warning" | "danger" | "neutral";

export function statusLabel(status: string): string {
  switch (status) {
    case "active":
      return "Active";
    case "paused":
      return "Paused";
    case "pending_approval":
      return "Needs approval";
    case "removed":
      return "Removed";
    default:
      return status;
  }
}

export function statusTone(status: string): Tone {
  switch (status) {
    case "active":
      return "accent";
    case "pending_approval":
      return "warning";
    default:
      return "neutral";
  }
}

export function fireStatusLabel(status: FireStatus): string {
  switch (status) {
    case "running":
      return "Running";
    case "completed":
      return "Completed";
    case "failed":
      return "Failed";
    case "skipped":
      return "Skipped";
    default:
      return status;
  }
}

export function fireStatusTone(status: FireStatus): Tone {
  switch (status) {
    case "completed":
      return "accent";
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}

export function deliveryStatusLabel(status: DeliveryStatus): string {
  switch (status) {
    case "pending":
      return "Waiting to send";
    case "sending":
      return "Sending";
    case "sent":
      return "Sent";
    case "failed":
      return "Not sent";
    case "unknown":
      return "Not known if sent";
    default:
      return status;
  }
}

export function deliveryStatusTone(status: DeliveryStatus): Tone {
  switch (status) {
    case "sent":
      return "accent";
    case "failed":
      return "danger";
    case "unknown":
      return "warning";
    default:
      return "neutral";
  }
}

/** A reason of the API (for example "it sends to slack:#ops") as a sentence. */
export function approvalReasonText(reason: string): string {
  const text = reason.trim().replace(/\.+$/, "");
  if (text === "") return "";
  return `${text[0]?.toUpperCase() ?? ""}${text.slice(1)}.`;
}

/** The sentences that say why a job waits for approval. */
export function approvalNotice(job: Pick<Job, "version" | "approval_reasons" | "created_by_kind">): {
  summary: string;
  reasons: string[];
} {
  const maker = job.created_by_kind === "agent" ? "An agent made" : "Someone made";
  return {
    summary: `${maker} version ${job.version} of this job. It does not run until you approve it.`,
    reasons: job.approval_reasons.map(approvalReasonText).filter((reason) => reason !== ""),
  };
}

/** `failures` failed fires in a row, as a sentence. Empty when there is none. */
export function failureNotice(failures: number): string {
  if (failures <= 0) return "";
  return failures === 1 ? "The last fire failed." : `The last ${failures} fires failed.`;
}

/** The pending jobs first, then the active ones by next fire, then the paused ones, then the rest. */
export function sortJobs(jobs: readonly Job[]): Job[] {
  const rank = (job: Job) => ["pending_approval", "active", "paused"].indexOf(job.status);
  const order = (job: Job) => (rank(job) === -1 ? 3 : rank(job));
  const when = (job: Job) => (job.next_fire_at === null ? Infinity : Date.parse(job.next_fire_at));
  return [...jobs].sort(
    (a, b) =>
      order(a) - order(b) ||
      (when(a) === when(b) ? 0 : when(a) < when(b) ? -1 : 1) ||
      a.name.localeCompare(b.name),
  );
}

// -- actions ------------------------------------------------------------------------------------

export type JobAction = "pause" | "resume" | "run" | "cancel" | "remove";

/** The actions that the screen offers for a job in this status. */
export function availableActions(status: string): JobAction[] {
  switch (status) {
    case "active":
      return ["run", "pause", "cancel", "remove"];
    case "paused":
      return ["run", "resume", "cancel", "remove"];
    case "pending_approval":
      return ["cancel", "remove"];
    default:
      return [];
  }
}

// -- times --------------------------------------------------------------------------------------

/**
 * A time for a person. It uses the time zone `tz` when it is a valid one, else the time zone of
 * the browser. An empty or bad `iso` gives an empty text.
 */
export function formatWhen(iso: string | null | undefined, tz?: string | null, locale?: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const options: Intl.DateTimeFormatOptions = {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  };
  try {
    return new Intl.DateTimeFormat(locale, { ...options, timeZone: tz || undefined }).format(date);
  } catch {
    return new Intl.DateTimeFormat(locale, options).format(date);
  }
}

/** The IANA zone of the browser, or `UTC`. */
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** A schedule as text that the user can edit and the API can read again. */
export function scheduleToText(schedule: JobSchedule, fallback: string): string {
  if (schedule.kind === "cron" && schedule.expr) return schedule.expr;
  if (schedule.kind === "interval" && typeof schedule.every_s === "number") {
    const every = schedule.every_s;
    for (const [size, name] of [
      [86400, "day"],
      [3600, "hour"],
      [60, "minute"],
    ] as const) {
      if (every % size === 0) return `every ${every / size} ${name}${every / size === 1 ? "" : "s"}`;
    }
    return `every ${every} seconds`;
  }
  return fallback;
}

// -- delivery targets ---------------------------------------------------------------------------

export type TargetResult = { ok: true; value: string } | { ok: false; error: string };

/** The stored form of a target. The same rules as `parse_target` in the API. */
export function parseTarget(text: string, field = "deliver"): TargetResult {
  const value = text.trim();
  if (value === "origin" || value === "none") return { ok: true, value };
  const colon = value.indexOf(":");
  const kind = (colon === -1 ? value : value.slice(0, colon)).trim().toLowerCase();
  const rest = colon === -1 ? "" : value.slice(colon + 1).trim();
  if (kind === "slack" && SLACK_CHANNEL.test(rest)) return { ok: true, value: `slack:${rest}` };
  if (kind === "telegram" && TELEGRAM_CHAT.test(rest)) return { ok: true, value: `telegram:${rest}` };
  if (kind === "email" && EMAIL.test(rest)) return { ok: true, value: `email:${rest.toLowerCase()}` };
  return {
    ok: false,
    error: `${field} must be origin, none, slack:<channel>, telegram:<chat id>, or email:<address>.`,
  };
}

/** A target as words for a person. */
export function formatTarget(target: string): string {
  if (target === "origin") return "The chat of the job";
  if (target === "none") return "No delivery";
  const result = parseTarget(target);
  if (!result.ok) return target;
  const colon = result.value.indexOf(":");
  const rest = result.value.slice(colon + 1);
  switch (result.value.slice(0, colon)) {
    case "slack":
      return `Slack ${rest.startsWith("#") ? rest : `#${rest}`}`;
    case "telegram":
      return `Telegram chat ${rest}`;
    default:
      return `Email ${rest}`;
  }
}

// -- the form -----------------------------------------------------------------------------------

export type JobMode = "agent" | "script";

/** The text of each field of the form. */
export interface JobDraft {
  name: string;
  schedule: string;
  tz: string;
  prompt: string;
  deliver: string;
  failureDeliver: string;
  model: string;
  attachToSession: boolean;
  mode: JobMode;
  script: string;
  /** Skill names, separated by commas. */
  skills: string;
  /** JSON: a list of `{tool, connection?, limits}`. Empty: none. */
  preapprovedTools: string;
  agentProfileId: string;
}

export function newDraft(tz: string, agentProfileId = ""): JobDraft {
  return {
    name: "",
    schedule: "",
    tz,
    prompt: "",
    deliver: "origin",
    failureDeliver: "",
    model: "",
    attachToSession: false,
    mode: "agent",
    script: "",
    skills: "",
    preapprovedTools: "",
    agentProfileId,
  };
}

export function toDraft(job: Job): JobDraft {
  return {
    name: job.name,
    schedule: scheduleToText(job.schedule, job.schedule_text),
    tz: job.schedule.tz ?? "UTC",
    prompt: job.prompt,
    deliver: job.deliver,
    failureDeliver: job.failure_deliver ?? "",
    model: job.model ?? "",
    attachToSession: job.attach_to_session,
    mode: job.mode === "script" ? "script" : "agent",
    script: job.script ?? "",
    skills: job.skills.join(", "),
    preapprovedTools: job.preapproved_tools.length > 0 ? JSON.stringify(job.preapproved_tools, null, 2) : "",
    agentProfileId: job.agent_profile_id,
  };
}

export function parseSkills(text: string): string[] {
  return [...new Set(text.split(/[,\s]+/).filter((name) => name !== ""))];
}

export type ToolsResult = { ok: true; value: PreapprovedTool[] } | { ok: false; error: string };

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** The pre-approved tools of the JSON text. Empty text gives an empty list. */
export function parsePreapprovedTools(text: string): ToolsResult {
  if (text.trim() === "") return { ok: true, value: [] };
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return { ok: false, error: "Pre-approved tools must be valid JSON." };
  }
  if (!Array.isArray(data)) return { ok: false, error: "Pre-approved tools must be a list." };
  if (data.length > MAX_PREAPPROVALS) {
    return { ok: false, error: `A job can pre-approve at most ${MAX_PREAPPROVALS} tools.` };
  }
  const tools: PreapprovedTool[] = [];
  for (const [index, item] of data.entries()) {
    const label = `Pre-approved tool ${index + 1}`;
    if (!isObject(item)) return { ok: false, error: `${label} must be an object.` };
    const extra = Object.keys(item).find((key) => !["tool", "connection", "limits"].includes(key));
    if (extra !== undefined) return { ok: false, error: `${label} has an unknown field: ${extra}.` };
    if (typeof item.tool !== "string" || item.tool.trim() === "") {
      return { ok: false, error: `${label} needs a tool name.` };
    }
    if (item.connection !== undefined && item.connection !== null && typeof item.connection !== "string") {
      return { ok: false, error: `${label}: connection must be text.` };
    }
    const limits: PreapprovedTool["limits"] = {};
    if (item.limits !== undefined) {
      if (!isObject(item.limits)) return { ok: false, error: `${label}: limits must be an object.` };
      for (const [key, values] of Object.entries(item.limits)) {
        const valid =
          Array.isArray(values) &&
          values.length > 0 &&
          values.every((v) => ["string", "number", "boolean"].includes(typeof v));
        if (!valid) {
          return { ok: false, error: `${label}: the limit ${key} must be a list of values.` };
        }
        limits[key] = values as (string | number | boolean)[];
      }
    }
    const tool: PreapprovedTool = { tool: item.tool.trim(), limits };
    if (typeof item.connection === "string") tool.connection = item.connection;
    tools.push(tool);
  }
  return { ok: true, value: tools };
}

/** The first problem of the draft as a sentence, or `null` when the draft is valid. */
export function validateDraft(draft: JobDraft): string | null {
  const name = draft.name.trim();
  if (name.length > MAX_NAME_CHARS) return `The name has more than ${MAX_NAME_CHARS} characters.`;
  if (draft.schedule.trim() === "") return "Write when the job runs.";
  if (draft.tz.trim() === "") return "Write a time zone, for example UTC.";
  if (draft.tz.trim().length > MAX_TZ_CHARS) return "The time zone is too long.";
  if (draft.mode === "agent") {
    if (draft.prompt.trim() === "") return "Write a prompt for the agent.";
  } else if (draft.script.trim() === "") {
    return "Write a script.";
  }
  if (draft.prompt.length > MAX_PROMPT_CHARS)
    return `The prompt has more than ${MAX_PROMPT_CHARS} characters.`;
  if (draft.script.length > MAX_SCRIPT_CHARS)
    return `The script has more than ${MAX_SCRIPT_CHARS} characters.`;
  for (const [field, text] of [
    ["deliver", draft.deliver],
    ["failure_deliver", draft.failureDeliver],
  ] as const) {
    if (text.trim() === "") continue;
    if (text.length > MAX_TARGET_CHARS) return `${field} is too long.`;
    const result = parseTarget(text, field);
    if (!result.ok) return result.error;
  }
  const model = draft.model.trim();
  if (model !== "" && !MODEL.test(model)) return "The model name is not valid.";
  const skills = parseSkills(draft.skills);
  if (skills.length > MAX_SKILLS) return `A job can use at most ${MAX_SKILLS} skills.`;
  const bad = skills.find((name) => !SKILL.test(name));
  if (bad !== undefined) return `The skill name ${bad} is not valid.`;
  const tools = parsePreapprovedTools(draft.preapprovedTools);
  if (!tools.ok) return tools.error;
  if (draft.mode === "script" && tools.value.length > 0) return "A script job has no tools to pre-approve.";
  return null;
}

/** The ids that a new job needs and an update does not change. */
export interface JobScope {
  workspaceId?: string;
}

const same = (a: string, b: string) => a.trim() === b.trim();

/**
 * The body for the API. A new job (`initial` is `null`) gets every field that has a value. An
 * update gets only the fields that differ from `initial`, so the API keeps the other ones and
 * a version that needs no new approval stays approved. The caller checks the draft first.
 */
export function buildInput(draft: JobDraft, initial: JobDraft | null, scope: JobScope = {}): JobInput {
  const input: JobInput = {};
  const base = initial ?? newDraft("");
  const creating = initial === null;

  if (creating ? draft.name.trim() !== "" : !same(draft.name, base.name)) input.name = draft.name.trim();

  const scheduleChanged = creating || !same(draft.schedule, base.schedule);
  if (scheduleChanged) {
    input.schedule = draft.schedule.trim();
    input.tz = draft.tz.trim();
  } else if (!same(draft.tz, base.tz)) {
    input.tz = draft.tz.trim();
  }

  if (draft.mode !== base.mode || creating) input.mode = draft.mode;
  if (!same(draft.prompt, base.prompt)) input.prompt = draft.prompt.trim();
  if (draft.mode === "script" && (creating || !same(draft.script, base.script) || base.mode !== "script")) {
    input.script = draft.script;
  }

  // An empty target is sent on an update: the API goes back to `origin` (deliver) or to none.
  if (creating ? draft.deliver.trim() !== "" : !same(draft.deliver, base.deliver)) {
    input.deliver = parseTargetOrText(draft.deliver);
  }
  if (creating ? draft.failureDeliver.trim() !== "" : !same(draft.failureDeliver, base.failureDeliver)) {
    input.failure_deliver = parseTargetOrText(draft.failureDeliver);
  }
  if (creating ? draft.model.trim() !== "" : !same(draft.model, base.model)) input.model = draft.model.trim();
  if (draft.attachToSession !== base.attachToSession || (creating && draft.attachToSession)) {
    input.attach_to_session = draft.attachToSession;
  }

  const skills = parseSkills(draft.skills);
  if (creating ? skills.length > 0 : skills.join(",") !== parseSkills(base.skills).join(",")) {
    input.skills = skills;
  }

  const tools = parsePreapprovedTools(draft.preapprovedTools);
  const toolsChanged = !creating && !same(draft.preapprovedTools, base.preapprovedTools);
  if (tools.ok && (creating ? tools.value.length > 0 : toolsChanged)) input.preapproved_tools = tools.value;

  if (draft.agentProfileId !== "" && (creating || draft.agentProfileId !== base.agentProfileId)) {
    input.agent_profile_id = draft.agentProfileId;
  }
  if (creating && scope.workspaceId) input.workspace_id = scope.workspaceId;
  return input;
}

function parseTargetOrText(text: string): string {
  const result = parseTarget(text);
  return result.ok ? result.value : text.trim();
}
