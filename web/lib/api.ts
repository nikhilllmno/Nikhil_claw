// The types of the sama API (src/sama/api/app.py) and a small client for it.
// Every call sends `Authorization: Bearer <access token>`. On 401 the client refreshes the
// token once and tries the call again. If that fails, it signs the user out.
// Each call belongs to the sign-in generation that it started in. If the account changes
// while the call waits, the answer is dropped: it is never retried, and it never signs out
// the new account.

import type {
  LibraryAgent,
  LibraryCreate,
  LibraryPatch,
  LibraryTestBody,
  LibraryTestResult,
  LibraryVersion,
  SubagentCatalog,
} from "./subagents";

// -- types --------------------------------------------------------------------------------------

export interface PublicConfig {
  supabase_url: string | null;
  supabase_anon_key: string | null;
  integrations: boolean;
  /** True when the API has the connection routes. */
  connections?: boolean;
  /** `open`: a user with a confirmed email can make an org. `invite_only`: only invites. */
  signup?: SignupMode;
}

export type SignupMode = "open" | "invite_only";

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
  /** The last error of the app. Only the answer of a connect has it. */
  last_error?: string | null;
}

export interface Org {
  id: string;
  name: string;
  /** The short name of the org. A delete of the org asks for it. */
  slug: string;
  user_id: string;
  display_name: string | null;
  /** True for an org owner or admin. */
  admin: boolean;
  /** The org role of the user. */
  role: OrgRole;
  /** The plan of the org, for example `free` or `team`. */
  plan: string;
  workspaces: Workspace[];
  agent_profiles: AgentProfileSummary[];
  integrations: IntegrationSummary[];
  /** The region of the org, for example `ap-south-1` (Section 17.3). */
  region: string;
  /** True when this deployment serves the region. Else the lists above are empty. */
  region_here: boolean;
  /** The address of the deployment that serves the region, if it is known. */
  region_url: string | null;
  /** The permissions of the role of the user (Section 17.7, M7.10). An old API has none. */
  permissions?: PermissionKey[];
}

export type ModelProvider = "anthropic" | "openai";

/** The model key of one provider for an org. It has no part of the key. */
export interface ModelKeyStatus {
  provider: ModelProvider;
  /** True when the org has its own key in the vault. */
  org_key: boolean;
  updated_at: string | null;
  /** True when sama has a key for orgs without their own key. */
  default_key: boolean;
}

export type ApiKeyScope = "chat" | "mcp";
export type ApiKeyApprovals = "deny" | "pending";

/** An org API key for sama as a server (Section 10.7). It has no part of the key after the prefix. */
export interface ApiKey {
  id: string;
  name: string;
  /** The first characters of the key, to recognise it. */
  prefix: string;
  workspace_id: string;
  workspace_name: string;
  agent_profile_id: string | null;
  agent_profile_name: string | null;
  scopes: ApiKeyScope[];
  approvals: ApiKeyApprovals;
  user_id: string;
  user_name: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  status: "active" | "revoked" | "expired";
}

export interface ApiKeyCreate {
  name: string;
  workspace_id: string;
  agent_profile_id: string | null;
  scopes: ApiKeyScope[];
  approvals: ApiKeyApprovals;
  expires_in_days: number | null;
}

/** The answer of a key create. `key` shows one time only. */
export interface ApiKeyCreated extends ApiKey {
  key: string;
}

/** The scope of a custom tool (Section 10.8). */
export type CustomToolScope = "user" | "workspace" | "org";
export type CustomToolKind = "http" | "script";
export type CustomToolPolicy = "allow" | "ask" | "deny";
export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD";

export interface CustomToolHttp {
  method: HttpMethod;
  /** `https://host/path?x={{arg}}`. A placeholder can be in the path and the query only. */
  url: string;
  headers: Record<string, string>;
  /** A JSON value with placeholders, or null for no body. */
  body: unknown;
  auth: { type: "none" | "bearer" | "api_key" | "basic"; header?: string | null; query?: string | null };
  response: { path?: string | null; fields: string[]; max_chars: number };
}

export interface CustomToolScript {
  language: "python" | "node";
  code: string;
  timeout_s: number;
}

/** One definition: what the API takes for a new version. */
export interface CustomToolDefinition {
  kind: CustomToolKind;
  description: string;
  input_schema: Record<string, unknown>;
  policy: CustomToolPolicy;
  http?: CustomToolHttp | null;
  script?: CustomToolScript | null;
}

export interface CustomToolVersion extends CustomToolDefinition {
  version: number;
  change: "create" | "edit" | "revert";
  summary: string;
  review: "approved" | "pending" | "rejected";
  author_user_id: string | null;
  reviewed_by_user_id: string | null;
  reviewed_at: string | null;
  created_at: string;
}

/** A custom tool. It has no credential: the API never sends one back. */
export interface CustomTool {
  id: string;
  scope: CustomToolScope;
  workspace_id: string | null;
  user_id: string | null;
  name: string;
  /** The name that the agent uses: `custom_<name>`. */
  tool_name: string;
  /** The version that runs. Null only for a tool from before M7.3 removed proposals. */
  current_version: number | null;
  current: CustomToolVersion | null;
  latest: CustomToolVersion | null;
  has_credential: boolean;
  can_manage: boolean;
  created_at: string;
  updated_at: string;
}

export interface CustomToolCredential {
  value?: string;
  username?: string;
  password?: string;
}

export interface CustomToolCreate extends CustomToolDefinition {
  name: string;
  scope: CustomToolScope;
  summary?: string;
  credential?: CustomToolCredential | null;
}

export interface CustomToolTestResult {
  ok: boolean;
  result: string | null;
  error: string | null;
  ms: number;
  version: number;
}

/** A saved login of the browser vault. It has no user name and no password. */
export interface BrowserLogin {
  id: string;
  name: string;
  /** The origin where sama fills the login, for example `https://github.com`. */
  site: string;
  scope: "org" | "personal";
  user_id: string | null;
  created_at: string;
  updated_at: string;
  /** True when the login belongs to the user who asks. */
  mine: boolean;
  can_manage: boolean;
}

export interface BrowserLoginAdd {
  name: string;
  site: string;
  username: string;
  password: string;
  scope: "org" | "personal";
}

/** The URL policy of the browser of an org. */
export interface BrowserUrlPolicy {
  allow_hosts: string[];
  block_hosts: string[];
  mode: "allow_list" | "block_list";
  updated_at: string | null;
}

export interface Me {
  orgs: Org[];
}

export type OrgRole = "owner" | "admin" | "member";
/** An invite cannot give the role owner. */
export type InviteRole = "admin" | "member";

export interface CreatedOrg {
  id: string;
  name: string;
  slug: string;
  plan: string;
  user_id: string;
  workspace_id: string;
  agent_profile_id: string;
}

export interface OrgMember {
  user_id: string;
  display_name: string;
  email: string | null;
  role: OrgRole;
  joined_at: string | null;
  workspace_ids: string[];
}

export interface OrgInvite {
  id: string;
  email: string;
  role: InviteRole;
  workspace_id: string | null;
  invited_by: string | null;
  expires_at: string | null;
  created_at: string | null;
  expired: boolean;
}

/** A new invite. The API gives the token only here, once. */
export interface CreatedInvite extends OrgInvite {
  token: string;
}

/** An open invite for the email of the user. */
export interface MyInvite {
  id: string;
  org_id: string;
  org_name: string;
  role: InviteRole;
  invited_by: string | null;
  expires_at: string | null;
}

export interface AcceptedInvite {
  org_id: string;
  org_name: string;
  user_id: string;
  role: InviteRole;
}

export type InviteAnswer = { token: string } | { invite_id: string };

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
  /** True when the user can rename or archive the session (M7.10). An old API has none. */
  can_manage?: boolean;
  /** The coding agent of the direct mode (M7.2), or `null` for sama. An old API has none. */
  coding_agent?: CodingAgentName | null;
  /** The state of the chat runs (only in the list, M8.2). `null`: no run yet. */
  run_state?: SessionRunState | null;
  /** The start of the newest chat run (only in the list, M8.2). */
  last_activity_at?: string | null;
  /**
   * The runtime of the session (M8.2). `GET /sessions/{id}` and `PATCH /sessions/{id}` fill it
   * and the next three fields. `GET /sessions` leaves them `null` or `false`. An old API has none.
   */
  runtime?: string | null;
  /** The model that the next run of the session uses. */
  model?: string | null;
  /** The model without a choice of the user (the model that `model: null` goes back to). */
  default_model?: string | null;
  /** True when a person chose a model that is not the default one. */
  model_is_override?: boolean;
}

/** The answer of `GET /sessions/{id}/models` (M8.2): what a model picker shows. */
export interface SessionModels {
  runtime: string;
  /** The model ids that the runtime knows, sorted. Empty for a coding agent. */
  models: string[];
  /** The model that the next run uses. */
  model: string;
  default_model: string;
  is_override: boolean;
  /** True when the user can change the model of this session. */
  editable: boolean;
}

export type SessionRunState = "running" | "waiting" | "failed" | "done";

export type CodingAgentName = "claude_code" | "codex";

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
  /**
   * The images that the user sent with the message (M8.2), in the order of the
   * `[image attached: ...]` lines of `text`. Load one with `getAttachment`.
   */
  images?: MessageImage[];
}

/** The metadata of an image of a message. The bytes come from `getAttachment`. */
export interface MessageImage {
  id: string;
  name: string;
  mime_type: string;
  size_bytes: number;
}

/** The rules of the API for the images of one message (`sama.media.images`, M8.2). */
export const MAX_IMAGES_PER_MESSAGE = 4;
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
export const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"] as const;

/**
 * The reason why the API would refuse these files, or `null`. The API checks the first bytes of
 * each file too, so this is only a fast check before the upload.
 */
export function imageProblem(files: readonly File[]): string | null {
  if (files.length > MAX_IMAGES_PER_MESSAGE) {
    return `sama reads only the first ${MAX_IMAGES_PER_MESSAGE} images of a message.`;
  }
  for (const file of files) {
    if (!(IMAGE_TYPES as readonly string[]).includes(file.type)) {
      return "sama reads only PNG, JPEG, GIF, and WebP images.";
    }
    if (file.size > MAX_IMAGE_BYTES) return "That image is too large. The limit is 10 MB.";
    if (file.size === 0) return "sama could not read that image. Send it again.";
  }
  return null;
}

/** The answer of `POST /sessions/{id}/messages`. */
export interface SentMessage {
  run_id: string;
  /** True when the message waits behind an open run of the session (M3.9). */
  queued: boolean;
  /**
   * True when the message joined the running run at its next step (busy mode `steer`, M7.1).
   * `run_id` is then the running run, and the note is in `GET /sessions/{id}/steers`.
   */
  steered?: boolean;
}

export type SteerStatus = "pending" | "delivered" | "requeued" | "dropped";

/** One row of `GET /sessions/{id}/steers` (M7.1): a message that joined a run. */
export interface SteerNote {
  id: string;
  run_id: string;
  text: string;
  status: SteerStatus;
  created_at: string;
  delivered_at: string | null;
  requeued_run_id: string | null;
}


/** One row of `GET /sessions/{id}/open-runs` (M7.1), oldest first. */
export interface OpenRun {
  /** The place of the run for `/stop NUMBER`, from 1. */
  number: number;
  run_id: string;
  status: string;
  /** True when the run started next to another open run. */
  side: boolean;
  text: string;
}

/** The answer of `POST /sessions/{id}/steer`. */
export interface Steered {
  /** True when the note went to the open run. False when the API sent it as a message. */
  steered: boolean;
  /** The open run for a note, or the new run for a message. */
  run_id: string;
  queued: boolean;
}

/** The answer of `POST /sessions/{id}/handoff`. */
export interface Handoff {
  to_channel: string;
  /** The answer for the user. */
  text: string;
}

/** A checkpoint of the session files (`GET /sessions/{id}/checkpoints`). */
export interface CheckpointItem {
  number: number;
  /** A stable id of the checkpoint (40 to 64 hex characters). The number can shift. */
  commit: string;
  created_at: string;
  tool: string;
  kind: string;
}

/** The answer of `POST /sessions/{id}/undo` and `/rollback`. */
export interface Restored {
  created_at: string;
  files: number;
  deleted: number;
  /** Large or excluded items that the restore did not replace. */
  kept: number;
  /** The answer for the user. */
  text: string;
}

/** The answer of `POST /sessions/{id}/coding-agent`. */
export interface CodingAgentAnswer {
  /** The answer for the user. */
  text: string;
}

/** A repo that is attached to a session (M7.9). */
export interface RepoOut {
  id: string;
  /** `owner/name`. */
  repo: string;
  url: string;
  /** The branch or tag the user asked for. `null`: the default branch. */
  ref: string | null;
  folder: string;
  /** Set when the status is `ready`. */
  path: string | null;
  /** A `pending` repo clones into the sandbox when the next run starts. */
  status: "pending" | "ready" | "failed";
  /** True: the repo clones through the sama git proxy and can push `sama/*` branches. */
  proxied: boolean | null;
  commit: string | null;
  branch: string | null;
  /** Set when the status is `failed`. */
  error: string | null;
  created_at: string;
}

export type PlanStatus = "proposed" | "approved" | "rejected" | "superseded";

/** A plan that the agent proposed in plan mode (M7.9). */
export interface PlanOut {
  id: string;
  version: number;
  /** The text of the plan, as Markdown. After an edit, this is the edited text. */
  body: string;
  /** The text that the agent proposed. */
  proposed_body: string;
  status: PlanStatus;
  edited: boolean;
  /** The text of a reject. */
  feedback: string | null;
  run_id: string | null;
  decided_at: string | null;
  created_at: string;
}

/** The answer of `GET /sessions/{id}/coding`. */
export interface CodingOut {
  plan_mode: boolean;
  repos: RepoOut[];
  /** The newest plan of the session. */
  plan: PlanOut | null;
}

/** The body of `POST /sessions/{id}/repos`. */
export interface NewRepo {
  /** A GitHub URL or `owner/name`. */
  repo: string;
  ref?: string;
  folder?: string;
}

/** The answer of the plan routes that start a run. */
export interface PlanRun {
  run_id: string;
  queued: boolean;
}

/** The answer of `POST /sessions/{id}/plans/{plan_id}/approve`. */
export interface PlanApproved extends PlanRun {
  plan: PlanOut;
}

/** The answer of `POST /sessions/{id}/plans/{plan_id}/reject`. Without feedback, no run starts. */
export interface PlanRejected {
  run_id: string | null;
  queued: boolean;
  plan: PlanOut;
}

/** A checkpoint that a diff starts or ends at. */
export interface DiffPoint {
  number: number;
  /** A stable id of the checkpoint (40 to 64 hex characters). */
  commit: string;
  created_at: string;
  tool: string;
  kind: string;
}

export type FileStatus = "added" | "modified" | "deleted" | "renamed" | "type_changed";

/** One changed file of a diff. */
export interface DiffFile {
  path: string;
  old_path: string | null;
  status: FileStatus;
  /** `null` for a binary file. */
  additions: number | null;
  deletions: number | null;
  binary: boolean;
  /** The unified diff of the file. It starts with `diff --git`. */
  patch: string;
  /** The API cut the patch of this file. */
  truncated: boolean;
}

/** The answer of `GET /sessions/{id}/diff`. */
export interface DiffOut {
  base: DiffPoint;
  /** `null`: the current files. */
  head: DiffPoint | null;
  files: DiffFile[];
  /** The API cut the list of files or the total text. */
  truncated: boolean;
}

/** A command or test run of the agent (`GET /sessions/{id}/commands`). */
export interface CommandOut {
  run_id: string;
  call_id: string;
  tool: string;
  command: string;
  /** True when sama thinks the command runs tests. */
  test: boolean;
  status: "running" | "ok" | "failed";
  /** The end of the output (at most 4,000 characters). */
  output: string;
  /** A result line that sama found in the output. */
  summary: string | null;
  started_at: string;
  finished_at: string | null;
}

/** A pull request of the agent (`GET /sessions/{id}/pull-requests`). */
export interface PullRequestOut {
  run_id: string;
  call_id: string;
  repo: string | null;
  title: string | null;
  head: string | null;
  base: string | null;
  url: string | null;
  status: "running" | "opened" | "failed";
  /** The tool result or the error (at most 2,000 characters). */
  output: string;
  created_at: string;
}

/** The body of `POST /sessions/{id}/pull-requests`. */
export interface NewPullRequest {
  repo?: string;
  title?: string;
  base?: string;
  draft?: boolean;
}

/**
 * How far an approval reaches. `once`: this call. `session`: also the same tool for the rest of
 * this session. `always`: also this tool for this person and this agent profile from now on.
 */
export type ApprovalScope = "once" | "session" | "always";

/** The answer of `POST /approvals/{id}/approve` and `/deny`. */
export interface ApprovalDecision {
  status: string;
  /** The scope that the server applied. It can be narrower than the scope that was sent. */
  scope?: ApprovalScope;
  resumed: boolean;
}

export interface Approval {
  id: string;
  run_id: string;
  call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  reason: string;
  /** True for a proposal of a change. A proposal has no "Always allow". */
  proposal?: boolean;
  /** The scopes that a person may choose. A proposal and a tool that always asks have only `once`. */
  scopes?: ApprovalScope[];
  /** The diff of a change of an agent profile (M6.1), or null. */
  diff?: string | null;
  created_at: string;
  expires_at: string;
}

export interface ClarifyQuestion {
  question: string;
  choices: string[];
  /** A question of the native AskUserQuestion tool of Claude (M8.3) also has these. */
  header?: string;
  descriptions?: string[];
  multi_select?: boolean;
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
  /** The `run_events.id`: one order for all runs of the session. Not on every event. */
  id?: number;
  run_id: string;
  seq: number;
  type: string;
  payload: Record<string, unknown>;
}

/** The answer of `GET /sessions/{id}/activity`: the tool trail of the newest runs. */
export interface SessionActivity {
  events: RunEventPayload[];
  /** True when older events were left out. */
  truncated: boolean;
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

export type MemoryProvider = "builtin" | "mem0";

export interface AgentProfileOptions {
  toolsets: ToolsetOption[];
  /** The models of the saved runtime. */
  models: string[];
  /** True if the saved runtime has sub-agents. */
  subagents: boolean;
  runtimes: RuntimeOption[];
  /** The long-term memory providers, for example `builtin` and `mem0`. */
  /** Since M7.5. */
  memory_providers?: string[];
  limits: AgentProfileLimits;
  /** The reasoning efforts that a profile can choose (M7.10). */
  reasoning_efforts: ReasoningEffort[];
  /** The run budget of the plan of the org. `null`: no cap. */
  budget_caps: BudgetCaps;
  /** The busy modes (M7.1). The API gives them only when it has the busy mode. */
  busy_modes?: BusyMode[];
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
  /** The highest parallel run limit (M7.1). */
  max_parallel_runs?: number;
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
  /** What a message does while a run is open (M7.1). */
  busy_mode?: BusyMode;
  /** The most open runs of one session in the busy mode `parallel`. */
  max_parallel_runs?: number;
  /** The ids of the library sub-agents (M7.4), in order. */
  library_subagents: string[];
  /** The model of the `delegate_task` child runs. `null`: the model of the profile. */
  delegation_model: string | null;
  memory_provider: MemoryProvider;
  updated_at: string;
  /** True if the user is an org owner or admin. */
  editable: boolean;
  options: AgentProfileOptions;
  /** The run budget of the profile (M7.10). */
  budget: ProfileBudget;
  /** `null`: the default of the model. */
  reasoning_effort: ReasoningEffort | null;
}

/** The body of `PATCH /orgs/{org_id}/agent-profiles/{profile_id}`. Each field is optional. */
export interface AgentProfilePatch {
  name?: string;
  soul?: string;
  runtime?: string;
  model?: string;
  toolsets?: string[];
  subagents?: Subagent[];
  budget?: ProfileBudget;
  reasoning_effort?: ReasoningEffort | null;
  busy_mode?: BusyMode;
  max_parallel_runs?: number;
  library_subagents?: string[];
  /** An empty string: use the model of the profile. */
  delegation_model?: string;
  memory_provider?: MemoryProvider;
}

export type ProfileChangeKind = "soul" | "profile" | "toolsets" | "policy";
export type ProfileChangeSource = "proposal" | "edit" | "revert";

/** One row of `GET /orgs/{org_id}/agent-profiles/{profile_id}/changes`, newest first. */
export interface ProfileChange {
  id: string;
  kind: ProfileChangeKind;
  source: ProfileChangeSource;
  summary: string | null;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  diff: string;
  actor_user_id: string | null;
  actor_name: string | null;
  proposal_id: string | null;
  reverts_change_id: string | null;
  reverted_by_change_id: string | null;
  /** True when sama redacted a secret in the values. Such a change cannot be reverted. */
  redacted: boolean;
  created_at: string;
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
  /**
   * `composio`: a sign-in link. `http_api`: a form. `mcp_remote`: a remote MCP server.
   * `mcp_hosted`: a stdio MCP server that sama runs in a connector host.
   */
  kind: string;
  scopes: ConnectionScope[];
  fields: ConnectorField[];
  /** Empty for a remote MCP server: the server lists its tools after the connection. */
  tools: ConnectorTool[];
  mcp?: { url: string; auth: McpAuth; header: string | null } | null;
  hosted?: HostedSpec | null;
  api?: { base_url: string; auth: ApiAuthType } | null;
}

/** The package of a stdio MCP server. The version is one exact version. */
export interface StdioPackage {
  registry: "npm" | "pypi";
  name: string;
  version: string;
  bin?: string | null;
  args?: string[];
}

/** What a connector host runs and what it can reach. It has no secret values. */
export interface HostedSpec {
  package: StdioPackage | null;
  command: string[] | null;
  /** The names of the environment variables. The vault holds the values. */
  env: string[];
  egress: string[];
  memory_mb: number | null;
  idle_s: number | null;
}

/** How sama signs in to an HTTP API. */
export type ApiAuthType = "api_key" | "bearer" | "basic" | "oauth2" | "none";

/** How sama signs in to a remote MCP server. */
export type McpAuth = "oauth" | "bearer" | "header" | "none";

/** One tool of a remote MCP server, as sama shows it to the agent. */
export interface McpTool {
  /** The name for the agent: `mcp_<connection>_<tool>`. */
  name: string;
  remote_name: string;
  description: string;
  access: "read" | "write" | "destructive";
  /** False when a filter of the connection or of the connector turns the tool off. */
  enabled: boolean;
  /** The HTTP method and path of an HTTP API tool, else `null`. */
  method?: string | null;
  path?: string | null;
}

/** A remote MCP server that an admin added by URL. */
export interface McpCustom {
  title: string | null;
  url: string;
  auth: McpAuth;
  header: string | null;
}

/** A stdio MCP server that an admin added. */
export interface HostedCustom extends HostedSpec {
  title: string | null;
}

/** An HTTP API that an admin added. */
export interface ApiCustom {
  title: string | null;
  base_url: string | null;
  auth: ApiAuthType | null;
  auth_name: string | null;
  auth_location: "header" | "query" | null;
  token_url: string | null;
}

/**
 * The tool part of a connection with a tool list (`kind` is `mcp_remote`, `mcp_hosted`, or
 * an HTTP API that an admin added).
 */
export interface ConnectionMcp {
  /**
   * The server or API that an admin added, or `null` for a connector of the catalog. The kind
   * of the connection tells which type it is.
   */
  custom: McpCustom | HostedCustom | ApiCustom | null;
  include: string[];
  exclude: string[];
  discovered_at: string | null;
  /** The operations of an OpenAPI document that sama did not use. */
  skipped?: number | null;
  tools: McpTool[];
}

/** A new stdio MCP server (`POST /workspaces/{id}/connections/stdio`). */
export interface StdioServerAdd {
  name: string;
  title?: string | null;
  scope: ConnectionScope;
  /** A pinned package, or a custom command (the org owner only). */
  package?: StdioPackage | null;
  command?: string[] | null;
  env: string[];
  egress: string[];
  memory_mb?: number | null;
  idle_s?: number | null;
}

/** One endpoint of an HTTP API without an OpenAPI document. */
export interface ApiEndpoint {
  name?: string | null;
  method: "GET" | "PUT" | "POST" | "DELETE" | "PATCH" | "HEAD";
  path: string;
  description?: string | null;
  query: string[];
  body: boolean;
}

/** A new HTTP API (`POST /workspaces/{id}/connections/api`). Give one source of operations. */
export interface ApiAdd {
  name: string;
  title?: string | null;
  scope: ConnectionScope;
  base_url?: string | null;
  auth: {
    type: ApiAuthType;
    name?: string | null;
    location?: "header" | "query";
    token_url?: string | null;
    scope?: string | null;
    client_auth?: "basic" | "body";
  };
  openapi?: string | null;
  openapi_url?: string | null;
  endpoints?: ApiEndpoint[] | null;
  test?: string | null;
}

/** A new remote MCP server by URL (`POST /workspaces/{id}/connections/mcp`). */
export interface McpServerAdd {
  name: string;
  url: string;
  auth: McpAuth;
  header?: string | null;
  oauth_scope?: string | null;
  title?: string | null;
  scope: ConnectionScope;
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
  /** Only for a remote MCP server. */
  mcp?: ConnectionMcp | null;
  /** True when the user can change or delete the connection (M7.10). An old API has none. */
  can_manage?: boolean;
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
  mode: "agent" | "script" | "curator" | (string & {});
  script: string | null;
  deliver: string;
  failure_deliver: string | null;
  preapproved_tools: PreapprovedTool[];
  attach_to_session: boolean;
  workspace_id: string;
  agent_profile_id: string;
  consecutive_failures: number;
  paused_reason: string | null;
  created_by_kind: "user" | "agent" | "system" | (string & {});
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

export type MemoryWritten = Omit<MemoryNotes, "can_edit">;

export type SkillScope = "builtin" | "org" | "workspace" | "user";
export type WritableSkillScope = Exclude<SkillScope, "builtin">;
export type SkillAction = "create" | "edit" | "write_file" | "remove_file" | "archive";

export type SkillSettingValue = string | number | boolean;

/** One setting of a skill, from the `config` block of its SKILL.md. */
export interface SkillSettingSpec {
  key: string;
  type: "string" | "number" | "boolean";
  description: string;
  default: SkillSettingValue | null;
  required: boolean;
}

/** `GET` and `PUT /workspaces/{workspace_id}/skills/{name}/settings`. */
export interface SkillSettings {
  name: string;
  scope: string;
  specs: SkillSettingSpec[];
  /** The stored values. */
  values: Record<string, SkillSettingValue>;
  /** The values in use: the stored value, else the default. */
  effective: Record<string, SkillSettingValue | null>;
  can_edit: boolean;
  /** Why the config block cannot be read. Then `specs` is empty. */
  problem: string | null;
}

export interface SkillSummary {
  /** `null` for a built-in skill. */
  id: string | null;
  name: string;
  description: string;
  scope: SkillScope;
  version: string;
  tags: string[];
  can_edit: boolean;
  /** `stale`: no agent used the skill for 30 days (Section 9.7). It still works. */
  status: "active" | "stale" | (string & {});
  /** A pinned skill stays active. */
  pinned: boolean;
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

export interface ArchivedSkill {
  id: string;
  name: string;
  scope: WritableSkillScope;
  description: string;
  version: number;
  archived_at: string;
  can_restore: boolean;
}

export type SkillStateAction = "pin" | "unpin" | "restore";

export interface SkillState {
  id: string;
  name: string;
  scope: WritableSkillScope;
  status: string;
  pinned: boolean;
  version: number;
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

export type ChatChannel = "telegram" | "slack" | "whatsapp" | "email";

/** The WhatsApp providers that an org can connect (Section 11.9). */
export type WhatsAppProvider = "meta" | "twilio" | "gupshup";

export interface WhatsAppConnect {
  provider: WhatsAppProvider;
  number: string;
  api_token: string;
  app_secret?: string;
  phone_number_id?: string;
  account_sid?: string;
  app_id?: string;
  app_name?: string;
}

export interface EmailConnect {
  address: string;
  api_key: string;
  public_key: string;
  sender_name?: string;
}

/** The answer of a WhatsApp or email connect: the URL for the provider and the steps left. */
export interface ChannelSetup extends IntegrationSummary {
  id: string;
  webhook_url: string;
  verify_token: string | null;
  steps: string[];
}

/** The answer of `POST /orgs/{org_id}/integrations/slack/setup` (Section 11.6). */
export interface SlackSetup {
  integration: IntegrationSummary;
  events_url: string;
  interactions_url: string;
  commands_url: string;
  manifest: Record<string, unknown>;
  /** Opens the Slack page that makes a new app from the manifest. */
  manifest_link: string;
}

/** A new agent profile (M6.9). Null `workspace_id`: a profile of the whole org. */
export interface NewAgentProfile {
  name: string;
  soul?: string;
  runtime?: string;
  model?: string;
  workspace_id?: string | null;
}

// -- admin views (Section 17.6, M6.9) -----------------------------------------------------------

export interface UsageTotals {
  /** A decimal number as a string. */
  cost_usd: string;
  /** The part of `cost_usd` on the own model keys of the org. */
  org_key_cost_usd: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  model_calls: number;
  runs: number;
  sessions: number;
}

export interface UsageDay {
  /** A UTC day, `YYYY-MM-DD`. */
  day: string;
  cost_usd: string;
  input_tokens: number;
  output_tokens: number;
  model_calls: number;
}

export interface UsageRow {
  key: string | null;
  label: string;
  cost_usd: string;
  input_tokens: number;
  output_tokens: number;
  model_calls: number;
}

export interface OrgUsage {
  days: number;
  since: string;
  until: string;
  totals: UsageTotals;
  by_day: UsageDay[];
  by_user: UsageRow[];
  by_profile: UsageRow[];
  by_model: UsageRow[];
  by_key_source: UsageRow[];
}

export interface AuditEntry {
  id: number;
  created_at: string;
  action: string;
  actor_kind: string;
  actor_user_id: string | null;
  actor_name: string | null;
  workspace_id: string | null;
  target_type: string | null;
  target_id: string | null;
  run_id: string | null;
  details: Record<string, unknown>;
}

export interface AuditPage {
  entries: AuditEntry[];
  /** The `before` of the next (older) page. `null` on the last page. */
  next_before: number | null;
}

export interface CuratorJob {
  id: string;
  workspace_id: string;
  workspace_name: string;
  status: string;
  next_fire_at: string | null;
  last_fire_at: string | null;
}

export interface CuratorReport {
  id: string;
  job_id: string;
  workspace_id: string;
  workspace_name: string;
  status: string;
  manual: boolean;
  scheduled_for: string;
  finished_at: string | null;
  output: string | null;
  error: string | null;
  truncated: boolean;
}

export interface CuratorReports {
  jobs: CuratorJob[];
  reports: CuratorReport[];
}

/** `GET /orgs/{org_id}/data-settings` (Section 17.4). Null days: keep everything. */
export interface DataSettings {
  plan: string;
  retention_days: number | null;
  plan_default_days: number | null;
  plan_max_days: number | null;
  effective_days: number | null;
}

/** What a delete removed. */
export interface Removed {
  sessions: number;
  runs: number;
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

// -- billing (docs/architecture.md, Section 17.5) -----------------------------------------------

/** A plan of the price list. Amounts are US dollars as decimal strings, for example `"20.00"`. */
export interface BillingPlan {
  name: string;
  title: string;
  price_usd: string | null;
  included_usd: string;
  paid: boolean;
  max_members: number | null;
  model_usd_per_month: string | null;
  model_usd_per_user_day: string | null;
  max_running_runs: number;
  run_budget: Record<string, unknown>;
}

export interface BillingPlans {
  /** False when the server has no Stripe: nobody can buy a plan. */
  billing_enabled: boolean;
  plans: BillingPlan[];
}

export interface BillingOverview {
  org_id: string;
  billing_enabled: boolean;
  plan: BillingPlan;
  subscription: {
    status: string;
    plan: string;
    current_period_start: string | null;
    current_period_end: string | null;
    cancel_at_period_end: boolean;
    has_subscription: boolean;
  } | null;
  usage: {
    period_start: string | null;
    period_end: string | null;
    model_usd: string | null;
    included_usd: string | null;
    billable_usd: string | null;
    reported_usd: string | null;
  };
  quotas: {
    model_usd_per_month: string | null;
    model_usd_this_month: string | null;
    model_usd_per_user_day: string | null;
    max_members: number | null;
    members: number;
    max_running_runs: number;
    user_messages_per_minute: number;
    org_messages_per_minute: number;
  };
  can_buy: boolean;
  can_change: boolean;
  can_open_portal: boolean;
}

/** The answer of Checkout and of the portal: the Stripe page to open. */
export interface StripeRedirect {
  url: string;
}

// -- M7.10 settings console ---------------------------------------------------------------------

export type ReasoningEffort = "low" | "medium" | "high";
/** `steer`: a new message joins the open run. `parallel`: a second run. `queue`: it waits. */
export type BusyMode = "steer" | "parallel" | "queue";

/** The run budget of a profile. `null`: no limit. `max_cost_usd` is a decimal string. */
export interface ProfileBudget {
  max_turns: number | null;
  max_cost_usd: string | null;
  max_wall_time_s: number | null;
  max_tokens: number | null;
}

export interface BudgetCaps {
  max_turns: number | null;
  max_cost_usd: string | null;
  max_wall_time_s: number | null;
}

export type PermissionKey =
  | "jobs.create"
  | "workflows.create"
  | "triggers.create"
  | "connections.add"
  | "skills.write"
  | "browser.logins"
  | "sessions.shared"
  | "members.invite"
  | "agents.manage"
  | "chat_apps.manage";

export type EditableRole = "admin" | "member";

export interface PermissionInfo {
  key: PermissionKey;
  label: string;
  description: string;
  roles: EditableRole[];
}

/** `GET /orgs/{org_id}/role-permissions`. The owner has every permission. */
export interface RolePermissions {
  permissions: PermissionInfo[];
  roles: Record<EditableRole, Partial<Record<PermissionKey, boolean>>>;
  editable: Record<EditableRole, boolean>;
}

export type PolicyAction = "allow" | "ask" | "deny";

export interface ToolPolicyRule {
  /** An exact tool name, or a prefix that ends in `*`. */
  tool: string;
  action: PolicyAction;
}

/** A change of a rule. `action: null` removes the rule. */
export interface ToolPolicyChange {
  tool: string;
  action: PolicyAction | null;
}

export interface ToolInfo {
  name: string;
  toolset: string;
  /** True when the toolset of the tool is on for the profile. */
  enabled: boolean;
  /** The verdict without a rule. */
  default: PolicyAction;
  /** True when the tool always asks a person (for example `repo_push_approval`). */
  always_ask: boolean;
}

/** `GET /orgs/{org_id}/agent-profiles/{profile_id}/tool-policy`. */
export interface ToolPolicy {
  editable: boolean;
  max_rules: number;
  rules: ToolPolicyRule[];
  org_rules: ToolPolicyRule[];
  tools: ToolInfo[];
}

/** `GET /orgs/{org_id}/tool-policy`: the rules for every agent of the org. */
export interface OrgToolPolicy {
  editable: boolean;
  max_rules: number;
  rules: ToolPolicyRule[];
}

export type TriggerKind = "webhook" | "composio";
export type TriggerStatus = "active" | "disabled";

/** A trigger of a workspace (Section 12.2). */
export interface Trigger {
  id: string;
  workspace_id: string;
  owner_user_id: string;
  agent_profile_id: string | null;
  kind: TriggerKind;
  name: string;
  prompt: string | null;
  toolsets: string[];
  target: "run" | "workflow";
  workflow_id: string | null;
  status: TriggerStatus;
  last_error: string | null;
  created_at: string;
  updated_at: string;
  /** A webhook trigger: the address that takes the signed deliveries. */
  url?: string;
  connection_id?: string | null;
  composio_slug?: string | null;
  composio_config?: Record<string, unknown> | null;
}

/** The answer of a create and of a secret rotation. The API gives the secret only here, once. */
export interface TriggerWithSecret extends Trigger {
  secret?: string;
}

export interface NewTrigger {
  kind: TriggerKind;
  name: string;
  prompt?: string;
  agent_profile_id?: string;
  toolsets?: string[];
  connection_id?: string;
  composio_slug?: string;
  composio_config?: Record<string, unknown>;
  workflow_id?: string;
}

export interface TriggerEdit {
  name?: string;
  prompt?: string;
  toolsets?: string[];
  status?: TriggerStatus;
}

export type DeliveryOutcome =
  | "accepted"
  | "duplicate"
  | "bad_signature"
  | "disabled"
  | "bad_body"
  | "rate_limited"
  | "rejected"
  | "test_ok";

/** One delivery to a trigger, newest first. No payload is kept. */
export interface TriggerDelivery {
  id: string;
  delivery_id: string | null;
  received_at: string;
  outcome: DeliveryOutcome;
  http_status: number;
  error: string | null;
  test: boolean;
  session_id: string | null;
  run_id: string | null;
  workflow_instance_id: string | null;
}

export type WorkflowStatus = "active" | "paused" | "pending_approval" | "removed";
export type InstanceStatus = "running" | "waiting" | "completed" | "failed" | "cancelled";

export interface Workflow {
  id: string;
  name: string;
  description: string | null;
  version: number;
  status: WorkflowStatus;
  next_fire_at: string | null;
  last_fire_at: string | null;
  workspace_id: string;
  agent_profile_id: string;
  created_by_kind: string;
  created_at: string;
  updated_at: string;
}

export interface WorkflowInstance {
  id: string;
  workflow_id: string;
  workflow: string;
  version: number;
  status: InstanceStatus;
  inputs: Record<string, unknown> | null;
  output: unknown;
  error: string | null;
  trigger: string;
  scheduled_for: string | null;
  parent_instance_id: string | null;
  depth: number;
  created_at: string;
  finished_at?: string | null;
}

export interface WorkflowDetail extends Workflow {
  source?: string;
  definition?: Record<string, unknown>;
  hash?: string;
  needs_approval?: boolean;
  approved?: boolean;
  approval_reasons?: string[];
  triggers?: Array<Record<string, unknown> & { text?: string }>;
  instances?: WorkflowInstance[];
}

export interface WorkflowStep {
  step_id: string;
  kind: string;
  status: string;
  input: unknown;
  output: unknown;
  error: string | null;
  attempts: number;
  session_id: string | null;
  run_id: string | null;
  child_instance_id: string | null;
  wake_at: string | null;
  event: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface WorkflowInstanceDetail extends WorkflowInstance {
  steps: WorkflowStep[];
}

export interface WorkflowValidation {
  definition: Record<string, unknown>;
  hash: string;
  needs_approval: boolean;
  approval_reasons: string[];
  next_fire_at: string | null;
}

export interface SessionPatch {
  title?: string;
  status?: "active" | "archived";
  /** A model id of the runtime of the session, or `null` for the model of the profile (M8.2). */
  model?: string | null;
}

/** The answer of the test of a chat app. */
export interface IntegrationCheck {
  ok: boolean;
  detail: string | null;
  integration: IntegrationSummary;
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
  if (status === 402) return "The org used a model quota of its plan.";
  if (status === 404) return "Not found.";
  if (status === 413) return "The upload is too large.";
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
  /** A `multipart/form-data` body. The browser sets the `Content-Type` with the boundary. */
  form?: FormData;
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
        body: options.form ?? (options.body === undefined ? undefined : JSON.stringify(options.body)),
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
  /** Makes an org with the user as the owner (sign-up). */
  createOrg = (name: string, displayName: string | null) =>
    this.json<CreatedOrg>("/orgs", {
      method: "POST",
      body: displayName ? { name, display_name: displayName } : { name },
    });
  billingPlans = () => this.json<BillingPlans>("/billing/plans");
  billingOverview = (orgId: string) =>
    this.json<BillingOverview>(`/orgs/${encodeURIComponent(orgId)}/billing`);
  /** Starts Stripe Checkout for a paid plan. Open the returned URL. */
  startCheckout = (orgId: string, plan: string) =>
    this.json<StripeRedirect>(`/orgs/${encodeURIComponent(orgId)}/billing/checkout`, {
      method: "POST",
      body: { plan },
    });
  /** Moves a paid subscription to another paid plan. Stripe prorates the price. */
  changePlan = (orgId: string, plan: string) =>
    this.json<BillingOverview>(`/orgs/${encodeURIComponent(orgId)}/billing/plan`, {
      method: "PUT",
      body: { plan },
    });
  /** Opens the Stripe customer portal: payment method, invoices, and cancel. */
  openBillingPortal = (orgId: string) =>
    this.json<StripeRedirect>(`/orgs/${encodeURIComponent(orgId)}/billing/portal`, {
      method: "POST",
    });
  listMembers = (orgId: string) =>
    this.json<OrgMember[]>(`/orgs/${encodeURIComponent(orgId)}/members`);
  /** Changes the role or the workspaces (the whole set) of a member. */
  changeMember = (
    orgId: string,
    userId: string,
    change: { role?: OrgRole; workspace_ids?: string[] },
  ) =>
    this.json<OrgMember>(
      `/orgs/${encodeURIComponent(orgId)}/members/${encodeURIComponent(userId)}`,
      { method: "PATCH", body: change },
    );
  /** Removes a member. With the user's own id: leaves the org. */
  removeMember = (orgId: string, userId: string) =>
    this.json<null>(`/orgs/${encodeURIComponent(orgId)}/members/${encodeURIComponent(userId)}`, {
      method: "DELETE",
    });
  listInvites = (orgId: string) =>
    this.json<OrgInvite[]>(`/orgs/${encodeURIComponent(orgId)}/invites`);
  createInvite = (
    orgId: string,
    body: { email: string; role: InviteRole; workspace_id: string | null },
  ) =>
    this.json<CreatedInvite>(`/orgs/${encodeURIComponent(orgId)}/invites`, {
      method: "POST",
      body,
    });
  revokeInvite = (orgId: string, inviteId: string) =>
    this.json<null>(
      `/orgs/${encodeURIComponent(orgId)}/invites/${encodeURIComponent(inviteId)}`,
      { method: "DELETE" },
    );
  myInvites = () => this.json<MyInvite[]>("/invites");
  acceptInvite = (answer: InviteAnswer, displayName: string | null = null) =>
    this.json<AcceptedInvite>("/invites/accept", {
      method: "POST",
      body: displayName ? { ...answer, display_name: displayName } : answer,
    });
  listSessions = (limit = 50, status: "active" | "archived" = "active") =>
    this.json<SessionOut[]>(`/sessions${query({ limit, status: status === "active" ? null : status })}`);
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
  /** `parallel`: start a run next to the open runs (`/bg`, M7.1). */
  sendMessage = (id: string, text: string, parallel = false) =>
    this.json<SentMessage>(`/sessions/${id}/messages`, {
      method: "POST",
      body: parallel ? { text, parallel } : { text },
    });
  /**
   * Sends a message with up to 4 images (M8.2). `text` can be empty when there is an image.
   * The answer is the answer of `sendMessage`. A message with images never joins a running
   * run: it queues, or with `parallel` it starts a run next to the open runs.
   */
  sendMessageWithImages = (id: string, text: string, files: readonly File[], parallel = false) => {
    const form = new FormData();
    form.append("text", text);
    if (parallel) form.append("parallel", "true");
    for (const file of files) form.append("images", file, file.name);
    return this.json<SentMessage>(`/sessions/${id}/messages/images`, { method: "POST", form });
  };
  /**
   * The bytes of an image of a message. An `img` element cannot send the Bearer token, so the
   * web app fetches the image here and shows `URL.createObjectURL(file.blob)`.
   */
  getAttachment = (sessionId: string, attachmentId: string) =>
    this.download(`/sessions/${sessionId}/attachments/${attachmentId}`, "image");
  listSteers = (id: string) => this.json<SteerNote[]>(`/sessions/${id}/steers`);
  listOpenRuns = (id: string) => this.json<OpenRun[]>(`/sessions/${id}/open-runs`);
  /** The tool calls and sub-agent progress of the newest runs, for runs that ended earlier. */
  getActivity = (id: string) => this.json<SessionActivity>(`/sessions/${id}/activity`);
  /** A note for the open run. Without an open run the API sends the text as a message. */
  steer = (id: string, text: string) =>
    this.json<Steered>(`/sessions/${id}/steer`, { method: "POST", body: { text } });
  handoff = (id: string, to: string) =>
    this.json<Handoff>(`/sessions/${id}/handoff`, { method: "POST", body: { to } });
  /** Starts (`claude_code` or `codex`) or ends (`null`) the direct mode of a session (M7.2). */
  codingAgent = (id: string, agent: CodingAgentName | null) =>
    this.json<CodingAgentAnswer>(`/sessions/${id}/coding-agent`, { method: "POST", body: { agent } });
  listCheckpoints = (id: string) => this.json<CheckpointItem[]>(`/sessions/${id}/checkpoints`);
  undo = (id: string) => this.json<Restored>(`/sessions/${id}/undo`, { method: "POST" });
  /**
   * Restores a checkpoint. With `commit`, the API restores only if checkpoint `number` is still
   * that commit. Else it answers 409 and changes nothing.
   */
  rollback = (id: string, number: number, commit?: string) =>
    this.json<Restored>(`/sessions/${id}/rollback`, {
      method: "POST",
      body: commit === undefined ? { number } : { number, commit },
    });
  /** The state of coding mode: plan mode, the repos, and the newest plan (M7.9). */
  getCoding = (id: string) => this.json<CodingOut>(`/sessions/${id}/coding`);
  /** The plans of a session, newest first. */
  listPlans = (id: string, limit = 10) =>
    this.json<PlanOut[]>(`/sessions/${id}/plans${query({ limit })}`);
  attachRepo = (id: string, body: NewRepo) =>
    this.json<RepoOut>(`/sessions/${id}/repos`, { method: "POST", body });
  detachRepo = (id: string, repoId: string) =>
    this.json<null>(`/sessions/${id}/repos/${encodeURIComponent(repoId)}`, { method: "DELETE" });
  setPlanMode = (id: string, on: boolean) =>
    this.json<{ plan_mode: boolean }>(`/sessions/${id}/plan-mode`, { method: "PUT", body: { on } });
  /** Turns plan mode on and sends the task as a message. */
  startPlan = (id: string, task: string) =>
    this.json<PlanRun>(`/sessions/${id}/plan`, { method: "POST", body: { task } });
  /** Approves a proposed plan. With `body`, the agent gets that text in place of the plan. */
  approvePlan = (id: string, planId: string, body?: string) =>
    this.json<PlanApproved>(`/sessions/${id}/plans/${encodeURIComponent(planId)}/approve`, {
      method: "POST",
      body: body === undefined ? {} : { body },
    });
  rejectPlan = (id: string, planId: string, feedback?: string) =>
    this.json<PlanRejected>(`/sessions/${id}/plans/${encodeURIComponent(planId)}/reject`, {
      method: "POST",
      body: feedback ? { feedback } : {},
    });
  /**
   * The file changes between two checkpoints. `base`: a checkpoint number or `oldest`. `head`:
   * a checkpoint number, or `null` for the current files.
   */
  getDiff = (id: string, base: number | "oldest" = "oldest", head: number | null = null) =>
    this.json<DiffOut>(`/sessions/${id}/diff${query({ base, head })}`);
  listCommands = (id: string, tests = false, limit = 50) =>
    this.json<CommandOut[]>(`/sessions/${id}/commands${query({ tests: String(tests), limit })}`);
  listPullRequests = (id: string) => this.json<PullRequestOut[]>(`/sessions/${id}/pull-requests`);
  /** Asks the agent to open a pull request. A person approves the `pr_create` call. */
  requestPullRequest = (id: string, body: NewPullRequest) =>
    this.json<PlanRun>(`/sessions/${id}/pull-requests`, { method: "POST", body });
  compact = (id: string, focus: string) =>
    this.json<{ run_id: string }>(`/sessions/${id}/compact`, { method: "POST", body: { focus } });
  cancelRun = (runId: string) => this.json<{ status: string }>(`/runs/${runId}/cancel`, { method: "POST" });
  listApprovals = (id: string) => this.json<Approval[]>(`/sessions/${id}/approvals`);
  approve = (approvalId: string, scope: ApprovalScope = "once") =>
    this.json<ApprovalDecision>(`/approvals/${approvalId}/approve`, {
      method: "POST",
      body: { scope },
    });
  deny = (approvalId: string) =>
    this.json<ApprovalDecision>(`/approvals/${approvalId}/deny`, {
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
  listAgentProfileChanges = (orgId: string, profileId: string) =>
    this.json<ProfileChange[]>(`/orgs/${orgId}/agent-profiles/${profileId}/changes`);
  revertAgentProfileChange = (orgId: string, profileId: string, changeId: string) =>
    this.json<{ id: string; profile: AgentProfile }>(
      `/orgs/${orgId}/agent-profiles/${profileId}/changes/${changeId}/revert`,
      { method: "POST", body: {} },
    );
  listConnectors = () => this.json<Connector[]>("/connectors");
  /** Why a connector cannot connect in the org of the workspace now (M8.1): name to reason. */
  connectorAvailability = (workspaceId: string) =>
    this.json<Record<string, string>>(`/workspaces/${workspaceId}/connectors/availability`);
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
  getSkillSettings = (workspaceId: string, name: string) =>
    this.json<SkillSettings>(
      `/workspaces/${encodeURIComponent(workspaceId)}/skills/${encodeURIComponent(name)}/settings`,
    );
  /** Replaces all stored values. `null` or a missing key: use the default. */
  putSkillSettings = (workspaceId: string, name: string, values: Record<string, SkillSettingValue | null>) =>
    this.json<SkillSettings>(
      `/workspaces/${encodeURIComponent(workspaceId)}/skills/${encodeURIComponent(name)}/settings`,
      { method: "PUT", body: { values } },
    );
  listArchivedSkills = (workspaceId: string) =>
    this.json<ArchivedSkill[]>(`/workspaces/${encodeURIComponent(workspaceId)}/archived-skills`);
  /** Pins, unpins, or restores a stored skill (Section 9.7). */
  setSkillState = (workspaceId: string, name: string, scope: WritableSkillScope, action: SkillStateAction) =>
    this.json<SkillState>(
      `/workspaces/${encodeURIComponent(workspaceId)}/skills/${encodeURIComponent(name)}/${action}${query({ scope })}`,
      { method: "POST" },
    );
  listSkillVersions = (skillId: string) =>
    this.json<SkillVersion[]>(`/skills/${encodeURIComponent(skillId)}/versions`);
  getSkillVersion = (skillId: string, version: number) =>
    this.json<SkillVersionDetail>(`/skills/${encodeURIComponent(skillId)}/versions/${version}`);
  listSubagents = (orgId: string, workspaceId: string | null) =>
    this.json<SubagentCatalog>(
      `/orgs/${encodeURIComponent(orgId)}/subagents${query({ workspace_id: workspaceId })}`,
    );
  getSubagent = (orgId: string, agentId: string) =>
    this.json<LibraryAgent>(`/orgs/${encodeURIComponent(orgId)}/subagents/${encodeURIComponent(agentId)}`);
  createSubagent = (orgId: string, body: LibraryCreate) =>
    this.json<LibraryAgent>(`/orgs/${encodeURIComponent(orgId)}/subagents`, { method: "POST", body });
  updateSubagent = (orgId: string, agentId: string, patch: LibraryPatch) =>
    this.json<LibraryAgent>(`/orgs/${encodeURIComponent(orgId)}/subagents/${encodeURIComponent(agentId)}`, {
      method: "PATCH",
      body: patch,
    });
  /** The API answers 409 when an agent profile uses the agent. The error text names them. */
  deleteSubagent = (orgId: string, agentId: string) =>
    this.json<null>(`/orgs/${encodeURIComponent(orgId)}/subagents/${encodeURIComponent(agentId)}`, {
      method: "DELETE",
    });
  listSubagentVersions = (orgId: string, agentId: string) =>
    this.json<LibraryVersion[]>(
      `/orgs/${encodeURIComponent(orgId)}/subagents/${encodeURIComponent(agentId)}/versions`,
    );
  revertSubagent = (orgId: string, agentId: string, version: number) =>
    this.json<LibraryAgent>(
      `/orgs/${encodeURIComponent(orgId)}/subagents/${encodeURIComponent(agentId)}/versions/${version}/revert`,
      { method: "POST" },
    );
  /** Starts a private session that runs as the library agent. */
  testSubagent = (orgId: string, agentId: string, body: LibraryTestBody) =>
    this.json<LibraryTestResult>(
      `/orgs/${encodeURIComponent(orgId)}/subagents/${encodeURIComponent(agentId)}/test`,
      { method: "POST", body },
    );
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
  /** Sends the bot token. It goes only to this call, never to the chat. */
  connectTelegram = (orgId: string, botToken: string) =>
    this.json<IntegrationSummary>(`/orgs/${encodeURIComponent(orgId)}/integrations/telegram`, {
      method: "POST",
      body: { bot_token: botToken },
    });
  /** Sends the credentials of the provider. They go only to this call, never to the chat. */
  connectWhatsApp = (orgId: string, body: WhatsAppConnect) =>
    this.json<ChannelSetup>(`/orgs/${encodeURIComponent(orgId)}/integrations/whatsapp`, {
      method: "POST",
      body,
    });
  /** Sends the SendGrid key. It goes only to this call, never to the chat. */
  connectEmail = (orgId: string, body: EmailConnect) =>
    this.json<ChannelSetup>(`/orgs/${encodeURIComponent(orgId)}/integrations/email`, {
      method: "POST",
      body,
    });
  disconnectIntegration = (orgId: string, kind: ChatChannel) =>
    this.json<null>(`/orgs/${encodeURIComponent(orgId)}/integrations/${kind}`, { method: "DELETE" });
  modelKeys = (orgId: string) =>
    this.json<ModelKeyStatus[]>(`/orgs/${encodeURIComponent(orgId)}/model-keys`);
  setModelKey = (orgId: string, provider: ModelProvider, apiKey: string) =>
    this.json<ModelKeyStatus>(`/orgs/${encodeURIComponent(orgId)}/model-keys/${provider}`, {
      method: "PUT",
      body: { api_key: apiKey },
    });
  removeModelKey = (orgId: string, provider: ModelProvider) =>
    this.json<null>(`/orgs/${encodeURIComponent(orgId)}/model-keys/${provider}`, { method: "DELETE" });
  apiKeys = (orgId: string) => this.json<ApiKey[]>(`/orgs/${encodeURIComponent(orgId)}/api-keys`);
  /** The answer has the key. It shows one time only. */
  createApiKey = (orgId: string, body: ApiKeyCreate) =>
    this.json<ApiKeyCreated>(`/orgs/${encodeURIComponent(orgId)}/api-keys`, { method: "POST", body });
  revokeApiKey = (orgId: string, keyId: string) =>
    this.json<null>(`/orgs/${encodeURIComponent(orgId)}/api-keys/${encodeURIComponent(keyId)}`, {
      method: "DELETE",
    });
  browserLogins = (orgId: string) =>
    this.json<BrowserLogin[]>(`/orgs/${encodeURIComponent(orgId)}/browser/logins`);
  /** Sends the user name and the password. They go only to this call, never to the chat. */
  addBrowserLogin = (orgId: string, body: BrowserLoginAdd) =>
    this.json<BrowserLogin>(`/orgs/${encodeURIComponent(orgId)}/browser/logins`, {
      method: "POST",
      body,
    });
  removeBrowserLogin = (orgId: string, loginId: string) =>
    this.json<null>(
      `/orgs/${encodeURIComponent(orgId)}/browser/logins/${encodeURIComponent(loginId)}`,
      { method: "DELETE" },
    );
  customTools = (workspaceId: string) =>
    this.json<CustomTool[]>(`/workspaces/${encodeURIComponent(workspaceId)}/custom-tools`);
  /** Adds a tool. The credential goes only to this call and to the vault. */
  createCustomTool = (workspaceId: string, body: CustomToolCreate) =>
    this.json<CustomTool>(`/workspaces/${encodeURIComponent(workspaceId)}/custom-tools`, {
      method: "POST",
      body,
    });
  /** A new version. Only a person who manages the tool can write it. */
  editCustomTool = (toolId: string, body: CustomToolDefinition & { summary?: string }) =>
    this.json<CustomTool>(`/custom-tools/${encodeURIComponent(toolId)}`, { method: "PUT", body });
  /** The scopes in which the user can add a tool: `user`, and the shared scopes for an admin. */
  customToolScopes = (workspaceId: string) =>
    this.json<{ scopes: CustomToolScope[] }>(
      `/workspaces/${encodeURIComponent(workspaceId)}/custom-tools/scopes`,
    );
  customToolVersions = (toolId: string) =>
    this.json<CustomToolVersion[]>(`/custom-tools/${encodeURIComponent(toolId)}/versions`);
  revertCustomTool = (toolId: string, version: number) =>
    this.json<CustomTool>(`/custom-tools/${encodeURIComponent(toolId)}/revert`, {
      method: "POST",
      body: { version },
    });
  setCustomToolCredential = (toolId: string, credential: CustomToolCredential) =>
    this.json<CustomTool>(`/custom-tools/${encodeURIComponent(toolId)}/credential`, {
      method: "PUT",
      body: credential,
    });
  testCustomTool = (toolId: string, args: Record<string, unknown>, version?: number) =>
    this.json<CustomToolTestResult>(`/custom-tools/${encodeURIComponent(toolId)}/test`, {
      method: "POST",
      body: { arguments: args, version: version ?? null },
    });
  deleteCustomTool = (toolId: string) =>
    this.json<null>(`/custom-tools/${encodeURIComponent(toolId)}`, { method: "DELETE" });
  browserUrlPolicy = (orgId: string) =>
    this.json<BrowserUrlPolicy>(`/orgs/${encodeURIComponent(orgId)}/browser/url-policy`);
  setBrowserUrlPolicy = (orgId: string, allowHosts: string[], blockHosts: string[]) =>
    this.json<BrowserUrlPolicy>(`/orgs/${encodeURIComponent(orgId)}/browser/url-policy`, {
      method: "PUT",
      body: { allow_hosts: allowHosts, block_hosts: blockHosts },
    });
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
  /** Adds a remote MCP server by URL. Only a workspace admin can do this. */
  addMcpServer = (workspaceId: string, body: McpServerAdd) =>
    this.json<StartedConnection>(`/workspaces/${workspaceId}/connections/mcp`, {
      method: "POST",
      body,
    });
  /** Adds a stdio MCP server in a connector host. A custom command needs the org owner. */
  addStdioServer = (workspaceId: string, body: StdioServerAdd) =>
    this.json<StartedConnection>(`/workspaces/${workspaceId}/connections/stdio`, {
      method: "POST",
      body,
    });
  /** Adds an HTTP API from an OpenAPI document or a list of endpoints. */
  addApi = (workspaceId: string, body: ApiAdd) =>
    this.json<StartedConnection>(`/workspaces/${workspaceId}/connections/api`, {
      method: "POST",
      body,
    });
  /** Makes the pending Slack app of the org and gives its manifest (Section 11.6). */
  setupSlack = (orgId: string) =>
    this.json<SlackSetup>(`/orgs/${encodeURIComponent(orgId)}/integrations/slack/setup`, {
      method: "POST",
    });
  /** Sends the bot token and the signing secret. They go only to this call. */
  connectSlack = (orgId: string, botToken: string, signingSecret: string) =>
    this.json<IntegrationSummary>(`/orgs/${encodeURIComponent(orgId)}/integrations/slack`, {
      method: "POST",
      body: { bot_token: botToken, signing_secret: signingSecret },
    });
  // -- M6.9 -------------------------------------------------------------------------------------
  createAgentProfile = (orgId: string, body: NewAgentProfile) =>
    this.json<AgentProfile>(`/orgs/${encodeURIComponent(orgId)}/agent-profiles`, {
      method: "POST",
      body,
    });
  orgUsage = (orgId: string, days: number) =>
    this.json<OrgUsage>(`/orgs/${encodeURIComponent(orgId)}/usage${query({ days })}`);
  orgAudit = (
    orgId: string,
    options: { limit?: number; before?: number | null; action?: string | null } = {},
  ) =>
    this.json<AuditPage>(
      `/orgs/${encodeURIComponent(orgId)}/audit${query({
        limit: options.limit ?? 50,
        before: options.before ?? null,
        action: options.action || null,
      })}`,
    );
  curatorReports = (orgId: string, limit = 10) =>
    this.json<CuratorReports>(`/orgs/${encodeURIComponent(orgId)}/curator-reports${query({ limit })}`);
  dataSettings = (orgId: string) =>
    this.json<DataSettings>(`/orgs/${encodeURIComponent(orgId)}/data-settings`);
  setRetention = (orgId: string, retentionDays: number | null) =>
    this.json<DataSettings>(`/orgs/${encodeURIComponent(orgId)}/data-settings`, {
      method: "PUT",
      body: { retention_days: retentionDays },
    });
  /** The ZIP of all the data of the org (an org owner). */
  exportOrg = (orgId: string) =>
    this.download(`/orgs/${encodeURIComponent(orgId)}/export`, `sama-org-${orgId.slice(0, 8)}.zip`);
  /** The ZIP of the data of one user in the org (the user, or an org owner). */
  exportUser = (orgId: string, userId: string) =>
    this.download(
      `/orgs/${encodeURIComponent(orgId)}/users/${encodeURIComponent(userId)}/export`,
      `sama-user-${userId.slice(0, 8)}.zip`,
    );
  deleteUser = (orgId: string, userId: string) =>
    this.json<Removed>(`/orgs/${encodeURIComponent(orgId)}/users/${encodeURIComponent(userId)}`, {
      method: "DELETE",
    });
  /** Deletes the org. `confirm` must be the slug of the org. */
  deleteOrg = (orgId: string, confirm: string) =>
    this.json<Removed>(`/orgs/${encodeURIComponent(orgId)}${query({ confirm })}`, { method: "DELETE" });

  /** A file answer. A status that is not 2xx throws an `ApiError` with the JSON detail. */
  private download = async (path: string, fallback: string): Promise<ExportFile> => {
    const generation = this.tokens.generation;
    const response = await this.raw(path, {}, generation);
    if (!response.ok) {
      const data: unknown = await response.json().catch(() => null);
      this.assertCurrent(generation);
      throw new ApiError(response.status, errorMessage(response.status, data));
    }
    const blob = await response.blob();
    this.assertCurrent(generation);
    return { name: attachmentName(response.headers.get("Content-Disposition"), fallback), blob };
  };
  /** Ends an OAuth sign-in: the server sent the user back with `state` and `code` (or `error`). */
  finishOAuth = (state: string, code: string | null, error: string | null) =>
    this.json<Connection>("/connections/oauth/callback", {
      method: "POST",
      body: { state, code, error },
    });
  /** The include and exclude filters of the tools of a remote MCP server. */
  setConnectionTools = (id: string, include: string[], exclude: string[]) =>
    this.json<Connection>(`/connections/${id}/tools`, { method: "PUT", body: { include, exclude } });
  // -- M7.10 settings console -------------------------------------------------------------------
  rolePermissions = (orgId: string) =>
    this.json<RolePermissions>(`/orgs/${encodeURIComponent(orgId)}/role-permissions`);
  setRolePermissions = (orgId: string, role: EditableRole, permissions: Partial<Record<PermissionKey, boolean>>) =>
    this.json<RolePermissions>(`/orgs/${encodeURIComponent(orgId)}/role-permissions/${role}`, {
      method: "PUT",
      body: { permissions },
    });
  toolPolicy = (orgId: string, profileId: string) =>
    this.json<ToolPolicy>(
      `/orgs/${encodeURIComponent(orgId)}/agent-profiles/${encodeURIComponent(profileId)}/tool-policy`,
    );
  setToolPolicy = (orgId: string, profileId: string, rules: ToolPolicyChange[]) =>
    this.json<ToolPolicy>(
      `/orgs/${encodeURIComponent(orgId)}/agent-profiles/${encodeURIComponent(profileId)}/tool-policy`,
      { method: "PUT", body: { rules } },
    );
  orgToolPolicy = (orgId: string) =>
    this.json<OrgToolPolicy>(`/orgs/${encodeURIComponent(orgId)}/tool-policy`);
  setOrgToolPolicy = (orgId: string, rules: ToolPolicyChange[]) =>
    this.json<OrgToolPolicy>(`/orgs/${encodeURIComponent(orgId)}/tool-policy`, {
      method: "PUT",
      body: { rules },
    });
  listTriggers = (workspaceId: string) =>
    this.json<Trigger[]>(`/workspaces/${encodeURIComponent(workspaceId)}/triggers`);
  getTrigger = (workspaceId: string, triggerId: string) =>
    this.json<Trigger>(
      `/workspaces/${encodeURIComponent(workspaceId)}/triggers/${encodeURIComponent(triggerId)}`,
    );
  createTrigger = (workspaceId: string, body: NewTrigger) =>
    this.json<TriggerWithSecret>(`/workspaces/${encodeURIComponent(workspaceId)}/triggers`, {
      method: "POST",
      body,
    });
  updateTrigger = (workspaceId: string, triggerId: string, body: TriggerEdit) =>
    this.json<Trigger>(
      `/workspaces/${encodeURIComponent(workspaceId)}/triggers/${encodeURIComponent(triggerId)}`,
      { method: "PATCH", body },
    );
  rotateTriggerSecret = (workspaceId: string, triggerId: string) =>
    this.json<TriggerWithSecret>(
      `/workspaces/${encodeURIComponent(workspaceId)}/triggers/${encodeURIComponent(triggerId)}/secret`,
      { method: "POST" },
    );
  deleteTrigger = (workspaceId: string, triggerId: string) =>
    this.json<null>(
      `/workspaces/${encodeURIComponent(workspaceId)}/triggers/${encodeURIComponent(triggerId)}`,
      { method: "DELETE" },
    );
  triggerDeliveries = (workspaceId: string, triggerId: string, limit = 50) =>
    this.json<TriggerDelivery[]>(
      `/workspaces/${encodeURIComponent(workspaceId)}/triggers/${encodeURIComponent(triggerId)}/deliveries${query({ limit })}`,
    );
  /** Sends a signed test delivery. `start: false` only checks it; `true` starts the run. */
  testTrigger = (
    workspaceId: string,
    triggerId: string,
    body: { payload?: Record<string, unknown> | null; start?: boolean } = {},
  ) =>
    this.json<{ delivery: TriggerDelivery }>(
      `/workspaces/${encodeURIComponent(workspaceId)}/triggers/${encodeURIComponent(triggerId)}/test`,
      { method: "POST", body },
    );
  validateWorkflow = (yaml: string) =>
    this.json<WorkflowValidation>("/workflows/validate", { method: "POST", body: { yaml } });
  listWorkflows = (orgId: string) =>
    this.json<Workflow[]>(`/orgs/${encodeURIComponent(orgId)}/workflows`);
  getWorkflow = (orgId: string, workflowId: string) =>
    this.json<WorkflowDetail>(
      `/orgs/${encodeURIComponent(orgId)}/workflows/${encodeURIComponent(workflowId)}`,
    );
  createWorkflow = (
    orgId: string,
    body: { yaml: string; workspace_id?: string; agent_profile_id?: string },
  ) =>
    this.json<WorkflowDetail>(`/orgs/${encodeURIComponent(orgId)}/workflows`, {
      method: "POST",
      body,
    });
  updateWorkflow = (
    orgId: string,
    workflowId: string,
    body: { yaml: string; workspace_id?: string; agent_profile_id?: string },
  ) =>
    this.json<WorkflowDetail>(
      `/orgs/${encodeURIComponent(orgId)}/workflows/${encodeURIComponent(workflowId)}`,
      { method: "PUT", body },
    );
  approveWorkflow = (orgId: string, workflowId: string, version: number) =>
    this.json<WorkflowDetail>(
      `/orgs/${encodeURIComponent(orgId)}/workflows/${encodeURIComponent(workflowId)}/approve`,
      { method: "POST", body: { version } },
    );
  startWorkflow = (orgId: string, workflowId: string, inputs: Record<string, unknown> = {}) =>
    this.json<WorkflowInstanceDetail>(
      `/orgs/${encodeURIComponent(orgId)}/workflows/${encodeURIComponent(workflowId)}/start`,
      { method: "POST", body: { inputs } },
    );
  pauseWorkflow = (orgId: string, workflowId: string) =>
    this.json<WorkflowDetail>(
      `/orgs/${encodeURIComponent(orgId)}/workflows/${encodeURIComponent(workflowId)}/pause`,
      { method: "POST" },
    );
  resumeWorkflow = (orgId: string, workflowId: string) =>
    this.json<WorkflowDetail>(
      `/orgs/${encodeURIComponent(orgId)}/workflows/${encodeURIComponent(workflowId)}/resume`,
      { method: "POST" },
    );
  deleteWorkflow = (orgId: string, workflowId: string) =>
    this.json<null>(`/orgs/${encodeURIComponent(orgId)}/workflows/${encodeURIComponent(workflowId)}`, {
      method: "DELETE",
    });
  listWorkflowInstances = (orgId: string, workflowId: string | null = null, limit = 20) =>
    this.json<WorkflowInstance[]>(
      `/orgs/${encodeURIComponent(orgId)}/workflow-instances${query({ workflow_id: workflowId, limit })}`,
    );
  getWorkflowInstance = (orgId: string, instanceId: string) =>
    this.json<WorkflowInstanceDetail>(
      `/orgs/${encodeURIComponent(orgId)}/workflow-instances/${encodeURIComponent(instanceId)}`,
    );
  cancelWorkflowInstance = (orgId: string, instanceId: string) =>
    this.json<WorkflowInstanceDetail>(
      `/orgs/${encodeURIComponent(orgId)}/workflow-instances/${encodeURIComponent(instanceId)}/cancel`,
      { method: "POST" },
    );
  updateSession = (sessionId: string, body: SessionPatch) =>
    this.json<SessionOut>(`/sessions/${encodeURIComponent(sessionId)}`, { method: "PATCH", body });
  /** The model ids that the session can use, and the model that it uses now (M8.2). */
  /** Starts the sandbox of the session while the person types (M8.2). It makes no run. */
  warmSession = (sessionId: string) =>
    this.json<{ status: string }>(`/sessions/${encodeURIComponent(sessionId)}/warm`, { method: "POST" });
  getSessionModels = (sessionId: string) =>
    this.json<SessionModels>(`/sessions/${encodeURIComponent(sessionId)}/models`);
  /**
   * Chooses the model of a session, like `/model` in Claude Code (M8.2). It applies from the
   * next run. `null` goes back to the model of the profile. The answer is the session.
   */
  setSessionModel = (sessionId: string, model: string | null) =>
    this.updateSession(sessionId, { model });
  checkIntegration = (orgId: string, kind: ChatChannel) =>
    this.json<IntegrationCheck>(
      `/orgs/${encodeURIComponent(orgId)}/integrations/${encodeURIComponent(kind)}/check`,
      { method: "POST" },
    );
  enableConnection = (id: string) =>
    this.json<Connection>(`/connections/${encodeURIComponent(id)}/enable`, { method: "POST" });
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
