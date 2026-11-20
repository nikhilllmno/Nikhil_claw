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
  /** True when the API has the connection routes. */
  connections?: boolean;
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

/** A runtime that an editor can choose, with its models and its sub-agent support. */
export interface RuntimeOption {
  name: string;
  models: string[];
  subagents: boolean;
}

export interface AgentProfileOptions {
  toolsets: ToolsetOption[];
  /** The models of the saved runtime. */
  models: string[];
  /** True if the saved runtime has sub-agents. */
  subagents: boolean;
  runtimes: RuntimeOption[];
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
  runtime?: string;
  model?: string;
  toolsets?: string[];
  subagents?: Subagent[];
}

export type ConnectionScope = "personal" | "workspace";

export type ConnectionState =
  | "pending"
  | "initiated"
  | "connected"
  | "failed"
  | "expired"
  | "disabled"
  | (string & {});

export interface ConnectorField {
  name: string;
  label: string;
  /** A secret field goes to the vault. The API never returns its value. */
  secret: boolean;
  required: boolean;
  help: string;
}

export interface ConnectorTool {
  name: string;
  description: string;
  access: "read" | "write" | "destructive";
}

/** A connector of the catalog (`GET /connectors`). */
export interface Connector {
  name: string;
  title: string;
  description: string;
  /** `composio`: a sign-in link. `http_api`: a form. */
  kind: string;
  scopes: ConnectionScope[];
  fields: ConnectorField[];
  tools: ConnectorTool[];
}

/** A connection without secrets. Only the backend sets the state `connected`. */
export interface Connection {
  id: string;
  workspace_id: string;
  connector: string;
  kind: string;
  scope: ConnectionScope;
  owner_user_id: string | null;
  state: ConnectionState;
  last_error: string | null;
  last_health_at: string | null;
  created_at: string;
  updated_at: string;
  /** The values of the fields that are not secret. */
  fields: Record<string, string>;
  has_secret: boolean;
}

/** The answer to a new connection: the sign-in link of Composio, or `null` for a form. */
export interface StartedConnection {
  connection: Connection;
  redirect_url: string | null;
}

// -- scheduled jobs (src/sama/scheduler) --------------------------------------------------------

export type JobStatus = "active" | "paused" | "pending_approval" | "removed" | (string & {});

/** A stored schedule. `cron` has `expr` and `tz`, `interval` has `every_s`, `once` has `at`. */
export interface JobSchedule {
  kind: "cron" | "interval" | "once" | (string & {});
  expr?: string;
  tz?: string;
  every_s?: number;
  at?: string;
}

export type FireStatus = "running" | "completed" | "failed" | "skipped" | (string & {});
export type DeliveryStatus = "pending" | "sending" | "sent" | "failed" | "unknown" | (string & {});

export interface JobDelivery {
  kind: string;
  target: string;
  status: DeliveryStatus;
  attempts?: number;
  provider_message_id?: string | null;
  error: string | null;
  sent_at?: string | null;
}

export interface JobFire {
  id: string;
  scheduled_for: string;
  status: FireStatus;
  manual: boolean;
  session_id?: string | null;
  output: string | null;
  error: string | null;
  finished_at?: string | null;
  deliveries: JobDelivery[];
}

/** A tool that a person approved for the job. `limits` maps an argument to its allowed values. */
export interface PreapprovedTool {
  tool: string;
  connection?: string | null;
  limits: Record<string, (string | number | boolean)[]>;
}

export interface Job {
  id: string;
  name: string;
  status: JobStatus;
  version: number;
  approved_version: number | null;
  needs_approval: boolean;
  approval_reasons: string[];
  schedule: JobSchedule;
  schedule_text: string;
  next_fire_at: string | null;
  next_fires: string[];
  last_fire_at: string | null;
  prompt: string;
  skills: string[];
  model: string | null;
  mode: "agent" | "script" | (string & {});
  script: string | null;
  deliver: string;
  failure_deliver: string | null;
  preapproved_tools: PreapprovedTool[];
  attach_to_session: boolean;
  workspace_id: string;
  agent_profile_id: string;
  consecutive_failures: number;
  paused_reason: string | null;
  created_by_kind: "user" | "agent" | (string & {});
  created_at: string;
  updated_at: string;
  /** The last fires, newest first. Only `GET /orgs/{org_id}/jobs/{job_id}` fills it. */
  fires: JobFire[];
}

/**
 * The body of `POST` and `PATCH /orgs/{org_id}/jobs`. Each field is optional. A field that is
 * not there does not change in an update.
 */
export interface JobInput {
  name?: string;
  schedule?: string | JobSchedule;
  tz?: string;
  prompt?: string;
  skills?: string[];
  model?: string;
  mode?: "agent" | "script";
  script?: string;
  deliver?: string;
  failure_deliver?: string;
  preapproved_tools?: PreapprovedTool[];
  attach_to_session?: boolean;
  agent_profile_id?: string;
  workspace_id?: string;
}

/** The answer of `POST /jobs/preview`. */
export interface SchedulePreview {
  schedule: JobSchedule;
  text: string;
  next_fires: string[];
}

// -- errors -------------------------------------------------------------------------------------

/** The status of a card on the task board (docs/architecture.md, Section 12.4). */
export type TaskStatus = "todo" | "running" | "blocked" | "done" | "cancelled";

export interface TaskOut {
  id: string;
  workspace_id: string;
  title: string;
  body: string;
  status: TaskStatus;
  audience: Audience;
  owner_user_id: string | null;
  assignee_profile_id: string | null;
  assignee_user_id: string | null;
  parent_task_id: string | null;
  depends_on: string[];
  session_id: string | null;
  run_id: string | null;
  result: string | null;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface TaskList {
  /** The user row of the caller in the org of the workspace. */
  user_id: string;
  tasks: TaskOut[];
}

export interface TaskComment {
  id: string;
  task_id: string;
  author_kind: "user" | "agent" | "system";
  author_user_id: string | null;
  run_id: string | null;
  body: string;
  created_at: string;
}

export interface TaskDetail {
  task: TaskOut;
  comments: TaskComment[];
}

export interface NewTaskBody {
  title: string;
  body: string;
  audience: Audience;
  assignee_profile_id: string | null;
  assignee_user_id: string | null;
}

export interface TaskPatch {
  status?: Exclude<TaskStatus, "running">;
  title?: string;
  body?: string;
  result?: string;
  assignee?: "agent" | "person" | "none";
  assignee_profile_id?: string;
  assignee_user_id?: string;
}

// -- M3.12: memory, skills, search, export, traces, settings --------------------------------

export type MemoryTarget = "agent" | "user" | "workspace";

export interface MemoryNotes {
  target: MemoryTarget;
  entries: string[];
  chars: number;
  cap: number;
  revision: number;
  can_edit: boolean;
}

export interface MemoryView {
  workspace_id: string;
  targets: MemoryNotes[];
}

export interface MemoryRevision {
  seq: number;
  action: string;
  entries: string[];
  author_kind: string;
  author_user_id: string | null;
  session_id: string | null;
  run_id: string | null;
  reason: string | null;
  reverted_to: number | null;
  created_at: string;
}

export interface MemoryWritten {
  target: MemoryTarget;
  entries: string[];
  chars: number;
  cap: number;
  revision: number;
}

export type SkillScope = "builtin" | "org" | "workspace" | "user";
export type WritableSkillScope = Exclude<SkillScope, "builtin">;
export type SkillAction = "create" | "edit" | "write_file" | "remove_file" | "archive";

export interface SkillSummary {
  /** `null` for a built-in skill. */
  id: string | null;
  name: string;
  description: string;
  scope: SkillScope;
  version: string;
  tags: string[];
  can_edit: boolean;
}

export interface SkillCatalog {
  skills: SkillSummary[];
  writable_scopes: WritableSkillScope[];
}

export interface SkillDetail extends SkillSummary {
  requires_toolsets: string[];
  skill_md: string;
  files: Record<string, string>;
  /** The other scopes that have a skill with the same name. */
  other_scopes: SkillScope[];
}

export interface SkillVersion {
  version: number;
  description: string;
  change: string;
  summary: string | null;
  author_kind: string;
  author_user_id: string | null;
  session_id: string | null;
  run_id: string | null;
  created_at: string;
}

export interface SkillVersionDetail {
  skill_id: string;
  version: number;
  name: string;
  description: string;
  change: string;
  summary: string | null;
  author_kind: string;
  author_user_id: string | null;
  created_at: string;
  skill_md: string;
  files: Record<string, string>;
}

export interface SkillWriteBody {
  action: SkillAction;
  name: string;
  scope: WritableSkillScope;
  content?: string;
  file_path?: string;
  file_content?: string;
  summary?: string;
}

export interface SkillWritten {
  name: string;
  scope: WritableSkillScope;
  version: number;
  change: string;
}

export interface SearchSnippet {
  message_id: string;
  role: string;
  created_at: string;
  text: string;
}

export interface SearchHit {
  session_id: string;
  workspace_id: string;
  title: string | null;
  audience: string;
  created_at: string;
  updated_at: string;
  snippets: SearchSnippet[];
}

export type ExportFormat = "markdown" | "json";

export interface ExportFile {
  name: string;
  blob: Blob;
}

export interface RunSummary {
  id: string;
  session_id: string;
  kind: string;
  status: string;
  runtime: string;
  compact: boolean;
  triggered_by_user_id: string | null;
  failure_code: string | null;
  failure_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  input_tokens: number;
  output_tokens: number;
  /** A decimal number as a string, for example "0.0123". */
  cost_usd: string;
  model_calls: number;
}

export interface TraceStep {
  seq: number;
  /** The event type. `text` is a run of `text_delta` events, merged. */
  type: string;
  created_at: string;
  payload: Record<string, unknown>;
}

export interface TraceToolCall {
  call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  decision: string | null;
  status: string;
  result_preview: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface TraceModelCall {
  provider: string;
  model: string;
  endpoint: string;
  status_code: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: string;
  duration_ms: number;
  created_at: string;
}

export interface RunTrace {
  run: RunSummary;
  steps: TraceStep[];
  tool_calls: TraceToolCall[];
  model_calls: TraceModelCall[];
  truncated: boolean;
}

export type ChatChannel = "telegram" | "slack";

export interface IntegrationView {
  id: string;
  kind: string;
  status: string;
  external_id: string | null;
  name: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface ChannelIdentity {
  id: string;
  org_id: string;
  workspace_id: string;
  agent_profile_id: string;
  integration_id: string;
  created_at: string;
}

export interface PairingCode {
  code: string;
  kind: "user" | "group";
  expires_at: string;
  bot_username: string | null;
  link: string | null;
  command: string;
}

/** The file name in a `Content-Disposition: attachment; filename="…"` header, else `fallback`. */
export function attachmentName(header: string | null, fallback: string): string {
  const match = header ? /filename="?([^";]+)"?/i.exec(header) : null;
  const name = match?.[1]?.trim();
  return name && !/[\\/]/.test(name) ? name : fallback;
}

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
  listConnectors = () => this.json<Connector[]>("/connectors");
  listConnections = (workspaceId: string) =>
    this.json<Connection[]>(`/workspaces/${workspaceId}/connections`);
  startConnection = (workspaceId: string, connector: string, scope: ConnectionScope) =>
    this.json<StartedConnection>(`/workspaces/${workspaceId}/connections`, {
      method: "POST",
      body: { connector, scope },
    });
  checkConnection = (id: string) =>
    this.json<Connection>(`/connections/${id}/check`, { method: "POST" });
  /** Sends the form. The secret values go only to this call, never to the chat. */
  submitConnectionForm = (id: string, values: Record<string, string>) =>
    this.json<Connection>(`/connections/${id}/form`, { method: "PUT", body: { values } });
  disableConnection = (id: string) =>
    this.json<Connection>(`/connections/${id}/disable`, { method: "POST" });
  deleteConnection = (id: string) => this.json<null>(`/connections/${id}`, { method: "DELETE" });
  listTasks = (workspaceId: string) =>
    this.json<TaskList>(`/workspaces/${encodeURIComponent(workspaceId)}/tasks`);
  getTask = (workspaceId: string, taskId: string) =>
    this.json<TaskDetail>(`/workspaces/${encodeURIComponent(workspaceId)}/tasks/${taskId}`);
  createTask = (workspaceId: string, body: NewTaskBody) =>
    this.json<TaskOut>(`/workspaces/${encodeURIComponent(workspaceId)}/tasks`, {
      method: "POST",
      body,
    });
  updateTask = (workspaceId: string, taskId: string, patch: TaskPatch) =>
    this.json<TaskOut>(`/workspaces/${encodeURIComponent(workspaceId)}/tasks/${taskId}`, {
      method: "PATCH",
      body: patch,
    });
  addTaskComment = (workspaceId: string, taskId: string, body: string) =>
    this.json<TaskComment>(
      `/workspaces/${encodeURIComponent(workspaceId)}/tasks/${taskId}/comments`,
      { method: "POST", body: { body } },
    );
  previewSchedule = (schedule: string | JobSchedule, tz?: string, signal?: AbortSignal) =>
    this.json<SchedulePreview>("/jobs/preview", {
      method: "POST",
      body: tz ? { schedule, tz } : { schedule },
      signal,
    });
  listJobs = (orgId: string) => this.json<Job[]>(`/orgs/${orgId}/jobs`);
  getJob = (orgId: string, jobId: string) => this.json<Job>(`/orgs/${orgId}/jobs/${jobId}`);
  createJob = (orgId: string, body: JobInput) =>
    this.json<Job>(`/orgs/${orgId}/jobs`, { method: "POST", body });
  updateJob = (orgId: string, jobId: string, body: JobInput) =>
    this.json<Job>(`/orgs/${orgId}/jobs/${jobId}`, { method: "PATCH", body });
  /** Approves one version of a job. The API refuses a version that is not the current one. */
  approveJob = (orgId: string, jobId: string, version: number) =>
    this.json<Job>(`/orgs/${orgId}/jobs/${jobId}/approve`, { method: "POST", body: { version } });
  pauseJob = (orgId: string, jobId: string) =>
    this.json<Job>(`/orgs/${orgId}/jobs/${jobId}/pause`, { method: "POST" });
  resumeJob = (orgId: string, jobId: string) =>
    this.json<Job>(`/orgs/${orgId}/jobs/${jobId}/resume`, { method: "POST" });
  /** Starts a fire now, outside the schedule. */
  runJob = (orgId: string, jobId: string) =>
    this.json<{ fire_id: string }>(`/orgs/${orgId}/jobs/${jobId}/run`, { method: "POST" });
  /** Cancels the runs of the job that are open. */
  cancelJob = (orgId: string, jobId: string) =>
    this.json<{ cancelled: number }>(`/orgs/${orgId}/jobs/${jobId}/cancel`, { method: "POST" });
  deleteJob = (orgId: string, jobId: string) =>
    this.json<null>(`/orgs/${orgId}/jobs/${jobId}`, { method: "DELETE" });
  // -- M3.12 ------------------------------------------------------------------------------------
  getMemory = (workspaceId: string, agentProfileId: string | null) =>
    this.json<MemoryView>(
      `/workspaces/${encodeURIComponent(workspaceId)}/memory${query({ agent_profile_id: agentProfileId })}`,
    );
  listMemoryRevisions = (
    workspaceId: string,
    target: MemoryTarget,
    agentProfileId: string | null,
    limit = 20,
  ) =>
    this.json<MemoryRevision[]>(
      `/workspaces/${encodeURIComponent(workspaceId)}/memory/${target}/revisions${query({
        agent_profile_id: agentProfileId,
        limit,
      })}`,
    );
  editMemory = (
    workspaceId: string,
    target: MemoryTarget,
    entries: string[],
    agentProfileId: string | null,
    reason: string | null,
  ) =>
    this.json<MemoryWritten>(`/workspaces/${encodeURIComponent(workspaceId)}/memory/${target}`, {
      method: "PUT",
      body: { entries, agent_profile_id: agentProfileId, reason },
    });
  revertMemory = (
    workspaceId: string,
    target: MemoryTarget,
    seq: number,
    agentProfileId: string | null,
    reason: string | null,
  ) =>
    this.json<MemoryWritten>(
      `/workspaces/${encodeURIComponent(workspaceId)}/memory/${target}/revisions/${seq}/revert`,
      { method: "POST", body: { agent_profile_id: agentProfileId, reason } },
    );
  listSkills = (workspaceId: string) =>
    this.json<SkillCatalog>(`/workspaces/${encodeURIComponent(workspaceId)}/skills`);
  getSkill = (workspaceId: string, name: string, scope: SkillScope | null = null) =>
    this.json<SkillDetail>(
      `/workspaces/${encodeURIComponent(workspaceId)}/skills/${encodeURIComponent(name)}${query({ scope })}`,
    );
  writeSkill = (workspaceId: string, body: SkillWriteBody) =>
    this.json<SkillWritten>(`/workspaces/${encodeURIComponent(workspaceId)}/skills`, {
      method: "POST",
      body,
    });
  listSkillVersions = (skillId: string) =>
    this.json<SkillVersion[]>(`/skills/${encodeURIComponent(skillId)}/versions`);
  getSkillVersion = (skillId: string, version: number) =>
    this.json<SkillVersionDetail>(`/skills/${encodeURIComponent(skillId)}/versions/${version}`);
  searchSessions = (orgId: string, q: string, workspaceId: string | null, limit = 10) =>
    this.json<SearchHit[]>(
      `/orgs/${encodeURIComponent(orgId)}/session-search${query({ q, workspace_id: workspaceId, limit })}`,
    );
  /** The export file of a session. The body is a file, not JSON. */
  exportSession = async (id: string, format: ExportFormat): Promise<ExportFile> => {
    const generation = this.tokens.generation;
    const response = await this.raw(`/sessions/${id}/export${query({ format })}`, {}, generation);
    if (!response.ok) {
      const data: unknown = await response.json().catch(() => null);
      this.assertCurrent(generation);
      throw new ApiError(response.status, errorMessage(response.status, data));
    }
    const blob = await response.blob();
    this.assertCurrent(generation);
    const fallback = `session-${id.slice(0, 8)}.${format === "json" ? "json" : "md"}`;
    return { name: attachmentName(response.headers.get("Content-Disposition"), fallback), blob };
  };
  listRuns = (sessionId: string, limit = 50) =>
    this.json<RunSummary[]>(`/sessions/${sessionId}/runs${query({ limit })}`);
  getRunTrace = (runId: string) => this.json<RunTrace>(`/runs/${encodeURIComponent(runId)}/trace`);
  listIntegrations = (orgId: string) =>
    this.json<IntegrationView[]>(`/orgs/${encodeURIComponent(orgId)}/integrations`);
  /** Sends the bot token. It goes only to this call, never to the chat. */
  connectTelegram = (orgId: string, botToken: string) =>
    this.json<IntegrationView>(`/orgs/${encodeURIComponent(orgId)}/integrations/telegram`, {
      method: "POST",
      body: { bot_token: botToken },
    });
  disconnectIntegration = (orgId: string, kind: ChatChannel) =>
    this.json<null>(`/orgs/${encodeURIComponent(orgId)}/integrations/${kind}`, { method: "DELETE" });
  listIdentities = (channel: ChatChannel) =>
    this.json<ChannelIdentity[]>(`/channels/${channel}/identities`);
  unlinkIdentity = (channel: ChatChannel, identityId: string) =>
    this.json<null>(`/channels/${channel}/identities/${encodeURIComponent(identityId)}`, {
      method: "DELETE",
    });
  createPairingCode = (
    channel: ChatChannel,
    workspaceId: string,
    agentProfileId: string,
    kind: "user" | "group" = "user",
  ) =>
    this.json<PairingCode>(`/channels/${channel}/pairing-codes`, {
      method: "POST",
      body: { workspace_id: workspaceId, agent_profile_id: agentProfileId, kind },
    });
}

/** A query string (`?a=1&b=2`) of the values that are not `null`, else "". */
export function query(values: Record<string, string | number | null | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined) params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
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
