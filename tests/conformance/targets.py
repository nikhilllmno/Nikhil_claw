"""Conformance targets: one per runtime and model (docs/architecture.md, Section 5.5).

A target owns a runtime, its ``RunConfig``, and the MCP tool server. ``prompt`` turns a
scenario into the input text of a run. The suite then checks only contract events, so the same
tests run on every target.

- ``fake``: ``FakeRuntime`` with a script per scenario. It calls no MCP server.
- ``claude-scripted``: ``ClaudeRuntime`` and the real Claude CLI, with the scripted model as
  the Messages API and a real MCP tool server. CI runs it.
- ``claude-live``: the same with a real Claude model. The nightly job runs it.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from sama.runtime import (
    AgentRuntime,
    ApprovalRequired,
    Compacted,
    FailureCode,
    McpServerConfig,
    RunCompleted,
    RunConfig,
    RunEvent,
    RunFailed,
    RuntimeKind,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    Usage,
)
from sama.runtime.fake import FakeRuntime, FakeScript
from tests.conformance.tool_server import LOOKUP, SEND_NOTE, SERVER_NAME, ToolServer, tool_id

CANCEL_LIMIT_S = 10.0
LIVE_MODEL = "claude-haiku-4-5"


class Scenario(StrEnum):
    TEXT = "text"  # reply with a word
    REMEMBER = "remember"  # store a word in the session
    RECALL = "recall"  # name the stored word, after a resume
    LOOKUP = "lookup"  # call the allowed MCP tool and use its result
    NOTE = "note"  # call the MCP tool that the gate asks about
    WRITE = "write"  # write a file with the native shell tool, which the gate asks about
    BUDGET = "budget"  # a tool call with max_turns=1, so the run hits the limit
    SLOW = "slow"  # a long reply, to cancel
    COMPACT = "compact"  # compact the session


class Target(Protocol):
    """``calls_mcp`` is true if the runtime really calls the MCP tool server."""

    def prompt(self, scenario: Scenario, word: str = "") -> str: ...

    @property
    def name(self) -> str: ...
    @property
    def runtime(self) -> AgentRuntime: ...
    @property
    def config(self) -> RunConfig: ...
    @property
    def tools(self) -> ToolServer: ...
    @property
    def calls_mcp(self) -> bool: ...
    @property
    def workdir(self) -> Path | None:
        """The folder of native file tools, or None if the runtime has no native tools."""
        ...


GATED_TOOLS = (SEND_NOTE,)
WRITE_TOOL = "Bash"
"""The native shell tool. On an approve resume the Claude CLI sends a shell write through
``can_use_tool``, but an MCP call through the hook, so the suite covers both paths."""
NOTE_FILE = "note.txt"


def is_gated(tool_name: str) -> bool:
    """The tool gate of the suite: it asks for every gated tool and allows the others."""
    if tool_name == WRITE_TOOL:
        return True
    return any(tool_name in {name, tool_id(name)} for name in GATED_TOOLS)


def mcp_config(tools: ToolServer, url: str) -> tuple[McpServerConfig, ...]:
    return (
        McpServerConfig(name=SERVER_NAME, url=f"{url}/mcp", bearer_token=SecretStr(tools.token)),
    )


# ---------------------------------------------------------------------------------------------
# Claude: scripted model and live model
# ---------------------------------------------------------------------------------------------

SCRIPTED_PROMPTS: Mapping[Scenario, str] = {
    Scenario.TEXT: "Say {word}",
    Scenario.REMEMBER: "Remember the word {word}",
    Scenario.RECALL: "What word did I ask you to remember?",
    Scenario.LOOKUP: "CALL " + tool_id(LOOKUP) + ' {{"key": "colour"}}',
    Scenario.NOTE: "CALL " + tool_id(SEND_NOTE) + ' {{"text": "{word}"}}',
    Scenario.WRITE: "CALL " + WRITE_TOOL + ' {{"command": "echo {word} > {path}"}}',
    Scenario.BUDGET: "CALL " + tool_id(LOOKUP) + ' {{"key": "colour"}}',
    Scenario.SLOW: "SLOW 40",
    Scenario.COMPACT: "/compact",
}

LIVE_PROMPTS: Mapping[Scenario, str] = {
    Scenario.TEXT: "Reply with exactly this word and nothing else: {word}",
    Scenario.REMEMBER: "Remember the word {word}. Reply with only: OK",
    Scenario.RECALL: "What word did I ask you to remember? Reply with only that word.",
    Scenario.LOOKUP: (
        f"Call the {LOOKUP} tool with key 'colour'. Then reply with the exact value that it"
        " returned and nothing else."
    ),
    Scenario.NOTE: f"Use the {SEND_NOTE} tool to send this exact note: {{word}}",
    Scenario.WRITE: (
        f"Use the {WRITE_TOOL} tool to run exactly this command: echo {{word}} > {{path}}"
    ),
    Scenario.BUDGET: f"Call the {LOOKUP} tool with key 'colour', then tell me the value.",
    Scenario.SLOW: "Write a 600-word story about a lighthouse keeper.",
    Scenario.COMPACT: "/compact",
}


@dataclass
class ClaudeTarget:
    name: str
    runtime: AgentRuntime
    config: RunConfig
    tools: ToolServer
    prompts: Mapping[Scenario, str]
    workdir: Path | None
    calls_mcp: bool = True

    def prompt(self, scenario: Scenario, word: str = "") -> str:
        path = self.workdir / NOTE_FILE if self.workdir else NOTE_FILE
        return self.prompts[scenario].format(word=word, path=path)


def claude_env(config_dir: Path, model_url: str | None) -> dict[str, str]:
    """The CLI environment. A clean config folder keeps host skills and memory out of the run."""
    env = {
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    if model_url is not None:
        env |= {"ANTHROPIC_BASE_URL": model_url, "ANTHROPIC_API_KEY": "scripted-model"}
    return env


# ---------------------------------------------------------------------------------------------
# Fake
# ---------------------------------------------------------------------------------------------


def _usage(step: int) -> Usage:
    return Usage(input_tokens=10, output_tokens=step)


def _reply(text: str, start: int = 1) -> list[RunEvent]:
    return [
        TextDelta(text=text, usage=_usage(start)),
        RunCompleted(output=text, usage=_usage(start), runtime_state=b"{}"),
    ]


@dataclass
class FakeTarget:
    """Scripts that match what a real runtime streams for each scenario."""

    tools: ToolServer
    name: str = "fake"
    calls_mcp: bool = False
    workdir: Path | None = None
    runtime: FakeRuntime = field(default_factory=FakeRuntime)
    config: RunConfig = field(
        default_factory=lambda: RunConfig(runtime=RuntimeKind.FAKE, model="fake")
    )
    _remembered: str = ""

    def prompt(self, scenario: Scenario, word: str = "") -> str:
        self.runtime.add_script(self._script(scenario, word))
        return f"{scenario}:{word}"

    def _script(self, scenario: Scenario, word: str) -> FakeScript:
        if scenario is Scenario.TEXT:
            return FakeScript(events=_reply(word))
        if scenario is Scenario.REMEMBER:
            self._remembered = word
            return FakeScript(events=_reply("OK"))
        if scenario is Scenario.RECALL:
            return FakeScript(events=_reply(self._remembered))
        if scenario is Scenario.LOOKUP:
            name = tool_id(LOOKUP)
            value = f"colour={self.tools.secret}"
            arguments = {"key": "colour"}
            return FakeScript(
                events=[
                    ToolCallStarted(call_id="c1", name=name, arguments=arguments, usage=_usage(1)),
                    ToolCallFinished(call_id="c1", output_preview=value, usage=_usage(1)),
                    *_reply(value, start=2),
                ]
            )
        if scenario in {Scenario.NOTE, Scenario.WRITE}:
            if scenario is Scenario.NOTE:
                name, arguments = tool_id(SEND_NOTE), {"text": word}
            else:
                name, arguments = WRITE_TOOL, {"command": f"echo {word} > {NOTE_FILE}"}
            return FakeScript(
                events=[
                    ToolCallStarted(call_id="c2", name=name, arguments=arguments, usage=_usage(1)),
                    ApprovalRequired(
                        approval_id="c2",
                        call_id="c2",
                        tool_name=name,
                        arguments=arguments,
                        reason="write",
                        usage=_usage(1),
                        runtime_state=b"{}",
                    ),
                ],
                on_approve=[
                    ToolCallFinished(call_id="c2", output_preview="sent", usage=_usage(1)),
                    *_reply("sent", start=2),
                ],
                on_reject=[
                    ToolCallFinished(call_id="c2", output_preview="rejected", usage=_usage(1)),
                    *_reply("not sent", start=2),
                ],
            )
        if scenario is Scenario.BUDGET:
            # The fake does not count turns. It replays the stop that a real runtime reports.
            return FakeScript(
                events=[
                    ToolCallStarted(call_id="c3", name=tool_id(LOOKUP), usage=_usage(1)),
                    ToolCallFinished(call_id="c3", output_preview="colour=...", usage=_usage(1)),
                    RunFailed(
                        code=FailureCode.BUDGET_EXCEEDED,
                        message="the run reached max_turns",
                        usage=_usage(1),
                    ),
                ]
            )
        if scenario is Scenario.SLOW:
            words = [TextDelta(text=f"word{i} ", usage=_usage(i)) for i in range(1, 41)]
            return FakeScript(events=[*words, *_reply("done", start=41)], delay_s=0.2)
        if scenario is Scenario.COMPACT:
            return FakeScript(
                events=[
                    Compacted(tokens_before=1000, tokens_after=100, usage=_usage(1)),
                    RunCompleted(output="", usage=_usage(1), runtime_state=b"{}"),
                ]
            )
        raise AssertionError(f"no fake script for {scenario}")


def new_word() -> str:
    return f"tamarind{secrets.randbelow(10_000):04d}"
