// The org API keys on the admin page (docs/architecture.md, Section 10.7). A key shows one time,
// in the answer of the create. The list has only the prefix.

import type { AgentProfileSummary, ApiKey, ApiKeyCreate, ApiKeyScope, Org } from "./api";

export const NAME_MAX_CHARS = 80;
export const MAX_EXPIRY_DAYS = 365;

export const SCOPE_TITLES: Record<ApiKeyScope, string> = {
  chat: "OpenAI-compatible chat (/v1)",
  mcp: "MCP server (/mcp)",
};

export interface ApiKeyDraft {
  name: string;
  workspaceId: string;
  /** An empty text means every agent of the workspace. */
  profileId: string;
  chat: boolean;
  mcp: boolean;
  approvals: "deny" | "pending";
  /** An empty text means no expiry. */
  expiresInDays: string;
}

export function emptyDraft(org: Pick<Org, "workspaces">): ApiKeyDraft {
  return {
    name: "",
    workspaceId: org.workspaces[0]?.id ?? "",
    profileId: "",
    chat: true,
    mcp: true,
    approvals: "deny",
    expiresInDays: "",
  };
}

/** The agent profiles that a key of the workspace can use. */
export function profilesFor(org: Pick<Org, "agent_profiles">, workspaceId: string): AgentProfileSummary[] {
  return org.agent_profiles.filter((p) => p.workspace_id === null || p.workspace_id === workspaceId);
}

/** The request body for a draft, or the problem with the draft. The same checks as the API. */
export function draftBody(draft: ApiKeyDraft): { body: ApiKeyCreate } | { problem: string } {
  const name = draft.name.trim();
  if (name === "" || name.length > NAME_MAX_CHARS) {
    return { problem: `Give a name of 1 to ${NAME_MAX_CHARS} characters.` };
  }
  if (draft.workspaceId === "") return { problem: "Choose a workspace." };
  const scopes: ApiKeyScope[] = [];
  if (draft.chat) scopes.push("chat");
  if (draft.mcp) scopes.push("mcp");
  if (scopes.length === 0) return { problem: "Choose at least one use for the key." };
  let expires: number | null = null;
  if (draft.expiresInDays.trim() !== "") {
    const days = Number(draft.expiresInDays.trim());
    if (!Number.isInteger(days) || days < 1 || days > MAX_EXPIRY_DAYS) {
      return { problem: `A key expires after 1 to ${MAX_EXPIRY_DAYS} days.` };
    }
    expires = days;
  }
  return {
    body: {
      name,
      workspace_id: draft.workspaceId,
      agent_profile_id: draft.profileId === "" ? null : draft.profileId,
      scopes,
      approvals: draft.approvals,
      expires_in_days: expires,
    },
  };
}

/** The badge and its class for the status of a key. */
export function statusBadge(key: Pick<ApiKey, "status">): {
  label: string;
  className: string;
} {
  switch (key.status) {
    case "active":
      return { label: "Active", className: "badge badge-accent" };
    case "expired":
      return { label: "Expired", className: "badge" };
    default:
      return { label: "Revoked", className: "badge" };
  }
}

/** The text that tells what happens to a tool call that needs an approval. */
export function approvalsText(approvals: ApiKey["approvals"]): string {
  return approvals === "deny"
    ? "A tool call that needs an approval is denied."
    : "A tool call that needs an approval waits for a person in the web app.";
}

/** The example settings for a client, with the base address of the API. */
export function clientSnippets(apiBase: string): {
  openai: string;
  mcp: string;
} {
  const base = apiBase.replace(/\/+$/, "");
  return {
    openai: `from openai import OpenAI\nclient = OpenAI(base_url="${base}/v1", api_key="<your sama key>")\nclient.chat.completions.create(model="sama", messages=[{"role": "user", "content": "Hello"}])`,
    mcp: `claude mcp add --transport http sama ${base}/mcp --header "Authorization: Bearer <your sama key>"`,
  };
}
