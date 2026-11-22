# Sama — Architecture and Design

| | |
|---|---|
| Status | Draft v0.3. The owner answered all open questions (Section 22). Connectors and databases added (Sections 10.3 to 10.7). No code yet. |
| Date | 2026-11-18 |
| Owner | Nikhil Mandhani |
| Companion docs | [`feature-map.md`](feature-map.md) (every Hermes feature and our plan for it), [`roadmap.md`](roadmap.md) (milestones and backlog) |

## Contents

1. [Summary](#1-summary)
2. [Goals, non-goals, and terms](#2-goals-non-goals-and-terms)
3. [Decisions](#3-decisions)
4. [System overview](#4-system-overview)
5. [Runtime adapter layer](#5-runtime-adapter-layer)
6. [Prompt assembly and context management](#6-prompt-assembly-and-context-management)
7. [Sessions](#7-sessions)
8. [Memory](#8-memory)
9. [Skills and the self-improvement loop](#9-skills-and-the-self-improvement-loop)
10. [Tools, toolsets, and integrations](#10-tools-toolsets-and-integrations)
11. [Channels: web, Slack, Telegram, WhatsApp, email, Chrome](#11-channels-web-slack-telegram-whatsapp-email-chrome)
12. [Scheduler, cron jobs, and workflows](#12-scheduler-cron-jobs-and-workflows)
13. [Sub-agents and multi-agent work](#13-sub-agents-and-multi-agent-work)
14. [Browser use and the Chrome extension](#14-browser-use-and-the-chrome-extension)
15. [Code editing and self-modification](#15-code-editing-and-self-modification)
16. [Safety: approvals, sandbox, secrets](#16-safety-approvals-sandbox-secrets)
17. [Users, teams, and tenants](#17-users-teams-and-tenants)
18. [Observability and cost](#18-observability-and-cost)
19. [Technology stack and repository layout](#19-technology-stack-and-repository-layout)
20. [Milestones](#20-milestones)
21. [Risks](#21-risks)
22. [Resolved questions](#22-resolved-questions)
23. [References](#23-references)

---

## 1. Summary

Sama is a **cloud agent engine**. It gives an assistant the features of
[Hermes Agent](https://github.com/NousResearch/hermes-agent): skills, tools, memory, sessions,
context management, cron jobs, Slack and Telegram, sub-agents, code editing, and a
self-improvement loop. It adds browser control through a cloud browser and a Chrome extension.

Sama does not have its own agent loop. It runs two agent SDKs behind one contract:

- **Claude Agent SDK** (default). It is the Claude Code loop as a Python library: file edits,
  shell, search, sub-agents, compaction, session resume, hooks, and a permission callback.
- **OpenAI Agents SDK** (second). It gives handoffs, guardrails, `needs_approval` tools, a
  `SandboxAgent` with shell, file, skills, and compaction capabilities, and tracing.

Everything that makes the assistant "Hermes-like" lives **above** that contract, in sama:
memory, skills, session search, scheduling, channels, approvals, budgets, and audit. So each
feature works the same on both SDKs, and a third SDK needs only one more adapter.

Sama is the **core engine**. Our team uses it first. Customers use it later. Other products
build on top of it. The first planned product is
[vorflux](https://github.com/nikhilllmno/vorflux), the hotel agent harness. vorflux adds
hotel-specific parts (hotel repos, eval gate, hotel operations) and uses sama for the rest.

Five rules shape the design:

1. **Cloud only.** The API, channels, scheduler, and memory run 24/7 on Fly.io. Every session
   runs in its own **E2B** cloud sandbox. Browsers run in **Browserbase**. Nothing needs a
   local install. The Chrome extension is optional and connects out to the cloud.
2. **The sandbox is the security boundary.** The SDK loop, shell, files, and code run inside
   the sandbox. The sandbox holds no long-lived secrets. Model calls go through the sama
   **model proxy**, and sama tools go through the sama **MCP tool server**. Both accept only a
   short-lived run token.
3. **State is in Postgres** (Supabase), not in files. Every row has an `org_id`. Row-level
   security is on from the first table, so customers later need no data migration.
4. **The prompt prefix is frozen per session** (a Hermes rule). The system prompt, tool list,
   memory snapshot, and skill index do not change during a session. This keeps prompt caching
   cheap. Prompt changes apply from the next session. Compaction is the only exception.
   Authority is not frozen: a revoked permission or a lower budget applies at once.
5. **Self-improvement is bounded and reversible.** The agent can write memory, skills, cron
   jobs, and workflows. Each write is versioned. Changes to sama's own code go through a pull
   request that a human merges. The agent never changes the running service directly.

---

## 2. Goals, non-goals, and terms

### 2.1 Goals

- Give one assistant all the Hermes features listed in [`feature-map.md`](feature-map.md).
- Run on the Claude Agent SDK by default and on the OpenAI Agents SDK as a second runtime,
  with the same features on both.
- Talk to users in a web app, Slack, Telegram, and a Chrome side panel.
- Control a browser: a cloud browser for unattended work, and the user's own Chrome (through
  the extension) for sites where the user is logged in.
- Run scheduled jobs and multi-step workflows without a human present, and deliver results to
  any channel.
- Learn from use: curated memory, agent-written skills, and session search.
- Edit code in a sandbox and open pull requests, including pull requests to sama itself.
- Serve a team now and many customers later, with no change to the data model.
- Be a library and service that other products (vorflux first) build on.

### 2.2 Non-goals for v1

- No local install, terminal UI, or desktop app. (Hermes has these. Our users are in the
  cloud surfaces above.)
- No model providers other than Anthropic and OpenAI. (Owner decision for vorflux, kept here.)
- No own agent loop. We use the two SDKs.
- No third-party plugin code inside the control plane. Extensions come as MCP servers, skills,
  or Composio toolkits.
- No model training pipeline (Hermes batch trajectories). We keep transcripts so that we can
  add this later.
- No public skill marketplace in v1. We curate built-in skills in this repo.

### 2.3 Terms

| Term | Meaning |
|---|---|
| Org | A tenant: our team now, one customer account later. The billing and isolation unit. |
| Workspace | A unit of work inside an org, for example a project or a team. vorflux maps one hotel to one workspace. Memory, skills, jobs, and connections can be workspace-scoped. |
| User | A person in an org, with a role (`owner`, `admin`, `member`). |
| Agent profile | A named assistant configuration: SOUL, default runtime and model, toolsets, skills, approval policy, budgets. Like a Hermes profile, but stored as data. An org can have many profiles. |
| SOUL | The persona and standing instructions of an agent profile (Hermes `SOUL.md`). |
| Session | One conversation with an agent profile: messages, tool calls, and state. It can span many runs and stays on one runtime. |
| Run | One execution of agent turns for a session, in one sandbox, from a trigger to a stop. |
| Trigger | What starts a run: a user message, a cron job, a workflow step, a webhook, or a delegated task. |
| Runtime | An agent SDK that executes a run: `claude_agent_sdk` or `openai_agents_sdk`. |
| Adapter | Sama code that maps one runtime to the sama runtime contract (Section 5). |
| Runner | The sama process inside the sandbox that hosts the adapter and the runtime. |
| Sandbox | An isolated E2B microVM that holds the workspace files, the runner, and the shell for one session. |
| Toolset | A named group of tools. An agent profile enables toolsets. |
| Sama tools | Tools that sama implements once for both runtimes (memory, skills, cron, delegation, browser, connectors). They reach the runtime through the sama MCP tool server. |
| Native tools | Tools that a runtime ships (for example Claude `Read`, `Edit`, `Bash`). They run inside the sandbox. |
| Connector | A kind of external system that the agent can use as tools: a Composio toolkit, an MCP server, a database, an HTTP API, or a plugin (Section 10.3). |
| Connection | One configured account or endpoint of a connector, owned by a user or a workspace, with its credentials in the sama vault. |
| Channel | A surface where users talk to the agent: web, Slack, Telegram, Chrome side panel. |
| Skill | A versioned folder with a `SKILL.md` (agentskills.io format) that tells the agent how to do a task. |
| Memory | Small, curated notes that sama puts in every new session (Section 8). |
| Job | A scheduled trigger (cron expression or one time) with a prompt, skills, and a delivery target. |
| Workflow | A saved, versioned multi-step process with triggers, agent steps, tool steps, and approval steps (Section 12.3). |
| Approval | A human decision on one paused tool call. |

---

## 3. Decisions

### 3.1 Owner answers (2026-11-18)

| Question | Answer | Effect on the design |
|---|---|---|
| How does Sama relate to vorflux and `hermes_like_agent`? | Sama is the core engine. We build products on top of it later. | Sama is domain-neutral and has a public Python API and HTTP API. Hotel parts stay in vorflux. |
| Who uses it? | Our team now, customers later. | Multi-user from day one. `org_id` on every row and RLS. Sign-up and billing come later (M6). |
| Where does it run? | Only in the cloud. | Fly.io control plane, E2B sandboxes, Browserbase browsers. The Chrome extension connects out to the cloud. |
| Language? | Python core. TypeScript only for the Chrome extension and the web UI. | Python 3.12, uv, FastAPI. Next.js web UI. |
| First pull request? | Design doc only. | This document, the feature map, and the roadmap. |
| Runtimes? | Claude Agent SDK default, OpenAI Agents SDK second. | Two adapters behind one contract (D2). |

### 3.2 Key decisions

| # | Decision | Reason |
|---|---|---|
| D1 | Write our own Python code. Do not fork or embed Hermes. Copy ideas, prompts, and limits from Hermes with attribution (`THIRD_PARTY_NOTICES.md`). | Hermes is a single-user app with file-based state and its own agent loop. We need multi-user state and two SDK loops. Hermes uses the MIT license, so we may reuse small parts with the notice. |
| D2 | One runtime contract (`AgentRuntime`) with two adapters: `claude_agent_sdk` (default) and `openai_agents_sdk`. A `fake` runtime exists for tests. | The owner uses only Anthropic and OpenAI models. One contract keeps every sama feature runtime-neutral and makes a third runtime cheap. |
| D3 | Sama tools reach both runtimes through **one MCP server per run** (streamable HTTP), scoped by a run token. | Both SDKs are MCP clients. One implementation serves both. Tenant scope, approvals, and audit happen in one place. |
| D4 | The runner (adapter plus SDK) runs **inside the sandbox**, not in the API process. | The Claude Agent SDK starts the Claude Code CLI as a subprocess, and its native tools run where the CLI runs. The OpenAI `SandboxAgent` needs a sandbox too. The OS boundary is the only real boundary against a model that misbehaves (Hermes security policy). |
| D5 | **E2B** is the sandbox provider, behind a `SandboxProvider` interface. One sandbox per session. E2B pause and resume keep the workspace between runs. Docker is for local development and CI. | Owner chose cloud sandboxes with E2B. The OpenAI Agents SDK has an E2B sandbox client. The interface keeps a path to Fly Machines, which vorflux chose. Owner decision: E2B now, Fly Machines later only if needed (Q1). |
| D6 | **No long-lived secrets in the sandbox.** Model calls go to the sama **model proxy** (`ANTHROPIC_BASE_URL` / OpenAI `base_url`) with a run token. Integration calls go through sama tools on the control plane. | A prompt-injected agent cannot leak a key that it cannot read. The proxy also meters cost, enforces budgets, and sends traces. |
| D7 | **Supabase** is the system of record: Postgres with RLS, Auth, Storage, and pgvector. | Already in the owner's stack. RLS is a second tenant guard under the API. pgvector covers session search and memory recall. |
| D8 | **Postgres is the job queue** (`FOR UPDATE SKIP LOCKED` with leases) for runs, cron fires, workflow steps, and deliveries. No separate workflow engine in v1. | Fewer moving parts. vorflux made the same choice. The queue sits behind an interface. |
| D9 | The **prompt prefix is frozen per session**: SOUL, context files, tool list, memory snapshot, skill index. New memory and skills apply from the next session. A short note on the next user message tells the agent about new skills. | Hermes rule. It keeps prompt caching valid on both SDKs and makes runs reproducible (`RunConfig.fingerprint()`). |
| D10 | **Connectors use one model for all kinds**: Composio toolkits, remote MCP servers, hosted stdio MCP servers, databases, HTTP APIs, and plugins. Connections are per user or per workspace, with credentials in the sama vault. Rare tools sit behind `tool_search`. | The owner has Composio and any MCP server. This gives hundreds of apps without our own connector code, and keeps the tool list short. |
| D11 | **Channels** are adapters in one gateway: web (first-party), Slack (Events API over HTTP, Web API with `httpx`), Telegram (Bot API webhook), and the Chrome extension (WebSocket). Email and WhatsApp later. Chats bind to a user and an agent profile through pairing. | Same shape as the Hermes gateway: normalise inbound, send outbound, render approvals, map slash commands. Unpaired chats get no agent access. |
| D12 | **Browser control has one tool contract with two targets**: `cloud` (Browserbase) and `my_chrome` (the extension). The tools work on page snapshots with element refs, and a vision mode uses screenshots and coordinates. | The agent learns one set of tools. Unattended jobs use the cloud browser. Logged-in personal sites use the user's own Chrome with consent. |
| D13 | **Approvals fail closed.** Each tool call gets `allow`, `ask`, or `deny` from a policy. `ask` pauses the run and sends an approval card to the user's channel. Unattended runs (cron, workflows, webhooks) turn `ask` into `deny`, unless a human approved a pre-approval for that tool in that job version. A hardline deny list cannot be overridden. | Hermes three-layer model. A human is not present for unattended runs. |
| D14 | **Self-improvement writes are versioned data.** Memory, skills, cron jobs, workflows, and SOUL changes are rows with history and provenance. Changes to sama's own code are pull requests that a human merges. | The agent can learn and change its stack, but every change can be reviewed and reverted. |
| D15 | **Langfuse** traces both runtimes. The model proxy records tokens and cost per run. The control plane sends the traces from its stored rows after each run (Section 18.2). | One trace view for both SDKs. The proxy is the source of truth for cost. The sandbox has no Langfuse key (D6). |
| D16 | **Sama is a library plus services.** Products import `sama` and register extra tools, skills, toolsets, channels, and policies through extension points. They do not patch sama code. | vorflux and later products build on the engine without a fork (Hermes rule: plugins never touch core). |
| D17 | **Databases use a sama database gateway, and stdio MCP servers run in connector hosts.** The gateway enforces read-only transactions, one statement per call, timeouts, row limits, and hidden columns at the driver. A connector host is a separate sandbox per connection that holds only that connection's secrets. | A prompt cannot remove a limit that the driver enforces. Many SQL MCP servers give full rights through one tool. Running stdio servers outside the agent sandbox keeps credentials out of the sandbox (D6) and still supports the Hermes MCP ecosystem. |
| D18 | **One control plane process per environment in M1.** The API, the tool server, the model proxy, the worker, the monitor, and the Telegram hub (Section 11.6) run in one process on one Fly.io machine (Section 19.1). | The clarify broker and the event wake-ups are in memory, so the API and the tool server must share a process. One machine is enough for the team. Split into process groups when the broker moves to Postgres and more machines are needed. |

---

## 4. System overview

```mermaid
---
config:
  flowchart:
    wrappingWidth: 1000
---
flowchart LR
  subgraph CH["Channels"]
    WEB["Web app<br/>(Next.js, Vercel)"]
    SL["Slack"]
    TG["Telegram"]
    EXT["Chrome extension<br/>side panel + actions"]
  end

  subgraph CP["Control plane (FastAPI on Fly.io)"]
    API["API + auth"]
    GW["Channel gateway"]
    ORCH["Run orchestrator<br/>Postgres queue"]
    SCHED["Scheduler<br/>cron + triggers"]
    WF["Workflow engine"]
    MCP["Sama MCP tool server"]
    MP["Model proxy<br/>keys, budgets, usage"]
    APR["Approval service"]
  end

  subgraph SB["Per-session sandbox (E2B)"]
    RUN["Runner<br/>adapter + SDK"]
    FS["Workspace<br/>files, git clones"]
  end

  DB[("Supabase<br/>Postgres + RLS,<br/>Storage, pgvector")]
  BB["Browserbase"]
  CMP["Composio +<br/>remote MCP servers"]
  LLM["Anthropic / OpenAI"]
  LF["Langfuse"]

  WEB --> API
  SL --> GW
  TG --> GW
  EXT --> GW
  GW --> API
  API --> ORCH
  SCHED --> ORCH
  WF --> ORCH
  ORCH -->|"create / resume"| SB
  RUN --> FS
  RUN -->|"run token"| MCP
  RUN -->|"run token"| MP
  MP --> LLM
  MP --> LF
  MCP --> DB
  MCP --> BB
  MCP --> CMP
  MCP -->|"my_chrome actions"| GW
  APR --> GW
  API --> DB
```

### 4.1 Components

| Component | Responsibility |
|---|---|
| **API** | Auth (Supabase JWT, org API keys for `/v1` and `/mcp` (Section 10.7)), REST and streaming endpoints, admin endpoints for profiles, skills, memory, jobs, workflows, and connections. The only holder of the database service key. |
| **Channel gateway** | Receives Slack, Telegram, and extension events. Checks pairing and access. Maps each chat or thread to a user, an agent profile, and a session. Streams replies, renders approval cards, and handles slash commands. Also routes `my_chrome` browser actions to the right extension connection. |
| **Run orchestrator** | Queues runs in Postgres with leases, one main run per session and parallel side runs up to the limit of the profile (Section 4.3). Enforces per-org concurrency, budgets, and rate limits. Creates or resumes the session sandbox, starts the runner, streams events, and handles cancel, timeout, crashed workers, and resume after approval. |
| **Scheduler** | Fires cron jobs and one-time jobs. Receives webhooks and Composio triggers. Creates runs or workflow instances. One leader at a time through a Postgres lease. |
| **Workflow engine** | Runs workflow instances step by step as queue items. Stores each step result. Pauses on approval steps. |
| **Sama MCP tool server** | The sama tools for both runtimes: memory, skills, session search, cron, workflows, delegation, browser, connectors (Composio and MCP proxy), todo, clarify, approvals, and change proposals. Each call carries the run token. |
| **Model proxy** | Holds the Anthropic and OpenAI keys. Accepts the run token, checks the model allowlist and budget, forwards the call, records usage and cost, and sends the trace. |
| **Approval service** | Stores pending approvals, routes them to the user's channel, records the decision, and resumes the run. |
| **Sandbox** | Runs the runner, the native shell and file tools, and code. Has no database access and no long-lived secrets. Egress is limited to the sama endpoints and an allowlist. |

### 4.2 A run, end to end

1. **Receive.** A user writes "Every Monday 9:00, send me a summary of open GitHub issues"
   in Telegram.
2. **Resolve.** The gateway finds the paired user, the default agent profile, and the
   session bound to this chat. It creates a session if none is open.
3. **Queue.** The API stores the message and queues a run.
4. **Prepare.** The orchestrator builds the frozen `RunConfig` for a new session: SOUL,
   context files, memory snapshot, skill index, toolsets, runtime, and model. For an open
   session it reuses the stored `RunConfig` (Section 6.1, M1.11).
5. **Provision.** The orchestrator resumes the session sandbox (or creates one from the sama
   E2B template) and issues a run token. The token is bound to `org_id`, `user_id`,
   `session_id`, and `run_id`, and expires after the run.
6. **Execute.** The runner starts the adapter. The SDK calls the model through the model
   proxy and calls sama tools through the MCP server. Here the agent calls
   `cronjob_manage(action="create", ...)`.
7. **Gate.** The tool gate checks the policy. `cronjob_manage` is `allow` in a private
   session and `ask` in a shared session, so here the call runs. If the job pre-approves
   tools, runs a script, or sends to another place, it waits in `pending_approval` and does
   not fire until the owner approves that version with `/cron approve` or on the jobs page
   (Section 12.1). A risky call (send an email to a customer, push code) gets `ask`: the run
   pauses with `ApprovalRequired`, and an approval card goes to Telegram.
8. **Finish.** The run ends with `RunCompleted` or `RunFailed`. The sandbox pauses.
9. **Learn.** A background review may propose memory or skill updates (Section 9.4).
10. **Record.** Transcript, tool calls, usage, cost, and approvals are in Postgres. The trace
    is in Langfuse.

### 4.3 Run queue (M1.10)

The run orchestrator is a Postgres queue (`sama.orchestrator.queue.RunQueue`) and a worker
(`sama.orchestrator.worker.Worker`). Migration `0005` adds the queue columns to `runs` and
`sandbox_id` to `sessions`. It adds no table.

**Enqueue.** `RunQueue.enqueue` checks that the session is in the org and is `active`. In one
transaction it stores a `queued` run, with the runtime and the budget of the agent profile, and
the user message with the `run_id`.

**Claim.** `claim(owner)` takes the oldest `queued` run whose session has no active run and no
older queued run, with `FOR UPDATE SKIP LOCKED`. The run becomes `running` with a lease:
`lease_owner` and `lease_expires_at` (30 seconds). `attempts` goes up by 1. Thus:

- One session has at most one active main run (`running` or `paused`). The partial unique index
  `ix_runs_one_main_per_session` makes this a database rule too. Since M7.1, side runs can run
  next to it (see "Busy modes and parallel runs" below).
- The messages of a session run in order. A new message during a run waits in the queue.
- Sessions run in parallel, and two workers never take the same run.

A new run gets the runtime state of the last completed run of the session with the same
runtime, so the adapter continues the session (Section 5.1).

**Review runs (M2.4).** A run has a kind: `chat` or `review` (migration `0016`). A review run is
queued by sama and not by a person. The claim treats the two kinds in this way: a chat run goes
before a review run, a chat run waits only for older chat runs, and a review run waits for every
queued chat run of its session. The runtime state, the rate limit, the chat run list, and the
event stream of the API count chat runs only. Section 9.4 has the design.

**Lease.** While the run streams, the worker extends the lease every third of the lease time.
Each write (events, the result) checks that the worker still holds the lease of this attempt:
the same `lease_owner`, the same `attempts` value, and a lease that did not expire. Thus an old
attempt cannot write after a reap, also when the same worker claims the run again. A worker that
lost the lease cancels the runtime at once, reads no more events, and writes nothing more. Check constraints keep the state valid:
a `running` run has a lease and other runs have none, and a `paused` run has its approval id.

**Events.** The worker stores each event in `run_events` with an increasing `seq`, without the
runtime state. It writes text deltas in batches (at most 50 events or 0.25 seconds) and other
events at once. A resumed run continues the `seq` of the run.

**End.** `RunCompleted` stores the reply as an assistant message and the runtime state.
`RunFailed` stores the failure code and message; the code `cancelled` gives the status
`cancelled`. `RunFailed.runtime_state` is optional. A runtime sets it only to keep a count that the
failure would lose, and the next run of the session starts from the newest state, also the state
of a failed run (Section 6.3, "Tokens of a compaction"). A stream with no terminal event fails with `internal`. When a run ends (not when it
pauses), the worker calls the run-end hooks, for example `ClarifyBroker.cancel_run`.

**Cancel.** `cancel(org_id, run_id)` ends a `queued` or `paused` run at once. For a `running`
run it sets `cancel_requested_at`. The worker sees it at the next lease extension and cancels
the runtime. If the runtime asks for an approval after a cancel request, `pause` locks the row,
sees the request, and ends the run as `cancelled`. `release_decided` never queues a run with a
cancel request. Thus a cancel always wins over an approval.

**Wall time.** The worker cancels the runtime when the run is longer than
`Budget.max_wall_time_s`. The run fails with `timeout`.

**Busy controls (M3.9, migration `0030`).** A user can send input while a run of the session is
open. Sama has three controls, and they work in Telegram, Slack, and the web app (Section 11.1):

- **Queue.** A new message waits in the queue, as above. `waits(org_id, run_id)` is true when an
  older open run of the same session comes before the new run. The channels then tell the user
  that the message is queued and how to steer or stop.
- **Stop.** `/stop` uses `cancel` (above).
- **Steer.** `steer(org_id, session_id, text, user_id)` adds a note (at most 4,000 characters,
  `MAX_STEER_CHARS`) to the active chat run of the session. A `/compact` run, a review run, and
  a run with a cancel request take no notes. When no run takes the note, `steer` returns
  `None` and the caller sends the text as a normal message. The note goes into the table
  `run_steers` with the status `pending`. A run has at most 10 pending notes
  (`MAX_PENDING_STEERS`). A user over the message rate cannot add a note, but the note itself
  does not count for the rate. `steer` takes a share lock on the run row, so the end of the run
  waits for the insert and sees the note.

The tool gate gives the notes to the run. At each tool call of a `running` run, `take_steers`
marks the pending notes as `delivered` and returns one text block (`steer_block`). A note adds
to the request of the run and does not replace it (M7.1). So the block, in an
`<added_messages>` tag, tells the model that each note is a new request in addition to the
original request, and that a note cancels or replaces the original request only when it says so
in clear words ("stop", "instead", "do not"). If two requests seem to conflict, the model does
both. The final reply must answer every request. (A first wording that said "follow the added
message for the part that it changes" made `claude-sonnet-5-5` drop the original request when a
note asked for a different reply word.) The
block repeats the original request (the first user message of the run, cut to 2,000
characters) and lists every open note of the run, oldest first: the notes of earlier steps are
marked "you got this one before", then the new ones. A note cannot close the tag. The gate
answer has the block in the field `steer`, but only for a runner that sends the header
`x-sama-steer: 1` (an older runner does not know the field). The runner sends the header only
for a runtime with `Capabilities.mid_run_steer`. The Claude adapter has it: it puts the block
in `additionalContext` of the `PreToolUse` hook, so the model reads it with the result of that
tool call. The OpenAI adapter (M3.6) does not have it, because its tool gate has no way to add
text for the model. So in an OpenAI run the notes stay pending. A run that calls no more tools
does not read the note. Then, when the run completes,
`complete` sends the pending notes as one new user message of the session (status `requeued`,
with `requeued_run_id`). The run answered the original request already, so the new run answers
the notes, and both requests get an answer. That message does not need the rate check, because each note passed it.
`requeued_run(org_id, run_id)` gives the new run, and the chat followers follow it. The worker
maintenance calls `sweep_steers()`: it sends the pending notes of completed runs (a note that
came in after `complete`) as a new message, and it drops (status `dropped`) the pending notes of
failed and cancelled runs. It handles at most 50 runs in one pass (`STEER_SWEEP_BATCH`).

**Busy modes and parallel runs (M7.1, migration `0044`).** The busy mode of an agent profile
(`agent_profiles.busy_mode`) says what a new message does while a run of its session is open.
The modes follow Claude Code:

| Mode | A new message while a run is open |
|---|---|
| `steer` (default) | Joins the running run at its next step, as a note of `/steer`. The model reads it at the next tool call, so one run handles both requests. If the run ends before its next step, the note becomes the next message at once (the rules of `/steer` above). |
| `parallel` | Starts a second run at the same time, in the same session and the same sandbox. |
| `queue` | Waits in the queue, as before M7.1. |

In every mode, `/bg TASK` (Telegram, a Slack message, the web composer) and the "Run in
parallel" button of the web chat start a parallel run, like a background agent of Claude Code.
`agent_profiles.max_parallel_runs` (default 3, from 1 to 10) is the most open chat runs
(`running` or `paused`) of one session.

- **Join.** In the mode `steer`, the channels and `POST /sessions/{id}/messages` first call
  `steer(..., busy_mode=True)`. It adds the text as a note only to a `running` chat run of a
  profile in the mode `steer`, and only a plain text message: no image, no skill command, at
  most `MAX_STEER_CHARS`. A run with `MAX_PENDING_STEERS` notes does not take more. When no run
  takes the note, the message goes to `enqueue` as before. When the session has more than one
  open chat run, the newest run gets the note. The API answers `steered: true`, and
  `GET /sessions/{id}/steers` lists the notes with their status (`pending`, `delivered`,
  `requeued`, `dropped`), so the web chat shows "Queued into the current run" and then
  "Delivered to the run". The OpenAI runtime has no mid-run steer (Section 5.3), so there a note
  always becomes the next message when the run completes.
- **Enqueue.** `enqueue(..., parallel=True)` or the mode `parallel` sets
  `runs.allow_parallel`.
- **Claim.** `_ready` is the rule of a queued run. FIFO per session stays: no older chat run of
  the session waits. A review run starts only when the session has no open run. A chat run never
  starts next to an open review run or `/compact` run. A chat run starts when the session has no
  open chat run, when it resumes after an approval (it was open before), or when it has
  `allow_parallel`, is not a `/compact` run, and the session has fewer open chat runs than
  `max_parallel_runs`. A chat run that starts while another chat run of its session is open is a
  side run (`runs.side`). Each chat run that starts stores `runs.context_seq`, the newest
  `messages.seq` of the session at its claim.
- **Locks.** The candidate statement locks the run and its session row
  (`FOR NO KEY UPDATE OF runs, sessions SKIP LOCKED`), so the claims of one session are in a
  sequence and the count of open runs stays true until the claim commits. `complete` takes the
  same session lock before it stores the reply. So the transcript that a run reads at its claim
  has every reply that committed before the claim and no later reply. A message insert takes only
  a key share lock, so the claim lock does not stop an enqueue.
- **Side run.** A side run has no runtime state: it starts a new runtime session (a new Claude
  SDK session, or a new OpenAI conversation), so it never writes the runtime files of another
  run. Its input carries the transcript (`sama.orchestrator.parallel.side_block`): the newest 30
  messages of the session up to its `context_seq` (each cut to 2,000 characters, at most 24,000
  characters), and the requests of the other open runs, with the rule that it must not do their
  work or change their files. It gets all pins and does not reserve them. It uses the frozen
  `RunConfig` of the session, so the prefix and the prompt cache are the same (D9). Its runtime
  state is stored but not used by later runs: `_session_state_query` skips side runs.
- **Main run and catch-up.** A main run resumes the runtime state of the newest main run, as
  before. The side runs that completed after that state was made (their reply is newer than the
  `context_seq` of the run that made the state) are not in it. The input of the main run carries
  them in a `<parallel_answers>` block, in the order of completion, at most the newest 10 turns
  (`catch_up_block`). The claim reads the newest side reply in its one `UPDATE ... RETURNING`, so
  a run with no catch-up uses no extra statement. The next main run resumes a state that has
  the catch-up, so no turn is lost and no turn comes two times. `runs.state_seq` keeps the
  watermark of the state that a run resumed. A failed or cancelled run saves the conversation
  from before it (Section 5.3), so `fail` sets its `context_seq` back to `state_seq`. A
  `/compact` run reads no catch-up, so its `context_seq` is `state_seq` too. Thus a failure or a
  compaction never hides a side answer from the next main run.
- **Order.** Each reply is stored when its run completes, so the replies join the transcript and
  the channel thread in the order of completion. Each channel follows each run, so each answer
  comes in the same thread when its run ends.
- **Stop.** `/stop` stops all open runs of the session. `/stop NUMBER` stops the run at that
  place of `open_runs` (the open chat runs, oldest first). A wrong number lists the open runs.
  The answer to a parallel run tells its number. `GET /sessions/{id}/open-runs` gives the list.
- **Coding agents (M7.2, Section 15.4).** A coding agent CLI (`claude_code`, `codex`) cannot
  read a note in the middle of a run, and it has no runtime session that a side run can share.
  So in a session in the direct mode (`sessions.coding_agent`), or while a coding agent run is
  open, the mode `steer` acts as `queue`: the message waits. No side run starts in a session in
  the direct mode, next to an open coding agent run, or as a `/claude` or `/codex` task.
- **Limits and quotas.** A side run counts like any run: one message for the message rates, one
  run for `max_running_runs` of the org and the plan (Section 17.5), and its model cost for the
  model quotas. A run that waits for the session limit stays `queued`.
- **Sandbox.** Both runs use the session sandbox, as sub-agents of Claude Code share the work
  folder. The side runner takes no runner lock (Section 16.2). Two runs can change the same
  files. Each runner takes its checkpoints under the checkpoint lock, and `/undo` refuses when the
  checkpoints of the last run mix with those of another run (Section 15.3).

**Approvals.** `ApprovalRequired` pauses the run: the status is `paused`, `pending_approval_id`
is the approval id, and the runtime state is stored. If the tool gate did not store the
approval, the queue stores it with the same rules as the gate (Section 16.1). When the approval
is decided or expired, `release_decided` queues the run again with `resume_decision` and
`resume_reason`. An expired approval resumes as `reject`. The next claim takes the paused run
before the newer runs of the session and sends a `ResumeRequest`. Each approval resumes once,
because `release_decided` acts only on a `paused` run, and the run is not `paused` after it.

**Dispatch.** After the setup and before the worker starts the runtime, `dispatch` sets
`runs.dispatched_at`. From this point a runner can execute tools, also when no event or tool
call is stored yet.

**Crashed workers.** `reap()` handles a `running` run whose lease expired:

- If a cancel was requested, the run becomes `cancelled`.
- If the run is not dispatched and `attempts` is less than 3, the run goes back to the queue.
  No runner started, so a new attempt is safe.
- Otherwise the run fails with `internal`. A tool can have run, so the run does not start
  again.

A runner of an ended run can still be alive (for example, the worker process stopped but the
sandbox did not). The tool gate denies every call of a `completed`, `failed`, or `cancelled`
run, so such a runner cannot use a tool (Section 16.1).

**Maintenance.** Every 30 seconds each worker times out approvals (`GateService.expire`),
queues the decided runs, and reaps runs. The worker makes a `GateService` on the queue engine
when the caller gives none. `Worker.serve(stop, concurrency=n)` runs `n` runs at
the same time and the maintenance loop. When `stop` is set, the current runs end first.

**Wakeup (migration `0020`).** An idle worker slot does not only poll the queue. Migration `0020`
adds the trigger function `sama_private.notify_run_queue()`. It sends `NOTIFY sama_runs` when a
run becomes `queued` (an insert or an update) and when a `running` or `paused` run changes to
another status, because the end of a run can free the next run of its session. The notification
has no payload. `QueueWakeup` (`sama.orchestrator.wakeup`) holds one database connection that
listens on the channel, and it wakes all idle slots on each notification. A slot takes a ticket
before it reads the queue and then waits on that ticket, so a notification that comes during
the read is not lost. The poll (`poll_s`, 1 second on the control plane) stays as the fallback:
if the listen connection breaks (the listener connects again after 5 seconds), or if a
connection pooler does not send notifications, each slot still finds the run on its next
poll. The `LISTEN` setup and each check of the connection have a time limit of 10 seconds. At
a worker stop, the listener gets 5 seconds to end, and then it is cancelled, so a database that
does not answer cannot hold the stop. A notification only starts a claim, so a lost or an extra notification cannot change the
queue rules. Before this change the wait from the enqueue to the claim was up to 1 second, on
average about 0.5 second.

**Round trips.** Each statement is one round trip to the database, and each transaction adds a
`BEGIN` and a `COMMIT`. The control plane and the database can be in different regions, so
the hot path of a run uses few statements:

- `enqueue` uses three statements in one transaction: the rate lock, one read of the session,
  the profile, and the rate counts, and one insert of the message and the run (a CTE). The
  read comes after the lock, in its own statement, so its counts include every enqueue before
  the lock.
- `claim` reads the full orgs without a transaction. In one transaction it reads the
  candidate, takes the running lock, reads the limit, and claims the run with one
  `UPDATE ... RETURNING` that also reads the session, the profile, the user message, the next
  event number, and the runtime state.
- A read that needs no lock (the full orgs, the frozen run config of the session) and a write
  of one statement (`dispatch`, `heartbeat`, and `add_events` when no event is `compacted`)
  run without a transaction (`sama.db.autocommit`). `add_events` inserts the batch only when
  the worker holds the lease, in one `INSERT ... SELECT` that locks the run row.
- `as_user` sets the claims and the role of an API request in one statement.

`tests/orchestrator/test_round_trips.py` keeps these counts. With a round trip of 59 ms (the
staging control plane to its database), the database part of one chat run went from about
2.6 s (20 statements) to about 1.4 s (14 statements): the enqueue from 0.61 s to 0.31 s, the
claim from 0.93 s to 0.50 s, the run config from 0.19 s to 0.06 s, and the first event batch
from 0.25 s to 0.06 s. The end of a run (`complete`) did not change.

**Migration.** Before migration `0005`, a `running` or `paused` run had no lease and no
approval id. No worker can continue such a run, so the upgrade ends each one as `failed` with
`internal`.

**Sandbox directory.** `PostgresSandboxDirectory` stores the sandbox of a session in
`sessions.sandbox_id`. With it, `SandboxedRuntime` in a new process finds the sandbox of the
session and does not create a new one (Section 16.2).

**Per-org limits (M1.12).** `orgs.limits` (JSON) sets three limits. A missing or bad value
uses the default (`sama.orchestrator.limits.QueueLimits`):

| Key | Default | Rule |
|---|---|---|
| `max_running_runs` | 5 | At most this many runs of the org are `running` at the same time. |
| `user_messages_per_minute` | 20 | At most this many messages of one user go into the queue in a rolling 60-second window. |
| `org_messages_per_minute` | 120 | At most this many runs of the org go into the queue in a rolling 60-second window, also from jobs. |

- `enqueue` counts the runs of the last 60 seconds under an advisory lock per org, so two
  messages at the same time cannot both take the last place. Over a limit it raises
  `RateLimitedError` with the seconds to wait. The API answers `429` with `Retry-After`. The
  count is of queued runs, not of started runs: a backlog can start faster than this rate. A
  cancelled run still counts, and a run that resumes after an approval does not count again.
- `claim` skips the orgs that are at `max_running_runs`. It takes an advisory lock per org and
  counts again before it starts the run, so two workers cannot both take the last place. A
  `paused` run does not count, because it holds no worker.
- Migration `0007` adds the index `runs (org_id, created_at)` for the rate count and the
  partial index `ix_runs_running_per_org` for the running count.

**Not in M1.10.** The clarify broker stays in memory (Section 16.7). Since M1.11, the worker gets the `RunConfig`
from the prompt assembler (Section 6.1). The worker stores the config fingerprint on the run
when it dispatches the run.

---

## 5. Runtime adapter layer

### 5.1 The contract

The contract lives in `src/sama/runtime/contract.py` (M1). It reuses the design of the runtime
contract v2 from `hermes_like_agent` and vorflux:

```python
class RuntimeKind(StrEnum):
    CLAUDE_AGENT_SDK = "claude_agent_sdk"  # default
    OPENAI_AGENTS_SDK = "openai_agents_sdk"
    FAKE = "fake"  # scripted, for tests


class AgentRuntime(Protocol):
    kind: RuntimeKind

    def capabilities(
        self,
    ) -> Capabilities: ...  # native_subagents, native_compaction, native_skills, ...
    def run(self, req: RunRequest) -> AsyncIterator[RunEvent]: ...
    def resume(
        self, req: ResumeRequest
    ) -> AsyncIterator[RunEvent]: ...  # after an approval decision
    async def cancel(self, run_id: str) -> None: ...


RunEvent = (
    TextDelta
    | ToolCallStarted
    | ToolCallFinished
    | ApprovalRequired
    | Compacted
    | SubagentEvent
    | RunCompleted
    | RunFailed
)
```

- `RunRequest` holds the `RunScope` (org, workspace, session, run, audience, and the user for
  a private session), the user input, the frozen `RunConfig`, the run `Budget`, and the
  runtime state from the previous run. `ResumeRequest` holds the approval id, the decision
  (`approve` or `reject`), and the runtime state from `ApprovalRequired`. Since M7.1, both have
  `parallel`: true for a side run (Section 4.3). A side run has no runtime state, and the runner
  runs it next to the main runner of the session (Section 16.2).
- `RunConfig` is frozen per session. It holds only what the model sees and what the cache
  needs: runtime, model, the sama system text, SOUL, context files, memory snapshot, skill
  index, the environment text, tool schemas, and MCP server configs. A context file path
  cannot have `"`, `<`, `>`, or a line break, so it cannot close its tag. It has `fingerprint()` (SHA-256 of canonical JSON, the same in every
  process) and `render_prefix()` (the tiers of Section 6.1). Remote MCP configs hide their
  bearer token in `repr`. The token changes per run, so it is not in the fingerprint.
  JSON objects in the contract (tool input schemas and tool call arguments) are read-only
  after validation, also in nested values, so the fingerprint cannot change during a session.
- **Authority is not frozen.** The control plane checks membership, connection access,
  approval policy, budgets, and revocations again at every tool call, model call, and
  approval resume. A newer `deny` or a lower budget applies at once, also to long-lived
  sessions. Section 16.1 has the code (M1.18). Connection access comes with connections
  (M3.4). A tool that is still in the frozen schema but is no longer allowed gets `deny`
  from the tool gate.
- Every event carries running `Usage` for the whole run. The stream ends with exactly one of
  `ApprovalRequired`, `RunCompleted`, or `RunFailed` (with a `FailureCode`). Usage never goes
  down, also across an approval resume: the resumed stream starts from the usage of
  `ApprovalRequired`.
- One stream at a time per run. A runtime frees the run before it yields the terminal event,
  so the orchestrator can resume at once. Each approval resumes once, and only for its run.
  `cancel` stops an active stream and makes the approvals of a paused run invalid.
- The adapter saves the runtime's own resumable state as an opaque blob (`bytes`). In JSON it
  is base64, so any byte value can be stored. The orchestrator stores it with the run.
- **Compaction (M2.6).** `RunRequest.compact` asks the runtime to compact the session now, with
  no user input and no answer. `Capabilities.manual_compaction` says that the runtime does it.
  `Capabilities.compaction_archive` says that the runtime gives its transcript to an `Archiver`
  before it compacts. `Compacted.trigger` is `manual` or `auto`. Section 6.3 has the design.

### 5.2 What sama owns and what the runtime owns

| Concern | Owner | Notes |
|---|---|---|
| Model calls, the tool loop, native compaction | Runtime | Sama does not write an agent loop. |
| Native shell and file tools | Runtime | Inside the sandbox only. |
| Sama tools (memory, skills, search, cron, workflows, delegation, browser, connectors) | Sama, through MCP | One implementation for both runtimes. |
| Approval policy and decisions | Sama | Adapters map SDK permission hooks to `ApprovalRequired`. |
| Prompt prefix (SOUL, memory, skill index) | Sama | Built once per session (Section 6). |
| Canonical transcript | Sama | Stored in Postgres. The runtime's own session state is a blob. |
| Tenant scope, budgets, audit | Sama | Enforced in the orchestrator, model proxy, and MCP server. |
| Sandbox lifecycle | Sama | The OpenAI `SandboxAgent` gets a client for the session sandbox. |

### 5.3 Mapping per runtime

| | Claude Agent SDK (default) | OpenAI Agents SDK (second) |
|---|---|---|
| Package | `claude-agent-sdk` (Python). It drives the bundled Claude Code CLI as a subprocess, so the sandbox image needs the CLI. | `openai-agents` (Python), in process. |
| Turn API | `ClaudeSDKClient.query()` and `receive_response()` | `Runner.run_streamed()` |
| Model routing | `ANTHROPIC_BASE_URL` points at the sama model proxy; the run token is the API key. | `AsyncOpenAI(base_url=<proxy>/openai/v1, api_key=<run token>)` and the Responses API |
| System prompt | `system_prompt` preset `claude_code` with the sama prefix appended, or a full custom prompt for non-coding profiles | `Agent.instructions` |
| Sama tools | `mcp_servers` with the sama MCP server (HTTP); `strict_mcp_config=True` | `MCPServerStreamableHttp` in `Agent.mcp_servers` |
| Approvals | `PreToolUse` hook calls the sama tool gate. `ask` returns a defer and the run pauses. Fallback: `can_use_tool` deny plus interrupt. | `needs_approval` on tools. `result.interruptions` become `ApprovalRequired`. `RunState.approve()` or `reject()` on resume. |
| Resume | `resume` with the SDK session id; `session_store` mirrors the transcript to sama. | Serialised `RunState` plus a sama `Session` (a `SessionABC` that reads and writes items through the control-plane API with the run token). The runner has no database credentials, so `SQLAlchemySession` is not used in the sandbox. |
| Sub-agents | `agents` option (`AgentDefinition`: prompt, tools, model, skills, MCP servers). Sama sets it from the agent profile (Section 13.1). | Agents as tools since M7.4: each sub-agent is a tool `subagent_<name>` (`Agent.as_tool`) of the main agent (Section 13.3). Handoffs are not used. |
| Skills | Native: sama writes the session's skills to `.claude/skills/` and sets the `skills` option. | The sama `skill_view` tool over MCP (`native_skills` is false). |
| Compaction | Native auto-compaction. The `PreCompact` hook gives the full transcript to the sama archive first. `/compact [focus]` is a CLI slash command that the adapter sends for a compact request (M2.6, Section 6.3). | The `Compaction` capability turns on server-side compaction. A manual compaction calls `POST /responses/compact` and replaces the items. The adapter archives the dropped items first, so it sets `manual_compaction` and `compaction_archive` (M3.6). |
| Shell and files | Native `Bash`, `Read`, `Edit`, `Write`, `Glob`, `Grep` | `SandboxAgent` with the `Shell` capability (`exec_command`). The runner is already inside the E2B sandbox, so it uses the local client (`UnixLocalSandboxClient`). The orchestrator owns the E2B lifecycle. The runner has no E2B API key. |
| Limits | `max_turns`, `max_budget_usd`, `task_budget` | `max_turns`; sama budget in the model proxy |
| Checkpoints | Sama git snapshot of the work folder (Section 15.3). Sama does not use `enable_file_checkpointing`: it covers only the file tools and not `Bash`, and it does not work for the second runtime. | Sama git snapshot of the work folder (Section 15.3) |
| Tracing | Control-plane trace from the stored rows (Section 18.2) | Control-plane trace from the stored rows (Section 18.2) |
| Main risks | CLI subprocess and Node in the image. Fast option changes. Claude models only. | Fast-moving API surface (`SandboxAgent` is new). OpenAI models only. |

#### Claude adapter notes (M1.4)

The adapter is `src/sama/runtime/claude.py`. These rules come from live tests with
`claude-agent-sdk==0.2.184`:

- **Options.** `setting_sources=[]`, so no file settings from the sandbox apply.
  `strict_mcp_config=True`. Remote MCP servers are `http` servers with the run token in an
  `Authorization` header. `include_partial_messages=True` gives text deltas. The preset prompt
  uses `exclude_dynamic_sections=True`, so the prefix stays the same for the prompt cache.
- **Tool search off.** The adapter sets `ENABLE_TOOL_SEARCH=false` in the CLI env. With tool
  search on, the CLI defers MCP tools, and the model must call the native `ToolSearch` tool
  to load a tool first. The staging check of M1.16 found this. `ClaudeSettings.env` can
  override the switch. The staging check of M1.18 found that the CLI can still defer the
  tools with the switch off, so the gate allows `ToolSearch` in every run (a tool loader,
  Section 16.1). When a run has many connector tools, sama `tool_search`
  (Section 10.3) does this job.
- **Tool gate.** The `PreToolUse` hook calls the `ToolGate` with the tool use id (Section
  16.1). `allow` and `deny` go to the CLI.
  `ask` becomes `defer`. A sub-agent cannot pause the run, so the hook tells the gate that a
  call is of a sub-agent (`child`, from the `agent_id` of the hook input). The gate gives `deny`
  for it in place of `ask`, and it records no approval (Section 13.1). The runner sends `child` only
  when it is true, so an older control plane that does not know the key still accepts the body of a
  call of the main agent. If the gate fails, the
  call gets `deny`.
- **Pause.** On `defer`, the CLI ends the turn with `stop_reason` `tool_deferred` and
  `deferred_tool_use`. The adapter yields `ApprovalRequired`. The approval id is the tool use
  id. `runtime_state` holds the SDK session id, the run id, the usage, and the pending call.
- **Permission callback.** The adapter also sets `can_use_tool`. It allows only the tool use
  ids that the hook allowed or that a human approved. It denies all other calls. Without it,
  the CLI uses its own permission rules, and these can block a call that sama allowed.
- **Approve.** The adapter resumes the SDK session with no prompt. On resume the CLI checks
  the deferred call again. For most calls it asks `can_use_tool`. For some read-only calls it
  runs the `PreToolUse` hook again. Both allow the approved tool use id without a new gate
  check, so the orchestrator checks authority before it calls `resume` (Section 5.1). Then the
  CLI runs the call and the model continues.
- **Reject.** A deny from `can_use_tool` or the hook on resume is not a reliable reject: some
  read-only calls skip `can_use_tool`, and the model can try the call again. So the adapter
  first resumes with the tool in `disallowed_tools` and
  `CLAUDE_CODE_DROP_UNAVAILABLE_DEFERRED_TOOL=1`. The CLI drops the call
  (`stop_reason` `tool_deferred_unavailable`). Then the adapter resumes again with all tools
  and tells the model that the user rejected the call. If the CLI does not drop the call, the
  run fails with `internal`.
- **New message while paused.** A new run on a session with an unanswered approval drops the
  deferred call first, in the same way.
- **One deferred call per turn.** The CLI defers only one tool call per turn.
- **Usage.** Token counts come from each `ResultMessage`. The CLI reports cost for the whole
  SDK session, also after a resume, so the adapter adds only the difference. The model proxy
  record is the source of truth for billing (M1.7).
- **Cancel.** `cancel` calls `interrupt()`. If the CLI does not stop within
  `cancel_grace_s` (5 seconds by default), the adapter ends the stream and stops the CLI.
  The stream ends with `RunFailed(cancelled)` within 10 seconds. If the pump already queued
  `ApprovalRequired`, the stream gives `RunFailed(cancelled)` in its place.
- **CLI stop.** The SDK stops the CLI in steps (close stdin, terminate, kill). This can take
  about 20 seconds, and a cancel of that work can leave the CLI running. So the adapter runs
  `disconnect()` in a task of its own and never cancels it. The stream does not wait for it
  longer than `cancel_grace_s`. `ClaudeRuntime.aclose()` waits until all CLI processes stopped.
- **Limits.** `max_turns` and `max_cost_usd` map to `max_turns` and `max_budget_usd`. The CLI
  counts these limits per process, not per SDK session. So `runtime_state` keeps the turns
  and the wall time that the run used, and each resume gets only the rest of each limit. If a
  limit is used up, the run fails before the CLI starts. `max_wall_time_s` is a timer in the
  adapter. `max_tokens` is enforced by the model proxy.
- **Errors.** Failure messages are fixed text. CLI error strings and the text of SDK
  exceptions can hold raw tool data, so they do not go into `RunFailed`.
- **Sub-agents (M2.7).** The adapter sets the `agents` option from `RunConfig.subagents`
  (Section 13.1). Each `SubagentDefinition` becomes one `AgentDefinition`. A native tool of
  the definition keeps its name. Each other tool gets the prefix `mcp__sama__`. The
  definition always has `disallowedTools` `Task` and `Agent`, `background` off, and the
  model `inherit` when it has no model. With sub-agents, the adapter also sets
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` in the CLI env. Without it, the CLI starts a
  sub-agent in the background, and the run can end before the sub-agent ends. The
  `PreToolUse` hook denies `Task` and `Agent` before the gate when `subagent_type` is not a
  name in the config (this also stops the built-in agents of the CLI) or when
  `run_in_background` is set. Messages with a `parent_tool_use_id` become `SubagentEvent`s
  with the id of the `Agent` call in `call_id`. The system messages `task_started` and
  `task_notification` of a sub-agent give a start event and an end event (`done`, and
  `tokens` when the CLI reports them). The adapter never cuts the text of an event, because it
  has no redactor and a cut can split a secret. A text over 100,000 characters stays out, and a
  fixed marker takes its place. The control plane redacts the text and then cuts it to 2,000
  characters (Section 13.1).
  `compact_boundary` system messages become `Compacted`. The `trigger` of its
  `compact_metadata` (`manual` or `auto`) goes into the event (M2.6).
- **Compaction (M2.6).** With an `archiver`, the adapter adds a `PreCompact` hook next to the
  `PreToolUse` hook. The hook input has `trigger`, `custom_instructions`, and
  `transcript_path`. The hook reads the transcript file, gives the entries to the archiver,
  and always returns an empty output, so the compaction goes on also when the archive fails
  (Section 6.3). A compact request resumes the SDK session and sends `/compact <focus>` as the
  prompt. A compact request with no earlier state fails with `invalid_request`. A compact run
  with no compaction boundary fails (`model_error`). The tokens of a compact run come from the
  difference of `model_usage`, because `usage` is zero (Section 6.3). A failed run keeps this
  count in `RunFailed.runtime_state` (`_Translator.failure_state`), so the next run does not count
  the paid work of the failed run again. The adapter
  does not use the CLI hook `SessionStart` with the source `compact`, because the Python SDK
  does not expose it: sama injects the pins itself (Section 6.3).

#### OpenAI adapter notes (M3.6)

The adapter is `src/sama/runtime/openai_agents.py`. It uses `openai-agents==0.25.0`.

- **Agent.** Each run builds one `SandboxAgent` with the sama prefix as `instructions`, the
  `Shell` and `Compaction` capabilities, and one `MCPServerStreamableHttp` for each MCP
  server of the config, with the run token in an `Authorization` header. Each MCP call can
  take 3,600 seconds (`MCP_CALL_TIMEOUT_S`; 1,200 seconds before M7.2). The SDK default is 5 seconds, which fails every
  sama tool that takes longer (for example a new browser session or `clarify`). The SDK sandbox
  client is `UnixLocalSandboxClient` in the work folder, with no snapshot. The model is
  `OpenAIResponsesModel` over an `AsyncOpenAI` client with the proxy URL and the run token.
- **Session items.** The SDK `Session` of the run is a sama store (`_Store`) over
  `SessionItems` (`src/sama/runtime/session_items.py`). In the sandbox it is
  `RemoteSessionItems`: each operation is one `POST /session-items` to the tool server with
  the run token. The tool server takes the org, the session, and the run from the claims of the
  token. `PostgresSessionItems` (`src/sama/context/session_items.py`) checks that the run is
  `running`, that its user still has the authority of the run (Section 5.1), and that the
  session is `active`. It applies the operation in one transaction. The transaction first
  takes an advisory lock for the conversation, and then does the checks with a `FOR SHARE`
  lock on the run row. So an operation that waits for the advisory lock sees a cancel that
  came while it waited, and the worker cannot end the run while an operation commits. The
  rows are in `runtime_session_items` (Section 17.1). The operations
  are `get` (with a `limit`), `add`, `pop`, `clear`, `replace`, and `trim`. `seq` starts at 0
  and has no gaps, because every operation removes items only from the end or removes all of
  them. Limits: a body has at most 8 MB and 5,000 items, and a conversation has at most
  20,000 items and 16 MB of items (`MAX_SESSION_ITEMS_BYTES`, the UTF-8 bytes of the JSON text
  of each item). The store refuses an `add` or a `replace` over a limit, so a `get` answer is
  also at most 16 MB. The Postgres store checks the size again after the write, with the
  stored `jsonb` text, because a number such as `1e308` is longer in that text. If the size is
  over the limit, the transaction rolls back. The tool server keeps at most a fixed number of
  bodies and answers in memory at one time: a request holds its slot until the server sent the
  answer, so a slow client does not let more answers in. A store refusal is `409`, and the run fails with `internal`, not retryable.
  A store fault fails the run with `internal`, retryable.
- **Conversations.** `runtime_state` names the conversation of the session. A run with no
  `runtime_state` (the first chat run, or a background review run, Section 9.4) starts a new
  conversation, so a review run never changes the items of the chat. `item_count` in
  `runtime_state` is the count that the next run keeps. A run that failed or was cancelled
  keeps the count of the run before it, so the next run first trims the items of the failed
  run. This is also true for a failure before the trim, for example a store fault: the
  failure state keeps the count of the run before it, so the items stay. After an unanswered
  approval, the next run gives an output to each paused call (see Approve and reject). If
  that run fails before it gives the outputs, its failure state has `repair` set, so the run
  after it gives them.
- **Tool gate.** The adapter wraps each tool with `needs_approval`, which asks the sama
  `ToolGate` (Section 16.1). The shell call `exec_command` goes to the gate as a `Bash` call
  with a `command`, so one policy fits both runtimes. The adapter removes the SDK tool
  `write_stdin`, because it lets a call go on after the gate allowed it. MCP tools keep the sama name. `ask`
  makes the SDK stop with an interruption: the adapter saves the SDK `RunState` in
  `runtime_state` and yields `ApprovalRequired`. The approval id is the call id. A `deny`
  becomes a tool input guardrail that gives the model the reason in place of the result.
- **Sub-agents (M7.4).** The capability `native_subagents` is true. Each `SubagentDefinition`
  of the config becomes one tool `subagent_<name>` of the main agent, made with
  `Agent.as_tool`. The tool takes one `input` text. The gate and the events see the call as an
  `Agent` call with `subagent_type` and `prompt`, as on Claude, so one policy fits both
  runtimes. The sub-agent is a plain `Agent` with the prompt of the definition, the model of the
  definition or of the run, and `max_turns` (20 when not set). Its tools are only the sama MCP
  tools that the definition names, and a copy of the shell tool of the run when the definition
  names `Bash`. It has no agent tool, so it cannot start a sub-agent. Each tool call of the
  sub-agent asks the gate with `child`, and the adapter maps an `ask` to a `deny` (A3 of
  Section 13.1.1). The nested run streams its tool calls and its final message as
  `SubagentEvent`s with the id of the `Agent` call. The output of the call gives the last event,
  with `done`. The cost of the nested model calls goes through the model proxy with the run
  token, so the budgets of the run apply.
- **Approve and reject.** A resume loads the `RunState` and calls `approve()` or
  `reject()` with a fixed text and the reason of the user. The SDK asks `needs_approval` only
  for a call with no decision, so the approved call does not go to the gate again. An approval
  id is used once. A new run while an approval is open gives the paused call an output
  (`The call did not run: the run ended before it.`), because the Responses API needs an output for each call.
- **Compaction.** The `Compaction` capability turns on the server-side compaction of the
  Responses API. After a run, the adapter archives the items before the last `compaction`
  item, drops them from the store, and yields `Compacted` with the trigger `auto`. A compact
  request (`RunRequest.compact`, or the input `/compact [focus]`) calls
  `POST /responses/compact` through the proxy with all the items, archives them, replaces
  them with the whole answer, and yields `Compacted` with the trigger `manual`. The answer is
  the new context window: it can have user messages before its `compaction` item, and the
  next calls must get all of it. `runtime_state.window` is the length of the answer. The
  adapter uses a `Compaction` capability that does not cut the context at a `compaction` item
  in the window. A server-side `compaction` item after the window cuts the context, and the
  window goes back to 0. A compact request with no
  earlier state fails with `invalid_request`. Archive entries come from the message, function
  call, and function call output items. Reasoning and compaction items have no readable text,
  so they stay out.
- **Usage.** Token counts come from the SDK usage. The SDK counts cached tokens in
  `input_tokens`, so the adapter moves them to `cache_read_tokens`, as the proxy does. The
  cost stays zero: the model proxy record is the source of truth for cost.
- **Limits.** `max_turns` maps to the SDK `max_turns` (200 when the budget has none).
  `max_wall_time_s` is a timer in the adapter, and `runtime_state` keeps the wall time that
  the run used before a pause. The model proxy enforces `max_tokens` and `max_cost_usd`
  (`402` becomes `budget_exceeded`).
- **Cancel.** `cancel` cancels the SDK stream at once. The stream ends with
  `RunFailed(cancelled)` within `cancel_grace_s`.
- **Errors.** Failure messages are fixed text. A model HTTP error gives `model_error` with the
  status, retryable for 408, 409, 429, 500, 502, 503, 504, and 529.
- **Runner.** `build_runtime` builds `OpenAIRuntime` when the job has `session_url` and the
  env has `SAMA_MODEL_URL`. Else the job fails with `invalid_request`. The orchestrator sets
  `session_url` to `<tools_url>/session-items` (`SandboxSettings.session_url`). The control
  plane has one `SandboxedRuntime` for each kind (`for_kind`). The two share the sandboxes and
  the run state, so a cancel or an approval finds the run in either one.

Versions at the time of writing: `claude-agent-sdk==0.2.184` and `openai-agents==0.25.0`.
Each adapter pins its SDK version in its own dependency group (`runtime-claude`,
`runtime-openai`). Exact option names are checked again when each adapter is built.

### 5.4 Choosing a runtime

- Each agent profile has a default runtime. The org default is `claude_agent_sdk`. An admin
  can switch the runtime of a profile to `openai_agents_sdk` in the profile editor
  (Section 11.4).
- A session keeps one runtime for its life (prompt cache rule). A different runtime means a
  new session or a delegated child run.
- Use the OpenAI Agents SDK when a task needs an OpenAI model, OpenAI hosted tools, or
  OpenAI-native patterns such as handoffs.
- Since M7.2, the runtimes `claude_code` and `codex` send each message of a session to the
  Claude Code CLI or the Codex CLI in the session sandbox (Section 15.4). They are not adapters
  of an SDK in the runner: the control plane drives the CLI. A profile can choose them, and a
  session can switch to them for a time (the direct mode, `/claude` or `/codex`, until `/sama`).
  One message can go to a coding agent with `/claude <task>` or `/codex <task>`. These are the
  only exceptions to "one runtime for the life of a session": the frozen config of the session
  keeps its runtime, and the coding agent keeps its own CLI session in the sandbox.

### 5.5 Conformance suite

Both adapters must pass one shared test suite before a profile can use them:

- Streams text and ends with exactly one terminal event.
- Calls a sama MCP tool and gets the result.
- Turns a gated tool call into `ApprovalRequired`, then resumes correctly on approve and on
  reject.
- Resumes a session after the sandbox paused and resumed.
- Honours `cancel()` within 10 seconds.
- Loads a skill and follows it.
- Reports token usage that matches the model proxy record.
- Makes no network call outside the sandbox egress allowlist.

CI runs the suite against a scripted model. A nightly job runs a small live suite with real
models and a low budget.

#### Suite design (M1.5)

The suite is `tests/conformance/`. Each test runs once per **target**:

| Target | Runtime | Model | Runs |
|---|---|---|---|
| `fake` | `FakeRuntime` with one script per scenario | none | CI |
| `claude-scripted` | `ClaudeRuntime` and the real Claude CLI | scripted Messages API | CI |
| `claude-live` | `ClaudeRuntime` and the real Claude CLI | `claude-haiku-4-5` | nightly, `-m live` |
| `openai-scripted` | `OpenAIRuntime` in the test process, with `MemorySessionItems` | scripted Responses API | CI |
| `openai-runner` | `OpenAIRuntime` in the runner, through the model proxy and `POST /session-items` | scripted Responses API | CI |

- **Scripted model.** A small local server copies the Anthropic Messages API, also its
  streaming events. The CLI uses it through `ANTHROPIC_BASE_URL`, the same way as the model
  proxy (D6). Its rules answer on the last user text: `CALL <tool> <json>` calls a tool, a
  tool result gives `tool result: ...`, `SLOW n` streams slowly, and so on. So CI tests the
  real CLI, its hooks, its MCP client, and its limits at no cost.
- **Tool server.** A real streamable HTTP MCP server with a bearer token, like the sama MCP
  server. `lookup` is allowed. `send_note` is gated. The suite also gates the native `Bash`
  tool, because the CLI can send an approved native call through `can_use_tool` and an
  approved MCP call through the hook (Section 5.3).
- **Scenarios.** A target turns each scenario into input text. The tests check only contract
  events and the records of the tool server, so every target passes the same tests.
- **Covered now:** text streaming and one terminal event, session resume from
  `runtime_state`, an MCP tool call, approve and reject for an MCP tool and a native tool, an
  approval used once, cancel within 10 seconds, the `max_turns` budget stop, and a
  `Compacted` event.
- **Model proxy.** The `claude-runner` and `claude-docker` targets send their model calls
  through the model proxy (Section 16.6) to the scripted model. Two tests use the proxy
  record: the tokens of a run match the record, and the proxy stops a run at `max_tokens`
  with `402`, so the run ends with `budget_exceeded`.
- **Later items:** sandbox pause and resume (M1.6), skills (M2.2), and egress (M1.6). Each
  item adds its test to this suite.
- **Clean CLI config.** Each Claude target uses a new `CLAUDE_CONFIG_DIR`, so skills, memory,
  and settings of the host do not reach the run.
- **Live cost cap.** All live tests in one pytest process share one cap
  (`tests/live_budget.py`, `SAMA_LIVE_MAX_COST_USD`, default USD 0.50). Each live run gets the
  rest of the cap as its budget, and a test fails before it calls the model when the cap is
  used up. The CLI checks the budget after each model turn, so the total can pass the cap by
  one turn.
- **New adapters.** A new runtime adds a target and must pass the suite. M3.6 added
  `openai-scripted` and `openai-runner`. The scripted Responses API
  (`tests/conformance/scripted_openai.py`) uses the same rules as the scripted Messages API.

---

## 6. Prompt assembly and context management

### 6.1 Prompt tiers

Sama builds the prompt prefix once per session, in three tiers, so that the longest part is
stable and cacheable (Hermes `agent/system_prompt.py`):

| Tier | Content | Changes |
|---|---|---|
| Stable | Sama identity and tool-use guidance, the agent profile SOUL, pinned skills | Only when the profile changes, from the next session |
| Context | Context files: workspace `AGENTS.md` / `CLAUDE.md` chain from cloned repos, profile instructions, channel formatting hints | Per session |
| Volatile | Skill index (name and description), memory snapshot, user profile block, date and time zone, environment hints | Per session |

Rules:

- Nothing in the prefix changes during a session. Compaction is the only exception.
- New skills or memory writes during a session appear as a one-line note on the next user
  message, not as a prompt change.
- Context files are capped (20,000 characters each, as in Hermes) and scanned for prompt
  injection patterns before use.

**Code (M1.11).** The package `src/sama/prompt/` builds the `RunConfig`.

- `render_prefix()` puts the parts in this order: the sama system text (`system`), the
  SOUL, the context files, the skill index, the memory snapshot, and the environment text
  (`environment`).
- `PromptAssembler(engine)` is the `ConfigBuilder` of the worker. It locks the session row.
  If `sessions.run_config` is set, it returns that config. If not, it builds the config and
  stores it with its fingerprint in `sessions.run_config` and `sessions.config_fingerprint`
  (migration `0006`). So all runs of a session use the same prefix, also when two runs start
  at the same time. A change to the profile SOUL, model, or runtime, or to a context file,
  applies from the next session.
- Since M6.1, the assembler also writes `sessions.profile_snapshot` when it freezes the config
  (migration `0034`): the toolsets, the group toolsets, and the policy rules of the profile at
  that time. The gate uses it (Section 16.1), so a new toolset or a looser profile rule applies
  from the next session. A session from before M6.1 has no snapshot and uses the live values.
- `RunQueue.enqueue` takes the runtime from the frozen config of the session, if there is
  one. The claim of a chat run that starts sets the runtime again, from the frozen config or
  else from the profile, because the profile can switch runtime while the run waits
  (Section 11.4). So a session does not change its runtime. The assembler fails a run whose
  runtime is not the session runtime.
- The environment text has the session start time in UTC (`sessions.created_at`) and the
  audience. The time does not change during the session, so the prefix stays stable.
- Context files come from the table `context_files`. A file has no workspace (the whole org)
  or one workspace, and no profile (all profiles) or one profile. The order is: org files,
  workspace files, profile files, profile files of the workspace, and by path in each group.
  The prompt gets at most 20 files (`MAX_CONTEXT_FILES`). The table stores at most 100,000
  characters per file. Files of different scopes can have the same path. The first file
  keeps the path, and a later file gets its scope after the path, for example
  `AGENTS.md (workspace)`.
- RLS on `context_files`: a member reads a file if the member can see its workspace (if it
  has one) and its profile (if it has one). So a profile file without a workspace stays
  hidden when its profile is in a workspace that the member cannot see.
- `sama.prompt.scan` checks the path and the full text for these patterns: instructions that
  override earlier instructions, a new identity, text that hides things from the user,
  instructions in HTML comments, invisible Unicode characters (zero-width, bidi, and tag
  characters), commands that send secrets out (`curl` or `wget` with a key variable, `cat` of
  `.env` or `~/.ssh`), and fake role markers. If a pattern matches, the prompt gets a note in
  place of the file. Sama logs a warning and writes the audit action `context_file.blocked`
  with the finding ids. All checks take linear time, so a file of maximum size cannot block
  the worker. The scan is a filter for common attacks. It is not a full defence, so
  the system text also tells the model that file text is data.
- A file longer than 20,000 characters keeps its head (70%) and its tail (30%). A marker
  `[... N characters cut ...]` replaces the middle.
- Not in M1.11: channel formatting hints (M1.13), pinned skills, and the user profile block
  (later milestones). The `RunConfig` fields for them exist, and they are empty.
  Since M2.1 the memory snapshot is full: the `agent`, `workspace`, and (in a private session)
  `user` notes of the run (Section 8.1). The user profile block is the `user` part of it.
- The skill index (M2.2) is filled when the session starts: name and description of each
  skill that the profile and the session can use (Section 9.2). It is part of the frozen
  prefix, so a skill that the agent writes during the session is not in the index. The agent
  can find it with `skills_list`.
- Repo `AGENTS.md` and `CLAUDE.md` files do not go into the prefix. The prefix stays the same
  for the whole session, and a repo is cloned during a run. Since M1.16 the `repo_clone`
  result holds these files (Section 15.1).

### 6.2 Context management

| Mechanism | How |
|---|---|
| Compaction | Native in both SDKs (Section 5.3). Before compaction, sama archives the full transcript in a child session of the live session (session lineage) and records the compaction. `/compact` asks the runtime to compact now (Section 6.3, M2.6). |
| Tool output limits | Sama tools return at most 50 KB (head and tail kept). Larger output is written to a file in the sandbox, and the tool returns a preview plus the path (Hermes "spill", Section 16.7). |
| Tool search | Rare tools (most integrations, cron admin) are deferred. The media tools `image_generate` and `text_to_speech` are direct tools of the `media` toolset (M4.9, Section 11.8). The model sees `tool_search`, `tool_describe`, and `tool_call` instead of their schemas. A deferred call passes the same gate as a direct call (Sections 6.3 and 16.1, M2.6). |
| Progressive skills | Only the skill index is in the prompt. The agent loads a full skill when it needs it. |
| Context pins | A user pins a text or a message. Sama puts the pins in front of the next message, and again after each compaction (Section 6.3, M2.6). Files are not pinnable yet. |
| `@` references | `@file`, `@url`, and `@session` in a message attach that content (M4.9, Section 6.4). |
| Budgets | Per run: max turns, max tokens, max cost, and wall time. Near the limit, sama tells the agent to wrap up. |

### 6.3 Context management (M2.6)

M2.6 adds five parts. The first four keep a long session usable. The fifth keeps the tool
list short.

| Part | Where | Section |
|---|---|---|
| Compaction record and transcript archive | `sama.context.compaction`, `sama.runtime.archive`, `POST /archive` of the tool server | below |
| Session lineage | `sessions.parent_session_id` and `sessions.lineage`, `sama.context.lineage` | below |
| `/compact` | `RunQueue.enqueue_compact`, the API, Telegram, Slack | below |
| Context pins | `context_pins`, `sama.context.pins` | below |
| Deferred tools | `sama.tools.deferred`, `tool_search`, `tool_describe`, `tool_call` | below, and Sections 10.3 and 16.1 |

**Contract changes.** Three additions in `src/sama/runtime/contract.py`. All are optional, so
an adapter that does not know them keeps working.

- `Capabilities.manual_compaction`: the runtime compacts when the request has `compact`.
- `Capabilities.compaction_archive`: the runtime gives its transcript to an `Archiver` before
  it compacts.
- `RunRequest.compact` (`CompactRequest(focus)`): compact the session now. The request has
  no user input. The runtime sends no answer. `Compacted.trigger` is `manual` or `auto`.

The worker fails a compact run with `INVALID_REQUEST` when the runtime has no
`manual_compaction`. So an adapter that does not support `/compact` gives a clear failure and
does not answer with a wrong text. The Claude adapter, the OpenAI adapter (M3.6), and the fake
runtime have the flag. The fake runtime has no archive.

**Compaction record.** A `Compacted` event is stored twice, in one transaction:

1. as the `run_events` row `compacted`, as before;
2. as a row of `session_compactions`: `trigger`, `tokens_before`, `tokens_after`, the run, the
   archive session, and the counts of the archive. `RunQueue.add_events` calls
   `sama.context.record_compacted`, so the event and the row exist together.

`record_compacted` also writes the audit row `context.compacted`, and it makes the runtime
forget the pins (see Pins). If no archive row is open for the run (an adapter without an
archive, or an archive that failed), it makes a row with no archive session.

**Transcript archive.** Before the Claude CLI compacts, it runs the `PreCompact` hook. The
hook input has `trigger` (`manual` or `auto`) and `transcript_path`.

1. The adapter reads the transcript file of the CLI and turns it into `ArchiveEntry` objects:
   user and assistant text (`role` and `text`), each tool call (`tool_name` and `tool_input`,
   the input as a **JSON value**), and each tool result as a `tool` entry. It skips thinking
   blocks and every line type that it does not know. The format of the file is not a public
   contract, so the parser never raises for a line that it does not understand
   (`sama.runtime.claude_transcript`).
2. The runner does not cut inside an entry. It has no redactor, and a cut before the
   redaction can split a secret into parts that the redactor does not know. So the parser and
   `bound_for_wire` leave **whole entries** out. The size of an entry is the bytes of its JSON
   text in UTF-8 (`entry_wire_bytes`). The model of the entry, the parser, and
   `bound_for_wire` use this one definition. An entry over 1 MB stays out, and a marker
   (`system` entry) takes its place. The parser checks each **content block**, so one large
   block does not remove the other blocks of the same message, and each large block counts
   once in `omitted`. A tool name over 200 characters is not cut either: the entry of that tool
   call stays out whole, with the same marker (the marker has no part of the name). A transcript over 5,000 entries or 6 MB keeps its first
   30 % and its last 70 %, with one marker for the middle. The request has `omitted`, the
   number of entries that stay out, so the row can show `archive_truncated`.
3. `RemoteArchiver` posts the entries to `POST /archive` of the sama tool server. It sends
   the run token as the bearer, like every other request from the sandbox. The runner has no
   database access (Section 16.2).
4. The tool server checks the run **before it reads the body** (`ContextArchive.authorize`),
   limits the bodies that it holds at one time (Section 16.7), and calls
   `ContextArchive.archive`. The archive needs a run in the status `running`, a user of the
   run that still sees the session (the live check of Section 5.1, `load_authority`), and a
   session in the status `active`. It checks this again in the transaction that writes. It
   redacts each entry **whole**: the text of an entry, and the input of a tool call as a JSON
   value (every string and key), so a secret with quotes, backslashes, or new lines still
   matches. Only then it makes the text (`[tool call NAME] {json}`) and cuts it. It makes an
   **archive session** and stores the entries as its messages. It makes a
   `session_compactions` row that points to the archive session (the row is open, with no
   token counts). It writes the audit row `context.archived`.
5. The CLI compacts. The `compact_boundary` message becomes `Compacted`. The event completes
   the open row (the token counts and `completed_at`).

Limits of the request (the runner and the model refuse more): 5,000 entries, 1 MB for one
entry (bytes of the JSON text in UTF-8, for the whole entry and not for each field), 8 MB for
the body. The list is counted before the model builds any entry (the check of the list length
comes after the items are built, and 200,000 small entries need hundreds of MB). The count is
a first parse of the body with `json.loads` in which every JSON object becomes `0`
(`object_pairs_hook`), so the list of items costs one pointer for each item and the count does
not depend on how the body spells its keys (`"role"` and `"\u0072ole"` are the same key).
Only a body with at most 5,000 entries goes to the model. A body that is nested too deeply is
refused too. Limits of the store, after
the redaction: 20,000 characters for one entry (the start 70 % and the end 30 % stay), 4,000
entries, 2,000,000 characters for one archive (the first 30 % and the last 70 % stay, with a
`system` entry that says how many entries are not in the archive), 60 seconds for the call, and
10 archives for one run. The row has `archive_truncated` when anything was cut, in the runner
or in the store.

**A failed compaction.** The CLI can answer `/compact` with a normal result that has an error
text (for example when the summary call fails with HTTP 400). There is no `compact_boundary`
then, and the context is not smaller. A compact run needs a boundary to complete: the adapter
ends it with `RunFailed` (`model_error`, "the compaction did not finish: the runtime made no
compaction boundary"). The archive row that the hook made stays open (`completed_at` is null).
It is the record of the attempt.

**Tokens of a compaction.** For `/compact` the CLI reports zero tokens in `ResultMessage.usage`,
but `model_usage` and the cost of the SDK session grow. The adapter keeps the cumulative tokens
of `model_usage` in its `runtime_state` (`sdk_tokens`). If a result has no tokens in `usage`,
the run counts the difference of `model_usage`. A normal turn has its tokens in `usage`, so it
is not counted two times. A state without `sdk_tokens` (an older state) has no known baseline,
so the first run counts nothing from `model_usage` and then saves the baseline.

The baseline (`sdk_tokens`, and the cost `sdk_cost_usd`) must also move on when a run **fails**
after it paid for model calls. A failed run has those tokens and that cost in its own usage.
The CLI counts them in the cumulative values of the SDK session too. If the next run started
from the baseline of the last completed run, it would count that work a second time (the
baseline 100, a failed run to 120, the next run to 150 would count 50 and not 30). So
`RunFailed` has an optional `runtime_state`, and the queue stores it in `runs.runtime_state` of
the failed run (`RunQueue.fail`). The next run of the session starts from the **newest** chat
run that saved a state, whether it is `completed`, `failed`, or `cancelled`. The state of a
failed run is not a state to resume the failed conversation: the adapter writes the state of
the last completed run (its `session_id`) with only the counts moved on
(`_Translator.failure_state`). A paused run keeps that session in `base_session_id`, so a run
that fails after a resume does the same. The adapter writes no state when there is no base
(the first run of a session, which starts a new SDK session next time), or when the run
continued another SDK session than the base: the count is not for the base session then, and
the next run uses the last completed state. A run that the worker stops after the cancel grace
period (the CLI did not answer) has no result, so it keeps no count (A21).

**An archive that fails does not stop the compaction.** A session that is full must still be
able to make room. The hook catches every error, logs only the error type, and returns an empty
output. The compaction then runs, and its row has no archive session. This is a choice: the
alternative is a hook that blocks the compaction, and then a failed archive makes a full session
useless.

**Session lineage.** A session can have a parent. `sessions.lineage` says how it came from the
parent: `compaction` (an archive), `branch` (`/new` and `/model` in a chat), `retry` (reserved),
`delegation` (M2.7). The archive session is a **child** of the live session. The live session
keeps its id, its binding, its pins, and its runtime state, so the compaction does not move any
chat to a new session. The archive session has the status `archived`, so it is not in the
session list of the API, and it cannot get new runs or pins.

- The archive has the audience, the owner, the workspace, and the profile of the live session.
  So the visibility rule (Section 11.3, `sama_private.can_read_session`) gives the same people
  the same access to both. An archive of a private session is private to its owner. The embed
  worker skips the archive messages (`embedded_at` is set when they are written), because the
  live session has the same user and assistant text.
- `session_search` does not list archives, because they repeat the live session. A user who
  needs a detail that the summary lost reads the archive: `session_read` of the live session
  names its archives (the ones that the run may see), and `session_read` of an archive shows the
  `tool` and `system` entries too. The header of a page and of a hit says
  `compaction of session: <id>` or `branch of session: <id>`.
- `GET /sessions/{id}/lineage` gives the ancestors and the children of a session. It reads as
  the user, so a linked session that the user cannot read is not in the answer
  (`sama.context.lineage`).

**`/compact`.** The user asks the runtime to compact the session now.

- API: `POST /sessions/{id}/compact` with an optional `focus` (at most 6,000 characters). It
  answers `202` with `run_id`. `409` has the reason: the session is busy, the session is not
  active, or it has nothing to compact. `429` is the message rate limit.
  `GET /sessions/{id}/compactions` lists the compactions of a session.
- Telegram: `/compact [focus]`. Slack: `/sama compact [focus]` in a direct message. The gateway
  shows "Compacting the context" and, when the run ends, the token counts ("The context is
  compacted: 1000 tokens before and 500 tokens now."). Slack in a channel does not support it,
  because the answer of a slash command is a message that only the sender sees, and a channel
  thread has no single session.
- The run is a `chat` run with `runs.compact = true`. `runs.compact_focus` holds the focus,
  redacted. A database check makes a compact run a chat run and allows a focus only on a
  compact run. The worker sends `RunRequest.compact` and no input. It reuses the claim, lease,
  event, follow, and cancel code of a chat run, so a compact run can be stopped with `/stop`.
- **Lock order.** Every enqueue takes the org rate lock first (an advisory lock), and the
  session row after it. A run insert needs a share lock on the session row, so `enqueue` held
  the rate lock and waited for the row, while `enqueue_compact` held the row and waited for the
  rate lock: a deadlock. Both paths use the same order now. The creation of a background
  review run (`review.trigger.after_completed_run`, M2.4) is the one exception, and it is safe:
  it runs in the transaction that completes a run, it locks the session row (the counters
  update) and then inserts the review run, and it **never takes the rate lock and never waits
  for it**. A lock cycle needs a transaction that holds one lock and waits for the other
  while another transaction does the reverse. The review path only waits for row locks, and
  the enqueue paths take the rate lock before any row, so no path holds the session row and
  waits for the rate lock. A review run is no user message, so it does not count for the rate
  limit, and it must not wait for the lock that puts the enqueues of an org in a sequence (that
  would slow the completion of every run). Tests: `test_a_review_trigger_never_waits_for_the_org_rate_lock`
  and `test_enqueues_and_review_triggers_of_one_session_do_not_deadlock`.
- **Busy rule.** `enqueue_compact` locks the session row and refuses when any run of the
  session is open (`queued`, `running`, or `paused`). A message uses the queue rule: it waits
  behind the open run. A compaction must not change the context under a run, and a run that
  waits for an approval has the context in the CLI. So `/compact` does not wait. It says "busy".
  A message that arrives while the compact run is open waits behind it, as it does behind any
  run.
- **Nothing to compact.** The session needs a completed run with a runtime state. A session
  with only a queued message has no CLI session yet.
- A compact run counts as one message for the rate limit of the user and of the org. It uses a
  model call (the summary), and it is the same abuse path as a message.
- A compact run writes no message and starts no review or title job. It is not a user turn, so
  the history of the session reads the same as before.
- **The focus.** The text that goes to the CLI is the focus that the user wrote, then the pins
  of the session: "Keep these pinned notes in the summary, word for word". So the summary keeps
  them. The whole text is cut to 6,000 characters. The Claude adapter sends `/compact <text>`.
  This is a slash command of the CLI and the text is its custom instruction.

**Context pins.** A pin is a short text that the user wants the agent to keep in mind. It stays
true until the user removes it.

- Table `context_pins` (migration `0018`): `org_id`, `session_id`, `created_by_user_id`, `text`,
  `source` (`user` or `message`), `injected_at`, `injected_run_id`. RLS follows the session.
- Limits for each session: 10 pins, 2,000 characters for one pin, 8,000 characters in all. The
  store takes an advisory lock for the session, so two adds at the same time cannot both take
  the last place. The text is redacted when it is stored, and again when it is injected.
- API: `GET`, `POST` (`text` or `message_id`, not both), and `DELETE` on
  `/sessions/{id}/pins`. A message pin copies the text of a user or assistant message of the
  same session. Telegram: `/pin <text>`, `/pins`, `/unpin <number>`. Slack, in a direct
  message: `/sama pin TEXT`, `/sama pins`, `/sama unpin NUMBER`. Any user who can read the
  session can add and remove pins, as any user can send a message. An archive session cannot
  have pins. The list (API, Telegram, Slack) redacts the **whole** text again with the secrets
  that the redactor knows now, and then the caller folds and cuts it. So a secret that the
  system learned after the pin was made is not shown, and a cut cannot split it.
- **Injection.** The runtime has no native pin. The control plane puts the pins that the
  runtime has not seen in front of the next user message, in a `<pinned_context>` block that
  says the text is notes from the user and not system instructions. The stored message is the
  text of the person. The block is only in the input of the run. A claim of the same run again
  (after a lost lease) gets the same pins again. An approval resume gets none, because the CLI
  session already has them.
- **Reservation, not a mark.** The claim **reserves** the pins for the run (`injected_at` and
  `injected_run_id`). The reservation is final only while a runtime state that has the pins can
  be resumed. A pin is due again at the next claim when the run that got it did not keep it:
  the run failed or was cancelled (it saves no state), or it completed without a runtime
  state. A run that is `queued`, `running`, or `paused` holds its pins, and a run that
  completed with a state keeps them (the next run resumes that state). The rule is a query at
  claim time, so it holds for every way that a run can end (fail, cancel, reap, approval
  time-out) with no hook in each of them.
- **Survive compaction.** A compaction can summarise the pin text away. So `record_compacted`
  sets `injected_at` to null for all pins of the session, and the next message carries all of
  them again. A `/compact` also passes the pins in the focus, so the summary tries to keep
  them.

**Deferred tools.** Each tool schema is in every model call. A product or a connector can
register a tool in the `DeferredCatalog` (`sama.tools.deferred`) instead of listing it. The
model finds it with three sama tools (toolset `tool_search`, Section 10.2).

- `tool_search(query, limit)` lists the deferred tools that match some words, best first
  (name 3 points, tag 2, description 1). `tool_describe(name)` gives the description and the
  input schema. `tool_call(name, arguments)` runs the tool.
- Both search tools show only the tools that the run can use: the tools of its toolsets, minus
  the tools that a `deny` rule blocks (`GateService.visible_tools`, so a shared run uses its
  group set). A hidden tool and an unknown tool give the same error, so a run does not learn
  that a tool exists. **Fail closed:** a gate that has no `visible_tools` (and a server with no
  gate) shows no deferred tool. The search is empty and `tool_describe` gives the same error as
  for an unknown tool. `tool_call` still goes through the gate.
- **A deferred call never bypasses the gate.** Section 16.1 has the rule: the gate judges a
  `tool_call` as a call of the tool that it names.
- The tool server shows the three tools only when the catalog has a tool when the app starts.
  No product registers a deferred tool in M2.6. M3.4 (connections) is the first user: the
  connector tools of the catalog are deferred tools in the toolset `integrations`, and the
  search shows only the tools of a connection that the run can use (Section 10.3). The
  catalog is the only extension point: a tool registers a name, a toolset, a description, a
  JSON schema of the arguments, and an async handler that gets the run token claims. The
  server checks only that the required arguments are present. The handler validates the rest.
  A tool name cannot be the name of a sama tool or of a meta tool, and it cannot start with
  `mcp__sama__`, so the gate and the catalog always read the same name.
- Claude has its own native `ToolSearch` tool. It stays allowed in every run as a loader
  (Section 16.1) and the adapter keeps `ENABLE_TOOL_SEARCH=false` (Section 5.3). The sama tools
  are separate, and they work with every runtime.

**Assumptions.** These are choices where the owner did not decide.

| # | Assumption |
|---|---|
| A1 | The archive is a child session of the live session, with lineage `compaction`. The live session is not replaced. The alternative (a new live session after each compaction) moves the chat binding, the pins, and the runtime state. |
| A2 | The archive belongs to the runtime adapter: the adapter knows its transcript format, and the control plane stores plain entries. The OpenAI adapter (M3.6) sets `compaction_archive` and gives entries to the same `Archiver`: before a manual compaction it archives the items of the context, and after a server-side compaction it archives the items before the compaction item (Section 5.3). |
| A3 | A failed archive does not block compaction. |
| A4 | `/compact` refuses a busy session. A message waits in the queue, but a compaction must not wait behind a run that waits for a human. |
| A5 | A compact run is a chat run with a flag, not a new run kind. It reuses the claim, lease, event, follow, and rate code. A new kind would need new code in each of them. |
| A6 | The archive is redacted and cut in the control plane, not in the runner, because the redactor knows the vault secrets of the org. The runner never cuts inside an entry: it leaves whole entries out. A tool input stays a JSON value to the redaction. |
| A7 | Archives are not searched. They repeat the live session. They are readable by id. |
| A8 | Pins are session notes that the control plane injects. No runtime has a native pin, so the injection is the same for every runtime. |
| A9 | Any user who can read a session can pin and unpin, as any user can send a message. |
| A10 | `compact` and pins in Slack work in direct messages only. |
| A11 | The default profile gets the toolset `tool_search`. The default group set does not, because no deferred tool is registered yet and a shared session should opt in. Migration `0018` changes only a profile that has the exact old default list. |
| A12 | `tool_search` and `tool_describe` are allowed by default (they only read). `tool_call` has no default of its own: the called tool has its own rule or its default (`ask` for an unknown tool). |
| A14 | The archive endpoint has hard limits at the model (5,000 entries, 1 MB for an entry, 8 MB for the body), a check of the run before the body is read, and 2 archive bodies at one time (a third gets `429`; the hook is fail-open, so the cost is one archive). The entries are counted before the model builds them (a first parse that turns every object into `0`), and the size limit of an entry is for the whole entry in UTF-8 bytes. The count runs on the event loop and parses the body two times (at most 8 MB, 0.1 s in a test with 500,000 small entries). |
| A15 | An archive needs the live authority of the run (the user still sees the session), as a tool call does. A user who loses access while a run runs cannot write an archive. The compaction itself is not stopped. |
| A16 | A pin is due again unless the run that got it is open or completed with a runtime state. A failed run does not save a state, so the pin goes again. A run that fails after the CLI saved its own session can give a pin two times to that session. The cost is a repeated note, and this is the safe side. |
| A17 | A compact run that has no compaction boundary fails. The run ends as `failed` with `model_error`. The archive that the hook made stays as an open row. |
| A18 | `model_usage` of the CLI is cumulative for the SDK session across processes, as the cost is. This is checked with the real CLI and the scripted model, and not with a live model. |
| A19 | The pin list is redacted again when it is shown. The text in the table can still hold a secret that the redactor learned after the pin was made, until the pin is removed. |
| A20 | The tool search fails closed without a visibility source. A tool server in a test or a product that has no gate shows no deferred tool. |
| A21 | The accounting baseline (`sdk_tokens`, `sdk_cost_usd`) of a failed or cancelled run is kept in `runs.runtime_state` of that run (`RunFailed.runtime_state`, optional, so the contract stays compatible), and the next run starts from the newest chat run that has a state. The state of a failed run has the session of the last completed run, so the conversation is never the failed one. A run that the worker stops after the cancel grace period has no result and keeps no baseline: the next run can count that work again (only its cost and the tokens of a compaction; a normal turn counts its own tokens). |
| A22 | The tool name of a transcript entry is never cut in the runner. A name over 200 characters makes the tool-call entry stay out whole (`omitted` counts it). A real tool name is much shorter, so the cost is a marker for a name that is not real. |
| A23 | Background review creation does not take the org rate lock. It never waits for it, so it cannot close a lock cycle with the enqueue paths, and it is no user message (it does not count for the rate limit). Taking the lock would put the completion of every run of an org in a sequence. |
| A13 | The PreCompact hook and the compact boundary behave as the CLI of `claude-agent-sdk==0.2.184` documents. They are not checked against a live model in M2.6 (no key in the test environment). A live check is in the nightly conformance job when a key exists. |

**Limits and gaps.**

- Pins are not injected again in the middle of a run after an automatic compaction. The next
  message carries them. A pin is also not a file: only text and messages can be pinned.
- An agent cannot pin by itself. A pin is an action of a user. An agent tool for it needs the
  review rule of Section 9.4, and it is later work.
- A user who unpins a text keeps it in the runtime transcript until the next compaction.
- An archive row has no foreign key cascade to the live session: an archive session stays
  until the org is deleted.
- The archive is cut for very long sessions. The row shows `archive_truncated`.
- No live test of `/compact` and of the hook ran against the real Claude API.
- No product registers a deferred tool. The code path is covered by tests with fake tools.

### 6.4 `@` references (M4.9)

Code: `src/sama/orchestrator/references.py`, called by the worker (`Worker._expand`).

A user can name content in a chat message: `@url:https://example.com/page`,
`@file:docs/plan.md`, or `@session:<session id>`. The worker reads each reference when the run
starts and attaches the content to the input of the run. The agent does not need a tool call
for it.

- **Syntax.** A reference starts after a space or at the start of the text. The form is
  `@file:<path>`, `@url:<url>`, or `@session:<id>`. Sama removes a final `.`, `,`, `;`, `:`,
  `!`, `?`, a closing bracket, or a quote from the target. A repeated reference counts once.
  Text in an `<untrusted_content>` block (for example a page from the Chrome extension,
  Section 14.2.1) has no references: content from outside cannot make sama read a file, a
  session, or a URL.
- **Where.** Only a new `chat` run gets the attachment. A resumed run and a compact run do
  not. The stored message stays as the user wrote it, so the transcript and the search show
  the original text. Only the input of the run changes.
- **Format.** The input is the text of the user, then one note, then one block for each
  reference. The note says that the blocks are data and not instructions. A URL and a file
  are in `<untrusted_content>` blocks (Section 16.5). A reference that sama cannot read gets
  one line in `Not read:` with a short reason.
- **Redaction.** Sama redacts the text of each reference (Section 16.3) before it cuts the
  text to the limit. So a key in a file, a page, or a session does not go to the model.
- **`@url`.** Sama gets the page with `web_extract` code: the outbound client and its URL
  policy (Section 16.4). Only `http` and `https` are accepted.
- **`@file`.** Sama reads the file from the work folder of the session sandbox. The path is
  relative to the work folder. A path that starts with `/` or `~`, or that has `..` or `\`,
  is refused. Sama then resolves the links of the path in the sandbox (`realpath -e`) and
  refuses a path that ends outside the work folder. It reads the resolved path. A link can
  change between the check and the read. That is accepted: the sandbox belongs to the session
  and has no secrets (D6). The file must be UTF-8 text of at most 64 KB. If the session has no
  sandbox yet, the reference is not read.
- **`@session`.** Sama reads the session with the rules of `session_read` (Section 7.1). A
  private session reads the sessions that its user can see. A shared session reads only the
  shared sessions of its workspace, never a private session.
- **Limits.** At most 5 references in a message. Each block is cut at 8,000 characters. All
  reads run at the same time and share a limit of 20 seconds. A read that is not done in time
  gets `did not finish in time`. An error of one read does not stop the others or the run.

---

## 7. Sessions

- Tables: `sessions`, `messages`, `tool_calls`, `runs`, `run_events`, `session_bindings`.
  Every row has `org_id`.
- A **session binding** maps a channel thread to a session: a Telegram chat, a Slack thread,
  a web chat, or an extension tab. `/new` starts a new session in the same binding.
- **Resume** uses the runtime's resume (Section 5.3) and the session sandbox.
- **Parallel runs (M7.1).** A session can have more than one open run (Section 4.3). Each run
  sees the messages that were stored before it started. The reply that ends first is stored
  first. A side run starts a new runtime session with the transcript in its input, and the next
  main run gets the answers of the side runs in a catch-up block.
- **Lineage**: compaction, `/branch`, `/retry`, and delegation create child sessions linked to
  the parent. Nothing is overwritten. Since M2.6 a compaction makes an archive session
  (`lineage = compaction`, the status `archived`) that holds the transcript from before the
  compaction (Section 6.3). `/new` after `/model` makes a `branch`. Since M4.6 `delegate_task`
  makes a `delegation` session. Its `parent_run_id` names the parent run, and the key
  `(org_id, parent_session_id, parent_run_id)` proves that the run is a run of the parent
  session. Only a `delegation` session has a `parent_run_id` (Section 13.2). `retry` comes
  later.
- **Cross-channel continuity**: a user can continue a session from another channel with
  `/resume <id>` or a session link.
- **Search**: `session_search` uses Postgres full-text search plus pgvector embeddings,
  filtered to sessions that the user can see. It returns redacted snippets and makes no LLM
  call (Hermes design). `session_read` then loads the messages of one session. Section 7.1
  has the design (M2.5).
- **Titles and summaries**: a small model writes a title after the first turn and a summary
  when the session goes idle. Summaries feed session search and the background review.
  Summaries are not in M2.4: the review reads the transcript (Section 9.4).
- **Retention**: per-org setting (`orgs.retention_days`) inside the limits of the plan.
  Default: keep everything. The retention sweep deletes the old sessions with their rows,
  sandboxes, and Langfuse traces (M6.8, Section 17.4).
- **Runtime state**: the canonical transcript is `messages`. The runtime keeps its own state
  as a blob: the Claude adapter keeps the SDK session id in `runs.runtime_state`. The OpenAI
  adapter keeps its conversation items in `runtime_session_items`, through
  `POST /session-items` of the tool server (M3.6, Section 5.3). A session delete removes these
  rows (cascade).

### 7.1 Session search (M2.5)

The agent uses `session_search` when the user refers to earlier work, and `session_read` to
load the messages of a session that the search found. Neither tool calls an LLM.

**Index.** Migration `0014` makes the `vector` extension (pgvector) and changes two tables
(Section 17.1).

- `messages.search_vector` is a generated `tsvector` column with a GIN index. It covers the
  first 100,000 characters of the message text. It uses the `simple` configuration, so words
  stay as they are and the index works for every language. No stemming and no stop words.
- `message_embeddings` has one row for each embedded message: `embedding` (`vector(1536)`),
  `model`, and the keys of the message. A composite foreign key on `(org_id, session_id,
  message_id)` makes the row point to a message of its own session and org. An HNSW index
  with cosine distance covers `embedding`. RLS gives a member the rows of the sessions that
  the member can see (`sama_private.can_read_session`). No policy allows writes.
- `messages.embedded_at` is null until the embed worker has handled the message. A partial
  index on the null rows keeps the work queue small.

**Migration lock.** `search_vector` is a stored generated column. `ALTER TABLE ... ADD COLUMN`
with a stored expression rewrites the whole `messages` table, and it holds an `ACCESS
EXCLUSIVE` lock until the rewrite ends. The GIN index build adds to this time. A large
deployment must run migration `0014` in a maintenance window, with the control plane and the
tool servers stopped. A small database needs seconds.

**Embed worker.** `EmbedWorker` (`src/sama/search/embed.py`) is a loop of the control plane.
It runs only if an OpenAI key is available (the vault or `OPENAI_API_KEY`, as for voice notes
and the model proxy). It takes the oldest messages with no `embedded_at`, 64 at a time, and
calls the OpenAI embeddings endpoint (`text-embedding-3-small`, 1,536 dimensions) with the key
of the org. The input is the first 6,000 characters of the redacted text (see "Redaction
order"). The rows and `embedded_at` go in one transaction.

- A message with no text, or with the role `system` or `tool`, gets `embedded_at` and no
  embedding.
- An org without a key waits. The worker skips its messages for 10 minutes, so one org
  without a key does not block the others. With a key later, the worker embeds the old
  messages too, because they still have no `embedded_at`.
- A request that the provider refuses (a 4xx other than 401, 403, 408, and 429) is tried one
  message at a time. The message that fails gets `embedded_at` and no embedding.
- A network error, a 401, 403, 408, 429, or a 5xx puts only the org of the call on a backoff,
  5 seconds doubled for each failure up to 5 minutes. Nothing is marked. The query for
  pending messages skips an org that waits, and the worker goes on with the other orgs in
  the same pass. So the failing key of one org does not stop the embeddings of the others. A
  success clears the backoff of the org.

**Redaction order.** Secrets must not reach the embedding provider, the snippets, or the
agent. Redaction runs on the complete text first. A cut, a highlight, or a mark comes after
that, because each of them can split a secret into parts that the redactor does not know.

- `OpenAIEmbedder` is the only way out of the process for the text of a message or a query.
  It redacts each complete text, then cuts the redacted text to 6,000 characters. The worker
  gives it the complete texts. A message that was stored before the redactor learned a
  secret is redacted when it is embedded, not when it is stored. The search also redacts the
  query before it calls the embedder.
- A snippet is made from the redacted text. `ts_headline` runs on that text, and the cut to
  20,000 characters comes after the redaction. A search for a part of a secret still finds
  the message through the index, but the snippet shows `[REDACTED]` and not the secret. The
  last redaction of the output stays as a second guard.
- A vector that was stored before the redactor learned a secret still encodes that secret.
  It cannot give the text back, and nothing returns it to the agent.

**Search.** `session_search(query, limit)` runs one or two ranked lists. Each list gives
50 messages (roles `user` and `assistant`).

1. Full text: `websearch_to_tsquery('simple', query)`, ranked with `ts_rank_cd`.
2. Meaning: only if the org has embeddings and the control plane can embed the query. The
   query goes to the embedder (3 seconds at most). Messages rank by cosine distance, and a
   distance over 0.7 is not a match. Without a key, after a timeout, or after an error, this
   list is skipped and the result uses the full-text list alone. The tool does not fail.

Reciprocal rank fusion (`1 / (60 + rank)` for each list, added up) joins the lists. A session
ranks as its best message. The tool gives the best `limit` sessions (default 5, at most 10),
each with up to two snippets. The snippet is a `ts_headline` of about 40 words, with the
matching words between `<<` and `>>`. For a meaning match without a matching word, it is the
start of the message. A query of more than 400 characters is refused. An empty query lists the
latest sessions, with no snippet. The current session is not in the result.

**Read.** `session_read(session_id, offset, limit)` gives the `user` and `assistant`
messages of one session in order: 20 by default, at most 50. A message is cut to 4,000
characters, and a page to 20,000 characters. The result says which `offset` starts the next
page.

**Fresh grants.** The query embedding is a slow call (3 seconds at most). The search makes
the query vector first, and only then loads the grants of the user. All reads of the search
use this fresh rule. The last step loads the titles and messages of the chosen sessions with
the same rule, and not by id alone. So a membership that ends during the embedding call
removes the session from the result. The ids that the ranked lists chose are never an
authorization.

**Who sees what.** Every query has the org of the run and this rule (the rule of
Section 11.3, "Audience and principal", and `sama_private.session_visible`). The control
plane connects as the table owner, so RLS does not protect these queries. The rule in code
does.

| Run | Sessions that it sees |
|---|---|
| Private (the run has a user) | `shared` sessions and the own `private` sessions, in the workspaces that the user sees (all workspaces for an org admin). The user must be an org member. |
| Shared (no user) | `shared` sessions of its own workspace. It never sees a `private` session. |

`session_read` gives the error `session not found` for a session that the run may not see, for
a session of another org, for a session that does not exist, and for an id that is not a UUID.
So the tool does not show which sessions exist.

**Output.** The title, the snippets, and the messages are text that people and tools wrote.
They are redacted (Section 16.3) and put in an `<untrusted_content source="session_search">`
or `source="session_read"` block (Section 16.5).

**Limits and gaps.**

- The index covers the first 100,000 characters of a message. The embedding covers the first
  6,000.
- The HNSW index filters after the scan. With pgvector 0.8 or later, sama sets
  `hnsw.iterative_scan = relaxed_order` for the query, so a filter that removes many rows
  still gives enough rows. With an older pgvector, the meaning list can have fewer rows. The
  join with the session rule keeps the result correct in both cases.
- Embedding calls are not in `model_calls`, so the cost of embeddings is not in the budget
  of a run.
- The text of messages goes to OpenAI for the embedding, like the text of a model call.
  Without a key, nothing leaves the control plane.
- Changing the model or the dimension needs a new migration and a new `embedded_at` pass.
- Titles and summaries are not searched yet (M2.4 makes summaries).

---

## 8. Memory

Sama copies the Hermes memory model, because the small caps force curation and keep the
prompt cheap.

| Store | Scope | Cap | Content |
|---|---|---|---|
| `agent` notes | Agent profile in a workspace | 2,200 characters | Facts about the environment, conventions, lessons, completed work |
| `user` profile | One user (across profiles in the org) | 1,375 characters | Preferences, role, style, recurring needs |
| `workspace` notes | Workspace | 2,200 characters | Shared team facts. Writes need `admin` or an approval. |

- **Tool**: one `memory` tool with `target` (`agent`, `user`, `workspace`) and actions `add`,
  `patch` (replace a unique substring), and `remove`. Batch form with `operations`. No `read`
  action, because the content is in the prompt.
- **Over the cap**: the write fails and returns the current entries and the characters to
  free. The agent must consolidate in one batch. Memory never compacts itself.
- **Frozen snapshot**: sama renders memory into the volatile tier at session start.
- **History**: every write is a row in `memory_revisions` with session, run, and author.
  Users can see, edit, and revert memory in the web app.
- **Long-term recall**: session search (Section 7) covers what does not fit in memory. A
  memory provider interface allows an external store (for example Mem0) later. Postgres plus
  pgvector is the default.
- **Safety**: delegated children and webhook-triggered runs cannot write memory. Memory
  content is scanned for secrets before it is saved.

#### 8.1 Implementation (M2.1)

The code is in `src/sama/memory/`. It does not import the tool server, so the prompt
assembler can use it.

| File | Content |
|---|---|
| `core.py` | The rules, with no database: caps, `apply` (the three actions), `validate` (cap, secrets, injection), `render_snapshot`. |
| `store.py` | `MemoryStore` protocol, `InProcessMemoryStore` (tests, local runs), `PostgresMemoryStore`, and the read helpers that the API and the assembler use. |
| `service.py` | `MemoryService`: who may write, and the steps of a write. The tool, the API edit, and the revert use it. |

**Entries.** A scope holds a list of text entries. They show to the model with the text
`\n§\n` between them, and this text counts for the cap. Caps: `agent` 2,200, `user` 1,375,
`workspace` 2,200 characters. An entry cannot contain `§`.

**Actions.**

- `add` appends a trimmed entry. An entry that is already in the list is a no-op.
- `patch` replaces `old_text` with `content`. `old_text` must occur exactly one time in all
  entries. Zero or more than one is an error, and the error shows the current entries (zero)
  or asks for a longer text (more than one).
- `remove` removes the whole entry that has `old_text`. The text must be in exactly one entry.
- `operations` (at most 20) run in order, as one change. If one step fails, sama saves
  nothing. Sama checks the cap only on the end result, so one batch can remove old entries
  and add new ones. `reason` (at most 200 characters) goes to the revision.

**Over the cap.** The message says that the memory is full, how many characters the change
needs and the cap, how many characters to free, and the current entries (before the change)
with their size. The tool returns it as a normal result that starts with `Not saved:`, not as
an error result. The tool server cuts an error result to 2,000 characters (`MAX_ERROR_CHARS`),
and the agent needs all current entries (up to 2,200 characters) to make room. The API maps
the same failure (`MemoryFullError`) to `422`.

**Content checks.** For each new entry, sama refuses text that the redactor changes (a
secret, Section 16.3) and text that the prompt scan finds (Section 6.1). The error does not
repeat the secret. Entries that are already saved are not checked again, so the agent can
remove them. The tool, the API edit, and the revert all use these checks.

The redactor must know the secrets of the process, or the check only finds the key patterns.
The control plane gives it the settings and refreshes the vault values. The standalone API
(`python -m sama.api`) does the same: it adds `SAMA_DATABASE_URL`, `SAMA_SUPABASE_JWT_SECRET`,
and `SAMA_VAULT_KEY` when it builds the app. With `SAMA_VAULT_KEY` it refreshes the vault values
before the first request (a failed refresh stops the start) and then every few minutes. The
standalone tool server (`python -m sama.tools --store postgres`) writes memory and skills, so it
does the same with the same helper (`sama.vault.standalone`): it adds `SAMA_VAULT_KEY` to the
secrets, refreshes the vault values before it serves (a failed refresh stops the start), and
refreshes them in the background. Without `SAMA_VAULT_KEY` there are no vault values to learn.

**Who may write.** The service checks the rules in this order:

1. An unattended run (cron, workflow, webhook) cannot write. The run has no person.
2. A run of a delegated child session cannot write.
3. `user`: only in a private session, for its owner. A shared session has no `user` target.
4. `workspace`: the user of the run must be a workspace admin: the org `owner`, or a member
   with the workspace role `admin` or `owner` (Q5). An org `admin` with no workspace role is
   not enough. Since M3.2, the write of another user in an attended chat run is a chat
   proposal: a workspace admin or the org owner approves it on an approval card (Section
   9.4.2). Without a proposal store, the user gets an error that tells the agent to ask an
   admin.
5. `agent`: any run that can write. The agent notes belong to the profile of the run in the
   workspace of the run.

The user of the run is the user who started it, else the owner of a private session
(`run_user`). The run token does not hold the agent profile or the unattended flag, so the
store loads them from the run and its session.

**Storage.** Migration `0012` adds two tables (Section 17.1).

- `memories`: one row per scope, with `entries` (JSON list) and `revision` (the number of
  the last revision). A unique index with `NULLS NOT DISTINCT` on the scope keys gives one
  row per scope. A check constraint makes the keys fit the scope: `agent` has a workspace and
  a profile, `user` has a user, `workspace` has a workspace.
- `memory_revisions`: one row per write, with the full new entries, the action (`add`,
  `patch`, `remove`, `batch`, `edit`, `revert`), the author (`user` or `agent`), the session
  and run, the `reason`, and `reverted_to`. The session, run, and author ids are plain
  columns, not foreign keys, so the history stays when a session is deleted. A revision is
  not changed after it is written, but no trigger enforces this (the audit log has one).

A write is one transaction: sama creates the row if it is missing, locks it with
`SELECT ... FOR UPDATE`, applies the change, and writes the new entries and the revision. Two
writes at the same time do not lose a note. A change that does not alter the entries writes no
revision.

**Snapshot.** When the assembler builds the config of a new session, it reads the notes in the
same transaction and puts them in `RunConfig.memory_snapshot` (Section 6.1). The text has a
note that the entries are data, and then one block for each scope that has entries, with its
usage (for example `Agent notes (120 of 2,200 characters)`). A block for which the prompt
scan finds a pattern is not loaded, and the prompt says so. An entry that the redactor
changes (it holds a secret) shows as a note in place of its text, so a value that was saved
before sama knew the secret never reaches the model. A shared session gets the `agent`
and `workspace` blocks. A private session also gets the `user` block of its owner. Only a
profile with the `memory` toolset gets a snapshot. The config is frozen with the session, so
a write of the run applies from the next session, and the tool result says so.

**HTTP API.** The routes are under `/workspaces/{workspace_id}/memory` (Section 11.4). They
read as the user (`as_user`), so RLS is the second guard, and they write through
`MemoryService`. A person replaces all entries of a scope (`edit`), or sets a scope back to an
old revision (`revert`). A revert adds a new revision with the old entries. Each write adds an
audit row (`memory.edited`, `memory.reverted`) with the new revision number but not the text.
The memory row, the revision, and the audit row are one transaction (`after` hook of
`MemoryStore.change`), so a failed audit write undoes the edit.

**Assumptions in M2.1.**

- A workspace write of a user who is not an admin returned an error. Since M3.2 it is a chat
  proposal that a workspace admin or the org owner approves (Section 9.4.2).
- The `user` target works in private sessions only. A person edits their own notes in the API.
- Any user who can see the workspace can edit the `agent` notes in the API, like the tool.
- Unattended and delegated runs cannot write. The only exception is the background review
  (M2.4, Section 9.4): in a private session it writes the `user` target, and it proposes
  `agent` and `workspace` writes as approvals. It never writes them directly.
- Migration `0012` adds `memory` to the toolsets of profiles that have exactly the old default
  toolsets. New profiles get it from `DEFAULT_TOOLSETS`. The gate allows `memory` by default,
  because the tool checks the rules itself.

#### 8.2 External memory provider: Mem0 (M7.5)

Code: `src/sama/memory/provider.py`. The built-in memory of Section 8.1 is always on. A profile
can also use an external provider. The column `agent_profiles.memory_provider` (migration
`0046`) is `builtin` (the default) or `mem0`. An org admin sets it in the web app (agent
settings) or approves a `change_propose` of the target `profile`.

- **Interface.** `MemoryProvider` has `recall`, `remember`, `forget`, and `available`. The one
  implementation is `Mem0Provider` (the Mem0 platform API, `api.mem0.ai`). A product can add
  another provider behind the same interface (D16).
- **Key.** The Mem0 key of the org in the vault (`mem0_api_key`) comes first, then the key of
  the process (`MEM0_API_KEY`). Without a key for any org, the provider, the tool, and the sync
  are off. The key stays in the control plane.
- **Scope.** One user of one org. The Mem0 `user_id` is `sama:<org id>:<user id>`, so two orgs
  and two users never share memories.
- **Write (sync).** A run-end hook of the worker (`MemorySync`) sends the user message and the
  answer of a run to Mem0 (`POST /v1/memories/`, asynchronous mode). Only a completed chat run
  of a private session sends, and only when the profile has `mem0`. A shared session, a
  delegated child, an unattended run, a `/compact` run, and a run without a user send nothing.
  The redactor reads each text first, and each text is cut to 8,000 characters. A failed sync
  writes a log line and never fails the run. At most 32 syncs run at one time.
- **Read (tool).** `memory_recall(query, limit)` is in the toolset `memory` and in the default
  allow list. It searches the Mem0 memories of the owner of the private session of the run
  (`POST /v2/memories/search/`, at most 10 results). In a shared session it answers that it
  works only in a private session. For a profile with `builtin` it answers that external memory
  is off. The facts are untrusted content.
- **Delete.** A user delete and an org delete write a `data_purges` row of the kind `mem0`
  with the user ids (migration `0046`). The purge worker deletes the memories of each user at
  Mem0. Without a key, there is nothing that sama can delete, and the row is done. An org with
  its own Mem0 key loses the key with the org (the vault rows cascade), so the cleanup before
  an org delete deletes the memories of all its users first. When Mem0 fails, or the key of
  the org does not decrypt, the org delete stops with `409` and deletes nothing.
- **Redaction.** The query of `memory_recall` goes through the redactor before it leaves. Each
  fact is redacted whole and then cut to 500 characters, so a cut cannot keep part of a secret.
- **Not in the prompt.** Recall is a tool, not a part of the frozen prefix, so D9 holds and the
  prompt cache stays valid.

---

## 9. Skills and the self-improvement loop

### 9.1 Format

A skill is a folder with `SKILL.md` (YAML frontmatter and Markdown) and optional
`references/`, `templates/`, `scripts/`, and `assets/`. This is the agentskills.io format
that both Claude and Hermes use. Frontmatter: `name`, `description` (one sentence, at most
60 characters, as in Hermes), `version`, `tags`, `requires_toolsets`, `requires_integrations`,
and `config` (settings that the user fills in once).

**Code (M2.2).** The package `src/sama/skills/` holds the format, the lint, the scan, the
store, and the sync. The rules:

- `SKILL.md` starts with a YAML block between two `---` lines. `sama.skills.format` reads it
  with `yaml.safe_load`. It refuses anchors and aliases, a block longer than 4,000
  characters, and keys that the format does not know. The known keys are `name`,
  `description`, `version`, `tags`, `requires_toolsets`, `requires_integrations`, `config`,
  `license`, `compatibility`, `metadata`, and `allowed-tools`. `version` can be a number in
  the file. Sama reads it as text.
- Lint (`sama.skills.lint`) runs on every write. The `name` has lowercase letters, digits,
  and single hyphens, at most 64 characters. It must be the folder name, and it must not be
  a reserved command (Section 9.5). The `description` is one line of 1 to 60 characters.
  There are at most 10 tags (30 characters each, `[a-z0-9-]`). Each entry of
  `requires_toolsets` must be a toolset of Section 10.2. The body is not empty.
  `SKILL.md` has at most 20,000 characters. A file has at most 50,000 characters. A skill has
  at most 20 files and 200,000 characters. A file path is relative, has no `..` and no
  backslash, has at most 200 characters, and starts with `references/`, `templates/`,
  `scripts/`, or `assets/`. Files are text.
- Scan (`sama.skills.lint`) runs on `SKILL.md`, every file, and the description. It uses
  `sama.prompt.scan` for prompt injection and `sama.vault.redact` for secrets: text that the
  redactor would change is a finding. A write with a finding fails, and the error lists the
  finding ids. The check for network calls to unknown hosts (Section 9.4, step 5) is not in
  M2.2, because sama has no list of known hosts yet.

### 9.2 Library and scopes

| Scope | Who writes | Where |
|---|---|---|
| `builtin` | Our team, through pull requests to this repo | `skills/` in this repo, synced at deploy |
| `org` | Org admins; promotion from a lower scope | Postgres plus Supabase Storage |
| `workspace` | Members and the agent | Postgres plus Supabase Storage |
| `user` | The user and the agent | Postgres plus Supabase Storage |

- Every change is a new version with provenance (author, session, run). Old versions stay.
- A narrower scope overrides a wider scope with the same name.
- An agent profile chooses which skills and categories are on. The session gets the index
  of these skills.

**Code (M2.2).**

- Storage. The table `skills` has one row per skill and scope: `org_id`, `scope`
  (`org`, `workspace`, or `user`), `workspace_id` (workspace scope only), `user_id` (user
  scope only), `name`, `status` (`active` or `archived`), and `current_version`. The table
  `skill_versions` has one row per version: the name, the description, the full `SKILL.md`,
  the files, the tags, `requires_toolsets`, the change (`create`, `patch`, `edit`,
  `write_file`, `remove_file`, or `archive`), a summary, and the provenance: author kind
  (`user`, `agent`, or `system`), author user, session, and run. Session and run have no
  foreign key, so the provenance stays when a session is deleted. A version never changes.
  Builtin skills are not in the database. They are the folders of `skills/`.
- Assumption: the files of a skill are in the version row, as a JSON object of path and
  text. This replaces Supabase Storage for now. The size caps of Section 9.1 keep a row
  small. Binary files are not possible. A move to Storage later does not change the tools.
- Names. A narrower scope overrides a wider scope: `user`, then `workspace`, then `org`, then
  `builtin`. An archived skill is not in the library, and it does not override a wider
  skill. A `create` with the name of an archived skill of the same scope makes a new version
  of that row, and its status is `active` again. Archive is the only way to remove a skill.
  Sama never deletes a row.
- Write rules (Section 9.4, step 3, applies to every writer). `skill_manage` takes the scope
  of the write. The default is `user` in a private session and `workspace` in a shared
  session. A `user` write applies at once, but only in a private
  session, because a shared session has no single owner. A `workspace` write applies when
  the user of the run is a workspace `admin` or `owner`, or the org `owner`. Other users get
  an error that says an admin must make the change. Since M3.2, such a write in an attended
  chat run is a chat proposal with an approval card (Section 9.4.2). An `org` write needs the
  org `owner`: the write of another user is a chat proposal that only the org owner decides. A `builtin` skill is read-only. To change one,
  the agent makes a skill with the same name in a narrower scope.
- Every write locks the row of the skill, adds the next version, and adds the audit entry
  `skill.<change>` (agent actor, user, run). Two writes at the same time get two versions
  in sequence.
- RLS. A member reads a skill of `org` scope. A member reads a skill of `workspace` scope
  when the member can see the workspace. Only the owner reads a skill of `user` scope. A
  version has the visibility of its skill. The tables have no write policy. The control
  plane writes as the table owner.
- The index. A run sees a skill when all of these are true: the profile has the `skills`
  toolset; the profile list `agent_profiles.skills` is empty (all skills) or has the name;
  each `requires_toolsets` entry is in `agent_profiles.toolsets`; and, in a shared session,
  the scope is not `user`. The index has at most 200 skills, sorted by name.
- Migration `0013` adds `skills` to the toolsets of a profile only when its toolsets are
  exactly the previous default list (`core`, `todo`, `web`, `clarify`, `github`, `memory`).
  This is the default profile that bootstrap made, and it is the same rule as migration
  `0012`. A profile that an admin changed does not change. An admin adds `skills` to it.
  `python -m sama.admin bootstrap` gives the toolset to new profiles. The standalone
  processes `python -m sama.api` and `python -m sama.tools --store postgres` also have the
  skill library, so their skill routes and tools work.
- Metadata is scanned like text. Sama refuses a write when a file path, the `summary`, the
  description, or a tag has a secret (the redactor) or a prompt injection (Section 6.1). The
  check runs for every action, `archive` too. The summary has at most 500 characters. The
  error text does not repeat the secret.
- File paths are canonical. A path has folders and a file name that are split by a single
  `/`. It has no `.`, `..`, empty part, leading or trailing `/`, backslash, or NUL character.
  Two paths that differ only in case are refused, so two paths never name one file.
- A `patch` with `replace_all` computes the new length before it replaces. A result over the
  limit of `SKILL.md` or of one file, or over the total limit of the skill, is refused and no
  big text is built.
- `GET /skills/{skill_id}/versions` first reads the skill under RLS. A skill that the user
  cannot see gives `404`. The version rows are then selected with the `org_id` and the
  `skill_id` of that skill.

- `skills_list` takes an optional `query` and returns the index of the run: name,
  description, scope, and version. It is the same list as the prompt, but fresh. A skill that
  an earlier call of the run created is in it.
- `skill_view` takes `name` and an optional `file_path`. Without a path it returns
  `SKILL.md` and the list of files. With a path it returns that file. The result also gives
  the folder of the skill in the sandbox.
- `skill_manage` takes `action`, `name`, and the fields of the action: `content` (`create`,
  `edit`), `old_string`, `new_string`, and `replace_all` (`patch`), `file_path` and
  `file_content` (`write_file`), `file_path` (`remove_file`), `scope`, and an optional
  `summary`. `patch` works on `SKILL.md`, or on `file_path`. It fails when `old_string` is
  not in the text exactly once, unless `replace_all` is true. The result gives the new
  version number.
- Errors of these tools (not found, lint, scan, and permission errors) are tool errors with a
  text that the model can act on. They do not stop the run.
- The gate allows the three tools without a policy rule, in the toolset `skills`. The role
  check is in the store, because it depends on the scope.
- Sync into the sandbox. Before a run starts the runner, `SandboxedRuntime` makes
  `.sama/skills/` in the work folder of the sandbox hold exactly the files of the visible
  skills (`.sama/skills/<name>/...`). There is no manifest, because the agent and its scripts
  can change that folder. The sync reads only one thing from the sandbox: the answer of a
  check. Each sync does these steps. (1) One command (the check) refuses a link in place of
  `.sama` and removes the staging files of an earlier sync that did not finish. Then it makes
  a SHA-256 digest of the files in `.sama/skills` from inside the sandbox (`find`, `sort`, and
  `sha256sum`: the path and the content of each file), compares it with the digest of the
  library files, and writes `same` or `changed`. It writes `same` only when `.sama/skills` is a
  real folder that holds only files and folders (no link) and the digests are equal. If `find`
  cannot read a folder or `sha256sum` cannot read a file, the check writes `changed`. The
  check and the runner start are one command (`sama.skills.sync.checked_start`): the command
  runs the check, writes `sama-skills: same` or `sama-skills: changed` as its first stdout
  line, and on `same` replaces itself with the runner (`exec`), which then reads the job line.
  On `changed`, or when the check fails or times out (30 seconds), the command reads the job
  line, does not run it, and exits. The orchestrator then does steps (2) and (3) and starts the
  runner again without the check. Both answers come before the job is read, so the
  orchestrator always knows whether a runner got the job. So a follow-up run with unchanged
  skills costs no sandbox call for the skills. The orchestrator reads the skill files from the
  database while the sandbox starts. A false `same` only keeps the files that the agent
  already had. (2) On `changed`, the sync
  writes all the library files as one tar archive
  `.sama/.skills-<32 random hex characters>.tar`. (3) One command refuses a link in place of
  `.sama` again, extracts the archive into a new staging folder (made with `mkdir`, which fails
  if the name exists), removes the old `.sama/skills` (`rm -rf` removes a link and does not
  follow it), and moves the staging folder to `.sama/skills`. So a file that the agent
  changed, added, linked, or deleted makes the check say `changed`, and the sync restores the
  folder. A file that left the library is gone, and a link that a process makes at
  `.sama/skills` while the files are written does not redirect a write, because no write goes
  to that path. A failed sync removes its staging files. The cost is no extra call when the
  skills did not change, and three extra calls (a file write, the publish command, and a
  second runner start) when they changed. Before this
  change the cost was one write for each skill file in every run (about 1.5 seconds for 15
  files on E2B). The sync refuses more than 2,000 files or 20 million characters, and it only
  writes paths of the form
  `<name>/SKILL.md` or `<name>/<folder>/<path>` that are canonical. Each sync that writes
  files logs `skill sync run=<id> files=<n> duration_s=<s>`. The answer of the check is the
  `skills=` field of the `sandbox steps` line (Section 18.1).
- The limit of this protection. The agent can write anywhere in its own sandbox, and it runs
  in the same sandbox as the sync. The staging name and the single publish command protect the
  sync from accident and from simple tampering (a stray link or a script that changes the
  folder). They do not protect it from the agent itself: an agent that is determined can
  change `.sama` between the steps, change the files after the sync, or replace the tools
  that the check uses, so that the check says `same`. The files are a
  convenience for scripts. They are not a trust boundary, and no tool of the run reads them.
  A failed sync logs a warning and does not fail the run. The sync does not use
  `.claude/skills`, because that folder turns on the native `Skill` tool of Claude, and the
  gate does not allow it.

**Builtin library (M7.7).** `skills/` has 70 builtin skills: 68 from M7.7, and `claude-code` and
`openai-codex` from M7.2 (Section 15.4). Sama wrote all of them. They
cover the categories of the Hermes bundled skills (docs/feature-map.md, Section 3), except
`apple`, because those skills need a Mac. No Hermes text is in them. The rules:

- Each skill has at least one category tag: `agents`, `creative`, `data`, `devops`, `email`,
  `media`, `notes`, `productivity`, `research`, `social`, `software`, `web`, or `writing`.
  `skills_list` with a category as the query finds the skills of that category.
- `requires_toolsets` lists each toolset whose tools the steps need. So a skill is not in
  the index when the profile does not have the tools (conditional activation). A skill can
  name the tools of `todo`, `clarify`, and `memory` without the requirement, but its steps
  must work without them.
- A skill names sama tools by their real names. It does not name the native tools of one
  runtime (for example `Bash` or `WebFetch`). It says "the shell" or "the sandbox", so that it
  works on both runtimes.
- A skill that uses a connector (Gmail, Google Calendar, Notion, Linear, GitHub) requires
  `connections`, `integrations`, and `tool_search`. It checks the connection with
  `connections_list`, and it finds and calls the connector tools with `tool_search`,
  `tool_describe`, and `tool_call`.
- A skill that writes outside the sandbox (an email, an event, a form, a post) tells the
  agent to show the change to the user first. The gate still asks for an approval where the
  policy says `ask`.
- A skill that makes a scheduled job (Section 12.1) for a watch or a digest sets
  `attach_to_session`, so that each run sees the answers of the runs before. It tells the user
  that each run sends its answer, because a job cannot stay silent unless `deliver` is `none`.
- A skill that sends data to a public service follows the usage policy of that service. For
  example, `maps` does not send a private address to Nominatim.
- The test `tests/skills/test_builtin_library.py` runs on each folder. It checks the lint and
  the injection scan, the secret scan of the redactor, a category tag, a version of the form
  `x.y.z`, the sections, and the tool names in code font and in calls against
  `sama.gate.decide.TOOLSETS`. It also checks that the skill hides when a required toolset is
  missing, and that `/<name>` starts it.
- In a profile with all toolsets, the index lines of these skills have about 4,300 characters
  in total. The index has a limit of 200 skills.

### 9.4 Self-improvement loop

```mermaid
---
config:
  flowchart:
    wrappingWidth: 1000
---
flowchart LR
  A["Run ends or<br/>N turns pass"] --> B["Background review<br/>(fork, write-only tools)"]
  B --> C{"Proposal"}
  C -->|"user memory,<br/>user skills"| D["Apply and<br/>notify user"]
  C -->|"workspace, org,<br/>SOUL"| E["Approval card"]
  D --> F["Next session<br/>uses it"]
  E --> F
  G["Curator<br/>(weekly)"] --> H["Mark stale,<br/>archive, report"]
```

1. **Nudge.** Sama counts user turns and tool iterations per session. When a run ends, sama
   checks the counters. If either counter reached 10 since the last review (Hermes defaults),
   sama queues one **background review** and resets both counters. A session has at most
   one review running and one queued.
2. **Review.** A forked run reads the transcript with the Hermes review prompts ("should a
   skill or memory be saved or updated?"). It has only `memory` and `skill_manage`, and its
   own nudges are off, so it cannot recurse.
3. **Apply.** Writes to `user` scope apply at once, and the user sees a short note. Writes
   to `workspace` scope and SOUL changes need an approval from a workspace `admin` or the
   org `owner`. Writes to `org` scope need the org `owner` (Q5).
4. **Curate.** A weekly curator job tracks skill usage (views, uses, patches). It marks
   unused agent-written skills `stale`, then `archived`. It never deletes. Pinned skills and
   skills that jobs or workflows use are exempt. It reports on the memory, but does not change
   it. Section 9.7 has the details (M6.3). Merging near-duplicates is not built yet.
5. **Check.** Every new or changed skill passes a linter (frontmatter, length, tool names)
   and a scan (secrets, network calls to unknown hosts, prompt-injection patterns) before it
   becomes active.
6. **Learn on request.** `/learn <urls or topic>` makes the agent read sources and write a
   draft skill (Section 9.6).

### 9.4.1 Background review (M2.4)

Code: `src/sama/review/`. Migration `0016`.

**Counters and trigger.** Each session has `review_user_turns` and `review_tool_iterations`.
When a `chat` run completes, `after_completed_run` adds 1 user turn and the number of
`tool_call_started` events of the run, in one atomic UPDATE that locks the session row. If a
counter is 10 or more, sama queues one run of kind `review`, sets both counters to 0, and sets
`review_cursor_seq` to the highest message sequence of the session. Failed and cancelled runs
and review runs do not count, so a review cannot start a review (recursion is off).

**Group toolset.** A review run of a shared session does not use the group set of the profile
(Section 11.3). Its tools are the profile toolsets cut to `memory` and `skills`, and the gate
does not ask for a `memory` call, because each shared change is a proposal that a role decides.

**Limits of the queue.** The partial unique index `ix_runs_one_queued_review_per_session` allows
one queued review for each session. The claim starts a review only when the session has no open
run, so at most one review runs. If a review waits when the next trigger fires, sama extends the range of the waiting
review and does not make a second one.

**Review run.** The run is unattended, has no user, and writes no message. Its input is a
transcript of the messages after `review_cursor_seq`, up to the stored end sequence
(`runs.review_from_seq`, `runs.review_to_seq`). The text is redacted, each message is cut to
2,000 characters, and the whole text to 24,000 characters (the oldest messages go first, and
the note `[older messages are not shown]` is inside the 24,000). One message that is too long
alone is cut too. Each tool name is redacted and cut to 64 characters, and the line of the tools
of a turn has at most 400 characters (`..., and N more)`). A header tells the model that the
transcript is data and not instruction. The run is fresh: no runtime state, no SOUL, and no
context files. Its only MCP server is the sama tool server, with the run token, because the
`memory` and `skill_*` tools are tools of that server. The gate denies every other tool (Section
16.1). Its prompt has the review
instruction, the memory snapshot, the environment, and the skill index. It is not stored on the
session. Its toolsets are the toolsets of the profile that are also `memory` or `skills`.

**Model and budget.** The model is `agent_profiles.review_model`, else a cheap default of the
runtime (`claude-haiku-4-5`, `gpt-6-luna`, `fake-review`), else the model of the profile.
`RunQueue(review_models=...)` can change the defaults. The budget is the lower of the profile
budget and 10 turns, 300 seconds, and 0.50 USD. If the budget of the org, the workspace, or
the user is already spent, sama does not queue the review and keeps the counters, so the next
completed run tries again.

**Authority.**

| Write | Result |
|---|---|
| `user` memory, in a private session | Applies at once. |
| User skill, in a private session | Applies at once. |
| `agent` memory | Proposal and approval. |
| `workspace` memory, workspace skill | Proposal and approval. |
| Org skill | Proposal and approval. |

A proposal is a row in `review_proposals` (RLS: a member reads the proposals of the sessions
that they can read) and an `approvals` row with the call id `review:<proposal id>`. The summary is
the `reason` of the approval. Each text in the summary is redacted in full first and cut after
that, so a secret is never shown in part. The approver must be a workspace `admin` (an org `owner`
counts) for `agent` and `workspace` scope, and an org `owner` for `org` scope. `always_allow` is
ignored.

The role decides, and the access to the session does not. A private session can belong to a
person who is not an admin, and the admin cannot read that session. So the admin lists the open
proposals with `GET /workspaces/{id}/review-proposals` (`kind`, `scope`, the redacted summary and
proposal, and the times; no session, run, or message) and decides with
`POST /approvals/{id}/approve` or `/deny`. `GateService.decide_approval` checks the role for a
proposal, and the session visibility only for a normal approval. A plain member, also the owner
of the session, cannot decide. A person who does not see the workspace gets `404`. This is done
in the API and in the gate. The redactor can learn a secret after the proposal was stored (the
vault stores a credential later). So each place that shows a proposal builds the view again from
the stored payload, with the redactor of that moment: `GET /workspaces/{id}/review-proposals`,
`GET /sessions/{id}/approvals`, and the approval cards of Telegram and Slack
(`src/sama/review/shown.py`). The payload is redacted in full, and the summary is made from the
redacted payload and cut after that. The stored `approvals.reason` and `approvals.arguments` of
a proposal are never returned. It is not done with RLS on `approvals` and `review_proposals`,
because the control plane is their only reader, and a policy for it would break the rule that
every row of a session follows the access to the session.

The `ReviewApplier` applies a decided proposal with the roles of the approver, read again at that
time. The change, the status `applied`, and the audit entry are one transaction: the status is
set with `UPDATE ... WHERE status = 'pending'` after the write (the `after` hook of the memory
store and of the skill store). If the process stops before the commit, nothing is written, and
the proposal stays `pending`, so the next call applies it once. A second caller that works at
the same time waits, finds the proposal not `pending`, and rolls back. `rejected` and `expired`
give `discarded`. A domain error gives `failed` with a redacted error (a second, short
transaction, because nothing was written). The audit log has `review.proposed`,
`review.applied`, `review.discarded`, and `review.failed`. The API applies the proposal after the
decision, and the worker sweeps decided proposals in `maintain()`, so a repeat is safe.

A skill proposal stores the scope that the review resolved, the id of the skill that it saw, and
its version (`null` for a new skill). The apply uses that scope and does not resolve the name
again. If the skill was archived or changed after the proposal, or is another skill now, the
proposal is `failed` with the reason, and no other skill (for example an org skill with the same
name) is written. The review must propose again.

**Assumptions in M2.4.**

- Only completed `chat` runs count. Each one is 1 user turn. A tool iteration is a
  `tool_call_started` event.
- A session of a delegated lineage is not reviewed. A profile with `background_review` off does
  not count: its counters and its cursor stay as they were while the setting is off, and they
  count again when it is turned on. A review that waits in the queue when the setting goes off
  is cancelled when a worker claims it (`failure_code` `cancelled`), and does not block a chat
  run. The setting is on by default.
- A review runs only when its session is idle. A running review blocks a new chat message of the
  same session until it ends (300 seconds at most). So in practice a session has one review at a
  time. The database still enforces one running and one waiting review for any writer.
- A review that reaches the budget fails with no chat message.
- Review runs are not in the rate limit, in the runtime state of the session, in the run list, or
  in the event stream.
- `agent` memory needs approval, because all users of the profile share it and the review is
  unattended.
- The review prompt is written for sama, so `THIRD_PARTY_NOTICES.md` does not change.
- Summaries (Section 7) are not in M2.4. They stay for a later item.
- The downgrade of migration `0016` does not delete a review run, because the model calls of a
  run go with the run, and the budgets of the org and the user add up these calls. It cancels the
  review runs that are queued, running, or paused, deletes the pending approvals of the proposals,
  and drops `review_proposals`. It also sets `runtime_state` of every review run to `NULL`,
  because the old queue resumes a session from the newest completed run with a state and cannot
  tell a review from a chat run. A completed or failed review run stays as an ended chat run
  without a message.
- An admin decides a proposal without access to the private session of its author, and sees only
  the redacted proposal.
- The only MCP server of a review run is the sama tool server.
- A skill proposal that became stale is `failed`, and sama does not resolve it again.

### 9.4.2 Chat proposals and approvals over chat (M3.2)

Code: `src/sama/memory/service.py`, `src/sama/skills/store.py`, `src/sama/review/soul.py`,
`src/sama/gateway/`. Migration `0021`.

**Chat proposals.** A chat run can ask for a shared change that the user of the run has no role
for. The change does not apply. It becomes a proposal with `origin` `chat` (the column
`review_proposals.origin`; a background review has `review`). The proposal uses the same table,
approval card, applier, and timeout as a proposal of the background review (Section 9.4.1).

| Change | Who writes it at once | Else: a chat proposal that this role decides |
|---|---|---|
| `workspace` memory | a workspace `admin` or `owner`, or the org `owner` | a workspace admin or the org owner |
| `workspace` skill | a workspace `admin` or `owner`, or the org `owner` | a workspace admin or the org owner |
| `org` skill | the org `owner` | the org owner |
| SOUL of a workspace profile | nobody: it is always a proposal | a workspace admin or the org owner |
| SOUL of an org profile (no workspace) | nobody: it is always a proposal | the org owner |

- Only an attended chat run proposes. An unattended run (cron, workflow, webhook) and a delegated
  child run get an error, as before.
- `user` memory and user skills never become a proposal.
- The card names the source of a proposal: "the agent proposes a change of ..." for a chat
  proposal, and "the background review proposes ..." for a review proposal.
- The tool result tells the agent that the change is not saved, who must approve it, and that it
  must not propose it again.
- The chat run does not pause. The approval is a row with the run of the chat, and the card goes
  to the channel of the session. The run ends normally, and the proposal stays open until a
  person decides it or it times out.
- The role decides, not the access to the session. The gate checks the role again when a person
  decides (`GateService.decide_approval`). The card says who may decide:
  "A workspace admin or the org owner can decide this." or "Only the org owner can decide this."

**`change_propose`.** The toolset `self` has the tool `change_propose` (target `soul`, the full
new `content`, and a `summary` of at most 200 characters; the other targets are below). A SOUL changes the agent for every
user of the profile, so a new SOUL is always a proposal, also for an admin. The proposal keeps a
SHA-256 digest of the SOUL that the run saw: the frozen SOUL of the session
(`sessions.run_config.soul`), or the live SOUL of the profile when the session has no frozen
SOUL. When a person approves it, the applier writes the new SOUL only if the live SOUL still has
that digest; else the proposal is `failed` with "the soul changed after the agent saw it. Ask
for a new proposal". Thus a proposal from a session that started before a SOUL change also
fails when a person approves it. The applier adds the audit row `profile.updated`. A session keeps its frozen
SOUL, so the new SOUL applies from the next session. The tool server hides the tool when it has
no proposer. The bootstrap presets do not have `self` (Section 10.2), so an admin adds it to a
profile on purpose.

**More targets (M6.1).** `change_propose` also has these targets. Each one takes only its own
fields and a `summary`:

- `profile`: `name`, `runtime`, `model`, and `subagents` (the full list). The same checks as the
  `PATCH` of the web app apply (Section 11.4).
- `toolsets`: `toolsets` and `group_toolsets`, each one the full new list.
- `policy`: `rules`, at most 20 pairs of `tool` and `action` (`allow`, `ask`, `deny`, or `none`).
  A rule is a policy rule of the profile (Section 16.1), for every user. The tool is a name or a
  prefix with `*` as the last character. `none` removes the rule of the profile.

The tool refuses a change that has a secret, and it returns "Nothing to propose" when the profile
already has the values. A change of the setup is always a proposal, with the roles of a SOUL
proposal. The proposal keeps the values that the agent saw (`before`) and the new values
(`after`), only for the fields that change. When a person approves it, the applier locks the
profile and writes the change only if the profile still has the `before` values. Else the
proposal is `failed` with "the profile changed after this change was made". After the lock, the
applier also checks the role of the approver again, with a share lock on the membership rows. If
the approver lost the role, the proposal is `failed` with "the person who approved it does not
have the role now". A change that is not
valid now (for example, a sub-agent that uses a removed toolset) also fails.

The card shows a diff in place of the arguments, in the web app, Telegram, and Slack
(`approval_text`, "Change:"): a line diff for a SOUL, `+` and `-` lines for toolsets, and
`old -> new` lines for the other fields. A diff has at most 1,500 characters. The web card says
that the change applies to new sessions.

Each approved change, each edit of an admin (Section 11.4), and each revert writes one row of
`profile_changes` (the history, migration `0034`) and the audit row `profile.updated` with the
`change_id`. A row has the kind (`soul`, `profile`, `toolsets`, or `policy`), the source
(`proposal`, `edit`, or `revert`), `before`, `after`, the actor, and the proposal. RLS on
`profile_changes` is the same as for `agent_profiles`: an org owner or admin sees every row of
the org, and a member sees the rows of the org profiles and of the profiles of the workspaces
of the member.

A member can read `profile_changes` directly (RLS), so the row has redacted values: sama replaces
each known secret in `before`, `after`, and `summary` before the write, and sets `redacted` to
true when the redaction changed a value. An example is a secret that an admin removes from a
SOUL. A redacted change cannot be reverted (`409`), because sama does not keep the old values.
The history redacts the values again before it makes the diff, because the `-` and `+` in front
of each line can hide a secret of many lines from the redactor.

**Approvals over chat.** A Slack card and a Telegram card have three buttons for a tool call:
Approve, Deny, and "Always allow for me". A proposal card has Approve and Deny only, because a
policy rule cannot approve a proposal. A card for a tool in `ALWAYS_ASK` (`repo_push_approval`,
Section 15.1) or for `db_<conn>_execute` (Section 10.4) also has Approve and Deny only.

- "Always allow for me" approves the call and writes a user rule `allow` for the tool and the
  profile of the run (Section 16.1). The card then shows "Always allowed by <user>".
- A click after `expires_at` counts as deny. The approval becomes `expired`, the run resumes as
  `reject`, and the card shows "Expired by <user>". A timeout without a click is also a deny:
  the worker maintenance expires the approval (Section 16.1), and the applier discards an expired
  proposal with "the approval timed out".
- After a decision on a proposal, the gateway asks the applier to apply the decided proposals of
  the run at once. The worker maintenance applies them too, so a failure only delays the change.
- The chat gateway checks first that the person can see the session (Section 11.3). So an admin
  who cannot read a private direct message decides its proposal in the web app
  (`/workspaces/{id}/review-proposals`), and not on the card.

**Assumptions in M3.2.**

- `change_propose` has only the target `soul`. The targets profile, toolsets, and policy, the
  diff on the card, the history, and the revert came in M6.1 (above).
- The tool description says that a workspace admin or the org owner approves. For an org profile
  only the org owner can approve; the card and the gate say so.
- A proposal of a shared session goes to the card in the channel of the session. Sama does not
  send a copy to the direct messages of the admins.

### 9.5 Skills as slash commands (M2.3)

- `/<skill-name> <request>` makes a normal message that runs with that skill. The gateway
  does not load the skill. It changes the text of the message, so the frozen prefix stays
  the same. The new text is the original text, then one line: `(Skill command: load the
  skill "<name>" with skill_view and follow it for this request.)` If the user gave no
  request, the line says so. The agent then calls `skill_view`. The stored message is the
  new text, so the transcript shows what the agent got.
- A command is a skill command only when the library of the session has the skill. The
  library follows the rules of Section 9.2: scope override, the profile list, and
  `requires_toolsets`. An unknown command works as before. In Telegram it gets the help
  text, and in the web page and in Slack it is normal text.
- The names of channel commands are reserved (`new`, `stop`, `model`, `start`, `pair`,
  `help`, `skills`, `approve`, `deny`, `resume`, `memory`, `cron`, `compact`, `learn`,
  `steer`, `handoff`, and more). The lint refuses them as skill names, so a skill never hides a command.
- Telegram: `/name rest`. A skill name can have hyphens, but Telegram menus and the legacy
  parser allow only letters, digits, and `_`. So `/code_review` and `/code-review` both
  mean the skill `code-review`. In a group, the message still needs a mention or a reply,
  as for all text.
- Web page: a message that starts with `/name` is expanded in the API before it is queued.
- Slack: a message to the app (DM or mention) that starts with `/name` is expanded. Slack
  does not send an unknown slash command to the app, so the slash command is
  `/sama <name> <request>`. Sama accepts it in a DM, where the reply goes to the thread of
  the DM. In a channel, the user mentions the app and writes `/<name> <request>`.
- `/skills` in Telegram lists the index of the session in one message.

### 9.6 Skill import and `/learn` (M4.10)

**`skill_import`.** Code: `src/sama/skills/importer.py`. The tool is in the `skills` toolset.
It is not in the default allow list, so the gate asks a person before each import
(Section 16.1).

- **Source.** `url` is `https://github.com/<owner>/<repo>` or
  `https://github.com/<owner>/<repo>/tree/<ref>/<folder>`. The arguments `path` and `ref`
  replace the folder and the ref of the URL. The default ref is `HEAD`. Only public GitHub
  repositories work, because sama sends no token.
- **Fetch.** Sama reads the commit of the ref, then the tree of that commit, then each file at
  that commit SHA. So all files come from one commit. It uses the outbound client with a
  policy that allows only `api.github.com` and `raw.githubusercontent.com` and follows no
  redirect (Section 16.4). A truncated tree is refused.
- **Files.** The folder must have `SKILL.md`. Sama takes `SKILL.md` and the files in
  `references/`, `templates/`, `scripts/`, and `assets/`. It leaves out the other files and
  the files that are not UTF-8 text, and the result lists them. The limits of Section 9.2
  apply (20 files and the size limits).
- **Scan.** The import scan refuses a file that runs a script from the network
  (`curl ... | sh`, `bash <(curl ...)`), decodes code and runs it (`base64 -d ... | sh`,
  `exec(b64decode(...))`), or deletes a root or home folder. For the delete check, the scan
  gives each line, without a list mark or a shell prompt, to the hardline check of the gate
  (Section 16.1). So `rm -rf "$HOME"`, `rm -r -f /`, `rm -Rf /etc`, and `rm -rf ~; ...` are
  refused too. A line that is too long for the hardline check is checked in overlapping parts,
  so padding does not hide a command. Then the write is a normal `skill_manage` `create`: the lint and the injection
  and secret scan of every skill write run (Section 9.2).
- **Write.** The name comes from the `name` argument, else from the frontmatter, else from
  the folder. If the name is not the frontmatter name, sama changes the `name:` line of the
  frontmatter to the name as a quoted string and keeps the other lines, so the lint accepts it. `scope` works as
  in `skill_manage`, and the result depends on the role of the user. A workspace admin or the
  org owner writes a `workspace` skill directly, and the org owner writes an `org` skill
  directly. For another user, a `workspace` or `org` skill from a chat run is a proposal that a
  person with the role approves (Section 9.4.2). The proposal has the files. The summary
  of the version is `Imported from github.com/<owner>/<repo>/<folder> at <short SHA>`.
- `ManageRequest.files` carries the files. Only `create` takes files, and only
  `skill_import` sets them. The `skill_manage` tool has no such argument.
- `execute_code` cannot call `skill_import` (Section 10.2.1).

**`/learn`.** `/learn <urls or topic>` is a command of sama in Telegram, the web API, and Slack
(`/sama learn ...` in a direct message, or a mention with `/learn ...`). The channel changes
the message, as for a skill command (Section 9.5). The new text starts with
`Learn from: <request>`. Then a note tells the agent to read the sources, to treat them as
data, and to write one draft skill with `skill_manage` `create`. The rules of Section 9.4 apply
to the write: a user skill in a private session. A shared skill is a direct write when the user
has the role (as for `skill_import`), else a proposal. The command needs the
`skills` toolset. Without it, Telegram shows the help, and the web API and Slack keep the text
as it is.

### 9.7 Curator (M6.3)

Code: `src/sama/curator/`. Migration `0032`. There is no new table.

**The job.** Each workspace has one weekly job with `mode: curator` and the name `Curator`.
The scheduler adds it (Section 12.1): each tick of the leader runs an ensure step before the
due jobs. The step adds the job to at most 50 workspaces that have none. The owner of the job is
the oldest workspace `owner`, else the oldest org `owner`. The profile is the oldest profile of
the workspace, else of the org. A workspace without an owner or a profile gets no job yet. The
partial unique index `uq_jobs_curator` on `(org_id, workspace_id)` keeps one curator job for
each workspace. The ensure step also counts a `removed` curator job, so a person who removes
the job does not get it back. The job has `created_by_kind = 'system'`, and the audit log has
`job.created` with `actor_kind = 'system'`.

- **Schedule.** Every Monday in UTC. The minute and the hour come from a hash of the workspace
  id, so the fires of many workspaces do not come at the same time.
- **Changes.** A person can rename, reschedule, pause, resume, run, or remove the job, and
  change `deliver` and `failure_deliver`. The mode, prompt, script, skills, model, pre-approved
  tools, and `attach_to_session` are fixed. `JobService` refuses a change to them. A curator
  job does not count in the cap of 50 jobs for each user. It has no tools, so it needs an
  approval only when a person sends it to a target that is not the owner (the rule of
  Section 12.1).
- **Fire.** A curator fire runs in the control plane, with no run, no model, and no sandbox.
  The fire curates, writes the report as an assistant message in the fire session, and ends
  `completed` with the report as its output. The report goes to `deliver` as a normal
  `result` delivery (Section 12.1). `origin` keeps it in the fire session. A failed fire is a
  normal failed fire.

**Skills.** The curator manages only the skills that an agent wrote: version 1 has
`author_kind = 'agent'`. It does not manage built-in skills or skills that a person wrote.

- **Counters (`skills` columns).** `view_count` adds one for each `skill_view`. `use_count` adds
  one for each slash command of the skill (Section 9.5). `patch_count` adds one for each content
  write (`patch`, `edit`, `write_file`, `remove_file`). `last_used_at` is the time of the last
  view, use, or write. A job fire that names the skill does not count as a use, but such a
  skill is exempt (below).
- **States.** `skills.status` is `active`, `stale`, or `archived`. `stale_since` is set only
  for `stale` (a check constraint).
  - An `active` skill with no view, use, or write for 30 days becomes `stale`.
  - A `stale` skill becomes `archived` after 90 days with no activity, and only when it was
    `stale` for at least 7 days, so the owner sees it in one report first. The archive writes a
    new version with `change = 'archive'` and `author_kind = 'system'`.
  - A view, a use, a write, or a pin makes a `stale` skill `active` at once. The curator also
    makes it `active` again when it is pinned or exempt.
  - A stale skill still works and is still in the skill index. Only an archived skill is gone
    from the agents.
- **Exempt.** A pinned skill, and a skill whose name is in the `skills` of a job or a
  workflow that is not removed. A name match exempts the skill in each scope.
- **Run.** A run reads only the skills that must change, the oldest activity first, at most
  2000 for each run. The next run continues with the rest. Each change checks the row again in
  its update: a pin, a use, or a write that commits during the run wins, and the curator keeps
  the skill.
- **Never deletes.** The curator changes only `status`, `stale_since`, and the archive
  version. Each change writes the audit entry `skill.stale`, `skill.active`, or
  `skill.archive` with `actor_kind = 'system'`.
- **Coverage.** Each job covers the workspace skills of its workspace. The first workspace of
  the org (the oldest) also covers the org skills and the user skills. The report shows the
  user skills only as counts, because the owner cannot read them.

**Memory.** The curator only reports on the memory. Memory is always in the prompt and has no
usage signal of its own, so the curator does not change it. The report shows each agent and
workspace memory: the entries, the characters of the cap, the entries that did not change for
90 days, and a note when 90% of the cap or more is used. The first workspace also shows how
many users have a profile and how many of them are nearly full.

**Pins and restore.** The web page `/skills` shows a `Stale` or `Pinned` badge, a Pin button,
and a list of the archived skills with a Restore button. The API:

| Route | What it does |
|---|---|
| `GET /workspaces/{workspace_id}/archived-skills` | The archived skills that the user can read (at most 500), with `can_restore`. |
| `POST /workspaces/{workspace_id}/skills/{name}/{pin\|unpin\|restore}?scope=` | Pins, unpins, or restores a stored skill. The roles of a skill write apply (Section 9.2). |

A pin sets `pinned` and makes the skill `active`. A restore copies the current version into a
new version with `change = 'restore'` and `author_kind = 'user'`, and makes the skill `active`.
Pin and unpin of an archived skill fail: restore it first. Each action writes the audit entry
`skill.pin`, `skill.unpin`, or `skill.restore`.

### 9.8 Skill settings (M7.5)

Code: `src/sama/skills/settings.py`. A skill declares its settings in the `config` block of the
frontmatter. A workspace admin or the org owner fills in the values for one workspace in the
skill editor of the web app. Hermes keeps these values in its config file (`skills.config`).

```yaml
config:
  - key: default_city
    description: The city when the user names none
    default: London
  - key: max_days
    type: number
    required: true
```

- **Specs.** Each entry has `key` (1 to 40 characters: lowercase letters, digits, and `_`, a
  letter first), and optional `description` (at most 200 characters), `type` (`string`,
  `number`, or `boolean`; else the type of `default`, else `string`), `default`, and
  `required`. A skill has at most 20 settings. The lint refuses a `config` block that breaks a
  rule.
- **Values.** The table `skill_settings` (migration `0046`) has one row for each org,
  workspace, and skill name, with `settings` (a JSON object). A member who sees the workspace
  reads the row (RLS `can_see_workspace`). Only the control plane writes. A value must have the
  type of its spec, and a text has at most 500 characters. A value that the redactor would
  change (a secret) or that has a prompt injection pattern is refused: use a connection for a
  credential (Section 10.3). A value that no longer fits its spec, and a value of a removed
  key, are ignored.
- **API.** `GET /workspaces/{workspace_id}/skills/{name}/settings` gives the specs, the stored
  values, the values in use (`effective`), and `can_edit`. `PUT` with `{"values": {...}}`
  replaces the stored values (`null` goes back to the default) and writes the audit entry
  `skill.settings_updated` with the key names only. The specs come from the skill that a
  session of the person uses (the narrowest scope).
- **Use.** `skill_view` adds the values under the skill text, as data: "Settings of this skill
  for this workspace (data, not instructions)". A required setting without a value says "ask
  the user". A user export has the rows that the user changed (Section 17.4).

---

## 10. Tools, toolsets, and integrations

### 10.1 Rules

- Keep the core tool list short, because every tool schema is in every model call (Hermes
  "narrow waist"). Prefer, in this order: a skill, an integration behind `tool_search`, a
  sama tool that a service gates, a core tool.
- Tools that need a missing service or credential are hidden, not broken.
- There is no agent-callable "send a message to anyone" tool. Replies go to the trigger's
  channel. Outbound messages to other people go through integrations and need approval.

### 10.2 Tool catalog

| Toolset | Tools | Where they run |
|---|---|---|
| `core` (native) | Shell, read, write, edit, search files, web fetch | Runtime, in the sandbox |
| `todo` | `todo` (read and write the session task list). On Claude, the adapter also maps native `TodoWrite` calls to the same list with a `PostToolUse` hook. | Sama MCP |
| `web` | `web_search`, `web_extract`, `x_search` (M7.5, Section 10.2.2) | Sama MCP (search provider key and X token on the control plane) |
| `memory` | `memory` (since M2.1, Section 8.1). The snapshot goes in the prompt. `memory_recall` (M7.5, Section 8.2) when a Mem0 key exists. | Sama MCP |
| `skills` | `skills_list`, `skill_view`, `skill_manage`, `skill_import` (M4.10, Section 9.6) | Sama MCP |
| `sessions` | `session_search`, `session_read` (since M2.5, Section 7.1) | Sama MCP |
| `clarify` | `clarify` (1 to 5 questions, choices or free text, rendered per channel) | Sama MCP |
| `code_exec` | `execute_code` (Python that calls sama tools through a stub, M4.8, Section 10.2.1) | Sandbox, tools through MCP |
| `subagents` | `Task` and `Agent`, the native tool of Claude that starts a sub-agent (since M2.7, Section 13.1). It is separate from `delegation`. | Runtime, in the sandbox |
| `delegation` | `delegate_task`, `delegate_status` (since M4.6, Section 13.2) | Sama MCP |
| `coding_agents` | `coding_agent_run` (Claude Code or Codex in the session sandbox, since M7.2, Section 15.4). Default `ask`. Migration `0041` adds it to a profile that has the default toolsets of migration `0036`. Not in the default group set. | Sama MCP, CLI in the sandbox |
| `tasks` | `task_create`, `task_list`, `task_complete`, `task_block`, `task_comment` (since M4.7, Section 12.4) | Sama MCP |
| `cron` | `cronjob_manage` (preview, create, list, get, update, pause, resume, remove, run, cancel; since M4.2, Section 12.1). Migration `0026` adds `cron` to a profile that has the old default toolsets. | Sama MCP |
| `workflows` | `workflow_manage`, `workflow_run` | Sama MCP |
| `browser` | `browser_open`, `browser_snapshot`, `browser_act`, `browser_screenshot`, `browser_extract`, `browser_tabs` (since M5.1, Section 14.1). Each has an optional `target` (since M5.5). `browser_vault` (since M5.3, when the process has the vault). `browser_task` (since M5.6, `cloud` target only). `browser_act`, `browser_vault`, and `browser_task` are `ask` by default, and a sensitive action in the user's Chrome is always `ask` (Section 14.2.2). | Sama MCP to Browserbase (`cloud`) and to the extension of the user (`my_chrome`, since M5.5). |
| `media` | `vision_analyze` (M2.8, Section 11.7), `image_generate` and `text_to_speech` (M4.9, Section 11.8), `video_analyze` (M7.5, Section 10.2.2), `transcribe` | Sama MCP |
| `moa` | `moa`: mixture of agents (M7.5, Section 10.2.2) | Sama MCP (model keys on the control plane) |
| `connections` | `connections_list`, `connection_request` (gives the user a link to the Connections page), `connection_test` (since M3.4, Section 10.3) | Sama MCP |
| `integrations` | Connector tools, behind `tool_search` (Section 10.3). Since M3.4: Composio and HTTP API. Since M3.5: remote MCP servers (`mcp_<connection>_<tool>`). Since M3.11: database (PostgreSQL, Supabase, Neon). Since M4.13: MySQL, MariaDB, SQL Server, BigQuery, Snowflake, ClickHouse, MongoDB. Since M4.11: hosted stdio MCP servers (`mcp_<connection>_<tool>`). Since M4.12: HTTP APIs from OpenAPI (`api_<connection>_<tool>`). Later: plugin. | Sama MCP (Composio, HTTP client, MCP proxy, database gateway) |
| `github` | `repo_clone`, `pr_create`, `pr_comment`, `repo_push_approval` through the sama GitHub App and the sama git proxy (M3.7). Without the App: `repo_clone` of public repos only (Section 15.1). | Sama MCP plus sandbox git |
| `plan` | `plan_propose` (since M7.9): the agent proposes a plan in coding mode, and a person approves, edits, or rejects it (Section 15.5). | Sama MCP |
| `self` | `change_propose` (SOUL, profile, toolsets, policy), `sama_pr` (code change to sama, Section 15). Since M3.2, `change_propose` has the target `soul`; since M6.1, also `profile`, `toolsets`, and `policy` (Section 9.4.2). `sama_pr` is done in M6.2 (Section 15.2.1). | Sama MCP |
| `custom_tools` | The custom HTTP and script tools of the user, the workspace, and the org (`custom_<name>`, since M7.3, Section 10.8), behind `tool_search`. | Sama MCP (HTTP on the control plane, scripts in the session sandbox) |
| `tool_search` | `tool_search`, `tool_describe`, `tool_call` (since M2.6, Section 6.3). The server has them when a product has registered a deferred tool, or when the connection service is on (since M3.5: the tools of a remote MCP server come for each run), or when the custom tool service is on (since M7.3). | Sama MCP |

Agent profiles combine toolsets. Built-in presets: `assistant` (everything except `self`),
`coding` (core, web, skills, memory, sessions, github, delegation), `safe` (web, media,
clarify; the default of an event trigger, Section 12.2), and `cron` (like `assistant` but no `cron` and no `clarify`).
A profile also has a **group set**: the toolsets that a chat run in a shared session uses
(Section 11.3). The default group set is `todo`, `web`, `clarify`, `memory`, `sessions`,
`media`, and `tasks`. It has no `core`, `code_exec`, `skills`, `github`, `delegation`,
`browser`, `workflows`, `cron`, `self`, `connections`, `integrations`, or `tool_search`. The
default profile of the first org has `tool_search` since migration `0018` (M2.6), `subagents`
since migration `0019` (M2.7), `connections` and `integrations` since migration `0022` (M3.4),
`delegation` and `tasks` since migration `0023` (M4.6, M4.7), `cron` since migration `0026`
(M4.2), `code_exec` since migration `0027` (M4.8), `workflows` since migration `0029`
(M4.5), `browser` since migration `0036` (M5.1), `coding_agents` since migration `0044`
(M7.2), `custom_tools` since migration `0045` (M7.3), and `moa` since migration `0046`
(M7.5). The default group set has no
`subagents`, `delegation`, `connections`, or `integrations`. It has `tasks` because the run of
a shared card is a run in a shared session, and that run ends its card with `task_complete`.
Migrations `0022`, `0023`, `0026`, `0027`, `0029`, `0031`, `0036`, `0041`, and `0045` change
only the lists that are equal to the old defaults.
Sub-agents (Section 13.1) do not get `connections`, `integrations`, or `custom_tools`. A child
run of `delegate_task` (Section 13.2) does not get `browser`.

### 10.2.1 Code execution (M4.8)

Code: `src/sama/tools/code.py`. `execute_code(code, timeout_s)` runs a Python program in the
session sandbox. The program can call sama tools, and only its standard output returns to the
model. So one call can do many tool calls and give back a short result.

- **Stub.** Sama writes the module `sama_tools.py` next to the program. `sama_tools.call(name,
  **arguments)` or `sama_tools.<name>(**arguments)` sends one MCP `tools/call` request to the
  sama tool server and returns the text result, or raises `ToolError`. The stub uses the run
  token of the run, which the sandbox runner already has. So the sandbox gets no new secret,
  and the template of the sandbox does not change.
- **Gate.** Each stub call passes the tool gate on the tool server, as a direct call does. A
  tool that gets `ask` runs only with an approved call that has the same arguments. The stub
  also refuses `execute_code`, `clarify`, `memory`, `skill_manage`, `skill_import`,
  `change_propose`, and `tool_call`, and it allows at most 50 calls. The block list and the
  call limit are helper behavior and not a security boundary: they live in the program process,
  and the program also has the run token, so it can change them or send requests itself. The
  gate (toolsets, deny rules, and approval tickets) is the boundary, and every request passes
  it. `execute_code` itself gets `ask` by default, like `Bash`.
- **Limits.** The code has at most 50,000 characters. The time limit is 60 seconds by default
  and 300 seconds at most. A file that the program writes has at most 50 MB. The tool reads at
  most 200 KB of standard output, and the output cap of the tool server applies (50 KB, then a
  spill file). The result has the exit code and the end of the standard error only when the
  program fails or stops at the time limit.
- **Where.** Each call has its own folder in `.sama/code/` of the work folder, and the folder
  is removed at the end. The session must have a sandbox. New profiles get `code_exec`.
  Migration `0027` adds it to a profile that still has the default toolsets of `0026`. The
  default group set has no `code_exec`.

### 10.2.2 Hermes tool parity: `x_search`, `video_analyze`, and `moa` (M7.5)

These tools close the Hermes tools that sama had deferred (feature map, Section 2). Each is a
sama tool on the control plane, so both runtimes have it. Each is hidden when its service has no
key (`check_fn`): with the vault on, an org can add its own key, so the tool shows and answers
"not available" for an org without a key.

| Tool | Toolset | What it does |
|---|---|---|
| `x_search(query, limit, sort)` | `web` | Searches posts on X of the last 7 days with the recent search of the X API v2. `limit` is 1 to 20 (the API returns at least 10 and charges for each post). `sort` is `recency` or `relevancy`. The key is the org vault entry `x_bearer_token`, else `X_BEARER_TOKEN`. The posts are untrusted content. A `402` (no credits) gives a short text that an admin must act. |
| `video_analyze(source, question)` | `media` | Answers a question about a video with Gemini (`gemini-3.5-flash`). `source` is a YouTube link (Gemini reads it directly), another `https` link to a video file, or a path in the work folder of the session sandbox. A file has at most 14 MB (an inline request of Gemini has at most 20 MB after base64). The download follows the outbound address rules (Section 16.4), and the type comes from the first bytes (MP4, MOV, 3GP, WebM, AVI, FLV, MPEG, WMV). The model reads frames and audio at low resolution (about 100 tokens a second), with no thinking and at most 2,048 answer tokens. The answer is redacted, cut to 8,000 characters, and untrusted. |
| `moa(question, max_cost_usd)` | `moa` | Mixture of agents. Three reference models answer at the same time: `claude-sonnet-5-5`, `gpt-6.1-terra`, and `gemini-3.5-flash`, each with at most 1,500 answer tokens. Then the aggregator (`claude-opus-5-5`, else `gpt-6.2-sol`, else `gemini-3.5-flash`, the first with a key) writes one answer from the answers, with at most 2,500 tokens. A model without a key is left out. It needs keys of at least two providers. The answer is untrusted. |

**Money.** The three tools that call a model (`video_analyze`, `moa`) use the keys of the org
(the vault first, then the key of the process) and the `MediaMeter` of Section 11.8: the
budget checks of the model proxy before the call, one `model_calls` row for each call with the
real usage or an estimate, and the shielded task that a cancel cannot stop. `model_calls`
accepts the provider `gemini` and the endpoint `generate_content` (migration `0046`). The
prices are in the `gemini` section of `prices.toml`. Gemini charges audio input at twice the
rate of other input, so the count gives each audio token 2 input tokens. The model proxy has no
Gemini route: a run cannot call Gemini as its own model.

**The cost cap of `moa`.** Before the calls, sama computes the highest cost of the plan: the
input estimate (one token for each 2 bytes) and the full answer limit of each call, at the
price of each model. The plan must fit under the lowest of `max_cost_usd`, the cap of the
process (`SAMA_MOA_MAX_COST_USD`, default USD 0.50, at most USD 5), and the money that the run
can still spend. When it does not fit, sama drops the most expensive reference model, but it
keeps two. When two do not fit, the call fails before it spends money. A secret in one answer
does not reach the next provider: the redactor reads each answer before the aggregator gets it.
Each answer is then cut to 3,000 bytes, so the aggregator prompt fits the estimate of the plan
(the system text, the question, and at most 1,500 tokens and a tag for each answer). Before the
aggregator call, sama does the budget checks again: a record that did not write, a lost
authority, or a spent budget stops the call.

**Redaction of `x_search`.** The query goes through the redactor before it goes in the URL.
Each post is redacted whole and then cut to 600 characters.

**Gate.** `x_search`, `video_analyze`, and `moa` are in the default allow list, like
`web_search` and `vision_analyze`: they read, and the budget limits their cost. The toolset
`moa` is new. New profiles get it, and migration `0046` adds it to a profile that still has the
default toolsets of `0045`. The default group set has no `moa`, and a delegated child never has
it (`CHILD_BLOCKED_TOOLSETS`), because one call spends the money of four model calls.

**Assumptions (M7.5).**

- A1. `x_search` records no `model_calls` row: the X API is not a model, and its price is per
  post. The limit of 20 posts for each call keeps the cost small.
- A2. `video_analyze` sends a file inline. The Gemini Files API (larger files) is a later step.
- A3. The models of `moa` are fixed in code. A setting for each org can come with the settings
  console (M7.10).
- A4. `video_generate` stays deferred (feature map): video generation is slow, costs much more
  than an image, and needs a new kind of `generated_media`.

### 10.3 Connectors: one model for apps, MCP servers, APIs, and databases

A **connector** is a kind of external system that sama can reach. A **connection** is one
configured account or endpoint of a connector, with its credentials in the sama vault.
Hermes has the same idea (`tools/connectors/`, the `optional-mcps/` catalog, and
`mcp_servers` in its config). Sama keeps the idea and moves it to a multi-user cloud.

**Connector kinds**

| Kind | What it connects | Examples | Where the calls run |
|---|---|---|---|
| `composio` | Managed app toolkits with managed OAuth and triggers | Gmail, Google Calendar, Drive, Sheets, GitHub, Linear, Notion, HubSpot, Slack | Control plane, through the Composio API |
| `mcp_remote` | A remote MCP server over streamable HTTP. Auth: OAuth 2.1 (with dynamic client registration and PKCE), a bearer token, or a custom header | Supabase, Neon, Sentry, Stripe, Atlassian, Notion, Linear, any vendor MCP | Control plane, through the sama MCP proxy |
| `mcp_hosted` | A stdio MCP server (`command` and `args`, for example an `npx` or `uvx` package) that sama runs for the user | Community MCP servers with no hosted version | A **connector host** (see below) |
| `database` | A database with a native sama driver | PostgreSQL (also Supabase, Neon, RDS) since M3.11. MySQL and MariaDB, Microsoft SQL Server, BigQuery, Snowflake, ClickHouse, and MongoDB (read only) since M4.13 | Control plane, through the sama database gateway (Section 10.4) |
| `http_api` | A REST API from an OpenAPI document or from a list of endpoints. Auth: API key, bearer, basic, or OAuth 2 client credentials | Internal company APIs, a PMS, a CRM without a Composio toolkit | Control plane, through the sama HTTP client |
| `plugin` | Python code that a product registers through the extension points (D16) | vorflux: Beds24 PMS, hotel channel managers | Control plane, in the product's tool pack |

**Rules for every kind**

- **No credentials in the agent sandbox.** Every call runs on the control plane or in a
  connector host. The agent sandbox sees only tool results (D6).
- **One tool path.** Every connector tool goes through the sama MCP server and the tool gate
  (Section 16.1). Approvals, audit, budgets, and redaction work the same for all kinds.
- **The outbound URL policy applies** (Section 16.4). A connection to a private network
  needs an explicit network route (Section 10.6).
- **Results are untrusted content** (Section 16.5). A database row or an API response can
  contain a prompt injection.

**Connector catalog**

The catalog is a folder of YAML manifests in the sama repo, `connectors/catalog/<name>.yaml`,
one file per connector, like Hermes `optional-mcps/`. A manifest is added through a reviewed
pull request. A manifest holds:

- `name`, `kind`, `description`, `icon`, and `docs_url`.
- The transport: a URL (`mcp_remote`), a pinned package and command (`mcp_hosted`), a driver
  (`database`), or an OpenAPI URL (`http_api`).
- The auth type and the setup form fields. Secret fields are marked `secret: true`.
- Default tool policies (`allow`, `ask`, `deny` per tool or per pattern) and default tool
  filters (include and exclude lists).
- Keywords for suggestions, for example "the user mentioned Supabase".

An org admin can also add a custom connection that is not in the catalog (a URL, an OpenAPI
document, a database, or a stdio command). A custom `mcp_hosted` connection needs the org
`owner`. Since M3.5, a workspace `admin` or the org `owner` can add a remote MCP server by URL
to a workspace. Since M4.11, they can add a stdio MCP server with a pinned package (Section
10.3.2). Only the org `owner` can add one with a custom command. Since M4.12, they can add an
HTTP API from an OpenAPI document or a list of endpoints (Section 10.3.3).

**Connection record**

| Field | Meaning |
|---|---|
| `org_id`, `workspace_id` | Tenant scope (RLS) |
| `scope` | `personal` (one user) or `workspace` (shared) |
| `owner_user_id` | The user for a `personal` connection |
| `connector`, `kind` | Catalog entry or `custom` |
| `config` | Non-secret settings: URL, host, database name, schemas, read-only flag, tool filters |
| `secret_ref` | Reference to the vault entry. Never the secret value. |
| `state` | `pending`, `initiated`, `connected`, `failed`, `expired`, `disabled` |
| `tool_cache` | Tool names and schemas from the last discovery, with a hash |
| `policy_overrides` | Admin changes to the default tool policies |
| `last_health_at`, `last_error` | Health check result |

The state model follows Hermes `tools/connectors/contract.py`: only the backend can set
`connected` (an OAuth callback, a successful health check, or a successful discovery). The
model and the user's chat text cannot set it. A clock moves `initiated` to `expired`, and a
failed health check or a refused token moves `connected` to `expired` or `failed`. The owner
gets one notice per change.

**How a user connects something**

1. The user opens the Connections page in the web app, or types `/connect supabase` in any
   channel, or the agent calls `connection_request` when it needs a connector.
2. Sama shows a card with a link. OAuth connectors open the provider's consent page. Form
   connectors (databases, API keys, stdio servers) open a form in the web app. **Secrets are
   never typed in chat**, so they never enter a transcript.
3. Sama stores the secret in the vault, tests the connection, discovers the tools, and sets
   `connected`.
4. The tools are available at once in new and running sessions through `tool_search`, so the
   frozen prompt prefix does not change (D9). Hermes needs `/reload-mcp`, which breaks the
   prompt cache. Sama does not.

**Agent tools for connections** (toolset `connections`)

| Tool | Purpose |
|---|---|
| `connections_list` | Lists the connections that this session can use, with state and a short description |
| `connection_request` | Asks the user to connect a connector. Sends a card with the link or form. The agent cannot supply credentials. |
| `connection_test` | Runs the health check for one connection and returns the state |

**Tool naming and exposure**

- Connector tools have a prefix for the kind and the connection name, as in Hermes:
  `mcp_<connection>_<tool>` for MCP (remote and hosted), `api_<connection>_<tool>` for an
  HTTP API that an admin adds, and `db_<connection>_<tool>` for databases. Since M3.4,
  the tools of a catalog connector start with the connector name, for example
  `gmail_send_email` and `notion_search`. A Composio tool keeps its Composio slug (for example
  `GMAIL_SEND_EMAIL`) in the manifest, and the control plane uses the slug for the call. The
  control plane rejects a tool name that is longer than 64 characters. Each connection is also
  a toolset `conn-<connection>` (later).
- Connector tools are deferred behind `tool_search` by default. An agent profile can pin a
  connection so that its tools are always visible. The mechanism is in M2.6: a connector
  registers its tools in the `DeferredCatalog`, each with a toolset (Section 6.3).
- Admins can include or exclude tools per connection, as in Hermes per-server filters.

**Who can use a connection**

- A `personal` connection runs as its owner. It is usable only in that user's private
  sessions (Section 11.3), and in that user's jobs and workflows.
- A `workspace` connection is usable by all workspace members and in shared sessions. Only a
  workspace `admin` or the org `owner` can create, change, or remove it.
- Default policies: read tools `allow`, write tools `ask`, destructive tools `ask` with a
  stronger warning. Admins can change this per tool. Unattended runs follow Section 12.1.

**What M3.4 builds**

- **Table.** Migration `0022` adds `connections` with the fields of the connection record, an
  RLS policy, and one row per `(org, workspace, connector, owner)` (`NULLS NOT DISTINCT`, so
  one workspace connection per connector). A member sees the workspace connections and only
  their own personal connections. The vault takes the names `composio_api_key` and
  `connection:<uuid>`. Since M4.4, a Composio trigger instance on the account of a connection
  can start runs (Section 12.2).
- **Catalog.** `connectors/catalog/` has `gmail`, `googlecalendar`, `github`, `slack`, and
  `linear` (kind `composio`), and `notion` (kind `http_api`, a form with an API key). The
  tool schemas are a trimmed copy of the Composio schemas. Each tool has `access`: `read`,
  `write`, or `destructive`. The control plane checks each manifest when it starts. It rejects
  unknown keys, a tool name that does not start with the connector name, an `http_api` base URL
  that is not `https` or has a path, and two connectors with the same tool name.
  `SAMA_CONNECTORS_DIR` changes the folder.
- **Composio.** Sama uses the Composio v3 API. The key is the vault entry
  `composio_api_key` of the org, else `COMPOSIO_API_KEY` of the control plane. Sama finds or
  makes one auth config per toolkit (Composio-managed OAuth first) and keeps it in memory.
  Every auth scheme uses the Composio link flow: the user signs in on a Composio page, never in
  sama. The Composio user ID (the entity) is `sama-<org>-u-<user>` for a personal connection
  and `sama-<org>-w-<workspace>` for a workspace connection: one entity per user and one per
  workspace. `secret_ref` is empty for a Composio connection, because Composio keeps the
  token. A new link deletes the old Composio account of the connection, and so does a delete.
- **Forms (`http_api`).** The secret fields go to the vault entry `connection:<id>` as one JSON
  value. The other fields go to `config.fields`. The API never returns a secret. Sama tests
  the form with the `test` request of the manifest before it sets `connected`. Sama rejects a
  path value of `.` or `..`. A form that the API rejects (HTTP 422) does not echo the values
  in the answer. The `http_api` requests go through the outbound client of Section 16.4: the
  address must be public, sama does not follow a redirect, and an answer has at most 2 MB.
- **States.** Only a backend check sets `connected`: the Composio account status `ACTIVE`, or a
  good test request. An `initiated` connection becomes `expired` after 30 minutes, when sama
  next reads it. A check does not start an `expired` sign-in again, but a late Composio `ACTIVE`
  still connects it. A Composio 404 gives `expired`. A network error, a timeout, or an HTTP 408,
  429, or 5xx keeps the state and stores the error. A check writes its result only if the row
  did not change during the check, so a `disable` or a remove during a check wins. A check does
  not change a `disabled` connection. `policy_overrides` is stored but not
  used yet. For `composio` and `http_api`, the tools come from the manifest. Discovery and
  `tool_cache` are used by `mcp_remote` (M3.5).
- **Web app.** The Connections page (`/connections`) lists the connectors of the catalog with a
  row per scope. It starts a connection, sends the user to the Composio link, checks the
  connection when Composio sends the user back (`?connection=<id>`), shows the form of an
  `http_api` connector, and disables or removes a connection. The page shows only the
  connections of the current workspace, and it ignores a callback connection of another
  workspace. It goes only to an `https` sign-in link without a user name or a password. The API routes are
  `GET /connectors`, `GET` and `POST /workspaces/{id}/connections`,
  `POST /connections/{id}/check`, `PUT /connections/{id}/form`,
  `POST /connections/{id}/disable`, and `DELETE /connections/{id}`. A form has at most 20
  fields. The links of the page and the Composio callback use the first origin of
  `SAMA_WEB_ORIGINS`, else `SAMA_PUBLIC_URL`. A user who is not signed in signs in and opens
  the link again.
- **Channels.** `/connect <name>` in Telegram and `/sama connect <name>` in Slack answer with a
  link to the Connections page and a warning not to send secrets in chat. In a private chat
  the link is for a personal connection. In a group chat or a linked channel it is for a
  workspace connection. They never take a secret.
- **Agent tools.** `connections_list`, `connection_request`, and `connection_test` are in the
  default `allow` list. `connection_request` gives a link only. The connector tools are
  deferred tools in the toolset `integrations`. `tool_search` shows a connector tool only when
  the run has a connected connection that it can use. A private session uses the personal
  connection of its user first, then the workspace connection. A shared session uses only the
  workspace connection. Default policy: `read` is `allow`, `write` is `ask`, and
  `destructive` is `ask` with a stronger reason. An admin rule wins over the default.

#### 10.3.1 What M3.5 builds: remote MCP servers

- **Catalog servers.** `connectors/catalog/` has `deepwiki` (no sign-in), `neon-mcp` and
  `stripe` (a bearer token in a form), and `sentry` and `supabase-mcp` (OAuth). The names
  `neon` and `supabase` belong to the database connectors of M3.11 (Section 10.4). So the tools
  of the Neon MCP server are `mcp_neon_mcp_<tool>`. A manifest of kind
  `mcp_remote` has an `mcp` block: `url` (`https`, no query), `auth` (`oauth`, `bearer`,
  `header`, or `none`), `header` (for `auth: header`), `scope` (the OAuth scope), `include`
  and `exclude` (tool patterns), and `access` (an access per tool pattern). It has no tool
  list: the server lists its tools. The form comes from `auth`: one secret field `token` for
  `bearer` and `header`, and no form for `oauth` and `none`. sama refuses a header that it sets
  itself (for example `Host` or `Mcp-Session-Id`).
- **Servers by URL.** `POST /workspaces/{id}/connections/mcp` adds a server that is not in the
  catalog. Only a workspace `admin` or the org `owner` can do this, also for a personal
  connection. The name has the rule of a connector name and cannot be the name of a catalog
  connector. The server goes into `config.mcp` of the row, so it stays after a restart. A new
  URL, auth, or header for the same name deletes the stored credential and the tool list, so
  a credential never goes to a server that it was not given for.
- **Client.** sama speaks MCP over streamable HTTP (protocol `2025-06-18`): `initialize`, then
  `tools/list` (at most 10 pages and 200 tools) and `tools/call`. It reads a JSON answer and an
  SSE answer. sama keeps one MCP session for each connection in memory. A new credential starts
  a new session. A server that forgets a session (HTTP 404) gets a new session once. A tool
  error comes back to the agent as text.
- **OAuth 2.1.** sama follows the MCP authorization specification. It finds the protected
  resource metadata (RFC 9728) from the `401` answer or the well-known path, then the metadata
  of the authorization server (RFC 8414 or OpenID Connect). It registers a client for each
  sign-in (dynamic client registration, RFC 7591). The sign-in link has PKCE (`S256`), a
  `state`, and the `resource` parameter (RFC 8707). The redirect URI is the Connections page
  (`/connections`). The state is `<connection id>.<random>`. The web app gives the `code` and
  the `state` to `POST /connections/oauth/callback`. The API compares the state with the stored
  one, and uses the state and the PKCE verifier once. Before the code exchange, the callback
  claims the row: it moves the row from `initiated` to `pending` only if the row did not change
  since the API read it. A second callback at the same time loses the claim and gets a `400`,
  so one code is exchanged once. If the server does not answer after the exchange, the row
  stays `pending` with the error, and a check (the page shows **Check**) finishes it with the
  stored tokens. The tokens, the client secret, the state, and the verifier are
  in the vault entry `connection:<id>`. The tokens carry `bound`, a hash of the server URL and
  the OAuth client id. sama does not use tokens whose `bound` is not that of the current
  connection, so a late refresh or exchange for an old server or an old client cannot give a
  token to the new one. sama refreshes a token 60 seconds before it expires and after a
  `401`. One process refreshes one connection at a time. A refused refresh sets `expired`.
- **Discovery, cache, and health.** A check opens a new session and lists the tools. This is
  the health check. The tool names, descriptions, schemas, and annotations go into
  `tool_cache`. sama cuts a description to 1,800 characters. It drops a tool whose schema is not
  an object, whose schema is larger than 20,000 bytes, or whose name does not fit. A tool
  without a schema gets `{"type": "object"}`. A tool list that is older than 6 hours
  is read again in the background when a run asks for the tools. A network error, a timeout, or
  an HTTP 5xx keeps the state and the old tool list, and stores the error.
- **Remote text is untrusted.** The server writes the tool descriptions, titles, and schemas.
  sama redacts the secrets that the vault knows from them (Section 16) before it cuts or stores
  them, and again when it reads `tool_cache`, so a secret that the vault learns later is also
  removed. `tool_search` and `tool_describe` put this text in an `untrusted_content` block with
  the source `connector:<name>`, one block for each server.
- **Tool names and access.** A remote tool `<tool>` is `mcp_<connection>_<tool>` for the agent
  (a `-` becomes `_`, and a name longer than 64 characters is dropped). Two connections can give
  the same name (for example `a-b` and `a_b`). Such a name is ambiguous: no connection gives it
  to a run, the gate does not know it, and sama logs a warning. The Connections page still
  shows the tool. The access comes from
  the `access` of the manifest, else from the annotations: `readOnlyHint` gives `read`,
  `destructiveHint: false` gives `write`, and a tool without hints is `destructive`, as the MCP
  specification says. The default policy of Section 10.3 applies.
- **Filters.** The `include` and `exclude` of the manifest apply when sama reads the tool
  list. The `include` and `exclude` of the connection (`PUT /connections/{id}/tools`, at most
  50 patterns each) apply when a run reads the tools. A pattern uses `*` and `?`. An excluded
  tool is not in `tool_search`, and the gate denies a call to it.
- **No new session.** The tools of the remote servers are not in the static
  `DeferredCatalog`. The sama MCP server asks the connection service for the tools of the run
  at each `tool_search`, `tool_describe`, and `tool_call`. The gate asks the same service for the
  access and the toolset of a tool (`run_tools`). So a server that connects during a session
  gives its tools to the next search, and the frozen prompt prefix does not change (D9).
- **Outbound policy.** Every MCP and OAuth request goes through `OutboundClient` (Section
  16.4), without redirects. sama checks the URL when an admin adds a server. An SSE answer is
  read only to the end of the event that has the answer (with `\n`, `\r\n`, or `\r` line
  ends), so a stream that stays open does not hold the request.
- **Web app.** The Connections page shows the servers of the catalog and the servers by URL,
  a form to add a server by URL, the tool list of a connected server with its access, and the
  include and exclude filters. It handles the OAuth answer (`?code=&state=` or
  `?error=&state=`). It removes the code and the state from the address, also when the answer
  is not valid, and then shows a notice. Each connection of a server by URL has its own form,
  from its own URL and auth.

#### 10.3.2 What M4.11 builds: hosted stdio MCP servers

- **Manifest.** A manifest of kind `mcp_hosted` has a `hosted` block:
  - `package`: `registry` (`npm` or `pypi`), `name`, `version`, `bin` (optional), and `args`.
    The version is one exact version. For npm, it is a full semver version (`1.2.3`, with an
    optional pre-release or build part), because npm reads `1`, `1.2`, and `1.x` as ranges.
    For PyPI, it is one PEP 440 version such as `1.2`, `1.2.3rc1`, `1.2.3.post1`, or
    `1!2.0+local`. sama refuses a range, a wildcard (`1.*`), a tag, and `latest`.
  - or `command`: the program and its arguments. Only a server that an admin adds can have a
    command, not a catalog manifest.
  - `env`: the environment variables of the server. Each variable is one secret field of the
    form, with the name in lower case. sama refuses `PATH`, `HOME`, and the other shell
    variables, and the prefixes `LD_`, `DYLD_`, `NODE_`, `NPM_`, `PYTHON`, `PIP_`, `UV_`, and
    `SAMA_`, because they can change the program that runs.
  - `egress`: at most 20 host names that the server can reach.
  - `memory_mb` (64 to 4096, default 384) and `idle_s` (60 to 3600, default 600).
  - `include`, `exclude`, and `access`, as for a remote server.
- **Servers that an admin adds.** `POST /workspaces/{id}/connections/stdio` adds a server.
  A workspace `admin` or the org `owner` can add a pinned package with its default program and
  no arguments. Only the org `owner` can add a custom command, or a package with a `bin` or
  `args`, because a program such as `python` or `bash` with arguments runs any code. The rules of a name are the rules
  of a server by URL. The server goes into `config.hosted` of the row. A new program, new
  egress, or a new list of variables deletes the stored values. If E2B is not
  set up, the route answers `409`.
- **Connector host.** Each connection has its own E2B sandbox (Section 10.5). The control
  plane keeps the hosts in memory. The labels of a host are `sama.kind=connector_host`,
  `sama.connection_id`, and `sama.host_hash`. The session id of the host is
  `host-<connection id>`, so no agent session can find or resume it. After a restart of the
  control plane, sama finds a host again by its labels. So sama needs no table for the hosts.
  `SAMA_CONNECTOR_HOST_TEMPLATE` sets the E2B template. The default is the public `base`
  template: it has Node.js and Python 3, and no sama code and no sama secret.
- **Egress.** The egress allowlist of the host is `egress` plus the package registry
  (`registry.npmjs.org` for npm, `pypi.org` and `files.pythonhosted.org` for PyPI). E2B
  blocks all other addresses. A new allowlist makes a new host and removes the old one.
- **Bridge.** sama writes a small bridge program (`host_bridge.py`, the standard library only)
  into the host and starts it. The bridge installs the package (`npx` for npm, a virtual
  environment for PyPI), then starts the server in its own process group. The secrets go only
  to the environment of the bridge process. They are not in the labels, the files, or the
  argv. sama speaks MCP over the stdin and stdout of the bridge (newline-delimited JSON-RPC).
  The first start can take up to 240 seconds, because the install runs. A call has a
  60-second limit.
- **Life cycle.** A host starts on the first call or check. After `idle_s` seconds without a
  call, a reaper task of the control plane pauses the host. The E2B timeout is `idle_s`
  (at least 240 seconds, the start time of a server) plus 120 seconds, so E2B pauses the host
  if the control plane cannot, and not while a server starts. E2B caps it at 3600 seconds. The next call resumes the
  host and starts the server again. The bridge reads the memory of the process group each
  second. Above `memory_mb`, it stops the group and exits with code 86. A server that stops,
  for any reason, starts again on the next call. A remove of the connection removes the host.
  A tool list that is older than 6 hours is read again in the background, as for a remote
  server (Section 10.3.1). This check starts the host if it is paused.
- **No second send of a tool call.** sama sends a `tools/call` again only if the request did
  not leave sama (the server had stopped before the write). If the write to the stdin of the
  bridge fails, the server can have the request, so the call fails and is not sent again.
- **No commands from the agent.** The agent reaches the host only through the
  `mcp_<connection>_<tool>` tools and the tool gate. No sama tool runs a command in a host.
- **Tools and access.** The same as a remote server (Section 10.3.1): the tool list is in
  `tool_cache`, the health check lists the tools, and the access comes from the annotations.
- **Web app.** The Connections page has a form to add a stdio MCP server. The card of a hosted
  server shows the package or the command, the egress, and the names of the variables. The
  form of the connection asks for the value of each variable.

#### 10.3.3 What M4.12 builds: HTTP APIs

- **Import.** `POST /workspaces/{id}/connections/api` adds an HTTP API. Only a workspace
  `admin` or the org `owner` can do this. Give one source of operations:
  - `openapi`: an OpenAPI 3 document as JSON or YAML text (at most 2 MB);
  - `openapi_url`: the `https` address of the document. sama gets it through the outbound
    client (2 MB, at most 3 redirects, each checked);
  - `endpoints`: a list of endpoints, each with `method`, `path`, an optional `name`,
    `description`, `query` (the names of the query parameters), and `body` (true for a JSON
    body). Each `{name}` of a path is a required path argument.
- **Parser.** The parser does not run code and does not follow a remote `$ref`. It follows a
  local `$ref` to a depth of 10, stops at a cycle, and refuses YAML aliases, so a small document
  cannot expand. It reads path, query, and JSON body parameters. It ignores header and cookie
  parameters (sama sets the headers) and a body that is not JSON. An operation parameter
  replaces a path-item parameter with the same name and place, also its `required`. It skips an
  operation that it cannot use and counts it in `skipped`. Examples: a path template without a
  path parameter, a bad parameter name, or a query parameter that is an object, a nested
  array, or not the `form` style with `explode` (the OpenAPI default). The check reads a type
  list (`["object", "null"]`), `allOf`, `anyOf`, `oneOf`, and the item schemas. A schema with
  `properties` or `additionalProperties` is an object. At most 200 operations. The base URL is
  the `base_url` of the request, else the first `https` server of the document.
- **Tools.** Each operation is one tool `api_<connection>_<tool>`. The tool name comes from the
  `operationId`, else from the method and the path, in lower case with `_`. A second tool with
  the same name gets `_2`, `_3`, and so on. A name longer than 64 characters is cut. The tool
  list goes into `tool_cache` when the admin adds the API, so the API needs no discovery.
- **Access.** `GET` and `HEAD` are `read`, `DELETE` is `destructive`, and the other methods are
  `write`. The default policy of Section 10.3 applies: the gate allows a `read` tool and asks
  for a `write` or `destructive` tool. The include and exclude filters of Section 10.3.1 apply.
- **Auth.** `api_key` (a header or a query parameter, with its name), `bearer`, `basic`,
  `oauth2` (the client credentials grant, with `token_url` and an optional `scope`; the client
  sends its id and secret with basic auth or in the body), or `none`. The form asks for the
  values, and the vault entry `connection:<id>` holds them. sama keeps an OAuth 2 token in
  memory until 60 seconds before it expires. After a `401`, sama gets a new token and sends the
  request again once. One lock for each connection lets only one request get a token at a
  time. A request that gets a `401` replaces only the token that it used, so it uses a token
  that another request got after it. sama refuses an API key header that it sets itself (for example
  `Content-Type`).
- **Calls.** sama quotes a path argument into one path segment, so the agent cannot leave the
  base URL. It refuses a path argument of `.` or `..`, and an argument that the operation does
  not have. A query argument goes into the query. A list goes in as one pair for each item
  (`tag=a&tag=b`). `body` is the JSON body. Every request goes
  through the outbound client (Section 16.4) without redirects. An answer of `2xx` comes back
  to the agent as text. Another status comes back as an error with the status and the start of
  the answer. An error text never holds a secret.
- **Health check.** The `test` operation of the request, else the first `GET` operation without
  required arguments. A check runs without approval, so the `test` operation must be a `GET`
  or `HEAD` operation. sama refuses another method when an admin adds the API and when the
  check runs. A new base URL or auth deletes the stored values.
- **Web app.** The Connections page has a form to add an API: paste a document, give its
  address, or write one endpoint on each line (`GET /items?limit,cursor list_items`). For
  `oauth2`, the form asks how the client sends its id and secret (basic auth or the body). The tool
  list shows the method and the path of each tool.

**Triggers**: Composio triggers (for example "new email", "new GitHub issue") and webhooks can
start runs or workflows (Section 12).

### 10.4 Database connections

Sama has its own database connector because many SQL MCP servers give the model one
`execute_sql` tool with full rights. The **sama database gateway** is a module of the sama MCP
tool server. It holds the drivers and the connection pools, and it enforces the limits below
at the driver, so a prompt cannot remove them.

**Tools** (for each database connection)

| Tool | What it does | Default policy |
|---|---|---|
| `db_<conn>_schema` | Lists schemas, tables, columns, types, keys, indexes, and comments. Filters by schema or table. Results are cached for 10 minutes. | `allow` |
| `db_<conn>_sample` | Returns up to 20 rows of one table | `allow` |
| `db_<conn>_query` | Runs one read statement (`SELECT`, `WITH`, `EXPLAIN`, or the equivalent for the engine) with bound parameters | `allow` |
| `db_<conn>_export` | Runs one read statement and writes the full result as CSV (Parquet later) to a file in the agent sandbox, for analysis with code | `allow` |
| `db_<conn>_execute` | Runs one write statement (`INSERT`, `UPDATE`, `DELETE`, `MERGE`). Only on connections with writes turned on. | `ask`, with the SQL in the approval card (the estimated row count comes later) |

**Limits**

- **Read-only by default.** Sama asks for a read-only database user in the setup form.
  Sama also opens every `query` and `export` in a read-only transaction (for example
  PostgreSQL `SET TRANSACTION READ ONLY`, MySQL `START TRANSACTION READ ONLY`).
- **One statement per call.** Sama parses the SQL (with `sqlglot`) and rejects more than one
  statement. A `query` call that contains a write is rejected. DDL (`CREATE`, `ALTER`, `DROP`,
  `TRUNCATE`, `GRANT`) is `deny` by default. An admin can allow it for one connection, and
  each DDL call is then `ask`.
- **Limits per call**: a 30-second statement timeout, at most 1,000 rows and 50 KB returned
  to the model (the rest spills to a file, as in Section 6.2), and a cost limit where the
  engine reports one (for example BigQuery maximum bytes billed).
- **Allowed schemas and tables**: an admin can limit a connection to some schemas or tables,
  and can hide columns (for example `email` or `phone`). Sama applies the hidden columns to
  `schema`, `sample`, `query`, and `export` results.
- **Pools**: a small connection pool per connection, with a limit per org, so that the agent
  cannot overload a production database.

**Other ways to reach a database**

- A vendor MCP server (for example Supabase or Neon) through `mcp_remote`. This is good for
  admin tasks such as branches or migrations. Its tools follow the tool gate.
- The agent can also use a database through code in its sandbox only if the user gives a
  connection string for a scratch database. Sama does not put production credentials in the
  sandbox.

**What M3.11 builds**

- **Connectors.** `connectors/catalog/` has `postgres`, `supabase`, and `neon` (kind
  `database`, scopes `workspace` and `personal`). A `database` manifest has a `database` block
  (`engine: postgres`, the default `sslmode`, and help text for the form) and no tool list:
  the control plane adds the form and the five tools, `db_<conn>_schema`, `sample`, `query`,
  `export`, and `execute`. `<conn>` is the connector name with `_` for `-`. There is one
  connection per scope for each connector (Section 10.3), so the tool names are stable.
- **Form.** `host`, `port` (empty means 5432), `database`, `user`, `password` (the only
  secret, in the vault), `sslmode`, `schemas`, `tables`, `hidden_columns`, `writes`, and `ddl`.
  The lists are comma lists. `writes` and `ddl` are `on` or `off` (empty means `off`), and
  `ddl` needs `writes`. All fields are text fields, so `web/` does not change. The form test
  connects and runs `SELECT 1` in a read-only transaction.
- **Network and TLS.** The gateway resolves the host, rejects a private, loopback,
  link-local, or metadata address (Section 16.4), and connects to the checked address. It does
  not resolve the name again. TLS is required. `require` encrypts and does not check the
  certificate; `verify-full` checks the certificate (the `certifi` roots) and the real host
  name, also when the socket goes to the IP address. `postgres` and `supabase` default to
  `require`, because Supabase uses its own CA. `neon` defaults to `verify-full`.
- **SQL check.** `sama.connectors.sql` parses each statement with `sqlglot` (PostgreSQL
  dialect) and fails closed. `query` and `export` take exactly one `SELECT`, `WITH`, `VALUES`,
  set operation, or `EXPLAIN` of one of these. The check rejects a
  data-modifying CTE, `SELECT INTO`, a locking clause, and `SET`. `execute` takes exactly one
  `INSERT`, `UPDATE`, `DELETE`, or `MERGE`, and DDL only with `ddl` on. The check rejects all
  other statements (`COPY`, `CALL`, `DO`, `VACUUM`, ...) and, in every mode, functions that
  read files, run SQL from text, signal sessions, or change the server (for example
  `pg_stat_reset`, backup and WAL control, and the functions that write statistics). The SQL
  has at most 20,000 characters and at most 100 parameters (`$1`, `$2`, ...).
- **Names.** The check reads names as PostgreSQL does: a quoted name keeps its case, and
  other names are lower case. So `"USERS"` is not the allowed table `users`. A CTE name is a
  CTE only where PostgreSQL can see it: in the query of its `WITH`, in the CTEs after it (all
  CTEs for `WITH RECURSIVE`), and in the queries inside these. In all other places the name is
  a table, and the check applies the table rules to it. An alias in an inner query does not
  change the table of the same name in an outer query.
- **Transactions.** `schema`, `sample`, `query`, and `export` run in a `READ ONLY`
  transaction that the gateway always rolls back. So a function that writes, or `nextval`,
  also fails. `execute` runs in a read-write transaction that the gateway commits. Each
  transaction sets `statement_timeout` (30 seconds), `lock_timeout` (5 seconds), and, if the
  connection has allowed schemas, `search_path`. A local timer of 35 seconds ends the
  connection if the server does not answer.
- **Result limits.** The model gets at most 1,000 rows and 48,000 bytes of rows, so the
  result with its notes and the untrusted wrapper stays below the 50 KB of Section 6.2. If a
  result is larger, the full result (at most 100,000 rows or 20 MB) goes to a CSV file in
  `.sama/spill/` of the session sandbox, and the answer gives the path. A value longer than
  1,000 characters ends with `...[cut]` in the model text; the full values then also go to the
  file. The byte limits of the files are hard limits in UTF-8 bytes: the gateway does not add a
  row that does not fit, and the answer says that the result has more rows. `export` writes a CSV
  file (at most 200,000 rows or 50 MB) to `.sama/exports/`. Without a session sandbox,
  `export` fails and `query` gives the rows that fit.
- **Hidden columns.** An item is `column` (in every table) or `table.column`. The check
  rejects each reference to a hidden column, a whole-row reference (`row_to_json(t)`, `t`),
  a `*` where the gateway cannot drop the hidden columns by name, and column names after the
  alias of a table, a subquery, a `LATERAL` subquery, or a CTE (`users u(id, contact)`),
  because they rename columns. Column names after a function, `unnest`, or `VALUES` are
  allowed. For `alias.column`, the check finds the alias as PostgreSQL does: in the FROM of
  the nearest query first, then in the queries around it. The gateway drops
  result columns with a hidden name, and `schema` and `sample` leave them out, also in the
  keys and indexes that use them. A view or a function can still return a hidden value under
  another name, so the grants of the database user are the strongest control.
- **Allowed schemas and tables.** Each table of a statement must be in an allowed schema and,
  if `tables` is set, in the list. A name without a schema matches through `search_path`, so
  it must be allowed in each schema of that path. If it is not (for example, `a.orders` is
  allowed but `b.orders` is not, and the path is `a, b`), the check asks for the schema.
  The check rejects the system catalogs when a list is set, and the statistics views
  (`pg_stats`) when a list or a hidden column is set.
- **Approval.** The gate asks a person for each `execute` call (Section 16.1). Like
  `repo_push_approval`, an `allow` rule (exact or with a prefix, at any level) does not remove
  the ask, and "Always allow" writes no rule. A `deny` rule still wins. The approval card shows the full SQL and the parameters. The gateway rejects
  an `execute` call whose SQL has more than 1,000 characters, or whose card text has more than
  1,400 characters, so the card never cuts the SQL. `tool_search` shows `execute` only for a
  connection with `writes` on. Errors give the database message and the SQLSTATE, never the
  detail (it can hold row values).
- **Pools.** One `asyncpg` pool per connection (at most 2 connections, no prepared statement
  cache, so transaction poolers such as Supavisor and the Neon pooler work). The next call
  closes a pool that is idle for more than 5 minutes, also for the same connection, and makes a
  new one. A change of the settings makes a new pool. A pool with a running call does not
  close: the last call closes it. Idle sockets close after 60 seconds. At most 4 calls run at a time for
  each org. The `schema` result is cached for 10 minutes.
- **Tests.** `tests/connectors/test_sql.py` and `tests/connectors/test_database.py` test the
  check, the manifests, the form, TLS, and the limits. `tests/db/test_database_gateway.py`
  runs the tools against a real PostgreSQL server.

**What M4.13 builds**

- **Connectors.** `connectors/catalog/` also has `mysql`, `mariadb`, `sqlserver` (engine
  `mssql`), `clickhouse`, `bigquery`, `snowflake`, and `mongodb`. The tools are
  `db_<conn>_schema`, `sample`, `query`, `export`, and `execute`, as for PostgreSQL. `mongodb`
  has no `execute` tool: a MongoDB connection is read only. `sama.connectors.dbforms` makes
  the form and the tools of each engine, and `sama.connectors.dbsettings` checks the form.
- **Drivers.** The drivers are in the `databases` dependency group (`aiomysql`,
  `python-tds`, `pymongo`, `asyncssh`, `pyopenssl`). The group is a default group, and the
  image installs it. The gateway imports an engine module only when a connection of that
  engine is used, so the manifests load without the drivers. BigQuery, Snowflake, and
  ClickHouse need no driver: the gateway calls their HTTPS APIs with `httpx`.
- **Forms.** MySQL, MariaDB, SQL Server, ClickHouse, and MongoDB have `host`, `port`,
  `database`, `user`, `password`, `sslmode`, the policy lists, and the SSH fields (Section
  10.6). The default ports are 3306, 1433, 8443 (the HTTPS interface of ClickHouse), and
  27017. BigQuery has `project`, `dataset`, `location`, `service_account_json` (a secret), and
  `max_bytes_billed`. Snowflake has `account`, `user`, `private_key` (a secret, key-pair
  authentication), `warehouse`, `database`, `schema`, and `role`. A form field has at most
  8,000 characters, so a service account key or a private key fits.
- **Network and TLS.** The gateway checks the host as for PostgreSQL (a public address, no
  second DNS answer) and connects to the checked address, with the real host name for SNI
  and the certificate check. `require` encrypts and does not check the certificate;
  `verify-full` checks the chain (the `certifi` roots) and the host name. `disable` is
  allowed only through an SSH tunnel. BigQuery and Snowflake send each request through
  `OutboundClient` (Section 16.4).
  - SQL Server can send a routing redirect to another host. The gateway refuses it, because
    the other host is not checked.
  - pymongo resolves the host itself. So the driver connects to a local relay on
    `127.0.0.1` for one call, and the relay connects to the checked address with TLS. The
    client uses `directConnection`, so it connects to no other member of a replica set. A
    `mongodb+srv://` string is not supported: the form has a host and a port.
- **SQL check.** `sama.connectors.sql` parses each statement in the dialect of the engine
  (`mysql`, `tsql`, `bigquery`, `snowflake`, `clickhouse`). In `query` and `export`, every
  engine takes only `SELECT` (with `WITH` and set operations). `EXPLAIN` and `VALUES` are for
  PostgreSQL only. Each dialect has its own list of refused functions:
  - MySQL and MariaDB: `load_file`, the lock functions, `benchmark`, and the replication
    waits.
  - SQL Server: `OPENROWSET`, `OPENDATASOURCE`, `OPENQUERY`, `OPENXML`, `xp_*`, and `sp_*`.
  - ClickHouse: the table functions that read files, URLs, or other servers (`file`, `url`,
    `s3`, `remote`, `mysql`, ...), `dictGet`, `joinGet`, `COLUMNS`, and the functions that
    show settings or roles. A `FORMAT` clause is refused: the gateway sets the format.
  - BigQuery: `EXTERNAL_QUERY`. Snowflake: the `SYSTEM$` functions, the stage functions,
    `TO_QUERY`, `IDENTIFIER`, `RESULT_SCAN`, and `GET_DDL`.

  The engines that are not PostgreSQL also have these rules:
  - **No comments.** A statement with a comment is refused. MySQL and MariaDB run the text of
    `/*! ... */` and `/*M! ... */` as SQL, and an optimizer hint (`/*+ ... */`) can change
    the time limit. So the check cannot trust a comment. A comment-like text in a string is a
    value and is allowed.
  - **Table functions.** A function in `FROM`, `JOIN`, `LATERAL`, or `CROSS APPLY` must be
    on the list of the engine, without a schema or a database part: `private.string_split`
    can be a user function, so it is refused. Other table functions can read files, URLs, logs, or other
    servers. The lists: MySQL and MariaDB `JSON_TABLE`; SQL Server `OPENJSON`,
    `STRING_SPLIT`, and `GENERATE_SERIES`; Snowflake `FLATTEN`, `SPLIT_TO_TABLE`, and
    `GENERATOR`; ClickHouse `numbers` and `generate_series`; BigQuery none (`UNNEST` is not
    a function here, so it is allowed).
  - **Names from text.** Snowflake `IDENTIFIER('...')` is refused, because the check cannot
    see the name.
  - **Sequences.** `query` and `export` refuse `NEXT VALUE FOR`, `NEXTVAL(...)`,
    `SETVAL(...)`, `GETNEXTVAL(...)`, and on Snowflake `name.NEXTVAL`. A rollback does not
    undo a sequence value, so use `execute`. A column named `nextval` without a qualifier is
    a column and is allowed. On Snowflake, `t.nextval` is refused, because the check cannot
    tell it from a sequence.
- **Names.** A schema is a database for MySQL, MariaDB, and ClickHouse, and a dataset for
  BigQuery. A name without a schema is in the database of the form (MySQL, ClickHouse), the
  default dataset (BigQuery), or the schema of the form (Snowflake). Snowflake makes names
  without quotes upper case. SQL Server has no fixed default schema for the gateway, so a
  restricted SQL Server connection with a schema list needs the schema in each name
  (`dbo.orders`). A name without a schema is checked in the default schema: with
  `schemas=other` and `tables=orders`, `select * from orders` on MySQL (database `shop`) is
  refused, because `shop.orders` is not allowed. A name with a
  database part must use the database of the form (SQL Server, Snowflake) or the project
  (BigQuery).
- **Hidden columns.** The rules of PostgreSQL apply. Each engine also refuses the forms that
  read a whole row: `FOR JSON` and `FOR XML` (SQL Server), a `*` in a value subquery,
  `tuple(t.*)` (ClickHouse), `SELECT AS STRUCT` and `SELECT AS VALUE` (BigQuery), and a
  column position such as `$2` (Snowflake).
- **Read only and time limits.**
  - MySQL and MariaDB: a read runs in `START TRANSACTION READ ONLY` and is rolled back. The
    time limit is `max_execution_time` (MySQL) or `max_statement_time` (MariaDB). A lock wait
    stops after 5 seconds. The gateway sets a fixed `sql_mode` without `ANSI_QUOTES` and
    `NO_BACKSLASH_ESCAPES`, so the server reads quotes and strings as the check does. The
    driver does not ask for multi-statements. With TLS, the gateway does not send the login
    when the server does not offer TLS, and it closes a connection that is not encrypted.
  - SQL Server has no read-only transaction. A read runs in a transaction that is always
    rolled back, and the SQL check refuses writes. `SET LOCK_TIMEOUT` is 5 seconds. A
    statement stops when the socket has no answer for the time limit.
  - ClickHouse has no transactions. A read has `readonly=1`, so the server refuses each write
    and each change of a setting (also a `SETTINGS` clause). The time limit is
    `max_execution_time`. A write is done at once.
  - BigQuery: the time limit is `timeoutMs`, and each job also has `jobTimeoutMs`, so
    BigQuery tries to stop a job that runs longer. After the first answer gives the job ID,
    the gateway cancels the job when the time limit ends, when a poll fails, and when the call
    is cancelled. If the first request fails or the call stops before its answer, the gateway
    does not know the job ID and cannot cancel the job. Then only `jobTimeoutMs` (a best
    effort of BigQuery) and `maximumBytesBilled` limit the job.
    Each job has `maximumBytesBilled` (the form value, default 1 GB, at least 10 MB): BigQuery
    refuses a query that would bill more, and the query costs nothing.
  - Snowflake: each statement has `MULTI_STATEMENT_COUNT` 1. The gateway polls a long
    statement and cancels it after the time limit.
- **MongoDB.** `query` and `export` take `collection` and either `filter`, `projection`,
  `sort`, and `limit` (a `find`) or `pipeline` (an `aggregate`). `sample` reads 20 documents.
  `sama.connectors.mongoquery` checks each read before the gateway connects:
  - The collection, and each collection of `$lookup`, `$graphLookup`, and `$unionWith`, must
    be allowed. `system.*` collections are refused.
  - Only stages that read are allowed. `$out`, `$merge`, and the stages that show the server
    (`$currentOp`, ...) are refused. Operators that run code (`$where`, `$function`,
    `$accumulator`) are refused.
  - With hidden fields, a reference to a hidden field, `$$ROOT`, `$$CURRENT`, and the
    operators that read a field by a computed name (`$getField`, `$objectToArray`, ...) are
    refused. The gateway also drops hidden fields at every depth of the result.
  - Each row is one document as relaxed extended JSON. The time limit is `maxTimeMS`.
- **Errors.** An error gives the message of the database. A duplicate key or a foreign key
  error of MySQL, MariaDB, and SQL Server gives a fixed text, because the message of the
  database contains the key value.
- **Connections.** The new engines open one connection (or one HTTPS session) for each call
  and close it after the call. They keep no pool. The limit of 4 calls at a time for each
  org applies to all engines.
- **Tests.** MySQL, MariaDB, SQL Server, ClickHouse, and MongoDB have the M3.11 tests
  against real servers: `query` is read only, DDL is denied by default, `execute` changes rows
  with parameters, a query stops after the time limit, the model gets at most 1,000 rows and
  50 KB and the rest spills to a file, hidden columns never appear, only the allowed tables
  and schemas show, `export` writes a CSV file, and the form test uses the gateway.
  BigQuery and Snowflake are tested only against fakes (see below): the requests, the limits,
  the time limit and the cancel, the pages, the errors, the schema, and the SQL check of
  their dialects. The M3.11 tests through the whole gateway (spill, `export`, hidden columns
  in the result) do not run for them. That gateway code is the same for all SQL engines.
  - `tests/db/test_database_engines.py` (MySQL, MariaDB, SQL Server, ClickHouse),
    `tests/db/test_database_mongo.py`, and `tests/db/test_database_tunnel.py` run against
    real servers in docker. `scripts/test-databases.sh` starts the servers and runs the
    tests. The CI job "Database engines (docker)" runs the script. It is not a required
    check. Without the `SAMA_TEST_*_URL` variables, these tests skip.
  - BigQuery and Snowflake have no docker image. `tests/connectors/test_engine_bigquery.py`
    and `test_engine_snowflake.py` test them against fakes of the REST APIs (`httpx`
    transports). No test calls the real services.
  - `tests/connectors/test_engines.py` tests the dialect checks of all five SQL dialects,
    the MongoDB read check, the ClickHouse parameters, the MySQL TLS check (with a fake
    server greeting), and the tunnel pool without a server.

### 10.5 Connector hosts for stdio MCP servers

Hermes starts stdio MCP servers as local processes. In the cloud, sama runs each
`mcp_hosted` connection in a **connector host**: a small, separate E2B sandbox (or Fly
Machine later) that the control plane owns. M4.11 builds it (Section 10.3.2).

- One connector host per connection. It holds only that connection's secrets, as
  environment variables of the server process. It is not the agent sandbox, and the agent
  cannot run commands in it. The host has its own labels and session id, so the sandbox
  orchestrator of the sessions never finds it.
- The host runs the pinned package from the manifest. Sama speaks MCP to it over a stdio
  bridge, and proxies its tools like a remote MCP server.
- The host has its own egress allowlist from the manifest (for example only `api.vendor.com`),
  plus the package registry.
- The host starts on the first call, pauses when idle, and restarts if it uses too much
  memory (Hermes recycles stdio servers in the same way).
- Custom stdio commands that are not in the catalog need the org `owner` to approve them.
- A test (`tests/connectors/test_host.py`) shows that an agent sandbox cannot read the secrets
  of a host: the secrets are not in the labels, the spec, the argv, or the files of the host,
  an agent spec cannot resume the host, and the environment of a session sandbox does not have
  them.

### 10.6 Private networks

Many databases and internal APIs are not on the public internet. Sama supports these routes,
per connection:

| Route | How |
|---|---|
| IP allowlist | The sama services that call connectors use static egress IP addresses (Fly.io). The customer allows these addresses in the database firewall. See "Egress IP addresses" below. |
| SSH tunnel | Sama opens an SSH tunnel through the customer's bastion host. The SSH key is in the vault. Since M4.13, for every database engine with a host and a port. See "SSH tunnels" below. |
| Private network agent (later) | A small sama relay that the customer runs inside the network. It connects out to sama over TLS, so no inbound port is open. |

The outbound URL policy (Section 16.4) blocks private addresses for normal requests. A
connection that uses a tunnel or a relay is the only way to reach a private address, and only
for that connection.

**SSH tunnels (M4.13)**

`sama.connectors.tunnel` opens the tunnels of the database gateway. PostgreSQL, MySQL,
MariaDB, SQL Server, ClickHouse, and MongoDB connections can use a tunnel. BigQuery and
Snowflake are HTTPS APIs at fixed public hosts, so they have no tunnel.

- **Form.** `ssh_host`, `ssh_port` (empty means 22), `ssh_user`, `ssh_private_key` (a secret,
  OpenSSH or PEM, without a passphrase), and `ssh_host_key`. Empty `ssh_host` means no
  tunnel. With `ssh_host`, the other SSH fields are required.
- **Host key.** `ssh_host_key` is the public host key of the bastion (`ssh-ed25519 AAAA...`)
  or its fingerprint (`SHA256:...`). The gateway connects only to a bastion that sends this
  key. It reads no `known_hosts` file, no SSH agent, and no SSH config. Only the private key
  logs in (no password, no keyboard-interactive login).
- **Addresses.** The gateway checks the bastion like a database host: a public address, and
  the connection goes to the checked address. The database host is the host as the bastion
  sees it, so it can be private. A cloud metadata host and a link-local address are refused.
- **Route.** The tunnel listens on `127.0.0.1`, on a free port of the gateway process, and
  forwards to the database host and port. The driver of the connection connects to that
  port. The tunnel is kept for the connection (by its ID) and closes after 5 minutes without
  calls, as a pool does. A change of the form makes a new tunnel. A tunnel with a running call
  does not close.
- **Isolation.** Only the connection that owns the tunnel gets its port. Every other database
  connection and every outbound request (`web_extract`, connector calls) refuses loopback
  addresses (Section 16.4), so they cannot use the port. `tests/db/test_database_tunnel.py`
  shows this: a second connection with the host `127.0.0.1` or `localhost` and the port of the
  tunnel is refused, and `OutboundClient` refuses a URL to the port.
- **TLS.** TLS to the database is still the default through a tunnel. `sslmode` `disable` is
  allowed only through a tunnel, because the SSH connection encrypts the bytes.

**Egress IP addresses**

The database gateway runs in the control plane, so the database sees the egress address of
the Fly.io app. On 2026-11-21, `fly ips list` shows only ingress addresses for both apps:

| App | Region | IPv6 (ingress, dedicated) | IPv4 (ingress, shared) | Static egress |
|---|---|---|---|---|
| `sama-staging` | `sin` | `2a09:8280:1::211:5e7a:0` | `66.241.125.56` | None |
| `sama-production` | `sin` | `2a09:8280:1::211:5e7b:0` | `66.241.124.161` | None |

Ingress addresses are not egress addresses. Without a static egress address, the outbound
address of a Machine can change, so a customer cannot allowlist it. Until an operator
allocates static egress addresses, a database for sama must accept TLS connections from any
address, with a strong password and a user with few grants.

To allocate them (an operator does this; app-scoped egress addresses cost money):

```sh
fly ips allocate-egress --app sama-production --region sin
fly ips list --app sama-production
```

The command gives one IPv4 and one IPv6 address for the region. Add a region for each region
that runs Machines. Then add the addresses to this table and to the database setup help.
`fly ips release-egress` releases them.

### 10.7 Sama as a server: MCP server and OpenAI-compatible endpoint (M7.6)

Other agents and apps can use sama. The control plane has two entry points for them:

- `/mcp`: the sama MCP server for other agents (for example Claude Code, Cursor, or an OpenAI
  agent). It speaks streamable HTTP without MCP sessions (stateless, JSON responses).
- `/v1`: an OpenAI-compatible endpoint. `POST /v1/chat/completions` (also as a stream) and
  `GET /v1/models`, `GET /v1/models/{id}`.

Both take an **org API key** as the bearer. They are not the tool server of the runs
(`/tools/mcp`, Section 16.7), which takes run tokens. The code is in `src/sama/apikeys/` and
`src/sama/server/`.

**Org API keys.** An org owner or admin manages the keys in the web app (Admin, API keys) or
with the API (Section 11.4).

- A key acts as the admin who made it (`user_id`), in one workspace (`workspace_id`). An
  optional `agent_profile_id` limits the key to one agent profile. Else the key can use each
  profile of the org (no workspace) or of its workspace.
- `scopes` is `chat` (the `/v1` routes), `mcp` (the `/mcp` server), or both.
- `approvals` is `deny` (the default) or `pending` (below).
- `expires_in_days` is 1 to 365, or no expiry. An org has at most 50 keys that are active
  (not revoked and not expired).
- The key has the form `sama_sk_<43 characters>`. The API shows it one time, in the answer
  of the create. The table `api_keys` keeps only the SHA-256 hash (`key_hash`) and the first 12
  characters (`prefix`). Org admins read the rows through the Data API, but not `key_hash`
  (column grants, as for `org_invites`). Only the control plane writes the table.
- The redactor knows the form `sama_sk_...`, so a key that a person pastes in a message, a
  log, or a trace becomes `[REDACTED]` (Section 16.3).
- A create and a revoke write the audit entries `api_key.created` and `api_key.revoked`. A
  revoke applies at the next request.
- Each request checks the key again: it exists, it is not revoked or expired, it has the
  scope, the region of the org is served here, and its user is still an org owner or admin.
  When the admin leaves the org or loses the role, the key stops. One answer (`401`, "the API
  key is not valid") covers each failed check, so a caller does not learn whether a key
  exists. `last_used_at` changes at most once a minute.

**The same guards.** A request with a key goes through the same code as a request of the user
in the web app:

- Reads use the session rules of the user (code and RLS, Section 11.4), and the key sees only
  the sessions of its workspace with its profiles. Any other session gives `404`.
- A message goes through `RunQueue.enqueue` as the user of the key: the message rate, the
  quotas of the plan (Section 17.5), and the budget of the profile apply.
- Each tool call of the run goes through the tool gate (Section 16.1): the toolsets, the
  hardline list, the org, profile, and user policy rules.
- **Approvals.** A key with `approvals = deny` makes unattended runs (`runs.unattended`): the
  gate turns `ask` into `deny` (D13). A key with `approvals = pending` makes attended runs: a
  tool call that needs an approval pauses the run, and the approval card shows in the session
  in the web app. Only a person decides it there. The caller sees the status `paused` and the
  tool that waits. A caller can never approve a call.
- A new session of a key is a private session of the key user, in the key workspace. The user
  sees it in the web app. The audit entry `session.created` has `channel` `api` or `mcp` and
  `api_key_id`.

**OpenAI-compatible endpoint.**

- `GET /v1/models` lists the agent profiles of the key. The model id is the profile name when
  no other profile of the list has the same name (in any case), else the profile id. The
  entries also have `sama_profile_id` and `sama_profile_name`.
- `POST /v1/chat/completions` reads `model`, `messages`, `stream`, and
  `stream_options.include_usage`. Other fields are allowed and sama does not use them: the
  agent uses its own tools, so sama ignores `tools`, `tool_choice`, and the sampling settings.
  `model` is a model id of `/v1/models`, a profile name in any case, a profile id, or `sama`
  (or none) for the first profile of the key.
- Only text content is accepted (`400` `unsupported_content` for an image part). The last
  message must have the role `user`. The body has at most 512 KB and 200 messages.
- **Session mapping.** The header `X-Sama-Session-Id` (or the body field `session_id`, for
  example with `extra_body` of the OpenAI SDK) names a session that the key can use. Then
  only the last user message goes to the session: the session has the earlier messages.
  Without it, sama makes a new session. The system and developer messages and the earlier
  messages of the request go into the first user message, with user trust. They are not in
  the sama system prompt. The message has at most 32,000 characters
  (`400` `context_length_exceeded`). Each answer has the headers `X-Sama-Session-Id` and
  `X-Sama-Run-Id`, and the body has `sama.session_id` and `sama.run_id`. A client sends the
  session id again to continue the conversation.
- The answer is in the form of the OpenAI API: `id` is `chatcmpl-<run id>`, the content is
  the stored answer of the run, and `usage` is the sum of the model calls of the run
  (`prompt_tokens` has the cache tokens). A stream sends one `chat.completion.chunk` for each
  text delta of the run, then the end chunk with `finish_reason` `stop`, a usage chunk when
  `include_usage` is true, and `data: [DONE]`. A comment line (`: ping`) goes at least every
  15 seconds.
- A request without `stream` waits up to 20 seconds for a normal answer. A longer run gets
  status `200` at once, then a space every 15 seconds (JSON allows spaces before the value),
  then the body. The Fly proxy closes a connection with no bytes for 60 seconds, so this
  keeps it open. A request waits at most 30 minutes; the run goes on in the session after it.
- Errors have the OpenAI form `{"error": {"message", "type", "param", "code"}}`: `401`
  `invalid_api_key`, `404` `model_not_found` or `session_not_found`, `429`
  `rate_limit_exceeded` (with `Retry-After`) or `insufficient_quota` (a quota of the plan),
  `409` `session_not_active`, `502` with the failure code of the run (for example
  `budget_exceeded`), `409` `run_cancelled`, and `504` `run_timeout`. After the answer
  started, or in a stream, the error is in the body or in a `data:` event.
- One process lets at most 200 requests wait for runs at one time, and one key at most 16
  (`429`). The answer gives its place back in a `finally` when it ends, also when the client
  goes away or a send fails. A streamed answer ends at most 60 seconds after the wait limit.

**MCP server for other agents.** The tools give JSON text. Each tool call checks the key
again. A tool call waits at most 45 seconds, because a stateless MCP answer sends no byte
before it ends. A longer run gives its `run_id`, and the client asks again with `run_get`.

| Tool | What it does |
|---|---|
| `agents_list()` | The agent profiles of the key, with the name to use as `agent`. |
| `send_message(text, session_id, agent, wait_s)` | Starts a run: a message to a session of the key, or to a new session with the agent. Waits up to `wait_s` (default 30, at most 45) and gives the reply, or the `run_id`, the status, and the approval that the run waits for. |
| `run_get(run_id, wait_s)` | The status and the reply of a run of the key. |
| `session_read(session_id, after_seq, limit)` | The user and assistant messages of a session, oldest first, at most 50, each cut at 8,000 characters. |
| `session_search(query, limit)` | The sessions of the key workspace that the user sees (Section 7.1). A key for one agent profile finds only the sessions of that profile. |
| `skills_list()` | The skills that the user sees in the key workspace (Section 9.2). |
| `workflows_list()`, `workflow_run(workflow_id, inputs)`, `workflow_get(instance_id)` | The workflows of the key user in the key workspace (Section 12.3). A version that waits for a human approval does not start. A key for one agent profile cannot use these tools (`403`), because an agent step can name another profile. |

A request without a valid key gets `401` before MCP reads it. The MCP check of the `Host`
header is off, as in the tool server: a browser cannot add the key. The tool text is redacted
with the secrets of the vault.

**Data controls.** `api_keys` has `org_id` and an RLS policy. An org export has the keys
without `key_hash`. A user export has the keys that the user made or revoked. A user delete
removes the keys of the user (`ON DELETE CASCADE`); `revoked_by_user_id` becomes null. An org
delete removes all keys (Section 17.4).

**Not in M7.6.** The keys do not open the other routes of the HTTP API. A key has no budget
of its own: the budget of the profile and the quotas of the org apply. Images and caller tools
are not supported in `/v1/chat/completions`. The MCP server has no OAuth sign-in: a client
sends the key in a header.

### 10.8 Custom tools (M7.3)

A user, a workspace, or an org adds its own tool without code in the sama repo: an **HTTP
request tool** or a **script tool**. Code: `src/sama/customtools/` (`spec.py`, `http.py`,
`script.py`, `service.py`), the routes in `src/sama/api/custom_tools.py`, and the Tools page of
the web app (`/tools`, `web/components/CustomTools.tsx`).

**Definition.** Each version of a tool has:

- a `kind`: `http` or `script`;
- a `description` (at most 1,000 characters) that the agent reads to choose the tool;
- an `input_schema`: a JSON schema of the arguments with `"type": "object"` (at most 50
  properties and 20,000 bytes);
- a `policy`: `allow` (run without an approval), `ask` (an approval for each call), or `deny`
  (the tool is off);
- the request (`http`) or the script (`script`).

**HTTP request tool.** `method` (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`), a URL
template, headers, an optional JSON body template, the auth, and the response mapping.

- A placeholder is `{{name}}`, and `name` must be a property of the input schema. The scheme
  and the host of the URL are fixed: a placeholder can be only in the path and the query. A
  path value is quoted into one path segment (`/` becomes `%2F`), and `.` and `..` are refused,
  so the agent cannot leave the path or the host. A query parameter whose value is one
  placeholder of an argument that the call does not give is left out. A list gives one pair
  for each item.
- A header value is text with placeholders. It cannot have a line break. A tool cannot set
  `Host`, `Content-Length`, `Connection`, `Authorization`, `Cookie`, or the header of its own
  API key: a credential goes into the auth.
- The body is a JSON value. A string that is only one placeholder becomes the argument as a
  JSON value. A string with a placeholder in other text gets the argument as text. A `GET` or
  `HEAD` request has no body.
- Auth: `none`, `bearer`, `api_key` (a header or a query parameter, with its name), or `basic`.
  The credential is in the vault entry `custom_tool:<tool id>`, as a JSON object with `origin`
  and the fields (`value`, or `username` and `password`). The credential is **bound to the
  origin** (`https://host[:port]`) of the version that was current when it was saved. A
  version with another origin gets no credential: the call fails until a person sets the
  credential again. So a changed URL never sends a saved key to a new host. The redactor gets
  the fields, not the origin.
- Response mapping: `path` (a dot path such as `data.items` or `items.0`), `fields` (keep only
  these keys of an object, or of each object of a list), and `max_chars` (default 20,000, at
  most 100,000).
- The answer is redacted before the mapping cuts it, so a cut cannot leave a part of a secret.
  The redactor also learns the encoded `basic` value and the URL-encoded forms of an API key
  in the query, so an API that echoes the header or the URL does not show the credential.
- Every request goes through `OutboundClient` (Section 16.4): `https` only, public addresses
  only, no redirects, 2 MB, and 30 seconds. sama checks the origin with DNS when a person saves
  a version. A `2xx` answer comes back as text. Another status is an error with the status and
  the start of the answer.

**Script tool.** `language` (`python` or `node`), `code` (at most 50,000 characters), and
`timeout_s` (default 30, at most 120). The script runs in the **session sandbox** of the run,
like `execute_code` (Section 10.2.1), in its own folder of `.sama/tools/` that is removed at the
end. The arguments go to its standard input as one JSON object. Its standard output must be
one JSON value (at most 200 KB, and 50,000 characters go to the model). The script gets no run
token and no secret in its environment, and sama refuses a script whose code has a secret (the
redactor check). The script is not a new trust boundary: it runs as the same user as the agent
shell in the session sandbox (Section 16.2), so it has the same rights as a `Bash` call of the
agent. The gate on the tool server stays the boundary for every sama tool call.
The sandbox image has Node.js 22 since M7.2 (Section 15.4). A failure gives the exit code and the
end of the standard error.

**Scopes.** Any member adds tools for themselves. Only an admin adds and changes the shared
tools. This is the choice of the owner, and it is the rule of the sub-agent library (Section 13.3).

| Scope | Who sees it | Who writes it |
|---|---|---|
| `user` | Its user only | Its user |
| `workspace` | The members who see the workspace | A workspace admin, an org admin, or the org owner |
| `org` | Every member of the org | An org admin or the org owner |

- A create, an edit, or a revert of a `workspace` or `org` tool by another member gets `403`.
  That member reads and uses the tool. `GET /workspaces/{id}/custom-tools/scopes` gives the
  scopes in which the user can add a tool, and the web form offers only those scopes.
- Each write (create, edit, revert) is a new version (`custom_tool_versions`) and applies at
  once. A version never changes. The tool runs its `current_version`. Every version is
  `approved`: the column `review` stays from the first design of M7.3, which let members propose
  shared versions for an admin to approve. The owner did not choose that design, so it was
  removed before production.
- A revert copies an old version into a new version (`change = revert`).
- Only a person who manages the tool sets its credential, tests it, and deletes it.
- Each write is in the audit log: `custom_tool.created`, `edited`, `reverted`,
  `credential_set`, `tested`, and `deleted`.

**Checks of each write.** The description, the input schema (with the descriptions of the
arguments), the headers, the body, the URL, and the summary get the prompt injection scan of
Section 6.1 and the secret check of the redactor. The code of a script gets the secret check.
A finding refuses the write (HTTP 422). The
name has 2 to 40 characters (`[a-z][a-z0-9_]*`).

**The agent.** The tools are deferred tools (Section 6.3) of the new toolset `custom_tools`.
The agent name is `custom_<name>`. Both runtimes find them with `tool_search`, read the schema
with `tool_describe`, and run them with `tool_call`. The tool server asks the service for the
tools of the run at each search, so a new tool or a new version is usable at once, also in a
running session, and the frozen prompt prefix does not change (D9).

- A run sees the `org` tools, the `workspace` tools of its workspace, and, in a private session,
  the `user` tools of its run user. A shared session has no `user` tools. The narrowest scope
  wins for one name: `user`, then `workspace`, then `org`. At most 200 tools for each run; the
  query keeps one row for each name (the narrowest) before the limit.
- The gate reads the tools and their policies in its transaction (`run_tools` and the new
  `run_rules` of `GateService`). A policy is a rule of the same name. `ask` and `deny` are org
  rules, so a user rule (for example an "always allow" of an approval card) cannot loosen them.
  `allow` is a user rule for a `user` tool and an org rule for a shared tool. A `deny` rule of
  any level still wins, an admin `ask` rule still wins, and a user can make an `allow` tool
  stricter for themselves. Between two rules of the same level and the same kind of name, the
  stricter rule wins (a change of `_most_specific` for every rule, so the order of the rules
  never loosens a verdict). A `deny` tool is not in `tool_search`. A deferred call passes the gate as a
  direct call does (Section 16.1), so an `ask` tool needs an approval card with the real
  arguments, and an unattended run follows Section 12.1.
- The service checks the arguments against the top level of the schema: the required names,
  no unknown names, the type, and `enum`.
- The result is untrusted content with the source `custom_tool:<agent name>` (Section 16.5).
- `custom_tools` is in the default toolsets of a new profile. Migration `0045` adds it to a
  profile that still has the default list of `0041` (M7.2). The default group set does not have it.
  Sub-agents do not get it (they have no `tool_search`).

**Test button.** `POST /custom-tools/{id}/test` with `arguments` and an optional `version`
runs a version as the agent would and returns `ok`, the result or the error, and the time. The
answer goes to the browser and not through the tool server, so the service redacts it. An
HTTP test runs on the control plane. A script test runs in a new **scratch sandbox** from the
session template (`SandboxedRuntime.scratch_sandbox`), which the service kills at the end. A
scratch sandbox that is not killed pauses after 5 minutes.

**Routes.** `GET` and `POST /workspaces/{id}/custom-tools`, `GET`, `PUT`, and
`DELETE /custom-tools/{id}`, `GET /custom-tools/{id}/versions`,
`GET /workspaces/{id}/custom-tools/scopes`, `POST /custom-tools/{id}/revert`,
`PUT /custom-tools/{id}/credential`, and `POST /custom-tools/{id}/test`. No answer has a
credential. A validation error does not repeat the input.

**Tables.** Migration `0045` adds `custom_tools` (one row for each tool and scope, unique name
for each scope owner) and `custom_tool_versions`, both with `org_id` and RLS: a member reads the
`org` tools, the `workspace` tools of the workspaces that they see, and their own `user` tools.
An org admin does not read the `user` tools of other users. A version has the visibility of its
tool. Only the control plane writes. The vault accepts the names `custom_tool:<id>`. The data
controls (Section 17.4) export the tools of a user and the versions that they wrote or reviewed.
A user delete removes the `user` tools of the user and their credentials.

**Web app.** The Tools page lists the tools of the workspace with the scope, the kind, the
running version, and the policy. It adds and edits a tool (a form for the request or the
script), tests it, sets the credential, shows the versions with the whole definition of each
version, tests one version, reverts, and deletes. The scope list of the form has only the
scopes that the user can write, so a member sees "Only me". A member sees the shared tools
without the edit, test, and delete controls. `CustomToolsPanel` holds the whole feature for one
workspace, so the settings console (M7.10) can use it.

**Assumptions.**

| # | Assumption |
|---|---|
| T1 | The org URL lists of the browser (Section 14.1) do not apply to custom HTTP tools. The private address rules and the other outbound rules apply. |
| T2 | A credential belongs to the tool, not to a version. A revert does not restore an old credential. The origin binding stops a key from going to a new host. |
| T3 | Only a person who manages a tool tests it, because a test sends the saved credential and bypasses the policy. |
| T4 | The argument check covers the top level of the schema only. The script or the API checks nested values. |
| T5 | The agent cannot create or change custom tools. A person does it in the web app or with the API. |

---

## 11. Channels: web, Slack, Telegram, WhatsApp, email, Chrome

### 11.1 Gateway

One gateway service hosts all channel adapters. Each adapter does five things:

1. **Inbound**: verify the request signature, normalise the event (text, files, voice notes,
   thread, sender), and drop duplicates.
2. **Access**: find the paired sama user. Unknown senders get a pairing prompt or nothing.
3. **Route**: find or create the session binding and queue a run.
4. **Outbound**: stream the reply (edit one message as text arrives), send files and images,
   and split long text by the channel's limits.
5. **Controls**: render approval cards and `clarify` questions as buttons, and map slash
   commands (`/new`, `/stop`, `/steer`, `/handoff`, `/undo`, `/rollback`, `/resume`, `/model`,
   `/skills`, `/memory`, `/cron`, `/approve`, `/deny`, `/<skill-name>`). A skill command changes the text of the
   message (Section 9.5).

**Busy controls and handoff (M3.9, M7.1).** The busy modes, the queue, `/stop`, `/steer`, and
`/bg` are in Section 4.3. Each channel shows them in the same way:

| Control | Telegram | Slack | Web app |
|---|---|---|---|
| Queue | The reply to a message that waits says "Queued", with the steer and stop commands. When the run starts, the gateway follows it. | The same, in the direct message or the thread. | `POST /sessions/{id}/messages` answers `queued: true`, and the chat shows a notice. |
| Join (M7.1) | A plain message while a run works joins it, and the reply says so. | The same, in the direct message or the thread. | `POST /sessions/{id}/messages` answers `steered: true`. The chat shows the message with "Queued into the current run", then "Delivered to the run" (`GET /sessions/{id}/steers`). |
| Parallel (M7.1) | `/bg TASK`. The reply gives the number of the run. | `/bg TASK` in the message. | `/bg TASK` in the composer or the "Run in parallel" button (`parallel: true`). With two or more open runs, the chat lists them, each with its live text and a Stop button. |
| Stop | `/stop` stops all runs, `/stop NUMBER` one run | `/sama stop [NUMBER]` in a direct message. In a thread, a mention with `/stop [NUMBER]`. | `/stop [NUMBER]` in the composer, the Stop button (all runs), or the Stop button of one run. |
| Steer | `/steer TEXT` | `/sama steer TEXT` in a direct message. In a thread, a mention with `/steer TEXT`, because a Slack command has no thread. | `/steer TEXT` in the composer (`POST /sessions/{id}/steer`). |
| Handoff | `/handoff slack` or `/handoff web` in the direct chat | `/sama handoff telegram` or `/sama handoff web` in a direct message | `/handoff telegram` or `/handoff slack` in the composer (`POST /sessions/{id}/handoff`). |
| Undo (M6.4) | `/undo` in the direct chat | `/sama undo` in a direct message | `/undo` in the composer (`POST /sessions/{id}/undo`). |
| Rollback (M6.4) | `/rollback` lists the checkpoints and `/rollback NUMBER` restores one, in the direct chat | `/sama rollback [NUMBER]` in a direct message | `/rollback [NUMBER]` in the composer (`GET /sessions/{id}/checkpoints`, `POST /sessions/{id}/rollback`). |

WhatsApp and email (M7.8, Section 11.9) use the shared direct gateway
(`sama.gateway.direct`), so they have the same busy controls as Telegram: a plain message
joins the running run, `/bg TASK` starts a parallel run, and `/stop [NUMBER]` stops all runs or
one run. In email, a reply in the thread is the message.

A chat follower that follows a run also follows the new run when the notes of the run are
requeued (Section 4.3), so the answer to a late note comes in the same chat.

`/handoff` (`sama.gateway.handoff.Handoffs`) moves a session to the direct chat of the user in
another channel. The rules:

- Only a `private` session of the user can move, and only when it is `active` and has no worker
  bot. A shared session belongs to its channel or group.
- For Telegram and Slack, the user must have a channel identity in the same workspace (Section
  11.3), and the integration must be active. A Slack handoff opens the direct message with
  `conversations.open`.
- In one transaction, `move` locks the session row, binds the session to the target chat,
  deletes all other chat bindings of the session, writes a row in `session_handoffs`, and writes
  the audit entry `session.handoff`. The web app shows every session that the user can read, so
  a handoff to the web app only deletes the chat bindings, and the answer has the link to the
  session.
- The `session_handoffs` row keeps the open runs of the session in `run_ids`: the handoff takes
  them. After the commit, the target gateway follows these runs in the new chat and then sends
  a short note there. These followers do not check the follower limit of the gateway, because
  the handoff moves the followers and does not add them.
- Each run has one owner of its output: the follower that the newest handoff of the run
  started, or the first follower when no handoff took the run. Every other follower of the run
  stops at its next send (`moved_away`), and does not go on to the run of the late `/steer`
  notes of the run: the owner follows that run. Thus the reply of an open run comes only in the new
  chat, a round trip (Telegram to Slack to Telegram) sends the reply once, and the reply of a
  run that ended before the handoff still comes in the old chat. A handoff to the web app takes
  the open runs too, but no chat follows them.
- Telegram accepts `/handoff` only in a private chat, and Slack only in a direct message.

### 11.2 Adapters

| Channel | Transport | Notes |
|---|---|---|
| Web app | REST and server-sent events from the API | Full features: chat, approvals, settings, memory and skill editors, jobs, workflows, connections, run traces. |
| Slack | Events API, interactivity, and the `/sama` command over HTTP. Web API calls with `httpx` (no Bolt: sama needs only six methods and checks the signature itself) | One Slack app per org. An org admin makes the app from a manifest and connects it as an integration (Section 11.6). Direct messages and mentions in linked channels. A thread in a channel is a session. Approval buttons through Block Kit. Images that users send are stored for `vision_analyze` (Section 11.7). |
| Telegram | Bot API webhook | One bot per org. An org admin connects the bot of the org as an integration (Section 11.6), and the vault keeps its token (Q3). DMs and groups (mention or reply only in groups). Voice notes are transcribed. Photos and image files are stored for `vision_analyze` (Section 11.7). Inline keyboards for approvals. |
| Chrome extension | WebSocket from the extension to the API (`/channels/chrome/ws`, Section 14.2.1). Supabase Auth sign-in, like the web app | Side panel chat bound to the current tab. "Ask about this page" shares the page as untrusted content (M5.4). The `my_chrome` browser target on the same WebSocket (Section 14.2.2, M5.5). |
| WhatsApp (M7.8) | Webhooks of Meta WhatsApp Cloud API, Twilio, or Gupshup; the org picks one | One number per org. Direct chats only. Approval buttons or reply codes. Images and voice notes. Section 11.9. |
| Email (M7.8) | SendGrid Inbound Parse (signed) and the SendGrid mail API | One inbound address per org. A thread is a session. Approval links. Image attachments. Section 11.9. |
| Later | Discord, Microsoft Teams | Same adapter shape. |

### 11.3 Pairing and access

- A user links a Telegram or Slack identity from the web app. The app shows a short code,
  and the user sends it to the bot. Codes are single-use and expire after 10 minutes. Rate
  limits and lockout follow Hermes `gateway/pairing.py` (5 failed tries lock the identity for
  1 hour). Codes are never logged.
- A Slack workspace or Telegram group can bind to a workspace and an agent profile. Only
  paired members can start runs there.
- Group messages are untrusted input. In a shared session the agent uses the group set of
  the profile (the column `agent_profiles.group_toolsets`), and not `toolsets`. The default
  has no shell, no code edits, and no outbound writes (Section 10.2). The tool gate does the
  cut at step 1, so a tool that is not in the group set gets `deny`. A private session is not
  changed. See "Group toolset and approvers" below.
- **Audience and principal.** Each session has an audience: `private` (one user, for
  example a DM) or `shared` (a Slack channel or thread, a Telegram group). A private session
  runs as its user. A shared session runs as the workspace and uses only shared context:
  SOUL, `agent` and `workspace` memory, workspace skills, and workspace connections. It does
  not load `user` memory, user skills, or personal connections. A user who wants personal
  data uses a private session, or shares one item on purpose with an approval.
- The same audience rule applies to `session_search`, `session_read`, resume, and delivery.
  A user finds only sessions whose audience includes the user. A job delivers to a shared
  target only output from a shared-audience run.

**Group toolset and approvers.** Every linked member of a group can steer the run of its
shared session, and every member of the workspace can read it. So a shared session has two
more rules than a private session.

1. **Group set.** A `chat` run of a shared session has the toolsets of
   `agent_profiles.group_toolsets`. The default list is in Section 10.2 and in
   `DEFAULT_GROUP_TOOLSETS` (`sama.admin.bootstrap`). The column default is `[]`, so a profile
   that has no list has no tool in a group (fail closed). A resume gets the same set, so a
   resume cannot widen it. The tool `ToolSearch` stays open, as in a private session.
   The prompt follows the set: a shared run has the memory snapshot only with `memory` in
   the set, and the skill index only with `skills` in the set. The skills of a shared run
   (the index, `skills_list`, `skill_view`, and skill commands) also check `requires_toolsets`
   against the group set, and not against `toolsets`. The prompt of a shared run
   also says that this is a group chat with a limited toolset. A `review` run does not use
   the group set (Section 9.4.1). Its tools are the profile toolsets cut to `memory` and
   `skills`, and its writes are proposals that a role decides.
2. **Approvers.** The user who started the run, the org admins, the org `owner`, and the
   `admin` and `owner` of the workspace can decide an approval of a shared session. Any other
   member gets a refusal from `GateService.decide_approval`, and the approval stays pending.
   The Telegram gateway shows the refusal as the answer of the button, and the Slack gateway
   whispers it to the user. The card of a shared session says who can decide: the person who asked, the people that
   the profile lists as approvers, and the admins of the workspace and of the org. The key
   `approvers` of `agent_profiles.policy` widens the set: a list of user ids adds these users,
   and `["*"]` lets each member who can see the session decide (the default of Hermes). A
   value that is not a list, and an item that is not a user id, adds nobody. A private session
   is not changed: only its owner sees it. A proposal of a background review keeps its role
   rules (Section 9.4.1).
3. **Memory asks.** A `memory` call in a shared session gets `ask` by default, because the
   text goes to the prompt of every run of the group. A policy rule still wins (for example
   `allow`), and an approval of the call allows it. A review run is not asked, because its
   shared changes are already proposals.

**Assumptions.** These choices are for the group toolset. Change them in a new revision of
this section.

- A1. `memory` is in the default group set, with the ask of rule 3. The tool only writes,
  because the snapshot loads in the prompt.
- A2. The approvers are the requester and the admins. This is stricter than Hermes, where
  each allowed user can approve (`approvals.approvers`, default `["*"]`). Hermes has no group
  toolset: its only group control is the approver list. The default of sama is stricter on
  purpose, and `["*"]` gives the Hermes behavior back. The org admins are in the set,
  because they see each workspace.
- A3. `github` is not in the default group set, so `repo_clone`, `pr_create`, and
  `pr_comment` are not available in a group. `skills` is not in it either, so `skill_manage`
  is not available and the skill index is empty. An admin adds a toolset to the group list
  of a profile on purpose.
- A4. Migration `0017` fills the group set of the profiles that exist with the default list,
  cut to the toolsets of each profile and in the order of the default list. So a group never
  gets a toolset that the profile does not have. For a profile with the default toolsets, the
  result is the default group set.
- A5. A review run ignores the group set. The group set would block the skill proposals of
  a shared session.
- A6. The skill index of a private session also needs `skills` in the toolsets now. Before,
  the index did not depend on the toolsets.

### 11.4 HTTP API and minimal web page (M1.12)

`sama.api.SamaApi` is the FastAPI app of the control plane. `python -m sama.api` starts it.

**Auth.** Each request except `/`, `/static/*`, `/config`, and `/healthz` needs a Supabase Auth
access token in `Authorization: Bearer`. `JwtVerifier` checks it:

- `HS256` only with the legacy JWT secret of the project (`SAMA_SUPABASE_JWT_SECRET`). `ES256`
  and `RS256` only with a key of the project JWKS
  (`<supabase>/auth/v1/.well-known/jwks.json`), found by `kid`. Thus a token cannot choose an
  HMAC check with a public key. Other algorithms, also `none`, are refused.
- The token needs `exp`, `sub` (a UUID), `aud = authenticated`, `role = authenticated`, and the
  issuer `<supabase>/auth/v1`. An anonymous user (`is_anonymous`) is refused.
- The JWKS is cached for 10 minutes. An unknown `kid` fetches it again, so a key rotation works
  without a restart. The verifier fetches at most once in 30 seconds, also after a failed
  fetch, so forged key ids cannot make a fetch for each request. If a fetch fails, the old keys
  stay. A token with a known key does not wait for a fetch that runs at that time.

**Two guards.** For each session the API reads as the user (`sama.db.as_user`), so RLS applies.
In the same transaction it finds the user row of the token in the org of the session and
checks the memberships with `sama.tenancy.access.Grants`, the code copy of the RLS helpers.
A session that the user cannot read gets `404`, the same answer as a session that does not
exist. Writes go through `RunQueue` and `GateService`, after both checks. The tests repeat the
cross-org check of Section 17.1 through the API.

| Method and path | Purpose |
|---|---|
| `GET /config` | The Supabase URL and the public anon key, for the web page. `signup` is `open` or `invite_only` (Section 17.2). `billing` is true when billing is on (Section 17.5). |
| `GET /me` | The orgs, workspaces, and agent profiles that the user can see. Each org has the `slug`, and the `role` and the `plan` of the user. |
| `POST /orgs` | Make an org with the user as the owner (`201`). `403` when sign-up is closed, the email is not confirmed, or the domain is blocked. `429` over a rate limit (Section 17.2). |
| `GET /orgs/{org_id}/members`, `PATCH`, `DELETE /orgs/{org_id}/members/{user_id}` | The members of the org. `PATCH` changes `role` or `workspace_ids` (an admin). `DELETE` removes a member (an admin) or leaves the org (the user). `409` for the last owner (Section 17.2). |
| `GET`, `POST /orgs/{org_id}/invites`, `DELETE /orgs/{org_id}/invites/{invite_id}` | The invites of the org, for an admin. `POST` takes `email`, `role`, and `workspace_id`, and gives the token one time (`201`). `429` over the plan caps (Section 17.2). |
| `GET`, `POST /orgs/{org_id}/api-keys`, `DELETE /orgs/{org_id}/api-keys/{key_id}` | The org API keys of sama as a server, for an org admin (`403` for a member). `POST` takes `name`, `workspace_id`, `agent_profile_id`, `scopes`, `approvals`, and `expires_in_days`, and gives the key one time (`201`). `DELETE` revokes the key (`204`). `409` over 50 active keys (Section 10.7). |
| `GET /billing/plans` | The price list: the free and paid plans, their prices, included usage, and quotas, and `billing_enabled`. No sign-in (Section 17.5). |
| `GET /orgs/{org_id}/billing` | The plan, the subscription, the usage of the period, and the quotas of the org, for an org admin (`403` for a member). |
| `POST /orgs/{org_id}/billing/checkout`, `PUT /orgs/{org_id}/billing/plan`, `POST /orgs/{org_id}/billing/portal` | An org admin starts Stripe Checkout for `pro` or `business` (`url` to open), changes the paid plan (Stripe prorates), or opens the Stripe customer portal. `409` when billing is off, for the `team` plan, or for a wrong state. `422` for a plan that is not for sale. `502` or `503` when Stripe does not answer or the prices are missing. |
| `POST /stripe/webhook` | Signed Stripe events (Section 17.5). `400` for a bad signature, `404` when billing is off, `503` when Stripe must send the event again. |
| `GET /invites`, `POST /invites/accept` | The open invites for the confirmed email of the user. Accept with `token` or `invite_id`. `403` for another email, `410` for an ended invite (Section 17.2). |
| `GET /sessions`, `POST /sessions` | List the active sessions that the user can read (at most 100). Create a `private` or `shared` session with a profile of the workspace. The audit row is `session.created`. |
| `GET /sessions/{id}` | The session, its last 20 runs, and `last_event_id`. A session has `lineage` and `parent_session_id` (null for a session with no parent). |
| `GET /sessions/{id}/messages` | The messages by `seq`, after `?after=`. |
| `POST /sessions/{id}/messages` | Queue a run (`202` with `run_id` and `queued`). `queued` is true when the run waits behind an open run of the session (M3.9). At most 32,000 characters. `429` over a rate limit. `402` with `detail` and `quota` when the org or the user used a model quota of the plan (Section 17.5). `409` for an archived session. |
| `POST /sessions/{id}/steer` | Add a note (`text`, at most 4,000 characters) to the open run (`202` with `steered: true` and `run_id`). With no open run, queue the text as a message (`steered: false`, `run_id`, `queued`). `409` when the run has 10 pending notes or the session is not active. `429` over a rate limit (Section 4.3, M3.9). |
| `POST /sessions/{id}/handoff` | Move the session to the direct chat of the user in `telegram`, `slack`, or `web` (`200` with `to_channel` and `text`). `409` with the reason when the move is not possible (Section 11.1, M3.9). |
| `GET /sessions/{id}/checkpoints` | The checkpoints of the session files, newest first (`number`, `created_at`, `tool`, `kind`). `?limit=` from 1 to 50, default 10. `409` without a session sandbox (Section 15.3, M6.4). |
| `POST /sessions/{id}/undo` | Restore the session files to before the last run that changed them (`200` with `created_at`, `files`, `deleted`, `kept`, and `text`). `409` with the reason when the session has an open run, no sandbox, or nothing to undo (Section 15.3, M6.4). |
| `POST /sessions/{id}/rollback` | Restore the checkpoint `number` (1 is the newest) of `GET /sessions/{id}/checkpoints`. The answer and the `409` reasons are the same as for `undo` (Section 15.3, M6.4). |
| `GET /sessions/{id}/coding`, `GET /sessions/{id}/plans`, `POST`, `DELETE /sessions/{id}/repos`, `PUT /sessions/{id}/plan-mode`, `POST /sessions/{id}/plan`, `POST /sessions/{id}/plans/{plan_id}/approve`, `/reject`, `GET /sessions/{id}/diff`, `GET /sessions/{id}/commands`, `GET`, `POST /sessions/{id}/pull-requests` | Coding mode: attached repositories, plan mode and plans, diffs of the checkpoints, commands and test runs, and pull requests (Section 15.5, M7.9). |
| `POST /sessions/{id}/compact` | Queue a `/compact` run (`202` with `run_id`). The body is optional: `focus`, at most 6,000 characters. `409` if the session is busy, not active, or has nothing to compact. `429` over a rate limit (Section 6.3, M2.6). |
| `GET /sessions/{id}/compactions` | The compactions of the session, newest first: `trigger`, token counts, the archive session, `completed_at`. |
| `GET /sessions/{id}/lineage` | The ancestors and the children of the session. A linked session that the user cannot read is not in the answer. |
| `GET`, `POST /sessions/{id}/pins`, `DELETE /sessions/{id}/pins/{pin_id}` | The context pins of the session. `POST` takes `text` or `message_id`. `409` for a full session or a message that is not in the session (Section 6.3). |
| `GET /sessions/{id}/events` | Server-sent events (below). |
| `POST /runs/{id}/cancel` | `RunQueue.cancel`. |
| `GET /sessions/{id}/approvals` | The pending approvals. |
| `POST /approvals/{id}/approve`, `/deny` | `GateService.decide_approval`, then `RunQueue.release_decided`. `always_allow` writes the user rule. `409` if the approval is decided or expired. |
| `GET /workspaces/{id}/memory`, `GET .../memory/{target}/revisions`, `PUT .../memory/{target}`, `POST .../memory/{target}/revisions/{seq}/revert` | The memory of the workspace (Section 8.1). `target` is `agent`, `user`, or `workspace`. The `agent` target needs `agent_profile_id` (query or body). `user` is the notes of the caller. A user who cannot see the workspace gets `404`. A `workspace` write needs a workspace admin (`403`). A change that is not valid gets `422`. |
| `GET /sessions/{id}/questions`, `POST .../questions/{request_id}/answer` | The open `clarify` questions of the session, when the process has a `ClarifyBroker` (Section 16.7). |
| `POST /orgs/{org_id}/agent-profiles` | Make an agent profile (an org owner or admin, `201`). `409` over 20 profiles (Section 17.6, M6.9). |
| `GET /orgs/{org_id}/usage`, `GET /orgs/{org_id}/audit`, `GET /orgs/{org_id}/curator-reports` | The admin views: usage, audit log, and curator reports (an org owner or admin, Section 17.6, M6.9). |
| `GET /orgs/{org_id}/session-search?q=&workspace_id=&limit=` | Search the sessions of the org that the user can read (M3.12, below). At most 10 hits. `q` has at most 400 characters (`422`). An empty `q` gives the latest sessions. |
| `GET /sessions/{id}/export?format=markdown\|json` | The transcript as a file to download (M3.12, below). |
| `GET /sessions/{id}/runs?limit=` | The runs of the session, newest first, with the token use and the cost. Default 50, at most 200. |
| `GET /runs/{run_id}/trace` | The steps, the tool calls, and the model calls of one run (M3.12, below). |
| `GET /workspaces/{id}/skills`, `GET .../skills/{name}?scope=`, `POST .../skills`, `GET /skills/{skill_id}/versions/{version}` | The skill editor of the web app (M3.12, below). |

**Event stream.** `GET /sessions/{id}/events` sends `text/event-stream`:

- `run_event`: one row of `run_events` (`run_id`, `seq`, `type`, `payload`). The SSE `id` is
  `run_events.id`. One worker writes the events of a session at a time, so the ids of a session
  grow in the order of the commits. A client continues with `Last-Event-ID` or `?after=`.
  Without a cursor the stream starts after the last stored event.
- `runs`: the first event of each stream. The status of every open run and of the 20 most
  recent runs. A client that connects again uses it to correct the runs that it shows as open:
  a status change while the client was not connected is not lost.
- `run`: the status of a run changed (`status`, `failure_code`, `failure_message`). A run that
  the stream saw open stays in the poll until the stream sends its end status, also when more
  than 20 newer runs are in the session.
- `end`: the user cannot read the session any more. The stream ends.

The stream reads new rows every 0.5 seconds, sends `: ping` every 15 seconds, and checks the
access again at each read. It ends after one hour, or when the access token expires if that is
earlier. The time limit covers the whole response, also a slow database read or a send to a
client that does not read: the response stops at most one second after it. The client then
connects again with a fresh token. One process has at most 1,000 open
streams, and one user at most 8 (`ApiSettings`). One more stream gets `429`. The limits are per
process.

**Web page.** The API serves a minimal page at `/` (`src/sama/api/static/`): sign-in, a session
list, new session, chat with streamed text, approval cards (Approve, Always allow, Deny),
`clarify` answers, and a stop button. It has no build step and no third-party code:

- Sign-in uses the Supabase Auth REST API: email and password
  (`/auth/v1/token?grant_type=password`), a sign-in link, and token refresh. A form for an
  access token helps in development. The tokens stay in `sessionStorage`.
- The sign-in link uses PKCE (`/auth/v1/otp` with `code_challenge`, then
  `/auth/v1/token?grant_type=pkce`). The page that asks for the link keeps the verifier in
  `localStorage` for at most one hour. A link that another person sends cannot sign this browser
  in to the account of that person. The page never takes tokens from the URL fragment.
- The stream uses `fetch`, because `EventSource` cannot send the `Authorization` header.
- The page sets text with `textContent` only. The Content Security Policy allows scripts only
  from the API origin and connections only to the API and the Supabase project.

The Next.js app in `web/` replaces this page in M3. The web app keeps the same API.

**Web app and CORS.** The Next.js app in `web/` runs on another origin than the API. It calls
the API from the browser with a Bearer token and no cookies. The optional setting
`SAMA_WEB_ORIGINS` lists the origins that may do this. It is a comma-separated list of exact
origins, for example `https://sama-web.vercel.app,http://localhost:3000`:

- Each origin is `http://host` or `https://host` with an optional port. It has no path, no query,
  and no `*`. The process refuses a bad value at start-up and names the variable.
- With the setting, the API answers a preflight request from a listed origin. It allows the
  methods `GET`, `POST`, `PUT`, `PATCH`, and `DELETE`, the headers `Authorization`,
  `Content-Type`, and `Last-Event-ID`, and caches the answer for 600 seconds. It never allows
  credentials. It exposes `Content-Disposition`, so the web app reads the file name of an
  export (M6.9). The event stream and error answers have the CORS header, too, also the answer
  to an unhandled error (`500`): the CORS layer is outside the layer that makes that answer.
- Without the setting (the default), the API sends no CORS header. The page at `/` does not
  need CORS, because it has the same origin as the API.
- `sama.api.__main__` and `sama.controlplane` both read the setting.

**Agent profile read and update.** `/me` lists the agent profiles that the user sees. The
settings page of the web app uses two more routes:

- `GET /orgs/{org_id}/agent-profiles/{profile_id}` returns the profile to each org member who
  can see it: `id`, `org_id`, `workspace_id`, `name`, `soul`, `runtime`, `model`, `toolsets`,
  `group_toolsets`, `subagents`, `updated_at`, `editable`, and `options`. A profile of a
  workspace is visible to the same users as in `/me`: an org owner or admin, and a member of the
  workspace. Any other user gets `404`, as for a profile that does not exist. `editable` is true
  for an org owner or admin. `options` has the data for the form:
  `toolsets` (each name of the fixed toolset table with its sorted tools), `models` (the models
  that have a price for the provider of the runtime), `subagents` (true if the runtime has
  native sub-agents), `runtimes` (each runtime that an admin can choose, with its `name`, its
  `models`, and its `subagents` flag, M3.6), and `limits` (`name_max_chars`, `soul_max_chars`, `max_subagents`, and the
  limits of the sub-agent spec).
- `PATCH /orgs/{org_id}/agent-profiles/{profile_id}` changes the profile. Only an org owner or
  admin can do it (`403` for another member). The body has `name`, `soul`, `runtime`, `model`,
  `toolsets`, `subagents`, and `library_subagents` (M7.4, Section 13.3). Each field is optional, an unknown field is an error, and the body has at
  least one field. The answer has the same body as the `GET`. The `GET` also has
  `library_subagents`: the ids of the library agents of the profile, in order.

The rules of an update:

- `name` has 1 to 80 characters after the spaces at both ends are removed. `soul` has at most
  20,000 characters. The API refuses a soul that has a secret (`422`). It does not run the
  injection scan on the soul: the soul is the instruction of the admin.
- `runtime` must be `claude_agent_sdk` or `openai_agents_sdk` (M3.6). On a switch, the model
  (the new `model`, or the stored model when the body has none) must be a model of the new
  runtime, else the API gives `422` ("choose a model of the new runtime"). The stored
  sub-agents must fit the new runtime: a model of a sub-agent must be a model of the new
  runtime (both runtimes have native sub-agents since M7.4). A review model that the new runtime does not know goes back to
  the default (null).
- A switch applies to new sessions. A session keeps the runtime of its frozen config. A chat
  run of a session with no frozen config takes the runtime of the profile when the worker
  claims it, not when the API queues it, so its runtime and its model are from the same
  profile. A review run keeps the runtime that it got in the queue. It uses the review model
  of the profile only if that runtime knows the model, else the default review model of the
  runtime.
- `model` must be one of the models of the runtime (the new runtime on a switch). `toolsets` have no duplicates, and each one is in the
  fixed toolset table. `subagents` replaces the whole list. The same code as `python -m
  sama.admin subagents set` checks it (`sama.subagents.validate_subagents`), and its error text
  is safe to show.
- The API checks the sub-agents against the new toolsets in the same transaction. An admin
  cannot remove a toolset that a sub-agent uses, and cannot remove the toolset `subagents`
  while the profile has sub-agents (`422`).
- The transaction locks the row (`SELECT ... FOR UPDATE`), sets `updated_at`, and writes one
  audit entry: `action` `profile.updated`, `target_type` `agent_profile`, and `details` with
  `fields` (the names of the changed fields) and `subagents` (the names of the sub-agents, when
  the list changed). The entry never has the soul or a prompt. A request that changes nothing
  writes nothing.
- The read goes through the same guards as the session routes (code and RLS). The write uses
  the table owner, as the other writes do, after the code checked the role.
- A session keeps the soul, the runtime, the model, and the sub-agents that it froze. A change
  to them applies from the next session. A run uses the runtime of `sessions.run_config` when
  it has one, and else the runtime of the profile. Since M6.1, the gate uses only the toolsets
  that both the profile of now and the snapshot of the session have (Section 16.1). So a new
  toolset applies from the next session, and a removed toolset applies at the next tool call,
  also in a session that is running. This keeps a revoked toolset immediate (Section 5.1). The
  web app shows this note.
- A change to the soul or to the sub-agents needs a redactor that knows every secret of the org,
  as `python -m sama.admin subagents set` does (`sama.vault.standalone.know_the_secrets`). It
  fails closed with `503`, and the message has no secret, when the org has vault entries but the
  process has no `SAMA_VAULT_KEY`, or when the vault cannot be read or an entry does not decrypt.
  A change to the name, the model, or the toolsets does not need it.
- The write checks the role again after it locks the profile row, and it locks the membership
  row of the admin (`FOR SHARE`) until the end of the transaction. An admin who loses the role
  while the write waits for the lock gets `403`, and nothing is written.

In-place edits by an admin are the M3.8 interim. Since M3.2, the agent proposes a new SOUL with
`change_propose`, and since M6.1 also a change of the settings, the toolsets, and the policy
rules of the profile (Section 9.4.2). The change applies when a person approves the card.

**History and revert (M6.1).** Each edit writes a history row (Section 9.4.2), one row for each
kind that it changes.

- `GET /orgs/{org_id}/agent-profiles/{profile_id}/changes` returns the newest 50 changes of the
  profile to each member who can see the profile (`404` for another user). Each change has
  `id`, `kind`, `source`, `summary`, `before`, `after`, `diff`, `actor_user_id`, `actor_name`,
  `proposal_id`, `reverts_change_id`, `reverted_by_change_id`, `redacted`, and `created_at`.
  The answer is redacted with the secrets of the org.
- `POST /orgs/{org_id}/agent-profiles/{profile_id}/changes/{change_id}/revert` writes the
  `before` values of the change again, as a new change with the source `revert`. Only an org
  owner or admin can do it (`403`). It needs the redactor of the org, as an edit does (`503`).
  It gives `404` for an unknown change, `409` when the change is already reverted, when the
  change is redacted, or when the profile does not have the `after` values of the change now, and `422` when the old values are
  not valid now. The answer has the `id` of the new change and the profile.
- The settings page of the web app has a History section below the form. It shows each change
  with its diff, who made it, and when. An org owner or admin has a Revert button with a
  confirm step. A redacted change shows a note in place of the button. The web app has no editor for policy rules, so a policy change shows only in
  the history.
- `python -m sama.admin subagents set` and the reset of the review model on a runtime switch do
  not write a history row.

**Telegram pairing.** `POST /channels/telegram/pairing-codes` with `workspace_id`,
`agent_profile_id`, and `kind` (`user` or `group`) makes a pairing code (Section 11.5). It does
the same access checks as `POST /sessions`. The code is for the Telegram bot of the org of the
workspace. The answer has the bot username and a `t.me` link that opens the bot with the code
(`?start=` for a `user` code, `?startgroup=` for a `group` code). It returns `409` when the org
has no active Telegram integration (Section 11.6) and `429` when the user has 5 open codes.
`GET /channels/telegram/identities` lists the Telegram accounts of the user, and
`DELETE /channels/telegram/identities/{id}` unlinks one. `/config` gives `integrations: true`
to the page when the process can connect integrations. `/me` gives, for each org, `admin`
(the user is an org owner or admin) and the integrations of the org (kind, status, name).

**Web app v1 rest (M3.12).** These routes serve the memory, skills, search, runs, and settings
pages of the web app. They use the same guards as the session routes (code and RLS):

- **Session search.** `GET /orgs/{org_id}/session-search` uses the same index as the
  `session_search` tool (Section 8.3). The visibility rule is the rule of a private run: the
  shared sessions of the workspaces that the user sees, and the private sessions of the user.
  `workspace_id` limits the search to one workspace. A user who is not an org member, or who
  cannot see the workspace, gets `404`. An archive session (Section 6.3) is not a hit. Each hit
  has the session id, the workspace, the title, the audience, the times, and the snippets.
- **Export.** `GET /sessions/{id}/export` returns `format=markdown` (`text/markdown`) or
  `format=json` (`application/json`, `"format": "sama.session.v1"`). The file has the session
  data and the messages by `seq`. The API reads the messages in batches of 200. It stops after
  5,000 messages, or after the message that brings the text over 4,000,000 characters. Then
  the file has a note and `truncated: true`. The answer has `Content-Disposition: attachment` with the name
  `session-<first 8 characters of the id>.md` or `.json`, and `Cache-Control: no-store`. The
  redactor runs again on each message.
- **Runs and traces.** `GET /sessions/{id}/runs` gives `kind`, `status`, `compact`, the times,
  the failure, and the sum of `model_calls` (tokens, cost, count). `GET /runs/{run_id}/trace`
  needs read access to the session of the run, else `404`. It gives the run, its `run_events`
  as steps (at most 2,000, then `truncated: true`), its `tool_calls` (at most 500), and its
  `model_calls` (at most 500). Adjacent `text_delta` events become one step with at most 20,000
  characters. The redactor runs again on each payload and on the whole joined text, because a
  secret can span two deltas. The cut comes after the redaction and ends at a space. When the
  events are cut and the last step is text, that step is not in the trace, because the rest of
  its text is not there and the redactor cannot see a whole secret. Thus a trace never shows
  the start of a secret. The trace never has `runtime_state`.
- **Skill editor.** `GET /workspaces/{id}/skills` lists the skills that the user sees in the
  workspace (Section 9.4) with `can_edit` and `writable_scopes`. `GET .../skills/{name}` gives
  the `SKILL.md`, the files, and the other scopes that have the same name. `POST .../skills`
  takes `action` (`create`, `edit`, `write_file`, `remove_file`, or `archive`), `name`, `scope`
  (`user`, `workspace`, or `org`), and the content. The write goes through
  `PostgresSkillStore` with the same lint, scan, and role checks as `skill_manage`, and the
  version has `author_kind` `user`. A write without the role gets `403`, and a change that is
  not valid gets `422`. A `builtin` skill is read only. `GET /skills/{skill_id}/versions/{version}`
  gives the `SKILL.md` and the files of one version. A skill has no revert: the web app loads an
  old `SKILL.md` into the editor, and the user saves it as an edit. Without a skill store, the
  routes return `409`.

The web app routes are `/search`, `/memory`, `/skills`, `/settings`, and `/s/{id}/runs`. The chat
header has the export buttons and a link to the runs. The settings page shows the account, the
chat apps of each org (an admin connects and disconnects the Telegram bot there), the linked chat
accounts, and the agents. Since M6.9 an admin also connects the Slack app there, and the model
keys moved to the admin area (Section 17.6).

**Not in M1.12.** `python -m sama.api` runs only the API. The control plane process (Section
19.1) puts the API, the worker, and the tool server in one process, so that the API answers
the `clarify` questions of the tool server. Org API keys (M7.6) open only `/v1` and `/mcp`
(Section 10.7), not these routes.

### 11.5 Telegram gateway (M1.13)

`sama.gateway.telegram.TelegramGateway` is the Telegram adapter. One gateway serves the bot of
one org. The Telegram hub (Section 11.6) builds a gateway for each connected bot. The control
plane process serves the hub. `python -m sama.gateway` serves only the hub, on port 8081, with
`SAMA_DATABASE_URL` and `SAMA_VAULT_KEY`. A worker must run too: the gateway only queues runs
and reads their events.

**Tables (migration `0008`).**

| Table | Key columns |
|---|---|
| `channel_identities` | `org_id`, `user_id`, `channel`, `integration_id`, `external_id` (the Telegram user id, unique per integration), `workspace_id`, `agent_profile_id` |
| `channel_groups` | `org_id`, `channel`, `integration_id`, `external_id` (the chat id, unique per integration), `workspace_id`, `agent_profile_id`, `created_by_user_id` |
| `pairing_codes` | `org_id`, `user_id`, `channel`, `integration_id`, `kind` (`user` or `group`), `code_hash` (SHA-256, unique), `workspace_id`, `agent_profile_id`, `expires_at`, `used_at` |

Migration `0011` adds `integration_id` (Section 11.6). A delete of the integration deletes its
links and codes.

RLS: a user reads only their own identities and codes. A member of the workspace reads its
groups. Only the control plane writes these tables.

**Pairing.**

1. The user picks a workspace and an agent profile on the web page and makes a code. The code
   has 8 characters without `0`, `O`, `1`, and `I`. It expires after 10 minutes and works once.
   The table keeps only the SHA-256 digest. A user has at most 5 open codes. A row lock on
   the user keeps this cap when two requests come at the same time.
2. A `user` code: the user sends `/start <code>` (or only the code) to the bot in a direct chat.
   The `t.me` link of the code does this in one tap. The Telegram account then links to the
   user, the workspace, and the profile. A code works only with the bot of its org. One
   Telegram account has one link for each bot. A new code moves the link.
3. A `group` code: the user adds the bot to a group and sends `/pair <code>` there. Only the
   linked Telegram account of the user who made the code can use it. The group then links to
   the workspace and the profile of the code.
4. Each use checks again that the user sees the workspace and that the profile is in it.
   Links and unlinks write `channel_identity.linked`, `channel_group.linked`, and
   `channel_identity.unlinked` to the audit log.
5. Five wrong codes from one Telegram account lock it for one hour. All accounts together get
   at most 60 tries a minute. The gateway counts each try as wrong before it checks the code,
   and clears the count of the account when the code is right. Thus tries at the same time
   cannot pass the limit. The counts are in memory, per gateway process.

**Inbound.** Telegram sends each update to `POST /telegram/<integration id>/webhook` with the
secret of the bot in `X-Telegram-Bot-Api-Secret-Token`. The hub compares its digest in constant
time (Section 11.6). The gateway drops a repeated
`update_id`, answers at once, and handles the update in the background. Updates of one chat
run one at a time. Messages from bots and from other chat types are dropped. In a group the
bot reacts only to a command, an `@` mention of the bot, or a reply to a bot message. A voice
note of at most 5 minutes and 10 MB is downloaded and transcribed (OpenAI `whisper-1`). A
photo, or a document with an image type, is downloaded and stored as an attachment. A message
with an image and no text is a valid message (Section 11.7).

**Access.** An unknown sender gets a pairing prompt in a direct chat and nothing in a group.
For each message the gateway loads the grants of the user again. A user who lost the workspace
gets a refusal. In a group, the sender must be linked to the same bot. All links of a bot are in
the org of the bot.

**Sessions.** A direct chat is a `private` session of the user. A group is a `shared` session
of the group workspace. The binding is `session_bindings` with `channel = telegram` and
`external_key = <integration id>:chat:<chat id>`, because a chat id is unique only for one
bot. The gateway reuses the bound session only when it is active,
in the same org, workspace, and audience, and visible to the user. Otherwise it makes a new
session (audit `session.created` with `channel: telegram`). Sessions that Telegram made get a
channel hint in the volatile prompt tier (Section 6.1).

**Commands.** `/new` starts a new session with the same profile. `/stop` cancels the open runs
of the session, and `/stop NUMBER` cancels one (M7.1). `/bg TASK` starts a parallel run. `/model` lists the profiles of the workspace. `/model <name>` starts a child
session (`lineage = branch`) with that profile and binds the chat to it. `/help` lists the
commands. Since M2.6, `/compact [focus]` makes the context of the session shorter (it follows
the busy rule of Section 6.3 and answers with the token counts), and `/pin <text>`, `/pins`,
and `/unpin <number>` manage the context pins. They work in direct chats and in linked groups.
Since M3.9, `/steer <text>` adds a note to the open run, and `/handoff slack` or `/handoff web`
moves a private session from the direct chat (Section 11.1). Since M6.4, `/undo` and
`/rollback [number]` restore the session files from a checkpoint. They work only in a direct
chat (Section 15.3).

**Outbound.** For each run the gateway reads the run status and then the new `run_events`.
Text deltas go into one reply message that the gateway edits at most once in 1.5 seconds.
After 4,000 characters the reply goes on in a new message, cut at a line end or a space.
Replies are plain text, without `parse_mode`. When the run pauses, the gateway sends one
approval card per pending approval, with the tool name, the reason, the arguments (cut at
1,500 characters), and the buttons Approve and Deny. A failed run gets its failure message.
A stopped run gets a notice. A run without text deltas gets its stored answer.

Before each Bot API call that sends or edits output of a run, the gateway checks the chat
again. A direct chat must still
link to the user and org of the run, and the user must still see the workspace. A group must
still link to the org and workspace of the run. If a check fails, the gateway stops the
delivery. Thus an unlink or a new group link stops the output of runs that are open.

When Telegram answers `429 Too Many Requests`, the gateway waits `retry_after` seconds and
tries again, at most 3 times and only for waits of 30 seconds or less. For run output, the
gateway checks the chat again after each wait.

One gateway process follows at most 200 runs. When all places are in use, the gateway does not
queue the message and asks the chat to send it again later. A message takes its place before
it queues the run.

**Approval buttons.** The callback data is `ap:<approval id>:y` or `ap:<approval id>:n`. The
gateway finds the linked user of the presser, checks that the user can see the session, calls
`GateService.decide_approval`, releases the run, and edits the card to show the decision and
the person. In a group, `decide_approval` also checks the approvers (Section 11.3). If the
presser is not an approver, the card does not change and the presser sees the refusal. The card
of a group names who can decide.

**Limits of M1.13.**

- The lockout, the repeat filter, and the run followers are in memory. A restart of the gateway
  loses the followers: the runs go on, but their replies do not reach Telegram.
- `clarify` questions do not show in Telegram yet.
- Voice notes use the OpenAI key of the org in the vault, else the `OPENAI_API_KEY` of the
  process. Without a key, voice notes are off. The key never goes to a sandbox.
- `httpx` logs request URLs, and a Telegram URL holds the bot token. The control plane and
  `python -m sama.gateway` set the `httpx` logger to `WARNING`.

### 11.6 Integrations

An **integration** connects a chat app to an org: the Telegram bot of the org or the Slack app
of the org (M3.1). Any org admin connects one from the web page, without the operator. The secrets
of an integration are in the vault (Section 16.3). `SAMA_VAULT_KEY` and `SAMA_PUBLIC_URL` turn
integrations on. Without them the routes answer `409`.

**Table `integrations` (migration `0011`).**

| Column | Meaning |
|---|---|
| `org_id`, `kind` | `telegram`, `slack`, `whatsapp`, or `email` (Section 11.9). One integration of each kind per org (unique). |
| `status` | `pending`, `active`, or `failed`. Only an `active` integration makes pairing codes. |
| `external_id`, `name` | The bot id and username (Telegram), or the team id and name of the Slack workspace (Slack). One bot or Slack workspace belongs to one org (unique per kind). A `pending` Slack row has no `external_id`. |
| `secret_hash` | The SHA-256 digest of the webhook secret (Telegram) or of the signing secret (Slack), in hex. The row has no clear text secret. |
| `last_error`, `created_by_user_id`, `settings` | The last connect error, the admin, and options. A Slack row keeps `bot_user_id`, `bot_id`, and `app_id` in `settings`. |

RLS: every member of the org reads the rows. Only the control plane writes them.
`channel_identities`, `channel_groups`, and `pairing_codes` refer to the integration with a
cascade delete. Migration `0011` deletes the links and codes of the old shared bot: users pair
again with the bot of their org.

**Routes.**

| Route | Who | Does |
|---|---|---|
| `GET /orgs/{org_id}/integrations` | A member of the org | Lists the integrations, without secrets. |
| `POST /orgs/{org_id}/integrations/telegram` with `bot_token` | An org owner or admin | Connects or replaces the bot of the org. |
| `POST /orgs/{org_id}/integrations/slack/setup` | An org owner or admin | Makes the `pending` Slack row (or keeps the row) and returns its request URLs, the app manifest, and a link that makes the app from the manifest. |
| `POST /orgs/{org_id}/integrations/slack` with `bot_token`, `signing_secret` | An org owner or admin | Connects the Slack app of the row. |
| `DELETE /orgs/{org_id}/integrations/{kind}` | An org owner or admin | Disconnects the integration. |
| `POST /channels/{channel}/pairing-codes`, `GET /channels/{channel}/identities`, `DELETE /channels/{channel}/identities/{id}` | A user of the workspace | Pairing for `telegram`, `slack`, `whatsapp`, or `email` (Sections 11.3 and 11.9). The answer has `link` and `command`: for Slack, a link that opens the app in Slack and `/sama pair CODE`; for WhatsApp, a `wa.me` link with the code; for email, a `mailto:` link with the code as the subject. |

A user who is not a member gets `404`. A member who is not an admin gets `403`.

**Connect Telegram.** The admin makes a bot with @BotFather (`/newbot`) and pastes the token on
the web page. `sama.integrations.Integrations`:

1. checks the shape of the token and asks Telegram for the bot (`getMe`). A wrong token gets
   `400`;
2. in one transaction: refuses (`409`) a bot that another org connected, deletes the old row
   when the org connects a different bot (its links go with it), writes the row as `pending`
   with the digest of a new random webhook secret, stores the token in the vault
   (`telegram_bot_token`), and writes `integration.connected` to the audit log;
3. registers `<SAMA_PUBLIC_URL>/telegram/<integration id>/webhook` with the secret
   (`setWebhook`). The status becomes `active`, or `failed` with `last_error`. A connect again
   with the same token makes a new secret and tries again.

**Disconnect** deletes the row (with its links and codes) and the vault entries in one
transaction, writes `integration.disconnected`, and then deletes the webhook at Telegram (best
effort).

**Connect result on the web page.** The web page keeps the last connect result of each org and
kind in memory. It shows the result in the status line of the form after the card renders
again. A disconnect or a sign-out clears the result.

**Changes at the same time.** A connect or a disconnect holds two Postgres advisory locks
(transaction locks on their own connection): one for the org and kind, then one for the bot.
It holds them from its first database write to the end of its Telegram calls. A second change
of the same org or the same bot, also in another process, gets `409` at once and does not
wait, so it does not keep a pooled connection that the first change needs. Thus the stored
digest is always the digest of the last secret that Telegram got. The lock transaction sets
`idle_in_transaction_session_timeout` to 5 minutes, which is longer than its Telegram calls. The last status update also
matches only the digest that this connect registered. The page asks the admin to confirm a
replace, because a different bot deletes the links of the old bot.

**Telegram hub.** `sama.integrations.TelegramHub` serves
`POST /telegram/<integration id>/webhook`. For each update it reads the row and compares the
SHA-256 digest of the given secret with `secret_hash` in constant time, also for an unknown id.
An unknown id, a wrong secret, or a `failed` integration gets `401`. The hub keeps one gateway
for each bot and builds it on the first update with the token from the vault. It reads the
token from the table and not from the vault cache, because another process can have replaced
the bot a moment ago. A token of a different bot than `external_id` gets `503`. A new connect
makes a new secret, so a gateway with an old digest is built again. The process that changes an
integration also drops the gateway at once. An old gateway ends its open work in the background.
All gateways share one pairing lockout and one limit of 200 followed runs.

**Connect Slack.** A Slack app needs the request URLs of sama before Slack installs it, so the
connect has two steps:

1. the admin clicks "Start Slack setup". `setup_slack` makes a `pending` row (audit
   `integration.created`) and returns `<SAMA_PUBLIC_URL>/slack/<integration id>/events`,
   `.../interactions`, and `.../commands`, and a manifest with these URLs, the bot scopes
   (`app_mentions:read`, `chat:write`, `commands`, `files:read`, `files:write`, `im:history`,
   `im:read`, `im:write`, `users:read`), the events `app_mention` and `message.im`, and the `/sama` command. The link
   opens `https://api.slack.com/apps?new_app=1&manifest_json=...`. The admin picks the Slack
   workspace, makes the app, and installs it. When Slack checks the events URL, the `pending`
   row answers the `url_verification` challenge;
2. the admin pastes the bot token (`xoxb-`) and the signing secret. `connect_slack` checks their
   shape and asks Slack for the workspace and the bot of the token (`auth.test`, then
   `bots.info` for the app id). In one transaction it refuses (`409`) a Slack workspace that
   another org connected and a connect without a setup, deletes the links and codes of the row
   when the token is of another Slack workspace, sets the row `active` with the team, the
   digest of the signing secret, and `settings`, stores both secrets in the vault
   (`slack_bot_token`, `slack_signing_secret`), and writes `integration.connected`. The row keeps
   its id, because the URLs in the Slack app hold it. When the new bot token is not the old
   one, sama then revokes the old token (`auth.revoke`, best effort).

A connect holds the advisory locks of the org and kind and of the Slack workspace. A disconnect
deletes the row, its links, and the vault entries, and then revokes the bot token
(`auth.revoke`, best effort).

**Slack hub.** `sama.integrations.SlackHub` serves the three request URLs. For each request it
reads the row. An unknown id gets `401`. A row that is not `active` answers only the
`url_verification` challenge. For an `active` row the hub builds one `SlackGateway` with the
secrets from the vault (not from the cache), and keeps it while `updated_at` of the row stays
the same. A signing secret with a digest that is not `secret_hash` gets `503`. The hub checks
`X-Slack-Signature` (HMAC-SHA256 of `v0:<timestamp>:<raw body>`) in constant time, and refuses a
timestamp more than 5 minutes from now (`401`). The hub reads the body in chunks and stops at
1 MB (`413`), also when the request has no `Content-Length`. A request
of another Slack workspace than the row is dropped. Events and button clicks run in the
background, so Slack gets its answer at once. A repeated event (same integration and
`event_id` in the last 10 minutes) is dropped. The hub keeps this log for all its gateways, so a
gateway that is built again after a reconnect also drops a repeat. A `/sama` command gets an
answer that only the sender sees.

**Slack gateway.** `sama.gateway.slack.SlackGateway`:

- a direct message with the app is a `private` session of the linked user. An unknown user
  gets a pairing prompt. A pairing code in a direct message links the Slack account;
- in a channel the app answers only a mention. A linked channel (`/sama pair CODE` with a group
  code, from a linked user) maps each thread to a `shared` session of the channel workspace,
  and the app answers in the thread. An unknown user or a channel that is not linked gets a
  message that only the sender sees;
- `/sama new` (direct messages; it takes the lock of the direct message, so a message at the
  same time cannot bind another session), `/sama stop` (the direct message, or all threads of the
  channel), `/sama pair CODE`, and `/sama help`. Since M2.6, `/sama compact [focus]`,
  `/sama pin TEXT`, `/sama pins`, and `/sama unpin NUMBER` work in direct messages (Section 6.3);
- since M3.9, `/sama steer TEXT` and `/sama handoff telegram|web` work in direct messages. In a
  thread, a mention with `/steer TEXT` adds a note to the run of the thread (Section 11.1);
- the gateway escapes `&`, `<`, and `>` in all text that it sends, so the text of a model
  cannot mention people or channels. Approval cards use `plain_text` blocks;
- before each send it checks the link again, as the Telegram gateway does (Section 11.3). In a
  worker bot channel it checks that the same bot still has the channel;
- approval cards have the buttons Approve, Deny, and "Always allow for me" (a proposal card and
  a card for a tool in `ALWAYS_ASK` have no "Always allow for me"). Section 9.4.2 has the rules.

**Shared sessions in Slack (M3.3).** A channel thread is a `shared` session (Section 11.3), and
the same rules hold as for a Telegram group:

- the run of a shared session has no user in its run token. So it does not load `user` memory,
  user skills, or personal connections, and `session_search` and `session_read` see only the
  shared sessions of its workspace (Section 7.1). A direct message is a `private` session of the
  linked user, and it gets the `user` memory of that user;
- the answer of a run goes only to the thread of the session, and only while the channel is still
  linked to the same workspace (or has the same worker bot). A run of a private session goes
  only to the direct message of its user, while the account is still linked;
- a test (`tests/gateway/test_slack_m3.py`) shows that the `user` memory of a member is in the
  frozen prefix of the direct message and not in the frozen prefix of a channel thread.

The Telegram and Slack gateways share the session, follow, and approval code
(`sama.gateway.sessions`).

**Images.** The scope `files:read` lets the gateway download the files of a message. A message
with an image is valid without text. Section 11.7 has the rules. An app that was connected
before M2.8 needs the new scope: the admin adds `files:read` to the app and installs it again.
Without the scope, the download fails and the sender gets the message that sama cannot read
the image.

**Generated files.** The scope `files:write` lets the gateway upload the images and the audio
that `image_generate` and `text_to_speech` make (Section 11.8). An app that was connected before
M4.9 needs the new scope: the admin adds `files:write` to the app and installs it again. Without
the scope, the upload fails, the file stays in the API, and the reply text still goes.

**Limits.** Slack has no voice notes and no `/model` command. In a channel thread, each message
to the app needs a mention. The pairing lockout and the limit of 200 followed runs are for each
hub: the Telegram hub and the Slack hub each have their own.

**Worker bots (M3.10).** A worker bot is an agent profile that is bound to one Slack channel, with
a daily budget in USD. Code: `src/sama/workerbots.py`. Migration `0021`.

| Column of `worker_bots` | Meaning |
|---|---|
| `org_id`, `workspace_id` | The workspace of the bot. Its sessions are shared sessions of this workspace. |
| `agent_profile_id` | The profile of the bot: a profile of the workspace or of the org. |
| `integration_id`, `external_id` | The Slack integration of the org and the channel id. One bot per channel (unique). |
| `name`, `daily_budget_usd`, `status` | A name of 1 to 80 characters, a budget above 0 and at most 10000, and `active` or `disabled`. |
| `created_by_user_id`, `created_at`, `updated_at` | The admin and the times. |

- RLS: a user who can see the workspace reads its bots (`sama_private.can_see_workspace`).
  Writes go through the API as the table owner.
- API: `GET /workspaces/{id}/worker-bots` (each member of the workspace; each bot has
  `spent_today_usd`), `POST` (`name`, `agent_profile_id`, `channel_id`, `daily_budget_usd`),
  `PATCH /{bot_id}` (any of these fields and `status`), and `DELETE /{bot_id}`. Only a workspace
  admin, the org owner, or an org admin writes; another member gets `403`. The org needs an
  active Slack integration (`409`). A second bot for the same channel gets `409`. Each write adds
  an audit row `worker_bot.created`, `worker_bot.updated`, or `worker_bot.deleted`.
- A mention of the app in the channel of an `active` bot starts a shared session with the profile
  and the workspace of the bot, also when a group pairing links the channel: the bot wins. The
  sender must be a linked user who can see the workspace. The session keeps the id of the bot
  (`sessions.worker_bot_id`).
- The bot also wins for delivery. When a channel has a bot, the gateway posts only the output of
  the sessions of that bot to the channel. A run that started from the group pairing before an
  admin bound the bot posts nothing more to the channel. A session of a bot that is removed or
  bound to another channel also posts nothing.
- The cost of a bot in one day is the sum of `model_calls.cost_usd` of the sessions of the bot
  since 00:00 UTC. The gateway does not start a run when the bot spent its budget. The sender
  gets a message that only the sender sees: "This worker bot reached its daily budget. It works
  again after 00:00 UTC."
- `load_authority` lowers the cost limit of a run of a bot session to the rest of the budget
  (Section 16.1), so the model proxy and the gate stop a run at the limit. The rest is the budget
  less the cost of the other runs of the bot today. A run that started before 00:00 UTC also
  keeps its cost from before 00:00 UTC: the limit adds that cost, because the run limit compares
  with all the cost of the run. So the run gets the full budget of the new day.
- The model proxy writes its pending usage records before it reads the authority of a call. So
  the cost of a call of one bot run applies to the next call of every run of that bot. A bot that is disabled or removed, or that
  spent its budget, stops its runs ("the worker bot is disabled or removed", "the worker bot
  reached its daily budget").
- `/sama stop` in a bot channel stops the runs of the bot threads.

**Not in this step.** Integrations for each workspace, more than one app of a kind per org,
Slack OAuth distribution (one app for many Slack workspaces), and a check that the webhook still
works after the connect.

### 11.7 Images from channels and `vision_analyze` (M2.8)

A user can send an image to sama in Telegram or Slack. The gateway stores the image, tells the
agent that it exists, and the agent looks at it with the tool `vision_analyze` when it needs to.
The code is in `src/sama/media/`.

**Inbound rules.** The Telegram and the Slack gateway use the same code (`sama.media.images`).

- Accepted: a Telegram photo, a Telegram document with an image type, and a Slack file with an
  image type. The types are PNG, JPEG, GIF, and WebP. Other files are ignored, as before.
- An image is at most 10 MB. A message has at most 4 images. A larger image, another image
  type (for example HEIC or SVG), too many images, a download that fails, and bytes that do not
  match the type get a short plain-text answer, and the whole message stops. The answer says
  only what the sender already knows.
- The gateway does not trust the type that the channel gives. It reads the first bytes of the
  file, and they must match the type (PNG, JPEG, GIF, or WebP signature).
- A message with an image and no text is valid. The text of the message is the caption (or the
  text) and one line for each image: `[image attached: <id> <name>; use vision_analyze to look
  at it]`. The line is the only change to the text. The name is cleaned to letters, digits,
  `.`, `_`, and `-`, and cut to 80 characters.
- The gateway checks the size limit before it downloads (the size that the channel states) and
  while it downloads (a byte cap). The Slack download sends the bot token only to an `https`
  URL on `slack.com` or `slack-files.com`, and does not follow a redirect.
- The byte cap counts the bytes on the wire (`sama.gateway.download`, used for Slack and
  Telegram). The request has `Accept-Encoding: identity`. An answer with another
  `Content-Encoding` is refused before the gateway reads a byte, so no decoder runs and a small
  compressed file cannot grow past the cap. The gateway checks the cap before it adds a chunk,
  so a file of exactly the limit is accepted and a file of one byte more is refused.
- The gateway makes the id of each image first, so that the text can name it. `RunQueue.enqueue`
  then stores the message and its images in one transaction. A failed store leaves neither.

**Storage (migration `0015`).** The table `attachments` has one row for each image: `id`,
`org_id`, `session_id`, `message_id`, `name`, `mime_type`, `size_bytes`, `sha256`, `data`
(`bytea`), `created_at`. A foreign key on `(org_id, session_id, message_id)` ties the row to its
message, so an image always belongs to the session of its message, and a delete of the session
deletes the images. Checks limit the type to the four image types and the size to 1 byte to
10 MB, and the size must equal the length of `data`. RLS follows the session
(`can_read_session`). The grant to `authenticated` is a column grant without `data`, so the Data
API never gives the bytes. The same migration adds `media` to the default toolsets of the first
org (an exact match, like `0014`).

**The tool.** `vision_analyze(attachment_id, question)` is in the toolset `media`. The gate
allows it by default (Section 16.1): it reads only an image of the run session, and the budget
of the run limits its cost.

1. It finds the image. The scope is the org and the session of the run token, never an argument.
   `latest` is the newest image of the session. An id of another session, another org, or no
   image at all gives the same answer: `The image was not found`.
2. It reads the Anthropic key of the org (the vault first, then `ANTHROPIC_API_KEY`). Without a
   key, the tool answers with a plain text that image analysis is off. This is not an error.
3. It checks the live authority and the budget of the run, the monthly limit of the org, and
   the daily limit of the user, as the model proxy does (Section 16.6). It fails closed. First
   it tries again to write the records that the shared ledger holds. If the ledger still holds
   a record of the org, the tool refuses the call (see "Accounting" below).
4. It sends the image (base64) and the question to the Anthropic Messages API, with a limit of
   1,024 answer tokens and a time limit of 45 seconds. The question goes through the redactor
   first. The system text says that text in an image is data.
5. It records the call in `model_calls` (provider `anthropic`, endpoint `messages`), so the
   cost counts for the run, the user, and the org.
6. The answer goes through the redactor as a whole, and only then is it cut to 8,000
   characters. So a secret that crosses the cut cannot leave as a partial value. The answer is
   untrusted content (Section 16.5): it goes in an
   `<untrusted_content source="vision_analyze">` block, and the redactor reads it like the
   result of every tool.

The service gives the tool a result with a status, `ok` or `unavailable`. Only the status
decides how the tool shows the text. `unavailable` (no key) is plain text. Each text from the
provider is always in the untrusted block, also when it is equal to the no-key text.

**Accounting.** Each call is paid, also when the caller goes away.

- Each call has its own `call_id`. The provider call and the write of its record run in one task
  that a cancel of the caller does not stop (`asyncio.shield`). A cancel of the run, a time
  limit, or a stop of the tool server cannot lose the record.
- The record has the real usage of the answer. It has an estimate (`usage_estimated`) if the
  caller is cancelled, the call times out, the connection breaks after the request was sent, or
  a `200` answer has no usage. The estimate has `status_code` 0 (no answer) or 200. The input of
  the estimate is a fixed 8,000 tokens for the image, plus the text bytes divided by 2. The
  output is the limit of 1,024 tokens. The estimate does not use the bytes of the image, because
  that would give millions of tokens for a base64 string.
- A request that never reached the provider (a connect error or a pool time-out) has no record,
  because it cost nothing. A status other than 200 has no record either.
- A record is pending from the start of its settlement (when the provider answers, or when the
  call is cancelled or fails) until its write succeeds. A pending record blocks new calls of its
  org, like a fenced record, so a write that stalls without an error cannot open the budget. A
  budget check first waits for the pending writes of its own org, for at most 2 seconds. A normal
  write takes milliseconds, so concurrent calls of one org do not refuse each other. A write that
  still runs after the wait refuses the call. Other orgs do not wait.
- Each write attempt has a time limit of 5 seconds (an attempt that the limit stops can have
  reached the database). The write is tried 3 times with the same `call_id`, so a retry cannot
  count a call twice. If all tries fail, or a cancel stops the write, the ledger fences the
  record. New calls of that org (of the model proxy and of `vision_analyze`) are refused until a
  later write succeeds. The retry of fenced records runs under a lock, with the same time limit.
  The budget check fails closed.
- `aclose` stops in a fixed time. The open calls get 5 seconds to end. The service cancels the
  rest, and each of them writes an estimate. A call that still writes after another 5 seconds is
  cancelled again: the cancel fences the record in the shared ledger, so the final flush and log
  of the model proxy handle it. Then the http client closes, also if a call did not end.

A failure of the model call becomes a short text without the key, the URL, or the body of the
provider: busy (`429`), refused key (`401`, `403`), bad image (`400`, `413`), no answer in time,
or not available.

**Assumptions.** These choices are for M2.8. Change them in a new revision of this section.

- A1. The tool sees only the images of its own session. A session that the user can read
  through search does not give its images (a later step can add this with the same rules as
  `session_read`).
- A2. The images stay in Postgres. At 10 MB and 4 images for each message, this is simple and
  keeps one transaction. A move to object storage is possible later: the table keeps the
  metadata and the hash.
- A3. The vision model is a separate Anthropic call of the control plane, not the model of the
  run. So the tool works with both runtimes, and the run does not need to carry the image in
  its context. The model is `claude-sonnet-5-5` (`SAMA_VISION_MODEL` changes it). The model
  needs a price in the price table, or the process does not start.
- A4. The answer and the question can have secrets that the redactor does not know. The
  redactor reads the question and the answer. It cannot read the pixels of an image.
- A5. A Telegram album arrives as one update for each photo, so it makes one run for each
  photo. The limit of 4 images is for one message.
- A6. Old attachments stay as long as the session does. The retention sweep deletes them
  with their session (M6.8, Section 17.4).
- A7. The Anthropic key of the org pays for the call. An org with no key has no image analysis.
- A8. The agent profile of an existing org keeps its own toolset list. Only the default list
  gets `media`. An admin adds `media` to a changed list. The group set (Section 11.3) is
  separate: migration `0017` fills it for each existing profile with the default group list, cut
  to the toolsets of the profile.
- A9. The fence and the list of pending records are in memory. A restart of the control plane
  loses them, the same as for the model proxy. A durable queue is in the backlog (Section 16.6).
- A10. A cancelled call is charged with an estimate that is higher than the usual cost of a
  description. We prefer to count too much than too little. The estimate of 8,000 image tokens
  is a fixed allowance for all images.
- A11. A call that fails after the provider read the request (a time-out, a broken connection)
  is charged with the estimate, because the provider can have counted it. A connect error is not
  charged.
- A12. The download code reads with the decoder of the http client, but it refuses every encoded
  answer first. So the code cannot decode. A server that sends only compressed files gives a
  failed download, and the sender sees the usual short answer.
- A13. A call that the provider has not answered yet is not in the budget. Calls of one org that
  run at the same time can pass the limit together by their cost, as in the model proxy
  (Section 16.6). The budget limits the calls that start after a record is pending or written.

### 11.8 Generated images and voice notes (M4.9)

Code: `src/sama/media/generate.py` and `src/sama/media/meter.py`. Migration `0027` adds the
table `generated_media`.

**Tools.** Both tools are in the `media` toolset and in the default allow list, like
`vision_analyze`. The budget checks still apply to each call.

| Tool | What it does |
|---|---|
| `image_generate(prompt, size)` | Makes one PNG image with the OpenAI image model (`gpt-image-2.6-flare`, quality `medium`). `size` is `1024x1024` (default), `1536x1024`, or `1024x1536`. The prompt has at most 4,000 characters. |
| `text_to_speech(text, voice)` | Makes one Opus audio file in an Ogg container with the OpenAI speech model (`tts-1`). The text has at most 4,096 characters. `voice` is `alloy` (default), `echo`, `fable`, `onyx`, `nova`, or `shimmer`. |

`gpt-image-1` shut down on 2026-10-23, so the image tool uses `gpt-image-2.6-flare`. OpenAI
shuts down `tts-1` on 2027-01-06. A change of the speech model before that date is a
separate item.

- The tools use the OpenAI key of the org from the vault. The key stays in the control plane.
  The sandbox, the logs, and the errors never have it. Without a key the tool answers that the
  feature is not available.
- Sama checks the bytes: a PNG signature for an image and an Ogg signature for audio. A file
  has at most 10 MB.
- The tool result tells the model that the file goes to the user with the reply. The result
  has no file bytes.

**Storage.** `generated_media` has `org_id`, `session_id`, `run_id`, `kind` (`image` or
`audio`), `mime_type`, `size_bytes`, `sha256`, `caption`, `data`, `delivered_at`, and
`created_at`. RLS lets a member read the metadata of the sessions that the member can see,
never the bytes: the column `data` is not in the grant. No policy allows a write. The control
plane writes as the table owner.

**Delivery.** When the run ends, the gateway that follows the run sends the files of the run
that are not sent yet, oldest first, at most 10 for each run:

- Telegram: an image is a photo (`sendPhoto`), and audio is a voice note (`sendVoice`). Both
  reply to the message of the user.
- Slack: the gateway uploads the file in the thread with `files.getUploadURLExternal`, a POST
  of the bytes, and `files.completeUploadExternal`. The upload URL must be `https` on a
  `slack.com` host. The POST of the bytes has no token. This needs the scope `files:write`
  (Section 11.6).
- A file that was sent gets `delivered_at`. A failed send stops the rest of the files of the
  run. They stay pending and are not sent again, but the HTTP API still gives them.
- HTTP API: `GET /v1/sessions/{id}/media` lists the files of a session (`limit` 1 to 100,
  default 50). `GET /v1/sessions/{id}/media/{media_id}` returns the bytes with the stored type,
  `Content-Disposition: inline`, and `Cache-Control: private, max-age=3600`. Both need access
  to the session.

**Money.** The `MediaMeter` does the checks of the model proxy before each call: the budget of
the run token, and the daily and monthly limits of the user and the org. It writes one
`model_calls` row for each call (endpoints `images` and `speech`). A call that the API
refuses gets a row with its status code and zero cost. It shares the
`UsageLedger` of the model proxy (Section 16.6). `image_generate` records the tokens that the
API returns. `text_to_speech` records one input token for each character, because the API
charges by character. The prices are in the `media` section of `prices.toml`. The proxy does
not allow these models, so a run cannot call them through the proxy.

### 11.9 WhatsApp and email (M7.8)

WhatsApp and email have the same adapter shape as Telegram and Slack (Section 11.1): signed
inbound requests, pairing, sessions, approvals, and media. They have only direct
conversations with one person, so every session is `private` and a pairing code is a `user`
code. A `group` code for these channels gets `400`. Both channels cannot edit a sent message,
so the reply of a run goes out when the run ends or pauses for an approval
(`sama.gateway.sessions.FinalReply`).

Code: `sama.gateway.direct` (the shared part), `sama.gateway.whatsapp`,
`sama.gateway.whatsapp_providers`, `sama.gateway.twilio_client`, `sama.gateway.mail`,
`sama.gateway.mail_inbound`, `sama.gateway.sendgrid_client`, `sama.gateway.links`,
`sama.integrations.whatsapp_hub`, `sama.integrations.email_hub`, and `sama.integrations.guide`.
Migration `0039` adds the kinds `whatsapp` and `email` to `integrations`, the channels
`whatsapp` and `email` to the `channel` checks of Section 11.5 and of `session_handoffs`, and
the vault names below. It adds no table: the channels use `integrations`,
`channel_identities`, `pairing_codes`, and `session_bindings`, which already have `org_id`,
RLS, and export and delete rules (Section 17.4).

**Integrations.** One WhatsApp number and one inbound email address per org, like the other
kinds (Section 11.6). An org owner or admin connects them on the Settings page of the web app.

| Route | Body | Does |
|---|---|---|
| `POST /orgs/{org_id}/integrations/whatsapp` | `provider` (`meta`, `twilio`, or `gupshup`), `number` (E.164), `api_token`, and the fields of the provider | Checks the credentials with the provider, stores the row `active` and the secrets in the vault, and answers with `webhook_url`, `verify_token` (Meta only), and `steps`. |
| `POST /orgs/{org_id}/integrations/email` | `address`, `api_key` (SendGrid), `public_key`, `sender_name` | Checks that the key has the scope `mail.send` and that the public key is an ECDSA P-256 key, stores the row and the secrets, and answers with `webhook_url` and `steps`. |
| `DELETE /orgs/{org_id}/integrations/{whatsapp,email}` | | Deletes the row, its links and codes, and the vault entries. For Gupshup it also deletes the webhook subscription of sama. |

| Column | WhatsApp | Email |
|---|---|---|
| `external_id` | The account or app that the credentials prove: `meta:<phone number id>`, `twilio:<account SID>:<number>`, or `gupshup:<app id>`. Unique per kind. A number alone is not proven, so it does not block the same number in another org. | `sendgrid:<16 hex characters of the key digest>:<address>`. Unique per kind. The address alone is not proven. |
| `name` | The number in E.164 form. | The inbound address in lower case. |
| `settings` | `provider`, and `phone_number_id` (Meta), `account_sid` (Twilio), or `app_id`, `app_name`, and `subscription_id` (Gupshup). | `public_key` of the parse security policy and `sender_name`. |
| `secret_hash` | The SHA-256 digest of the random token in the webhook URL. | The SHA-256 digest of the SendGrid key. The hub refuses a vault key with another digest (`503`). |

| Vault name | Value |
|---|---|
| `whatsapp_api_token` | The access token (Meta), the auth token (Twilio), or the API key (Gupshup). |
| `whatsapp_app_secret` | The app secret of the Meta app, or the value of the secret header of the Gupshup subscription. Twilio has none. |
| `whatsapp_link_secret` | A random key that signs the media links (below). |
| `sendgrid_api_key` | The SendGrid key with `mail.send`. |
| `email_link_secret` | A random key that signs the approval links (below). |

A connect holds the advisory locks of the org and kind and of the `external_id`, as in
Section 11.6. A new `external_id` deletes the old row with its links. So a new SendGrid key, a
new Twilio account, or a new Meta phone number id deletes the links of the old row, and the
users pair again. A Meta connect checks
that the phone number id of the token shows the given number. A connect again
makes a new webhook token and new link secrets.

**WhatsApp providers.** The gateway sees one interface, `WhatsAppProvider`: `verify`, `parse`,
`check`, `send_text`, `send_buttons`, `send_media`, and `download`. Each provider calls only its
own API host and downloads media only from its own hosts. A redirect of a download gets no
credentials.

| Provider | Inbound signature | Approval card | Text limit |
|---|---|---|---|
| Meta WhatsApp Cloud API | `X-Hub-Signature-256`: the HMAC-SHA256 of the raw body with the app secret. `GET` with `hub.verify_token` answers `hub.challenge`. | Reply buttons (`interactive`). | 4,096 |
| Twilio | `X-Twilio-Signature`: the HMAC-SHA1 of the public webhook URL and the sorted form fields with the auth token. | Reply codes, because buttons need an approved content template. | 1,600 |
| Gupshup | Gupshup does not sign its callbacks. sama adds the subscription itself (version 3, the Meta format) with the header `X-Sama-Webhook-Secret`, and checks the header. The token of the URL is the second check. A row without the header secret in the vault gets `503` (fail closed). | Quick reply buttons. | 4,096 |

**WhatsApp webhook.** Every provider posts to `POST /whatsapp/<integration id>/webhook/<token>`.
The hub compares the digest of the token with `secret_hash` in constant time (also for an
unknown id), builds the gateway with the secrets from the vault, checks the signature of the
provider, and gives each message to the gateway in the background. A wrong token or signature
gets `401`. The body limit is 1 MB. Twilio gets an empty TwiML answer. A message to another
number of the provider is dropped. A repeated message id is dropped.

**WhatsApp gateway.**

- An unknown number gets a pairing prompt. A code in the message, or `/pair CODE`, links it.
  The lockout of Section 11.3 applies.
- A chat is a `private` session with the key `<integration id>:chat:<number>`. Commands:
  `/new`, `/stop`, `/steer TEXT`, `/skills`, `/<skill> TEXT`, `/pair CODE`, and `/help`.
- An approval card has the buttons Approve, Deny, and "Always allow for me" (when the tool
  allows it, Section 9.4.2). A button sends the text `yes CODE`, `no CODE`, or `always CODE`,
  where `CODE` is the first 6 characters of the approval id. A typed reply with the same text
  decides too. A card without buttons starts with the reply codes, so a cut at the text limit
  keeps them. The gateway looks for the code only among the open approvals of the session of
  the chat, and `decide` checks the user as for Telegram.
- Images go to `vision_analyze` (Section 11.7). A voice note is transcribed with the OpenAI key
  of the org (Section 11.5). Another file type gets a short refusal.
- A file that a run makes (Section 11.8) goes out through
  `GET /whatsapp/<integration id>/media/<media id>?e=<expiry>&s=<signature>`. The signature is
  the HMAC-SHA256 of the integration, the media, and the expiry with `whatsapp_link_secret`.
  A link works for one hour. A wrong or old link gets `404`.
- Before each send the gateway checks the link of the number again, as Telegram does.

**Email.** SendGrid Inbound Parse posts each email to the inbound address to
`POST /email/<integration id>/inbound`. The hub checks `X-Twilio-Email-Event-Webhook-Signature`:
the ECDSA P-256 signature (SHA-256) of the timestamp and the raw body, with the public key of
the security policy of the parse webhook. The timestamp must be within 10 minutes of now. The
body limit is 30 MB. The hub reads both forms of the parse webhook: the fields, and the raw
MIME message (`send_raw`).

- **Sender check.** The `From` address can be forged. The gateway accepts an email only when
  DKIM passed for the domain of `From` (or a parent domain), or SPF passed with an envelope
  domain that matches. Other email is dropped and logged without content.
- **Threads.** A new email starts a new thread and a new `private` session. The binding key is
  `<integration id>:thread:<sender>:<token>`, so one sender cannot reach the thread of another.
  Each email of sama has `Reply-To: <local>+t<token>@<domain>` and
  `Message-ID: <sama.<token>.<random>@<domain>>`, and keeps `In-Reply-To` and `References`.
  A reply finds its thread from the `+t` address or from the references. The gateway cuts the
  quoted text of a reply. The first message of a thread starts with its subject.
- **Pairing.** The user sends the code as the subject (or the first lines) of an email. An
  unknown sender gets the pairing prompt at most once an hour.
- **Commands.** An email whose whole text is `/stop`, `/steer TEXT`, `/skills`, `/<skill> TEXT`,
  `/pair CODE`, or `/help`.
- **Output.** The reply of a run goes out as one plain text email in the thread (a reply over
  100,000 characters goes on in a second email). A stored answer of a run without text deltas
  goes to the outbox whole (at most 200,000 characters), so it follows the same rule. A file of
  a run is an attachment.
- **Approvals.** The card email has one link for each choice:
  `<public url>/email/<integration id>/approvals/<token>`. The token holds the approval, the
  choice, the user, and an expiry of 24 hours, with an HMAC-SHA256 of `email_link_secret`.
  `GET` shows a page with one button and changes nothing, so a mail scanner that opens a link
  decides nothing. `POST` decides as the user of the link, only while that user still has a
  linked address on the integration. `decide` and the gate check the user as for the other
  channels. A forwarded email lets its reader decide as the user; the card says so.

**Capability URLs in logs.** The webhook token, the approval token, the media signature, and
`hub.verify_token` are parts of URLs, and access logs show paths. The redactor of the logs
(`sama.vault.redact`) replaces them with `[REDACTED]` in every log line (Section 16.3).

**Setup through the agent.** `connection_request` accepts the channel names `whatsapp`,
`whatsapp_meta`, `whatsapp_twilio`, `whatsapp_gupshup`, `email`, `telegram`, and `slack`. For
`whatsapp` it asks the agent to find the provider first. For the other names it gives the steps
for a person and a link to the Settings page (`/settings?connect=whatsapp&provider=twilio`), which
opens the form of the provider. The builtin skill `channel-setup` tells the agent to give the
steps one at a time and never to ask for a secret in the chat. The secrets go only into the
web app.

**Steps for a person.** sama cannot do these steps:

- Meta: make a Meta app with WhatsApp and a system user token, then enter the webhook URL and
  the verify token under WhatsApp > Configuration and subscribe the field `messages`. A
  message that the business sends first needs an approved template; a reply within 24 hours
  of a user message does not.
- Twilio: set the webhook URL as "When a message comes in" of the WhatsApp sender or of the
  WhatsApp sandbox. Each tester of the sandbox first sends its join word.
- Gupshup: nothing after the connect. A tester of an app that is not live opts in first.
- Email: an MX record of the inbound subdomain to `mx.sendgrid.net`, a domain authentication in
  SendGrid for the sender address, an Inbound Parse host with the URL of sama, and a security
  policy with signature verification attached to it.

**Limits of M7.8.**

- Discord is not in this step: no bot token exists. Microsoft Teams and other channels are
  later.
- `/handoff` from or to WhatsApp or email is not available, because WhatsApp needs a template
  outside the 24-hour window, and an email thread starts only from the user. An unlink of the
  number or the address stops the output of its sessions.
- A job with the target `origin` in these channels keeps its result in the session (Section
  12.1). `email:<address>` delivery works as before.
- The repeat filter, the pairing lockout, the prompt limit, and the run followers are in memory,
  per hub, as for Telegram.
- Workspace-level channel integrations are not in this step: the org picks one provider.

---

## 12. Scheduler, cron jobs, and workflows

### 12.1 Jobs

| Field | Meaning |
|---|---|
| `schedule` | Cron expression with a time zone, an interval, or one time. The agent turns natural language ("every weekday 9am") into this form, and sama validates it. |
| `prompt`, `skills` | What the run does. |
| `agent_profile`, `model` | Pins. The default is the profile default when the job fires. |
| `deliver` | `origin` (the chat that created it), a channel target (`slack:#ops`, `telegram:<chat>`, `email:<address>`), or `none`. Plus `failure_deliver`. |
| `mode` | `agent` (an LLM run) or `script` (run a script in the sandbox and deliver its output, no LLM). Sama adds one `curator` job to each workspace (migration `0032`, Section 9.7). |
| `preapproved_tools` | Tools that may run without a human in this job, each bound to a connection and optional argument limits (for example "Slack post to `#eng` only"). Everything else that needs `ask` is denied. Adding or widening a pre-approval needs a human approval, which is bound to that job version. A change to the job's prompt, schedule, or target creates a new version, and write pre-approvals then need approval again. |
| `attach_to_session` | Run inside one long-lived session (heartbeat) instead of a fresh session. |

Execution (M4.1 to M4.3):

- **Process.** The scheduler is a loop in the control plane process (`sama.scheduler.service`,
  Section 19.1). It is not a separate Fly.io process, because a loop costs nothing more and
  the lease below makes more than one machine safe. The loop ticks every 60 seconds (as in
  Hermes). Between ticks it polls every 5 seconds for ended runs, script fires, and
  deliveries.
- **Leader lease.** A tick takes the transaction advisory lock
  `pg_try_advisory_xact_lock(hashtextextended('sama.scheduler.tick', 0))`. A process that does
  not get it skips the tick. The lock ends with the transaction, so a crashed leader holds
  nothing.
- **A fire runs once.** The tick locks the due `jobs` rows with `FOR UPDATE SKIP LOCKED`,
  writes the `job_fires` row, and moves `next_fire_at` in the same transaction. `job_fires`
  has a unique key on `(job_id, scheduled_for)`. Two schedulers therefore write one fire, and a
  test runs two ticks at the same time to show it.
- **Missed and busy fires.** After downtime, a job fires once for the missed time, then
  follows its schedule. When the last fire of a job still runs, the new fire is `skipped`.
  A `once` job pauses after its fire.
- **Agent fires.** A fire is an unattended run (Section 16.1) in a new private session of the
  owner. With `attach_to_session`, the job keeps one session and uses it while it is active.
  `runs.model` (migration `0026`) holds the model pin. The worker uses
  `coalesce(runs.model, agent_profiles.model)`.
- **Script fires.** `mode: script` runs the script with `bash -c` in the session sandbox,
  with no LLM, no run token, and no secret. The time limit is 10 minutes. A script fire that
  has no result 5 minutes after its limit fails and does not run again. Script mode needs the
  sandbox runtime. A prompt or a script that holds a known secret is refused when the job is
  made or changed, and a script fire that holds one does not run.
- **The fire keeps its version.** A `job_fires` row copies `deliver`, `failure_deliver`, and
  (in script mode) the script when the fire starts. A later change to the job does not
  change a fire that already started. A fire of a version that is not cleared (for example a
  `skipped` fire) keeps `origin` and no script.
- **Recursion guard.** A job run cannot create or change jobs. The gate removes the `cron`
  toolset in a job run, and `JobService` refuses a change from a run that a job started.
- **Approval.** A job version needs the approval of its owner when it has
  `preapproved_tools`, uses `mode: script`, or sends to a target that is not the owner
  (`origin`, `none`, and the owner's own email do not count). A job that a person makes or
  changes is approved by that change only when the version before it was approved or did not
  need approval. Thus a person's change (also a change of the name only) does not approve an
  agent version that the person did not see. A resume never approves: a job with a version
  that needs approval goes back to `pending_approval`. A job that the agent makes or changes
  waits in `pending_approval` and does not fire. Only the owner approves, and the approval names the
  version that the owner saw. It stores `approved_version` and `approved_hash` (a digest of the
  fields that the version covers). A pre-approval is `{tool, connection, limits}`. The
  connection comes from the tool name (`mcp__<connection>__<tool>`). The caps are 20
  pre-approvals, 10 limited arguments, and 20 values for each argument. Some tools cannot be
  pre-approved: `cronjob_manage`, `clarify`, `tool_search`, `tool_describe`, `tool_call`,
  and `ToolSearch`.
- **Delivery (`deliveries` table).** A fire writes one `result` delivery, or one `failure`
  delivery to `failure_deliver` (else to `deliver`). A sender claims a row with a 120-second
  lease and records `sent` with a receipt (the provider message id, or `session:<id>` for an
  origin with no chat), `failed`, or `unknown`. The sender claims one row at a time, so a
  row does not wait behind other sends while its lease runs. A `sent` result that comes after
  the lease is lost still writes its receipt over `unknown`.
  - A definite failure (no connection, a refusal, no token) tries again after 30 s x
    2^attempts, for at most 3 attempts.
  - A result that is not known (a timeout after the request went out, a 5xx answer, a lost
    lease) is `unknown` and is never sent again, so a message never goes out twice.
  - `origin` sends to the Telegram chat or the Slack DM or thread of the session that made
    the job. A job from the web app keeps the result in its fire session.
  - Slack and Telegram use the bot token of the org from the vault. Email uses SendGrid with
    `SAMA_SENDGRID_API_KEY` and `SAMA_EMAIL_FROM` (Section 19.2).
- **Failures.** The third failed fire in a row pauses the job and writes one `alert`
  delivery to `failure_deliver` (else to `deliver`, else to `origin` when the target is
  `none`). `alerted_at` makes sure the alert goes out once. A resume clears the count.
- **Interfaces.** The `cronjob_manage` tool, the `/cron` command (Telegram, and `/sama cron`
  in Slack, both only in a direct chat), the API (`/orgs/{org_id}/jobs`, `/jobs/preview`), and
  the web page `/jobs` all use `JobService` and edit the same rows. Each shows the checked
  schedule and the next 3 fire times. A user has at most 50 jobs. Only the owner sees a job
  (RLS on `jobs`, `job_fires`, and `deliveries`).
- **Schedules.** A schedule is a 5-field cron expression with an IANA time zone, an interval
  (at least 5 minutes), or one time. The parser reads simple English ("every weekday at 9am",
  "every 2 hours", "monthly on the 1st at 8am", "tomorrow at 7pm") and refuses what it cannot
  read. A cron schedule must fire no more often than every 5 minutes. Sama checks the gaps
  between the daily fire times as a cycle: it assumes that each day matches, so the gap from
  the last time of a day to the first time of the next day also counts. This is strict for a
  schedule whose days never follow each other, but no day-of-month or day-of-week case can
  get past it. Sama also checks the real times around each daylight saving change in the
  next 400 days.

### 12.2 Event triggers

Webhooks (signed, one URL per trigger), Composio triggers, and channel events (for example "a
message in #support that matches a pattern") can start a run or a workflow. Event payloads
are untrusted. Event-triggered runs use the `safe` toolset unless the trigger says otherwise.
Since M7.10 each delivery to a known trigger has a row in a delivery log, and the owner or an
admin can send a signed test delivery (Section 17.7).

**What M4.4 builds**

- **Tables.** Migration `0028` adds `triggers` (one event source of a workspace) and
  `trigger_events` (one row for each accepted delivery), both with `org_id` and RLS. A member of
  the workspace or an org admin sees its triggers. An event row shows to the users who can read
  its session. The migration also adds `runs.toolsets` and the vault names `trigger:<uuid>` and
  `composio_webhook_secret`. The field `target` is `run` or, since M4.5, `workflow`.
- **Kinds.** A `webhook` trigger has its own URL, `<SAMA_PUBLIC_URL>/triggers/<id>/webhook`,
  and its own secret `whsec_<base64>` in the vault entry `trigger:<id>`. Sama shows the secret
  one time, when it makes or rotates it. A `composio` trigger is a Composio trigger instance
  (a slug such as `GMAIL_NEW_GMAIL_MESSAGE` and a config) on the account of a connection. Sama
  makes the instance when it makes the trigger and deletes it with the trigger. Composio gives
  the same instance for the same account, slug, and config, so a second trigger for it gets
  `409`, and the instance of the first trigger stays. A transaction lock on the connection,
  slug, and config covers the upsert, the insert, and the clean-up of a create, and the
  clean-up of a delete. A request that waits for the lock tries again and keeps no database
  connection while it waits. After 30 seconds it gets `409`. Sama deletes an instance only when
  no trigger row uses it.
- **Signature.** Both kinds use the Standard Webhooks scheme: the headers `webhook-id`,
  `webhook-timestamp`, and `webhook-signature` (`v1,<base64 HMAC-SHA256>` of
  `<id>.<timestamp>.<body>`). Sama compares in constant time. A timestamp more than 300 seconds
  from now is refused. The key of a webhook trigger is the decoded part of its secret. The key
  of Composio is the webhook secret of the org (vault entry `composio_webhook_secret`), else
  `SAMA_COMPOSIO_WEBHOOK_SECRET`. A GitHub-style signature has no timestamp, so sama does not
  accept it.
- **Limits.** Sama reads at most 1 MiB of a body (`413`), also when the body has no
  `Content-Length`. A body that is not JSON, or that has more than 64 levels of arrays and
  objects, gets `400`. A bad signature and an unknown trigger
  get the same `401`. A disabled trigger gets `410`. A replay (the same `webhook-id` for the
  same trigger) gets `200` with `duplicate` and starts nothing. Over the message rate of the
  owner, the answer is `429` with `Retry-After`, and no row stays, so the sender tries again.
- **Runs.** One transaction makes a private session of the trigger owner (`Trigger: <name>`),
  queues an unattended run as the owner, and writes the event row. The message is the prompt of
  the trigger and the payload as compact JSON, redacted, cut at 20,000 characters, and in an
  `untrusted_content` block. The transaction reads the trigger again with a share lock, so a
  change or a disable that commits after the signature check applies to the run. A Composio
  event for a trigger that is gone at that time gets `200` with `ignored`. `runs.toolsets`
  limits the run to the toolsets of the trigger and of the profile, in the gate, the prompt,
  and the skills. The default is `safe`. Only a workspace admin, an org admin, or the org owner
  can give more than `safe`. A trigger cannot have `delegation`, `tasks`, `cron`, or
  `workflows`, the toolsets that start other runs. A run that another run starts (a delegated child, a steer, or a task card)
  also keeps the `runs.toolsets` limit of the run that started it. Unattended rules
  apply (Section 12.1): `ask` becomes `deny`, and `clarify` gets no answer. If the owner cannot
  see the workspace now, the event gets `410` and the trigger gets `last_error`.
- **Management.** `GET` and `POST /workspaces/{id}/triggers`, `GET`, `PATCH`, and `DELETE
  /workspaces/{id}/triggers/{trigger}`, and `POST .../secret` (rotate). A member makes a
  webhook trigger. A Composio trigger needs a user who manages the connection (Section 10.3).
  Only the owner or an admin changes or deletes a trigger. Each change writes an audit row
  (`trigger.created`, `trigger.updated`, `trigger.secret_rotated`, `trigger.deleted`).
  `PUT /orgs/{id}/composio/webhook` (org admin) subscribes the Composio project of an org with
  its own `composio_api_key` to `<SAMA_PUBLIC_URL>/composio/webhook` and keeps the secret in the
  vault. The operator subscribes the project of sama and sets `SAMA_COMPOSIO_WEBHOOK_SECRET`.
- **Workflow targets (M4.5).** A trigger with `workflow_id` has the target `workflow`. Each
  accepted event starts an instance of that workflow (Section 12.3) with the trigger
  `trigger`, not a run. The workflow must be in the workspace of the trigger, it must have the
  same owner (else `403`), and it must not be removed (else `404`). The trigger uses the
  profile of the workflow, and its prompt is optional (the default is `Start the workflow
  <name>.`). One transaction starts the instance and writes the event row, which points to
  the instance (`trigger_events.workflow_instance_id`) and has no session and no run. When
  the workflow defines the input `event`, the instance gets the payload in it: the redacted
  compact JSON, cut at 9,000 bytes so the inputs stay under their limit. The toolsets of the
  trigger go to `workflow_instances.toolsets`, and each agent step run of the instance and of
  its child instances gets them as its `runs.toolsets` limit. Tool steps do not take the
  limit: the owner approved their tools and arguments with the version. A workflow that is
  paused or that waits for an approval refuses the event with `409`, sets `last_error` on the
  trigger, and writes no row, so the sender can try again. Each instance that a trigger
  starts counts toward the message rate of the owner and of the org, with the chat runs. Over
  the rate, the event gets `429` with `Retry-After`, and sama writes no row. Such an event row
  shows to the users who see its workflow instance (the owner).
- **Not in M4.4.** Delivery of the answer to a channel (M4.3; the
  answer stays in the trigger session), channel events, and a web page. A follow-up message
  in a trigger session is a normal run with the toolsets of the profile. A late Composio
  event for a deleted trigger of an org with its own Composio project gets `401`, not `200`:
  without the trigger row, sama does not know the org, so it checks with the secret of the
  process.

### 12.3 Workflows

A workflow is a saved, versioned definition of steps. It turns a task that works into a
repeatable process.

```yaml
name: weekly-github-digest
inputs:
  repo: { default: Nikhil_claw }
triggers:
  - cron: "0 9 * * MON"
    tz: Asia/Kolkata
steps:
  - id: fetch
    tool: github_list_repository_issues   # tool step: one connector tool call, no LLM
    args: { owner: nikhilllmno, repo: "{{inputs.repo}}", state: open }
  - id: summarise
    agent: { prompt: "Summarise {{steps.fetch.output}} by priority." }
  - id: approve
    approval: { prompt: "Post this digest to #eng?" }
  - id: post
    tool: slack_send_message
    args: { channel: "#eng", text: "{{steps.summarise.output}}" }
```

- Step types: `tool` (one tool call, no LLM), `agent` (a run with a prompt and skills),
  `approval`, `branch` (a condition), `parallel` (fan out), `wait` (time or event), and
  `workflow` (a sub-workflow).
- The workflow engine runs each step as a queue item and stores its output. A crash
  resumes from the last finished step.
- Each step has a stable operation id. Sama passes it as an idempotency key where the
  provider supports one, and writes a receipt before and after the external call.
- A step that failed for sure (for example a 4xx response) retries with backoff. A
  non-idempotent write with an unknown result (for example a crash after the call) is not
  retried. Sama tries to check the result (reconcile). If it cannot, the instance waits for
  a human.
- The agent can write workflows with `workflow_manage`. "Turn this session into a workflow"
  is a built-in skill. New or changed workflows that write to external systems need an
  approval before their first run.
- The web app shows each workflow instance with its steps, outputs, and approvals.

Definition (M4.5):

- **Fields.** `name` (letters, digits, `-` and `_`, unique for the owner), `description`,
  `inputs` (a map of names to `{default, required, description}`), `triggers` (at most 5,
  each `{cron, tz}` or `{schedule, tz}` with the schedule rules of Section 12.1), and `steps`.
  The YAML text is at most 50 KB. A workflow has at most 50 steps in all.
- **Step ids.** Each step has an `id` (`[a-z0-9_-]`, at most 64 characters). An id is unique
  in the whole workflow, also in branches. Each step has exactly one step key.
- **Templates.** `{{inputs.<name>}}` and `{{steps.<id>.output}}` (and `.output.<key>` for a
  field of an object output). A value that is one template keeps its JSON type. A template in
  a longer text becomes text. A template must name a known input or step.
- **`tool`.** `tool: <name>` and `args`. In M4.5 a tool step runs a connector tool of the
  catalog (Section 10.3). Other tools run in an `agent` step.
- **`agent`.** `{prompt, profile, model, skills, preapproved_tools}`. The run is an
  unattended run (Section 16.1) in a new private session of the owner. Its final answer is the
  output of the step. `preapproved_tools` has the form of Section 12.1.
- **`approval`.** `{prompt}`. The step waits until the owner approves or rejects it in the
  API or the web app. A rejection fails the instance. An agent cannot decide an approval step.
- **`branch`.** `{when, then, else}`. `when` is `{value, equals | not_equals | contains |
  empty}`. Sama does not evaluate code: a condition is a fixed comparison. The output is the
  output of the last step of the branch that ran.
- **`parallel`.** `{branches: {<name>: [steps]}}`, at most 10 branches. The output is a map of
  the branch names to the output of the last step of each branch. One failed branch fails
  the step.
- **`wait`.** `{seconds}`, `{until}` (an ISO time), or `{event, timeout_seconds}`. The longest
  wait is 30 days. `workflow_run signal` or the API sends an event. The data of the event is
  the output of the step. An event wait that times out fails.
- **`workflow`.** `{name, inputs}`. It starts an instance of another workflow of the owner
  and waits for it. The output is the output of that instance. The depth is at most 3.

Execution (M4.5):

- **Tables.** `workflows` (one row for each workflow, with its current version and status),
  `workflow_versions` (the YAML text, the checked definition, its SHA-256 hash, and the
  approval of the version), `workflow_instances` (one row for each run of a workflow),
  `workflow_steps` (one row for each step of an instance, unique on
  `(instance_id, step_id)`), and `workflow_receipts` (one row for each external call).
  Only the owner reads them (RLS). Only the control plane writes them.
- **Process.** The workflow engine is a loop in the control plane process, as the scheduler
  is (`sama.workflows.engine`). It ticks every 60 seconds under the leader lease
  `sama.workflows.tick` and starts the due schedules. An instance is unique on
  `(workflow_id, scheduled_for)`, so a schedule fire starts once. Each 2 seconds the loop
  advances the instances, runs the tool steps, takes back expired leases, and reconciles.
- **Advance.** The engine locks one instance row (`FOR UPDATE SKIP LOCKED`), reads the
  step rows, and walks the step tree in order. A step that has no row gets one: a tool step
  is `pending` with its rendered arguments, an agent step gets its session and run in the
  same transaction, and a sub-workflow step gets its child instance in the same transaction.
  The engine evaluates `branch` and `parallel` steps during the walk. A crash between two
  transactions loses nothing: the next walk reads the same rows.
- **The version is fixed.** An instance runs the version that it started with. A later
  change of the workflow does not change an instance that started.
- **Tool steps.** A worker claims a `pending` step with `FOR UPDATE SKIP LOCKED`, sets
  `running` with a lease of 120 seconds, and writes the receipt `intent` in the same
  transaction, before the call. After the call, one transaction writes the receipt (`done`
  with the clipped, redacted answer, or `failed`, or `unknown`) and the step.
  - The operation id is `<instance id>:<step id>`. It does not change between attempts.
  - Each attempt sends the idempotency key of the first attempt, from its receipt. When the
    first attempt sent no key, no attempt sends a key: a key that comes later cannot stop a
    second write of a call without a key. A retry after an unknown result is safe only for a
    read tool, or when the call sent a key and the tool still takes keys.
  - An `http_api` connector with `http.idempotency_header` in its manifest sends the
    operation id in that header, so the tool is idempotent. A Composio tool has no
    idempotency key, so sama treats a Composio write as not idempotent.
  - A definite failure (a 4xx answer, a refusal, a missing connection) retries after
    10 s x 2^attempts, for at most 3 attempts. Then the step fails, and the instance fails.
  - An unknown result (a network error, a time limit, a 5xx, 408, or 429 answer, or a lost
    lease) of a read tool or of an idempotent tool retries with the same operation id. The
  reap of a lost lease reads the key from the receipt of the attempt, not from the catalog of
  now.
  - An unknown result of a write that is not idempotent is not retried. The receipt becomes
    `unknown`, and the step becomes `needs_reconcile`.
  - A worker that crashes leaves the step `running` with the receipt `intent`. When the lease
    ends, the engine treats the step as an unknown result with the rules above.
- **Reconcile.** For a `needs_reconcile` step, the engine asks the tool runner to check the
  result with the operation id. When the write happened, the step completes with the found
  output. When it did not happen, the step retries. When the runner cannot tell (the default
  for connector tools in M4.5), the step becomes `waiting_human`. The owner then marks it
  `done` (with an optional output), `retry`, or `fail`
  (`POST /orgs/{org_id}/workflow-instances/{id}/steps/{step_id}/resolve`). A reconcile result
  applies only to the attempt that it checked: it changes the step only while the step is
  still `needs_reconcile` with the same `attempts`, so a late result cannot change a newer
  attempt.
- **Authority of a tool step.** A tool step runs as the owner, with the connections of a
  private session of the owner in the workspace of the workflow. A `deny` policy rule for the
  tool, or a profile without the `integrations` toolset, fails the step. A database tool
  (M3.11, and the engines of M4.13) saves files in the session of its run, so a tool step
  cannot call it: use an agent step. A remote MCP tool (M3.5), a hosted MCP tool (M4.11), and a tool of a custom HTTP API
  connection (M4.12) are not in the connector catalog, so a tool step cannot call them
  either: use an agent step. The approval of the version stands in for the `ask` of a write
  tool. At each claim, the engine hashes the definition again and compares it with the
  approved hash. At each advance and each claim,
  it checks that the owner still sees the workspace of the workflow. An owner who lost the
  workspace fails the instance, and the start of a sub-workflow checks the workspace of the
  child workflow.
- **Agent steps.** The gate removes the `workflows` and `cron` toolsets from an agent step
  run, and `WorkflowService` refuses a change from such a run (the recursion guard). An
  instance that a trigger started also limits its agent step runs to the toolsets of the
  trigger (Section 12.2). The
  `preapproved_tools` of the step apply only when the owner approved the version of the
  instance and its stored hash matches.
- **Approval.** A version needs the approval of its owner when it has a tool step with
  `write` or `destructive` access, or an agent step with `preapproved_tools`. Such a version
  is `pending_approval` until the owner approves that version, also when a person wrote it.
  The approval names the version that the owner saw, and it stores the hash of the
  definition. An agent cannot approve. A version that does not need approval is `active` at
  once.
- **Cancel.** A cancel of an instance cancels its open steps, the runs of its agent steps,
  and its child instances, in one transaction. It locks the runs first, so a requeue of a
  run with an expired lease cannot slip between its updates. A crash after the commit cannot
  leave an agent run alive. The answer of a tool call that is still in flight writes its
  receipt, but it does not change the cancelled step.
- **Interfaces.** The tools `workflow_manage` (create, update, validate, list, get, pause,
  resume, remove) and `workflow_run` (start, status, list, cancel, signal), the API, and the
  triggers of M4.4 (`WorkflowService.start_in`) all use `WorkflowService`. The API routes are
  `POST /workflows/validate`, `GET` and `POST /orgs/{org_id}/workflows`, `GET`, `PUT`, and
  `DELETE /orgs/{org_id}/workflows/{id}`, `POST .../{id}/approve`, `.../start`, `.../pause`,
  and `.../resume`, `GET /orgs/{org_id}/workflow-instances` and `.../{id}`, and `POST
  .../{id}/cancel`, `.../signal`, `.../steps/{step_id}/decide`, and
  `.../steps/{step_id}/resolve`. A user has at most 50
  workflows. The gate gives both tools `ask` in a shared session, and a delegated child does
  not have them.

### 12.4 Task board (M4.7)

For multi-agent work, the `tasks` table holds **cards** (Hermes kanban). A card has a title,
a body, a status, an assignee (an agent profile or a person, not both), a parent card, a
list of dependencies, a result, and comments in `task_comments`. A card belongs to one
workspace and has the audience of a session: `shared` (the members of the workspace see it)
or `private` (only its owner sees it). The select policy uses `session_visible`, and a
comment uses `can_read_task`. No policy allows writes: the control plane writes as the
table owner.

**Statuses**: `todo`, `running`, `blocked`, `done`, and `cancelled`. Only the dispatcher sets
`running`. A person sets the other statuses on the web board. When a person moves a card out
of `running`, sama cancels the run of the card.

**Limits**: a title has at most 200 characters, a body and a result 20,000, and a comment
4,000. A card has at most 20 dependencies and at most 5 parent cards above it. A workspace
has at most 500 open cards. A shared card can only link to shared cards, so the run of a
shared card never reads a private result.

**Dispatcher** (`TaskDispatcher`, a control-plane loop, every 5 seconds):

1. It takes at most 20 `todo` cards that have an agent assignee, oldest first, with
   `FOR UPDATE SKIP LOCKED`, so two control planes do not start one card two times. A card
   that waits for a dependency is not taken, unless another dependency is cancelled or
   deleted. An org with 5 running cards is not taken, and
   an org gives at most 5 cards to one pass. So waiting cards and one busy org do not stop
   the cards of other orgs.
2. A card waits until each dependency is `done`. A cancelled or deleted dependency moves
   the card to `blocked` and adds a comment from `system`.
3. An org has at most 5 running cards. The dispatcher counts them under an advisory lock of
   the org, so two control planes cannot both see free capacity. It takes the locks in the
   order of the org ids. An agent profile that is not in the workspace moves the card to
   `blocked`.
4. The dispatcher makes a new session for the card (with the audience and owner of the
   card) and an unattended run in one transaction. It clears the result of an earlier
   attempt. The prompt has the title, the body, and
   the results of the dependencies (each cut to 1,500 characters and marked as untrusted
   data). A rate limit leaves the card in `todo` for the next pass.
5. When the run ends, the dispatcher reconciles the card. A completed run makes the card
   `done`, with the result from `task_complete` or else the last answer of the run. A run
   that fails or is cancelled makes the card `blocked` with a comment that gives the reason.

**Tools** (toolset `tasks`, sama MCP):

| Tool | What it does |
|---|---|
| `task_create` | Makes a card. The assignee is `self` (the profile of the run), `agent` (a profile id), `person` (the user of the run), or `none`. The parent is the card of the run by default. |
| `task_list` | Lists the cards that the session can see (at most 50) and marks the card of the run. |
| `task_complete` | Sets a card to `done` with a result. With no `task_id` it is the card of the run. |
| `task_block` | Sets a card to `blocked` and adds the reason as a comment. |
| `task_comment` | Adds a comment to a card. |

A run sees cards as its session does: a shared session sees shared cards, and a private
session also sees the cards of its owner. An agent cannot stop the run of another card:
only a person does that.

**Web board**: the route `/tasks` of the web app shows the cards of the current workspace
in four columns (To do, Running, Blocked, Done). A person makes cards, changes the status
and the assignee, reads the result, and adds comments. The board loads again every 10
seconds while an agent works on a card. The API routes are
`GET` and `POST /workspaces/{id}/tasks`, `GET` and `PATCH /workspaces/{id}/tasks/{task_id}`,
and `POST /workspaces/{id}/tasks/{task_id}/comments`.

---

## 13. Sub-agents and multi-agent work

| Kind | When | How |
|---|---|---|
| Native sub-agents | Short side tasks inside one run (search a codebase, review a diff) | Claude `agents` option. OpenAI agents as tools (M7.4). Same sandbox, same run. |
| Sub-agent library | Reusable sub-agents of a user, a workspace, or the org | A profile names library agents, and `delegate_task(agent=...)` starts one as a child (Section 13.3, M7.4). |
| `delegate_task` | Parallel or long tasks, a different runtime or model, or a clean context | Sama creates a child session and run, in its own sandbox, linked to the parent. Only the child's summary (and file paths) return to the parent. |
| Task board | Multi-step projects with many agents and humans | Section 12.4. |

Limits for `delegate_task` (Hermes defaults): depth 1, at most 10 children per run, a turn
and cost budget per child, and blocked tools for children (`delegate_task`, the task board,
`memory`, `cronjob_manage`, `clarify`, and outbound messages). Children inherit the parent's
approval policy and can only narrow it. Section 13.2 has the design (M4.6).

### 13.1 Native sub-agents (M2.7)

A native sub-agent is a short side task inside one run. It has its own prompt, its own tools,
and an empty context. Only its final answer goes back to the main agent. Only a runtime with
the capability `native_subagents` has them. Claude has them since M2.7, and the OpenAI runtime
since M7.4 (agents as tools, Section 5.3). The inline specs of this section are one source.
The sub-agent library of Section 13.3 is the other.

**Profile.** `agent_profiles.subagents` (migration `0019`) is a JSON list of at most 8 specs.
It is empty by default. A spec has:

| Field | Rule |
|---|---|
| `name` | Lowercase letters, digits, and single hyphens. 40 characters at most. Unique in the profile. |
| `description` | 1 to 300 characters. It tells the main agent when to use the sub-agent. |
| `prompt` | 1 to 8,000 characters. The system prompt of the sub-agent. |
| `toolsets` | Names of toolsets. No duplicates. |
| `model` | Not set or `inherit`: the model of the run. Else a model id that has a price in the price table. |
| `max_turns` | Not set, or 1 to 50. |

**Write.** Two code paths write the column. They share `lock_profile` and `validate_subagents`:
`set_profile_subagents` (the admin command) and `sama.profiles.update_profile` (the web app, Section
11.4). `validate_subagents` checks the specs. It rejects the specs when: the runtime of the profile has no
`native_subagents`; the profile has no `subagents` toolset; a spec uses a toolset that the
profile does not have, or `subagents`, `clarify`, or `tool_search`; a model has no price; or the description
or the prompt has a secret or a finding of the injection scan (Section 6.1). The error text
has only the names of the fields of the schema and fixed messages. It never has the text of a
spec, and it never has the name of an unknown key: such a key shows as `unknown field`. The function writes an audit entry
`profile.subagents_set` with the names of the sub-agents only. The admin command
`python -m sama.admin subagents show|set --org <id> --profile <id> [--file <path>]` calls it.
The command first gives the redactor the secrets of the environment and the vault of
the org, so a secret that no pattern matches is also found in a spec. It fails closed: it reads
the vault in one strict refresh, and it stops when any entry does not decrypt or the vault cannot
be read. It also stops when the org has vault entries but `SAMA_VAULT_KEY` is not set. The web app
changes the sub-agents with `PATCH /orgs/{org_id}/agent-profiles/{profile_id}` (Section 11.4). It
writes the audit entry `profile.updated`, not `profile.subagents_set`.

**Build.** The prompt assembler (Section 6.1) turns the specs into `SubagentDefinition`
objects and puts them in `RunConfig.subagents`. The config is frozen with the session (D9),
so a change applies from the next session. A definition has sama tool names. The tools are
the tools of the toolsets of the spec that the run also has, so a sub-agent never has more
tools than the run. A shared chat run has the group set, which has no `subagents` by default,
so it has no sub-agents. The assembler gives no sub-agents when the run has no `subagents`
toolset, when the runtime has no `native_subagents` (it writes a warning), and for a review
run. A column with a bad value gives no sub-agents and a warning, and the run goes on. The
fingerprint of a config without sub-agents does not change.

**Rules at run time.**

- A sub-agent cannot start a sub-agent, and it cannot ask a person. The toolsets `subagents`,
  `clarify`, and `tool_search` are never in a definition (`tool_search` reaches each deferred
  tool of the run with the authority of the run), and the adapter adds `Task` and `Agent` to
  `disallowedTools`.
- The model can start only the sub-agents of the config. The hook denies the built-in agents
  of the CLI (`general-purpose`, `Explore`, `Plan`) and every call without a known
  `subagent_type`.
- The tool gate judges the `Agent` call, and it judges each tool call inside the sub-agent
  with its own call id. `Agent` and `Task` are allowed without a rule when the run has the
  toolset `subagents`. A rule can still deny them.
- A sub-agent cannot pause the run. The gate gets `child` for a call inside a sub-agent. For
  such a call, it changes `ask` to `deny` before it records anything: the `tool_calls` row is
  `denied`, and there is no pending approval and no `approval.requested` entry. A risky tool such as `Bash` is unusable in a sub-agent unless a rule allows it.
- A sub-agent runs in the foreground. The adapter turns off the background tasks of the CLI.

**Budget.** The model calls of a sub-agent go through the model proxy (D6) with the run
token, and the cost goes into the usage of the run. So the budgets of the run, the session,
and the org apply. `max_turns` limits one sub-agent call (50 at most). The proxy does not
know which sub-agent made a call, so the cost of one sub-agent is not in the data.

**Traces.** The adapter sends `SubagentEvent`s with `call_id` (the `Agent` call), `done`, and
`tokens`. The control plane stores them as `run_events` of the type `subagent_event`. The
trace exporter (Section 18.2) groups them by `call_id` and writes one span `subagent:<name>`
for each call, with the type `agent`. A span of a call that has no id uses the name. At most
200 event lines go in one span. The gate rows of the tool calls of a sub-agent are children of
the root and not of the sub-agent span, because the gate does not receive the sub-agent id.

#### 13.1.1 Assumptions

| # | Assumption |
|---|---|
| A1 | Sub-agents belong to the agent profile and are frozen with the session config (D9). |
| A2 | A spec uses toolsets, not tools. The profile must have each toolset, and the build uses only the toolsets that the run has. `subagents`, `clarify`, and `tool_search` are never given. Deferred and connector tools are not given. |
| A3 | A gate `ask` inside a sub-agent is `deny`, because nobody can approve it. The gate does this when it gets `child`, before it records an approval. The adapter also maps `ask` to `deny` for a gate that does not. |
| A4 | `Task` and `Agent` are allowed by default and need the `subagents` toolset. The default profile of the first org has it. The default group set does not. |
| A5 | The built-in agents of the CLI and background sub-agents are not allowed. The adapter sets `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` for a config with sub-agents. |
| A6 | A runtime without `native_subagents` ignores sub-agents. The assembler logs a warning, and the write rejects them. A review run has none. |
| A7 | The model of a sub-agent is `inherit` or a priced Anthropic model id. |
| A8 | The spend of a sub-agent is limited by the budgets of the run and by `max_turns`. The tokens of one sub-agent come from the CLI task message when the CLI reports them. |
| A9 | The runner never cuts the text of a `SubagentEvent`. The control plane redacts it and then cuts it, so a secret cannot lose a part to a cut before the redaction (same rule as the archive, M2.6 A6). |

### 13.2 `delegate_task` (M4.6)

`delegate_task` gives 1 to 10 independent tasks to child agents. Each task has a `goal`, an
optional `context`, an optional budget (`max_turns`, `max_cost_usd`), and an optional `agent`:
the name of a library agent that the child runs as (Section 13.3, M7.4). For each task, sama
makes a child session and a queued child run in one transaction:

- The child session has the lineage `delegation`, `parent_session_id`, and `parent_run_id`.
  It copies the workspace, the agent profile, the audience, and the owner of the parent
  session, so a child never sees more than its parent. One sandbox belongs to one session
  (D5), so each child has its own sandbox.
- The child run is **unattended**: a gate `ask` is `deny`, because no person watches it.
- The prompt of the child has only the goal and the context. The child does not get the
  conversation of the parent and gets no memory snapshot.

**Limits**

| Limit | Value |
|---|---|
| Depth | 1. A child has no `delegation` toolset, so it cannot delegate. |
| Children | At most 10 per call and at most 10 per parent run in total. |
| Blocked toolsets | `delegation`, `tasks`, `memory`, `clarify`, `cron`, `connections`, `integrations`, and `moa` since M7.5 (`CHILD_BLOCKED_TOOLSETS`). The gate and the prompt assembler take them away for a `delegation` session. |
| Budget | The lower of the profile budget and the defaults: 30 turns, 1.00 USD, and 1,800 seconds. A task can ask for at most 100 turns and 10 USD, and it never gets more than the profile allows. |
| Result | The answer of a child is cut to 4,000 characters and is marked as untrusted data. |
| Model | `agent_profiles.delegation_model` (M7.5, migration `0046`), for example a cheaper model. Without it, the model of the profile. The model must be a model of the runtime of the profile: a runtime change clears a model that the new runtime does not know. The child run pins it (`runs.model`). |

**Wait**: with `wait=true` (the default), the call polls every 2 seconds until each child ends
or `timeout_s` (at most 600 seconds) passes. Then it returns the status, the cost, and the
answer or error of each child. With `wait=false`, it returns the child ids at once.

**`delegate_status`** takes an `action`:

| Action | What it does |
|---|---|
| `status` | Shows the children of the run, or one child. |
| `cancel` | Cancels the open run of a child. |
| `steer` | Sends a new message to a child. The message runs as the next run of the child session, after the current run. A message during a run comes with M3.9. |
| `agents` | Lists the library agents that `delegate_task` can start with `agent` (M7.4). |

A run can control only the children of its own session. A cancel of the parent does not
cancel its children: each child ends on its own budget.

**Risk**: a parent that waits holds a worker slot. A child run counts toward the chat rate
limits of the user and the org.

### 13.3 Sub-agent library (M7.4)

A library agent is a sub-agent that a person writes once and uses in many places. The package
`sama.subagent_library` has the store and the rules. The tables are `library_agents` and
`library_agent_versions` (migration `0044`).

**Fields.** A library agent has the fields of an inline spec (Section 13.1) and one more limit:

| Field | Rule |
|---|---|
| `name`, `description`, `prompt`, `toolsets`, `max_turns` | The rules of Section 13.1. |
| `model` | Not set or `inherit`: the model of the run. Else a model id with a price for Claude or for OpenAI. A run of the other runtime uses its own model. |
| `max_cost_usd` | Not set, or more than 0 and at most 10 USD. The cost limit of a run of the agent (a delegated child or a test run). A native sub-agent shares the budget of its run. |

The checks at each write: a toolset must be a toolset of sama, and not `subagents`, `clarify`,
`tool_search`, `connections`, or `integrations`; the model must have a price; and the description
and the prompt cannot have a secret or a finding of the injection scan. The API first gives the
redactor the vault of the org and fails closed, as for the soul. An error text has only the
names of fields and fixed messages.

**Scopes and rights.**

| Scope | Who reads | Who writes |
|---|---|---|
| `user` (personal) | The owner | The owner. Any member writes their own agents, with no approval. |
| `workspace` | The members who see the workspace | A workspace admin, or an org owner or admin |
| `org` | Every member of the org | An org owner or admin |

An org owner or admin also needs the permission `agents.manage` of the role (M7.10) to write a
workspace or org agent. A personal agent needs no permission. The settings console (M7.10)
shows the library in its Sub-agents section.

RLS has the same read rule. Only the control plane writes the tables. A write locks the row and
the memberships, so a change of a role cannot slip in before the write. Each create, edit, and
revert makes a version. A revert makes a new version with the spec of an old version and checks
the text again. The audit entries (`subagent_library.created`, `updated`, `reverted`, `deleted`)
have the name and the version, never the prompt. A name is unique in its scope.

**Use in a profile.** `agent_profiles.library_subagents` is a JSON list of agent ids, in order.
An org owner or admin sets it with `PATCH /orgs/{org_id}/agent-profiles/{profile_id}`. The
profile history records it with the kind `profile`, so a revert works. The checks: each agent
exists in the org; a workspace agent is of the workspace of the profile (any workspace for a
profile of the org); a personal agent that the change adds is of the editor; the inline and the
library sub-agents together are at most 8 and have unique names; the profile has the toolset
`subagents` and a runtime with native sub-agents. The toolsets of an agent are not checked
against the profile: the build uses only the toolsets that the run has. An agent that a profile
uses cannot be deleted (`409`).

**Build.** The prompt assembler adds library agents after the inline specs: first the agents of
the profile that apply to the session, then the personal agents of the owner of a private
session. A workspace agent applies to its workspace. A personal agent applies only to the
private sessions of its owner, so a member's own agents need no admin. A missing agent, an agent
of another workspace or user, a name that is already used, and an agent after the eighth are left
out with a log line. Each agent becomes a `SubagentDefinition` with the rules of Section 13.1, so
it never has more tools than the run. The config is frozen with the session (D9): a change of
an agent applies from the next session.

**`delegate_task(agent=...)`.** A task of `delegate_task` (Section 13.2) can name a library
agent. The name resolves to the narrowest agent that the parent session can use: the personal
agent of the owner of a private session, then the workspace agent, then the org agent. An
unknown name stops the whole call. `delegate_status(action="agents")` lists the names. The child
session keeps the frozen spec in `sessions.library_agent`. For such a session:

- The soul of the config is the name and the prompt of the agent, not the soul of the profile.
  The config has no sub-agents, so there are no nested sub-agents.
- `RunQueue.enqueue_in` caps each run of the session with the agent: the toolset limit of the run
  is the toolsets of the agent (without the toolsets that a sub-agent never has), the budget is
  at most `max_turns` and `max_cost_usd`, and the model is the model of the agent when the
  runtime of the session knows it. A steer keeps these limits.
- The toolset snapshot of the session (Section 11.4) has only the toolsets of the agent, so the
  gate never gives the session more.
- The limits of the agent are the defaults of the child, and a task cannot ask for more.

**Test run.** `POST /orgs/{org_id}/subagents/{agent_id}/test` makes a new private session of the
caller with an agent profile of the workspace and the first message. The session runs as the
agent, with the same rules as a delegated child, but it is attended: a gate `ask` waits for the
person. The web app opens the session.

**Web API.**

| Route | What it does |
|---|---|
| `GET /orgs/{org_id}/subagents?workspace_id=` | The agents that the caller sees, `writable_scopes`, and `options` (toolsets, models of each runtime, limits). |
| `POST /orgs/{org_id}/subagents` | Creates an agent (`scope`, `workspace_id` for a workspace agent, and the fields). |
| `GET`, `PATCH`, `DELETE /orgs/{org_id}/subagents/{agent_id}` | Reads, changes, or deletes one agent. Each answer has `can_edit`, `version`, and `used_by` (the profiles that use it). |
| `GET /orgs/{org_id}/subagents/{agent_id}/versions` | The versions, newest first. |
| `POST /orgs/{org_id}/subagents/{agent_id}/versions/{version}/revert` | A new version with an old spec. |
| `POST /orgs/{org_id}/subagents/{agent_id}/test` | A test run (above). |

The web app has the page `/subagents` (list, editor, history, revert, delete, and test run) and a
"Library sub-agents" part in the profile editor. The components are self-contained, so the
settings console (M7.10) can use them.

**Data controls.** A user export has the personal agents of the user, the agents that the user
made, and their versions. A user delete deletes the personal agents (the keys cascade) and
removes them from `library_subagents` of each profile. An org delete deletes all rows.

#### 13.3.1 Assumptions

| # | Assumption |
|---|---|
| L1 | A profile names library agents by id. A session freezes the agent at its version of that time. A profile does not pin a version. |
| L2 | The personal agents of a member apply to the private sessions of the member without a change of the profile, as the personal skills do. A shared session never gets a personal agent. |
| L3 | The cost limit applies to a run that runs as the agent (a child or a test run). A native sub-agent uses the budget of its run, because the proxy does not know which sub-agent made a call (Section 13.1). |
| L4 | A workspace agent can be written by an org admin too, so an org admin can manage every scope. |
| L5 | The agent cannot propose library agents with `change_propose`. A person adds them in the web app. |
| L6 | Only a runtime with native sub-agents runs library agents. On another runtime (for example a coding agent CLI runtime of M7.2), the profile write refuses library agents, the build gives none, and `delegate_task(agent=...)` and the test run refuse to start one, because such a runtime does not take the prompt and the tool limits of the agent. |

---

## 14. Browser use and the Chrome extension

### 14.1 One tool contract, two targets

| Target | Engine | Use |
|---|---|---|
| `cloud` (default) | Browserbase session controlled with Playwright over CDP from the sama MCP server | Unattended work, research, forms, scraping. Each user has a Browserbase context, so logins persist. |
| `my_chrome` | The sama Chrome extension in the user's own browser | Sites where the user is logged in, and tasks where the user wants to watch. Needs the extension to be online. |

Tools (the plan; M5.1 has the first six, see "Cloud browser (M5.1)" below):

- `browser_open(url, target)` and `browser_tabs` manage tabs.
- `browser_snapshot` returns an accessibility-tree snapshot with element refs (`e12`), not raw
  HTML. This is cheap and reliable.
- `browser_act(action, ref | x,y, text)` performs `click`, `type`, `select`, `hover`,
  `scroll`, `press`, `drag`, `upload`.
- `browser_screenshot` returns an image. **Vision mode**: when a page has no usable
  accessibility tree (canvas apps), the agent uses screenshots and coordinates, like
  computer use.
- `browser_extract(schema)` returns structured data from the page.
- `browser_vault(action)` lists saved logins and fills a login form. The model never sees the
  password.
- `browser_task(goal)` hands a whole goal to a browser sub-agent on the cloud target and
  returns the result (M5.6, see "Browser sub-agent (M5.6)" below).

#### Cloud browser (M5.1)

The package `sama.browser` has the browser. The `cloud` target is there when the control plane
has `BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID` (Section 19.2). Since M5.5 the control
plane always has the `my_chrome` target (Section 14.2.2), so the sama MCP tool server (Section
16.7) always lists the browser tools. The descriptions of the tools name only the targets that
the process has.

**Layers.**

| Part | Module | Job |
|---|---|---|
| Target | `sama.browser.target` | The `BrowserTarget` and `BrowserSession` protocols. One target for each name: `cloud` (M5.1) and `my_chrome` (M5.5, `sama.browser.chrome`). The tools do not know the target. |
| Cloud target | `sama.browser.browserbase` | Creates a Browserbase session with the REST API and connects Playwright to it over CDP. The process has no browser binaries. |
| CDP session | `sama.browser.cdp` | `PlaywrightSession`: snapshots, actions, tabs, dialogs, screenshots, and extracts on a Playwright browser. The `my_chrome` target can use other code with the same protocol. |
| Contexts | `sama.browser.contexts` | The table `browser_contexts` (migration `0036`): one provider context for each org, user, and target. |
| Guard | `sama.browser.guard` | The outbound URL policy for navigation (Section 16.4). |
| Service | `sama.browser.service` | `BrowserService`: one session for each run, limits, idle close, and the text for the agent. |

**Sessions.** The first browser tool of a run starts a session, and the other calls of the run
use it. One call at a time uses a session. The service closes the session:

- when the run ends (the run-end hook of the worker calls `end_run`),
- when the session is idle for 10 minutes (a reaper checks every 30 seconds),
- when the process stops.

The Browserbase session also has a `timeout` of 15 minutes. This is the hard limit when sama
does not close a session, for example when the process stops or a run waits for an approval.
An org has at most 5 live sessions, and a process has at most 20. A start keeps its place in
the limits before it calls Browserbase, so concurrent starts cannot pass the limits. A failed
or cancelled start frees its place.

**Persistent context.** A run of a user in a private session uses the Browserbase context of
that user, with `persist: true`. Browserbase keeps the cookies and the local storage when the
session ends, so logins stay from run to run. The table `browser_contexts` keeps only the
context id, not a secret. When two runs of a user make a context at the same time, the first
row wins, and the service deletes the other context. When Browserbase does not know a stored
context, the service forgets it and makes a new one. A run in a shared session gets a new
context that is not kept. A user export has the rows of the user in `browser_contexts`. A user
or org delete deletes the rows and, through the purge worker, the contexts at Browserbase
(Section 17.4).

Browserbase says that two sessions must not write one context at the same time, and that the
context needs some seconds to save after a session closes. So a process gives each context to
one writer at a time:

- The first run of a user gets the context with `persist: true`.
- A second run of the same user at the same time gets the context with `persist: false`. It
  reads the saved logins but does not save. Its first result has the note "another run of
  this user uses the browser profile now, so the logins and cookies of this run are not kept".
- When the writer closes, the next writer waits until 10 seconds after that close.
- When Browserbase does not confirm the release of a writer, that session can live until its
  hard timeout. Until then, a new run reads the context with `persist: false` and the same
  note, and does not wait.
- A failed or cancelled start ends its Browserbase session first, and then frees the context.
  When the start is cancelled while Browserbase makes the session, a background task waits
  for the answer and ends that session.
- When a session closes, it stops taking paused requests, and it cancels and waits for the
  checks that run.

This coordination is for each process. Two processes can write one context, and then the last
save wins.

**Tools.**

| Tool | Arguments | Result |
|---|---|---|
| `browser_open` | `url`, `snapshot` | Opens the URL in the active tab and returns a snapshot. A URL without a scheme gets `https://`. Only `http` and `https` are allowed. |
| `browser_snapshot` | none | The accessibility tree of the active tab, in YAML, with `[ref=eN]` on the elements that an action can use. A new snapshot makes the old refs invalid. |
| `browser_act` | `action`, `ref`, `text`, `values`, `submit`, `target_ref`, `direction`, `amount`, `seconds`, `accept`, `prompt_text`, `snapshot`; since M5.2 also `x`, `y`, `to_x`, `to_y`, `clicks` | `click`, `type`, `select`, `hover`, `drag` (these need a `ref`, or a point in vision mode, see below), `scroll`, `press`, `wait` (at most 10 s), `back`, `forward`, `reload`, `dialog`. Returns a new snapshot. After `click`, `press`, and `type` with `submit`, the session waits 0.5 s so that a popup or a navigation can start. |
| `browser_screenshot` | `full_page` | A JPEG image of the active tab (a full page is at most 6,000 px high) and a caption. Since M5.2 the caption gives the size of the image, and for a viewport image the range of the points for `browser_act`. |
| `browser_extract` | `kind`, `ref`, `fields` | `text` (at most 100,000 characters), `links` (at most 500), `tables` (at most 20, 200 rows each), or `fields` (the text of the elements with the given refs, as JSON). It does not call a model. |
| `browser_tabs` | `action`, `index`, `url` | `list`, `new`, `select`, `close`. At most 10 tabs. A tab that the page opens (a popup) becomes the active tab. |

All results except the image are text. Page text, titles, and URLs come from the web, so they
are in an `<untrusted_content source="browser" url="...">` block (Section 16.5). The notes of
sama (a blocked navigation, a new tab, an open dialog) are outside the block, and they are
fixed text. An error message of sama is fixed text too. When the error has text from the page
or from the browser (for example the message of a dialog or a Playwright error), that text is
in a separate untrusted block after the message. A page cannot close the block early.

**Dialogs.** When an action opens a JavaScript dialog (`alert`, `confirm`, `prompt`), the
action returns at once with a note. The agent answers with `browser_act(action="dialog",
accept=..., prompt_text=...)`. Then the first action finishes.

**Gate.** The toolset is `browser` (Section 10.2). `browser_act` is `ask` by default, and the
other browser tools are `allow`. A child run of `delegate_task` and a run in a shared session
do not get the toolset.

**URL policy.** Every navigation goes through the outbound URL policy of Section 16.4 and the
host lists of the org (M5.3, below). See Section 16.4 for the limits.

**Later items.** M5.5 adds the `my_chrome` target (Section 14.2.2). M5.6 adds `browser_task`
("Browser sub-agent (M5.6)" below). The Browserbase key is a setting of the process. A key for
each org comes later.

#### Browser vault and URL policy (M5.3)

**Saved logins.** A saved login has a name (for example `GitHub`), a site, a user name, and a
password. The agent asks to sign in with a saved login by its name. Sama fills the user name and
the password into the page. The agent never sees them.

| Part | Module | Job |
|---|---|---|
| Logins | `sama.browser.logins` | `BrowserLogins`: add, change, remove, and list. The table `browser_logins` (migration `0038`) keeps the name and the site. The vault entry `browser_login:<id>` keeps the user name and the password, as JSON. One transaction writes both. |
| Fill | `sama.browser.cdp` | `PlaywrightSession.fill_login`: finds the fields and fills them. The protocol `LoginFiller` of `sama.browser.target` is separate from `BrowserSession`, so a target without it (for example `my_chrome`) gives a clear error. |
| Tool | `sama.tools.browser` | `browser_vault(action, name, username_ref, password_ref, submit, snapshot)`. |
| API and web | `sama.api.browser`, `web/components/BrowserSettings.tsx` | The settings routes and the "Browser" card of the settings page. |

- **Scopes.** An org login (no `user_id`) is for every member of the org. Only an org admin adds,
  changes, or removes it. A personal login is for one user. The user adds it, and the user or an
  org admin changes or removes it. A name is unique for each scope (case does not matter). When a
  personal login and an org login have the same name, the personal login wins.
- **Site.** The site is an origin: `https://host` or `https://host:port`. A value without a
  scheme gets `https://`. Sama fills a login only into a field whose document has the same
  scheme, the same port, and the same host or a subdomain of it. Sama checks the URL of the
  frame of each field, so a login form of another site in a frame does not get the login, and a
  login form of the site in a frame of another page does. The frame comes from the browser
  (`ElementHandle.owner_frame`), not from page JavaScript, which a page can change. Each field is
  a pinned element handle, and sama checks its frame again just before the fill. So a frame that
  navigates to another site during the fill does not get the value: the handle of the old
  document fails. Sama writes the value to the pinned element itself (the native `value` setter
  and the `input` and `change` events), not with keyboard input, so a page that takes the focus
  during the fill does not get the value.
- **Fields.** Without refs, sama takes the first visible password field in a frame of the site.
  The user name field is in the same form: a field with `autocomplete="username"`, else an
  `email` field, else the last text field before the password field. A page with no password
  field (the first step of a sign-in in two steps) gets the user name only. The agent can give
  `username_ref` and `password_ref` from a snapshot. `password_ref` must be a password field.
  `submit=true` presses Enter in the last filled field.
- **The secret stays out of the transcript.**
  - The tool arguments have the name and the refs only. The approval card shows them.
  - A snapshot shows the value of each password field as `[hidden]`, and an extract of a
    password field gives `[hidden]`.
  - After a fill, each result of the run (also an error) has the filled user name and password
    replaced with `[REDACTED]`, also when the page shows them later. Sama replaces the value as
    it is, escaped in JSON (an extract), escaped in HTML (a URL attribute), and encoded in a URL
    (a form that sends with GET, as a browser encodes it, and as `encodeURIComponent` encodes
    it). A user name shorter than 3 characters is not replaced: such a
    short word is in too much page text.
  - After a fill, `browser_screenshot` refuses for the rest of the run, because an image cannot
    be scrubbed: the page can show the user name, or the password after "show password".
  - The vault gives each value to the redactor of the process (Section 16.3). So the logs, the
    stored tool calls, the run events, and the Langfuse spans have no value of 8 or more
    characters.
  - An error of the fill is fixed text: a Playwright error can hold a part of the value.
  - Browserbase records no session and keeps no session log by default (`recordSession` and
    `logSession` are false), because a log can keep a form body with a password.
    `SAMA_BROWSERBASE_RECORD=1` turns them on for a test.
- **Gate.** `browser_vault` is in the toolset `browser` and is `ask` by default, also for
  `list`. A rule can allow it. A run in a shared session and a child run of `delegate_task` do
  not have the toolset, and the tool refuses a run without a user.
- **Limits.** The page gets the password, so a script of the site can read it. The user name and
  the password go to Browserbase over CDP when sama fills them. The `my_chrome` target (M5.5)
  does not fill logins yet.

**URL policy of each org.** An org admin sets two lists of hosts on the settings page
(`browser_url_policies`, migration `0038`, one row for each org):

- `block_hosts`: the browser never opens these hosts and their subdomains.
- `allow_hosts`: when the list is not empty, the browser opens only these hosts and their
  subdomains (allow list mode).

An entry can be `example.com`, `*.example.com`, or a URL (sama keeps the host). Sama keeps each
host in its IDNA form, in lower case. A list has at most 200 hosts. The private address rules of
Section 16.4 apply also to a host in the allow list. `sama.browser.guard.OrgGuard` checks the
lists, then the address rules, for each navigation of a tab or a frame, each redirect hop, and
each WebSocket. The block list applies also to the other requests of a page (images, scripts,
`fetch`). The allow list does not, because a page loads scripts and styles from other hosts.
The error names the host and the list, for example "the URL policy blocked
https://www.evil.test/: www.evil.test is in the block list of the org". The lists apply to the `cloud`
target. The `my_chrome` target runs on the network of the user and keeps its own rules (the
blocked list and the consent of the user, Section 14.2.2).

`PolicyCache` keeps the policy of an org for 30 seconds. A change applies at once in the process
of the API that took it, and after at most 30 seconds in another process, also in a live browser
session. A session starts only after the policy of the org loads. When the policy cannot be read,
the check fails closed. Before the quick check of another request of a page, the session awaits
`OrgGuard.fresh`: when the cache time of the policy is over or an admin changed it, the request
waits for a new load (requests at the same time share one load). The guard keeps the expiry of
the cache, so a cached policy does not get a second period. The quick check refuses every
request while the policy is old or after a failed load.

**Routes.**

| Route | Who | Use |
|---|---|---|
| `GET /orgs/{org}/browser/logins` | a member | The org logins and the own logins. An org admin gets every login of the org. No answer has a user name or a password. |
| `POST /orgs/{org}/browser/logins` | a member (`scope: personal`), an org admin (`scope: org`) | `{name, site, username, password, scope}`. `409` for a name that exists. |
| `PATCH /orgs/{org}/browser/logins/{id}` | the owner or an org admin | Change the name, the site, the user name, or the password. A new user name keeps the password, and the reverse. |
| `DELETE /orgs/{org}/browser/logins/{id}` | the owner or an org admin | Removes the row and the vault entry. |
| `GET`, `PUT /orgs/{org}/browser/url-policy` | an org admin | `{allow_hosts, block_hosts}`. The answer has `mode` (`allow_list` or `block_list`). |

Each change writes an audit row (`browser.login_created`, `browser.login_changed`,
`browser.login_removed`, `browser.url_policy_set`, and the `vault.*` rows) without a secret.
Without the vault, the login routes answer `503`.

#### Vision mode (M5.2)

Some pages have no usable accessibility tree: a `<canvas>` app (a drawing, a game, a chart, a
map), a remote desktop in a page, or a page with bad markup. Their snapshot has no refs for the
parts that the agent must use. In vision mode, the agent looks at a screenshot and acts at a
point of it, as with computer use.

- **Points.** `browser_act` takes `x` and `y` instead of `ref` for `click`, `type`, `hover`,
  `drag`, and `scroll`. A `drag` at a point also needs `to_x` and `to_y` (the drop point), not
  `target_ref`. `clicks` (1 to 3, default 1) gives a double click or a triple click, also with a
  ref. A point is in CSS pixels from the top left corner of the viewport. The tool schema
  accepts 0 to 10,000 for each value.
- **Actions at a point.** The session uses the mouse and the keyboard of Playwright (CDP
  `Input` events), so the page gets real events (`mousedown`, `mousemove`, `wheel`,
  `keydown`). `click` clicks at the point. `type` clicks at the point and then types the text
  with the keyboard (not `fill`), and presses Enter with `submit`. `hover` moves the mouse to
  the point. `drag` presses the button at the point, moves to the drop point in 12 steps, and
  releases the button. `scroll` moves the mouse to the point and turns the wheel by `amount`.
  `press` needs no point: it uses the keyboard of the page.
- **Screenshots.** A viewport screenshot (`full_page=false`) has the Playwright scale `css`,
  so one pixel of the image is one CSS pixel of the viewport, also on a screen with a device
  scale factor above 1. The caption gives the size of the image (sama reads it from the JPEG
  header) and the range of the points, for example "The image is 1280x720 pixels. It is the
  viewport: browser_act takes x (0 to 1279) and y (0 to 719)". The caption of a full-page image
  says that its points are not viewport points.
- **Checks.** The service refuses arguments that do not fit, before a session starts: `x`
  without `y`, a ref and a point together, a point on an action that cannot use it (`select`,
  `press`, and the others), `to_x` and `to_y` without a `drag` at a point, and `clicks` on an
  action other than `click`. The session refuses a point outside the viewport before the
  browser acts. The agent gets the size of the viewport in the error.
- **The hint.** When a snapshot has 2 refs or fewer (and the tab is not blank), the result has
  the fixed note "the page has few elements with refs (for example a canvas); to act on it,
  take browser_screenshot and use browser_act with x and y (vision mode)". The note is outside
  the untrusted block.
- **Gate.** A point does not change the gate: `browser_act` is `ask` by default, with a ref or
  with a point. The approval shows the point.
- **Targets.** A session that can act at a point has the class attribute `vision = True`
  (`sama.browser.target.has_vision`). `PlaywrightSession` has it, so the `cloud` target has
  vision mode. A session without it gets the error "the <target> browser cannot act at a point
  (x, y) yet; use a ref from browser_snapshot". It also refuses `clicks` other than 1, so a
  target cannot ignore a double click. The target protocol does not change, so a
  target can add vision mode later. For `my_chrome`, see Section 14.2.2.
- **Viewport.** The `cloud` target asks Browserbase for a 1280x720 viewport
  (`browserSettings.viewport`, `sama.browser.browserbase.VIEWPORT`). Without it, a session of
  the project had a 2560x1440 viewport. Claude Code scales an image wider than 2000 pixels down,
  so the model had to convert each point, and each screenshot cost more tokens.
- **Tests.** `tests/browser/canvas_app.py` is a canvas app with six steps (click three circles in
  order, double-click, hover, drag, type, and scroll the wheel). Its accessibility tree is empty.
  `tests/browser/test_vision.py` does the task at points with a local Chromium.
  `tests/conformance/test_browser_runtimes.py` does it from the Claude runtime and from the
  OpenAI runtime over MCP. `tests/browser/test_vision_live.py` does it on Browserbase, and gives
  a real model of each runtime a canvas app with a random layout (`?seed=N`): the model must
  look at the screenshots. The live tests need a public URL of the canvas app
  (`SAMA_LIVE_CANVAS_URL`) and run by hand.
- **Limits.** Each screenshot is an image in the history of the run, so a long vision task
  uses many input tokens. The agent must take a new screenshot after an action that changes the
  page: the result of an action has a snapshot, not an image. A point is good only for the
  scroll position of the last screenshot.
- **Vault.** After `browser_vault` fills a saved login, screenshots are off for the rest of the
  run (M5.3), so vision mode is off too. Actions at a point still work, but the agent cannot see
  the page.

#### Browser sub-agent (M5.6)

`browser_task(goal, start_url, max_steps, max_cost_usd, timeout_s)` hands a whole browser goal
to a sub-agent. One call runs many browser steps and returns a short result and the list of the
actions. The code is in `sama.browser.task` (the loop), `sama.browser.budget` (the limits), and
`sama.tools.browser_task` (the tool). The tool server has the tool when the browser service has
the `cloud` target (the Browserbase key and project).

**Why not `browser-use`.** The roadmap item named the `browser-use` library. Sama does not use
it, because it cannot run safely in this design:

| Option | Problem |
|---|---|
| `browser-use` 1.0 in the tool server | The model writes Python, and the library runs it in a local subprocess with raw CDP access to the browser. Its docs say that this is not a sandbox. In the control plane, that code can read the database, the vault, and the keys of the process. |
| `browser-use` 1.0 in the session sandbox | The sandbox needs the CDP address of the Browserbase session. With raw CDP, sandbox code can read every cookie of the persistent context of the user and act without the gate. This breaks D6 (no secrets in the sandbox) and the gate of Section 16.1. The sandbox image also gets larger. |
| `browser-use` 0.x (tool calls, no code) | It pins `mcp==2.1.1` and `pydantic==2.13.5`. Sama needs `mcp>=2.3` and `pydantic>=2.14.3`. It also pulls many provider SDKs and sends telemetry by default. |
| Browser Use Cloud (hosted API) | It uses its own browser and its own model, not the Browserbase session of the user and not the model proxy. The pages and the goal go to a third party. |

So sama runs its own small loop with the typed browser tools. This is the closest safe
version: the same tools, the same session, and the same rules as the six browser tools.

**The loop.** The loop runs in the tool server, in the process of the call.

1. The model of the sub-agent gets a fixed system prompt, the goal, and seven tools: `open`,
   `act`, `snapshot`, `screenshot`, `extract` (`text`, `links`, `tables`), `tabs`, and `done`.
   The first six call `BrowserService` with the `cloud` target on the browser session of the
   run, so they use the same tabs, cookies, and logins as `browser_open` with `target=cloud`.
   The sub-agent never uses `my_chrome` (Section 14.2.2): the user is not there to click for a
   sensitive action. Without the `cloud` target (no Browserbase key), the tool is hidden. With `start_url`, the loop opens the URL before
   the first model call.
   The `act` tool has the vision mode of M5.2: `x` and `y` (and `to_x`, `to_y`, `clicks`) in
   place of a ref, after a `screenshot`.
2. Each model call is one step. A step can run at most 5 tool calls. The last step must call
   `done` (`tool_choice`), so the task always ends with the best result that the sub-agent has.
3. The task ends when the sub-agent calls `done`, answers with text only, or reaches a limit.
4. Only one `browser_task` of a run runs at a time. The browser session stays open after the
   task, so the main agent can read the last page with `browser_snapshot`.

**Model proxy.** The sub-agent calls the Anthropic Messages API through the model proxy, with
the run token of the `browser_task` call as the key. In the control plane, `ProxyModelClient`
sends the request to the proxy app in the same process (an ASGI transport), so the request does
not leave the process. The proxy checks the token, the live authority, and the budgets of the
run, the user, and the org, uses the org key or the sama key, and writes a `model_calls` row for
each call (Section 16.6). The model is `SAMA_BROWSER_TASK_MODEL` (default `claude-sonnet-5-5`).
It must have a price in the price table.

**Budgets.**

| Limit | Default | Maximum |
|---|---|---|
| `max_steps` (model calls) | 15 | 40 |
| `max_cost_usd` (estimated with the price table of the proxy) | 0.50 | 2.00 |
| `timeout_s` | 300 | 600 (at least 30) |
| Output tokens of one model call | 2,048 | |
| Goal | | 4,000 characters |
| Result for the main agent | | 4,000 characters |

The task stops before a model call when the estimate reaches `max_cost_usd`, so the last call can
pass the limit by its own cost.

The time limit does not cut work in the middle. Each model call and each browser action runs in
its own task, which a time limit or a cancel of the tool call does not stop. The loop checks the
time before each model call, and before each action also after its gate check. When the time is over during a model call,
the task returns at once, and the call goes on in its task: the proxy records its usage when it
ends (the estimate of the task does not have that cost). A browser action that runs always ends
first (the browser has its own action time limits), so no click or typing happens after the
task returns. The task of an action keeps the lock of the browser session until the action ends,
and the run cannot start a new `browser_task` until all work of the old task ends. When the tool
server stops, it waits for the model calls that still run and never cancels them (the time
limits of the proxy end them), and it waits up to 30 seconds for the browser actions. The tool
server stops before the model proxy, so the proxy records these calls. The budgets of the run, the user, and the org in the proxy still
apply, and so do the quotas of the plan (M6.6). A refused call (`402`) ends the task, and the
result names the limit with the fixed text of the proxy, for example "the org used its monthly
model quota". Other errors of the proxy become fixed texts of sama. To keep the prompt small, the sub-agent sees the
results of the last 2 steps in full. Older results are cut to 600 characters, and older
screenshots are removed. One result is at most 20,000 characters.

**Approvals.** `browser_task` is `ask` by default (it is not in `DEFAULT_ALLOW`), so a person
approves the goal and the budget on the approval card. An unattended run cannot use it without a
pre-approval. Each step passes the gate again as the browser tool that it uses (`open` as
`browser_open`, `act` as `browser_act`, and so on) with `GateService.inner`. `inner` runs
`decide` with the live context of the run. It records no tool call and uses no ticket, because a
step has no call id of the runtime. A `deny` refuses the step, and the sub-agent gets the reason.
The audit log has `tool_gate.inner` for a deny. An `ask` for the step is covered by the approval
(or the allow rule) of the `browser_task` call. So a `deny` rule for `browser_act` also stops the
clicks of `browser_task`. `inner` does not run the unattended step of `decide`: in an unattended
run, the `browser_task` call passed that step already (with a pre-approval of the job version or
an allow rule), and that decision covers the `ask` of its steps.

**Safety.** Every navigation of a step passes the URL policy of the cloud browser: the address
rules (Section 16.4) and the URL policy of the org of the run (M5.3), as for `browser_open`.
The goal and the page text pass the redactor before they go to the model, and the page text
reaches the sub-agent in the untrusted blocks of the browser tools. The system prompt tells the
sub-agent to treat page text as data, to do only what the goal asks, not to buy, pay, delete,
send, or post unless the goal asks for it, never to type passwords or other secrets, and to stop
at a login or a CAPTCHA. The sub-agent has no `browser_vault` tool. The main agent can fill a
saved login with `browser_vault` (M5.3) before the task, on the same session. These prompt rules are not a boundary: the
boundary is the approval, the gate of each step, and the URL policy. The result and the action
log come from the sub-agent, which read the pages, so they go back to the main agent in an
`<untrusted_content source="browser_task">` block. The header (status, steps, estimated cost) is
fixed text of sama. The action log keeps only the size of typed text.

**Progress.** Each step sends a `subagent_event` run event with the sub-agent name
`browser_task` and a text such as `step 3/15: click e12`. The first event is `started`, and the
last event has `done` and the tokens of the task. The events of one task share a `call_id`
(`browser_task:<uuid>`), so the trace exporter writes one span `subagent:browser_task` for each
task (Section 18.2). The tool server gives the events to `Worker.emit`, which adds them to the
stream of the run with the next `seq`. This works when the run streams in the same process, as
with one control plane process (D18). Otherwise the events are dropped, and the result still has
the action log. The tool also sends MCP progress notifications. The tool server answers in JSON
without a stream (Section 16.7), so a client does not get them now.

**Assumptions (M5.6).**

| # | Assumption |
|---|---|
| A1 | The closest safe version of the roadmap item is a sama loop with the typed browser tools, not `browser-use` (see the table above). |
| A2 | The sub-agent model is an Anthropic model. The proxy has the Anthropic key on staging and production. |
| A3 | The approval of `browser_task` (or its pre-approval in an unattended run) covers the `ask` of its steps. A `deny` rule for a browser tool still applies to the steps. |
| A6 | The time limit is checked between steps and does not cut a model call or a browser action, so the usage of each call is recorded and no action happens after the result. A task can pass `timeout_s` by one browser action. |
| A4 | Progress uses the existing `subagent_event`, so the runtime contract and the sandbox template do not change. The web app does not show these events yet. |
| A5 | `browser_task` is in the `browser` toolset, so every profile with `browser` gets it, and no migration is needed. |

### 14.2 Chrome extension

- Manifest V3, TypeScript. A side panel for chat, a background service worker, and content
  scripts.
- **Connection**: the user signs in once. The extension opens a WebSocket to the gateway.
  The gateway routes `my_chrome` tool calls for that user to this connection.
- **Actions**: the `chrome.debugger` API (CDP `Input.dispatchMouseEvent` and
  `Input.insertText`) gives real clicks and typing. A visible overlay shows the agent cursor
  and the element it acts on.
- **Context**: "Ask about this page" sends the page text and a screenshot to the session.
- **Consent**: per-site permission (ask, allow for this site, never). Sensitive actions
  (submit a form, purchase, send, delete) always need a click from the user. The user can
  stop the agent at any time, and the agent pauses when the user takes over.
- **Limits**: no access to `chrome://` pages or other extensions. Banking and password
  manager sites are blocked by default.

#### 14.2.1 Extension core (M5.4)

M5.4 builds the side panel chat and "Ask about this page". It does not build browser actions.
The code is in `extension/` (TypeScript, Manifest V3, esbuild). The server side is
`sama.api.chrome` (the WebSocket) and `sama.gateway.chrome` (page content and connections).

**Parts of the extension.**

- **Service worker** (`background.ts`). It keeps the Supabase Auth tokens in
  `chrome.storage.session`, so the tokens go away when the browser closes. It opens the
  WebSocket, calls the REST API for the side panel, and adds the bearer token. On a `401`, it
  refreshes the token one time and tries again. The side panel never sees the tokens. When
  the sign-in service does not answer, the worker keeps the sign-in and tries again later. It
  signs out only when Supabase refuses the refresh token, or when the API refuses a token that
  cannot refresh. Chrome can stop the worker when no side panel is open; a panel that opens
  starts the worker and the connection again.
- **Side panel** (`panel/`). It talks to the service worker through a `chrome.runtime` port.
  It shows the sign-in form, the chat of the current tab, the approvals, and the composer.
  It builds the DOM with `textContent` only. The CSP of the manifest allows only the scripts
  of the extension.
- **Tab binding**. The extension keeps the session of each tab in `chrome.storage.session`,
  one key for each tab (`sama.tab.<tab id>`), so two writers do not overwrite each other.
  The first message in a tab makes a private session ("Chrome: <tab title>") in the first
  workspace that has an agent profile. A message goes to the session of the tab where the user
  clicked, also when the user changes the tab before the message is sent. A closed tab removes
  its binding. The session stays in the web app.

**Sign-in.** Email and password through Supabase Auth, with the anonymous key from
`GET /config`. Like the web app, the extension does not keep a password. An advanced form
accepts an access token (for tests and for staging). The API address is set at build time
(`SAMA_API_URL`, default staging). The user can change it in the side panel. The extension
then asks for the host permission of the new address and signs out.

**WebSocket protocol** (`/channels/chrome/ws`, protocol `1`). JSON text frames with a `type`.

| Direction | Frame | Use |
|---|---|---|
| Client | `auth {token}` | The first frame, within 10 seconds. The token is never in the URL, so it is not in access logs. A later `auth` with a fresh token of the same user moves the end of the connection. |
| Client | `subscribe {session_id, after?}`, `unsubscribe {session_id}` | Follow the events of a session from an event id. At most 8 sessions for each connection. Each followed session uses one event stream slot of the user, as SSE does. |
| Client | `send {id, session_id, text, page?}` | Queue a user message. `page` is "Ask about this page". |
| Client | `ping` | Keep the connection and the service worker alive (every 20 seconds). |
| Server | `ready {protocol, expires_at, max_frame_chars}`, `auth_ok {expires_at}` | The connection is signed in until `expires_at` (epoch seconds, from the token). |
| Server | `runs`, `run_event`, `run`, `end` with `session_id`, `data`, `event_id` | The same events as the SSE stream of Section 11.4. |
| Server | `sent {id, run_id, queued}`, `error {id?, code, message, retry_after_s?}`, `pong` | Replies. The error codes are `forbidden`, `not_found`, `busy`, `invalid`, `rate_limited`, and `quota_exceeded` (a model quota of the plan, Section 17.5). |

Close codes: `4400` frame that is not valid, `4401` token that expired or is not valid,
`4403` origin that is not allowed or a token of a different user, `4408` no `auth` in time,
`4429` too many connections (4 for each user and process) or too many frames (40 in 10
seconds), `1009` frame larger than 6,000,000 characters. The extension refreshes the token
after `4401`. It does not reconnect after `4403`. For other closes, it reconnects with back-off
(1 to 30 seconds) and follows its sessions again from the last event id.

**Origin.** A browser always sends the `Origin` of a WebSocket. The API accepts
`chrome-extension://<id>`, and refuses all other browser origins before it accepts the
connection, so a web page cannot use the cookie-free socket with a stolen token. A client that
is not a browser sends no `Origin` and must still send a valid token.
`SAMA_EXTENSION_IDS` (comma-separated 32-letter ids) limits the accepted extensions. Set it
in production to the id of the Chrome Web Store item.

**Ask about this page.** The user clicks the button. The side panel asks for the host
permission of the origin of the tab (an optional permission, one time for each site). It then
reads `document.title`, `innerText`, and the selection with `chrome.scripting`. With "Include
a screenshot", it takes a JPEG of the visible tab. Chrome takes a screenshot only with the
`<all_urls>` host permission (a host permission for one site is not sufficient), so in this case
the same request also asks for `<all_urls>`. Chrome takes the visible tab of the window, so the
side panel takes the screenshot only if the tab of the click is still the visible tab before and
after the capture. If it has no screenshot, the side panel sends
the text and shows a notice. Pages that are not `http` or `https`, and the
Chrome Web Store, are refused. The `send` frame has `page {url, title, text, screenshot?}`.

The API (`sama.gateway.chrome.page_message`):

- accepts a page only in a private session that the user owns, because the page can hold
  private data;
- accepts only `http` and `https` URLs without user info;
- redacts secrets in the URL, the title, and the text, then cuts the text to 20,000
  characters with a note (redaction first, so a cut cannot leave part of a secret). The
  extension sends at most 28,000 characters. When it cuts the text, it drops the last 8,000
  characters of the part that it cut, and the API drops the last 8,000 characters of a text of
  28,000 characters, because a cut can go through a secret that the redactor then cannot find.
  A known secret longer than 8,000 characters is not covered;
- puts the title, the URL, the selection, and the text in an
  `<untrusted_content source="chrome_page">` block after a header that tells the model to treat
  it as data (Section 16.5);
- stores the screenshot as an image attachment for `vision_analyze` (Section 11.7).

The question of the user stays outside the untrusted block. The question is "Summarize this
page." when the composer is empty.

Since M5.5 the server also sends `tool_call`, and the client answers with `tool_result`
(Section 14.2.2).

**Limits of M5.4.** The API keeps the open connections in memory (`ChromeHub`), one map for
each API process. The side panel does not show tool call details or traces. The web app shows
them.

#### 14.2.2 Browser actions in the user's Chrome (M5.5)

M5.5 adds the `my_chrome` target of the browser tools (Section 14.1). The calls go over the
WebSocket of the extension (Section 14.2.1). The agent uses the same six tools as for the cloud
browser, so both runtimes have the target.

**Parts.**

| Part | Where | Job |
|---|---|---|
| Target | `sama.browser.chrome` | `ChromeTarget` and `ChromeSession`. A session sends each browser call to the extension and turns the answer into the data classes of `sama.browser.target`. It has the blocked list and the sensitive refs of the latest snapshot. |
| Hub | `sama.gateway.chrome.ChromeHub` | The open extension connections of each user. `link(user)` gives the newest connection. |
| Frames | `sama.api.chrome` | `tool_call` (server) and `tool_result` (client) on the WebSocket. |
| Service | `sama.browser.service` | One session for each run and target. The tools have an optional `target`. |
| Gate | `sama.gate` | `escalate`: a sensitive action always asks a person (Section 16.1). |
| Extension | `extension/src/lib/actions.ts` and others | The `Actor`: CDP actions, the overlay, the consent, the sensitive check, Stop, and takeover. |

**Targets of a call.** Each browser tool has an optional `target` (`cloud` or `my_chrome`).
Without it, the call uses the target of the last browser call of the run, else the default
target. The default target is `cloud` when the process has the Browserbase key, else
`my_chrome`. A run has at most one session on each target. The descriptions of the tools name
only the targets that the process has.

**Routing.** The control plane is one process (D18), so `ChromeHub` is in memory, and the tool
server and the API share it. A call of the `my_chrome` target goes to the newest connection of
the Supabase Auth user of the run user (`users.auth_user_id`). A run without a user (a job of an
org) cannot use the target. When the user has no open connection, the call fails with "the sama
Chrome extension of this user is offline". When sama has more than one API process, a Postgres
`LISTEN/NOTIFY` channel for each user must send the call to the process that holds the
connection. That is not built.

**Frames.**

| Direction | Frame | Use |
|---|---|---|
| Server | `tool_call {call_id, op, args, session_id, run_id, approved, timeout_ms}` | One browser call. `op` is `navigate`, `snapshot`, `act`, `screenshot`, `extract`, `tabs`, or `release`. `approved` is true when a person approved this call in the approval card of the gate. |
| Client | `tool_result {call_id, ok, result \| error {code, message}}` | The answer. The API drops an answer for a call that is not open. |

A connection has at most 16 open calls. The API waits 150 seconds for an answer, because an
answer can wait for the user. The extension gets a deadline 5 seconds earlier. A closed
connection fails its open calls with "offline". `release` tells the extension that the run
ended: the extension detaches the debugger and removes the overlay. The service sends it when
the run ends, when the session is idle for 10 minutes, and when the process stops.

The extension sends codes, not text, for the notes of a page state and for the errors. The API
maps each code to fixed text (`NOTES` and `ERRORS` in `sama.browser.chrome`) and drops unknown
codes. The detail of an error (text of Chrome or of the page) goes in the untrusted block. The
title, the URL, the tree, and the extracted text are in the untrusted block, as for the cloud
browser (Section 16.5).

**Tabs.** The agent acts only in the tabs of its run:

- the tab that is bound to the session of the run (Section 14.2.1),
- the tabs that the agent opens (`browser_open` without a bound tab, or `browser_tabs` `new`),
- a tab that a page of the run opens (a popup). It becomes the active tab.

`browser_tabs` lists only these tabs, and it closes only the tabs that the agent opened. A run
has at most 10 tabs. A tab that another run uses gives "busy". The agent opens its tabs in the
window of the active tab of the run. It does not make a tab group.

**Actions.** The extension attaches `chrome.debugger` (CDP `1.3`) to a tab of the run when the
first call needs it. Chrome shows its bar "sama started debugging this browser" while the
debugger is attached.

- Snapshot: `DOM.getDocument` (for the attributes and the forms) and
  `Accessibility.getFullAXTree`. The tree has the format of the cloud browser
  (`- button "Pay" [ref=e3]`). The value of a password field is never in the tree. Only the
  main frame is in the tree.
- Click, hover, drag: `DOM.scrollIntoViewIfNeeded`, `DOM.getContentQuads`, then
  `Input.dispatchMouseEvent`.
- Type: `DOM.focus`, select the old text, then `Input.insertText`. `submit` presses Enter.
- Press: `Input.dispatchKeyEvent` (for example `Enter` or `Control+A`).
- Select: sets the options in the page and sends `input` and `change`.
- Scroll: a `mouseWheel` event, or a scroll of the element into view.
- Back, forward, reload, dialogs: the `Page` domain. An action that opens a JavaScript dialog
  returns at once with a note, as in the cloud browser.
- Navigate: `chrome.tabs.update` (or `chrome.tabs.create`), then a wait for the load.
- Screenshot: `Page.captureScreenshot` (JPEG, a full page of at most 6,000 px). The overlay is
  hidden during the capture.
- Extract: functions in the isolated world of the overlay. A password field gives `(hidden)`.

The functions that run in the page run in an isolated world (`Page.createIsolatedWorld`), not in
the world of the page scripts.

**Overlay.** The isolated world draws the agent cursor, a box around the element of the action,
and a bar "sama is acting in this tab" with a Stop button. The overlay is in a closed shadow
root, and its buttons act only on a real click (`isTrusted`). It talks to the extension through a
CDP binding of the isolated world (`Runtime.addBinding` with `executionContextName`), so the page
scripts cannot call it. The extension also checks the execution context of each signal.

**Takeover.** When the user clicks or presses a key in the tab while the agent does not send
input, the overlay tells the extension, and the tab is paused. The bar says "You have control.
sama is paused." with "Give back to sama" and Stop. The side panel shows the same state and
buttons. The next call of the run waits until the user gives the tab back. After 120 seconds it
fails with "the user took over the tab", and the agent tells the user what it was doing.

**Calls and cancellation.** The calls of one run run one at a time in the extension: a call
starts only after the work of the last call ended. Each CDP command of a call checks first that
the run is not stopped or released and that the deadline of the call did not pass, so a call that
timed out sends no more input. Right before input, the extension checks again that the page did
not navigate since the checks, that the site is still allowed, and that the user does not have
control. When Chrome attaches after a Stop or a release, the extension detaches at once. When
the WebSocket closes, the extension releases all runs, because the API cannot release them
later.

**Stop.** Stop in the overlay or in the side panel cancels the run (`POST /runs/{id}/cancel`),
closes the open questions of the run, and detaches. Later calls of the run fail with "stopped".
When the user closes the debugging bar of Chrome, the extension stops the run the same way.

**Consent.** Before the agent reads or acts on a site (a host name), the site must be allowed:

- "Allow on this site" is kept in `chrome.storage.local` (`sama.site.<host>`).
- "Allow once" is for this run only.
- "Not now" refuses this call.
- "Never" is kept, and later calls on the site fail at once.

The question shows in the side panel. When no side panel is open, the extension opens a small
sama window (`prompt.html`). Only a click in an extension page answers a question, so a web page
cannot answer it. A question that nobody answers in 120 seconds fails the call. The side panel
lists the kept choices under "Site access", and the user can remove each one. Only `http` and
`https` pages are allowed.

**Blocked sites.** `sama.browser.chrome.BLOCKED_HOSTS` and `extension/src/lib/blocklist.ts`
have the same list (a test checks it): Google account and payment pages, the Chrome Web Store,
password managers, payment and exchange sites, and banks. A host with a label that has the word
`bank` is a bank too. The API refuses a navigation to a blocked site before it calls the
extension. The extension refuses every call on a blocked site, also after a link or a redirect,
and the user cannot allow one.

**Sensitive actions.** These always need a click of the user:

| Code | When |
|---|---|
| `submit` | A click on the submit button of a form that writes (a method other than GET), or Enter in a text field of such a form. |
| `pay`, `send`, `delete`, `confirm` | A click on a button or link with a name such as "Pay", "Place order", "Send", "Delete", or "Confirm". |
| `password`, `payment` | Typing into a password field, or into a payment field (`autocomplete="cc-*"`, or a name such as card number or CVV). |

There are two checks:

1. **The gate.** The snapshot marks these elements with `[needs approval]` and sends their codes
   to the API. When the agent calls `browser_act` on such a ref, the gate gives `ask` even with an
   `allow` rule (Section 16.1). The approval card shows "a sensitive action in the user's Chrome:
   buy or pay". The user approves it in the side panel or in the web app.
2. **The extension.** Before it acts, the extension reads the live element again. If the action
   is sensitive and no person approved the call (`approved` is false), the extension asks the
   user ("Allow this action on shop.example? sama asks to buy or pay: 'Place order'.") and waits.
   This check covers a page that changed after the snapshot, and Enter without a ref.

An approval in the gate is good only for the element that the person saw. The session keeps
the snapshot and the reason of each call when the gate first asks for it. The extension skips
its own question only when the call runs in that same snapshot with that same reason
(`approved_reason` in the `tool_call` frame). After a new snapshot, the extension asks again. A
ticket of an approved call that then runs under an `allow` rule is used up, so it cannot approve
a later call with the same arguments (Section 16.1).

Keys count too: Enter or Space on a sensitive button, Enter in a text field of a form that
writes, and a key with text in a password or payment field.

**Address rules.** The address rules of Section 16.4 do not apply to `my_chrome`: the browser
of the user is on the network of the user, and a DNS check from sama means nothing there. The
blocked list and the consent of the user apply instead.

**Gate.** `browser_act` stays `ask` by default for both targets (M5.1). A user who wants the
agent to act without a card for each click chooses "Always allow" on a `browser_act` card. The
sensitive actions still ask. An unattended run cannot do a sensitive action.

**Permissions.** M5.5 adds `debugger` to the manifest (version `0.2.0`). Chrome shows a new
warning ("Access the page debugger backend"), and the Chrome Web Store item needs a new review
(Section 14.2.3).

**Limits.**

- One API process (see Routing).
- The snapshot has the main frame only. Frames of other sites are not in the tree.
- A takeover is a pointer or key event of the user in the page. A scroll with the mouse wheel
  is not a takeover. An event that comes while the agent sends input is not a takeover.
- The overlay is in the DOM of the page. A page can hide or remove it; the extension checks its
  own host element (not an id that the page can copy) and puts it back at the next call.
- The overlay copies the light-theme values of the panel tokens, because the page cannot load
  the CSS of the extension.
- The keyword list of the sensitive check is in English.

**Vision mode.** The `my_chrome` session does not have vision mode yet (Section 14.1,
"Vision mode (M5.2)"). The service refuses a point (`x`, `y`) and `clicks` other than 1 on it,
so a point cannot pass the sensitive check, which reads refs. To add vision mode later:

- the session sends the point to the extension and sets `vision = True`;
- the extension maps a point to `Input.dispatchMouseEvent` (and `Input.insertText` for
  `type`), and takes the viewport screenshot with `Page.captureScreenshot` and a device scale
  factor of 1, so that one pixel is one CSS pixel;
- the sensitive check covers points: the extension finds the element at the point
  (`DOM.getNodeForLocation`) and checks it as it checks a ref, and the gate treats a point
  that it cannot check as a sensitive action.

#### 14.2.3 Publish to the Chrome Web Store

A person does these steps. The agent cannot do them.

1. Build: `cd extension && npm ci && SAMA_API_URL=https://sama-production.fly.dev npm run build`.
2. Make the zip: `cd dist && zip -r ../sama-extension.zip .`.
3. Open the Chrome Web Store Developer Dashboard
   (`https://chrome.google.com/webstore/devconsole`) with the publisher Google account. Pay
   the one-time developer fee and verify the email if this is the first item.
4. Click "New item" and upload `sama-extension.zip`.
5. Store listing: name, short and full description, the 128 px icon
   (`extension/static/icons/128.png`), at least one 1280x800 screenshot of the side panel,
   category "Productivity", and the language.
6. Privacy: give a single purpose ("chat with the sama assistant about the current tab"). Give
   a reason for each permission: `sidePanel` (the chat), `storage` (the sign-in and the tab
   binding), `tabs` (the title and address of the current tab), `scripting` and `activeTab`
   (read the page when the user clicks "Ask about this page"), `debugger` (since M5.5: the agent
   clicks and types in the tabs of the user after the user allows each site; it is attached only
   while the agent acts, and Chrome shows its bar), host permissions (the sama API
   and the pages that the user shares; the optional `<all_urls>` is requested only when the user
   asks for a screenshot, because `chrome.tabs.captureVisibleTab` needs it). Declare "Website content" and "Authentication
   information" as data that the item uses, not sold, and used only for the single purpose.
   Add a privacy policy URL.
7. Distribution: choose "Private" (a list of testers or a Google Workspace domain) or
   "Unlisted" for the first release. Submit for review.
8. After the item has an id, set `SAMA_EXTENSION_IDS=<id>` on the production app, for example
   `fly secrets set SAMA_EXTENSION_IDS=<id> -a sama-production`.
9. For each new version, increase `version` in `extension/manifest.json`, build, zip, and
   upload on the "Package" page.

---

## 15. Code editing and self-modification

### 15.1 Editing code

- The sandbox has no GitHub token. Git traffic goes through the **sama git proxy** on the
  control plane, which holds the GitHub App installation tokens. The sandbox authenticates
  to the proxy with the run token. The org allowlist decides which repos.
- The agent clones with `repo_clone`, edits with native tools (Claude `Edit`, OpenAI
  `apply_patch`), runs tests in the sandbox, commits to a `sama/<slug>` branch, pushes
  through the proxy, and opens a pull request with `pr_create`.
- The proxy checks every push: the target repo is on the allowlist, every ref is a
  `sama/*` branch, there is no force-push and no ref delete, and changed protected paths
  (for example `.github/workflows`) have an approval.
- The agent never merges. Sama has no merge tool, and no GitHub token reaches the sandbox.
  A GitHub App with `contents: write` can merge through the API, so the permission alone
  does not stop a merge. Repos also need branch rules (rulesets) on the default branch that
  require a reviewed pull request and give the sama App no bypass.
- Repo `AGENTS.md` and `CLAUDE.md` load as context files for that session.

**Code (M1.16).** The first step: the agent clones a public repo, edits files, and runs tests
in the sandbox with native tools. M3.7 adds push and pull requests (below).

| Part | Where | What it does |
|---|---|---|
| Parser | `src/sama/tools/repos.py` | `parse_repo` accepts `https://github.com/owner/name` (with or without `.git` or a final `/`), `github.com/owner/name`, and `owner/name`. It refuses other hosts and schemes, user info, ports, queries, fragments, and other paths. |
| Cloner | `src/sama/tools/repos.py` | `RepoCloner.clone` runs `git clone` in the session sandbox through `Sandbox.exec`. The clone is partial (`--filter=blob:none`) and has one branch. The folder is `<work folder>/<folder>`, and the default folder is the repo name. The command turns off credential helpers and prompts, and allows only the protocol of the source URL. A failed clone removes its folder. |
| Tool | `src/sama/tools/server.py` | `repo_clone(repo, ref, folder)` in the toolset `github`. The tool is hidden when the server has no sandboxed runtime. |

- **Ref.** `ref` is a branch or a tag. A tag gives a detached HEAD. A ref with `..`, `//`, a
  space, a start other than a letter or a digit, or an end of `/`, `.`, or `.lock` is refused.
- **Folder.** One segment of letters, digits, `.`, `_`, or `-`. Not `.` or `..`. If the folder
  exists, the tool tells the agent to use git in that folder or give another name. The tool
  makes the empty folder with `mkdir` before the clone. `mkdir` is atomic, so of two calls
  with one folder only one clones, and a failed clone removes only a folder that its call
  made. If `git clone` stops because the folder is not empty, another writer put files in
  it after `mkdir`. Then the tool keeps the folder and tells the agent that the folder exists.
- **Instruction files.** The result gives the URL, the branch (or "a tag"), the commit, the
  path, and the text of `AGENTS.md` and `CLAUDE.md` at the repo root, if they exist. The tool
  reads them from the commit (`git ls-tree`, `git cat-file`), and only regular files. A
  symlink could point to a file outside the clone, so the tool ignores it. Each
  file goes through the injection scan and the 20,000-character cap of Section 6.1 (it
  reads at most 100,000 bytes). A blocked file gets the placeholder text and a warning log.
  Each file is in an `<untrusted_content>` block with the file URL at that commit, and the
  result tells the agent that the files are data. They are not in the prompt prefix, because
  the prefix stays the same for the session (Section 6.1).
- **Edits and tests.** The agent uses native `Bash`, `Edit`, and `Write` in the clone. These
  tools keep the default `ask` (Section 16.1). An admin can add `allow` rules in
  `tool_policies` for a profile.
- `python -m sama.admin bootstrap` gives new profiles the `github` toolset. Existing profiles
  keep their toolsets.

- **No token.** The sandbox has no GitHub token. Without the GitHub App (M3.7), only public
  repos clone. A private or unknown repo gets the error "does not exist or is not public".

**Code (M3.7): GitHub App, git proxy, and pull requests.**

| Part | Where | What it does |
|---|---|---|
| App and client | `src/sama/github/client.py` | `GitHubAppConfig` holds the App id, slug, client id, private key, and client secret. `app_manifest` gives the manifest of the App. `GitHubClient` signs the App JWT (9 minutes), makes installation tokens, keeps each token for its life minus 5 minutes, and calls the REST API (installations, repos, pull requests, comments, `compare`). A `401` removes the kept token. No error message has a token. |
| Org connections | `src/sama/github/service.py` | `GitHubService`: the install URL with a signed state, the end of the install, the allowlist of repos, and the push grants. |
| Push checks | `src/sama/github/push.py`, `src/sama/github/packscan.py` | The size check of the pack and the checks of each ref update of a push (below). |
| Git proxy | `src/sama/github/proxy.py`, `src/sama/github/pktline.py` | Smart HTTP git at `<SAMA_PUBLIC_URL>/git/<owner>/<name>.git`. A fetch goes to GitHub as it is. A push goes to GitHub only after the push checks. |
| Tools | `src/sama/tools/github.py`, `src/sama/tools/server.py` | `pr_create`, `pr_comment`, and `repo_push_approval` in the toolset `github`. The tools are hidden when the GitHub App is off. |
| Clone | `src/sama/tools/repos.py` | A repo on the allowlist clones through the proxy. Other repos clone from GitHub with no credentials. |
| API | `src/sama/api/app.py`, `src/sama/github/routes.py` | The org routes and the two public pages of the install (below). |
| Admin | `src/sama/admin/github_app.py` | `python -m sama.admin github-app manifest` and `convert` (Section 19.5). |
| Tables | migration `0024` | `github_installations`, `github_repos`, `github_push_grants` (below). |

- **The App.** One sama GitHub App serves one deployment. Its permissions are `contents:
  write`, `pull_requests: write`, `metadata: read`, and `members: read`. `members: read` lets
  sama read the role of a person in a GitHub organization during the install. The App is
  public, so each sama org can install it on its own GitHub account. It has no `workflows` permission, so
  GitHub also refuses a push that changes `.github/workflows`. It has no `administration`
  permission, so it cannot change a branch rule or a ruleset. The webhook is off, because sama
  needs no events. A person creates the App from the manifest (Section 19.5). Only the control
  plane has the private key and the installation tokens.
- **Install.** An org admin calls `POST /orgs/{org_id}/github/install`. The answer is the URL
  `https://github.com/apps/<slug>/installations/new?state=<state>`. The state is signed with
  the run token key, has the org and the admin, and lives 30 minutes. The App has "request
  user authorization (OAuth) during installation" on. So GitHub sends the person to
  `/github/setup` with `installation_id`, `code`, `setup_action`, and the same `state`.
  - The `GET` changes nothing. It shows a confirm page with the sama org and the GitHub
    account, and a "Connect" button. The button sends a `POST` to `/github/setup`. A link from
    another person thus cannot connect an account without a click on the page.
  - The `POST` checks the state again. The admin in the state must be an org admin now.
  - The installation id in the URL alone proves nothing. Sama changes the code into a user
    token and checks that this person can see the installation (`GET /user/installations`).
  - To see an installation is not enough: a collaborator with read access to one repo also
    sees it. For an organization account, the person must be an active `admin` of that GitHub
    organization (`GET /user/memberships/orgs/{org}`). For a user account, the person must
    be that user.
  - Then sama writes the installation for the org, in a transaction that checks the org
    admin again. One installation belongs to one org: another org gets `409`.
  - `setup_action=request` shows that a GitHub owner must approve the install first.
- **Allowlist.** An org admin adds a repo with `POST /orgs/{org_id}/github/repos`
  (`repo` is `<owner>/<name>`, `protected_paths` is optional). Sama asks GitHub for the
  installation of the repo, and the installation must belong to the org. The default protected
  paths are `.github/workflows/`, `.github/actions/`, `.github/CODEOWNERS`, `CODEOWNERS`, and
  `docs/CODEOWNERS`. A path that ends with `/` is a folder. The match ignores case. Members
  read the list with `GET`. Admins remove a repo or an installation with `DELETE`. Each change
  writes an audit entry.
- **Clone.** `repo_clone` asks the allowlist first. For a repo on the allowlist, the control
  plane runs `git clone` in the sandbox from the proxy, with the run token of the call in the
  environment of that command only. The clone keeps a credential helper that reads
  `$SAMA_RUN_TOKEN` each time git runs. So the token is not in `.git/config`, and a later run
  of the session uses its own token. The runner process in the sandbox has the run token in
  its environment, and the shell of the agent gets it from that environment.
- **Proxy.** The proxy reads the run token from HTTP Basic auth (the password) and checks it.
  The run must be live: a run that ended, or a run whose user lost access to the session, gets
  `403`. The repo must be on the allowlist of the org of the token, else `404`. Before it
  sends a push to GitHub, the proxy checks the run and the allowlist again. The proxy takes only
  the smart protocol. It removes the push capabilities `side-band`, `side-band-64k`,
  `report-status-v2`, `push-cert`, and `push-options`, and adds `no-thin`. So the push has
  only the commands and a full pack, and the proxy writes the push result itself. A refused
  push gets a `report-status` that refuses every ref with the reason, so `git push` shows the
  reason to the agent. A push is at most 100 MB. The limit counts the bytes that arrive, also
  for a gzip body, and the inflated body too. A gzip body must end once, with no data after it.
  At most 4 push checks run at one time.
- **Push checks.** The proxy sends a push to GitHub only when every ref update passes:
  - The ref is a branch `refs/heads/sama/<name>`. Other branches, tags, and other refs are
    refused.
  - The update does not delete the ref.
  - The update is a fast-forward. GitHub `compare` must say that the old commit is an
    ancestor of the new commit. A force-push is refused.
  - The protected paths of the new commit are the same as in a trusted base, or a person
    approved the push. The trusted bases are the old commit of the branch and the commit of
    the default branch. GitHub runs the workflows of the pushed commit, so the check compares
    trees: it looks at the tree of the new commit, not at each commit of the push. A change
    that a later commit puts back needs no approval. A symlink in place of a protected folder
    is a change. `git index-pack` reads the pack in an empty temporary repo, and the proxy
    fetches the trees of the bases from GitHub without blobs. If a check cannot read an
    object, the proxy refuses the push.
  - The proxy finds the commits and their parents with `git rev-list --parents`, which reads
    only the real parent headers of a commit. A `parent` line after the `committer` line does
    not make a fast-forward.
  - Before `git index-pack` reads the pack, a size check (`src/sama/github/packscan.py`)
    reads the header of each object and inflates it in small parts, with no copy of the
    output. A commit or a tag is at most 1 MB, a tree at most 16 MB, and a delta, its base,
    and its result at most 64 MB each. All objects together inflate to at most 1 GB, and a
    pack has at most 200,000 objects. Each object must inflate to the size of its header.
    `git index-pack` runs with `core.bigFileThreshold=1m`, so it streams a large blob and does
    not keep it in memory. The proxy reads at most 64 MB of output from each git command.
- **Approval of protected paths.** The refusal tells the agent to call
  `repo_push_approval(repo, branch, commit)`. This tool is in `ALWAYS_ASK` (Section 16.1): a
  person approves each call, an `allow` rule does not remove the ask, and "always allow"
  writes no rule. The Slack and Telegram cards for it have no "Always allow for me" button. It is in `NEEDS_A_HUMAN`, so an unattended run gets `deny`. The tool server
  checks the gate itself before it runs a sama tool, so the sandbox cannot call the tool with
  only the run token. The approved call writes a push grant for one repo, one branch, and one
  commit. The grant lives 1 hour. The proxy uses it once (`FOR UPDATE SKIP LOCKED`), then the
  agent pushes again.
- **Pull requests.** `pr_create(repo, head, base, title, body, draft)` opens a pull request
  from a `sama/*` branch. `pr_comment(repo, number, body)` comments on a pull request only,
  not on an issue. The tools run on the control plane with the installation token. They are
  not in `DEFAULT_ALLOW`, so they get `ask` unless a policy rule allows them. Sama has no
  merge tool.
- **Branch rules.** The App has `contents: write`, so a token of the App can merge through
  the API. The proxy and the tools never do. The setup still requires a ruleset on the default branch of
  each repo on the allowlist, with no bypass for the App (Section 19.5).
- **Tables.** `github_installations` (org, installation id, account, unique installation
  id), `github_repos` (org, installation, owner, name, protected paths, unique per org and
  lower-case name), and `github_push_grants` (org, repo, ref, commit, expiry, used time, the
  approving run). Members of the org read the installations and the repos. Org admins read
  the grants. No policy allows writes: the control plane writes with the service role.
- **Residual risks.**
  - The proxy uses a grant before it sends the push to GitHub. If GitHub then refuses the
    push, the agent needs a new approval.
  - A push larger than 100 MB is refused. A push with more than 2,000 new commits is refused.
  - The tree check trusts the default branch of GitHub at the time of the push.
  - A sama org admin can send an install link to a person who owns another GitHub account.
    That person sees the sama org name on the confirm page and must click "Connect".
  - In its 30 minutes, a state works again. A second install of the same installation for
    the same org changes the same row.
  - The git commands of the proxy have no memory limit of their own. The size check above
    limits what they read. A push can write up to 1 GB of objects to a temporary folder on the
    disk of the control plane.
  - Not verified live: that the Claude Code `Bash` tool in an E2B sandbox sees
    `SAMA_RUN_TOKEN`. The proxy tests run `git` with that variable on the local machine.

### 15.2 Changing its own stack

"Make changes to the current agent stack" has two levels:

| Level | What | How it applies |
|---|---|---|
| Configuration (data) | SOUL, agent profiles, toolsets, approval rules, skills, memory, jobs, workflows, connections | Through sama tools and the web app. Versioned rows. Low-risk changes apply at once (user skills, user memory). Others create a `change_propose` card and apply after approval. Prompt content (SOUL, memory, skills) applies from the next session. New toolsets and looser profile rules also apply from the next session (M6.1). Restrictions (deny rules, removed tools, revoked connections, lower budgets) apply at once (Section 5.1). Since M6.1, a change to the SOUL, the settings, the toolsets, or the policy rules of a profile can be reverted from the history in the web app (Section 11.4). |
| Code | Sama itself (and later products such as vorflux) | The agent uses `sama_pr`: it clones the sama repo in its sandbox, makes the change, runs the tests, and opens a pull request. A human reviews and merges. CI deploys. Changes to protected paths (auth, tenancy, approvals, model proxy, sandbox, CI) need the owner's review (Section 15.2.1). |

The agent never edits the running service, its database schema, or its secrets directly.

#### 15.2.1 `sama_pr` (M6.2)

`sama_pr(action, ...)` is in the toolset `self`. It works only on one repository: the sama
repository of the deployment, `SAMA_SELF_REPO` (default `nikhilllmno/Nikhil_claw`). It uses
the GitHub App, the git proxy, and the allowlist of Section 15.1, so the repository must be
on the allowlist of the org. The tool is not in `DEFAULT_ALLOW`, so each call gets `ask`
unless a policy rule allows it. A delegated child gets `deny` for `ask`, and `execute_code`
cannot call the tool.

| Part | What it does |
|---|---|
| `start(name, folder)` | Clones the repository into the sandbox through the git proxy (as `repo_clone`) and makes the branch `sama/<name>`. A clone that does not use the proxy fails. The result tells the agent what to do next: change the code and the docs, run `ruff`, `mypy`, and `pytest`, commit, `git push origin sama/<name>`, and call `sama_pr` with `open`. |
| `open(branch, title, summary, tests, draft)` | Opens a pull request from the pushed `sama/*` branch to the default branch. `summary` and `tests` (the tests that the agent ran and their results) are required. The tool compares the branch with the default branch on GitHub. A branch that is not pushed, has no new commits, or changes 300 files or more fails. |
| Body | `## Summary`, `## Changes` (the changed files, each with its owners), `## Tests`, `## Owner review` (the changed paths with code owners, and the changed protected paths of the git proxy), and a line that says that a person merges. Each list has at most 100 lines and 12,000 characters, then a line with the number of the other entries. The summary and the tests have at most 10,000 characters each. So the body stays below the GitHub limit of 65,536 characters. The review request still goes to all owners. |
| Owner review | The tool reads the code owners file of the default branch (`.github/CODEOWNERS`, then `CODEOWNERS`, then `docs/CODEOWNERS`), because GitHub uses that file for the review rule. A change to the file on the branch does not change the owners of the pull request. The old path of a renamed file counts. The tool asks the user and team owners for a review. A failed review request does not fail the tool, because the ruleset still requires the approval. |

The repository has `.github/CODEOWNERS`. It names an owner for the protected paths: CI and
`.github/`, the deploy files, migrations, authentication (`src/sama/api/auth.py`), tenancy,
approvals (`src/sama/gate/`), the model proxy, the vault, the sandbox, and the GitHub code
with `sama_pr`. The code owners file is also a protected path of the git proxy, so a push
that changes it needs a `repo_push_approval`.

The parser supports the patterns of GitHub: `*`, `**`, `?`, a leading `/`, a trailing `/`,
and the rule that the last matching line wins. A backslash before a space makes the space
part of the pattern (`/my\ dir/`). It skips lines with `!`, `[ ]`, or a leading `\#`,
which GitHub does not support either. Matching is case-sensitive, as on GitHub. The tool
reads a code owners file of up to 3 MB, the GitHub limit. The contents API gives no content
for a file larger than 1 MB, so the tool then reads the git blob of the file.

The agent cannot merge:

- Sama has no merge tool, and no GitHub token goes to the sandbox.
- The git proxy refuses a push to a branch that is not `sama/*`.
- The App has no `workflows` permission, so GitHub refuses a push that changes
  `.github/workflows/`. A person changes CI.
- The ruleset of the default branch requires a pull request, an approval, and the review of
  the code owners, with no bypass for the App (Section 19.5).

Residual risks:

- GitHub ignores the code owners rule if the ruleset does not have "Require review from Code
  Owners". The tool cannot check the ruleset, because the App has no `administration`
  permission.
- An owner in the code owners file that has no write access to the repository gets no review
  request. GitHub shows the file as having an error.
- The tool checks the changed paths that GitHub gives at the time of `open`. A later push to
  the branch can change more paths. GitHub then asks the new owners itself.

### 15.3 Checkpoints

Since M6.4, sama takes a snapshot of the session files before each tool call that can change
them. The user can restore a snapshot with `/undo` or `/rollback`. These are channel commands
for the user, not model tools, so the agent cannot roll back the user's view of its own work.
The idea comes from Hermes checkpoints. Sama does not copy Hermes code.

**Shadow repository.** `sama.runtime.checkpoints.Checkpoints` keeps a git repository in
`.sama/checkpoints` of the work folder (`/home/user/work`). The work folder has no `.git` of
sama: the shadow repository uses `GIT_DIR` and `GIT_WORK_TREE`, a clean environment, no
global or system git configuration, and its own index. A snapshot is a commit on the ref
`refs/sama/checkpoints`. The commit message keeps the run id, the tool name, and the kind
(`tool` or `rollback`). Git stores each file content once, so a snapshot of files that did not
change costs little. The first snapshot of a small folder takes about 15 ms. In a live E2B
sandbox, the first snapshot of 1,000 files took about 0.2 s, and a later snapshot about 40 ms.

Git stores the bytes of each file as they are. The file `info/attributes` of the shadow
repository turns off the attributes that change the content (`text`, `filter`, `ident`, and
`working-tree-encoding`). It has a higher priority than the `.gitattributes` files of the work
folder. The user and system attribute files are off too.

**What a snapshot has.** The files and the symbolic links of the work folder, with the
executable bit. Files in a nested git repository (for example a `repo_clone`) are normal
files of the snapshot. A snapshot does not have:

- the folders `.git`, `.hg`, `.svn`, `.sama`, `node_modules`, `.venv`, `venv`, `__pycache__`,
  `.cache`, and the caches of mypy, pytest, ruff, and tox,
- files larger than 10 MiB.

If the work folder has more than 50,000 files, sama takes no snapshot and logs a warning.
A restore does not change the files that a snapshot does not have.

**When.** The runner wraps its tool gate in `CheckpointGate`. For each tool call with the
decision `allow` or `ask`, the wrapper takes a snapshot before the call runs. A snapshot
at `ask` covers the call that a human approves later. The tools are `Bash` (and
`exec_command` of the second runtime, which the gate sees as `Bash`), `Write`, `Edit`,
`MultiEdit`, `NotebookEdit`, `repo_clone`, and `execute_code`. A snapshot is not taken for a
`deny`, for tools that only read, or when the files are the same as in the last snapshot of
the same run. Thus a run gets a new checkpoint only after it changed files. A failed
snapshot logs a warning and does not stop the tool call. The design works the same in both
runtimes, because it is in the gate and not in an adapter (Section 5.3).

**Restore.** The control plane runs `python -m sama.runtime.checkpoints` in the session
sandbox (`sama.orchestrator.checkpoints.CheckpointService`). The command prints one JSON
line. A restore:

1. refuses when the session has an open run. The service locks the session row
   (`FOR UPDATE`) for the whole restore. `RunQueue.enqueue` locks the same row, and a new run
   row takes a key share lock on it, so no run can start during a restore. The restore does
   not take the rate lock of the org. But a message to the same session during a restore
   waits for the row while it holds the rate lock, and then the new runs of the other sessions
   of the org also wait until the restore ends. A restore takes about 1 second in E2B, and at
   most the command timeout (120 seconds). The runner also holds `.sama/runner.lock`, and a
   restore waits at most 10 seconds for it.
2. takes a `rollback` checkpoint of the current files first, so that the user can go back with
   `/rollback 1`.
3. finds the files of the target that it cannot write without data loss. To write a file,
   git must remove what is at its place. The restore removes only a file or a link that the
   `rollback` checkpoint has, or a folder that has only such files. It does not write a target
   file at the place of a large file, or of a folder that has excluded or large files (for
   example a clone with its `.git` folder). It keeps these items as they are and counts them as
   `kept`. The answer to the user names the count.
4. deletes the files that the `rollback` checkpoint has and the target does not have, and
   writes the other files of the target.

The control plane writes the audit entry `session.undo` or `session.rollback` with the commit
and the counts of files (`files`, `deleted`, `kept`).

**Commands.**

- `/rollback` lists the last 10 checkpoints, newest first, with the time in UTC and the tool.
  `/rollback NUMBER` restores that checkpoint.
- `/undo` restores the files to before the last run that changed them. A second `/undo`
  goes back one more run. A run that did not change the files is skipped. This is not the
  conversation undo of Hermes: the messages of the session do not change.
- **Parallel runs (M7.1).** Two runs of one session can change files at the same time (Section
  4.3). Their checkpoints are in one history, under the checkpoint lock. If another run has a
  checkpoint between the checkpoints of the run that `/undo` chooses, the undo would also undo a
  part of the work of the other run. Then `/undo` changes nothing and tells the user to choose a
  checkpoint with `/rollback`. A restore still refuses while any run of the session is open, and
  each restore first takes a `rollback` checkpoint, so no file of a run is lost without a way
  back. Sama has no lock on single paths: like sub-agents of Claude Code in one work folder, the
  runs share the files, and the side run is told which requests the other runs work on.

The commands work in a Telegram direct chat, a Slack direct message, and the web composer
(Section 11.1). The HTTP routes are in Section 11.4. The commands need the session sandbox,
so the standalone gateway (without a sandbox) answers that checkpoints are not available.
Sama does not delete old checkpoints. They live as long as the session sandbox. A deploy that
changes the template makes a new sandbox, and the work folder with its checkpoints does not
stay (Section 16.2). The commands also answer that checkpoints are not available when the
session has no sandbox yet, or when its sandbox is from an old template.

**Diffs (M7.9).** The command `diff` compares two checkpoints, or a checkpoint and the current
files. The Code page of the web app shows it (Section 15.5).

**Limits.** The agent can change or delete `.sama/checkpoints` with `Bash`. Checkpoints are a
help for the user, not a security control.

### 15.4 Coding agents: Claude Code and Codex (M7.2)

Sama can give coding work to two coding agent CLIs: **Claude Code** (`@anthropic-ai/claude-code`)
and **Codex** (`@openai/codex`). This is the sama form of the Hermes skills that send work to
Claude Code or Codex. Sama does not copy Hermes code. The CLIs run headless in the E2B sandbox of
the session. They have no provider key and no GitHub token:

- Models: Claude Code gets `ANTHROPIC_BASE_URL=<model proxy>/anthropic` and the run token as
  `ANTHROPIC_API_KEY`. Codex gets a custom model provider `sama` with
  `base_url=<model proxy>/openai/v1`, `wire_api="responses"`, and `env_key="SAMA_RUN_TOKEN"`.
  The proxy uses the key of the org from the vault, else the key of sama (Section 16.6, M6.7).
  There is no login of a person with a subscription of Anthropic or OpenAI.
- Git: a repository on the allowlist clones through the sama git proxy, and the credential
  helper of the clone reads `$SAMA_RUN_TOKEN` (Section 15.1). The CLI gets the run token in its
  environment for this. A push goes only to a `sama/*` branch, and the proxy checks it.

**Three ways in.**

| Way | What it does |
|---|---|
| Tool `coding_agent_run(agent, prompt, repo, open_pr, session, timeout_s)` | The agent of a run gives one task to `claude_code` or `codex`. The CLI runs in the sandbox of the session of the run, so the agent can read the result and run the tests in the same folder. Toolset `coding_agents`. |
| `/claude <task>`, `/codex <task>` | The channel queues one message for the coding agent (`runs.coding_agent`). The run uses the runtime of the agent and not the runtime of the session. |
| Direct mode | `/claude` or `/codex` with no task sets `sessions.coding_agent`. Each later chat message of the session goes to that CLI until `/sama`. An agent profile with the runtime `claude_code` or `codex` is in the direct mode for all its sessions. |

**Who may switch.** A switch of the direct mode changes the session for each person in it, so
it follows the rule of the settings console (`PATCH /sessions/{id}`, M7.10): in a private
session the owner; in a shared session the owner of the session or an admin of the workspace
or the org (`sama.coding.commands.may_switch`). Others get `403` from
`POST /sessions/{id}/coding-agent`, and `/claude`, `/codex`, or `/sama` with no task answers
that only the owner or an admin can switch the session. A task (`/claude <task>`) is one
message: each member who may send messages may send it, and the tool gate asks as usual.

The commands work in Telegram (`/claude`, `/codex`, `/sama`), in Slack (`/sama claude
[task]`, `/sama codex [task]`, `/sama sama` in a direct message, or a mention with `/claude
<task>`), in WhatsApp and email (M7.8: `/claude`, `/codex`, `/sama` as the whole text), and in
the web composer (a bare `/claude`, `/codex`, or `/sama` calls
`POST /sessions/{id}/coding-agent`; `/claude <task>` goes through `POST /sessions/{id}/messages`).

**A request.** A message to a coding agent is `[<repo>] <task> [--pr]`. The first word is the
repository when it is `github.com/<owner>/<name>` (with or without `https://`) or a short
`<owner>/<name>` without a dot. So `src/app.py` is not a repository. `--pr` as the first or the
last word asks for a pull request. `CodingService.run` (`src/sama/coding/service.py`) then:

1. prepares the folder: `<work>/<name>` for a repository, the same folder as `repo_clone`. A
   folder that has a clone of the same repository (its `origin`) is used again, so a follow-up
   request sees the earlier changes. A folder with another repository of the same name is
   refused. Else `RepoCloner` clones it. Without a repository, the folder is the folder of the last request
   of the session, else the work folder;
2. starts the CLI with the task on stdin, and reads one JSON event per line;
3. stops the CLI at the end of the turn, on a cancel, or at the time limit (default 20 minutes,
   at most 50 minutes, because E2B keeps a sandbox at most 1 hour per connect);
4. reads the diff since the commit at the start: the commits of the CLI and the changes that
   are not committed, with new files (`git add --intent-to-add`). The result has at most 60,000
   bytes of the redacted diff;
5. with `--pr` or `open_pr`, commits the changes to `sama/claude-<id>` or `sama/codex-<id>`,
   pushes through the git proxy, and opens a pull request to the branch of the start with the
   GitHub App (`GitHubTools.create_pull`). The git config `sama.target` of the clone keeps
   the branch of the start. A later request with `--pr` in the same folder pushes its commits
   to the same `sama/*` branch, so the open pull request gets them. The title and the body are
   redacted (the secrets that the redactor knows and the run token) before they go to GitHub. A
   repository that is not on the allowlist gets no push and no pull request. A person reviews
   and merges.

**The CLI commands.** `sama.coding.agents.cli_command` builds them. The versions are pinned in
`sandbox/Dockerfile` (Claude Code 2.1.330, Codex 0.171.0, Node.js 22).

| | Claude Code | Codex |
|---|---|---|
| Command | `claude -p --output-format stream-json --verbose --model <m> --dangerously-skip-permissions --settings {"skipWebFetchPreflight": true} [--resume <id>]` | `codex exec [resume <flags> <id>] --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox --model <m> -c model_provider="sama" -c model_providers.sama={...} ... -` |
| Model | The model of the session if it is an Anthropic model with a price, else `claude-sonnet-4-6`. Every model alias of the CLI (`ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_*_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`) is set, so the CLI never sends a model without a price. The small model is `claude-haiku-4-5`. | The model of the session if it is an OpenAI model with a price, else `gpt-6.1-terra`. |
| No other hosts | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (without it the CLI calls `api.anthropic.com` and a log host directly), `DISABLE_AUTOUPDATER`, `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`, and `skipWebFetchPreflight`. | `analytics.enabled=false`, `feedback.enabled=false`, `features.plugins=false` (else Codex clones a plugin catalog from GitHub), the `otel` exporters `none`, and no update check. The built-in `openai` provider of Codex is never used: it ignores `OPENAI_BASE_URL` and goes to `api.openai.com`. |
| Retries | `CLAUDE_CODE_MAX_RETRIES=3`, so a refused run token does not retry for 3 minutes. | `request_max_retries=1`, `stream_max_retries=1`. |
| Own sandbox | Off. The sandbox user is not root, so the CLI takes the flag. | Off. The bubblewrap sandbox of Codex does not work in the E2B VM (`bwrap: loopback`), and with it every command fails. |
| Resume | `--resume <session id>`. The transcript is in `~/.claude`. | `codex exec resume <thread id>`. The thread is in `~/.codex`. |
| Cost in the events | From the price table and the `usage` of the result (the `total_cost_usd` of the CLI counts the whole session after a resume). | From the price table and `turn.completed.usage`, less the count at the end of the last request (the count is for the whole thread). |

The model proxy needed no change. A live probe (2026-11-21, 75 calls) showed that Claude Code
calls only `POST /anthropic/v1/messages?beta=true` (streaming, with its `anthropic-beta`
header) and Codex calls only `POST /openai/v1/responses` (streaming, `store: false`). Codex
sends a compaction as a normal `/responses` call. Neither CLI calls `count_tokens` or
`GET /models`. `SandboxSettings.carry_dirs` has `.claude` and `.codex`, so a new template keeps
the CLI sessions (Section 16.2).

**The runtimes `claude_code` and `codex`.** `CodingAgentRuntime` (`src/sama/coding/runtime.py`)
implements `AgentRuntime` on the control plane. It gets the session sandbox and the run token
from `SandboxedRuntime` (`acquire`, `issue_token`, `release`), and it drives the CLI itself: the
sandbox runner does not start it.

- **Events.** The text of the agent is `TextDelta`. Each tool of the CLI is `ToolCallStarted`
  (with a one-line `summary`) and `ToolCallFinished`. The diff and the pull request come as the
  last `TextDelta`, and the whole result is `RunCompleted.output`.
- **State.** The runtime state is JSON: the CLI session of each folder, the last folder, the
  Codex usage of each thread, and the request that waits for an approval. The run queue keys
  the state by runtime, so the state of `claude_code` never goes to `claude_agent_sdk`. When a
  saved CLI session is gone (a new sandbox), the runtime starts a new CLI session once.
- **Config.** The prompt assembler gives a coding run the frozen config of the session with
  the runtime of the agent and a model of its provider. The frozen config of the session keeps
  the runtime of the profile, also when the first run of the session goes to a coding agent, so
  `/sama` goes back to it. The CLI does not get the sama prompt or the sama MCP server.
- **Capabilities.** `streaming` and `resumable_sessions`. The CLIs compact their own context,
  so `native_compaction` is true and `manual_compaction` is false: `/compact` in the direct
  mode compacts the context of the sama runtime of the session. No native sub-agents, no
  `mcp_client`, and no mid-run `/steer`. A background review of the session runs on the sama
  runtime of the same provider (`claude_agent_sdk` or `openai_agents_sdk`).
- **Profiles.** The web app and `PATCH /orgs/{org_id}/agent-profiles/{profile_id}` accept the
  runtimes `claude_code` and `codex`. A `codex` profile needs an OpenAI model with a price.

**Approvals, budgets, and cancel.**

- The tool gate judges each request as one call of `coding_agent_run` with the agent, the
  repository, the task (first 4,000 characters), and `open_pr`. The tool is not in
  `DEFAULT_ALLOW`, so the default is `ask`: in a run of the runtimes, the run ends with
  `ApprovalRequired`, and the resume after an approval starts the CLI. "Always allow" writes a
  user rule, as for other tools. An unattended run gets `deny`. A `deny` rule wins. The run needs
  the toolset `coding_agents`; migration `0044` adds it to a profile with the default toolsets of
  migration `0036`, and it is in the default list of `bootstrap`. It is not in the default group
  set.
- The approval covers the whole request, also the pull request when `open_pr` is set. The gate
  does not see the tools that the CLI calls in the sandbox. The sandbox (egress allowlist, no
  secrets) and the git proxy (only `sama/*` branches, approval for protected paths) are the
  boundary inside the request.
- Every model call of the CLI goes through the model proxy with the run token, so the budget of
  the run (`max_tokens`, `max_cost_usd`), the quota of the org, and the quota of the user apply,
  and `model_calls` records each call. A refused call (`402`) ends the turn; the runtime maps it
  to `RunFailed(budget_exceeded)`.
- The wall time of the run limits the CLI (the CLI stops 60 seconds before it, for the diff and
  the pull request). `/stop` cancels the run, and the cancel kills the CLI process at once. A
  cancel during the gate check stops the run before the CLI starts.
  A cancel of a run that called `coding_agent_run` ends the tool call, and the tool kills the
  CLI too.

**Progress in the channel.** In the runtimes, the progress is the run events. The tool runs on
the tool server, and the channel shows only the events of the run, so `coding_agent_run` adds
its progress to the stream of the run with `Worker.emit`, the seam of `browser_task` (M5.6,
Section 14.1). It sends a `SubagentEvent` (`subagent` is `claude_code` or `codex`) for each text
and each tool of the CLI, at most 200 per call. Telegram and Slack show these events as
`> Claude Code: <text>` lines, and the web page shows them in the live run text. The control
plane runs the tool server and the worker in one process (Section 19.1); a run of another
process gets no progress lines, only the result.

**Redaction.** The parsers redact each text of the CLI before they cut it to a preview or a
progress line, because a cut secret no longer matches the redactor. Sama reads at most
240,000 bytes of the diff, redacts all of it, and then cuts it to 60,000 bytes on a line end.
After a cut, a key block that has no end line is removed. The pull request body scrubs each
field whole before its cut. The tool server and the worker redact again (Sections 16.3 and
16.7).

**The result of the tool** has the answer of the CLI, the diff, the pull request, and the CLI
session id. The agent gives `session=<id>` to continue the same CLI session. The answer and the
diff come from the repository and the CLI, so they are in an `<untrusted_content>` block.

**Skills.** The builtin skills `claude-code` and `openai-codex` tell the agent when to give a
task to a coding agent, how to write the task, how to follow up, how to review the diff, and when
to open a pull request. They need the toolset `coding_agents`, so they are hidden without it.
The names `claude`, `codex`, and `sama` are reserved for the commands.

**Other changes.** The OpenAI runtime waits up to 1 hour for one MCP call (`MCP_CALL_TIMEOUT_S`,
1,200 seconds before), because `coding_agent_run` can take 50 minutes.

**Code.**

| Part | Where |
|---|---|
| Agents, CLI commands, limits | `src/sama/coding/agents.py` |
| Event parsers (`ClaudeStream`, `CodexStream`) | `src/sama/coding/events.py` |
| Folder, diff, pull request | `src/sama/coding/workspace.py` |
| One request | `src/sama/coding/service.py` |
| Runtimes `claude_code`, `codex` | `src/sama/coding/runtime.py` |
| Tool body and progress | `src/sama/coding/tool.py`, `src/sama/coding/progress.py` |
| Commands | `src/sama/coding/commands.py`, `src/sama/gateway/telegram.py`, `src/sama/gateway/slack.py`, `src/sama/api/app.py`, `web/` |
| Columns | migration `0044`: `sessions.coding_agent`, `runs.coding_agent`, and the runtimes `claude_code` and `codex` in the checks of `agent_profiles` and `runs` |

#### 15.4.1 Assumptions

| # | Assumption |
|---|---|
| A1 | The CLIs sign in only with the run token through the model proxy (the key of the org or of sama). Sama does not build a login with a subscription of a person. |
| A2 | The gate judges one call per request, not each tool of the CLI. Claude Code has `--permission-prompt-tool`, which could send each tool to the gate later; Codex has no such hook in `exec`. |
| A3 | The CLI does not get the sama tools (no sama MCP server in the CLI). |
| A4 | The progress of the tool needs the run in the same process as the tool server (Section 19.1). |
| A5 | A message is a repository only in the forms of the parser above. A repository name with a dot needs the `github.com/` form. |
| A6 | The conformance suite (Section 5.5) does not run the coding runtimes: its scripted tool calls and sama tool checks need `mcp_client`, the sama prompt, and a gate per tool call, which these runtimes do not have. The unit tests cover the contract rules that apply: one terminal event, approval resume once, reject, cancel within 10 seconds, the time limit, the usage of the run, and the state across runs (`tests/coding/`). |
| A7 | The pull request is not a draft. The ruleset of the repository stops a merge without a review (Section 19.5). |
| A8 | The CLI writes its transcript in the home folder of the sandbox. The sama archive of compactions (Section 6.3) does not see it. The run events and the messages of the session have the text that the user saw. |

### 15.5 Coding mode (M7.9)

Coding mode makes the web app work like Claude Code and Codex for a coding task. A person
attaches a repository to a session, asks for a plan, approves or edits the plan, and then sees
the file changes as diffs, the commands and the test runs, and the checkpoints with undo. At the
end, the agent opens a pull request. Each part is control plane code or a sama tool, so it works
the same for both runtimes and for the coding agent harnesses of M7.2 (below).

| Part | Where | What it does |
|---|---|---|
| Service | `src/sama/coding_mode/service.py` | `CodingService`: attach and detach repositories, plan mode, plans (propose, approve, edit, reject), and the messages for the agent. Each change writes an audit entry. |
| Before a run | `src/sama/coding_mode/prepare.py`, `src/sama/orchestrator/sandboxed.py` | `CodingPreparer` clones the pending repositories into the session sandbox and gives a note to the run input. |
| Plan mode in the gate | `src/sama/gate/plan.py`, `src/sama/gate/decide.py` | The step `plan` refuses the tools that change files or data while the session is in plan mode (Section 16.1). |
| Tool | `src/sama/tools/plan.py`, `src/sama/coding_mode/tool.py` | `plan_propose(plan, title)` in the toolset `plan`. |
| Diffs | `src/sama/runtime/checkpoints.py`, `src/sama/orchestrator/checkpoints.py` | The command `diff` of the checkpoint module, and `CheckpointService.diff`. |
| Commands and pull requests | `src/sama/coding_mode/activity.py` | Read from the run events of the session. |
| Chat command | `src/sama/coding_mode/commands.py` | `/plan` in the web composer, Telegram, and Slack. |
| API | `src/sama/api/coding_mode.py` | The routes below. |
| Web app | `web/app/(shell)/s/[sessionId]/code/page.tsx`, `web/components/CodingPanel.tsx` | The Code page of a session. |
| Claude Code and Codex | `src/sama/coding_mode/harness.py`, `src/sama/coding/runtime.py` | `HarnessHooks`: clone, note, checkpoint, and plan mode for the runtimes of M7.2. |
| Tables | migration `0047` | `sessions.plan_mode`, `session_repos`, `session_plans`. |

**Attach a repository.** `POST /sessions/{id}/repos` takes `repo` (as for `repo_clone`), an
optional `ref` (a branch or a tag), and an optional `folder` (default: the repository name). The
service checks the values with the rules of `repo_clone` (Section 15.1). A session has at most
5 repositories, and each has its own folder. The row has the status `pending`.

- The repository clones when the next run of the session starts. `SandboxedRuntime` calls
  `CodingPreparer.prepare` after it has the session sandbox and the run token, and before it
  starts the runner. The step uses `RepoCloner` with the run token of the run. So a repository
  on the allowlist of the org clones through the git proxy, and the agent can push `sama/*`
  branches. Any other repository clones from GitHub without credentials, so only a public
  repository clones, and it is read-only.
- The row becomes `ready` (with the path, the commit, the branch, and `proxied`) or `failed`
  (with the reason). A failed clone does not stop the run.
- A sandbox can be new after a pause, a crash, or a deploy, and then its work folder is empty.
  So before each chat run the step checks the folder of each `ready` repository
  (`test -e PATH`, at most 30 seconds). A repository whose folder is gone goes back to
  `pending` and clones again in the same run, and the note names it again. A failed check keeps
  the repository as it is.
- The step adds a note after the message of the run: the repositories that it cloned, their
  folders, and whether the agent can push them, or the reason of a failed clone. The stored
  message does not change. The instruction files of the repository are not in the note: the
  agent reads `AGENTS.md` and `CLAUDE.md` in the folder, as data.
- A background review run and a `/compact` run have no coding step. Each clone has a time limit
  of 330 seconds (`git clone` stops after 300). A clone that fails or takes too long marks only
  its repository `failed` with the reason; the notes of the other repositories and of plan mode
  stay. The step never raises.
- `DELETE /sessions/{id}/repos/{repo_id}` removes the attachment. The folder in the sandbox
  stays. A clone is not a checkpoint tool call: the checkpoint of the next tool call has its
  files.

**Plan mode.** `sessions.plan_mode` is true while the session is in plan mode.

- `/plan TASK` (or `POST /sessions/{id}/plan`) turns plan mode on and sends TASK as a message.
  The coding step adds the plan mode note to each run while plan mode is on: read and explore,
  then call `plan_propose` with the plan, and stop.
- The gate refuses the tools that change files or data (`deny`, step `plan`, Section 16.1). The
  agent can read files, search, run read-only shell commands, clone a repository, ask the user,
  and keep its task list.
- `plan_propose(plan, title)` stores the plan as a new version in `session_plans` with the
  status `proposed`. An open plan of the session becomes `superseded`. The tool turns plan mode
  on, and it tells the agent to end its answer. The tool does not wait for the person: a run
  that waits for many hours would hold the sandbox and the run slot. `plan_propose` is in
  `DEFAULT_ALLOW`, because it only stores a plan that a person decides.
- A person decides the newest plan in the web app or with a chat command. `/plan approve`
  approves it. `/plan edit TEXT` approves it with TEXT in its place (`edited` is true, and
  `proposed_body` keeps the text of the agent). An approval turns plan mode off and sends the
  final plan to the agent as a message, so the agent does the work in a new run.
  `/plan reject FEEDBACK` rejects it. Plan mode stays on, and the feedback goes to the agent as
  a message. A reject without feedback sends nothing.
- When the queue refuses the message of a decision (a rate limit, a quota, or a busy rule), the
  decision goes back: the plan is `proposed` again, plan mode is as before, and the audit log has
  `plan.decision_undone`. The person sees the queue error and can try again.
- `/plan` shows plan mode and the newest plan. `/plan on` and `/plan off` turn plan mode on or
  off without a plan.
- When a person turns plan mode off by hand (`/plan off`, the switch on the Code page, or
  `PUT /sessions/{id}/plan-mode`), the next chat run of the session gets a one-time note that
  plan mode is off and the agent can change files again. Without it, the agent can act on the
  plan mode rules in its history. The first chat run that starts after the change claims the
  note with the audit entry `session.plan_mode_noted` (with its run id) under the session lock,
  so exactly one run gets it, also when runs start at the same time; a retry of that run gets it
  again. A `/compact` run and a review run do not take it. An approval sends the plan as a
  message, so it gives no note. No extra column.
- Any user who can read the session decides a plan, as for `/undo`. The work after the
  approval still passes the gate: each tool call that asks still needs its approval.
- The chat commands work in a Telegram direct chat, a Slack direct message (`/sama plan`), and
  the web composer, as the checkpoint commands do. `/plan TASK` makes the session of the chat
  when it has none.
- A plan has at most 20,000 characters, and feedback at most 4,000. The service redacts both.

**Diffs.** `GET /sessions/{id}/diff?base=N|oldest&head=M` runs
`python -m sama.runtime.checkpoints diff` in the session sandbox. The diff goes from the
checkpoint `base` (default: the oldest checkpoint of the session) to the checkpoint `head`, or
to the current files when `head` is not given. Checkpoint N holds the files before a tool call,
so `base=N&head=N-1` is what that call changed, and `base=oldest` is all changes of the session.

- The command uses `git diff-tree -M` on the trees of the shadow repository, with no external
  diff program and no text conversion. It reads the patch output of git in parts and stops git
  when the budget below is used, so a large diff does not fill the memory of the sandbox. A file has its path, the old path of a rename, the
  status (`added`, `modified`, `deleted`, `renamed`, or `type_changed`), the counts of added and
  deleted lines (none for a binary file), and its patch.
- A diff lists at most 500 files. The patch of one file has at most 100,000 characters, and all
  patches together at most 1,000,000 characters. A cut patch has `truncated`, and the files
  after the end of the budget have an empty, cut patch. The diff of the
  current files writes the index of the shadow repository under the checkpoint lock. It does not
  make a checkpoint.
- The files that a checkpoint does not hold (Section 15.3), for example `node_modules`, the
  `.git` folders, and files larger than 10 MiB, are not in a diff.
- The control plane redacts each patch. `409` when the session has no sandbox or no
  checkpoints.

**Commands and test runs.** `GET /sessions/{id}/commands` reads the `tool_call_started` and
`tool_call_finished` events of the session as the user (row-level security). It lists the calls
of `Bash` (also the shell of the second runtime) and `execute_code`, newest first, with the
command, the status (`running`, `ok`, or `failed`), and the end of the output. A command that
runs tests (for example `pytest`, `npm test`, `go test`, `cargo test`, `make test`, or a test
script in the place of a command: `sh test.sh`, `bash "scripts/run_tests.sh"`, `./test.sh`,
`cd repo && ./test.sh`; a script name as an argument, as in `cat ./test.sh`, is not; nor is a
command that only shows information (`pytest --version`, `npm test --help`) or installs a test
tool (`pip install pytest`)) has `test`
true and the last result line of the output as `summary`. `?tests=true` lists only these.

- The exit status of a pipe is the status of its last command, so `pytest | tail` ends with 0
  after a failed test. A test run whose output has a line that reports a failure or an error
  is `failed`, also when the tool call ended without an error; that line becomes the summary
  when the summary line does not show the failure. Such a line has a positive count (`1 failed`,
  `2 errors`, `3 tests failed`; not `0 tests failed`), or a status word in a status place:
  `FAIL`, `FAILED`, or `ERROR` at the start of the line (pytest, jest, `--- FAIL:` of go), at the
  end of a verbose pytest line (`tests/a.py::t FAILED [ 50%]`), or a TAP `not ok`. The same word
  in a test id, as in `test_x[FAIL] PASSED`, does not count. A test run that ended with an error is `failed`, also when its result line
  reports only passes. So a failed result never shows "passed".
- The repository note asks the agent to start a piped test command with `set -o pipefail;`,
  so the exit status of the test itself counts. sama does not turn on `pipefail` for every
  shell of the sandbox, because that changes the result of other scripts.

- Since M7.9 the output preview of a tool call keeps its start and its end (2,000 characters
  with a marker in the middle), so the result line of a long test run is in the preview. The
  preview was the start only before.

**Checkpoints and undo.** The Code page lists the checkpoints and has Undo and Restore, through
the routes of Section 15.3. Since M7.9 each checkpoint in `GET /sessions/{id}/checkpoints` has
its `commit`, and `POST /sessions/{id}/rollback` takes an optional `commit`. With it, the restore
gives `409` and changes nothing when checkpoint `number` is another checkpoint now (for example
after a new run). So a person who confirms a restore on the page gets the checkpoint that they
saw. The checkpoint command also has `snapshot --run RUN --tool TOOL`
(`CheckpointService.snapshot`): a run that does not pass the gate of the runner, such as a
coding agent CLI of M7.2, takes one checkpoint before it changes files.

**Pull requests.** `GET /sessions/{id}/pull-requests` lists the `pr_create` calls and the
`sama_pr` `open` calls of the session from the run events, with the URL of the pull request.
`POST /sessions/{id}/pull-requests` (optional `repo`, `title`, `base`, `draft`) sends a message
to the agent: commit, push a `sama/*` branch through the git proxy, and call `pr_create`. The
agent does the git work, so the git proxy and its checks apply (Section 15.1), and a person
approves the `pr_create` call on the approval card. A repository that did not clone through the
proxy cannot get a push: the message tells the agent to say that an org admin must add the
repository to the allowlist.

**API.**

| Method and path | Purpose |
|---|---|
| `GET /sessions/{id}/coding` | `plan_mode`, the attached `repos`, and the newest `plan`. |
| `GET /sessions/{id}/plans?limit=` | The plans, newest first (1 to 50, default 10). |
| `POST /sessions/{id}/repos`, `DELETE /sessions/{id}/repos/{repo_id}` | Attach (`201`) or detach (`204`) a repository. `422` for a bad value, `409` for a full session, a used folder, or a session that is not active. |
| `PUT /sessions/{id}/plan-mode` | `{on}`. |
| `POST /sessions/{id}/plan` | `{task}`: plan mode on, and the task as a message (`202` with `run_id` and `queued`). |
| `POST /sessions/{id}/plans/{plan_id}/approve` | Optional `{body}` (the edited plan). `202` with `run_id`, `queued`, and `plan`. `409` when the plan is not `proposed`. |
| `POST /sessions/{id}/plans/{plan_id}/reject` | Optional `{feedback}`. `run_id` is null without feedback. |
| `GET /sessions/{id}/diff` | The diff above. |
| `GET /sessions/{id}/commands?tests=&limit=` | The commands above (1 to 200, default 50). |
| `GET`, `POST /sessions/{id}/pull-requests` | The pull requests above, and the request to the agent (`202`). |

The routes that send a message have the answers of `POST /sessions/{id}/messages`: `402`, `409`,
and `429`. Without a coding service, the write routes answer `409`.

**Web app.** The Code page (`/s/{id}/code`, a link in the chat header) has six tabs: Repos,
Plan, Changes, Commands and tests, Checkpoints, and Pull requests. Each tab is a component that
takes the session id (`RepoAttach`, `PlanPanel`, `DiffViewer`, `TestRuns`, `Checkpoints`,
`PullRequests`), so the settings console of M7.10 can use them again. `CodingPanel` puts them
together and reads the state again every 5 seconds while the tab is visible. The diff view shows
each file with its line numbers, and it closes large files at first. The plan tab shows the plan
as Markdown with Approve, Edit and approve, and Reject. The chat shows a short card when a plan
waits for a decision, and the web composer has the `/plan` commands.

**Tables.** `session_repos` (org, session, owner, name, ref, folder, status, path, proxied,
commit, branch, error, the user who attached it; unique per session and folder) and
`session_plans` (org, session, the run, version, body, proposed body, status, edited, feedback,
the user who decided, the time; unique per session and version). Members read the rows of the
sessions that they can see (`sama_private.can_read_session`). No policy allows writes: the
control plane writes them. Migration `0047` also adds the toolset `plan` to each profile that has
the toolset `github`, and `python -m sama.admin bootstrap` gives it to new profiles.

**Claude Code and Codex (M7.2).** The runtimes `claude_code` and `codex` (Section 15.4) run a
CLI in the session sandbox, and the runner does not start it. Coding mode works for them, also
in the direct mode (`sessions.coding_agent`) and for a one-off run (`runs.coding_agent`), through
`HarnessHooks` (`src/sama/coding_mode/harness.py`), which `CodingAgentRuntime` calls:

- Before the gate check, the runtime reads plan mode. In plan mode the gate judges the name
  `coding_agent_plan` (toolset `coding_agents`, default `ask`), which plan mode does not
  block, with `open_pr` false. No MCP tool has this name, so only the runtime asks for it. The
  tool `coding_agent_run` of a sama agent stays blocked in plan mode, because it starts the CLI
  with all permissions.
- After it has the sandbox, the hooks clone the pending repositories and give the note
  (`CodingPreparer`, the same step as for the SDK runtimes; the note goes after the task). The
  hooks add the plan mode rules themselves, from the plan mode that the gate judged, so neither
  a change of plan mode during an approval nor a failed read can start a plan run without them.
- When the folder of the request is ready (also after a clone of the repository of the task)
  and before the CLI starts, the hooks take one checkpoint of the work folder
  (`CheckpointService.snapshot`, tool `claude_code` or `codex`). The tools of the CLI do not
  pass the gate of the runner, so they take no checkpoints. With this checkpoint, the diff of
  the Code page and `/undo` cover the changes of the CLI.
- In plan mode, Claude Code starts with `--permission-mode plan` in place of
  `--dangerously-skip-permissions`, and the note asks it to end with `ExitPlanMode`. The parser
  gives the input `plan` of that tool as `CliPlan`. Codex keeps its flags: its own read-only
  sandbox (bubblewrap) does not work in the E2B VM, and with it every command fails. So Codex
  gets only the plan-first note, and its answer is the plan.
- At the end of a plan mode run, the plan (`CliPlan`, else the answer of a CLI that ended
  without an error) becomes a proposed plan (`CodingService.propose`, at most 30 seconds), and
  the answer of the run says how to decide it. A failure of the CLI, for example a refused
  budget, stays a failure of the run. A cancel stops the save, and its transaction rolls
  back; a save that committed before the cancel stays. An
  approval sends the plan as a message; in the direct mode that message goes to the same CLI,
  which then works with all permissions.
- The commands and the test runs of the CLI come from its `tool_call_*` run events. For
  `Bash`, the arguments have a one-line `summary` (at most 160 characters) and not the full
  command; the commands list reads it, so a long command is cut.
- The chat shows the progress of `coding_agent_run` (`subagent_event`) as a sub-agent group
  with the name "Claude Code" or "Codex", as for other sub-agents.

**Limits and risks.**

- The read-only command check of plan mode is strict and simple: no redirect, no substitution,
  no variable, no background job, only known read commands, and an allow list of the options
  of `git` (`status`, `log`, `diff`, `show`, `ls-files`, `ls-tree`, `rev-parse`, `blame`,
  `describe`, `shortlog`, `branch`, `remote`; also after `git -C DIR`; read options such as
  `--date=`, `--since=`, `--no-merges`, `--reverse`, `--follow` are allowed). `cd DIR` with one plain
  folder is allowed, so `cd repo && git log` works; the commands after it still pass the same
  check. Commands with an option that writes or runs a
  program (for example `sort -o`, `uniq IN OUT`, `git grep -O`, `find -exec`, `rg --pre`) are
  not allowed. It refuses many safe commands. It is a guard for the plan step, not a security
  boundary: for example a git configuration that the agent wrote before plan mode can still
  run a program from `git diff`.
- Plan mode does not stop a call that a person approved before (the recorded decision,
  Section 16.1). The tool server check honors the ticket of such a call once.
- Codex in plan mode has no technical guard: the CLI runs with all permissions and only the
  note asks it not to change files. The checkpoint before the run lets a person undo a change.
- The test detection reads the command text. A test that runs through a script whose name has
  no `test` in it (for example `sh check.sh`) is a normal command.
- A diff of a session with a very large work folder (more than 50,000 files) fails, as a
  checkpoint does.

---

## 16. Safety: approvals, sandbox, secrets

### 16.1 Tool gate

Every tool call passes one gate, in this order. Each tool call id gets exactly one verdict.

0. **Authority** (M1.18): the run must not be ended, the run user must still see the
   session, and the run must not have spent its live budget. Otherwise `deny`.
1. **Toolset**: the tool must be in the run's toolsets. Otherwise `deny`. In a shared
   session the toolsets are the group set of the profile (Section 11.3). Since M6.1, a
   session with `sessions.profile_snapshot` uses only the toolsets that both the profile of
   now and the snapshot have.
2. **Hardline**: a fixed deny list that nothing overrides (for example `rm -rf /`, fork
   bombs, disk wipes, piping a remote script to a shell as root).
3. **Recorded decision**: a one-time human decision for this call id, if any. A `deny`
   policy rule wins over a recorded approval, so a newer deny applies at once (Section 5.1).
4. **Policy**: rules per org, profile, and tool give `allow`, `ask`, or `deny`. An optional
   classifier (a small model) can approve low-risk shell commands, as in Hermes `smart`
   mode. The default for unknown tools is `ask`. In a shared session, `memory` also has the
   default `ask` (Section 11.3). Since M3.4, a deferred tool gets its default from the access
   of the tool in the catalog: `read` is `allow`, `write` and `destructive` are `ask`
   (Section 10.3). A policy rule wins over this default. Since M6.1, a session with a snapshot
   also gets the verdict of the rules with the profile rules of the snapshot in place of the
   live profile rules. The stricter verdict wins, and the reason of a stricter frozen verdict
   ends with "(until the next session)". So a looser profile rule applies from the next
   session, and a stricter one applies at once.
4a. **Plan mode** (M7.9): while the session is in plan mode, a tool that can change files or
   data gets `deny` before the policy step (Section 15.5, `sama.gate.plan`). Read tools,
   `plan_propose`, and read-only `Bash` commands go on to the policy step. A recorded approval
   is not stopped. A `review` run has no plan mode.
5. **Context**: in unattended runs, `ask` becomes `deny` unless a human approved a
   pre-approval for this tool in this job version, and the call fits its connection and
   argument limits (Section 12.1).

Approvals time out after 24 hours (configurable) and then count as `deny`. Decisions are in
the audit log. A user can choose "always allow this tool for me", which writes a policy rule.

**Escalation (M5.5).** `GateService` has an optional `escalation` hook: `(org, run, tool,
arguments)` gives a reason, or nothing. With a reason, the call gets `ask` at step 4 even with
an `allow` rule, like a tool in `ALWAYS_ASK`. A `deny` rule still wins, a recorded approval of
this call still allows it, and an unattended run gets `deny` (a pre-approval does not cover it).
The reason is the reason of the approval card. The control plane sets the hook to the sensitive
actions of the browser in the user's Chrome (Section 14.2.2). A hook that raises an error gives
`ask`. The tool server tells the body of a tool when a person approved the call (a ticket of
`screen`), in the context variable `sama.tools.approved.human_approved`. The `my_chrome` target
sends it to the extension, so the user is not asked a second time.

**Review runs (M2.4).** The toolsets of a `review` run are cut to `memory` and `skills` in the
gate (step 1), so every other tool gets `deny`. The `memory` and `skill_*` tools make a proposal
for a write that needs approval (Section 9.4). `decide_approval` checks the scope of a proposal:
an `org` scope needs the org `owner`, and `agent` and `workspace` scope need a workspace `admin`.
For a proposal the role decides and the access to the session does not (Section 9.4.1). It
ignores `always_allow` for a proposal. A review run does not use the group set of a shared
session, and its `memory` calls are not asked again.

**Shared sessions.** `GateContext.shared` is true for a `chat` run of a shared session. The
toolsets then come from `effective_toolsets` (the group set), and `memory` has the default
`ask`. `decide_approval` lets only the approvers decide a non-proposal approval of a shared
session (`src/sama/gate/approvers.py`, Section 11.3).

**Code (M1.9).**

| Part | Where | What it does |
|---|---|---|
| Verdict | `src/sama/gate/decide.py` | `decide(context, tool_name, arguments)` is a pure function. It runs the steps 0 to 5 and returns a `Verdict` (`action`, `step`, `reason`). `decide_recorded` is the same without the hardline step. |
| Hardline | `src/sama/gate/hardline.py` | The fixed deny list for shell commands (`Bash`). |
| Service | `src/sama/gate/service.py` | `GateService` loads the gate context of a run from Postgres, calls `decide`, and records each verdict. It also decides and times out approvals, checks approved calls again before a resume (`reconfirm`), and keeps the tickets of the tool server. A run that is unknown or ended (`completed`, `failed`, `cancelled`) gets `deny` with no audit row (Section 4.3). |
| Wire | `src/sama/gate/wire.py` | `GateRequest` and `GateAnswer`, the body of `POST /gate`. |
| Endpoint | `src/sama/tools/server.py` | `POST /gate` on the sama tool server. The runner sends the run token. |
| Runner | `src/sama/runtime/remote_gate.py` | `RemoteGate` is the `ToolGate` of the runner. It posts each call to `/gate`. |

Flow:

1. The `PreToolUse` hook of the runner calls `RemoteGate.check` with the tool use id.
2. The tool server checks the run token and calls `GateService.check`.
3. `GateService` loads the run, its profile, the policy rules, the tool call row, and the
   approval of this call id in one transaction. It records the verdict and returns it. A
   transaction-level advisory lock on the run and the call id puts concurrent checks of one
   call id in a sequence, so only one of them creates the approval.
4. On `ask`, the adapter pauses the run (Section 5.3). The approval row already exists.

If the control plane does not answer, or `GateService` fails, the call gets `deny`. Without
a database the tool server has no gate: `/gate` answers `503`, so the runner denies every call.
A static `GatePolicy` stays for tests and local development. A job cannot set both.

Rules of the steps:

- **Tool loaders.** The native `ToolSearch` tool only loads the schema of another tool of
  the run. Every run has it, in no toolset, and it is allowed without a rule. A deny rule or
  the authority step still stops it. The loaded tool passes the gate again at its own call.
- **Tool names.** `mcp__sama__X` is the sama tool `X`. Another MCP tool
  (`mcp__server__tool`) is in no toolset until integrations add toolsets (Section 10), so it
  gets `deny`.
- **Toolsets.** `core` (`Bash`, `Read`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `Glob`,
  `Grep`, `WebFetch`), `todo` (`todo`, `TodoWrite`), `web` (`web_search`, `web_extract`),
  `clarify`, `github` (`repo_clone`, M1.16; `pr_create`, `pr_comment`, `repo_push_approval`,
  M3.7), `plan` (`plan_propose`, M7.9), and `skills` (`skills_list`, `skill_view`,
  `skill_manage`, M2.2; allowed without a rule, because the store checks the role of the
  user), and `subagents` (`Task`, `Agent`, M2.7). They come from `agent_profiles.toolsets`. An unknown name has no tools. The
  default is an empty list, so a new profile can use no tool until an admin sets its toolsets.
- **Hardline.** Recursive deletes of `/`, `~`, `$HOME`, or a top-level system folder;
  `--no-preserve-root`; fork bombs; `mkfs`, `wipefs`, `blkdiscard`; `dd`, `shred`, or `>` to
  a disk device; a remote script piped to a root shell; and recursive `chmod`, `chown`, or
  `chgrp` of `/`. A delete target is normalized first, so `/etc/../usr` is `/usr`. A
  command longer than 32,000 characters is on the list too. The check takes linear time,
  because it runs in the event loop of the tool server for all orgs: no pattern has a gap
  that can follow many start positions, and each check with a gap scans each command segment
  a fixed number of times. The
  list is a guard against accidents, not a sandbox. The sandbox is the real boundary
  (Section 16.2).
- **Call identity.** The first check of a call id binds the id to the tool name and the
  SHA-256 digest of the arguments (canonical JSON) in `tool_calls`. A later check of the
  same id with another tool or other arguments gets `deny` and does not change the row. A
  retry of the same call gets a verdict again.
- **Recorded decision.** An approval holds the tool name and the digest of the arguments.
  If the call does not match, it gets `deny`. `approved` gives
  `allow`. `rejected` and `expired` give `deny`. `pending` gives `ask`.
- **Policy.** `tool_policies` rows have a level: org (no profile, no user), profile, or user.
  The user is the user who started the run, else the user of the run token. A rule matches an
  exact tool name or a prefix that ends with `*`. At one level an exact name wins over a
  prefix. A `deny` at any level wins. An org or profile `ask` holds over a user rule. Else
  the user rule wins, then the profile rule, then the org rule. Without a rule, read-only
  tools are allowed (`ToolSearch`, `Read`, `Glob`, `Grep`, `todo`, `TodoWrite`,
  `web_search`, `web_extract`, `clarify`), the sub-agent tools `Task` and `Agent` (a run needs
  the toolset `subagents`, and each call of the sub-agent has its own check, Section 13.1),
  and `repo_clone`, which only copies data into the
  sandbox. Every other tool gets `ask`. `repo_push_approval` is in `ALWAYS_ASK`: it gets `ask`
  even with an `allow` rule, and only a `deny` rule changes that (Section 15.1). The same
  applies to each `db_<conn>_execute` tool (Section 10.4). The classifier comes later.
- **Context.** `runs.unattended` marks a run with no human. In such a run `clarify` gets
  `deny`, and `ask` becomes `deny`. In the run of a job fire (M4.2), the gate loads the job
  of the run. When the fire ran the approved version and the stored fields still have the
  approved digest, an `ask` call that a pre-approval covers becomes `allow`. A pre-approval
  covers a call when the tool name is the same, the MCP connection is the same, and each limited
  argument has one of the allowed values with the same type (`true` is not `1`). A call with no
  arguments does not match a limit. The gate also removes the toolset `cron` from a job run,
  so a job run cannot call `cronjob_manage` (Section 12.1).

Records, all in the transaction of the verdict:

- `tool_calls`: one row per call id, with the tool name, the arguments, `arguments_digest`
  (migration `0004`), `decision`, and `status` (`started` or `denied`).
- `approvals` (migration `0004`): one row per `ask`, keyed by `(run_id, call_id)`. The
  approval id is the tool use id. Columns: the tool name, the arguments (cut at 8,000
  characters), the digest, the reason, the status, `expires_at`, and the decider. RLS follows
  the session.
- `tool_policies` (migration `0004`): the rules. Unique per org, profile, user, and tool.
  Members read the org and profile rules and their own user rules. Admins read all rules.
- `audit_log`: `tool_gate.verdict` for each verdict, `approval.requested`,
  `approval.approved`, `approval.rejected`, `approval.expired`, and `tool_policy.set`.
  `tool_gate.screen` records a deny of the tool server check (below).

Approvals:

- `GateService.decide_approval` records a human decision. Only a user who can read the session
  decides (the rule of `session_visible`). In a shared session, only the requester of the run,
  the admins, and the listed approvers decide (Section 11.3). A decision after `expires_at` raises
  `ApprovalExpiredError` and the approval becomes `expired`.
- With "always allow", it writes a user rule (`allow`) for the tool and the run profile, and
  the audit row `tool_policy.set`. Since M3.2 the chat cards have the button "Always allow for
  me" (Section 9.4.2). The gate ignores "always allow" for a proposal: a rule cannot approve a
  proposal.
- `GateService.expire()` times out due approvals in batches of 500. The worker maintenance
  calls it (Section 4.3). The gate also times out an approval when it reads it.
- The API (M1.12, Section 11.4) and the channels (M1.13) call `decide_approval` and then
  `RunQueue.release_decided(org_id, run_id)`, which queues the run for its resume. The API
  also calls it after `ApprovalExpiredError`, so the run resumes as `reject` at once. The worker
  maintenance also queues decided runs, so a missed call only delays the resume.

**Tool server check.** Before each sama MCP tool call, the tool server calls
`GateService.screen`, which runs the same steps without a call id. The server refuses
`deny`. On `ask`, the server needs a ticket (M1.18, below). The digest of the ticket uses the
arguments of the MCP request as the client sent them, not the parsed arguments with defaults.

**Live authority (M1.18).** `sama.tenancy.authority.load_authority` reads the live
authority of a run: the run status, the access of the run user (the rule of
`session_visible`, with `load_grants`), and the live budget. The run user is
`runs.triggered_by_user_id`, or the owner of a private session. The live budget is the lower
of `runs.budget` and the budget of the agent profile, for `max_tokens` and `max_cost_usd`.
Since M3.10, a run of a worker bot session also stays below the rest of the daily budget of the
bot, and a disabled, removed, or spent bot is a problem of the authority (Section 11.6).

- `GateService.check` and `screen` give `deny` at the authority step when the run ended, the
  run user lost access, or the recorded model calls of the run reached the live budget.
- The model proxy refuses a call with `403` (`permission_error`) when the run ended or the
  run user lost access. This applies to every route, also to the free `count_tokens` route.
  A store that cannot read the authority gives `503`. It uses the lower of the token budget and the live budget, so a lower
  budget applies at the next call. Before it reads the authority, the proxy writes its pending
  usage records (Section 16.6), so a live budget that more than one run shares (a worker bot)
  includes all the recorded calls.
- Before an approve resume, the worker calls `GateService.reconfirm(org_id, run_id,
  call_id)`. It runs `decide_recorded`: all steps except the hardline step, which ran at the
  first check. The user rules are the rules of the run user, as for the live authority. A
  `deny` (for example a newer deny rule, or a lost membership) turns the resume into a
  `reject` with the reason. A failed check, or a check longer than 10 seconds, also rejects.
  The check runs before the dispatch, so the dispatch checks the lease just before the start. An audit row
  `tool_gate.reconfirm` records the verdict.
- **Tickets.** On `allow`, `reconfirm` writes `decision = allow` and `status = started` to
  the `tool_calls` row of the call (it makes the row if the runtime reported an approval that
  the gate did not check). That row is the ticket of the call. When `screen` gives `ask`, the
  tool server takes one ticket of the run with the same tool name and arguments digest and an
  `approved` approval, and sets the row to `finished`. Without a ticket, `ask` becomes `deny`.
  So a process in the sandbox cannot call an `ask` sama tool with the run token alone. A used
  ticket stays `finished`: a check of the same call id again does not make a new ticket. A
  process in the sandbox can use the ticket before the model does, but only for the same tool
  and arguments that the human approved.

**Deferred calls (M2.6).** `tool_call(name, arguments)` runs a tool of the `DeferredCatalog`
(Section 6.3). The gate has no verdict for `tool_call` itself. `decide` does this:

1. It checks the authority and the toolset of `tool_call`. The run needs the toolset
   `tool_search`. A `deny` rule for `tool_call` stops every deferred call. An `allow` or `ask`
   rule for `tool_call` does nothing more: the next steps decide.
2. It reads the tool name and the arguments object from the arguments. Arguments that are not
   valid (no name, a name that is not text, arguments that are not an object) give `deny`.
3. A meta tool (`tool_search`, `tool_describe`, `tool_call`) and a tool loader (`ToolSearch`)
   cannot be the target. `deny`.
4. It gives the named tool the **whole gate with its own arguments**: its toolset (the toolsets
   of deferred tools come from the catalog through `ToolsetSource`, so a profile grants a
   deferred tool with its toolset), the hardline list, its policy rules, its default, and the
   unattended rule. A tool that is in no toolset of the run gets `deny`.

So a deferred call can never get more than a direct call of the same tool. It is not a way to
call a tool with no rule, to skip an approval, or to reach a tool outside the run's toolsets.

`GateService.check` and `screen` record, ticket, and ask for approval under the **target**:
the `tool_calls` row, the approval card, the digest, and the ticket have the deferred tool
name and its arguments. So the human sees `mail_send` and the real arguments, and a "always
allow" rule is for `mail_send`. The tool server screens the `tool_call` with the arguments as
the client sent them. The ticket of an approved call matches the target tool and its
arguments, and only once. The tool server then looks up the target in its catalog by the same
name: a name that the gate judged but the catalog does not know (for example with the prefix
`mcp__sama__`) gives an error and runs nothing. A catalog tool cannot take a name that the gate
would change (the prefix, a sama tool, a meta tool).

`GateService.visible_tools` gives the tools that a run can use: the tools of its toolsets
(group set for a shared run) minus the tools that a `deny` rule blocks. It is empty for a run
that has no authority. `tool_search` and `tool_describe` use it. A gate that does not have it
gives no deferred tool: the search fails closed (Section 6.3).

### 16.2 Sandbox and runner (M1.6)

- One E2B sandbox per session, from the sama template. A deployed environment uses the
  template of its own sandbox files, `sama-runner-<content hash>`
  (`SAMA_SANDBOX_TEMPLATE`, Section 19.3). Without the setting, the template is `sama-runner`,
  which the nightly job builds from `main`. It pauses when idle and resumes on the next run,
  and the files stay.
- **Template change.** Each sandbox has the label `sama.template`. A paused sandbox keeps the
  runner of the commit that made it, and the runner and the control plane refuse unknown job
  and event fields. So the orchestrator does not resume a sandbox whose label is not the
  template of the control plane (a deploy that changes the sandbox files, or a sandbox from
  before the label). A deploy that does not change them keeps the template and the sandboxes. `resume` raises `SandboxOutdatedError` with the running sandbox. The orchestrator
  then:
  1. creates a new sandbox from its own template;
  2. moves `SandboxSettings.carry_dirs` from the home folder of the old sandbox to the new
     one, as one archive of at most 64 MB. The default is `.claude`, the folder of the Claude
     CLI, which holds the transcripts that a run resumes, and `.codex`, the threads of Codex
     (since M7.2, Section 15.4);
  3. kills the old sandbox, also when the move fails.

  Other files of the old sandbox, for example in the work folder, do not stay. Skills sync
  again on the next run. The control plane reads the archive with `read_file(max_bytes=...)`,
  which stops after the limit, so an archive that the sandbox changes after the size check
  cannot make the read unbounded. When `session_sandbox()` finds an outdated sandbox (for
  example, for a tool that reads files), it returns nothing and does not pause the sandbox,
  because a run can move its state at that time. E2B pauses it when it is idle.
- **A lost CLI session.** The runner gives the Claude adapter `cli_session_exists`. It looks
  for `<CLAUDE_CONFIG_DIR or ~/.claude>/projects/*/<session>.jsonl`. When the transcript of
  the saved session is not there (the move failed, or the sandbox was lost), a run starts a
  new SDK session, with its cost and token counts from zero, and the earlier conversation is
  not in the CLI context. An approval resume of such a session fails with `invalid_request`.
- Default limits: 2 vCPU, 4 GB RAM, 1 hour per run, 15 minutes of idle before E2B pauses the
  sandbox. Later, 24 hours of pause before the workspace is archived to Supabase Storage.
- Egress: the sama API endpoints, package registries, GitHub, and a per-org allowlist. E2B
  blocks every other host (`deny_out` is all traffic, `allow_out` is the list). An empty
  list blocks all egress. Only `SandboxSpec.open_egress` turns the block off, and only for
  development. The Docker provider does not block egress. We track this as a risk (R4).
- The sandbox has no database credentials, no provider keys, and no integration tokens. It
  has only the run token. Git goes through the sama git proxy (Section 15.1).
- Default hosts (M1.16): `DEFAULT_SANDBOX_HOSTS` in `src/sama/controlplane/settings.py` is
  `github.com`, `codeload.github.com`, `pypi.org`, `files.pythonhosted.org`, and
  `registry.npmjs.org`. Every sandbox of the control plane gets them after the host of
  `SAMA_PUBLIC_URL` and before `SAMA_SANDBOX_ALLOW_OUT`.
- **Risk: exfiltration through allowed hosts.** An allowed host accepts writes too. A prompt
  injection can tell the agent to push session data to an attacker repo on GitHub, or to
  publish a package, with a token in the injected text. The sandbox has no sama secrets, so
  the data at risk is the session files and the conversation. The git proxy (Section 15.1,
  M3.7) holds the tokens of the repos on the allowlist. `github.com` stays an allowed host,
  for public clones and package installs, so a token in injected text can still push to
  another repo. Registry uploads stay a risk
  until the egress filter checks methods and paths. The gate `ask` on `Bash` is the control
  until then.

**Code.**

| Part | Where | What it does |
|---|---|---|
| Interface | `src/sama/sandbox/base.py` | `SandboxProvider` (`create`, `resume`), `Sandbox` (`exec`, `start`, files, `keep_alive`, `pause`, `kill`), `SandboxProcess` (stdin, stdout lines, exit code). `SandboxSpec` holds the org, the session, the template, the limits, and `allow_out`. |
| E2B | `src/sama/sandbox/e2b.py` | `lifecycle.on_timeout = "pause"`. Metadata `sama.org_id`, `sama.session_id`, and `sama.template`. `resume` connects and checks the metadata. It gives a sandbox of another template to the caller in `SandboxOutdatedError`. |
| Docker | `src/sama/sandbox/docker.py` | For local development and CI. One container per session. `pause` stops the container and `resume` starts it. A process runs under `setsid` in its own process group, and `kill` kills that group in the container, then the `docker exec` client on the host. The group kill also runs when the client has already ended. |
| Local | `src/sama/sandbox/local.py` | A folder on the host, for tests only. No isolation, only a clean environment. |
| Image | `sandbox/Dockerfile` | Python 3.12, git, ripgrep, the runner, and the Claude runtime (the SDK bundles the Claude CLI). Since M7.2 also Node.js 22 and the coding agent CLIs at pinned versions: Claude Code and Codex (Section 15.4). Docker uses it, and `python -m sama.sandbox.template` builds the E2B template from it. |
| Run token | `src/sama/tenancy/run_token.py` | HS256 JWT, issuer `sama`, audience `sama-run`. Claims: run, org, workspace, session, and user (private runs only). Lifetime at most 2 hours. |
| Runner | `src/sama/runtime/runner.py` | `python -m sama.runtime.runner` in the sandbox. One process per run or resume. |
| Warm runner | `src/sama/warm.py` | `python -m sama.warm` in the sandbox: the same protocol, with the imports already done in a warm server. The control plane uses it (`WARM_RUNNER_ARGV`). |
| Orchestrator | `src/sama/orchestrator/sandboxed.py` | `SandboxedRuntime` implements `AgentRuntime`. It gets the session sandbox, issues the run token, starts the runner, and streams its events. |

**Runner protocol.** One JSON object per line.

- stdin, line 1: a `RunnerJob` with a `RunRequest` or a `ResumeRequest`, and `gate_url`, the
  gate endpoint of the control plane (Section 16.1). Tests and local development can send a
  static tool policy (`ask`, `deny`) instead.
- stdin, later lines: `{"op": "cancel"}` cancels the run. The end of stdin also cancels it, so
  a runner never outlives its orchestrator.
- stdout: one `RunEvent` per line. The last line is the terminal event. stderr: logs only.
- The environment has `SAMA_RUN_TOKEN`, `SAMA_MODEL_URL` (the model proxy, Section 16.6), and
  `SAMA_WORKDIR`. The runner uses the run token as the bearer of every MCP server and as the
  model key for the proxy. It refuses to start if a provider key, a database URL, or another
  long-lived secret is in its environment.

**Rules in the control plane.** The runner is a new process for each invocation, so
`SandboxedRuntime` enforces the contract rules that span invocations: one stream per run id,
each approval resumes once, and a cancelled run cannot resume. The runtime state of the
adapter (for Claude, the session id and the transcript) goes back to the caller in the
terminal event, as in Section 5.1, so the next runner continues the session. For now the
session-to-sandbox map and these rules are in memory. The run queue (Section 4.3) enforces
the same rules across processes: one active main run per session, one resume per approval, and no
resume after a cancel. With `PostgresSandboxDirectory`, the session-to-sandbox map is in
`sessions.sandbox_id`.

If a resume fails before the orchestrator writes the job (for example, the sandbox does not
start), the approval is not used. The run waits for approval again, and the caller can retry.
If the job write fails, the runner can have the job, so the approved call can run. The
approval is then used, and the error is not retryable. This prevents a second run of the
approved call.

**Idle timeout.** The orchestrator creates or resumes the sandbox with an idle timeout of the
wall time of the run plus 10 minutes (`SandboxSpec.idle_timeout_s`), so the sandbox does not
pause during the run. This costs no separate call before the run. A running sandbox that the
orchestrator connects to again keeps the longer of its timeout and the new one (E2B). After
the last run of the session, `keep_alive` sets the timeout back to the idle limit (15
minutes), so an idle sandbox pauses on time. E2B refuses a timeout longer than 1 hour in a
create or a connect call, so the E2B provider caps every timeout at 1 hour
(`MAX_TIMEOUT_S`). A run with the default wall time (1 hour) can thus pause in its last
10 minutes.

**Cancel.** A worker task does the sandbox work of each invocation and puts the events in a
queue. The stream reads only the queue, so a slow provider call cannot hold the stream. The
orchestrator writes a cancel line. The runner gives the Claude CLI 4 seconds.
After 7 seconds the orchestrator kills the runner. The stream ends with `RunFailed`
(`cancelled`) within 9 seconds of the cancel, also if the runner does not answer. This fits in
the 10-second cancel limit of the contract. A cancel while the sandbox or the runner starts,
or while the job write hangs, ends the stream at once. The start continues in the background, and the orchestrator then
stops the late runner and sets the idle timeout back. A cancel also wins over an
`ApprovalRequired` that the runner wrote before the cancel.

**Failures.** If the sandbox does not start, the connection to the runner fails, or the runner
ends with no terminal event, the stream ends with `RunFailed` (`sandbox_error`, `retryable`). If the provider lost the session
sandbox, the orchestrator creates a new one. The files of the old one are then lost.

**Start time.** Each `create` and `resume` logs its duration (`sama.sandbox`), and the
orchestrator logs the time to the first runner event (`sama.orchestrator`). On E2B we measured
0.34 s for a create, 0.2 s for a pause, and 0.24 s for a resume (2026-11, `ap-south-1` client).

**Warm runner.** A cold runner process spends most of its start on imports: on E2B (2 vCPU),
about 1.3 seconds to import the runner and the Claude SDK in a resumed sandbox, and 2.4 to 3.4
seconds in a new one. The control plane therefore starts `python -m sama.warm`
(`WARM_RUNNER_ARGV`) and not the runner module. The protocol on stdin, stdout, and stderr does
not change.

- The client (`sama.warm`) imports only the standard library. It connects to the warm server
  on a Unix socket (`SAMA_RUNNER_SOCKET`, else `/tmp/sama-runner.sock`) and sends its stdin,
  stdout, and stderr (`SCM_RIGHTS`), its environment, and its work folder. It then waits for
  the exit code of the runner and exits with it.
- The warm server imports the runner and the Claude runtime once. For each client, it checks
  the user of the peer (`SO_PEERCRED`) and forks a watcher, which forks the runner process.
  The runner process takes the files, the environment, and the work folder of the client, so
  it is the same as a cold runner. The watcher puts the runner in a new process group at once
  (`setpgid`, before the runner starts its work). When the client stops (for example, a cancel
  kills it), the watcher kills the runner process and its process group, so a stop at any time
  leaves no runner.
- After the client sent the job, it never runs the job a second time. If the connection to
  the server breaks after the send, the client logs the error and exits with code 1, and the
  orchestrator gets no terminal event (a sandbox error).
- The warm server starts with a short allow list of variables (`SERVER_ENV`). It never gets
  the run token. Only a forked runner gets the environment of its client.
- If there is no warm server, the client starts one (detached) for the next run and runs this
  job in its own process (cold). The code id (a hash of the Python executable and the sama
  source files) must be the same in the client and the server. If it is not, the server
  stops, and the client runs cold and starts a new server. A Unix socket and a detached
  process in `/tmp` stay through an E2B pause and resume, so a follow-up run of a session is
  warm.
- Each runner logs one line on stderr at its end:
  `runner timing mode=<cold|warm> job_s= lock_s= runtime_s= first_event_s= terminal_s= exit_s=`.
  The orchestrator logs this line with `run=<id>` (Section 18.1).

**Runner lock.** A main runner holds an exclusive `flock` on `.sama/runner.lock` in the work
folder while it builds the runtime, streams, and closes it. A side runner (M7.1,
`RunRequest.parallel`) takes no runner lock: it has a new runtime session, so it never uses the
runtime files of another run, and it runs at the same time as the main runner. The session
sandbox, the warm runner, and the idle timeout are shared: the orchestrator counts the runs of
the session and sets the idle timeout back only after the last one. The next runner of the session waits for
the lock for at most 20 seconds. If the lock is still busy, the runner builds no runtime and
sends `RunFailed` (`sandbox_error`, retryable). So the runner of a run that is still stopping
(the Claude CLI writes its transcript at the stop) is never in the same session files as the
runner of the next run. The orchestrator kills a runner that does not stop in less time than
this wait. In one process (a `ClaudeRuntime` that is not in a runner), the adapter does the
same: a run or a resume of a session first waits until the CLI of the last run of that session
has stopped. It does its checks (an active run, a used approval, a cancel) after this wait, so
two calls cannot both pass, and a cancel during the wait ends the call with `cancelled`.

**Early terminal event.** The Claude adapter sends `RunCompleted` as soon as the result
message comes, and then stops the Claude CLI (about 0.5 to 1 second). The orchestrator gives a
terminal event to the caller at once, and then waits for the runner to exit before the
session ends its run (the idle timeout and the session record). Other terminal events
(`RunFailed`, `ApprovalRequired`) still come after the stop, so the elapsed time of an approval
counts the stop. The runner lock keeps the next run of the session from starting the CLI
before this stop ends.

**Tests.** `tests/sandbox/` runs the same provider tests on `local`, `docker` (with
`SAMA_DOCKER_IMAGE`), and `e2b` (live). The conformance suite has three more targets:
`claude-runner` (local provider), `claude-warm` (local provider with the warm runner), and
`claude-docker` (sama image). The tool server of the
suite accepts only run tokens that it signed. CI builds the image and runs the Docker tests.
The nightly job builds the E2B template from `main` and runs the E2B tests.

### 16.3 Secrets

- The **sama vault** (`sama.vault`, M1.17) holds the secrets of each org. M1 stores the
  Anthropic, OpenAI, and Brave keys and the tokens of the integrations (Section 11.6).
  Connection tokens come with M3. Since M5.3, the vault holds the saved logins of the browser
  (`browser_login:<id>`, Section 14.1). Since M4.4, the
  vault also holds the signing secret of each webhook trigger (`trigger:<id>`) and the
  Composio webhook secret of an org (`composio_webhook_secret`, Section 12.2).
- **Envelope encryption.** Each entry has its own random 256-bit data key. AES-256-GCM
  encrypts the value with the data key, and the vault key (`SAMA_VAULT_KEY`, 32 bytes in
  base64) encrypts the data key. Both steps bind the org id and the name, so a row that is
  copied to another org or name does not open. The table `vault_secrets` holds only the key
  id, the wrapped data key, and the ciphertext. We do not use Supabase Vault: envelope
  encryption works on any Postgres and in the tests.
- **Access.** Only the control plane decrypts. It writes the table as the owner. RLS lets
  org admins see which names exist, never a value. Each change writes an audit row
  (`vault.secret_set`, `vault.secret_deleted`) without the value.
- **Use.** The model proxy, the media tools, and `web_search` use the org key first and our
  environment key second. An org with no key and no default gets an error, not another org's
  key. The vault keeps each value in memory for 60 seconds. If the vault cannot be read, the
  proxy returns `503` and the tools return an error. They do not fall back to our key. An org
  admin sets the Anthropic and OpenAI keys of the org on the settings page (Section 17.3).
- **Admin CLI.** `python -m sama.admin secret set|list|delete --org <uuid> --name <name>`
  reads the value from stdin and never prints it. The HTTP API has routes only for the model
  keys (Section 17.3).
- **Redaction.** One redactor in each process removes known secret values and secret
  patterns (provider keys, bearer tokens, the password in a database URL, private keys, JWTs)
  and puts `[REDACTED]` in their place. The known values are the secrets from the environment
  and all vault values. The control plane loads the vault values before it starts work, and
  then every 60 seconds. A known value stays for the life of the process, also after a change
  in the vault, because a cache or a run can still use it. Values shorter than 8 characters
  are not used. Each pattern runs in linear time on hostile text. The redactor runs on:
  - log lines (both formats),
  - the user message, the run events, the reply, and the failure message before the queue
    stores them,
  - the stored tool arguments, the approval reasons, and the audit details of the tool gate,
  - the text that the sama tool server returns and its error text,
  - trace input, output, and tool spans (Section 18.2).
- **Limits.**
  - A user message is redacted before the runtime reads it. So a user cannot give a key in
    the chat. Use the vault for keys.
  - A secret that is split over two text events is not found in `run_events`. The final
    reply is redacted in full.
  - Native tool output inside the sandbox goes to the model without redaction. The sandbox
    has no secrets (Section 16.2).
  - If the vault key is lost, the entries do not open. Set them again. Key rotation comes
    later: the key id is stored so that two keys can work at the same time.

### 16.4 Outbound requests from the control plane

Some requests start on the control plane, not in the sandbox: `web_extract`, connector
discovery and calls (MCP, HTTP API, database), OAuth flows, skill imports (Section 9.6), `@url`
references (Section 6.4), webhook deliveries, and browser navigation. Sandbox egress rules do not cover them. One outbound HTTP client enforces a URL
policy for all of them:

- Only `https` (and `http` where the org allows it) to public addresses.
- Sama resolves the host, checks every resolved address, and connects to the checked
  address, so DNS rebinding cannot change the target. It blocks private, loopback,
  link-local, and cloud metadata addresses.
- Sama checks every redirect hop again, with a limit on the number of hops.
- Org allowlists and block lists apply. Response size and time limits apply.
- A connection that has a private network route (SSH tunnel or relay, Section 10.6) may reach
  the private addresses of that route only. No other request can use the route. Since M4.13,
  an SSH tunnel listens on `127.0.0.1` of the gateway. Only its own connection gets the port.
  Every other database connection and every outbound request refuses loopback addresses, so
  they cannot reach it (`tests/db/test_database_tunnel.py`).

**Code (M1.8).** `src/sama/tools/outbound.py` has `UrlPolicy` and `OutboundClient`.

- The URL must be `https` (`http` only if `UrlPolicy.allow_http` is set). A URL with a user
  name or a password is refused.
- The client resolves the host and refuses the request if one of the addresses is not a
  public unicast address: private, loopback, link-local, shared (`100.64.0.0/10`), reserved,
  multicast, or unspecified. IPv4 addresses inside IPv6 (mapped, 6to4, Teredo) get the same
  check. Known cloud metadata addresses and names (`169.254.169.254`, `fd00:ec2::254`,
  `100.100.100.200`, `metadata.google.internal`) have their own error text.
- The client connects to the checked address. The `Host` header and the TLS server name are
  the original host, so the certificate check is unchanged. The client keeps no open
  connections between requests, so two hosts on one address cannot share a TLS connection.
- Redirects are not followed by the HTTP library. The client checks each `Location` as a new
  URL, at most 5 hops.
- Limits: 5 MB of response body, 10 seconds for each hop, and 20 seconds for all hops. The
  client asks for no compression and ignores proxy variables of the environment. It reads the
  body as raw bytes and refuses a body with a `Content-Encoding` other than `identity`, so a
  small compressed body cannot expand past the size limit.
- The client keeps no cookies and sends no cookies. It sends requests to IP addresses, so a
  kept cookie could go to another host on the same address, also for another tenant.
- `allow_hosts` (only these hosts and their subdomains) and `block_hosts` are the org lists.
  They apply to every hop. The client compares host names in their IDNA (punycode) form, in
  lower case, without a final dot. So `bücher.de` and `xn--bcher-kva.de` match the same entry.
  The org settings that fill the lists come with the org settings API (M3).
- `skill_import` uses its own policy: only `api.github.com` and `raw.githubusercontent.com`,
  and no redirect (M4.10).
- **Remote MCP (M3.5).** The MCP client and the OAuth client use `OutboundClient` with their own
  limits. They follow no redirect, because a redirect could send the credential to another
  host. MCP: 5 MB, 60 seconds for each hop, and 90 seconds in total. OAuth: 1 MB, 20 seconds
  for each hop, and 30 seconds in total. `OutboundClient.check(url)` checks a URL without a
  request, for a server that an admin adds by URL. `OutboundClient.request` can send a form
  body (`form`) and stop at the first full answer of a stream (`enough`). `FetchResult.headers`
  has the answer headers with lower-case names.
- **HTTP APIs (M4.12).** A call of an `api_<connection>_<tool>` tool and the OAuth 2 token
  request follow no redirect, for the same reason. `OutboundClient.check(url)` checks the base
  URL and the token URL when an admin adds the API. The document at `openapi_url` can follow at
  most 3 redirects, each checked, because it has no credential.
- **Custom HTTP tools (M7.3).** A call of a `custom_<name>` HTTP tool follows no redirect, and
  sama checks the origin when a person saves a version (Section 10.8).
- **Connector hosts (M4.11).** A stdio MCP server does not use the outbound client. It runs in
  its own E2B sandbox, and the E2B egress allowlist of the host limits it (Section 10.5).
- **Browser (M5.1).** The browser runs in Browserbase, not in sama, but it acts for the org, so
  `sama.browser.guard.UrlGuard` applies the same address rules (`http` is allowed). The
  service checks the URL of `browser_open` and of a new tab with DNS (`OutboundClient.check`)
  before the browser opens it. A host that does not resolve is refused. The session opens a
  CDP session on the whole browser (not on one page) and enables `Fetch` for every URL. Chrome
  then pauses every request before it goes out, also each redirect hop, the requests of
  frames in other processes, workers, and popups. Playwright routes do not see redirect hops,
  so sama does not use them for the policy.
  - A document request (a tab, a frame, a redirect hop of a document) gets the full check with
    DNS. A blocked request fails with `BlockedByClient` and does not reach the host. The tab
    goes back to the last page. A blocked `browser_open` returns an error.
  - Every other request (images, scripts, `fetch`) gets a quick check without DNS. It refuses
    a private or loopback IP address, `localhost`, and the metadata hosts, and schemes other
    than `http`, `https`, `data`, and `blob`.
  - A WebSocket of a page goes through `route_web_socket` and gets the full check with DNS. A
    blocked handshake closes with code 1008 and does not reach the host.
  - The check fails closed: when the check raises an error, the request fails.
  - The blocked URLs come from the page, so the result shows them in the untrusted block, with
    a fixed note of sama outside the block.
  - Limits: the browser resolves the host again when it connects, so DNS rebinding is not
    fully stopped. A subresource with a host name that resolves to a private address passes
    the quick check. A WebSocket of a worker is not checked. Browserbase runs outside our
    network, so it cannot reach our private addresses. The guard stops the agent from asking
    it to try.
  - **Lists of each org (M5.3).** `OrgGuard` adds the allow list and the block list of the org
    (Section 14.1) to these rules. The lists apply to the browser only. The other outbound
    requests (`web_extract`, connectors) do not use them yet.

### 16.5 Untrusted content

- Content from the web, email, webhooks, other people in groups, and files is untrusted.
  Sama marks it as untrusted in tool results. The answer of `vision_analyze` is untrusted too:
  an image can contain text that tries to give orders (Section 11.7).
- A run triggered by untrusted input uses a narrow toolset by default.
- Context files and skills are scanned for prompt-injection patterns before use.

### 16.6 Model proxy (M1.7)

The model proxy is the only path from a sandbox to a model provider (D6). It holds the
Anthropic and OpenAI keys, accepts only a run token, checks the model and the budgets,
forwards the call, and records the usage and the cost of each call. The code is in
`src/sama/modelproxy/`. `python -m sama.modelproxy` starts it.

| Route | Upstream | Recorded |
|---|---|---|
| `POST /anthropic/v1/messages` | Anthropic Messages API, streaming or not | yes |
| `POST /anthropic/v1/messages/count_tokens` | Anthropic token count (free) | no |
| `POST /openai/v1/responses` | OpenAI Responses API, streaming or not | yes |
| `POST /openai/v1/responses/compact` | OpenAI Responses compaction (M3.6), JSON with usage | yes |
| `POST /openai/v1/chat/completions` | OpenAI Chat Completions, streaming or not | yes |
| `GET /healthz` | none | no |

Other routes get `404`. `SAMA_MODEL_URL` in the sandbox is the root URL of the proxy. The
runner gives the Claude CLI `ANTHROPIC_BASE_URL=<root>/anthropic` and the run token as
`ANTHROPIC_API_KEY`. The OpenAI adapter (M3.6) uses `<root>/openai/v1` as `base_url`. The
coding agent CLIs (M7.2, Section 15.4) use the same two routes: Claude Code calls
`/anthropic/v1/messages?beta=true` (the proxy does not forward the query), and Codex calls
`/openai/v1/responses` through a custom model provider. A live probe found no gap in the proxy
for them.

**One call.**

1. **Token.** The proxy reads the run token from `x-api-key` or `Authorization: Bearer`. A
   token that is not valid gets `401`. The tenant scope comes from the token only.
2. **Request.** The body is JSON of at most 32 MB. The `model` must be in the price table, so
   a model with no known price gets `400`. A dated id (`claude-haiku-4-5-20251001`,
   `gpt-6-luna-2026-05-01`) uses the price of its base id. Options with a cost that the
   table does not have also get `400`: Anthropic `speed` other than `standard`,
   Anthropic `inference_geo` other than `global`, OpenAI `background`, and an OpenAI
   `service_tier` other than `auto` or `default`. The proxy sends every OpenAI call with
   `service_tier: default`, because `auto` can use a project tier with other prices.
3. **Budget.** The proxy adds the recorded calls and refuses the call if one limit is used up:
   - the run: `max_tokens` and `max_cost_usd` of the run `Budget`. The orchestrator puts them
     in the run token, so the sandbox cannot change them;
   - the org: `model_usd_per_month` in `orgs.limits` (calendar month, UTC);
   - the user: `model_usd_per_user_day` in `orgs.limits` (calendar day, UTC), for runs whose
     token names a user (private sessions).

   The plan of the org sets the two quotas while billing is on (Section 17.5). An operator can
   also set them by hand.

   A refused call gets `402` with the error type `budget_exceeded`, in the error format of
   the provider. The check fails closed: if the store cannot read the spend, or a record of
   the org is not written yet, the call gets `503`. The SDKs do not retry `402`. The Claude adapter maps it to
   `RunFailed(budget_exceeded)`. A call that is in progress always ends, so the total can pass
   a limit by the calls that were in progress at the same time.
4. **Forward.** The proxy sends the body unchanged with the provider key and an allowlist of
   headers (`anthropic-version`, `anthropic-beta`, `openai-beta`, `content-type`). It never
   forwards the run token. For a streaming Chat Completions call it sets
   `stream_options.include_usage`, so the last chunk has the usage. The response goes back
   unchanged, also as a stream. Upstream errors go back with their status. If the provider
   cannot be reached, the call gets `502`.
5. **Record.** The proxy reads the usage from the response or from the stream events
   (`message_start` and `message_delta`, `response.completed`, or the last chunk). It
   computes the cost and writes one row to `model_calls` before the response ends. The row
   has `key_source`: `org` when the call used the key of the org from the vault, else `sama`
   (M6.7). The cost and the budgets are the same for both sources.

**Missing usage is never free.** A pump task reads the provider response, separately from
the client. It holds at most 16 chunks for a slow client, then it waits, so a slow client
does not make the proxy keep the whole response. If the client stops reading, also before
the body starts, the pump drops the held chunks and reads on for at most 10 minutes
(`drain_s`) to get the final usage. If
the provider stream breaks, the client stream ends with an error, not with a clean end. If the final usage still is not known (the stream
stopped, or a stream event is larger than 16 MB), the proxy records a high estimate and sets
`usage_estimated`: one input token for each 2 bytes of the request, and the output limit of
the request (64,000 tokens if the request has none). The seen counts stay if they are higher.

**A lost record blocks the org.** Each record has a `call_id`, and a second write of the
same record does nothing, so a retry cannot count a call twice. The proxy tries a record
write 3 times. If all fail, it keeps the record in memory and refuses new calls of that org
with `503`. The next call tries the write again, under a lock. The record stays in the block
list until its write succeeds. A record also blocks its org while its write runs, and each write
attempt has a time limit of 5 seconds, so a write that stalls without an error cannot open the
budget. A budget check waits at most 2 seconds for the pending writes of its own org. The model
proxy, `vision_analyze`, and the media tools (`image_generate`, `text_to_speech`) share one
`UsageLedger` (`sama.modelproxy.ledger`) in the control plane. So a record that one of them
cannot write blocks new calls of that org in all of them (Sections 11.7 and 11.8). The price
table has a `media` section for the media models. The proxy does not allow those models.

**Stop.** When the proxy stops, open streams get 5 seconds to end. Then each pump stops
reading and writes its record, with an estimate if the usage is not final. The proxy builds
each record before it closes the provider stream, so a cancel during the cleanup or the
write keeps the record in the block list. The last writes have 10 seconds. A record that
still is not written goes to the log as JSON, for a manual fix. A durable queue for records
that wait is in the backlog.

**Price table.** `src/sama/modelproxy/prices.toml` has USD per million tokens for input,
output, cache reads, short cache writes (Anthropic 5 minutes, OpenAI 30 minutes), and long
cache writes (Anthropic 1 hour, OpenAI 12 hours), and USD per 1,000 Anthropic web searches. A
model can have a long-context tier: a call whose prompt (input, cache reads, and cache writes)
is more than the threshold uses the second set of prices (Claude Haiku 5.5 above 100,000
tokens, OpenAI above 272,000 tokens). The table names its source pages and the date of the
prices. A price change is a pull request.

**Usage and cost.** Anthropic `input_tokens` does not include the cache. OpenAI
`input_tokens` includes the cached tokens and the cache writes, so the proxy subtracts them.
The `Usage` in the run events is what the adapter sees. The `model_calls` record is the source
of truth for cost (D15). The conformance suite checks that the tokens of a run match the
proxy record.

**`model_calls`** (migration `0002`): `call_id` (unique), `org_id`, `workspace_id`, `session_id`, `run_id`,
`user_id`, `provider`, `model`, `endpoint`, `status_code`, the five token counts,
`web_search_requests`, `usage_estimated`, `cost_usd`, `duration_ms`, and `created_at`. The row points to its run
with `(org_id, session_id, run_id)`. RLS follows the session, as for `run_events`. The run
queue writes the `runs` rows (Section 4.3). Local runs and tests without a database use the
in-memory store.

**Keys (M1.17).** The proxy uses the org key from the vault first and the key from its
environment second (Section 16.3). The Langfuse traces (M1.14, Section 18.2) read these rows.

**Live authority (M1.18).** Before each call, the proxy reads the live authority of the run
(Section 16.1). A run that ended (a cancel ends the run) or a run user who lost access gets
`403` on every route, also on `count_tokens`. The run limits are the lower of the token claims and the live budget. The proxy prices only the costs that the table lists. For example, OpenAI
hosted tools are not priced yet.

### 16.7 Sama MCP tool server (M1.8)

The sama tools reach both runtimes through one MCP server (D3). The code is in
`src/sama/tools/`. Start it with `python -m sama.tools`.

**Transport and scope.** The server speaks streamable HTTP at `/mcp`, without MCP sessions
(stateless, JSON responses). Every request carries a run token as the bearer. A request
without a valid run token gets `401` before MCP reads it. Each tool call takes the tenant
scope (org, workspace, session, run, user) from the token, never from its arguments. So the
server acts as one server per run: the token selects the run. The token is the only guard,
so the MCP check of the `Host` header (DNS rebinding) is off: a browser cannot add the token.
`GET /healthz` returns `ok`. If the client disconnects before the answer, the server cancels
the tool call. So a `clarify` call that nobody waits for ends.

**Tools in M1.8.**

| Tool | What it does | Needs |
|---|---|---|
| `web_search(query, limit)` | Searches the web and returns up to 10 results: title, URL, and snippet. | A search key (`BRAVE_API_KEY`, Brave Search API). Without a key the tool is hidden. |
| `web_extract(urls)` | Gets 1 to 5 pages through the outbound client (Section 16.4) and returns their text. HTML becomes plain text. Text, Markdown, JSON, XML, and CSV stay as they are. Other types get an error for that URL. | Nothing. |
| `clarify(questions)` | Asks the user 1 to 5 questions. Each question has 2 to 6 choices, or none for a free answer. The call waits for the answers, at most 10 minutes. If nobody answers, the tool tells the agent to continue and state its assumptions. | A clarify broker. Without one the tool is hidden. |
| `memory(target, action, content, old_text, operations, reason)` | Saves a short note for the next sessions (Section 8.1). `target` is `agent`, `user`, or `workspace`. `action` is `add`, `patch`, or `remove`. Give `action` or `operations` (at most 20), not both. Returns the current entries and the usage. The tool has no read action. | A memory service. Without one the tool is hidden. |
| `session_search(query, limit)` | Searches past sessions that the run may see, by words and meaning, and returns up to 10 sessions with title, audience, times, and snippets (Section 7.1). An empty query lists the latest sessions. | A database. Without a service the tool is hidden. Meaning search needs an OpenAI key. Without one, the tool uses full text only. |
| `session_read(session_id, offset, limit)` | Returns up to 50 messages of one session that the run may see (Section 7.1). Gives `session not found` for any other id. | Same as `session_search`. |
| `vision_analyze(attachment_id, question)` | Asks a vision model a question about one image that a user sent in a channel (Section 11.7). `attachment_id` is the id in the `[image attached: ...]` line of the message, or `latest` (the default). The tool reads only the images of the session of the run. Any other id gives `The image was not found`. | A database and an Anthropic key. Without a service the tool is hidden. An org without a key gets the text `Image analysis is not available: no vision model key is configured.` |
| `tool_search(query, limit)`, `tool_describe(name)`, `tool_call(name, arguments)` | Find, describe, and run the deferred tools that products and connectors register (Section 6.3). The gate judges `tool_call` as a call of the tool that it names (Section 16.1). | A `DeferredCatalog` with at least one tool at the start, or the connection service (M3.5: the tools of remote MCP servers come for each run), or the custom tool service (M7.3, Section 10.8). Otherwise the three tools are hidden. |
| `connections_list()`, `connection_request(connector, scope)`, `connection_test(connector)` | List the connectors and the connections that the run can use, give the user a link to connect one, and check one (Section 10.3). `connection_request` never takes a credential. | The connection service of the control plane (M3.4). |
| `todo(todos, merge)` | Reads or writes the task list of the session. With no `todos` it reads. `merge=false` replaces the list. `merge=true` changes items by `id` and adds new items. Items have `id`, `content`, and `status` (`pending`, `in_progress`, `completed`, `cancelled`). At most 100 items. | Nothing. |

**Tools since M4.8.** The table above has the tools of M1.8 and M2. These tools came
later. Each tool is hidden when its service is missing.

| Tool | Section | Needs | Gate default |
|---|---|---|---|
| `execute_code(code, timeout_s)` | 10.2.1 | The session sandbox | `ask` |
| `image_generate(prompt, size)` | 11.8 | A database and the OpenAI key of the org | `allow` |
| `text_to_speech(text, voice)` | 11.8 | A database and the OpenAI key of the org | `allow` |
| `skill_import(url, path, ref, name, scope)` | 9.6 | The skill store | `ask` |
| `browser_open`, `browser_snapshot`, `browser_screenshot`, `browser_extract`, `browser_tabs` (M5.1) | 14.1 | A `BrowserService` (the control plane always has one since M5.5) | `allow` |
| `browser_act` (M5.1; points in vision mode since M5.2) | 14.1 | A `BrowserService` | `ask`; a sensitive action in the user's Chrome is always `ask` (14.2.2) |
| `browser_task` (M5.6) | 14.1 | The `cloud` target (the Browserbase key and project) and a `BrowserTaskRunner` (the model proxy of the process) | `ask` |

`browser_screenshot` is the only tool that returns an image: a text caption and an MCP
`ImageContent` (JPEG, base64). The browser tools put page text in an
`<untrusted_content source="browser">` block. A `BrowserError` (for example a stale ref or a
blocked URL) is an expected error: the agent gets its text, and the log has no stack trace.
The tool server closes the browser sessions when it stops.

**Untrusted content.** `web_search` and `web_extract` put their results in an
`<untrusted_content source="web">` block (Section 16.5). The tool escapes a closing tag in the
content, so a page cannot end the block early.

**Output cap and spill.** Each tool result is at most 50,000 bytes of UTF-8. A larger result
goes to a file in the session sandbox, `.sama/spill/<tool>-<id>.txt` under the work folder. The
tool returns the first 30,000 bytes, a note with the size and the path, and the last 10,000
bytes. The agent reads the file with its native file tools. If the session has no sandbox or
the write fails, the note says that the full output was not kept. The spill uses
`SandboxedRuntime.session_sandbox`. Error results do not spill. The MCP SDK makes some errors
itself, for example for arguments that are not valid. The server cuts each error text to
2,000 characters.

**Clarify broker.** `ClarifyBroker` keeps the open questions in memory. The API (M1.12, Section
11.4) and the channels (M1.13) list them with `pending(org_id, session_id)` and answer them with
`answer(request_id, answers)`. A cancel of the run or a timeout ends the wait. A run waits on
one `clarify` call at a time: a new call of the same run ends the open call of that run, and
the old call returns "no answer". When a run ends, the orchestrator calls
`cancel_run(org_id, run_id)` as a run-end hook (Section 4.3). At most 5 questions of one
session can be open. The broker is in the process of the tool server, so the API, the tool
server, and the worker run in one process for now. A move of the questions to Postgres is in
the backlog. In unattended runs the tool gate denies `clarify` (Section 16.1).

**Task list storage.** `session_todos` (migration `0003`) has one row per session: `org_id`,
`workspace_id`, `session_id`, `items` (JSON), `run_id` of the last change, and `updated_at`. A
change runs in one transaction with a row lock, so two changes at the same time do not lose an
item. RLS follows the session, as for `model_calls`. Local runs and tests without a database
use the in-memory store.

**Audit.** The server logs each tool call with the org, session, run, tool, result size, and
time (`sama.tools`). Arguments and results are not in the log, because they can contain
personal data. An unexpected error in `web_extract` logs only the error type. The HTTPX and
MCP SDK loggers log full URLs and error texts at `INFO`, so `python -m sama.tools` keeps only
their warnings. The tool gate writes the audit log rows (Section 16.1).

**Gate.** With a database, the server checks each call with the tool gate and refuses a
`deny` (Section 16.1). It still lists every tool that it can serve.

**Archive endpoint (M2.6).** `POST /archive` takes a transcript before a compaction from the
runner of the run of the token (Section 6.3). The body is JSON: `trigger`, `entries`, and `omitted`. An entry has `role` and
either `text`, or `tool_name` and `tool_input` (a JSON value). Limits: 5,000 entries, 1 MB for an
entry (bytes of the JSON text in UTF-8 for the whole entry), 8 MB for the body. The order of
the checks:

1. `401` without a valid run token. `503` when the server has no archive store.
2. `ContextArchive.authorize`: `409` when the run is not running, its user cannot see the
   session any more, the session is not active, or the run made too many archives. The server
   has read no byte of the body at this point.
3. `413` for a `Content-Length` or a body over the limit. `429` when 2 archives are in work
   (`MAX_CONCURRENT_ARCHIVES`). The check is before the body is read, so a busy server holds no
   new body.
4. `422` for a body that is not valid: a bad field, too many entries, a value that is too large.
   The entries are counted in a first parse in which every object becomes `0`, so a body with
   more than 5,000 entries is refused before the model builds an entry, and it does not
   matter how the body spells its keys.
5. `ContextArchive.archive` checks the run again and stores the entries. `409` and `503` as
   before. The error text of a failure is fixed, so it holds no data.

`ContextArchive` redacts each entry whole (the structure of a tool input), cuts it after, and
stores it. The runner sends the request. The sandbox needs the control plane host in its egress
list, like the model and gate URLs.

**Not in M1.8.** The Claude adapter does not yet map the native `TodoWrite` tool
to `todo`. The search key of the org comes from the vault, with the key of the server
environment as the default (Section 16.3).

---

## 17. Users, teams, and tenants

| Table | Purpose |
|---|---|
| `orgs` | Tenant, plan, limits, region |
| `workspaces` | Units inside an org |
| `users`, `memberships` | Users and their role per org and workspace (`owner`, `admin`, `member`) |
| `agent_profiles` | SOUL, runtime, model, toolsets, sub-agents, skills, policy, budgets |
| `channel_identities`, `channel_groups`, `pairing_codes`, `session_bindings` | Paired Slack and Telegram identities and groups, pairing codes, chat-to-session bindings |
| `vault_secrets`, `connections`, `connector_hosts` | Encrypted secrets, connections of all kinds (Section 10.3), connector host state (Section 10.5) |

- Every table has `org_id`, and most have `workspace_id`. RLS policies check membership from
  the Supabase JWT. The API also checks scope in code (two guards).
- **Now (team)**: one org for our team, invite-only sign-up.
- **Customers (M6)**: self-serve sign-up, org creation, invites, and roles are done (M6.5,
  Section 17.2). Org keys and a region per org are done (M6.7, Section 17.3). Data export,
  delete, and retention are done (M6.8, Section 17.4). Plans, usage billing with Stripe, and
  quotas per plan are done (M6.6, Section 17.5).
- **Products on top**: vorflux creates one sama workspace per hotel and adds its own tables
  and tools through the extension points (D16).

### 17.1 Data model and row-level security (M1.2)

The first Alembic migration (`migrations/versions/0001_tenancy_and_sessions.py`) creates
these tables in the `public` schema of the Supabase project `sama` (`ap-south-1`). The
SQLAlchemy models are in `src/sama/db/models.py`. A test compares the models with the
migrated schema.

| Table | Key columns |
|---|---|
| `orgs` | `name`, `slug` (unique), `plan`, `region`, `limits`, `retention_days` (null for the plan default, 1 to 3,650; migration `0035`, Section 17.4) |
| `users` | `org_id`, `auth_user_id` (the Supabase Auth user), `email`, `display_name` |
| `workspaces` | `org_id`, `name`, `slug` (unique per org) |
| `memberships` | `org_id`, `user_id`, `workspace_id` (null for the org), `role` |
| `agent_profiles` | `org_id`, `workspace_id` (null for the org), `name`, `soul`, `runtime`, `model`, `toolsets`, `group_toolsets` (migration `0017`), `subagents` (migration `0019`), `skills`, `policy`, `budget` |
| `sessions` | `org_id`, `workspace_id`, `agent_profile_id`, `audience`, `owner_user_id`, `parent_session_id`, `lineage`, `title`, `status`, `sandbox_id` (migration `0005`), `run_config`, `config_fingerprint` (migration `0006`, Section 6.1), `worker_bot_id` (migration `0021`, Section 11.6) |
| `messages` | `org_id`, `session_id`, `run_id`, `seq`, `role`, `author_user_id`, `content`, `search_vector` (generated, GIN index) and `embedded_at` (migration `0014`, Section 7.1) |
| `runs` | `org_id`, `session_id`, `agent_profile_id`, `status`, `runtime`, `config_fingerprint`, `budget`, `usage`, `runtime_state`, `failure_code`, `unattended` (migration `0004`), the queue columns `lease_owner`, `lease_expires_at`, `attempts`, `cancel_requested_at`, `pending_approval_id`, `resume_decision`, `resume_reason` (migration `0005`, Section 4.3), `trace_exported_at` (migration `0009`, Section 18.2), `compact` and `compact_focus` (migration `0018`, Section 6.3) |
| `run_events` | `org_id`, `session_id`, `run_id`, `seq`, `type`, `payload` |
| `tool_calls` | `org_id`, `session_id`, `run_id`, `call_id`, `tool_name`, `arguments`, `decision`, `status`, `result_preview` |
| `session_bindings` | `org_id`, `workspace_id`, `session_id`, `channel`, `external_key` (unique per channel) |
| `audit_log` | `org_id`, `workspace_id`, `actor_kind`, `actor_user_id`, `action`, `target_type`, `target_id`, `run_id`, `details` |
| `model_calls` | One row per model proxy call (migration `0002`, Section 16.6) |
| `session_todos` | The task list of a session, one row per session (migration `0003`, Section 16.7) |
| `approvals` | One row per tool call that asks a human (migration `0004`, Section 16.1) |
| `tool_policies` | Tool gate rules per org, profile, and user (migration `0004`, Section 16.1) |
| `context_files` | Context files per org, workspace, and profile (migration `0006`, Section 6.1) |
| `skills` | One row per skill and scope, with the current version and the status (migration `0013`, Section 9.2). The curator columns `pinned`, `view_count`, `use_count`, `patch_count`, `last_used_at`, `stale_since` and the status `stale` (migration `0032`, Section 9.7) |
| `skill_versions` | One row per version of a skill: text, files, and provenance (migration `0013`, Section 9.2) |
| `channel_identities`, `channel_groups`, `pairing_codes` | Telegram links and pairing codes (migration `0008`, Section 11.5) |
| `integrations` | The chat apps of each org, for example its Telegram bot (migration `0011`, Section 11.6) |
| `worker_bots` | One row for each worker bot: `org_id`, `workspace_id`, `agent_profile_id`, `integration_id`, `external_id` (the Slack channel; unique per integration), `name`, `daily_budget_usd`, `status`, `created_by_user_id`. A user who can see the workspace reads it (migration `0021`, Section 11.6) |
| `memories` | The notes of one scope: `org_id`, `scope` (`agent`, `user`, `workspace`), `workspace_id`, `agent_profile_id`, `user_id`, `entries` (JSON list), `revision` (migration `0012`, Section 8.1) |
| `message_embeddings` | One embedding for each embedded message: `org_id`, `session_id`, `message_id`, `embedding` (`vector(1536)`, HNSW index), `model` (migration `0014`, Section 7.1) |
| `memory_revisions` | One row per memory write: `org_id`, `memory_id`, `seq`, `action`, `entries`, `session_id`, `run_id`, `author_kind`, `author_user_id`, `reason`, `reverted_to` (migration `0012`, Section 8.1) |
| `session_compactions` | One row for each compaction of a session: `org_id`, `session_id`, `run_id`, `trigger` (`auto` or `manual`), `tokens_before`, `tokens_after`, `archive_session_id` (unique), `archive_entries`, `archive_truncated`, `completed_at` (null while the compaction is open) (migration `0018`, Section 6.3). |
| `runtime_session_items` | The items of an OpenAI runtime conversation: `org_id`, `session_id`, `conversation`, `seq` (from 0, no gaps), `item` (JSON). The key is `(session_id, conversation, seq)` (migration `0025`, Section 5.3). |
| `context_pins` | The pins of a session: `org_id`, `session_id`, `created_by_user_id`, `text` (1 to 2,000 characters), `source` (`user` or `message`), `injected_at`, `injected_run_id` (migration `0018`, Section 6.3). |
| `attachments` | One row for each image of a message: `org_id`, `session_id`, `message_id`, `name`, `mime_type`, `size_bytes`, `sha256`, `data` (`bytea`) (migration `0015`, Section 11.7) |
| `run_steers` | The notes of `/steer`: `org_id`, `session_id`, `run_id`, `user_id`, `text` (1 to 4,000 characters), `status` (`pending`, `delivered`, `requeued`, `dropped`), `delivered_at`, `requeued_run_id` (migration `0030`, Section 4.3). A member reads the rows of the sessions that the member can see. Only the control plane writes. |
| `session_handoffs` | One row for each `/handoff`: `org_id`, `workspace_id`, `session_id`, `user_id`, `from_channel`, `from_key`, `to_channel`, `to_key`, `run_ids` (migration `0030`, Section 11.1). Same read rule as `run_steers`. |
| `browser_logins` | The saved logins of the browser: `org_id`, `user_id` (null for an org login), `name` (unique for each scope, case does not matter), `site`, `created_by_user_id`. The vault entry `browser_login:<id>` has the secret (migration `0038`, Section 14.1). A member reads the org logins and their own logins. An org admin reads all. Only the control plane writes. |
| `browser_url_policies` | The URL policy of the browser of each org: `org_id` (the key), `allow_hosts`, `block_hosts` (at most 200 each), `updated_by_user_id` (migration `0038`, Section 16.4). An org admin reads the row. Only the control plane writes. |
| `data_purges` | The cleanups outside Postgres after a delete: `org_id` (no foreign key, so the row stays after an org delete), `kind` (`sandboxes`, `traces`, `browser_contexts` since migration `0036`, or `mem0` since migration `0046`), `refs`, `attempts`, `next_attempt_at`, `last_error`, `done_at` (migration `0035`, Section 17.4). An org owner or admin reads the rows of the org. Only the control plane writes. |

Rules:

- **`org_id` on every row.** `orgs.id` is the org id. A user row belongs to one org. A
  person in two orgs has two user rows with the same `auth_user_id`.
- **No cross-org links.** Each parent has a unique key on `(org_id, id)`. Each child points
  to its parent with a composite foreign key on `(org_id, parent_id)`. So a session in org A
  cannot point to a workspace, profile, or user in org B.
- **No mixed scopes.** A message, run event, or tool call points to its run with
  `(org_id, session_id, run_id)`, so the run always belongs to the same session. A session
  binding points to its session with `(org_id, workspace_id, session_id)`, so the binding
  workspace is always the session workspace. RLS on a child row follows its session, so a
  mixed row could show private data to a shared audience.
- **Roles.** A membership with `workspace_id` null gives the org role. A membership with a
  `workspace_id` gives access to that workspace. Org `owner` and `admin` can see every
  workspace of the org.
- **Audience.** A `private` session has an `owner_user_id`. Only that user can read it, and
  only while the user can see its workspace. A `shared` session can be read by every user who
  can see its workspace (Section 11.3).
- **Append-only audit.** Triggers reject `UPDATE`, `DELETE`, and `TRUNCATE` on `audit_log`.
  To delete an org, the control plane sets `sama.audit_purge = 'on'` in the transaction
  first. The purge allows only the row deletes of that org delete.
- **Private objects.** The policy helpers and the Alembic version table are in the schema
  `sama_private`. The Data API does not expose it.

Row-level security:

- RLS is on for every table. The policies are for the Supabase role `authenticated` and use
  `auth.uid()` from the user's JWT. Helper functions in the schema `sama_private` (not
  exposed by the Data API) find the user rows of `auth.uid()` and check membership.
- `authenticated` gets `SELECT` only. A user reads:
  - the orgs, users, and memberships of the orgs where the user is a member;
  - the workspaces and agent profiles that the user can see;
  - the sessions whose audience includes the user, and the messages, runs, run events, tool
    calls, bindings, compactions, pins, and runtime session items of those sessions;
  - the audit log only as an org `owner` or `admin`;
  - the `agent` and `workspace` memory (and its revisions) of the workspaces that the user can
    see, and the `user` memory of the user's own user rows (Section 8.1).
- A member reads the `attachments` of the sessions that the member can see, but only the
  columns without `data`: the grant on the table is a column grant. The bytes of an image stay
  in the control plane and the tool server (Section 11.7).
- `anon` gets no access. All writes go through the control plane. The control plane
  connects as the table owner, which RLS does not limit, and checks scope in code.
- **Closed by default.** Supabase gives `anon` and `authenticated` all rights on new objects
  in `public`. The first migration takes these default rights away for the role that runs
  the migrations. A later migration that adds a table, view, or sequence must grant rights and
  add RLS policies itself. Postgres still gives `EXECUTE` on new functions to `PUBLIC`, so a
  migration that adds a function in `public` must revoke it. A view must use
  `security_invoker = true`, or it reads the tables without RLS.
- `sama.db.as_user()` runs a transaction as `authenticated` with the claims of one user.
  The API uses it for user reads, so RLS is the second guard under the code check. The role
  and the claims are `LOCAL` to the transaction, so a pooled connection does not keep them.
- **Migrations are append-only after merge.** Do not edit a merged migration. A schema change
  gets a new revision that upgrades the existing schema and its data. Before the merge of this
  PR, `0001` was edited and the `sama` project was rebuilt with it (it had no data).
- Tests: `tests/db/` runs the migration and the RLS tests on a local Postgres with a small
  Supabase Auth shim (`tests/db/supabase_shim.sql`). The database needs pgvector (migration
  `0014`). CI runs them with a Postgres service from `pgvector/pgvector:pg17`.
  A live test (`-m live`) checks isolation on the Supabase project through the Data API and
  through SQL. The sama HTTP API repeats the check in code and reads with RLS (Section 11.4).

### 17.2 Customer sign-up, invites, and roles (M6.5)

A person signs up with Supabase Auth, makes an org, and invites other people. The code is in
`sama.orgs` (`OrgService`, the plans, and the email checks) and `sama.api.orgs` (the routes in
Section 11.4). Migration `0031` adds the table `org_invites` and the column
`orgs.created_by_auth_user_id`.

**Sign-up.** The web app signs the person up with the Supabase Auth REST API
(`/auth/v1/signup` with a PKCE `code_challenge`). When the project needs a confirmed email,
Supabase sends a link that comes back to `/login?code=`, and the web app changes the code for
a session as for a sign-in link (Section 11.4). A Supabase Auth user is not a sama user yet.
The person becomes a sama user when the person makes an org or accepts an invite.

**Org creation.** `POST /orgs` makes, in one transaction: the org on the sign-up plan, the
`users` row, the org-level `owner` membership, the workspace `Main` (`main`), the agent
profile `sama` (the same default as `python -m sama.admin bootstrap`, Section 19.4), and the
audit row `org.created`. Guards against abuse:

- `SAMA_SIGNUP` must be `open`. The default is `invite_only`: then `POST /orgs` gives `403`.
  Invites work in both modes.
- The email comes from `auth.users`, and `email_confirmed_at` must be set. sama does not use
  the `email` claim of the token or the user metadata. If the API cannot read `auth.users`,
  the check fails (`403`).
- The email domain must not be a disposable-email domain (a built-in list) or a domain in
  `SAMA_SIGNUP_BLOCKED_DOMAINS`. A subdomain of a listed domain is blocked too (`403`).
- Rate limits (`429`): one Supabase Auth user makes at most 3 orgs in 24 hours and at most 10
  orgs in total. All users together make at most 50 orgs in one hour. A transaction-level
  advisory lock serializes the checks, so parallel requests cannot pass a limit.
- The name has 1 to 100 characters. The optional slug has 3 to 48 characters (`a-z`, `0-9`,
  and `-`, with no `-` at the start or the end) and is not a reserved word. Without a slug, sama makes one from the name and adds
  a short random suffix when it is taken.

**Plans.** `sama.orgs.plans` is the seam for M6.6 (billing) and M6.9. A `Plan` has a name,
a member cap, an invite cap per day, the org limits (Section 4.3), and the run budget of the
default agent profile. A new org gets the `free` plan: 5 members (with open invites), 20
invites in a day, 2 running runs, 10 messages per user and 30 per org in a minute, and a run
budget of 40 turns, USD 1.00, and 15 minutes. The `team` plan has no caps. An unknown plan name
counts as `free`. `GET /me` gives the plan and the role of the user for each org. M6.6 adds
the paid plans and the model quotas (Section 17.5).

**Invites.** An org owner or admin invites an email address with the role `admin` or
`member`. A `member` invite can name one workspace. An invite never gives the role `owner`.

- The API makes a random token, keeps only its SHA-256 hash, and gives the token one time in
  the answer of `POST /orgs/{org_id}/invites`. sama does not send email. The web app shows the
  link `<origin>/invite#<token>`: the token is in the fragment, so it does not go to a server
  log.
- An invite ends after 7 days (`SignupPolicy.invite_days`). A new invite for the same email ends the open one. The admin
  can revoke an open invite.
- The invite is for one email. To accept it, the confirmed email of the user (from
  `auth.users`) must be the same. A user accepts with the token of the link, or with the id
  from `GET /invites`, which lists the open invites for the email of the user.
- Accepting adds the `users` row (or uses the old row of a removed member), the org
  membership, and the workspace membership. The audit rows are `invite.created`,
  `invite.revoked`, and `invite.accepted`.
- RLS: an org admin reads the invites of the org. No role reads `token_hash` through the Data
  API (a column grant). Writes go through the API only.

**Roles.** `GET /orgs/{org_id}/members` lists the members for every member of the org. An
owner or admin changes a role and the workspaces of a member, and removes a member. Only an
owner gives, changes, or removes the role `owner`. The last owner cannot leave, lose the role,
or be removed (`409`). Every member can leave. A removed member keeps the `users` row, so the
history of the org stays complete. The audit rows are `member.role_changed`,
`member.workspaces_changed`, `member.removed`, and `member.left`.

**Web app.** `/login` has a "Create an account" mode. A signed-in user without an org sees
the open invites and, when sign-up is open, the form for a new org. `/invite` reads the token
of a link, keeps it in `localStorage` for one hour while the person signs in, and accepts it.
`/members` shows the members, the roles, the invite form, and the open invites.
### 17.3 Org keys and regions (M6.7)

**Org keys.** An org can bring its own Anthropic key and its own OpenAI key. Its own Telegram
bot and Slack app came earlier (Section 11.6).

- An org admin sets, replaces, or removes a key in the web app (Settings, "Org keys and
  region"). The routes are `GET /orgs/{org_id}/model-keys`,
  `PUT /orgs/{org_id}/model-keys/{provider}` with `{"api_key": ...}`, and
  `DELETE /orgs/{org_id}/model-keys/{provider}`. `provider` is `anthropic` or `openai`. A
  member who is not an admin gets `403`. Without the vault key the routes answer `503`.
- Before it stores a key, sama checks the form of the key (`sk-ant-` or `sk-`, 20 to 512
  characters, no spaces). Then it calls `GET /v1/models` of the provider with the key. If the
  provider refuses the key (`401` or `403`), the API answers `400` and stores nothing. If the
  provider cannot be reached or gives another error, the API answers `502`.
- The key goes in the vault as `anthropic_api_key` or `openai_api_key` (Section 16.3), with the
  audit rows `vault.secret_set` and `vault.secret_deleted`. No answer, log line, or audit row
  has the key or a part of it. The list shows only if the org has a key, when it changed, and
  if sama has a default key.
- The model proxy, `vision_analyze`, `image_generate`, and `text_to_speech` use the org key
  first and the key of sama second. Each `model_calls` row records `key_source` (`org` or
  `sama`). The cost, the budgets, the quotas, and the usage pages count both sources the same
  way. Usage billing counts only `sama` (M6.6, Section 17.5).
- The routes use the vault of the model proxy in the same process, so a change applies at once
  there. Another machine keeps a vault value in its cache for at most 60 seconds.

**Regions.** Each org has a region (`orgs.region`, for example `ap-south-1`). A check
constraint allows only region ids. Sama has one deployment now, in `ap-south-1`. A second
region is not deployed. M6.7 adds the setting, the routing seam, and the operator steps.

- One deployment serves one region: its home region, `SAMA_REGION` (default `ap-south-1`).
  `SAMA_REGION_URLS` gives the web address of the deployment of each other region, as
  `region=url` pairs with commas. The address of the home region is the first
  `SAMA_WEB_ORIGINS` value, else `SAMA_PUBLIC_URL`.
- **Queue.** The worker claims only the runs of orgs in the home region. A run of an org in
  another region stays `queued`. Only a claimed run gets a run token, so the model proxy, the
  tool gate, and the sama tools serve only orgs of the home region. The scheduler, the
  workflows, and the deliveries need no check of their own: in a real setup each region has
  its own database, and a run that they make waits in the queue.
- **API.** For a member of an org in another region, the org routes answer
  `421 Misdirected Request` with `{"detail", "region", "url"}`. `url` is the address from
  `SAMA_REGION_URLS`, or `null`. A person who is not a member gets `404` as before and does
  not learn the region. `GET /me` gives `region`, `region_here`, and `region_url` for each org.
  For an org in another region, its workspaces, agent profiles, and integrations are empty
  lists. The web app shows the region and a link to the other deployment. A list that spans
  orgs (`GET /sessions`, the linked chat accounts) leaves out the rows of an org in another
  region, so one such org does not stop the list. A route that finds the org from a row (a
  skill version, a linked chat account) also answers `421` for an org in another region.
  The member and invite routes of Section 17.2 answer `421` the same way. An org that a user
  makes with self-serve sign-up gets the home region of the deployment.
- **Change.** Only an operator changes the region of an org:
  `python -m sama.admin region show|set --org <uuid> [--region <region>]`. `set` accepts only
  the home region and the regions of `SAMA_REGION_URLS`. It writes the audit row
  `org.region_changed` with the old and the new region. The web app shows the region but
  cannot change it.

**Operator steps for a second region.**

1. Make a Supabase project in the new region and a Fly.io app near it (Section 19.3). Set
   `SAMA_REGION` to the new region and `SAMA_REGION_URLS` to the address of each other
   region. Give each existing deployment the address of the new one in `SAMA_REGION_URLS`.
2. Deploy. The release command applies the migrations to the new database.
3. To move an org: stop new work of the org, and wait until its runs end. Then copy the rows
   of the org (all tables with its `org_id`) and its Supabase Auth users to the new database.
   The vault entries open only with the same `SAMA_VAULT_KEY`, so use the same key or set
   the secrets again.
4. Run `region set` on both databases. The old deployment then answers `421` with the new
   address, and the new deployment runs the queued runs of the org.
5. Connect the Telegram bot and the Slack app of the org again on the new deployment, so the
   webhooks go to its address. Check the jobs and the triggers of the org.
6. When the new deployment works, delete the rows of the org from the old database.

Until step 4 is done on the new database, the runs of the org stay queued there.

### 17.4 Data controls (M6.8)

An org can export its data, delete a user, delete the org, and set a retention. The code is
in `src/sama/datacontrols/`. The API checks the permission, and `DataControls` does the
work as the table owner.

| Route | Who | Result |
|---|---|---|
| `GET /orgs/{org_id}/export` | An org owner | A ZIP of all the data of the org |
| `GET /orgs/{org_id}/users/{user_id}/export` | The user, or an org owner | A ZIP of the data of the user |
| `DELETE /orgs/{org_id}/users/{user_id}` | The user, or an org admin. Only an owner deletes an owner. | `{sessions, runs}` |
| `DELETE /orgs/{org_id}?confirm=<slug>` | An org owner. `confirm` must be the org slug. | `{sessions, runs}` |
| `GET /orgs/{org_id}/data-settings` | A member | The retention setting, the plan default and maximum, and the retention in use |
| `PUT /orgs/{org_id}/data-settings` | An org owner or admin | Sets `retention_days` (null for the plan default) |

Errors: `404` when the org or the user does not exist, `403` for a missing role, `409` when
the user is the last owner or when runs are running, and `400` for a retention that the plan
does not allow. An export, a user delete, and a retention change write an audit row
(`data.exported`, `user.deleted`, `data.retention_changed`). An org delete removes the audit
log of the org, so the control plane logs it instead.

**Export.** The format is `sama-export/1`. The ZIP has `manifest.json`, one
`tables/<table>.jsonl` file for each table, and the stored files in `files/<table>/<id>`.

- The org export has the `orgs` row and every table with `org_id`, except `data_purges`.
  A test checks that a new table gets an export rule.
- The user export has the rows of the user only: the private sessions with their rows, the
  user rows (memberships, personal memory, rules, chat links, jobs, triggers, workflows, and
  connections), the rows that the user made or decided (task cards, approvals, audit rows),
  and the messages, steers, and pins that the user wrote in shared sessions, the invites
  that the user sent, accepted, or got, and the profile changes that the user made (M6.1). Agent profiles,
  vault secrets, and embeddings are not in a user export.
- The export never has a secret: not the vault columns (`wrapped_key`, `ciphertext`), not
  `integrations.secret_hash`, not `pairing_codes.code_hash`, not `org_invites.token_hash`,
  and not `api_keys.key_hash` (M7.6).
  Derived columns (`messages.search_vector`, `message_embeddings.embedding`) are also not in
  it. The manifest names the columns that it leaves out.
- The bytes of `attachments` and `generated_media` go to `files/`. The row gets `data_file`.
- The export reads one snapshot (`REPEATABLE READ`), so the tables agree. The control plane
  writes the ZIP to a temporary file and deletes the file after the download.
- Files in a sandbox are not in the export. The sandbox is a cache (D3).

**Delete.** A delete runs in one transaction with a lock timeout of 5 seconds:

- A session delete also deletes the `compaction` and `delegation` children. A `branch` or
  `retry` child stays and becomes a root session. The session rows cascade.
- A row that stays and points to a deleted row with a key that has no cascade gets a null.
  The code finds these keys in the models, so a new table is safe.
- A running run blocks the delete. Sama asks the run to stop, and the API answers `409`.
  The client tries again. A queued or paused run is deleted.
- A user delete removes the private sessions, the private task cards, the memberships, the
  personal memory, skills, connections, rules, chat links, jobs, triggers, workflows, and the
  vault secrets of the personal connections and triggers. Shared content stays. Its user
  columns get a null. The last owner of an org cannot be deleted. The invites that the user
  accepted and the open invites to the email of the user go too. The invites that the user sent
  stay without the user. The org keys in the vault (the provider keys of M6.7) belong to the
  org, so a user delete keeps them.
- A user delete also ends the open runs that the user started in shared sessions. A queued or
  paused run is cancelled. A running run blocks the delete, as above. A run with no user has
  no user limits (the live authority, Section 16.6), so a run of a deleted user must not go on.
- An org delete removes the org row, and every table cascades. It sets `sama.audit_purge`, so
  the audit log of the org goes too (Section 17.1). The vault rows cascade too, so the org
  keys go in the same transaction. A vault cache in another process can keep a key for its
  cache time, but no run of the org can start, because the org row is gone.
- Before the database delete, `ServiceCleanup` removes the accounts outside sama: the
  Composio accounts and triggers, the connector hosts, the Telegram webhooks, and the Slack
  tokens. A failure there is logged and does not stop the delete. The cleanup comes before
  the transaction and is not undone. Sama checks the last owner and the running runs before the
  cleanup, so a `409` after the cleanup is rare: a run that starts or an owner that leaves in
  between. The cleanup does the same work again without harm, so the client tries again.
- `Cleanup` is the hook for each account outside sama. A new service with such an account adds
  its step to `before_user_delete` or `before_org_delete`, and the step must be safe to run
  again. Billing (M6.6) ends the Stripe subscription of the org first in `before_org_delete`
  (`BillingService.close_org`, Section 17.5). A billing failure stops the org delete with the
  status of the error, before sama removes anything.
- In the same transaction the delete writes `data_purges` rows: the sandboxes of the deleted
  sessions (or all sandboxes of a deleted org), with the checkpoints of M6.4 in the work
  folder, and the Langfuse traces of the deleted runs that the exporter sent (at most 1,000
  trace ids in one row). A trace row waits one hour
  first: the exporter marks a run as sent before it sends the trace, and Langfuse stores a
  trace some time after it gets it.
- A user delete and an org delete also write a `browser_contexts` row with the Browserbase
  context ids of the deleted users (Section 14.1, at most 1,000 ids in one row). The rows of
  the table `browser_contexts` cascade from the user.
- A user delete and an org delete also write a `mem0` row with the ids of the deleted users
  (M7.5, Section 8.2). The rows of `skill_settings` cascade from the workspace.
- A user delete removes the personal saved logins of the user (they cascade) and their vault
  entries `browser_login:<id>` (M5.3). An org login that the user made stays.
- The Supabase Auth user is not deleted. A person can be a user in more than one org.

**Purge worker.** `PurgeWorker` runs in the monitor of each control plane. It claims rows
with `FOR UPDATE SKIP LOCKED` and a lease of 5 minutes.

- `sandboxes`: it finds the E2B sandboxes by the labels `sama.org_id` and `sama.session_id`,
  checks the labels again, and kills them. A full page of 100 keeps the row open for the next
  pass.
- `traces`: it calls `DELETE /api/public/traces` of Langfuse with the trace ids. Langfuse
  deletes them later.
- `browser_contexts`: it calls `DELETE /v1/contexts/{id}` of Browserbase for each id, so the
  cookies and logins of the user go. A context that Browserbase does not know counts as
  deleted.
- `mem0`: it calls `DELETE /v1/memories/?user_id=sama:<org>:<user>` of Mem0 for each user
  (M7.5). A user without memories counts as deleted. An org without a Mem0 key has nothing to
  delete, so the row is done.
- A failure waits 30 seconds times 2 to the power of the attempts, at most 6 hours. The row
  keeps the error type only. A full page waits 30 seconds. The worker forgets done rows after 30 days.
- A kind without a backend (no E2B key, no Langfuse key, no Browserbase key, or no Mem0
  provider) stays open.

**Retention.** `orgs.retention_days` is the setting of the org. Null means the default of
the plan. `PLAN_RETENTION` in `retention.py` is the seam for the plans of M6.6: each plan can
set a default and a maximum. The plans of M6.6 do not set a retention yet: every plan keeps
everything, and only an org setting turns the sweep on.

- `RetentionSweep` runs in the monitor every hour. For each org with a retention, it deletes
  the root sessions (and their `compaction` and `delegation` children) with no activity after
  the cutoff: no new message, no run that is open or ended after the cutoff, and no job that
  uses the session. A `branch` or `retry` child is checked on its own.
- It deletes at most 500 sessions of one org in one pass, in batches of 50, and it skips a
  session that is busy. A session with its children goes in one transaction, so it does not go
  when it is more than what is left of the limit. A session with more than 500 children goes
  alone, as the first delete of a pass. Each pass writes the audit row `data.retention`.
- The retention does not apply to the audit log, memory, skills, or other rows that are not
  in a session.
### 17.5 Billing and quotas (M6.6)

An org admin buys a plan with Stripe. The plan sets the caps of the org and its model quotas.
The model cost over the amount that the plan includes goes to Stripe as usage. The code is in
`sama.billing` (`BillingService`, the Stripe client, the webhook signature, and the setup
script), `sama.orgs.plans`, and `sama.api.billing`. Migration `0037` adds the tables
`org_billing` and `billing_events`.

**Plans.** `sama.orgs.plans` has four plans. Amounts are in US dollars.

| Plan | Price each month | Included model usage | Members | Runs at the same time | Model quota each month | Model quota each user and day |
|---|---|---|---|---|---|---|
| `free` | 0 | 0 | 5 | 2 | 5.00 | 2.00 |
| `pro` | 20 | 10.00 | 10 | 5 | 200.00 | 50.00 |
| `business` | 99 | 60.00 | 50 | 10 | 1000.00 | 200.00 |
| `team` | Not for sale | - | No cap | Default | No quota | No quota |

Each plan also sets the message rates and the run budget of the default agent profile. The
`team` plan is for the team of the operator. It has no billing, and Checkout refuses it.

**Billing on and off.** Billing is on only when the control plane has a Stripe key
(`SAMA_STRIPE_SECRET_KEY`) and the webhook secret (`SAMA_STRIPE_WEBHOOK_SECRET`). A live key
(`sk_live_`, `rk_live_`) also needs `SAMA_STRIPE_ALLOW_LIVE=true`. At start, the control plane
checks the key with one Stripe call. When Stripe refuses the key (`401` or `403`), billing is
off. A network error keeps billing on. When billing is off:

- The Billing page shows the plan, the usage, and the quotas, but no purchase. The purchase
  routes give `409`, and the webhook gives `404`. `GET /config` gives `billing: false`.
- A new org gets no model quota from its plan, because an admin cannot buy more. The other
  caps of the plan (members, runs, message rates, and run budget) stay.
- The usage report does not run.

When billing is on, a new free org gets the model quotas of the free plan. At start, each
free org that does not have them gets them too. A key that an operator set wins. To give one
free org no cap, use a larger value or the `team` plan. When an operator turns billing off
after it was on, the free orgs keep their quotas until the operator removes the keys.

**Stripe objects.** `python -m sama.billing.setup` makes the Stripe objects. A second run finds
them and changes only what is different. It uses lookup keys, so the code never stores a Stripe
id of a price:

- one product and one monthly base price for each paid plan (lookup keys `sama_pro_monthly`
  and `sama_business_monthly`, and the metadata `plan`). A changed price is a new price that
  takes the lookup key (`transfer_lookup_key`), because a Stripe price cannot change. The old
  price keeps its `plan` metadata, so sama still knows the plan of its subscribers;
- the billing meter `sama_model_usage` and its metered price (lookup key `sama_model_usage`,
  one US cent for each unit);
- the customer portal configuration: payment method, invoices, and cancel at the end of the
  period. A plan change in the portal is off, because a plan change is on the sama Billing page;
- with `--webhook-url`, the webhook endpoint. Stripe shows its signing secret only when it
  makes the endpoint. The script writes the secret to `--secret-file` with mode `0600` and
  never prints it. `--rotate-webhook` makes a new endpoint and deletes the old one.

The script refuses a live key without `--allow-live`.

**Checkout.** `POST /orgs/{org_id}/billing/checkout` makes the Stripe customer of the org one
time (an idempotency key), and a Checkout session in `subscription` mode with two prices: the
base price of the plan and the metered usage price. The success and cancel URLs go to
`/billing?org=<org_id>` on the origin of the request when it is in `SAMA_WEB_ORIGINS`, else on
the first origin. The audit row is `billing.checkout_started`. An org has at most one open
Checkout session (`org_billing.checkout_*`, open for one hour). The org row stays locked during
the Stripe calls. A second request for the same plan gets the open session again. A request for
another plan ends the open session first. When a session is paid but its webhook did not come
yet, a new request gets `409`. An org that pays changes the plan with
`PUT /orgs/{org_id}/billing/plan`: sama changes the base price of the subscription, and Stripe
prorates it. There is no change to `free` through the API: the admin cancels in the portal,
and the org gets `free` when the subscription ends.

**Webhook.** `POST /stripe/webhook` checks the `Stripe-Signature` header (HMAC-SHA256 with the
webhook secret, at most 5 minutes old) before it reads the event. It uses
`checkout.session.completed` and the `customer.subscription.*` events. For each event, sama
gets the subscription again from Stripe and copies its state, so the order of the events does
not matter. In one transaction, with the org row locked before the Stripe call, it records the
event id in `billing_events` (a second delivery does nothing), gets the subscription, writes
`org_billing`, and applies the plan. The lock stops a slow handler from writing an older state
over a newer one. The plan change of the Billing page uses the same path. The steps:

- `orgs.plan` and the plan keys of `orgs.limits` (other keys stay);
- the run budget of the org-wide agent profiles that still have the budget of the old plan
  (a budget that an admin changed stays);
- the audit row `org.plan_changed` with `via`.

A subscription with the status `active`, `trialing`, or `past_due` keeps the paid plan. Other
statuses give `free`. The end of an old subscription does not end a newer one. When an org has
two paid subscriptions, sama follows the newer one, logs an error, and writes the audit row
`billing.duplicate_subscription`, so an operator refunds the other. An event of the
other mode (`livemode`), of an unknown customer, or of a type that sama does not use gets
`200` and is not applied. When Stripe does not answer, the webhook gives `503`, and Stripe
sends the event again.

**Usage billing.** The ledger of the model proxy (`model_calls.cost_usd`, Section 16.6) is the
source. The control plane does these steps every 5 minutes for each paying org:

1. It adds the model cost of the current subscription period. The sum stops at the period end,
   also when the renewal webhook did not come yet. It counts only the calls on the keys of
   sama (`key_source = 'sama'`, Section 17.3): the org pays the provider itself for a call on
   its own key. The quotas count both sources, as the model proxy does.
2. It takes away the included amount of the plan, and rounds down to whole US cents.
3. It sends the new part to the meter as one meter event.

`org_billing` is an outbox. sama writes the target first (`usage_pending_cents`, with
`usage_pending_at`). Then it sends the event with the identifier
`sama-<org>-<period start>-<target>`. Then it marks the event sent. A crash or a failed call
sends the same event again, and Stripe drops the second copy. Stripe keeps the identifiers for
at least 24 hours. Thus sama does not send a pending event again after 12 hours: it logs an
error, and an operator checks the meter events in Stripe and clears the row. When the period
changes, sama first closes the old period with an event in its last second. Then it starts the
new period.

**Org delete.** The org delete (Section 17.4) calls `BillingService.close_org` first, in
`ServiceCleanup.before_org_delete`. It sends the last usage, then ends the subscription at
once with a final invoice (`invoice_now`), so Stripe bills the last usage and nothing more.
The Stripe customer stays, so Stripe can collect the final invoice. It raises an error, and
the delete stops, when billing is off and the org has a Stripe customer (`503`), when the last
usage is not in Stripe (`409`), or when Stripe does not end the subscription (`502`).

**Quotas.** The model quotas are the keys `model_usd_per_month` (the org, calendar month in
UTC) and `model_usd_per_user_day` (one user, calendar day in UTC) in `orgs.limits`. Two places
check them:

- The run queue, before it queues a message from the web app or a channel. Over a quota, the
  API gives `402` with `detail` and `quota`. Telegram and Slack reply with the same text. The
  web app shows a link to the Billing page. Jobs and workflows skip this check.
- The model proxy, before each model call (Section 16.6). A run then ends with
  `budget_exceeded`, and the message names the Billing page.

**Row-level security.** An org admin reads `org_billing` and `billing_events` of the org
through the Data API. No role writes them: the control plane writes as the table owner.

**Web app.** `/billing` is for org owners and admins, and the sidebar shows it only to them. It
shows the plan, the subscription status and renewal date, the model use of the month against
the quota, the usage of the period over the included amount, the member count, and the plans
with an upgrade or change button. "Payment method, invoices, and cancel" opens the Stripe
portal. The return from Stripe has `?org=`, so the admin comes back to the same org. After
Checkout, the page asks the API again every 3 seconds, at most 10 times, until the webhook
changed the plan. It does not say that the payment is done before then.

**Known limits.**

- Usage that sama reports after Stripe finalizes the invoice of the period is not billed.
- The Stripe calls of Checkout and of the webhook run with the org row locked. A slow Stripe
  answer (at most 20 seconds) delays other writes to that org row.
- A model call that the proxy records late, with an older `created_at`, can miss its period.
- A plan change in the middle of a period that lowers the billable amount gives no refund of
  usage that sama already reported.
- When a subscription ends, the last usage (at most 5 minutes) can be missed.
- Existing members stay when a plan with a lower member cap starts.

### 17.6 Customer web app (M6.9)

The web app (`web/`) is ready for customers: a setup guide after sign-up, an admin area, a
help center, and the same navigation, empty states, errors, and loading states on each page.
The Billing page of M6.6 (Section 17.5) is in the navigation, the setup guide, the admin
overview, and the help.

**Navigation.** The sidebar has the sessions, then three groups: Workspace (Jobs, Search,
Memory, Skills, Connections), Agents, and Org (Console, Members, Admin, Billing, Settings,
Help). The Console is the settings console of Section 17.7 (M7.10). Only
an owner or admin of an org sees Admin and Billing. A user without an org sees only the welcome page, Settings, and
Help. While the setup guide has open steps, an admin sees "Finish the setup" in the sidebar and
a card on the home page.

**Setup guide (`/onboarding`).** After a person creates an org, the web app opens the guide.
It has five steps: create the org (done), choose a plan, connect a chat app, pick or create an
agent, and send a first message.

- Plan: the guide lists the plans of `GET /billing/plans` and marks the plan of the org. The
  person keeps the plan, or an admin opens the Billing page to buy one (Section 17.5). The step
  is done when the person keeps the plan or the org has a plan other than `free`. Without
  billing (`billing_enabled` false), the guide says that the deployment sells no plans.

- Chat app: an admin connects the Telegram bot or the Slack app of the org. The Slack form
  calls `POST /orgs/{org_id}/integrations/slack/setup`, shows the link that makes the Slack app
  from the manifest, and sends the bot token and the signing secret to
  `POST /orgs/{org_id}/integrations/slack` (Section 11.6). The person can skip the step and use
  only the web app.
- Agent: the person picks an agent of the org, or an admin creates one from a template
  (assistant, research, writing, support) with `POST /orgs/{org_id}/agent-profiles` (below).
- First message: the guide makes a private session with the agent and sends the message, then
  opens the chat.
- The progress of each org is in `localStorage` (`sama.onboarding.<org_id>`). It only chooses
  what the screens show. A step is done when its result exists (an active chat app, a known
  agent) or when the person skipped it.

**New agent profile.** `POST /orgs/{org_id}/agent-profiles` takes `name`, `soul` (optional),
`runtime` and `model` (optional; the defaults of a new org), and `workspace_id` (null for the
whole org). Only an org owner or admin can do it (`403`). The profile gets the toolsets of a
new org and the run budget of the plan of the org (Section 17.2). The rules of an edit apply
(Section 11.4): a name of 1 to 80 characters, a soul of at most 20,000 characters without a
secret, a model of the runtime, and a workspace of the org (`422`). A soul needs the redactor
of the org (`503`, as for an edit). An org has at most 20 profiles (`409`). A transaction-level
advisory lock serializes the creates of an org, and the write checks the admin role again. The
audit row is `profile.created` with the runtime, the model, and whether a soul is set. A create
writes no history row: the history of Section 11.4 starts with the first change.

**Admin area (`/admin`).** Only an org owner or admin sees it. The tabs are Overview, Usage,
Audit log, Agent changes, Curator reports, Org keys and region, and Data controls. Members stays
at `/members`, because every member reads it, and Billing stays at `/billing` (Section 17.5).
The overview links to both. The usage tab shows the model cost of each day, person, agent, and
model; the Billing page shows the cost against the quotas of the plan. For an org in another region the area shows the
link to that deployment (Section 17.3).

| Route | Who | Result |
|---|---|---|
| `GET /orgs/{org_id}/usage?days=` | An org owner or admin | The model cost and tokens of the last `days` UTC days (1 to 90, default 30): totals, each day, and the 50 largest rows by person, agent, model, and key source |
| `GET /orgs/{org_id}/audit?limit=&before=&action=&actor_user_id=` | An org owner or admin | The audit rows of the org, newest first (`limit` 1 to 200, default 50), with the actor name. `before` is the id cursor. `action` is a prefix (at most 64 characters; `%` and `_` are not wildcards). `next_before` is null on the last page |
| `GET /orgs/{org_id}/curator-reports?limit=` | An org owner or admin | The curator jobs of the org (Section 9.7) and the newest ended fires (`limit` 1 to 50, default 10) with their reports |

- A member who is not an admin gets `403`. A person who is not a member gets `404`. An org in
  another region gives `421` (Section 17.3).
- The routes read as the table owner after the role check, and each query filters on
  `org_id`, as the data controls do (Section 17.4). Thus the usage counts the private sessions
  of all members. No answer has the text of a message.
- The redactor runs again on the audit details and on the report text. A report is cut after
  50,000 characters at a space, after the redaction, with `truncated: true`.
- The usage counts both key sources (Section 17.3) and shows the part on the org keys.
- Agent changes merges the history of each agent of the org (Section 11.4, M6.1), newest
  first. The revert stays on the page of the agent.
- Org keys and region is the card of Section 17.3. It moved from the settings page.
- Data controls is the retention, the org export (an owner), and the org delete (an owner, who
  types the slug of the org). `GET /me` gives the `slug` of each org for this check.

**Settings.** The settings page keeps the account, the chat apps (with the Slack form), the
browser card of each org (saved logins and the URL policy, Section 14.1), the linked chat
accounts, and the agents (an admin creates a new agent there). The browser card stays on the
settings page and not in the admin area, because each member adds personal saved logins. It adds "Your data"
for each org: the download of the user export and the delete of the own data
(`DELETE /orgs/{org_id}/users/{user_id}` with the id of the user, Section 17.4).

**Exports from the browser.** The API sends `Access-Control-Expose-Headers:
Content-Disposition`, so the web app reads the file name of an export (Section 11.4, CORS).

**Help (`/help`).** The help pages need no sign-in. Each topic has short sections in
ASD-STE100 Simplified Technical English and links to the pages that it explains: getting
started, sessions and chat, agents, Telegram and Slack, connections, memory and skills, jobs
and the task board, members and roles, the admin area, plans and billing, data and privacy, and
troubleshooting.
The text is in `web/lib/help.ts`. The sign-in page links to it.

### 17.7 Settings console (M7.10)

The web app has one settings console at `/console/<section>`, like the settings of Vorflux. A
left navigation lists the sections in four groups. Below a width of 64rem the navigation is a row
of tabs above the section. The console works on the org of the workspace that the sidebar shows.
The old pages (`/jobs`, `/skills`, `/connections`, `/members`, `/settings`, `/agents/{id}`) stay.
The console reuses their screens. Each write goes to the API, and the API writes the audit row.

| Group | Section | What it does |
|---|---|---|
| Org | Setup | The steps of the setup guide (Section 17.6) as a list, with a link to the section that does each step |
| Org | Slack | The chat apps of the org: Slack and Telegram (connect, disconnect, and a test), WhatsApp with the choice of its provider, and email (Section 11.9), and the linked chat accounts and pairing codes |
| Org | Roles | The members, the invites, and an editor of the permissions of each role |
| Org | API keys | The org API keys of Section 10.7 (M7.6) for the MCP server and the OpenAI-compatible endpoint: make (the key shows one time), list, and revoke. An org admin manages them |
| Agents | Harness | The runtime, the model, the reasoning effort, the busy mode, and the run budget of each agent |
| Agents | Tools | The toolsets of each agent, the policy of each tool for the agent, and the rules for every agent of the org |
| Agents | Sub-agents | The native sub-agents of each agent (Section 13.1): list, create, edit, and delete |
| Agents | Skills | The skill editor of Section 9.2 |
| Agents | MCP servers | The remote and hosted MCP servers of the workspace: add, test, disable, enable, tool filters, and delete |
| Automation | Webhooks | The `webhook` triggers (Section 12.2): create, the secret one time, edit, disable, rotate, delete, a test send, and the delivery log |
| Automation | Triggers | The `composio` triggers, with the same screens as the webhooks |
| Automation | Schedules | The jobs of Section 12.1, with "Run now" |
| Automation | Workflows | The workflows of Section 12.3: a YAML editor with a check, approve, start, pause, resume, delete, and the recent instances |
| Activity | Sessions | Search, rename, archive, restore, export, the direct mode of a coding agent (M7.2), and the retention of the org |

Each section has a list, a detail view, a create form, and a delete with a confirm. A section
shows its fields as read-only when the role of the user cannot change them, and says who can.
The Tools section shows the custom tools of the workspace (M7.3, `CustomToolsPanel`, Section
10.8), and the Sub-agents section shows the sub-agent library (M7.4) in its own card, with the
button "New library sub-agent" apart from "New sub-agent for <agent>". The Harness section sets the
busy mode of M7.1 (`steer`, the default, `parallel`, or `queue`) and the most parallel runs of
one session. The Harness section reads the runtimes from `options.runtimes`, so the runtimes of M7.2 show
without a change of the web app.

**Role permissions.** Migration `0040` adds the table `org_role_permissions` (`org_id`, `role`,
`permission`, `allowed`). The catalog is in `sama.orgs.permissions`:

| Key | Roles | Route that checks it |
|---|---|---|
| `jobs.create` | admin, member | `POST /orgs/{org_id}/jobs` |
| `workflows.create` | admin, member | `POST /orgs/{org_id}/workflows` |
| `triggers.create` | admin, member | `POST /workspaces/{workspace_id}/triggers` |
| `connections.add` | admin, member | `POST /workspaces/{workspace_id}/connections` and the MCP, stdio, and API routes |
| `skills.write` | admin, member | `POST /workspaces/{workspace_id}/skills` |
| `browser.logins` | admin, member | `POST /orgs/{org_id}/browser/logins` |
| `sessions.shared` | admin, member | `POST /sessions` with `audience: shared` |
| `members.invite` | admin | `POST /orgs/{org_id}/invites` |
| `agents.manage` | admin | The create, edit, and revert of agent profiles, and the tool policy routes |
| `chat_apps.manage` | admin | The routes of `/orgs/{org_id}/integrations` |

- Without a row, a permission is allowed, so an org without changes works as before. The org
  owner always has every permission. A permission only takes away: the role checks of the other
  sections stay, and a permission never gives more than they give.
- `GET /orgs/{org_id}/role-permissions` (every member) gives the catalog, the value of each key
  for `admin` and `member`, and which role the user can edit. `PUT
  /orgs/{org_id}/role-permissions/{role}` takes `{"permissions": {"<key>": bool}}` (1 to 20
  keys). Only the owner edits the role `admin`. An owner or admin edits the role `member`. The
  audit row is `role.permissions_changed` with the keys that changed.
- `GET /me` gives `permissions` for each org: the allowed keys of the role of the user.
- A refused call gives `403` with `Your role in this org cannot do this: <label>.`
- RLS: a member of the org reads the rows. Writes go through the API only.

**Tool policy per tool.** The rules are the rows of `tool_policies` (Section 16.1).

- `GET /orgs/{org_id}/agent-profiles/{profile_id}/tool-policy` gives the rules of the agent, the
  rules of the org, and each tool of `TOOLSETS` with its toolset, whether the agent has the
  toolset, and the verdict without a rule (`allow` for `DEFAULT_ALLOW`, else `ask`; the tools of
  `ALWAYS_ASK` have `always_ask`).
- `PUT` on the same path takes `{"rules": [{"tool", "action"}]}` (1 to 20 rules; `action` null
  removes the rule; a tool is a name or a prefix that ends in `*`). It writes through
  `write_change` with the kind `policy`, so the agent history and the audit row `profile.updated`
  have it.
- `GET` and `PUT /orgs/{org_id}/tool-policy` read and write the rules for every agent of the org.
  The audit row is `tool_policy.updated`.
- An org owner or admin with `agents.manage` writes. The hardline list (Section 16.1) does not
  change.

**Harness.** The agent profile has a run budget (Section 4.3) and a new column
`reasoning_effort` (`low`, `medium`, `high`, or null for the default of the model).

- `GET /orgs/{org_id}/agent-profiles/{profile_id}` gives `budget` (`max_turns`, `max_cost_usd`,
  `max_wall_time_s`, `max_tokens`) and `reasoning_effort`. `options` gives `reasoning_efforts`
  and `budget_caps` (the run budget of the plan of the org, Section 17.2).
- `PATCH` takes `budget` and `reasoning_effort`. Each budget value is above 0, `max_turns` is at
  most 10,000, and `max_wall_time_s` is at most 86,400. When the plan has a cap for a key, the
  value must be set and must not be above the cap (`422`). Both fields are in the `profile` kind
  of the history. `change_propose` cannot change them, so an agent cannot raise its own budget.
- `RunConfig.reasoning_effort` carries the effort into the run. It is not in the fingerprint when
  it is null. The Claude adapter gives it as `effort`, and the OpenAI adapter gives it as
  `ModelSettings(reasoning=Reasoning(effort=...))`. As the model, it applies from the next
  session.

**Trigger delivery log and test send.** Migration `0040` adds the table `trigger_deliveries`.

- sama writes one row for each delivery to a known trigger, also for a refused one. The
  `outcome` is `accepted`, `duplicate`, `bad_signature`, `disabled`, `bad_body`,
  `rate_limited`, `rejected`, or `test_ok`, with the HTTP status, a short redacted error, and the
  session, run, or workflow instance. sama keeps no payload. sama keeps the newest 200 rows of a
  trigger.
- `GET /workspaces/{workspace_id}/triggers/{trigger_id}/deliveries?limit=` (1 to 100) gives the
  rows, newest first, to a user who sees the trigger.
- `POST /workspaces/{workspace_id}/triggers/{trigger_id}/test` takes `{"payload": object | null,
  "start": bool}` (at most 20,000 bytes). For a webhook, sama signs the body with the secret of
  the trigger and sends it through the same check as a real delivery. A `composio` trigger has no
  secret of its own, so its test skips the signature. With `start: false` the test stops after
  the checks (`test_ok`). With `start: true` it starts the run or the workflow. The owner or an
  admin sends a test. The audit row is `trigger.test_sent`.
- RLS: a user who sees the workspace of the trigger reads the rows. Writes go through the API.

**Sessions.** `GET /sessions?status=archived` lists the archived sessions. `PATCH
/sessions/{session_id}` takes `title` and `status` (`active` or `archived`). The owner of a
private session changes it. An admin of the workspace or the org changes a shared session. A
compaction archive (Section 6.3) and a session with a compaction child keep their status, and a
session with an open run cannot be archived (`409`). The audit rows are `session.renamed`,
`session.archived`, and `session.restored`. Each session also gives `coding_agent` (`claude_code`,
`codex`, or null; Section 15.4). The Sessions section sets it with `POST
/sessions/{session_id}/coding-agent` for a user who can switch it (the owner, or an admin for a shared session, Section 15.4), and a change from the web writes the audit row
`session.coding_agent_changed` with the old and the new value.

**Test of a chat app.** `POST /orgs/{org_id}/integrations/{kind}/check` (an admin with
`chat_apps.manage`) calls Telegram `getMe` or Slack `auth.test` with the stored token and gives
`{"ok", "detail", "integration"}`. A failure sets `last_error`. A WhatsApp or email app has no
test (`409`). The audit row is `integration.checked`. The connect and disconnect routes of
WhatsApp and email (Section 11.9) also need `chat_apps.manage`.

**Enable a connection.** `POST /connections/{connection_id}/enable` moves a `disabled`
connection out of `disabled` and runs the check of Section 10.3, so the state comes from the
backend (`409` for a connection that is not disabled). The audit row is `connection.enabled`.
"Test connection" in the console is `POST /connections/{connection_id}/check`.

**Data controls.** The user export of Section 17.4 has the rows of `org_role_permissions` that
the user changed and the delivery rows of the triggers of the user.

---

## 18. Observability and cost

- **Traces**: Langfuse for both runtimes, one trace per run, with session and user ids.
- **Usage**: the model proxy records input, output, and cache tokens and the cost per call.
  Runs, sessions, users, and orgs roll this up.
- **Budgets**: hard limits per run, per user per day, and per org per month. The proxy
  rejects calls over the limit, and the run ends with `RunFailed(budget_exceeded)`
  (Section 16.6).
- **Audit log**: append-only rows for tool calls with side effects, approvals, configuration
  changes, logins, and admin actions.
- **Health**: structured logs, metrics (queue depth, run latency, sandbox start time, error
  rate), and alerts to a Slack channel.

The code is in `src/sama/observability/`. The process `python -m sama.observability` (the
monitor) sends the traces and the alerts. More than one monitor can run.

### 18.1 Structured logs (M1.14)

Each service (`api`, `gateway`, `modelproxy`, `tools`, `monitor`) calls
`configure_logging(service)` at start. Each record is one JSON line on stderr with `ts` (UTC),
`level`, `logger`, `msg`, `service`, and `exc` for an exception. `log_context(...)` adds fields
to each record in its block, also in tasks that start in the block. The worker adds
`org_id`, `workspace_id`, `session_id`, `run_id`, and `runtime` for each run. `extra=` fields
also become keys. A field never replaces `ts`, `level`, `logger`, or `msg`.

- `SAMA_LOG_FORMAT=text` gives plain lines for a terminal. `SAMA_LOG_LEVEL` sets the level.
- `httpx` and `httpcore` log only warnings, because a Telegram URL holds the bot token.
- Uvicorn uses the same handler (`log_config=None`).

**Run timing.** These lines show where the time of a run goes:

| Line | Logger | What it measures |
|---|---|---|
| `sandbox start provider= kind= ... duration_s=` | `sama.sandbox` | The create or the resume of the session sandbox. |
| `run claimed ... claim_s=` | `sama.orchestrator` | The claim of the run, from the queue (Section 4.3). |
| `worker steps run= config_s= dispatch_s= first_event_s= stream_s= finish_s=` | `sama.orchestrator` | The end of each worker step, in seconds from the start of the run: the run config, the dispatch, the first event, the last stored event, and the result. |
| `sandbox steps run= skills= acquire_s= files_s= start_s= write_s= check_s= ... first_event_s= terminal_s=` | `sama.orchestrator` | The end of each sandbox step, in seconds from the start of the sandbox work. `skills=` is `same`, `changed`, or `none` (no skills). On `changed`, `sync_s=` and `restart_s=` come after `check_s=` (Section 9.3). |
| `skill sync run= files= duration_s=` | `sama.orchestrator` | A skill sync that wrote files (Section 9.3). |
| `first runner event run= after_s=` | `sama.orchestrator` | From the runner start to its first event. |
| `runner timing mode= ... run=` | `sama.orchestrator` | The phases of the runner, from its stderr (Section 16.2). |
| `model call ...` | `sama.modelproxy` | Each model call through the proxy. |

### 18.2 Langfuse traces (M1.14)

The runner in the sandbox has no Langfuse key (D6). So the control plane builds the trace of
a run after the run ends, from rows that it already stores. The same code serves both
runtimes.

| Langfuse observation | Source | Content |
|---|---|---|
| Root, type `agent`, name `run` | `runs`, the profile name | Input: the user message. Output: the answer. Level `ERROR` and the failure code for a failed run, `WARNING` for a cancelled run. |
| `generation`, name = model | `model_calls` (Section 16.6) | Model, tokens (`input`, `output`, cache read, cache write), cost in USD, provider, endpoint, status. Start = end minus `duration_ms`. `ERROR` for a status of 400 or more. |
| `tool`, name = tool | `tool_calls`, `run_events` | Gate decision and status. `ERROR` when failed, `WARNING` when denied. With `SAMA_TRACE_CONTENT`, the redacted arguments as input and the redacted output preview of `tool_call_finished` as output, at most 4,000 characters each. |
| `agent`, name `subagent:<name>` | `run_events` (`subagent_event`) | One for each sub-agent call (M2.7, Section 13.1). The parent is the `tool` span of the `Agent` call. Metadata: the name, the number of events, `finished`, the call id, and the tokens that the CLI reports. With `SAMA_TRACE_CONTENT`, the redacted event lines as output, at most 4,000 characters. |
| `event` | `run_events` | `approval` (the tool name) and `compaction` (tokens before and after). |

- The trace id is the run id. The span ids come from a hash of the run id and the row, so the
  same run always gives the same ids. The trace name is the profile name. The session id and
  the user id are the sama ids. The tags are the runtime and the status. `SAMA_ENVIRONMENT`
  sets the Langfuse environment.
- The input and the output are cut at 20,000 characters. `SAMA_TRACE_CONTENT=off` keeps them
  out.
- Transport: OTLP/HTTP JSON to `{LANGFUSE_BASE_URL}/api/public/otel/v1/traces` with basic
  auth (public key, secret key) and the header `x-langfuse-ingestion-version: 4`. Langfuse
  Cloud closed the old `/api/public/ingestion` endpoint for traces on 2026-11-16. Sama does not
  use the OpenTelemetry SDK, because each span is sent one time when it is complete.
- Export: `TraceExporter` takes the oldest ready run with `trace_exported_at IS NULL` and
  locks it (`FOR UPDATE SKIP LOCKED`). In the same transaction it sets `trace_exported_at`
  and loads the rows. It commits, and then it sends the trace. Two monitors never send the
  same run. Migration `0009` adds the column and a partial index, and marks the runs that
  ended before it as sent.
- Ready: a completed run is ready 30 seconds after its end. A failed or cancelled run is
  ready 11 minutes after its end, because the model proxy can store the usage of a call up to
  600 seconds after the client goes away (`drain_s`, Section 16.6). A run that ended more
  than one day ago is not sent.
  These delays cover the normal path only. When all write attempts of a call record fail,
  the proxy keeps the record in memory, blocks the org, and writes it at a later request or at
  shutdown (Section 16.6). Such a record can arrive after the trace is sent. The trace then
  does not show that call. The `model_calls` table stays the source of truth for usage and
  cost. Langfuse traces are best effort.
- At most one send: Langfuse keeps each copy of a span that it gets, so a second send of a
  trace doubles its usage and cost. The exporter removes the mark and tries again at the next
  pass only when Langfuse surely did not store the trace: a connection error before the
  request left, or HTTP 401, 403, 408, 429, or 503. For all other errors (a read timeout, a
  broken answer, other HTTP errors, or rejected spans) the trace is lost and an error is
  logged. If the monitor stops between the commit and the send, the trace is lost. An error
  never holds a key.
- Delete: a data delete (Section 17.4) writes the trace ids of the deleted runs that the
  exporter sent to `data_purges`. The purge worker sends them to
  `DELETE /api/public/traces`, at most 1,000 ids in one request.

### 18.3 Error rate alerts (M1.14)

The monitor checks the runs of all orgs that ended in the last 15 minutes. The rate is
`failed / (completed + failed)`. A cancelled run does not count. When at least 5 runs ended
and the rate is 25% or more, the monitor posts one alert to the Slack incoming webhook in
`SAMA_ALERT_SLACK_WEBHOOK_URL`. The alert has the environment, the counts, and the top three
failure codes. When the rate goes below the threshold, the monitor posts one recovery
message. A new alert comes only 30 minutes or more after the last alert.

- Only one monitor checks the rate. It holds the Postgres advisory lock `0x53414D41414C5254`
  on one connection. When a check fails, the leader rolls back and gives the lock back. If it
  cannot give the lock back, it closes the database session, so that the pool does not keep
  the lock. Then the monitors try to get the lock again. A
  new leader does not know the state of the old leader, so it can post the same alert again.
- When Slack does not take the message, the monitor tries again at the next check.
- The webhook URL is a secret. It is never logged.

---

## 19. Technology stack and repository layout

| Layer | Choice |
|---|---|
| Language | Python 3.12, uv, Ruff, mypy (strict), pytest |
| API and services | FastAPI, Pydantic v2, SQLAlchemy 2 (async) with asyncpg, Alembic |
| Runtimes | `claude-agent-sdk`, `openai-agents` (separate dependency groups) |
| MCP | Official `mcp` Python SDK (streamable HTTP server) |
| Database and storage | Supabase: Postgres, RLS, pgvector, Storage, Auth |
| Hosting | Fly.io: one control plane app per environment (`sama-staging`, `sama-production`) in M1 (D18). Process groups (`api`, `gateway`, `worker`, `scheduler`) come later. |
| Sandboxes | E2B (custom template), Docker for local development |
| Browser | Browserbase plus Playwright; Chrome extension (TypeScript, MV3) |
| Connectors | Composio, remote MCP servers, hosted stdio MCP servers (E2B), database drivers (`asyncpg`, `aiomysql`, and others) with `sqlglot`, OpenAPI |
| Channels | Slack Events API and Web API (`httpx`), Telegram Bot API |
| Web UI | Next.js on Vercel |
| Observability | Langfuse, OpenTelemetry |

Repository layout (planned):

```text
src/sama/
  api/            # HTTP API, Supabase Auth tokens, minimal web page (M1.12)
  gateway/        # channel adapters: web, slack, telegram, extension
  orchestrator/   # run queue, sandbox lifecycle, events (M1.6: sandboxed.py)
  sandbox/        # sandbox providers: base, e2b, docker, local, template build
  runtime/        # contract, claude adapter, openai adapter, fake, runner entry point
  tools/          # sama MCP server and tool packs (M1.8: server, web, todo, clarify, outbound)
  memory/  search/ (M2.5)  skills/  sessions/  scheduler/  workflows/  browser/  approvals/
  modelproxy/  vault/  tenancy/ (run tokens, access rules)  observability/
  controlplane/   # one process with all the services (M1.15)
  admin/          # operator commands: bootstrap (M1.15)
skills/           # builtin skills (SKILL.md folders)
extension/        # Chrome extension (TypeScript)
web/              # Next.js app
e2e/              # Playwright end-to-end suite against staging (M7.11, Section 19.6)
sandbox/          # Dockerfile of the sama image (Docker and the E2B template)
deploy/           # Dockerfile of the control plane and the Fly.io configs (M1.15)
migrations/       # Alembic
tests/            # unit, conformance, integration
docs/
```


### 19.1 Control plane process (M1.15)

`python -m sama.controlplane` runs all services in one process (D18). They share one database
engine, one run token signer, one gate service, and one clarify broker.

| Path | Service |
|---|---|
| `/` | The API and the web page (Section 11.4), and `/telegram/<integration id>/webhook` when the vault is on (Section 11.6) |
| `/tools` | The sama MCP tool server: `/tools/mcp`, `/tools/gate`, `/tools/archive`, and `/tools/session-items` (Sections 5.3, 6.3, 16.1, and 16.7) |
| `/model` | The model proxy: `/model/anthropic` and `/model/openai` (Section 16.6) |
| `/v1`, `/mcp` | Sama as a server for other agents: the OpenAI-compatible endpoint and the MCP server, with an org API key (Section 10.7) |

- The worker runs runs in E2B sandboxes with the Claude runtime. The sandbox reaches the
  model proxy, the tool server, and the gate at `SAMA_PUBLIC_URL`. The egress allowlist of each
  sandbox holds the host of `SAMA_PUBLIC_URL`, the default hosts (Section 16.2), and the hosts in
  `SAMA_SANDBOX_ALLOW_OUT`.
- Large tool outputs spill into the session sandbox. `clarify` works, because the API answers
  in the same process.
- The trace export and the error rate alerts run in the process when their settings exist
  (Section 18).
- The scheduler of the jobs (Section 12.1) is a loop in the process. Each machine runs the
  loop, and the leader lease lets one machine tick at a time. It adds no Fly.io process.
- The workflow engine (Section 12.3) is a loop in the process too, with its own leader lease
  for the schedule tick. Its step claims use `FOR UPDATE SKIP LOCKED`, so each machine can
  run steps.
- When a background loop ends with an error, the process logs it and stops, so that Fly.io
  starts it again.
- A stop (SIGTERM or SIGINT) has this order, because the runs in the sandboxes need the model
  proxy and the tool server of the process:
  1. The worker claims no more runs. HTTP still serves while the current runs end, for at
     most 60 seconds. A run that is still open after that is ended by the next worker when its
     lease runs out (Section 4.3).
  2. uvicorn closes the listener and gives open requests (event streams too) 30 seconds.
  3. The model proxy writes its last usage records (at most 25 seconds). Then the process tries
     to pause the session sandboxes for at most 15 seconds. E2B pauses an idle sandbox by
     itself, so this step is optional.
  A second signal during step 1 cancels the worker and the monitor loops at once. The open
  runs end when their leases run out.

### 19.2 Settings (M1.15)

The process reads its settings from the environment at start. A missing or bad value stops
the start with a message that names the variable but not its value.

| Variable | Required | Use |
|---|---|---|
| `SAMA_DATABASE_URL` | Yes | The database |
| `SAMA_PUBLIC_URL` | Yes | The public address, with no path |
| `SAMA_RUN_TOKEN_KEY` | Yes | The run token key, at least 32 bytes |
| `SAMA_SUPABASE_URL`, `SAMA_SUPABASE_ANON_KEY` | Yes | Sign-in (Section 11.4) |
| `E2B_API_KEY` | Yes | The sandbox provider |
| `SAMA_VAULT_KEY` | No | The vault key, 32 bytes in base64 (Section 16.3). Without it, the vault and the integrations (Section 11.6) are off, and only the environment keys work. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | No | The provider keys of the model proxy. `OPENAI_API_KEY` is also the default key of the embed worker (Section 7.1). |
| `BRAVE_API_KEY` | No | `web_search` |
| `GEMINI_API_KEY` | No | `video_analyze` and a model of `moa` (Section 10.2.2). An org key in the vault (`gemini_api_key`) comes first. |
| `X_BEARER_TOKEN` | No | `x_search` (Section 10.2.2). An org token in the vault (`x_bearer_token`) comes first. |
| `MEM0_API_KEY` | No | The `mem0` memory provider (Section 8.2). An org key in the vault (`mem0_api_key`) comes first. |
| `SAMA_MOA_MAX_COST_USD` | No | The cost cap of one `moa` call, above 0 and at most 5. Default: `0.50` (Section 10.2.2). |
| `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` | No | The cloud browser (Section 14.1). With both, the browser tools have the `cloud` target, and it is the default target. Without them, the tools have only `my_chrome` (Section 14.2.2). The key is a setting of the process; it is not in the vault yet. |
| `SAMA_BROWSER_TASK_MODEL` | No | The Anthropic model of the browser sub-agent (Section 14.1, M5.6). Default `claude-sonnet-5-5`. It must have a price in the price table, else the process does not start. |
| `COMPOSIO_API_KEY` | No | The Composio connectors (Section 10.3). An org key in the vault (`composio_api_key`) comes first. Without both, a Composio connection cannot start. |
| `SAMA_COMPOSIO_WEBHOOK_SECRET` | No | The signing secret of the Composio webhook subscription of the sama project (Section 12.2). An org secret in the vault (`composio_webhook_secret`) comes first. Without both, sama refuses Composio events. |
| `SAMA_CONNECTORS_DIR` | No | The folder of the connector catalog. The image sets `/opt/sama/connectors/catalog`. |
| `SAMA_CONNECTOR_HOST_TEMPLATE` | No | The E2B template of the connector hosts of stdio MCP servers (Section 10.3.2). Default: `base`. |
| `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | No | Traces |
| `SAMA_ALERT_SLACK_WEBHOOK_URL` | No | Error rate alerts |
| `SAMA_GITHUB_APP_ID`, `SAMA_GITHUB_APP_SLUG`, `SAMA_GITHUB_CLIENT_ID`, `SAMA_GITHUB_APP_PRIVATE_KEY`, `SAMA_GITHUB_CLIENT_SECRET` | No | The sama GitHub App (Section 19.5). With `SAMA_GITHUB_APP_ID`, all five are required. The private key can have `\n` in place of its line breaks. Without them, the git proxy and the pull request tools are off. |
| `SAMA_SELF_REPO` | No | The sama repository of `sama_pr`, as `<owner>/<name>` (Section 15.2.1). Default: `nikhilllmno/Nikhil_claw`. |
| `SAMA_SENDGRID_API_KEY`, `SAMA_EMAIL_FROM` | No | Email delivery of jobs (Section 12.1). Without both, an `email:` delivery fails. |
| `SAMA_SIGNUP` | No | `open` lets a user with a confirmed email make an org. The default, `invite_only`, adds users only with an invite (Section 17.2). |
| `SAMA_SIGNUP_BLOCKED_DOMAINS` | No | More email domains that cannot make an org, comma-separated (Section 17.2). |
| `SAMA_EXTENSION_IDS` | No | The Chrome extension ids that may open the extension WebSocket (Section 14.2.1). Without it, every `chrome-extension://` origin may connect with a valid token. |
| `SAMA_REGION`, `SAMA_REGION_URLS` | No | The home region of the deployment (default `ap-south-1`) and the `region=url` pairs of the other regions (Section 17.3) |
| `SAMA_STRIPE_SECRET_KEY`, `SAMA_STRIPE_WEBHOOK_SECRET` | No | Billing (Section 17.5). Without both, or when Stripe refuses the key at start, billing is off and the plans add no model quota. |
| `SAMA_STRIPE_ALLOW_LIVE` | No | `true` accepts a live Stripe key. Without it, a live key turns billing off. |
| `SAMA_WEB_ORIGINS`, `SAMA_ENVIRONMENT`, `SAMA_TRACE_CONTENT`, `SAMA_SUPABASE_JWT_SECRET`, `SAMA_SANDBOX_TEMPLATE`, `SAMA_SANDBOX_ALLOW_OUT`, `SAMA_WORKER_CONCURRENCY`, `SAMA_VISION_MODEL` | No | See the module doc of `sama.controlplane.__main__` |

The provider and search keys are the defaults. An org key in the vault comes first
(Sections 16.3 and 17.3).

### 19.3 Fly.io apps and CI (M1.15)

| Environment | Fly.io app | Supabase project | Config |
|---|---|---|---|
| Staging | `sama-staging` | `sama-staging` (`ap-south-1`) | `deploy/fly.staging.toml` |
| Production | `sama-production` | `sama` (`ap-south-1`) | `deploy/fly.production.toml` |

- Both apps serve the sama region `ap-south-1` (`SAMA_REGION` is not set, so it is the
  default). A second region needs its own Fly.io app and Supabase project (Section 17.3).
- Both apps run in `sin` (Singapore), the Fly.io region nearest to `ap-south-1` (Q4). Each app
  has one machine with `shared-cpu-1x` and 1 GB of memory. The machine does not stop when it is
  idle, because the worker must listen on the queue (Section 4.3).
- `kill_timeout` is 180 seconds, longer than the whole stop of Section 19.1 (about 130
  seconds).
- `deploy/Dockerfile` builds the control plane image. It holds no agent runtime, because the
  runner runs in the sandbox. Secrets come from Fly.io secrets, never from the image.
- The release command `alembic upgrade head` applies the migrations before the new machine
  starts. A failed migration stops the deploy, and the old machine keeps running.
- `.github/workflows/deploy.yml`: after CI passes on a push to `main`, the `staging` job
  deploys that commit to staging and labels the image `main-<sha>`. The `production` job runs
  only by hand (`workflow_dispatch` with the commit SHA). It checks that the commit is on
  `main` and deploys the staging image of that commit, so production runs the tested image.
- The GitHub environments `staging` and `production` hold the secret `FLY_API_TOKEN`. Only
  `main` can deploy to them.
- Each deploy builds the E2B template `sama-runner-<content hash>` and sets
  `SAMA_SANDBOX_TEMPLATE` to it. `python -m sama.sandbox.template --print-alias` gives the
  alias. The hash (first 12 characters of a SHA-256) covers the Dockerfile, `pyproject.toml`,
  `uv.lock`, `README.md`, `.dockerignore`, the CPU, memory, and ignore settings of the build,
  and the source files that the sandbox entry points can import (`sama.runtime.runner`,
  `sama.warm`, and the modules in `PRELOAD` of `sama.warm`). The search reads every import
  statement, also in function bodies, and fails on an import by name outside `sama.warm`. A
  commit from before the content alias has no `--print-alias`, so the deploy uses
  `sama-runner-<first 12 characters of the SHA>` for it. The runner in the sandbox then has the
  runner code of the control plane that starts it. The runner and the control plane change
  their job and event models together (`sama.runtime.contract`), and both refuse unknown
  fields, so a different template is necessary when that code changes. A deploy that does not
  change the sandbox files keeps the alias, so the session sandboxes stay with their files. The
  template build needs the repository secret `E2B_API_KEY`. The nightly job still builds `sama-runner` from `main` for the E2B tests and
  for local use (Section 16.2).
- A template stays in E2B after its deploy. Old templates are removed by hand.
- A paused session sandbox of an older template is replaced on its next run. The CLI
  transcripts move to the new sandbox, and other files do not (Section 16.2).

### 19.4 First org (M1.15)

`python -m sama.admin bootstrap --org-slug <slug> --email <email>` creates the first org, an
org-level `owner` membership, the workspace `main`, and the agent profile `sama` (model
`claude-sonnet-5-5`, toolsets `core`, `todo`, `web`, `clarify`, `github`, `skills`). The owner signs in on the web
page one time first, so that the email finds the Supabase Auth user. A second run adds only
what is missing. Run it on the app machine with `fly ssh console`.

### 19.5 GitHub App (M3.7)

A person must do these steps. Sama cannot create a GitHub App or a ruleset by itself.

1. Make the form of the App manifest:
   `python -m sama.admin github-app manifest --public-url https://<host> --out form.html`.
   Add `--org <github-org>` to make an App that a GitHub organization owns. Without it, the
   personal account of the person owns the App. Add `--name <name>` if the name `sama` is
   taken: App names are unique on GitHub.
2. Open `form.html` in a browser that is signed in to GitHub, and click the button. GitHub
   shows the App and creates it. Then GitHub opens
   `<public_url>/github/app/created?code=<code>`. The page shows the next command. The code
   works one time, for 1 hour.
3. Run `python -m sama.admin github-app convert <code> --out-dir <private folder>
   --fly-app <app>`. It writes the private key and the client secret to files with mode
   `0600` and prints a `flyctl secrets set` command. The command reads the files, so the
   values are not in the output. Run that command, then delete the two files.
4. In the App settings on GitHub, keep the webhook off. Keep "Request user authorization
   (OAuth) during installation" on: the manifest sets it, and the install needs it.
5. For each sama org, an org admin calls `POST /orgs/{org_id}/github/install` and opens the
   URL of the answer. The same person must own the GitHub account: an owner of the GitHub
   organization, or the GitHub user. On GitHub, the person installs the App on the GitHub
   account, with "Only select repositories". GitHub sends the person back to
   `<public_url>/github/setup`, which shows the sama org and the GitHub account. Check them and
   click "Connect". The page then shows "GitHub is connected".
6. **Required: a ruleset on the default branch of each repo.** In the repo (or the GitHub
   organization) settings, open Rules, then Rulesets, and add a branch ruleset:
   - Target: the default branch.
   - Rules: "Require a pull request before merging" with at least 1 approval, "Block force
     pushes", and "Restrict deletions".
   - Bypass list: empty, or only people. **Do not add the sama App to the bypass list.**

   The App has `contents: write`, so without this ruleset a token of the App could merge or
   push to the default branch. The proxy refuses such pushes, and sama has no merge tool. The
   ruleset is the second wall.
7. An org admin adds each repo to the allowlist:
   `POST /orgs/{org_id}/github/repos` with `{"repo": "<owner>/<name>"}`. Add
   `"protected_paths"` to change the default list (Section 15.1).
8. Give the agent profiles the toolset `github`. The tools `pr_create` and `pr_comment` get
   `ask` by default. An admin can add `allow` rules in `tool_policies`.
9. **For `sama_pr` (M6.2).** Install the App on the sama repository (`SAMA_SELF_REPO`) and add
   the repository to the allowlist (steps 5 and 7). In the ruleset of step 6, also turn on
   "Require review from Code Owners". The file `.github/CODEOWNERS` of the default branch
   names the owners. Each owner must have write access to the repository. Then give the agent
   profile the toolset `self`. `sama_pr` gets `ask` by default.

The process turns the App on when `SAMA_GITHUB_APP_ID` is set (Section 19.2). Then the API
has the routes `/orgs/{org_id}/github/...`, the pages `/github/app/created` and
`/github/setup`, and the git proxy at `/git`.

### 19.6 End-to-end tests on staging (M7.11)

The Playwright suite in `e2e/` (TypeScript) tests the main flows on a deployed system. It
drives the web app in Chromium, as a person does. It uses the API for flows that have no page
yet (triggers, workflows) and to check results. A test of a tool also reads the run trace
(`GET /runs/{run_id}/trace`) and checks that the tool ran without an error, because a model
can give the expected words without the tool. `e2e/README.md` lists the flows and the rules
for tests.

- **When.** The workflow `.github/workflows/e2e.yml` runs it against staging each night at
  21:30 UTC (03:00 IST), after the nightly live suite, and by hand (`workflow_dispatch`, with
  an optional title pattern and other URLs). It is not a check of pull requests, because it
  tests what staging runs now, and a run takes about 15 minutes and costs model tokens. The CI
  job "Web" lints and typechecks the suite and runs the unit tests of its helpers, so a pull
  request cannot break the code of the suite.
- **Test data.** Each run makes its own data, so two runs do not share state:
  - Two Supabase users with confirmed emails, `sama-e2e+<run>-owner@example.com` and
    `sama-e2e+<run>-member@example.com`, with random passwords. The domain is reserved, so no
    mail goes anywhere.
  - One org `e2e-<run>-main` that the owner makes with self-serve sign-up (Section 17.2). It
    is on the free plan, so the plan caps (2 runs at the same time, 10 messages a minute)
    also apply to the suite. One worker runs the tests in order.
  - The global teardown deletes each test org with `DELETE /orgs/{org_id}` (all its rows go,
    Section 17.4), and then the users. It tries an org delete up to 5 times when the API
    answers `409` (runs of the org still stop) or a server error. When an org does not go, its
    owner stays, so the sweep can sign in as that user and try again. The sweep (`npm run sweep`) runs first in each nightly
    run. It removes each user with the `sama-e2e+` prefix that is older than 3 hours, and the
    `e2e-` orgs that the user owns. It gets a token of each user with an admin magic link
    (no email) and deletes each org with the slug from `GET /me`.
- **Credentials.** The repository secret `E2E_SUPABASE_SERVICE_ROLE_KEY` (the `service_role`
  key of the staging Supabase project) makes and removes the test users. Only the setup, the
  teardown, the sweep, and the sign-up test use it, and only in the Node process, never in the
  browser. The key goes only to the expected project (`E2E_SUPABASE_URL`, default the staging
  project): when `GET /config` of the API names another Supabase URL, the run stops before
  the key leaves. No request follows a redirect. Without the secret, the workflow skips the
  suite with a warning and does not fail. The tests use the tokens of the test users, as the web app
  does (`sessionStorage` key `sama.auth`). The world file of a run (`e2e/.state/world.json`,
  mode 0600, ignored by git) has the test passwords, and the teardown removes it.
- **Sign-up.** The staging project sends email with the built-in mailer, which sends only to
  the team. So the sign-up test sends the browser's `POST /auth/v1/signup` to the admin API
  (`generate_link` with `type: signup` makes the same unconfirmed user and sends no email),
  confirms the address with the admin API, and then signs in on the page. A retry of the test
  uses a new email address and org name.
- **Results.** The workflow keeps the HTML report, the JUnit file, and the screenshots,
  videos, and traces of failed tests for 7 days. A trace can have the short-lived token of a
  test user (it expires after 1 hour). After a clean teardown the user and the org are gone, so
  the token gives access to nothing. When the teardown keeps an owner because its org did not
  go, the token gives access to that test org until it expires or the sweep removes the org.
- **Billing.** The billing test opens Stripe Checkout in test mode (`cs_test_`) and does not
  pay.

Add the flows of a new item to the suite in the pull request of the item, or in the next one.
The staging web app deploys to Vercel by hand, so deploy it before a flow of a new page runs.
The channel flows of M7.8 use fake credentials: they check that a connect with credentials that
the provider refuses leaves no row, and the guards of the routes. A real WhatsApp number or
SendGrid account is not part of the suite. The flows of M7.6 use the official OpenAI Node SDK and the MCP
TypeScript SDK as the clients, so they test the compatibility that other apps rely on.

---

## 20. Milestones

[`roadmap.md`](roadmap.md) has the backlog with acceptance criteria. Summary:

| Milestone | Outcome |
|---|---|
| M0 | This design (doc only). |
| M1 Core engine | Repo foundations and CI. Runtime contract, Claude adapter, fake runtime, conformance suite. E2B sandbox and runner. Model proxy. Sessions in Postgres. SOUL and context files. Vault and redaction. Sama MCP tool server with output caps and spill. Outbound URL policy. Tool gate with approvals in the web chat and Telegram. HTTP chat API and a minimal web chat page. Telegram. Langfuse. Code editing in the sandbox (no pull requests yet). |
| M2 Learning loop | Memory, skills library and tools, session search, background review, context management (tool search, pins). |
| M3 Reach | Slack with shared-session rules. Approver rules per role. Connections: connection framework, Composio, remote MCP, and PostgreSQL. OpenAI Agents SDK adapter passes the conformance suite. GitHub pull request flow. Web app v1 for the team. |
| M4 Automation | Cron jobs and delivery, event triggers, workflows, `delegate_task`, task board, `@` references. Hosted stdio MCP, HTTP API connections, more databases, and SSH tunnels. |
| M5 Browser | Cloud browser tools, vision mode, browser vault, Chrome extension. |
| M6 Self-change and customers | `change_propose` and `sama_pr`, curator, checkpoints, customer sign-up, billing, quotas, org keys, regions, customer-ready web app. |

---

## 21. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | The two SDKs change fast and break adapters. | Pin versions. Conformance suite in CI. Nightly live suite. Upgrade on purpose, one SDK at a time. |
| R2 | Sandbox start time makes chat feel slow. | Pause and resume per session. Keep a small pool of warm sandboxes. Measure the start time from M1. A "lite" run mode without a sandbox for chat-only profiles is a later option. |
| R3 | Feature parity between runtimes drifts. | Sama tools carry the features. Capabilities flags show the gaps. The feature map tracks parity. |
| R4 | E2B egress controls are not strict enough for an allowlist. | Secrets stay out of the sandbox (D6), so leaks are limited. Log connections. Keep the `SandboxProvider` path to Fly Machines. |
| R5 | Prompt injection through web pages, email, or skills makes the agent act against the user. | Untrusted-content marking, narrow toolsets for untrusted triggers, approvals for writes, skill and context scans. |
| R6 | Self-improvement makes the agent worse over time (bad skills, noisy memory). | Caps, versioning, approvals for shared scopes, curator, and easy revert. |
| R7 | The Chrome extension can act on sensitive sites. | Per-site consent, user click for sensitive actions, block list, visible overlay, stop button. |
| R8 | Cost grows with background reviews, sub-agents, and jobs. | Budgets at every level, cheap models for reviews and titles, prompt caching. |
| R9 | Scope is very large. | Milestones deliver a usable assistant at M1 and add features in order of value. |

---

## 22. Resolved questions

The owner answered these questions on 2026-11-18. The answers are binding for M1. Section
3.1 lists the earlier owner answers.

| # | Question | Decision |
|---|---|---|
| Q1 | vorflux chose Fly Machines for sandboxes. Sama uses E2B. Should sama support both? | Sama uses E2B now, behind the `SandboxProvider` interface (D5). Sama adds a Fly Machines provider later only if a product needs it. vorflux decides when it adopts sama. |
| Q2 | Python package and service name? | Package `sama`. Services `sama-api`, `sama-gateway`, `sama-worker`, and `sama-scheduler`. |
| Q3 | Telegram: one shared bot or one bot per org? | One bot per org. An org admin connects it as an integration, and its token is in the org vault (Section 11.6). |
| Q4 | Which region? | Supabase in `ap-south-1` (Mumbai), the same as vorflux. Fly.io in the region nearest to it. |
| Q5 | Who may approve changes to workspace memory, workspace skills, and SOUL? | Workspace `admin` and org `owner`. Org-scope changes need the org `owner`. Since M3.2 the gate applies this rule to the proposals of chat runs and of the background review, and the chat cards name the role (Section 9.4.2). |
| Q6 | When do we build the web app? | A minimal web chat and approvals page in M1. Web app v1 for the team in M3. The customer-ready web app in M6. |
| Q7 | How do we start M1? | Foundations first. The first pull request has M1.1 (repo foundations) and M1.3 (runtime contract and fake runtime). Then one pull request per item or per small group. |
| Q8 | Which Supabase project does sama use? | A new Supabase project for sama in `ap-south-1`. Sama tables do not share a project with other apps. |
| Q9 | Which service keys does sama use? | The existing E2B, Anthropic, OpenAI, and Langfuse keys. Each org connects its own Telegram bot (Section 11.6). Keys for connectors are added later, when those items are ready. |
| Q10 | Where does sama deploy? | Fly.io. The owner adds `FLY_API_TOKEN` at M1.15. |
| Q11 | What is the name of the app? | Sama. The product, the Python package (`sama`), and the services (`sama-api`, `sama-gateway`, `sama-worker`, `sama-scheduler`) use this name. The earlier working name was claw. The GitHub repository keeps the name `Nikhil_claw`. |

No questions are open now.

---

## 23. References

- Hermes Agent: https://github.com/NousResearch/hermes-agent (MIT). Reviewed at commit
  `0e8e4216`. Docs: https://hermes-agent.nousresearch.com/docs/
- Hermes learning path: https://hermes-agent.nousresearch.com/docs/getting-started/learning-path
- Claude Agent SDK: https://docs.claude.com/en/api/agent-sdk/overview and
  https://github.com/anthropics/claude-agent-sdk-python
- OpenAI Agents SDK: https://openai.github.io/openai-agents-python/ and
  https://github.com/openai/openai-agents-python
- Agent Skills format: https://agentskills.io
- Model Context Protocol: https://modelcontextprotocol.io
- E2B: https://e2b.dev/docs
- Browserbase: https://docs.browserbase.com
- Composio: https://docs.composio.dev
- Langfuse: https://langfuse.com/docs
- vorflux architecture (hotel harness on top of sama): https://github.com/nikhilllmno/vorflux/blob/main/docs/architecture.md
