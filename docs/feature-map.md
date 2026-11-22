# Sama — Hermes Feature Map

| | |
|---|---|
| Status | Draft v0.4, 2026-11-21 |
| Source | Hermes Agent, commit `0e8e4216` (MIT, Copyright (c) 2025 Nous Research) |
| Design | [`architecture.md`](architecture.md) (sections are linked as §N) |
| Plan | [`roadmap.md`](roadmap.md) (milestones M1 to M6) |

This document lists the Hermes features, where they live in Hermes, and what sama does for
each one. Hermes paths are relative to the Hermes repository root.

**Decision key**

| Decision | Meaning |
|---|---|
| **Adapt** | Sama keeps the behaviour and redesigns it for a multi-user cloud service and two SDKs. |
| **SDK** | The Claude Agent SDK or the OpenAI Agents SDK already does it. Sama configures it. |
| **New** | Hermes does not have it. Sama adds it. |
| **Defer** | Useful, but after M6 or on request. |
| **Skip** | Not needed for a cloud, multi-user engine. |

**Runtime parity**: unless a row says otherwise, a feature works on both runtimes, because
sama implements it as a sama tool or in the control plane (architecture §5.3).

---

## 1. Agent core, prompt, and models

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| Agent loop | `run_agent.py`, `agent/` | The SDK runs the loop. Sama wraps it in the `AgentRuntime` contract (§5). | SDK | M1 |
| Provider abstraction (many providers) | `agent/transports/`, `providers/` | Only Anthropic and OpenAI models. Claude adapter (default) and OpenAI Agents adapter (M3.6 done). An admin switches the runtime of a profile. An org can bring its own Anthropic and OpenAI keys (§17.3, M6.7 done). | Adapt | M1, M3 (M3.6 done), M6 (M6.7 done) |
| Three-tier prompt (stable, context, volatile) | `agent/prompt_builder*.py` | Frozen prefix per session: SOUL, context files, memory snapshot, skill index. Volatile notes on the user message (§6.1, D9). | Adapt | M1 |
| `SOUL.md` personality | `~/.hermes/SOUL.md` | SOUL per agent profile, versioned rows, changes through `change_propose` (§15.2). Since M3.2, `change_propose` proposes a new SOUL, and a workspace admin or the org owner approves it (§9.4.2). Since M6.1, the card shows a diff, and the web app has the history and revert. | Adapt | M1 |
| Context files (`AGENTS.md`, `CLAUDE.md`, `.hermes.md`) | `agent/context_files*.py` | Loaded from the repo in the sandbox and from workspace settings, 20,000-char cap, injection scan (§6). | Adapt | M1 |
| Personalities (`/personality`) | `hermes_cli` | Agent profiles cover this. | Adapt | M3 |
| Model switch per session (`/model`) | `hermes_cli/commands.py` | `/model` starts a child session (lineage) with the new model and a new prefix. The old session keeps its frozen prefix. | Adapt | M1 |
| Fallback model and credential pools | `agent/credential_pool*.py` | The model proxy retries on a second key or model of the same provider. | Adapt | M3 |
| Prompt caching | `agent/prompt_caching*.py` | SDK caching plus the frozen prefix. Proxy records cache tokens. | SDK | M1 |
| Reasoning effort settings | config `agent.reasoning_effort` | Profile setting `reasoning_effort` (`low`, `medium`, `high`, or the model default), set in the Harness section of the settings console. The adapters give it to the SDK (`effort` for Claude, `reasoning.effort` for OpenAI) (§17.7). | SDK | M7 (M7.10 done) |
| Mixture of Agents (`hermes moa`) | `agent/moa_loop.py` | Sama tool `moa` in the toolset `moa`: three models of Anthropic, OpenAI, and Gemini answer at the same time, and an aggregator model writes one answer. A cost cap per call, metered like the model proxy. Children do not get it (§10.2.2). | Adapt | M7 (M7.5 done) |
| Codex app-server runtime | `agent/transports/codex_app_server.py` | Since M7.2: the runtimes `codex` and `claude_code` run the Codex CLI (`codex exec --json`) and the Claude Code CLI (`claude -p`, stream-json) headless in the session sandbox, through the model proxy and the git proxy. They implement the same `AgentRuntime` contract. A profile chooses them, or a session switches with `/codex` and `/claude` until `/sama` (§15.4). Sama uses `codex exec`, not the app server. | Adapt | M7 (M7.2 done) |
| Hand work to Claude Code or Codex (skills) | `skills/autonomous-ai-agents/` | The tool `coding_agent_run(agent, prompt, repo, open_pr, session)`, the commands `/claude <task>` and `/codex <task>`, and the builtin skills `claude-code` and `openai-codex` (our own text). The result has the summary, the diff, and an optional pull request. Approvals (`ask` by default), budgets, and time limits apply (§15.4). | Adapt | M7 (M7.2 done) |

## 2. Built-in tools

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| `terminal`, `process_manage` | `tools/terminal_tool.py`, `tools/process_registry.py` | Native shell tool of the SDK, in the E2B sandbox. Background processes through the sandbox. | SDK | M1 |
| `read_file`, `write_file`, `patch`, `search_files` | `tools/file_tools.py` | Native file tools (Claude `Read`, `Write`, `Edit`, `Grep`; OpenAI sandbox shell in M3.6, the filesystem capability and `apply_patch` later). | SDK | M1 |
| Terminal backends (local, Docker, SSH, Modal, Daytona, ...) | `tools/environments/` | One `SandboxProvider`: E2B in the cloud, Docker for development and CI (D5). | Adapt | M1 |
| `web_search`, `web_extract` | `tools/web_tools.py` | Sama tools, search provider key on the control plane (§10.2). | Adapt | M1 |
| `x_search` | `tools/x_search_tool.py` | Sama tool in the `web` toolset: the recent search of the X API v2 with the X token of the org or of sama, at most 20 posts, untrusted content (§10.2.2). | Adapt | M7 (M7.5 done) |
| `vision_analyze` | `tools/vision_tools.py` | Sama `media` tool. Images from Telegram and Slack are stored, and a vision model answers questions about them (§11.7). | Adapt | M2 (done) |
| `video_analyze` | `tools/vision_tools.py` | Sama `media` tool with Gemini: a YouTube link, an `https` video file, or a file of the sandbox (at most 14 MB). Frames and audio in one call, metered (§10.2.2). | Adapt | M7 (M7.5 done) |
| `video_generate` | `tools/video_generation_tool.py` | Not yet: a video takes minutes to make, costs much more than an image, and needs a new kind of generated file and channel delivery (§10.2.2, A4). | Defer | — |
| `image_generate` | `tools/image_generation_tool.py` | Sama `media` tool with the OpenAI image model. Telegram sends a photo, Slack uploads a file, and the API lists the file (§11.8). | Adapt | M4 (M4.9 done) |
| `text_to_speech` | `tools/tts_tool.py` | Sama `media` tool. Telegram sends a voice note, and Slack uploads the audio file (§11.8). | Adapt | M4 (M4.9 done) |
| Speech to text for voice memos | `tools/transcription_tools.py` | Gateway transcribes inbound voice with the OpenAI transcription model before the run (§11). | Adapt | M1 |
| `todo_list` | `tools/todo_tool.py` | Sama `todo` tool for both runtimes. On Claude, native `TodoWrite` calls map to the same list. The web app shows it (§10.2). | Adapt | M1 |
| `clarify` | `tools/clarify_tool.py` | Sama tool, rendered as buttons per channel. Denied in unattended runs. | Adapt | M1 |
| `execute_code` (RPC to tools) | `tools/code_execution_tool.py` | Sama `code_exec`: Python in the sandbox with a stub that calls sama tools over MCP. Only stdout returns to the model (§10.2). | Adapt | M4 (M4.8 done) |
| User-defined tools (no code) | none (Hermes adds tools in Python through `tools/registry.py` and plugins) | Custom tools from the web app or the API: an HTTP request tool (URL and body templates, auth from the vault bound to one origin, response mapping) or a Python or Node script in the session sandbox, with a JSON schema, a description, and a policy. Versions with revert, a test button, and scopes: any member for themselves, only an admin for workspace and org tools. Both runtimes reach them through `tool_search` as `custom_<name>` (§10.8). | New | M7 (M7.3 done) |
| `computer_use` (desktop control) | `tools/computer_use_tool.py` | Browser vision mode (§14) covers web apps. Full desktop control is desktop-only and is not planned. | Defer | — |
| `manage_connections` | `tools/connectors/tool.py` | `connections_list`, `connection_request` (a link to the Connections page), `connection_test`. Add, change, and remove happen in the web app. `/connect` gives a link, so secrets never enter chat (§10.3). | Adapt | M3 (M3.4 done) |
| Discord and Feishu tools | `tools/discord_tool.py`, `tools/feishu_*.py` | An org adds a Discord or Feishu (Lark) MCP server as a connection (`mcp_remote` or `mcp_hosted`, §10.3). Its tools come behind `tool_search`. No sama code. A Discord channel is M7.8. | Adapt | M3, M4 (connections done) |
| Desktop-only tools (`desktop_ui`, `project`, `catalog`) | `tools/project_tools.py` and others | No desktop app. | Skip | — |
| No agent-callable `send_message` | `toolsets.py` | Same rule: replies go to the trigger's channel (§10.1). | Adapt | M1 |
| Toolset bundles (`coding`, `safe`, platform bundles) | `toolsets.py` | Presets `assistant`, `coding`, `safe`, `cron` (§10.2). | Adapt | M1 |
| Tool `check_fn` gating | `tools/registry.py` | Tools hidden when their service or credential is missing (§10.1). | Adapt | M1 |

## 3. Skills

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| `SKILL.md` format (Agent Skills standard) | `skills/`, `agent/skill_*.py` | Same format. Stored as versioned rows with the files in the version row (Supabase Storage later), synced into the sandbox (§9). | Adapt | M2 (done) |
| Progressive disclosure (index, view, files) | `tools/skills_tool.py` | Index in the prefix (60-char descriptions), `skill_view` loads the body and files. Claude native skills are an option. | Adapt | M2 (done) |
| `skills_list`, `skill_view`, `skill_manage` | `tools/skills_tool.py`, `tools/skill_manager_tool.py` | Same tool names. `skill_manage`: create, patch, edit, write_file, remove_file, archive. | Adapt | M2 (done) |
| Skill scopes (builtin, user dir, project) | `agent/skill_*.py` | builtin, org, workspace, user. User scope applies at once. Shared scopes need approval (§9). | Adapt | M2 (done) |
| Skills as slash commands (`/<skill>`) | `gateway/slash_commands.py` | `/<skill-name>` in every channel (§11.1). | Adapt | M2 (done) |
| Bundled skills (44) and optional skills (141) | `skills/`, `optional-skills/` | 70 builtin skills that sama wrote (68 from M7.7, and `claude-code` and `openai-codex` from M7.2). They cover each category of the Hermes bundled skills except `apple` (Mac only): `autonomous-ai-agents` (`agents`), `creative`, `devops`, `email`, `media`, `note-taking` (`notes`), `productivity`, `research`, `social-media` (`social`), `software-development` (`software`), and `web`. Each skill has a category tag, `requires_toolsets`, and its own test (§9.2). No Hermes skill text is copied. | Adapt | M2 (done), M7 (M7.7 done) |
| Skills Hub install with quarantine and scan | `tools/skills_hub*.py`, `skills_guard.py` | `skill_import` from a public GitHub URL with an import scan, the skill lint and injection scan, and an approval (§9.6). `agentskills.io` is later. | Adapt | M4 (M4.10 done) |
| Conditional activation (`requires_toolsets`, platforms) | frontmatter | Same frontmatter keys. Skills whose tools are missing are hidden. | Adapt | M2 (done) |
| Per-skill config | `skills.config` | The `config` block of `SKILL.md` declares the settings. A workspace admin fills them in the skill editor of the web app, and `skill_view` shows them as data. No secrets (§9.8). | Adapt | M7 (M7.5 done) |
| Skill bundles | `agent/skill_bundles.py` | Agent profiles list skills. | Adapt | M2 (done) |

## 4. Memory

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| `MEMORY.md` (2,200 chars) and `USER.md` (1,375 chars) | `tools/memory_tool.py` | Targets `agent` (2,200), `user` (1,375), and `workspace` (2,200) per profile and user (§8). | Adapt | M2 (M2.1 done) |
| `memory` tool: add, patch, remove | `tools/memory_tool.py` | Same actions. Over-limit writes fail and return the current entries. | Adapt | M2 (M2.1 done) |
| Frozen memory snapshot in the prompt | `agent/memory_manager.py` | Snapshot in the frozen prefix. New writes apply from the next session. | Adapt | M2 (M2.1 done) |
| Memory nudges | `agent/background_review.py` | Background review every 10 user turns (§9.4.1). | Adapt | M2 (M2.4 done) |
| External memory providers (Mem0, Honcho, ...) | `plugins/memory/` | Optional Mem0 provider behind a `MemoryProvider` interface, per profile. The built-in memory stays the default. Private turns sync to Mem0, `memory_recall` searches them, and a delete purges them (§8.2). Honcho and others can use the same interface. | Adapt | M7 (M7.5 done) |
| Memory history | none (files only) | `memory_revisions` table with author, run, and reason. Revert in the API (M2.1) and in the memory editor of the web app (M3.12). | New | M2, M3 (M2.1 and M3.12 done) |

## 5. Sessions

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| SQLite `state.db` with FTS5 | `hermes_state.py`, `persistence/` | Postgres tables `sessions`, `messages`, `tool_calls`, `runs`, `run_events` with RLS (§7). | Adapt | M1 |
| Session per chat, survives restarts | `gateway/session.py` | `session_bindings` map a channel thread to a session. | Adapt | M1 |
| `/new`, `/reset`, `/resume`, `/title`, `/branch` | `hermes_cli/commands.py` | Same commands in all channels. Branch creates a child session (lineage). | Adapt | M1 |
| `session_search` | `tools/session_search_tool.py` | Postgres full-text plus pgvector. No LLM call in the tool (§7.1). | Adapt | M2 (M2.5 done) |
| Session lineage after compression | `agent/context_compressor*.py` | Parent and child sessions with the archived transcript (§6.2). | Adapt | M2 |
| Queue, interrupt, steer while busy | `gateway/session_*.py` | New message while a run is busy: since M7.1 it joins the running run by default (busy mode `steer`, as in Claude Code), and the tool gate gives it to the run at its next tool call. A note that the run does not read becomes a new message. The busy modes `parallel` and `queue` are options per profile. `/stop` cancels all runs, `/stop NUMBER` one run, and `/steer` adds a note (§4.3). Telegram, Slack, and the web app. | Adapt | M3 (M3.9 done), M7 (M7.1 done) |
| Parallel requests and background runs | none (Claude Code background agents) | `/bg TASK`, the "Run in parallel" button, or the busy mode `parallel` start a second run in the same session and sandbox, with the transcript up to that point. Replies come in order of completion. The next main run gets the side answers in a catch-up block (§4.3). | New | M7 (M7.1 done) |
| Cross-platform handoff (`/handoff`) | `gateway/` | `/handoff telegram`, `/handoff slack`, or `/handoff web` moves a private session to the direct chat of the user in that channel. The open runs answer there (§11.1). | Adapt | M3 (M3.9 done) |
| Retention and housekeeping | `session_retention.py` | Retention per org inside the plan limits. An hourly sweep deletes old sessions with their sandboxes and traces (§17.4). | Adapt | M6 (M6.8 done) |
| Export and import | `/export`, `/import` | Session export to Markdown and JSON from the web app (M3.12, `GET /sessions/{id}/export`). Org and user export as a ZIP of JSON lines, and delete per user and per org (M6.8, §17.4). No import. | Adapt | M3 (M3.12 done), M6 (M6.8 done) |

## 6. Context management

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| Compression at 50% of the window, protect last 20 | `agent/context_compressor.py` | Native compaction in both SDKs. Sama archives the transcript before, in a child session, and records each `Compacted` event (§6.3). | SDK | M2 (M2.6 done) |
| Tool output cap (50,000 bytes) and spill to file | `tools/tool_result_storage*.py` | Sama tools cap at 50 KB and spill to a sandbox file with a preview and path. | Adapt | M1 |
| Tool search (`tool_search`, `tool_describe`, `tool_call`) | `tools/tool_search*.py` | Same three tools for deferred tools and integrations. The gate judges `tool_call` as a call of the tool that it names (§6.3, §16.1). The Claude native `ToolSearch` stays a loader that the gate allows. | Adapt | M2 (M2.6 done) |
| Token accounting | `agent/usage*.py` | Model proxy records tokens and cost per call (§18). | Adapt | M1 |
| `@file`, `@url` references | `agent/context_references.py` | `@file`, `@url`, `@session` in a message. The worker attaches them to the run input as untrusted data (§6.4). | Adapt | M4 (M4.9 done) |
| `/compress` | slash command | `/compact [focus]` asks the runtime to compact now, in the API, Telegram, and Slack direct messages (§6.3). | Adapt | M2 (M2.6 done) |
| Context pins | none | A user pins a text or a message. Sama injects the pins and injects them again after a compaction (§6.3). Files are not pinnable yet. | New | M2 (M2.6 done) |

## 7. Self-improvement and self-modification

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| Background review fork (memory and skills) | `agent/background_review.py` | A separate review run after the main run, with a cheap model, recursion off, and only `memory` and `skills` tools (§9.4.1). | Adapt | M2 (M2.4 done) |
| Skill creation nudge (10 tool iterations) | `agent/agent_init.py` | Same default (§9.4.1). | Adapt | M2 (M2.4 done) |
| `/learn` (skill from sources) | `learn_prompt.py` | `/learn <urls or topic>` asks the agent to read the sources and write one draft skill with `skill_manage` (§9.6). | Adapt | M4 (M4.10 done) |
| Curator (7 days, active, stale, archived) | `agent/curator*.py` | Weekly curator job per workspace (`mode: curator`, `src/sama/curator/`). Usage counts, active to stale (30 days) to archived (90 days), pins and restore, a report to the owner. Memory is report-only. Never deletes (§9.7). | Adapt | M6 (M6.3 done) |
| Checkpoints and `/rollback`, `/undo` | `agent/checkpoints/` | A shadow git repository in the sandbox, with a snapshot before each tool call that can change files, in both runtimes. `/undo` restores the files of the last run that changed them, not the conversation. User commands in Telegram, Slack, and web, not model tools (§15.3). | Adapt | M6 (M6.4 done) |
| Coding mode like Claude Code and Codex (attach a repo, plan mode, diffs, test runs, undo, pull request) | partial (`/plan` skill, checkpoints) | A session attaches a GitHub repo that clones before the next run (through the git proxy for an allowlisted repo). Plan mode: the gate blocks the tools that change files, the agent calls `plan_propose`, and a person approves, edits, or rejects the plan (`/plan` in the web, Telegram, and Slack). The Code page of the web app shows the diffs from the checkpoints, the commands and test runs, the checkpoints with undo, and the pull requests, and asks the agent to open one. The same parts work for the `claude_code` and `codex` harnesses of M7.2 (§15.5). | New | M7 (M7.9 done) |
| Chat like Claude Code and Codex (tool rows, working line, Esc to stop, approval keys) | CLI and TUI (`cli.py`) | The web chat shows each tool call as one row in the order of the reply, a working line with the phase and the time, Esc to stop, a to-do list, approval cards with keys, the plan decision in the chat, and the tool trail of earlier runs (`GET /sessions/{id}/activity`) (§17.8). | New | M7 (M7.12 part A done) |
| Config changes by the agent | `hermes config set` through the terminal | `change_propose` creates an approval card with a diff for SOUL, profile, toolsets, and policy (§9.4.2, §15.2). The change applies from the next session. The web app has the history and revert. Done in M6.1 (the SOUL target in M3.2). | Adapt | M6 |
| Changes to its own code | `self_repo_guard.py`, `hermes update` | `sama_pr`: the agent clones the sama repo, changes it, runs the tests, and opens a pull request that a human merges. Code owners of changed paths get a review request. CI deploys (§15.2). | New | M6 (M6.2 done) |
| Version history and revert for all agent changes | partial (curator backups) | Every self-improvement write is a versioned row with provenance (D14). | New | M2 |

## 8. Scheduler, cron, and automation

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| Cron jobs (`jobs.json`, 60 s tick) | `cron/`, `tools/cronjob_tools.py` | Postgres `jobs` table, 60 s tick in the control plane process with an advisory-lock leader lease (§12.1). Done in M4.1. | Adapt | M4 |
| `cronjob_manage` actions | `tools/cronjob_tools.py` | Same name, plus `preview`. The same rows are in `/cron` (Telegram and Slack direct chats), the API, and the web page `/jobs`. Done in M4.2. | Adapt | M4 |
| Natural language schedules | `cron/` | Sama reads simple English or a cron expression with a time zone, validates it, and shows the next 3 fire times. Done in M4.2. | Adapt | M4 |
| Delivery to any channel with receipts | `cron/delivery*.py` | Targets `origin`, Slack, Telegram, email, and `none`, plus `failure_deliver`, with receipts. An unknown delivery result is not retried. Three failures pause the job with one alert (§12.1). Done in M4.3. | Adapt | M4 |
| No cron recursion | `toolsets.py` (`hermes-cron`) | A job run cannot create jobs: the gate removes the `cron` toolset, and the job service refuses it. Done in M4.2. | Adapt | M4 |
| Per-job model and script-only jobs | `cron/` | `model` pin (`runs.model`) and `mode: script` in the session sandbox. Done in M4.2. | Adapt | M4 |
| Unattended approvals deny | `tools/approval*.py` | `ask` becomes `deny` unless a human approved a pre-approval for that tool in that job version (D13, §12.1). | Adapt | M1 |
| Webhooks (restricted toolset) | `gateway/platforms/webhook*.py` | Signed webhook triggers (Standard Webhooks) with the `safe` toolset (§12.2). | Adapt | M4 (M4.4 done) |
| Heartbeat, goals, loops | `gateway/run_goals.py` | A job with `attach_to_session` runs inside one session (heartbeat). | Adapt | M4 |
| Automation blueprints | `cron/blueprint_catalog.py` | Workflow templates (§12.3). | Adapt | M4 |
| Workflows (multi-step, approvals, durable) | none | Versioned YAML workflows with tool, agent, approval, branch, parallel, wait, and workflow steps. Durable step rows in Postgres, operation ids, idempotency keys, receipts, reconcile, and owner approval of versions that write. `workflow_manage`, `workflow_run`, and the `session-to-workflow` skill (§12.3). | New | M4 (M4.5 done) |
| Composio triggers | none | Composio triggers start runs or workflows with the `safe` toolset (§12.2). | New | M4 (M4.4 and M4.5 done) |

## 9. Channels

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| Telegram adapter | `plugins/platforms/telegram/` | Bot API webhook adapter, inline keyboards, voice notes (§11.2). | Adapt | M1 |
| Slack adapter | `plugins/platforms/slack/` | Events API and Web API with `httpx` (no Bolt), one Slack app per org as an integration, Block Kit approvals, thread = session (§11.2, §11.6). Threads are shared sessions with the audience rule (M3.3). | Adapt | M3 (M3.1, M3.3 done) |
| WhatsApp adapter | `plugins/platforms/whatsapp/` | One adapter over three providers that the org picks: Meta WhatsApp Cloud API, Twilio, or Gupshup. Each webhook is signed (HMAC of Meta, signature of Twilio, secret header of the Gupshup subscription) and has a secret URL token. Direct chats are private sessions. Approval buttons or reply codes, images, voice notes, and signed media links (§11.9). | Adapt | M7 (M7.8 done) |
| Email adapter | `plugins/platforms/email/` | An inbound address per org with SendGrid Inbound Parse (ECDSA-signed) and the SendGrid mail API. DKIM or SPF must pass for the sender. A thread is a private session. Approval links with a confirm page, image attachments (§11.9). | Adapt | M7 (M7.8 done) |
| Other platforms (Discord, Signal, Teams, ...) | `plugins/platforms/` | Same adapter shape. Discord when a bot token exists. | Defer | — |
| API server (OpenAI-compatible) | `gateway/platforms/api_server*.py` | Sama HTTP API with streaming events (§4.1). Since M7.6, `POST /v1/chat/completions` (also as a stream) and `GET /v1/models` with an org API key: the model is an agent profile, and `X-Sama-Session-Id` continues a session (§10.7). | Adapt | M1, M7 (M7.6 done) |
| Authorization ladder, default deny | `gateway/authz_mixin.py` | Paired identities only. Group toolset and approver rule for shared sessions. Private and shared session audiences (§11.3). | Adapt | M1 |
| DM pairing codes with rate limits and lockout | `gateway/pairing.py` | Codes from the web app, 10-minute expiry, lockout (§11.3). | Adapt | M1 |
| Approvals over chat (buttons, `/approve`, `/deny`) | `gateway/approval_actor.py` | Approval cards per channel (§11.1): web and Telegram in M1, Slack and approver rules per role in M3. Since M3.2: "Always allow for me" on Slack and Telegram cards, chat proposals, and the timeout counts as deny (§9.4.2). | Adapt | M1, M3 (M3.2 done) |
| Streaming edits to messages | `gateway/stream_consumer*.py` | Edit one message as text arrives, with rate limits per channel. | Adapt | M1 |
| Slash command registry | `hermes_cli/commands.py` | One command registry for all channels (§11.1). | Adapt | M1 |
| Web dashboard | `web/` | Next.js web app: chat, approvals, memory, skills, session search, run traces, connections, and settings (M3.8, M3.12). Since M6.9: a setup guide, an admin area (usage, audit log, agent changes, curator reports, org keys, data controls), and help pages (§11.4, §17.6). Since M7.10: one settings console with Setup, Slack, Roles, Harness, Tools, Sub-agents, Skills, MCP servers, Webhooks, Triggers, Schedules, Workflows, and Sessions (§17.7). | Adapt | M1 (minimal), M3 (M3.8 and M3.12 done), M6 (M6.9 done), M7 (M7.10 done) |
| Chrome extension side panel | none | MV3 extension with chat bound to the current tab, Supabase sign-in, a WebSocket to the API, and "Ask about this page" as untrusted content (§14.2.1). | New | M5 (M5.4 done) |

## 10. Sub-agents and multi-agent work

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| `delegate_task` (single and batch) | `tools/delegate_tool.py` | Child session and run in its own sandbox. Summary returns to the parent (§13.2). | Adapt | M4 (done, M4.6) |
| Limits: depth 1, 10 children, blocked tools | `tools/delegate_tool_config.py` | Same defaults. Child runs also have a cost budget, and lose `tasks`, `memory`, `clarify`, `cron`, `connections`, and `integrations` (§13.2). | Adapt | M4 (done, M4.6) |
| Cheaper model for children | `delegation.model` | `agent_profiles.delegation_model`: the model of the children of `delegate_task`, set in the agent settings of the web app. Without it, the model of the profile (§13.2). | Adapt | M7 (M7.5 done) |
| Native in-run sub-agents | none (Hermes has its own loop) | Claude `agents` (M2.7 done). OpenAI agents as tools (M7.4 done). Handoffs are not used (§13). | SDK | M2 and M7 (done) |
| Sub-agent library | none (Hermes delegates with ad hoc prompts) | Personal, workspace, and org library agents with a prompt, toolsets, a model, and limits. A profile uses them, `delegate_task(agent=...)` starts one, and the web app edits them with versions and a test run (§13.3). | Sama | M7 (done, M7.4) |
| Background delegation and steering | `tools/async_delegation*.py` | `delegate_status`, cancel, and steer for children. Steer is a message after the current child run (§13.2). | Adapt | M4 (done, M4.6) |
| Kanban board and dispatcher | `hermes kanban` | Task board with a dispatcher, `task_*` tools, and the web route `/tasks` (§12.4). | Adapt | M4 (done, M4.7) |
| Worker bots in Slack channels | `gateway/bot_*` | An agent profile bound to a Slack channel with a daily budget (§11.6). Done in M3.10. | Adapt | M3 |

## 11. Browser

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| Browser over CDP (`browser_exec`, `browser_cdp`) | `tools/browser_*.py` | Typed tools on snapshots with refs: `browser_open`, `browser_snapshot`, `browser_act`, `browser_screenshot`, `browser_extract`, `browser_tabs` (§14.1). No raw CDP tool. Done in M5.1. | Adapt | M5 (done, M5.1) |
| Cloud browsers (Browserbase, Browser Use cloud) | `agent/browser_session_cloud*.py` | Browserbase with a persistent context per user (§14.1, table `browser_contexts`). Browser Use cloud is not used. Done in M5.1. | Adapt | M5 (done, M5.1) |
| Browser sub-agent (`browser-use` style) | — | `browser_task(goal)`: a sama sub-agent loop with the typed browser tools on the cloud browser of the run. Model calls go through the model proxy. `browser-use` is not used, because version 1.0 runs model-written Python and version 0.x conflicts with the sama dependencies (§14.1). Done in M5.6. | New | M5 (done, M5.6) |
| Local Chromium and real profile | `agent/browser_launcher*.py` | Not in the cloud. The Chrome extension replaces the real-profile mode. | Skip | — |
| Attach to the user's Chrome over CDP | `/browser connect` | The `my_chrome` target: the extension uses `chrome.debugger` in the tabs of the run, on the M5.4 WebSocket (§14.2.2). | New | M5 (done, M5.5) |
| `browser_dialog` | `tools/browser_*.py` | `browser_act(action="dialog", accept, prompt_text)` answers a dialog. An action that opens a dialog returns at once with a note. Done in M5.1. | Adapt | M5 (done, M5.1) |
| `browser_vault` (logins never in the transcript) | `agent/vault_*.py` | Saved logins in the sama vault, org or personal. `browser_vault` fills them on their own site inside the control plane, and the results hide the values (§14.1). Done in M5.3. | Adapt | M5 (done, M5.3) |
| URL policy and SSRF guard | `tools/browser_url_policy.py` | The outbound address rules apply to every navigation of the cloud browser since M5.1 (§16.4). The allow list and the block list of each org apply since M5.3 (§14.1). | Adapt | M5 (done, M5.1 and M5.3) |
| Vision mode (screenshot and coordinates) | `computer_use` | `browser_screenshot` (one pixel is one CSS pixel of the viewport) plus `browser_act` at a point (`x`, `y`) for `click`, `type`, `hover`, `drag`, and `scroll` (§14.1, "Vision mode (M5.2)"). Done in M5.2 on the `cloud` target. `my_chrome` later (§14.2.2). | Adapt | M5 (done, M5.2) |
| Visible cursor, click, type in the user's Chrome | none | Overlay with cursor and Stop, CDP input events, consent for each site, a click of the user for sensitive actions, pause on takeover, and a blocked site list (§14.2.2). | New | M5 (done, M5.5) |

## 12. MCP, integrations, and plugins

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| MCP client, HTTP and OAuth | `tools/mcp_tool*.py`, `tools/connectors/mcp_oauth.py` | `mcp_remote` connections: streamable HTTP, OAuth 2.1 with dynamic client registration and PKCE, bearer, or header. Proxied by the sama MCP server, tokens in the vault (§10.3). Also `none` for a public server. | Adapt | M3 (M3.5 done) |
| MCP client, stdio servers | `tools/mcp_tool_transport.py`, `tools/connectors/mcp_launch.py` | `mcp_hosted` connections run in a connector host (a separate E2B sandbox per connection), not in the agent sandbox (§10.5). A pinned npm or PyPI package, or a custom command that only the org `owner` can add. Egress allowlist, start on the first call, pause when idle, restart above the memory limit (§10.3.2). | Adapt | M4 (M4.11 done) |
| MCP tool naming `mcp_<server>_<tool>` | `tools/mcp_tool.py` | Same naming as `mcp_<connection>_<tool>`, plus `db_` and `api_` prefixes, behind `tool_search` (§10.3). | Adapt | M3 (M3.5 done for `mcp_`), M4 (M4.12 done for `api_`) |
| Per-server tool filters, schema cache, health checks | `tools/mcp_tool_registration.py`, `tools/mcp_schema_cache.py`, `tools/mcp_tool_health.py` | Include and exclude filters, `tool_cache`, and health checks per connection (§10.3). | Adapt | M3 (M3.5 done) |
| `/reload-mcp` | `tui_gateway/methods_tools.py` | Not needed. New connector tools are reachable through `tool_search`, so the prompt prefix does not change (§10.3). | Skip | — |
| `${ENV_VAR}` in MCP config | `tools/mcp_tool_config.py` | Secrets come from the vault reference of the connection, not from environment variables (§10.3). A hosted stdio server gets each value as an environment variable of its process in the connector host only (§10.3.2). | Adapt | M3 (M3.4 done for `http_api`, M3.5 for `mcp_remote`), M4 (M4.11 for `mcp_hosted`) |
| MCP catalog (`optional-mcps/`, 66 manifests) | `optional-mcps/` | Connector catalog of YAML manifests in `connectors/catalog/`, added by reviewed pull requests (§10.3). We write our own manifests. M3.4 has six: five Composio connectors and Notion (`http_api`). M3.5 adds five remote MCP servers: DeepWiki, Neon, Stripe, Sentry, and Supabase. | Adapt | M3 (M3.4 and M3.5 done) |
| Connection state contract | `tools/connectors/contract.py` | Same idea: only the backend sets `connected`; a clock expires `initiated` (§10.3). | Adapt | M3 (M3.4 done) |
| Managed connectors (Nous tool gateway) | `tools/connectors/` | Composio with entities per user and per workspace (§10.3). | Adapt | M3 (M3.4 done) |
| Database access | Through MCP servers (for example Supabase, Neon) | Native database connections with a sama database gateway: read-only by default, one statement per call, timeouts, row limits, hidden columns, approval for writes (§10.4). MongoDB is read only (`find` and `aggregate`). Vendor MCP servers also work. | New | M3 (PostgreSQL, Supabase, and Neon: M3.11 done), M4 (MySQL, MariaDB, SQL Server, BigQuery, Snowflake, ClickHouse, and MongoDB: M4.13 done) |
| HTTP API connectors | none | `http_api` connections from a list of endpoints in the manifest (M3.4). An admin can add an API from an OpenAPI document or a list of endpoints, with one tool `api_<connection>_<tool>` per operation and API key, bearer, basic, or OAuth 2 client credentials auth (§10.3.3). | New | M3.4 (endpoints), M4.12 (OpenAPI and lists, done) |
| Private networks | none | Static egress IPs and SSH tunnels per connection; a relay later (§10.6). The tunnel of a connection listens on `127.0.0.1`, and no other connection or outbound request can use it. | New | M4 (SSH tunnels: M4.13 done; static egress: an operator allocates it; relay: later) |
| Hermes as an MCP server (`hermes mcp serve`) | moved out of core (`hermes_cli/left_core_migration.py`) | Sama MCP server for other agents at `/mcp` (streamable HTTP) with an org API key: send a message or start a run, read and search sessions, list skills, and run workflows. The runs use the same gate, approvals, and budgets (§10.7). | Adapt | M7 (M7.6 done) |
| Plugin system | `plugins/`, `hermes_cli/plugins*` | Extension points for products (tools, skills, toolsets, channels, policies) (D16). | Adapt | M1 |
| ACP adapter for editors | `acp_adapter/` | Not planned. | Defer | — |

## 13. Safety

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| Hardline deny floor | `tools/approval_detection.py` | Same idea, own pattern list (§16.1). | Adapt | M1 |
| Dangerous command approval, `smart` mode | `tools/approval*.py` | Policy `allow`, `ask`, `deny` plus an optional small-model classifier (§16.1). | Adapt | M1 |
| Protected paths and write guards | `tools/file_tools_write_guards.py` | Sandbox isolation. The sama git proxy refuses a push that changes a protected path of a repo (for example `.github/workflows`) until a person approves `repo_push_approval` (§15.1). | Adapt | M3 (M3.7 done) |
| Secret redaction | `agent/redact*.py` | Redaction of tool output, logs, and traces (§16.3). | Adapt | M1 |
| Secrets in `.env` and vault backends | `agent/secret_sources/` | Sama vault in Postgres. No secrets in the sandbox (D6). | Adapt | M1 |
| SSRF and web access policy | `tools/website_policy.py` | Sandbox egress allowlist (§16.2) and control-plane outbound URL policy against SSRF and DNS rebinding (§16.4). | Adapt | M1 |
| Prompt-injection scans for context and skills | `agent/prompt_injection*.py` | Scans for context files (M1), skills and imported content (M2) (§16.5). | Adapt | M1, M2 |
| Container sandbox backends | `tools/environments/` | E2B for every run (D4, D5). | Adapt | M1 |
| Multi-user roles and tenant isolation | none | Orgs, workspaces, roles, RLS (§17). | New | M1 |
| Budgets per run, user, org | `agent/budget_config.py` (bot mode) | Proxy budgets at every level (§18). | Adapt | M1 |

## 14. Surfaces and operations

| Hermes feature | Hermes location | Sama design | Decision | Milestone |
|---|---|---|---|---|
| CLI and TUI | `cli.py`, `ui-tui/` | Cloud only. A small admin CLI (`sama`) for operators may come later. | Skip | — |
| Desktop app | `apps/desktop/` | No desktop app. The web app and the Chrome extension cover it. | Skip | — |
| Profiles (separate homes) | `~/.hermes/profiles/` | Agent profiles and workspaces in Postgres (§17). | Adapt | M1 |
| Setup wizard, doctor, update | `hermes setup`, `hermes doctor` | Web onboarding for customers (M6): sign-up, a first org, and invites (§17.2, M6.5), then a setup guide that connects a chat app, picks or creates an agent, and sends a first message (§17.6, M6.9). Health checks and alerts (§18). | Adapt | M6 (M6.5 and M6.9 done) |
| Gateway service management | `hermes gateway install` | Fly.io process groups, deploys from CI (§19). | Adapt | M1 |
| Batch runner and trajectories | `batch_runner.py`, `trajectory_compressor.py` | Not planned. Langfuse datasets cover evals. | Defer | — |
| Evals | `evals/` | Conformance suite in CI and a nightly live suite (§5.5). A Playwright end-to-end suite runs the main flows in the web app against staging every night (§19.6, M7.11 done). | Adapt | M1, M7 |
| Langfuse and telemetry | `agent/monitoring/` | Langfuse for both runtimes, proxy cost (D15). | Adapt | M1 |
| Skins, pets, achievements, language packs | various | Not planned. | Skip | — |

## 15. Features sama adds

| Feature | Sama design | Milestone |
|---|---|---|
| Two runtimes behind one contract with a conformance suite | §5 | M1, M3 (M3.6 done) |
| Multi-user orgs, workspaces, roles, RLS | §17 | M1 |
| Model proxy with budgets and no keys in the sandbox | D6, §18 | M1 |
| Workflows with approval steps | §12.3 | M4 (M4.5 done) |
| Chrome extension: side panel chat and "Ask about this page" (M5.4 done); click, type, cursor in the user's browser (M5.5 done) | §14.2 | M5 |
| Versioned and revertible self-improvement | D14 | M2 |
| GitHub App and git proxy: no token in the sandbox, `sama/*` branches only, no force-push or delete, pull requests by tool | §15.1 | M3 (M3.7 done) |
| Self-modification through pull requests | §15.2 | M6 |
| Customer sign-up, billing, quotas | §17 (sign-up, invites, and roles in §17.2; billing and quotas in §17.5) | M6 (M6.5 and M6.6 done) |
| Org keys (own Anthropic and OpenAI keys) and a region per org, with a routing seam for more regions | §17.3 | M6 (M6.7 done) |
| End-to-end tests of the deployed system: a Playwright suite with its own test users and org on staging, each night and on demand | §19.6 | M7 (M7.11 done) |
