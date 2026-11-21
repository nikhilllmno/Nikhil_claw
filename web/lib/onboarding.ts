// The first-run flow after sign-up (docs/architecture.md, Section 17.6, M6.9): create the org,
// choose a plan, connect a chat app, pick or create an agent, and send a first message. The progress of each
// org is kept in localStorage of this browser. It only chooses what the screens show: the API
// checks every rule again.

import type { Org } from "./api";
import type { KeyValueStore } from "./pkce";

export const ONBOARDING_PREFIX = "sama.onboarding.";

export type StepId = "org" | "plan" | "channel" | "agent" | "message";

/** The plan that a new org starts on (Section 17.5). */
export const START_PLAN = "free";

export interface Progress {
  /** The person kept the plan of the org. A paid plan counts as done without it. */
  plan?: "kept";
  /** `connected`: a chat app is active. `skipped`: the user chose the web app only. */
  channel?: "connected" | "skipped";
  /** The agent that the user picked or created. */
  agentId?: string;
  /** The first message is sent, or the user closed the guide. */
  done?: boolean;
}

export interface Step {
  id: StepId;
  title: string;
  hint: string;
  done: boolean;
}

function key(orgId: string): string {
  return `${ONBOARDING_PREFIX}${orgId}`;
}

/** The kept progress of an org. A damaged value counts as no progress. */
export function loadProgress(store: KeyValueStore, orgId: string): Progress {
  const raw = store.getItem(key(orgId));
  if (raw === null) return {};
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    const progress: Progress = {};
    if (value.plan === "kept") progress.plan = "kept";
    if (value.channel === "connected" || value.channel === "skipped") progress.channel = value.channel;
    if (typeof value.agentId === "string" && value.agentId !== "") progress.agentId = value.agentId;
    if (value.done === true) progress.done = true;
    return progress;
  } catch {
    return {};
  }
}

export function saveProgress(store: KeyValueStore, orgId: string, progress: Progress): void {
  store.setItem(key(orgId), JSON.stringify(progress));
}

/** True when the org has a chat app that works. */
export function hasActiveChatApp(org: Pick<Org, "integrations">): boolean {
  return org.integrations.some((i) => (i.kind === "telegram" || i.kind === "slack") && i.status === "active");
}

/** The steps of the guide for an org, with what is done. */
export function onboardingSteps(
  org: Pick<Org, "integrations" | "agent_profiles" | "plan">,
  progress: Progress,
): Step[] {
  const agentKnown = progress.agentId !== undefined && org.agent_profiles.some((p) => p.id === progress.agentId);
  return [
    { id: "org", title: "Create your org", hint: "Your org, a workspace, and an agent are ready.", done: true },
    {
      id: "plan",
      title: "Choose a plan",
      hint: "Your org starts on the free plan. Keep it, or buy a plan with larger quotas.",
      done: progress.plan === "kept" || org.plan !== START_PLAN,
    },
    {
      id: "channel",
      title: "Connect a chat app",
      hint: "Talk to your agents in Telegram or Slack. You can also use only the web app.",
      done: hasActiveChatApp(org) || progress.channel !== undefined,
    },
    {
      id: "agent",
      title: "Pick or create an agent",
      hint: "Choose the agent that answers you, or make a new one with its own instructions.",
      done: agentKnown,
    },
    {
      id: "message",
      title: "Send a first message",
      hint: "Start a session and ask the agent for something.",
      done: progress.done === true,
    },
  ];
}

/** The agents that a session in the workspace can use: the org-wide ones and the workspace ones. */
export function usableAgents<T extends { workspace_id: string | null }>(agents: T[], workspaceId: string | null): T[] {
  return agents.filter((a) => a.workspace_id === null || a.workspace_id === workspaceId);
}

/**
 * The chosen agent when the workspace can use it, else `null`. The screens never swap in another
 * agent: the person chooses again.
 */
export function usableChoice(
  agents: { id: string; workspace_id: string | null }[],
  workspaceId: string | null,
  chosen: string | null | undefined,
): string | null {
  if (!chosen) return null;
  return usableAgents(agents, workspaceId).some((a) => a.id === chosen) ? chosen : null;
}

/** The first step that is not done, or `null` when all are done. */
export function currentStep(steps: Step[]): StepId | null {
  return steps.find((s) => !s.done)?.id ?? null;
}

/** True when the home page shows the "Finish setting up" card for this org. */
export function showSetupCard(
  org: Pick<Org, "admin" | "integrations" | "agent_profiles" | "plan">,
  progress: Progress,
): boolean {
  if (!org.admin || progress.done === true) return false;
  return currentStep(onboardingSteps(org, progress)) !== null;
}

/** Starting points for the soul of a new agent. The admin can change the text. */
export const AGENT_TEMPLATES = [
  {
    id: "assistant",
    name: "Assistant",
    title: "General assistant",
    soul: "You are a helpful assistant for our team. Answer in short, clear sentences. Ask a question when a request is not clear.",
  },
  {
    id: "research",
    name: "Researcher",
    title: "Research",
    soul: "You research questions for our team. Search the web, compare sources, and give the sources with each fact. Say when you are not sure.",
  },
  {
    id: "writer",
    name: "Writer",
    title: "Writing",
    soul: "You write and edit text for our team: emails, posts, and documents. Keep the voice of the team: friendly, short, and clear.",
  },
  {
    id: "support",
    name: "Support",
    title: "Customer support",
    soul: "You help our support team answer customers. Be polite and exact. Never promise a refund or a date. Ask a person when you are not sure.",
  },
] as const;

/** Ideas for the first message. */
export const FIRST_PROMPTS = [
  "What can you do for our team? Give me five examples.",
  "Search the web for the three biggest news stories in our industry this week and summarize them.",
  "Write a short welcome message for a new member of our team.",
] as const;
