# Nikhil_claw — Hermes Feature Map

| | |
|---|---|
| Status | Draft v0.1, 2026-11-18 |
| Source | Hermes Agent, commit `0e8e4216` (MIT, Copyright (c) 2025 Nous Research) |
| Design | [`architecture.md`](architecture.md) (sections are linked as §N) |
| Plan | [`roadmap.md`](roadmap.md) (milestones M1 to M6) |

This document lists the Hermes features, where they live in Hermes, and what claw does for
each one. Hermes paths are relative to the Hermes repository root.

**Decision key**

| Decision | Meaning |
|---|---|
| **Adapt** | Claw keeps the behaviour and redesigns it for a multi-user cloud service and two SDKs. |
| **SDK** | The Claude Agent SDK or the OpenAI Agents SDK already does it. Claw configures it. |
| **New** | Hermes does not have it. Claw adds it. |
| **Defer** | Useful, but after M6 or on request. |
| **Skip** | Not needed for a cloud, multi-user engine. |

**Runtime parity**: unless a row says otherwise, a feature works on both runtimes, because
claw implements it as a claw tool or in the control plane (architecture §5.3).

---

## 1. Agent core, prompt, and models

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| Agent loop | `run_agent.py`, `agent/` | The SDK runs the loop. Claw wraps it in the `AgentRuntime` contract (§5). | SDK | M1 |
| Provider abstraction (many providers) | `agent/transports/`, `providers/` | Only Anthropic and OpenAI models. Claude adapter (default) and OpenAI Agents adapter. | Adapt | M1, M3 |
| Three-tier prompt (stable, context, volatile) | `agent/prompt_builder*.py` | Frozen prefix per session: SOUL, context files, memory snapshot, skill index. Volatile notes on the user message (§6.1, D9). | Adapt | M1 |
| `SOUL.md` personality | `~/.hermes/SOUL.md` | SOUL per agent profile, versioned rows, changes through `change_propose` (§15.2). | Adapt | M1 |
| Context files (`AGENTS.md`, `CLAUDE.md`, `.hermes.md`) | `agent/context_files*.py` | Loaded from the repo in the sandbox and from workspace settings, 20,000-char cap, injection scan (§6). | Adapt | M1 |
| Personalities (`/personality`) | `hermes_cli` | Agent profiles cover this. | Adapt | M3 |
| Model switch per session (`/model`) | `hermes_cli/commands.py` | `/model` sets the model for the next run. Prefix fingerprint changes, so a new prefix is built. | Adapt | M1 |
| Fallback model and credential pools | `agent/credential_pool*.py` | The model proxy retries on a second key or model of the same provider. | Adapt | M3 |
| Prompt caching | `agent/prompt_caching*.py` | SDK caching plus the frozen prefix. Proxy records cache tokens. | SDK | M1 |
| Reasoning effort settings | config `agent.reasoning_effort` | Profile setting passed to the SDK (`thinking` for Claude, `reasoning` for OpenAI). | SDK | M1 |
| Mixture of Agents (`hermes moa`) | `agent/moa_loop.py` | Not planned. Sub-agents cover most uses. | Defer | — |
| Codex app-server runtime | `agent/transports/codex_app_server.py` | Not planned. A third runtime would use the same contract. | Skip | — |

## 2. Built-in tools

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| `terminal`, `process_manage` | `tools/terminal_tool.py`, `tools/process_registry.py` | Native shell tool of the SDK, in the E2B sandbox. Background processes through the sandbox. | SDK | M1 |
| `read_file`, `write_file`, `patch`, `search_files` | `tools/file_tools.py` | Native file tools (Claude `Read`, `Write`, `Edit`, `Grep`; OpenAI sandbox filesystem and `apply_patch`). | SDK | M1 |
| Terminal backends (local, Docker, SSH, Modal, Daytona, ...) | `tools/environments/` | One `SandboxProvider`: E2B in the cloud, Docker for development and CI (D5). | Adapt | M1 |
| `web_search`, `web_extract` | `tools/web_tools.py` | Claw tools, search provider key on the control plane (§10.2). | Adapt | M1 |
| `x_search` | `tools/x_search_tool.py` | Through an integration if needed. | Defer | — |
| `vision_analyze` | `tools/vision_tools.py` | Claw `media` tool. Both models read images natively. | Adapt | M2 |
| `video_analyze`, `video_generate` | `tools/vision_tools.py`, `tools/video_generation_tool.py` | Not planned. | Defer | — |
| `image_generate` | `tools/image_generation_tool.py` | Claw `media` tool with the OpenAI image model, behind `tool_search`. | Adapt | M4 |
| `text_to_speech` | `tools/tts_tool.py` | Claw `media` tool. Telegram sends a voice note. | Adapt | M4 |
| Speech to text for voice memos | `tools/transcription_tools.py` | Gateway transcribes inbound voice with the OpenAI transcription model before the run (§11). | Adapt | M1 |
| `todo_list` | `tools/todo_tool.py` | Native todo tools of the SDK (Claude `TodoWrite`). Claw shows the list in the web app. | SDK | M1 |
| `clarify` | `tools/clarify_tool.py` | Claw tool, rendered as buttons per channel. Denied in unattended runs. | Adapt | M1 |
| `execute_code` (RPC to tools) | `tools/code_execution_tool.py` | Claw `code_exec`: Python in the sandbox with a stub that calls claw tools over MCP. | Adapt | M4 |
| `computer_use` (desktop control) | `tools/computer_use_tool.py` | Browser vision mode (§14) covers web apps. Full desktop control is not planned. | Defer | — |
| `manage_connections` | `tools/connectors/tool.py` | `connections_list`, `connection_request` with Composio OAuth links (§10.3). | Adapt | M3 |
| Discord and Feishu tools | `tools/discord_tool.py`, `tools/feishu_*.py` | Through Composio toolkits when needed. | Defer | — |
| Desktop-only tools (`desktop_ui`, `project`, `catalog`) | `tools/project_tools.py` and others | No desktop app. | Skip | — |
| No agent-callable `send_message` | `toolsets.py` | Same rule: replies go to the trigger's channel (§10.1). | Adapt | M1 |
| Toolset bundles (`coding`, `safe`, platform bundles) | `toolsets.py` | Presets `assistant`, `coding`, `safe`, `cron` (§10.2). | Adapt | M1 |
| Tool `check_fn` gating | `tools/registry.py` | Tools hidden when their service or credential is missing (§10.1). | Adapt | M1 |

## 3. Skills

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| `SKILL.md` format (Agent Skills standard) | `skills/`, `agent/skill_*.py` | Same format. Stored as versioned rows plus files in Supabase Storage, synced into the sandbox (§9). | Adapt | M2 |
| Progressive disclosure (index, view, files) | `tools/skills_tool.py` | Index in the prefix (60-char descriptions), `skill_view` loads the body and files. Claude native skills are an option. | Adapt | M2 |
| `skills_list`, `skill_view`, `skill_manage` | `tools/skills_tool.py`, `tools/skill_manager_tool.py` | Same tool names. `skill_manage`: create, patch, edit, write_file, remove_file, archive. | Adapt | M2 |
| Skill scopes (builtin, user dir, project) | `agent/skill_*.py` | builtin, org, workspace, user. User scope applies at once. Shared scopes need approval (§9). | Adapt | M2 |
| Skills as slash commands (`/<skill>`) | `gateway/slash_commands.py` | `/<skill-name>` in every channel (§11.1). | Adapt | M2 |
| Bundled skills (44) and optional skills (141) | `skills/`, `optional-skills/` | We write a small set of our own builtin skills. We do not copy Hermes skill text without attribution. | Adapt | M2 |
| Skills Hub install with quarantine and scan | `tools/skills_hub*.py`, `skills_guard.py` | Import from a Git URL or `agentskills.io` with a lint and injection scan, then approval. | Adapt | M4 |
| Conditional activation (`requires_toolsets`, platforms) | frontmatter | Same frontmatter keys. Skills whose tools are missing are hidden. | Adapt | M2 |
| Per-skill config | `skills.config` | Skill settings per workspace, filled in the web app. | Adapt | M3 |
| Skill bundles | `agent/skill_bundles.py` | Agent profiles list skills. | Adapt | M2 |

## 4. Memory

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| `MEMORY.md` (2,200 chars) and `USER.md` (1,375 chars) | `tools/memory_tool.py` | Targets `agent` (2,200), `user` (1,375), and `workspace` (2,200) per profile and user (§8). | Adapt | M2 |
| `memory` tool: add, patch, remove | `tools/memory_tool.py` | Same actions. Over-limit writes fail and return the current entries. | Adapt | M2 |
| Frozen memory snapshot in the prompt | `agent/memory_manager.py` | Snapshot in the frozen prefix. New writes apply from the next session. | Adapt | M2 |
| Memory nudges | `agent/background_review.py` | Background review every 10 user turns (§9). | Adapt | M2 |
| External memory providers (Mem0, Honcho, ...) | `plugins/memory/` | Optional Mem0 provider behind a `MemoryProvider` interface. | Defer | — |
| Memory history | none (files only) | `memory_revisions` table with author, run, and reason. Revert in the web app. | New | M2 |

## 5. Sessions

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| SQLite `state.db` with FTS5 | `hermes_state.py`, `persistence/` | Postgres tables `sessions`, `messages`, `tool_calls`, `runs`, `run_events` with RLS (§7). | Adapt | M1 |
| Session per chat, survives restarts | `gateway/session.py` | `session_bindings` map a channel thread to a session. | Adapt | M1 |
| `/new`, `/reset`, `/resume`, `/title`, `/branch` | `hermes_cli/commands.py` | Same commands in all channels. Branch creates a child session (lineage). | Adapt | M1 |
| `session_search` | `tools/session_search_tool.py` | Postgres full-text plus pgvector. No LLM call in the tool (§7). | Adapt | M2 |
| Session lineage after compression | `agent/context_compressor*.py` | Parent and child sessions with the archived transcript (§6.2). | Adapt | M2 |
| Queue, interrupt, steer while busy | `gateway/session_*.py` | New message while a run is busy: queue by default, `/stop` cancels, `/steer` adds a note to the running turn. | Adapt | M3 |
| Cross-platform handoff (`/handoff`) | `gateway/` | A session can bind to more than one channel. `/handoff telegram` moves replies. | Adapt | M3 |
| Retention and housekeeping | `session_retention.py` | Retention per org plan. Export and delete per user. | Adapt | M6 |
| Export and import | `/export`, `/import` | Export to Markdown and JSON from the web app. | Adapt | M3 |

## 6. Context management

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| Compression at 50% of the window, protect last 20 | `agent/context_compressor.py` | Native compaction in both SDKs. Claw archives the transcript before and records a `Compacted` event (§6.2). | SDK | M2 |
| Tool output cap (50,000 bytes) and spill to file | `tools/tool_result_storage*.py` | Claw tools cap at 50 KB and spill to a sandbox file with a preview and path. | Adapt | M2 |
| Tool search (`tool_search`, `tool_describe`, `tool_call`) | `tools/tool_search*.py` | Same three tools for deferred tools and integrations. Claude native tool search is an option. | Adapt | M2 |
| Token accounting | `agent/usage*.py` | Model proxy records tokens and cost per call (§18). | Adapt | M1 |
| `@file`, `@url` references | `agent/context_references.py` | `@file`, `@url`, `@session` in a message. | Adapt | M4 |
| `/compress` | slash command | `/compact` asks the runtime to compact now. | Adapt | M2 |
| Context pins | none | A user pins a message or file so that compaction keeps it. | New | M2 |

## 7. Self-improvement and self-modification

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| Background review fork (memory and skills) | `agent/background_review.py` | A separate review run after the main run, with a cheap model, recursion off, and only `memory` and `skill_manage` (§9). | Adapt | M2 |
| Skill creation nudge (10 tool iterations) | `agent/agent_init.py` | Same default. | Adapt | M2 |
| `/learn` (skill from sources) | `learn_prompt.py` | `/learn <url or files>` creates a draft skill for review. | Adapt | M4 |
| Curator (7 days, active, stale, archived) | `agent/curator*.py` | Weekly curator job per workspace. Archive, never delete. Pins (§9). | Adapt | M6 |
| Checkpoints and `/rollback`, `/undo` | `agent/checkpoints/` | Shadow git in the sandbox or Claude file checkpointing. User commands, not model tools (§15.3). | Adapt | M6 |
| Config changes by the agent | `hermes config set` through the terminal | `change_propose` creates an approval card for SOUL, profile, toolsets, and policy (§15.2). | Adapt | M6 |
| Changes to its own code | `self_repo_guard.py`, `hermes update` | `claw_pr`: a pull request to the claw repo that a human merges. CI deploys (§15.2). | New | M6 |
| Version history and revert for all agent changes | partial (curator backups) | Every self-improvement write is a versioned row with provenance (D14). | New | M2 |

## 8. Scheduler, cron, and automation

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| Cron jobs (`jobs.json`, 60 s tick) | `cron/`, `tools/cronjob_tools.py` | Postgres `jobs` table, 60 s tick with a leader lease (§12.1). | Adapt | M4 |
| `cronjob_manage` actions | `tools/cronjob_tools.py` | Same name and actions. | Adapt | M4 |
| Natural language schedules | `cron/` | The agent writes a cron expression with a time zone. Claw validates it and shows the next 3 fire times. | Adapt | M4 |
| Delivery to any channel with receipts | `cron/delivery*.py` | Delivery targets and receipts. An unknown delivery result is not retried (§12.1). | Adapt | M4 |
| No cron recursion | `toolsets.py` (`hermes-cron`) | A job run cannot create jobs. | Adapt | M4 |
| Per-job model and script-only jobs | `cron/` | `model` pin and `mode: script`. | Adapt | M4 |
| Unattended approvals deny | `tools/approval*.py` | `ask` becomes `deny` unless the job pre-approved the tool (D13). | Adapt | M1 |
| Webhooks (restricted toolset) | `gateway/platforms/webhook*.py` | Signed webhook triggers with the `safe` toolset (§12.2). | Adapt | M4 |
| Heartbeat, goals, loops | `gateway/run_goals.py` | A job with `attach_to_session` runs inside one session (heartbeat). | Adapt | M4 |
| Automation blueprints | `cron/blueprint_catalog.py` | Workflow templates (§12.3). | Adapt | M4 |
| Workflows (multi-step, approvals, durable) | none | YAML workflows with tool, agent, approval, branch, parallel, wait steps on the Postgres queue (§12.3). | New | M4 |
| Composio triggers | none | Composio triggers start runs or workflows (§10.3). | New | M4 |

## 9. Channels

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| Telegram adapter | `plugins/platforms/telegram/` | Bot API webhook adapter, inline keyboards, voice notes (§11.2). | Adapt | M1 |
| Slack adapter | `plugins/platforms/slack/` | Slack Bolt, Events API, Block Kit approvals, thread = session (§11.2). | Adapt | M3 |
| 24 other platforms (Discord, WhatsApp, Signal, Email, Teams, ...) | `plugins/platforms/` | Same adapter shape. Email and WhatsApp first, on request. | Defer | — |
| API server (OpenAI-compatible) | `gateway/platforms/api_server*.py` | Claw HTTP API with streaming events (§4.1). OpenAI-compatible endpoint later. | Adapt | M1 |
| Authorization ladder, default deny | `gateway/authz_mixin.py` | Paired identities only. Group policy per binding (§11.3). | Adapt | M1 |
| DM pairing codes with rate limits and lockout | `gateway/pairing.py` | Codes from the web app, 10-minute expiry, lockout (§11.3). | Adapt | M1 |
| Approvals over chat (buttons, `/approve`, `/deny`) | `gateway/approval_actor.py` | Approval cards per channel (§11.1). | Adapt | M3 |
| Streaming edits to messages | `gateway/stream_consumer*.py` | Edit one message as text arrives, with rate limits per channel. | Adapt | M1 |
| Slash command registry | `hermes_cli/commands.py` | One command registry for all channels (§11.1). | Adapt | M1 |
| Web dashboard | `web/` | Next.js web app: chat, approvals, memory, skills, jobs, workflows, connections, traces (§11.2). | Adapt | M1 (minimal), M3, M6 |
| Chrome extension side panel | none | MV3 extension with chat bound to the current tab (§14.2). | New | M5 |

## 10. Sub-agents and multi-agent work

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| `delegate_task` (single and batch) | `tools/delegate_tool.py` | Child session and run in its own sandbox. Summary returns to the parent (§13). | Adapt | M4 |
| Limits: depth 1, 10 children, blocked tools | `tools/delegate_tool_config.py` | Same defaults. Child runs also have a cost budget. | Adapt | M4 |
| Cheaper model for children | `delegation.model` | Child model per profile. | Adapt | M4 |
| Native in-run sub-agents | none (Hermes has its own loop) | Claude `agents`, OpenAI agents as tools and handoffs (§13). | SDK | M2 |
| Background delegation and steering | `tools/async_delegation*.py` | `delegate_status`, cancel, and steer for running children. | Adapt | M4 |
| Kanban board and dispatcher | `hermes kanban` | Task board with `task_*` tools (§12.4). | Adapt | M4 |
| Worker bots in Slack channels | `gateway/bot_*` | An agent profile bound to a Slack channel with a budget. | Adapt | M3 |

## 11. Browser

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| Browser over CDP (`browser_exec`, `browser_cdp`) | `tools/browser_*.py` | Typed tools on snapshots with refs: `browser_open`, `browser_snapshot`, `browser_act`, ... (§14.1). | Adapt | M5 |
| Cloud browsers (Browserbase, Browser Use cloud) | `agent/browser_session_cloud*.py` | Browserbase with a persistent context per user (§14.1). | Adapt | M5 |
| Local Chromium and real profile | `agent/browser_launcher*.py` | Not in the cloud. The Chrome extension replaces the real-profile mode. | Skip | — |
| Attach to the user's Chrome over CDP | `/browser connect` | Chrome extension with `chrome.debugger` (§14.2). | New | M5 |
| `browser_dialog` | `tools/browser_*.py` | `browser_act` handles dialogs (`accept`, `dismiss`). | Adapt | M5 |
| `browser_vault` (logins never in the transcript) | `agent/vault_*.py` | Claw vault fills logins inside the tool (§14.1). | Adapt | M5 |
| URL policy and SSRF guard | `tools/browser_url_policy.py` | URL policy per org on both browser targets (§16). | Adapt | M5 |
| Vision mode (screenshot and coordinates) | `computer_use` | `browser_screenshot` plus coordinate actions (§14.1). | Adapt | M5 |
| Visible cursor, click, type in the user's Chrome | none | Extension overlay and CDP input events, per-site consent (§14.2). | New | M5 |

## 12. MCP, integrations, and plugins

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| MCP client (stdio, HTTP, OAuth) | `tools/mcp_tool*.py` | Remote MCP servers only (no stdio in the cloud), proxied by the claw MCP server with OAuth tokens in the vault (§10.3). | Adapt | M3 |
| MCP tool naming `mcp_<server>_<tool>` | `tools/mcp_tool.py` | Same naming, behind `tool_search`. | Adapt | M3 |
| Managed connectors (Nous tool gateway) | `tools/connectors/` | Composio with entities per user and per workspace (§10.3). | Adapt | M3 |
| Plugin system | `plugins/`, `hermes_cli/plugins*` | Extension points for products (tools, skills, toolsets, channels, policies) (D16). | Adapt | M1 |
| ACP adapter for editors | `acp_adapter/` | Not planned. | Defer | — |

## 13. Safety

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| Hardline deny floor | `tools/approval_detection.py` | Same idea, own pattern list (§16.1). | Adapt | M1 |
| Dangerous command approval, `smart` mode | `tools/approval*.py` | Policy `allow`, `ask`, `deny` plus an optional small-model classifier (§16.1). | Adapt | M1 |
| Protected paths and write guards | `tools/file_tools_write_guards.py` | Sandbox isolation plus protected paths in repos (for example `.github/workflows`) need `ask`. | Adapt | M3 |
| Secret redaction | `agent/redact*.py` | Redaction of tool output, logs, and traces (§16.3). | Adapt | M1 |
| Secrets in `.env` and vault backends | `agent/secret_sources/` | Claw vault in Postgres. No secrets in the sandbox (D6). | Adapt | M1 |
| SSRF and web access policy | `tools/website_policy.py` | Egress allowlist and URL policy (§16.2). | Adapt | M1 |
| Prompt-injection scans for context and skills | `agent/prompt_injection*.py` | Scans for context files, skills, and imported content (§16.4). | Adapt | M2 |
| Container sandbox backends | `tools/environments/` | E2B for every run (D4, D5). | Adapt | M1 |
| Multi-user roles and tenant isolation | none | Orgs, workspaces, roles, RLS (§17). | New | M1 |
| Budgets per run, user, org | `agent/budget_config.py` (bot mode) | Proxy budgets at every level (§18). | Adapt | M1 |

## 14. Surfaces and operations

| Hermes feature | Hermes location | Claw design | Decision | Milestone |
|---|---|---|---|---|
| CLI and TUI | `cli.py`, `ui-tui/` | Cloud only. A small admin CLI (`claw`) for operators may come later. | Skip | — |
| Desktop app | `apps/desktop/` | No desktop app. The web app and the Chrome extension cover it. | Skip | — |
| Profiles (separate homes) | `~/.hermes/profiles/` | Agent profiles and workspaces in Postgres (§17). | Adapt | M1 |
| Setup wizard, doctor, update | `hermes setup`, `hermes doctor` | Web onboarding for customers (M6). Health checks and alerts (§18). | Adapt | M6 |
| Gateway service management | `hermes gateway install` | Fly.io process groups, deploys from CI (§19). | Adapt | M1 |
| Batch runner and trajectories | `batch_runner.py`, `trajectory_compressor.py` | Not planned. Langfuse datasets cover evals. | Defer | — |
| Evals | `evals/` | Conformance suite in CI and a nightly live suite (§5.5). | Adapt | M1 |
| Langfuse and telemetry | `agent/monitoring/` | Langfuse for both runtimes, proxy cost (D15). | Adapt | M1 |
| Skins, pets, achievements, language packs | various | Not planned. | Skip | — |

## 15. Features claw adds

| Feature | Claw design | Milestone |
|---|---|---|
| Two runtimes behind one contract with a conformance suite | §5 | M1, M3 |
| Multi-user orgs, workspaces, roles, RLS | §17 | M1 |
| Model proxy with budgets and no keys in the sandbox | D6, §18 | M1 |
| Workflows with approval steps | §12.3 | M4 |
| Chrome extension: click, type, cursor in the user's browser | §14.2 | M5 |
| Versioned and revertible self-improvement | D14 | M2 |
| Self-modification through pull requests | §15.2 | M6 |
| Customer sign-up, billing, quotas | §17 | M6 |
