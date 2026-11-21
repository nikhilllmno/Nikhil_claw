"""Coding agent connectors: Claude Code and Codex in the session sandbox (M7.2).

docs/architecture.md, Section 15.4. The control plane runs the Claude Code CLI or the Codex CLI
headless in the E2B sandbox of a session. The CLI calls models through the sama model proxy
with the run token, and git goes through the sama git proxy. No provider key and no GitHub
token goes to the sandbox.

Three ways reach a coding agent:

- the tool ``coding_agent_run(agent, repo, prompt)`` of the toolset ``coding_agents``;
- the channel commands ``/claude <task>`` and ``/codex <task>``: one run of the coding agent in
  the session;
- the direct mode: ``/claude`` or ``/codex`` with no task, or an agent profile with the runtime
  ``claude_code`` or ``codex``. Each message of the session then goes to the CLI, until
  ``/sama``.
"""

from sama.coding.agents import (
    CODING_AGENTS,
    CodingAgent,
    agent_for,
    coding_model,
    runtime_of,
)

__all__ = ["CODING_AGENTS", "CodingAgent", "agent_for", "coding_model", "runtime_of"]
