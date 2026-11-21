// The REST types of the sama API that the panel uses (src/sama/api/app.py).

export interface PublicConfig {
  supabase_url: string | null;
  supabase_anon_key: string | null;
}

export interface Workspace {
  id: string;
  name: string;
}

export interface AgentProfileSummary {
  id: string;
  name: string;
  workspace_id: string | null;
}

export interface Org {
  id: string;
  name: string;
  user_id: string;
  display_name: string | null;
  workspaces: Workspace[];
  agent_profiles: AgentProfileSummary[];
}

export interface Me {
  orgs: Org[];
}

export interface SessionOut {
  id: string;
  title: string | null;
  audience: string;
  workspace_id: string;
}

export interface RunOut {
  id: string;
  status: string;
}

export interface SessionDetail extends SessionOut {
  runs: RunOut[];
  last_event_id: number;
}

export interface ChatMessage {
  id: string;
  seq: number;
  run_id: string | null;
  role: "user" | "assistant" | "system" | "tool";
  text: string;
  created_at: string;
}

export interface Approval {
  id: string;
  run_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  reason: string;
}

/** Where a new chat goes: the first workspace and an agent of it. */
export interface ChatHome {
  workspaceId: string;
  agentProfileId: string;
  orgName: string;
}

export function chatHome(me: Me): ChatHome | null {
  for (const org of me.orgs) {
    for (const workspace of org.workspaces) {
      const profile =
        org.agent_profiles.find((p) => p.workspace_id === workspace.id) ??
        org.agent_profiles.find((p) => p.workspace_id === null);
      if (profile) return { workspaceId: workspace.id, agentProfileId: profile.id, orgName: org.name };
    }
  }
  return null;
}

export const MAX_SESSION_TITLE = 120;

/** The title of the chat of a tab. */
export function chatTitle(tabTitle: string | undefined): string {
  const title = (tabTitle ?? "").replace(/\s+/g, " ").trim();
  return `Chrome: ${title || "new tab"}`.slice(0, MAX_SESSION_TITLE);
}

/** The text of an error body: `detail` as a string, or the messages of a validation error. */
export function errorMessage(status: number, body: unknown): string {
  if (body !== null && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string" && detail !== "") return detail;
    if (Array.isArray(detail)) {
      const text = detail
        .map((item: unknown) =>
          item !== null && typeof item === "object" && "msg" in item ? String((item as { msg: unknown }).msg) : "",
        )
        .filter(Boolean)
        .join("; ");
      if (text) return text;
    }
  }
  if (status === 401) return "Your sign-in ended. Sign in again.";
  if (status === 403) return "You do not have permission to do this.";
  if (status === 404) return "Not found.";
  if (status === 429) return "Too many requests. Wait a moment and try again.";
  return `The request failed (${status}).`;
}
