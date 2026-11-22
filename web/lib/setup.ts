// The Setup section of the settings console (docs/architecture.md, Section 17.7, M7.10): the
// steps of the setup guide as a checklist. Each step has a status and a link to the part of the
// console, or the page, where the user does it. The steps and the progress come from
// `onboarding.ts`, so the checklist and the guide at /onboarding always agree.

import type { Org } from "./api";
import { currentStep, onboardingSteps, type Progress, type StepId } from "./onboarding";

export type SetupStatus = "done" | "skipped" | "next" | "todo";

export interface SetupItem {
  id: StepId;
  title: string;
  hint: string;
  done: boolean;
  status: SetupStatus;
  /** Where the user does the step. `null`: nothing to do (the org exists). */
  href: string | null;
  linkLabel: string;
}

const LINKS: Record<StepId, { href: string | null; label: string }> = {
  org: { href: null, label: "" },
  plan: { href: "/billing", label: "Open the plan" },
  channel: { href: "/console/slack", label: "Open Slack" },
  agent: { href: "/console/harness", label: "Open Harness" },
  message: { href: "/", label: "Open the sessions" },
};

export const SETUP_STATUS_LABELS: Record<SetupStatus, string> = {
  done: "Done",
  skipped: "Skipped",
  next: "Next",
  todo: "To do",
};

/** The steps of the guide for the org, with a status and a link for each. */
export function setupChecklist(
  org: Pick<Org, "integrations" | "agent_profiles" | "plan">,
  progress: Progress,
): SetupItem[] {
  const steps = onboardingSteps(org, progress);
  const next = currentStep(steps);
  const channelSkipped =
    progress.channel === "skipped" && !org.integrations.some((i) => i.status === "active");
  return steps.map((step) => {
    const link = LINKS[step.id];
    const skipped = step.id === "channel" && step.done && channelSkipped;
    const status: SetupStatus = skipped ? "skipped" : step.done ? "done" : step.id === next ? "next" : "todo";
    return { ...step, status, href: link.href, linkLabel: link.label };
  });
}

/** The count of the steps that are done (a skipped step counts). */
export function setupProgressText(items: readonly SetupItem[]): string {
  const done = items.filter((i) => i.done).length;
  return `${done} of ${items.length} steps done`;
}

export interface NextStep {
  title: string;
  text: string;
  href: string;
  linkLabel: string;
}

/** Ideas for after the setup. Each one is a section of the console. */
export const NEXT_STEPS: readonly NextStep[] = [
  {
    title: "Roles",
    text: "Invite your team and choose what each role can do.",
    href: "/console/roles",
    linkLabel: "Open Roles",
  },
  {
    title: "Tools",
    text: "Choose the tools that each agent can use and the ones that ask first.",
    href: "/console/tools",
    linkLabel: "Open Tools",
  },
  {
    title: "MCP servers",
    text: "Add MCP servers to give the agents more tools.",
    href: "/console/mcp",
    linkLabel: "Open MCP servers",
  },
  {
    title: "Schedules",
    text: "Run an agent on a schedule, for example every weekday at 9am.",
    href: "/console/schedules",
    linkLabel: "Open Schedules",
  },
  {
    title: "Webhooks",
    text: "Start a run when another system sends a signed webhook.",
    href: "/console/webhooks",
    linkLabel: "Open Webhooks",
  },
  {
    title: "API keys",
    text: "Let another app use sama through the MCP server or the OpenAI-compatible endpoint.",
    href: "/console/api-keys",
    linkLabel: "Open API keys",
  },
];
