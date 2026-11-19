# Sama — Roadmap and Backlog

| | |
|---|---|
| Status | Draft v0.3, 2026-11-18 |
| Design | [`architecture.md`](architecture.md) |
| Feature map | [`feature-map.md`](feature-map.md) |

Each milestone ends with a usable assistant. Each backlog item is about one pull request.
An item is done when its acceptance criteria pass in CI or in a recorded demo.

## Overview

| Milestone | Goal | Demo at the end |
|---|---|---|
| M0 | Design | This pull request: architecture, feature map, roadmap. |
| M1 | Core engine | Chat from Telegram and a web page. The agent runs code in an E2B sandbox on the Claude Agent SDK, asks for approval, and keeps the session. |
| M2 | Learning loop | The agent remembers facts, writes and improves skills, and finds old sessions. |
| M3 | Reach | Slack, connections (Composio, remote MCP, PostgreSQL), the OpenAI Agents SDK runtime, GitHub pull requests, web app v1. |
| M4 | Automation | Cron jobs, triggers, workflows, `delegate_task`, task board. |
| M5 | Browser | Cloud browser tools and the Chrome extension with click, type, and cursor. |
| M6 | Self-change and customers | `change_propose`, `sama_pr`, curator, checkpoints, customer sign-up and billing. |

```mermaid
flowchart LR
  M0["M0 Design"] --> M1["M1 Core engine"]
  M1 --> M2["M2 Learning loop"]
  M1 --> M3["M3 Reach"]
  M2 --> M4["M4 Automation"]
  M3 --> M4
  M3 --> M5["M5 Browser"]
  M4 --> M6["M6 Self-change and customers"]
  M5 --> M6
```

M2 and M3 can run in parallel after M1.

---

## M1 — Core engine

Done: M1.1 to M1.18.

- M1.2: migrations in `migrations/`, models in `src/sama/db/`, RLS tests in `tests/db/`. The
  Supabase project `sama` (`ap-south-1`) has the migrations. The tenancy check through the
  Supabase Data API is in `tests/db/test_supabase_live.py`. The sama HTTP API repeats it
  (M1.12, `tests/api/`).
- M1.3: contract in `src/sama/runtime/contract.py`, fake runtime in `src/sama/runtime/fake.py`.
- M1.4: Claude adapter in `src/sama/runtime/claude.py` (architecture Section 5.3). Live tests
  of the adapter run with `SAMA_LIVE_TESTS=1 uv run --all-groups pytest -m live`.
- M1.5: conformance suite in `tests/conformance/` (architecture Section 5.5). CI runs the
  `fake` and `claude-scripted` targets. `.github/workflows/nightly-live.yml` runs the
  `claude-live` target every night. It needs the repository secret `ANTHROPIC_API_KEY`.
- M1.6: providers in `src/sama/sandbox/`, run tokens in `src/sama/tenancy/run_token.py`, the
  runner in `src/sama/runtime/runner.py`, and `SandboxedRuntime` in `src/sama/orchestrator/`
  (architecture Section 16.2). The image is `sandbox/Dockerfile`. CI builds it and runs the
  Docker provider tests and the `claude-docker` conformance target. The nightly job builds the
  E2B template `sama-runner` and runs the E2B tests. It needs the repository secret
  `E2B_API_KEY`.
- M1.7: the model proxy in `src/sama/modelproxy/` (architecture Section 16.6), with the price
  table `prices.toml` and the `model_calls` table (migration `0002`). The `claude-runner` and
  `claude-docker` conformance targets send their model calls through the proxy. A live Claude
  run in E2B needs a proxy URL that the sandbox can reach, so it waits for the deploy (M1.15).
- M1.8: the sama MCP tool server in `src/sama/tools/` (architecture Section 16.7), with the
  outbound URL policy (Section 16.4) and the `session_todos` table (migration `0003`).
  `web_search` uses the Brave Search API. The clarify broker is in memory (see Later). The
  Claude `TodoWrite` mapping to `todo` comes later.
- M1.9: the tool gate in `src/sama/gate/` (architecture Section 16.1), with the `approvals`
  and `tool_policies` tables and `runs.unattended` (migration `0004`). The runner asks the
  control plane for each call (`POST /gate`). The API and the channels call
  `GateService.decide_approval` (M1.12, M1.13). The worker calls `GateService.expire()`
  (M1.10). Job pre-approvals come with jobs (M3).
- M1.10: the run queue and the worker in `src/sama/orchestrator/queue.py` and
  `src/sama/orchestrator/worker.py` (architecture Section 4.3), with the queue columns of
  `runs` and `sessions.sandbox_id` (migration `0005`). `PostgresSandboxDirectory` stores the
  sandbox of each session. A crashed run starts again only if the worker did not start the
  runner (`runs.dispatched_at`). The deploy (M1.15) applies migrations `0004` to `0009` to
  Supabase.
- M1.11: prompt assembly in `src/sama/prompt/` (architecture Section 6.1). The first run of a
  session builds the `RunConfig` and stores it in `sessions.run_config` (migration `0006`).
  Later runs reuse it. Context files come from the table `context_files`, with the injection
  scan and the 20,000-character cap. Channel hints come with M1.13, repo `AGENTS.md` files
  with M1.16 (in the `repo_clone` result, not in the prefix), and memory and skills later.
- M1.12: the HTTP API and the minimal web page in `src/sama/api/` (architecture Section 11.4).
  It checks Supabase Auth tokens (legacy secret or JWKS), reads with RLS and checks access in
  code (`src/sama/tenancy/access.py`), streams run events as server-sent events, and decides
  approvals. The per-org running and message limits are in `orgs.limits` (Section 4.3,
  migration `0007`). The page at `/` has no build step. The Next.js app replaces it in M3.
  The deploy (M1.15) runs the API, the worker, and the tool server in one process, so that the
  API answers `clarify` questions.
- M1.13: the Telegram gateway in `src/sama/gateway/` (architecture Section 11.5), with the
  tables `channel_identities`, `channel_groups`, and `pairing_codes` (migration `0008`).
  `python -m sama.gateway` runs it. The web page makes pairing codes. Not in M1.13: the group
  toolset (Section 11.3), `clarify` buttons in Telegram, and followers that survive a restart.
  The deploy (M1.15) runs the gateway and sets the webhook.
- M1.14: observability in `src/sama/observability/` (architecture Section 18.1 to 18.3).
  Each service writes JSON logs with the run ids. `python -m sama.observability` (the
  monitor) sends one Langfuse trace per ended run over OTLP and posts error rate alerts to
  Slack. Migration `0009` adds `runs.trace_exported_at`. The traces come from the stored
  rows, so they work for both runtimes. Not in M1.14: tool arguments and results in traces
  (M1.17 adds them, redacted), metrics such as queue depth, and a dashboard. The deploy
  (M1.15) runs the monitor next to the worker and sets the Langfuse and Slack secrets.
- M1.15: the deploy (architecture Section 19.1 to 19.4, decision D18). `python -m
  sama.controlplane` runs the API, the tool server, the model proxy, the worker, the monitor,
  and the Telegram gateway in one process, so the Fly.io apps are `sama-staging` and
  `sama-production` with one machine each, not separate `api`, `gateway`, and `worker` apps.
  CI deploys `main` to staging. The `Deploy` workflow promotes a staging image to production by
  hand. The release command applies the migrations. `python -m sama.admin bootstrap` creates
  the first org. Not in M1.15: the Telegram bot and the Slack alerts in the deployed apps (no
  valid bot token or Slack webhook exists yet; the settings turn them on when they are set), a
  custom domain, and more than one machine.
- M1.16: code editing in the sandbox (architecture Section 15.1). The `github` toolset has
  `repo_clone`, which clones a public GitHub repo into the session sandbox and returns the
  repo `AGENTS.md` and `CLAUDE.md` as untrusted data. The agent edits and runs tests with
  native tools. The sandbox can reach GitHub, PyPI, and npm by default. Not in M1.16: private
  repos, push, `pr_create`, and the git proxy (M2). See the exfiltration risk in architecture
  Section 16.2.
- M1.17: vault and redaction in `src/sama/vault/` (architecture Section 16.3). Migration
  `0010` adds `vault_secrets` (envelope encryption with `SAMA_VAULT_KEY`, RLS for org
  admins). The model proxy and `web_search` use the org key first and the environment key
  second. `python -m sama.admin secret` sets, lists, and deletes entries. One redactor removes
  known secrets and secret patterns from logs, stored transcripts, run events, tool arguments,
  approval reasons, tool server output, and traces. Traces now have the tool arguments and
  output. A test plants a secret and checks every stored table, the trace, and the logs. Not in
  M1.17: an HTTP API for secrets (M3), key rotation, org keys for the shared Telegram bot and
  for voice notes (M6), and redaction of native tool output in the sandbox.
- M1.18: live authority in `src/sama/tenancy/authority.py` (architecture Sections 5.1 and
  16.1). The tool gate and the model proxy read the run status, the access of the run user,
  and the run budget again at each call. The live budget is the lower of the run budget and
  the profile budget. A deny rule wins over an approval. Before a resume, the worker checks
  the approved call again (`GateService.reconfirm`), and a deny turns the resume into a
  reject. The allowed call becomes a ticket, and the tool server runs an `ask` sama tool only
  with an unused ticket for the same tool and arguments. No migration. Not in M1.18: live
  checks of connection access (connections come in M3.4) and a live check of the worker
  limits `max_turns` and `max_wall_time_s`, which stay frozen for the run.

| # | Item | Acceptance criteria |
|---|---|---|
| M1.1 | Repo foundations | `pyproject.toml` (Python 3.12, uv), Ruff, mypy strict, pytest, pre-commit. GitHub Actions runs lint, types, and tests on every pull request. Dependency groups `runtime-claude` and `runtime-openai`. |
| M1.2 | Database and tenancy | Supabase project in `ap-south-1`. Alembic migrations for `orgs`, `workspaces`, `users`, `memberships`, `agent_profiles`, `sessions`, `messages`, `tool_calls`, `runs`, `run_events`, `session_bindings`, `audit_log`. RLS policies. A test shows that a user in org A cannot read rows of org B, through the API and through RLS. |
| M1.3 | Runtime contract and fake runtime | `src/sama/runtime/contract.py` as in §5.1. A `fake` runtime that replays scripted events. `RunConfig.fingerprint()` is stable for the same input. |
| M1.4 | Claude Agent SDK adapter | Maps `RunRequest` to `ClaudeAgentOptions` and SDK messages to `RunEvent`s. Streams text. Cancel ends the run within 10 seconds. Session resume works. |
| M1.5 | Conformance suite | Shared tests for every runtime: streaming, tool call through MCP, approval pause and resume, cancel, budget stop, compaction event. The fake and Claude adapters pass. A nightly job runs it against the live Claude API. |
| M1.6 | Sandbox provider and runner | `SandboxProvider` with E2B and Docker. A sama E2B template. One sandbox per session with pause and resume. The runner starts inside the sandbox and talks to the control plane only with a run token. Start time is measured and logged. |
| M1.7 | Model proxy | Anthropic and OpenAI pass-through with run tokens. Records tokens and cost per call. Rejects calls over budget. No provider key exists in the sandbox (a test checks the sandbox environment). |
| M1.8 | Sama MCP tool server | Streamable HTTP MCP server per run, scoped by the run token. Tools: `web_search`, `web_extract`, `clarify`, `todo`. Output cap of 50 KB with spill. `web_extract` uses the outbound URL policy (architecture §16.4): a test shows that private, metadata, and redirect-to-private URLs are blocked. |
| M1.9 | Tool gate and approvals | Order of §16.1: toolset, hardline, recorded decision, policy, context. `ask` pauses the run and creates an approval. Unattended runs turn `ask` into `deny`. Every decision is in the audit log. Tests cover each step. |
| M1.10 | Run orchestrator and queue | Postgres queue with `FOR UPDATE SKIP LOCKED` and leases. A crashed worker's run is marked failed or resumed. One active run per session; new messages queue. |
| M1.11 | Prompt assembly | SOUL per profile, context files (20,000-char cap, injection scan), frozen prefix per session. Prompt changes apply from the next session. |
| M1.12 | HTTP API and minimal web chat | `POST /sessions/{id}/messages`, server-sent events for run events, approve and deny endpoints. Supabase Auth login. A minimal web page for chat and approvals. |
| M1.13 | Telegram channel | One shared sama bot for the team. Webhook adapter, pairing codes from the web page, streaming edits, inline-keyboard approvals, voice notes transcribed, `/new`, `/stop`, `/model` (starts a child session). DMs are private sessions. Groups are shared sessions without `user` memory or personal connections (architecture §11.3). |
| M1.14 | Observability | Langfuse traces per run for the Claude adapter. Structured logs. Alerts to a Slack channel on error rate. |
| M1.15 | Deploy | Fly.io apps for `api`, `gateway`, `worker`. CI deploys `main` to staging. A manual promote step deploys to production. |
| M1.16 | Code editing in the sandbox | The agent clones a public repo, edits files, and runs tests in the sandbox with native tools. No push yet. |
| M1.17 | Vault and redaction | Encrypted secrets per org for provider, search, and bot keys. Redaction of known secrets in tool output, logs, and traces. A test checks that a planted secret does not appear in a transcript or trace. |
| M1.18 | Live authority checks | The control plane checks membership, policy, budgets, and revocations at every tool call, model call, and approval resume. A test revokes a tool during a session and the next call gets `deny`. |

## M2 — Learning loop

| # | Item | Acceptance criteria |
|---|---|---|
| M2.1 | Memory | `memory` tool with targets `agent`, `user`, `workspace` and actions add, patch, remove. Char caps. Over-limit writes fail and return current entries. `memory_revisions` and revert. Snapshot in the frozen prefix. |
| M2.2 | Skills storage and tools | Skills as versioned rows plus files. Scopes builtin, org, workspace, user. `skills_list`, `skill_view`, `skill_manage`. Skill index (60-char descriptions) in the prefix. Skills sync into the sandbox. Lint and injection scan on every write. |
| M2.3 | Builtin skills | At least 10 builtin skills of our own (for example research, writing, code review, spreadsheet, PDF, meeting notes). `/<skill-name>` works in Telegram and web. |
| M2.4 | Background review | When a run ends and 10 user turns or 10 tool iterations passed since the last review, sama queues one review and resets the counters. At most one review runs and one waits per session. The review run with a cheap model may write memory and skills. Recursion is off. User-scope writes apply. Workspace and org writes create an approval. |
| M2.5 | Session search | Full-text plus pgvector index on messages. `session_search` and `session_read`. No LLM call in the tool. |
| M2.6 | Context management | Compaction events from the Claude SDK, transcript archive, session lineage. `tool_search`, `tool_describe`, `tool_call` for deferred tools. Context pins. `/compact`. |
| M2.7 | Native sub-agents | Claude `agents` option configured from the agent profile. Sub-agent events appear in traces. |
| M2.8 | Media tools | `vision_analyze` for images from channels. |

## M3 — Reach

| # | Item | Acceptance criteria |
|---|---|---|
| M3.1 | Slack channel | Slack app with OAuth install, DMs and mentions, thread = session, streaming edits, Block Kit approvals, slash commands. |
| M3.2 | Approvals over chat | Approval cards in Slack (Telegram cards come in M1.13). Approver rules per workspace role: workspace `admin` or org `owner` approve workspace memory, skills, and SOUL; org scope needs the org `owner`. Timeout counts as `deny`. "Always allow for me" writes a policy rule. |
| M3.3 | Shared sessions in Slack | Slack channels and threads are shared sessions (architecture §11.3). `session_search` and delivery follow the audience rule. A test shows that one user's `user` memory does not reach a shared thread. |
| M3.4 | Connection framework and Composio | `connections` table with RLS, states, and the vault reference (architecture §10.3). Catalog of YAML manifests in `connectors/catalog/`. Connections page in the web app and `/connect <name>` in channels; secrets go only into web forms, never into chat. `connections_list`, `connection_request`, `connection_test`. Only the backend can set `connected`. Composio entity per user and per workspace. Connector tools behind `tool_search`. Read tools `allow`, write tools `ask`. A test shows that a `personal` connection is not usable in a shared session. |
| M3.5 | Remote MCP connections | Add a remote MCP server from the catalog or by URL, with OAuth 2.1 (dynamic client registration and PKCE), a bearer token, or a header. Tools proxied by the sama MCP server as `mcp_<connection>_<tool>`. Include and exclude filters, a tool schema cache, and a health check. New tools are usable without a new session. All calls use the outbound URL policy (architecture §16.4). |
| M3.6 | OpenAI Agents SDK adapter | Maps the contract to `SandboxAgent` with the local sandbox client inside the E2B sandbox (no E2B key in the runner), a sama `Session` over the control-plane API (no database credentials in the runner), `needs_approval`, `RunState` approve and reject, sessions, and compaction. Passes the conformance suite. Langfuse traces. A profile can switch runtime. |
| M3.7 | GitHub App, git proxy, and pull requests | Sama GitHub App. Sama git proxy holds the tokens; the sandbox has none. `repo_clone`, `pr_create`, `pr_comment`. Allowlist of repos per org. The proxy rejects pushes to non-`sama/*` refs, force-pushes, and ref deletes, and needs an approval for protected paths. Setup docs require a default-branch ruleset with no bypass for the App. Tests cover each rejected push. |
| M3.8 | Web app v1 | Next.js app: chat, approvals, memory editor, skill editor, connections, sessions with search and export (Markdown and JSON), run traces, settings. |
| M3.9 | Busy controls and handoff | Queue, `/stop`, `/steer` while a run is busy. `/handoff` between channels. |
| M3.10 | Worker bots | An agent profile bound to a Slack channel with a daily budget. |
| M3.11 | PostgreSQL connections | Database gateway with PostgreSQL (also Supabase and Neon) (architecture §10.4). Tools `db_<conn>_schema`, `sample`, `query`, `export`, and `execute`. Tests show that: `query` runs in a read-only transaction; a write or a second statement in `query` is rejected; DDL is denied by default; a query stops after 30 seconds; the model gets at most 1,000 rows and 50 KB, and the rest spills to a file; hidden columns do not appear in any result; `execute` needs an approval that shows the SQL. Static egress IP addresses are documented for allowlists. |

## M4 — Automation

| # | Item | Acceptance criteria |
|---|---|---|
| M4.1 | Scheduler | `scheduler` service with a 60-second tick and leader lease. A fire runs once, also when two schedulers run. |
| M4.2 | Cron jobs | `jobs` table, `cronjob_manage`, `/cron`, and a web page. Natural language to cron with validation and the next 3 fire times. Model pin, `mode: script`, `preapproved_tools`, `attach_to_session`. A job run cannot create jobs. Adding or widening a pre-approval needs a human approval bound to the job version, connection, and argument limits. A test shows that a job created by the agent cannot run a write tool without that approval. |
| M4.3 | Delivery | Targets `origin`, Slack, Telegram, email, `none`, plus `failure_deliver`. Receipts. An unknown result is not retried. Repeated failures pause the job and alert once. |
| M4.4 | Event triggers | Signed webhooks and Composio triggers start runs or workflows with the `safe` toolset by default. |
| M4.5 | Workflows | YAML definition, versioned. Steps `tool`, `agent`, `approval`, `branch`, `parallel`, `wait`, `workflow`. Durable steps on the queue with resume after a crash. Stable operation ids, idempotency keys where the provider supports them, and receipts. A test crashes a worker after a non-idempotent write, and the step waits for reconcile or a human instead of a second write. `workflow_manage`, `workflow_run`. "Turn this session into a workflow" skill. New workflows that write externally need approval before the first run. |
| M4.6 | `delegate_task` | Child session and run in its own sandbox. Single and batch. Depth 1, 10 children, blocked tools, cost budget per child. `delegate_status`, cancel, steer. |
| M4.7 | Task board | `tasks` table, dispatcher, `task_*` tools, web board view. |
| M4.8 | Code execution | `execute_code` with a stub that calls sama tools over MCP. Only stdout returns to the model. |
| M4.9 | More media and references | `image_generate`, `text_to_speech` (Telegram voice note), `@file`, `@url`, `@session`. |
| M4.10 | Skills import and `/learn` | Import a skill from a Git URL with scan and approval. `/learn` creates a draft skill from sources. |
| M4.11 | Hosted stdio MCP connections | Connector host per connection in its own E2B sandbox (architecture §10.5). Pinned package from the manifest, egress allowlist, start on first call, pause when idle, restart on memory limit. The agent cannot run commands in the host. A custom command needs the org `owner`. A test shows that the agent sandbox cannot read the host's secrets. |
| M4.12 | HTTP API connections | Import an OpenAPI document or a list of endpoints. One tool per operation as `api_<connection>_<tool>`. Auth by API key, bearer, basic, or OAuth 2 client credentials. `GET` tools `allow`, other methods `ask`. |
| M4.13 | More databases and SSH tunnels | MySQL and MariaDB, Microsoft SQL Server, BigQuery (with maximum bytes billed), Snowflake, ClickHouse, and MongoDB (read with `find` and `aggregate`) in the database gateway, with the M3.11 tests for each engine. SSH tunnel route per connection (architecture §10.6). A test shows that a tunnel route cannot be used by another connection or by `web_extract`. |

## M5 — Browser

| # | Item | Acceptance criteria |
|---|---|---|
| M5.1 | Cloud browser | Browserbase sessions with a persistent context per user. `browser_open`, `browser_snapshot` (refs), `browser_act`, `browser_screenshot`, `browser_extract`, `browser_tabs`. Works from both runtimes. |
| M5.2 | Vision mode | Screenshot and coordinate actions for pages without a usable accessibility tree. A canvas-app test passes. |
| M5.3 | Browser vault and URL policy | Saved logins fill without entering the transcript. URL policy per org blocks private IPs and listed hosts. |
| M5.4 | Chrome extension core | MV3 extension in `extension/`. Sign-in, WebSocket to the gateway, side panel chat bound to the tab, "Ask about this page". |
| M5.5 | Chrome extension actions | `my_chrome` target: `chrome.debugger` clicks and typing, visible cursor overlay, per-site consent, user click for sensitive actions, stop button, pause on user takeover, blocked site list. |
| M5.6 | Browser sub-agent (optional) | `browser_task(goal)` on the cloud target with the `browser-use` library. |

## M6 — Self-change and customers

| # | Item | Acceptance criteria |
|---|---|---|
| M6.1 | `change_propose` | The agent proposes changes to SOUL, profile, toolsets, and policy. An approval card shows the diff. Approved changes apply from the next session. History and revert in the web app. |
| M6.2 | `sama_pr` | The agent clones the sama repo, changes code, runs tests, and opens a pull request with a summary. Changes to protected paths request the owner's review (CODEOWNERS). The agent cannot merge. |
| M6.3 | Curator | Weekly job per workspace. Usage counts, active to stale to archived, pins, report to the owner. Never deletes. |
| M6.4 | Checkpoints | Shadow git snapshots before file changes in the sandbox. `/undo` and `/rollback` in channels. |
| M6.5 | Customer sign-up | Self-serve sign-up, org creation, invites, roles. |
| M6.6 | Billing and quotas | Stripe plans and usage billing from proxy cost. Quotas per plan. |
| M6.7 | Org keys, bots, and regions | An org can bring its own Anthropic and OpenAI keys and its own Telegram bot. Region per org. |
| M6.8 | Data controls | Export and delete per user and per org. Retention per plan. |
| M6.9 | Customer-ready web app | Onboarding, billing pages, admin views, and docs. |

---

## Later (not planned)

- More channels: email, WhatsApp, Discord, Microsoft Teams.
- Mem0 as a memory provider.
- Fly Machines as a second sandbox provider, only if a product needs it (architecture Q1).
- Desktop control beyond the browser.
- ACP adapter for editors.
- A private network relay that customers run inside their network (architecture §10.6).
- Sama as an MCP server for other agents (architecture §10.7).
- An OpenAI-compatible chat endpoint.
- Clarify questions in Postgres, so the API, the tool server, and the workers can run in
  separate processes (architecture §16.7).
- A durable queue for model proxy records that wait for a write (architecture §16.6).
