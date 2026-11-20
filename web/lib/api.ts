// The types of the sama API (src/sama/api/app.py) and a small client for it.
// Every call sends `Authorization: Bearer <access token>`. On 401 the client refreshes the
// token once and tries the call again. If that fails, it signs the user out.
// Each call belongs to the sign-in generation that it started in. If the account changes
// while the call waits, the answer is dropped: it is never retried, and it never signs out
// the new account.

// -- types --------------------------------------------------------------------------------------

export interface PublicConfig {
  supabase_url: string | null;
  supabase_anon_key: string | null;
  integrations: boolean;
}

export interface Workspace {
  id: string;
  name: string;
}

export interface AgentProfileSummary {
  id: string;
  name: string;
  workspace_id: string | null;
  runtime: string;
  model: string;
}

export interface IntegrationSummary {
  kind: string;
  status: string;
  name: string | null;
}

export interface Org {
  id: string;
  name: string;
  user_id: string;
  display_name: string | null;
  /** True for an org owner or admin. */
  admin: boolean;
  workspaces: Workspace[];
  agent_profiles: AgentProfileSummary[];
  integrations: IntegrationSummary[];
}

export interface Me {
  orgs: Org[];
}

export type Audience = "private" | "shared";

export interface NewSession {
  workspace_id: string;
  agent_profile_id: string;
  audience: Audience;
  title: string | null;
}

export interface SessionOut {
  id: string;
  org_id: string;
  workspace_id: string;
  agent_profile_id: string;
  audience: string;
  title: string | null;
  status: string;
  created_at: string;
  /** `compaction` for the archive of a transcript, else `null`. */
  lineage: string | null;
  parent_session_id: string | null;
}

export type RunStatus =
  | "queued"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled"
  | (string & {});

/** The statuses of a run that has not ended. */
export const OPEN_RUN_STATUSES: ReadonlySet<string> = new Set(["queued", "running", "paused"]);

export interface RunOut {
  id: string;
  status: RunStatus;
  failure_code: string | null;
  failure_message: string | null;
  created_at: string;
}

export interface SessionDetail extends SessionOut {
  /** The last runs, newest first. */
  runs: RunOut[];
  /** Give it to the event stream as `after` to get only new events. */
  last_event_id: number;
}

export type MessageRole = "user" | "assistant" | "system" | "tool";

export interface ChatMessage {
  id: string;
  seq: number;
  run_id: string | null;
  role: MessageRole;
  author_user_id: string | null;
  text: string;
  created_at: string;
}

export interface Approval {
  id: string;
  run_id: string;
  call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  reason: string;
  created_at: string;
  expires_at: string;
}

export interface ClarifyQuestion {
  question: string;
  choices: string[];
}

export interface ClarifyRequest {
  id: string;
  run_id: string;
  questions: ClarifyQuestion[];
}

/** The status of a run in the `runs` and `run` stream events. */
export interface StreamRun {
  run_id: string;
  status: RunStatus;
  failure_code: string | null;
  failure_message: string | null;
}

export interface RunsEvent {
  runs: StreamRun[];
}

export interface RunEventPayload {
  run_id: string;
  seq: number;
  type: string;
  payload: Record<string, unknown>;
}

export interface Subagent {
  name: string;
  description: string;
  prompt: string;
  toolsets: string[];
  model: string | null;
  max_turns: number | null;
}

export interface ToolsetOption {
  name: string;
  tools: string[];
}

export interface AgentProfileOptions {
  toolsets: ToolsetOption[];
  models: string[];
  subagents: boolean;
  limits: AgentProfileLimits;
}

/** The limits of the editor. The API sends all of them. */
export interface AgentProfileLimits {
  name_max_chars: number;
  soul_max_chars: number;
  max_subagents: number;
  subagent_name_max_chars: number;
  subagent_name_pattern: string;
  subagent_description_max_chars: number;
  subagent_prompt_max_chars: number;
  subagent_max_turns: number;
}

export interface AgentProfile {
  id: string;
  org_id: string;
  workspace_id: string | null;
  name: string;
  soul: string;
  runtime: string;
  model: string;
  toolsets: string[];
  group_toolsets: string[];
  subagents: Subagent[];
  updated_at: string;
  /** True if the user is an org owner or admin. */
  editable: boolean;
  options: AgentProfileOptions;
}

/** The body of `PATCH /orgs/{org_id}/agent-profiles/{profile_id}`. Each field is optional. */
export interface AgentProfilePatch {
  name?: string;
  soul?: string;
  model?: string;
  toolsets?: string[];
  subagents?: Subagent[];
}

// -- errors -------------------------------------------------------------------------------------

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** The message of a caught error, or `fallback` if it is not an `Error`. */
export function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

/** The text of an error body: `detail` as a string, or the messages of a validation error. */
export function errorMessage(status: number, body: unknown): string {
  if (body !== null && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string" && detail !== "") return detail;
    if (Array.isArray(detail)) {
      const parts = detail.map((item: unknown) => {
        if (item !== null && typeof item === "object" && "msg" in item) {
          const { msg, loc } = item as { msg: unknown; loc?: unknown };
          const where = Array.isArray(loc) ? loc.filter((p) => p !== "body").join(".") : "";
          return where ? `${where}: ${String(msg)}` : String(msg);
        }
        return "";
      });
      const text = parts.filter(Boolean).join("; ");
      if (text) return text;
    }
  }
  if (status === 401) return "Your sign-in ended. Sign in again.";
  if (status === 403) return "You do not have permission to do this.";
  if (status === 404) return "Not found.";
  if (status === 429) return "Too many requests. Wait a moment and try again.";
  return `The request failed (${status}).`;
}

// -- client -------------------------------------------------------------------------------------

/** What the client needs from the sign-in state. */
export interface TokenSource {
  /** Changes when the account changes (sign-out or a new sign-in), not at a token refresh. */
  readonly generation: number;
  /** The access token to use now (the source refreshes one that is about to expire). */
  getToken(): Promise<string>;
  /**
   * Gets a new access token after a 401 in `generation`. Returns `null` if it cannot, or if
   * the generation is not the current one. The source signs out itself if the refresh token
   * of the current generation does not work.
   */
  refresh(generation: number): Promise<string | null>;
  /** The server did not accept the token. Does nothing if `generation` is not the current one. */
  signOut(generation: number): void;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/** The most that `GET /sessions/{id}/messages` returns in one page. */
const MESSAGE_PAGE = 500;

export class ApiClient {
  constructor(
    private readonly baseUrl: string,
    private readonly tokens: TokenSource,
    private readonly fetcher: FetchLike = (input, init) => fetch(input, init),
  ) {}

  /**
   * A call with the Bearer token. A 401 gives one refresh and one more try. A call whose
   * account changed while it waited throws and does nothing else. `generation` is the
   * account the whole operation belongs to (the current one if left out). A call that starts
   * in another one throws without sending anything.
   */
  async raw(
    path: string,
    options: RequestOptions = {},
    generation: number = this.tokens.generation,
  ): Promise<Response> {
    const current = () => this.assertCurrent(generation);
    current();
    const send = async (token: string): Promise<Response> => {
      const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
      if (options.body !== undefined) headers["Content-Type"] = "application/json";
      return this.fetcher(`${this.baseUrl}${path}`, {
        method: options.method ?? "GET",
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: options.signal,
      });
    };
    let token: string | null = await this.tokens.getToken();
    current();
    for (let retried = false; ; retried = true) {
      const response = await send(token);
      current();
      if (response.status !== 401) return response;
      token = retried ? null : await this.tokens.refresh(generation);
      if (token === null) {
        this.tokens.signOut(generation);
        throw new ApiError(401, errorMessage(401, null));
      }
      current();
    }
  }

  private assertCurrent(generation: number): void {
    if (this.tokens.generation !== generation) {
      throw new ApiError(401, "You signed in as someone else. Try again.");
    }
  }

  /**
   * A call that returns JSON. A status that is not 2xx throws an `ApiError`. The account is
   * checked again after the body is read, so an answer for the old account is never returned.
   */
  async json<T>(
    path: string,
    options: RequestOptions = {},
    generation: number = this.tokens.generation,
  ): Promise<T> {
    const response = await this.raw(path, options, generation);
    const data: unknown =
      response.status === 204 ? null : await response.json().catch(() => null);
    this.assertCurrent(generation);
    if (!response.ok) throw new ApiError(response.status, errorMessage(response.status, data));
    return data as T;
  }

  me = () => this.json<Me>("/me");
  listSessions = (limit = 50) => this.json<SessionOut[]>(`/sessions?limit=${limit}`);
  createSession = (body: NewSession) =>
    this.json<SessionOut>("/sessions", { method: "POST", body });
  getSession = (id: string) => this.json<SessionDetail>(`/sessions/${id}`);
  /** All messages of a session, oldest first, read page by page with the `after` cursor. */
  listMessages = async (id: string): Promise<ChatMessage[]> => {
    // One account for every page: a switch in between stops the read, it does not go on as B.
    const generation = this.tokens.generation;
    const all: ChatMessage[] = [];
    for (;;) {
      const after = all.at(-1)?.seq ?? 0;
      const page = await this.json<ChatMessage[]>(
        `/sessions/${id}/messages?after=${after}&limit=${MESSAGE_PAGE}`,
        {},
        generation,
      );
      all.push(...page);
      if (page.length < MESSAGE_PAGE) return all;
    }
  };
  sendMessage = (id: string, text: string) =>
    this.json<{ run_id: string }>(`/sessions/${id}/messages`, { method: "POST", body: { text } });
  compact = (id: string, focus: string) =>
    this.json<{ run_id: string }>(`/sessions/${id}/compact`, { method: "POST", body: { focus } });
  cancelRun = (runId: string) => this.json<{ status: string }>(`/runs/${runId}/cancel`, { method: "POST" });
  listApprovals = (id: string) => this.json<Approval[]>(`/sessions/${id}/approvals`);
  approve = (approvalId: string, alwaysAllow: boolean) =>
    this.json<{ status: string; resumed: boolean }>(`/approvals/${approvalId}/approve`, {
      method: "POST",
      body: { always_allow: alwaysAllow },
    });
  deny = (approvalId: string) =>
    this.json<{ status: string; resumed: boolean }>(`/approvals/${approvalId}/deny`, {
      method: "POST",
      body: {},
    });
  listQuestions = (id: string) => this.json<ClarifyRequest[]>(`/sessions/${id}/questions`);
  answerQuestion = (id: string, requestId: string, answers: string[]) =>
    this.json<null>(`/sessions/${id}/questions/${encodeURIComponent(requestId)}/answer`, {
      method: "POST",
      body: { answers },
    });
  getAgentProfile = (orgId: string, profileId: string) =>
    this.json<AgentProfile>(`/orgs/${orgId}/agent-profiles/${profileId}`);
  updateAgentProfile = (orgId: string, profileId: string, patch: AgentProfilePatch) =>
    this.json<AgentProfile>(`/orgs/${orgId}/agent-profiles/${profileId}`, {
      method: "PATCH",
      body: patch,
    });
}

/** The public config of the API (no sign-in needed). */
export async function fetchConfig(
  baseUrl: string,
  fetcher: FetchLike = (input, init) => fetch(input, init),
): Promise<PublicConfig> {
  const response = await fetcher(`${baseUrl}/config`);
  if (!response.ok) throw new ApiError(response.status, `The API did not answer (${response.status}).`);
  return (await response.json()) as PublicConfig;
}
