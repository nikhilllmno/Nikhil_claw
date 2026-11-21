// Helpers for the Webhooks and Triggers sections of the console: labels, the form draft and its
// change to a request, the test payload, and the signature help. The limits mirror
// src/sama/triggers/service.py; the API checks them again.

import type {
  DeliveryOutcome,
  NewTrigger,
  Org,
  Trigger,
  TriggerDelivery,
  TriggerEdit,
  TriggerKind,
  TriggerStatus,
  Workflow,
} from "./api";
import { parseJsonObject } from "./jsonText";
import { sameList } from "./profile";
import type { Tone } from "./jobs";

export const NAME_MAX_CHARS = 80;
export const PROMPT_MAX_CHARS = 8000;
export const PAYLOAD_MAX_BYTES = 20_000;
export const CONFIG_MAX_BYTES = 4000;
/** The tool groups that a new trigger gets. Any other group needs an admin. */
export const SAFE_TOOLSETS: readonly string[] = ["web", "media", "clarify"];

/** The word for one trigger of the kind, in the messages and the buttons. */
export function kindNoun(kind: TriggerKind): string {
  return kind === "webhook" ? "webhook" : "trigger";
}

// -- labels -------------------------------------------------------------------------------------

export function triggerStatusLabel(status: TriggerStatus): string {
  return status === "active" ? "Active" : "Disabled";
}

export function triggerStatusTone(status: TriggerStatus): Tone {
  return status === "active" ? "accent" : "neutral";
}

export function outcomeLabel(outcome: DeliveryOutcome): string {
  switch (outcome) {
    case "accepted":
      return "Accepted";
    case "duplicate":
      return "Duplicate";
    case "bad_signature":
      return "Bad signature";
    case "disabled":
      return "Trigger disabled";
    case "bad_body":
      return "Bad body";
    case "rate_limited":
      return "Too many requests";
    case "rejected":
      return "Rejected";
    case "test_ok":
      return "Test passed";
    default:
      return outcome;
  }
}

export function outcomeTone(outcome: DeliveryOutcome): Tone {
  switch (outcome) {
    case "accepted":
    case "test_ok":
      return "accent";
    case "duplicate":
    case "disabled":
    case "rate_limited":
      return "warning";
    case "bad_signature":
    case "bad_body":
    case "rejected":
      return "danger";
    default:
      return "neutral";
  }
}

/** True when the delivery was a good one (a run or a workflow started, or a test passed). */
export function isGoodOutcome(outcome: DeliveryOutcome): boolean {
  return outcome === "accepted" || outcome === "test_ok";
}

/** One sentence about a delivery, for the result of a test. */
export function deliverySummary(delivery: Pick<TriggerDelivery, "outcome" | "http_status" | "error">): string {
  const base = `${outcomeLabel(delivery.outcome)} (HTTP ${delivery.http_status}).`;
  return delivery.error ? `${base} ${delivery.error}` : base;
}

/** What the trigger starts. */
export function targetText(trigger: Pick<Trigger, "target">, agentName: string | null, workflowName: string | null): string {
  if (trigger.target === "workflow") {
    return workflowName ? `Starts the workflow ${workflowName}` : "Starts a workflow";
  }
  return agentName ? `Starts a run of ${agentName}` : "Starts a run";
}

/** True when the user can change the trigger: an admin, or the owner of the trigger. */
export function canManageTrigger(org: Pick<Org, "admin" | "user_id">, trigger: Pick<Trigger, "owner_user_id">): boolean {
  return org.admin || trigger.owner_user_id === org.user_id;
}

/** The workflows that a trigger of the workspace can start. */
export function workflowChoices(workflows: readonly Workflow[], workspaceId: string): Workflow[] {
  return workflows.filter((w) => w.workspace_id === workspaceId && w.status !== "removed");
}

// -- signature help -----------------------------------------------------------------------------

export const SIGNATURE_HEADERS: readonly { name: string; text: string }[] = [
  { name: "webhook-id", text: "The id of the delivery. A retry sends the same id. Sama refuses a second delivery with an id that it has seen." },
  { name: "webhook-timestamp", text: "The time of the delivery in Unix seconds. Sama refuses a time that is more than 5 minutes from its own clock." },
  { name: "webhook-signature", text: "v1, a comma, and the base64 of the HMAC-SHA256 of the signed text." },
];

export const SIGNED_TEXT = "<webhook-id>.<webhook-timestamp>.<body>";
export const SIGNATURE_KEY_HELP =
  "The key is the base64 part of the secret after whsec_, decoded to bytes. The body is the raw body of the request, without a change.";

// -- form ---------------------------------------------------------------------------------------

export interface TriggerDraft {
  name: string;
  target: "run" | "workflow";
  agentId: string;
  prompt: string;
  toolsets: string[];
  workflowId: string;
  connectionId: string;
  slug: string;
  /** The text of the config field. Empty: no config. */
  config: string;
}

export function newTriggerDraft(agentId: string): TriggerDraft {
  return {
    name: "",
    target: "run",
    agentId,
    prompt: "",
    toolsets: [...SAFE_TOOLSETS],
    workflowId: "",
    connectionId: "",
    slug: "",
    config: "",
  };
}

export function toTriggerDraft(trigger: Trigger): TriggerDraft {
  return {
    name: trigger.name,
    target: trigger.target,
    agentId: trigger.agent_profile_id ?? "",
    prompt: trigger.prompt ?? "",
    toolsets: [...trigger.toolsets],
    workflowId: trigger.workflow_id ?? "",
    connectionId: trigger.connection_id ?? "",
    slug: trigger.composio_slug ?? "",
    config: trigger.composio_config ? JSON.stringify(trigger.composio_config) : "",
  };
}

const SLUG = /^[A-Za-z0-9_.:-]{1,100}$/;

/** The first problem of a new trigger, or `null`. */
export function validateNewTrigger(draft: TriggerDraft, kind: TriggerKind): string | null {
  const name = draft.name.trim();
  if (name === "") return `The ${kindNoun(kind)} needs a name.`;
  if (name.length > NAME_MAX_CHARS) return `The name has at most ${NAME_MAX_CHARS} characters.`;
  if (draft.target === "workflow") {
    if (draft.workflowId === "") return "Choose a workflow.";
  } else {
    if (draft.agentId === "") return "Choose an agent.";
    if (draft.prompt.trim() === "") return "The prompt tells the agent what to do. Write it.";
    if (draft.prompt.trim().length > PROMPT_MAX_CHARS) {
      return `The prompt has at most ${PROMPT_MAX_CHARS.toLocaleString("en-US")} characters.`;
    }
  }
  if (kind === "composio") {
    if (draft.connectionId === "") return "Choose a connected app.";
    if (!SLUG.test(draft.slug.trim())) return "Write the name of the trigger type, for example GITHUB_COMMIT_EVENT.";
    const config = parseJsonObject(draft.config, "The config", CONFIG_MAX_BYTES);
    if (!config.ok) return config.error;
  }
  return null;
}

/** The first problem of the edit of a trigger, or `null`. */
export function validateTriggerEdit(draft: TriggerDraft, trigger: Trigger): string | null {
  const name = draft.name.trim();
  if (name === "") return `The ${kindNoun(trigger.kind)} needs a name.`;
  if (name.length > NAME_MAX_CHARS) return `The name has at most ${NAME_MAX_CHARS} characters.`;
  if (trigger.target === "run") {
    if (draft.prompt.trim() === "") return "The prompt tells the agent what to do. Write it.";
    if (draft.prompt.trim().length > PROMPT_MAX_CHARS) {
      return `The prompt has at most ${PROMPT_MAX_CHARS.toLocaleString("en-US")} characters.`;
    }
  }
  return null;
}

/** The request for a new trigger. Call `validateNewTrigger` first. */
export function buildNewTrigger(draft: TriggerDraft, kind: TriggerKind): NewTrigger {
  const body: NewTrigger = { kind, name: draft.name.trim() };
  if (draft.target === "workflow") {
    body.workflow_id = draft.workflowId;
  } else {
    body.agent_profile_id = draft.agentId;
    body.prompt = draft.prompt.trim();
    body.toolsets = draft.toolsets;
  }
  if (kind === "composio") {
    body.connection_id = draft.connectionId;
    body.composio_slug = draft.slug.trim();
    const config = parseJsonObject(draft.config, "The config", CONFIG_MAX_BYTES);
    if (config.ok && config.value !== null) body.composio_config = config.value;
  }
  return body;
}

/** Only the fields of the edit that differ from the saved trigger. An empty object: no change. */
export function buildTriggerEdit(trigger: Trigger, draft: TriggerDraft): TriggerEdit {
  const edit: TriggerEdit = {};
  if (draft.name.trim() !== trigger.name) edit.name = draft.name.trim();
  if (trigger.target === "run") {
    if (draft.prompt.trim() !== (trigger.prompt ?? "")) edit.prompt = draft.prompt.trim();
    if (!sameList(draft.toolsets, trigger.toolsets)) edit.toolsets = draft.toolsets;
  }
  return edit;
}
