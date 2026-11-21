// Helpers for the Workflows section of the console: labels, the template of a new workflow, the
// check of the YAML text, and the inputs of a start. The limits mirror
// src/sama/workflows/spec.py; the API checks them again.

import type { InstanceStatus, WorkflowStatus, WorkflowValidation } from "./api";
import type { Tone } from "./jobs";
import { parseJsonObject } from "./jsonText";

export const MAX_YAML_BYTES = 50_000;
export const MAX_INPUTS_BYTES = 20_000;

/** The text of a new workflow. It uses an agent step only, so it needs no approval. */
export const WORKFLOW_TEMPLATE = `name: weekly-summary
description: Write a short summary of a topic each week.
inputs:
  topic:
    default: the news of the week
triggers:
  - cron: "0 9 * * MON"
    tz: Asia/Kolkata
steps:
  - id: summarise
    agent:
      prompt: "Write a short summary of {{inputs.topic}}."
`;

export function workflowStatusLabel(status: WorkflowStatus): string {
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

export function workflowStatusTone(status: WorkflowStatus): Tone {
  switch (status) {
    case "active":
      return "accent";
    case "pending_approval":
      return "warning";
    default:
      return "neutral";
  }
}

export function instanceStatusLabel(status: InstanceStatus | string): string {
  switch (status) {
    case "running":
      return "Running";
    case "waiting":
      return "Waiting";
    case "completed":
      return "Completed";
    case "failed":
      return "Failed";
    case "cancelled":
      return "Cancelled";
    default:
      return status;
  }
}

export function instanceStatusTone(status: InstanceStatus | string): Tone {
  switch (status) {
    case "completed":
      return "accent";
    case "failed":
      return "danger";
    case "waiting":
      return "warning";
    default:
      return "neutral";
  }
}

/** True when the instance has not ended, so the user can cancel it. */
export function canCancelInstance(status: InstanceStatus | string): boolean {
  return status === "running" || status === "waiting";
}

/** The action to offer for the status of a workflow: pause, resume, or none. */
export function pauseAction(status: WorkflowStatus): "pause" | "resume" | null {
  if (status === "active") return "pause";
  if (status === "paused") return "resume";
  return null;
}

/** Start is possible for an active workflow only. */
export function canStart(status: WorkflowStatus): boolean {
  return status === "active";
}

/** The first problem of the YAML text that the browser can see, or `null`. */
export function validateYamlText(text: string): string | null {
  if (text.trim() === "") return "Write the YAML text of the workflow.";
  if (new TextEncoder().encode(text).length > MAX_YAML_BYTES) {
    return `The YAML text has more than ${MAX_YAML_BYTES.toLocaleString("en-US")} bytes.`;
  }
  return null;
}

export type InputsResult = { ok: true; inputs: Record<string, unknown> } | { ok: false; error: string };

/** The inputs of a start. An empty field gives no inputs. */
export function parseInputs(text: string): InputsResult {
  const result = parseJsonObject(text, "The inputs field", MAX_INPUTS_BYTES);
  if (!result.ok) return result;
  return { ok: true, inputs: result.value ?? {} };
}

/** The sentences about the result of a check, one for each fact. */
export function validationNotes(check: Pick<WorkflowValidation, "needs_approval" | "approval_reasons">): string[] {
  if (!check.needs_approval) return [];
  const notes = ["The owner must approve this version before it can run."];
  for (const reason of check.approval_reasons) {
    const text = reason.trim().replace(/\.+$/, "");
    if (text !== "") notes.push(`${text[0]?.toUpperCase() ?? ""}${text.slice(1)}.`);
  }
  return notes;
}
