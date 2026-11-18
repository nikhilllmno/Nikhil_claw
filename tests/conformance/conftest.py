"""Fixtures of the conformance suite. ``target`` runs each test once per runtime target.

- ``fake`` and ``claude-scripted`` run in CI. ``claude-scripted`` needs the Claude SDK and
  its bundled CLI (the default ``runtime-claude`` group).
- ``claude-live`` runs only with ``SAMA_LIVE_TESTS=1`` and ``ANTHROPIC_API_KEY``. It has the
  ``live`` marker, so ``pytest -m live`` selects it.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from sama.runtime import RunConfig, RunScope, RuntimeKind
from tests.conformance.scripted_model import ScriptedModel, serve
from tests.conformance.targets import (
    LIVE_MODEL,
    LIVE_PROMPTS,
    SCRIPTED_PROMPTS,
    ClaudeTarget,
    FakeTarget,
    Target,
    claude_env,
    is_gated,
    mcp_config,
)
from tests.conformance.tool_server import ToolServer

if TYPE_CHECKING:
    from sama.runtime.claude import ToolGate

HAS_CLAUDE_SDK = importlib.util.find_spec("claude_agent_sdk") is not None
LIVE = os.environ.get("SAMA_LIVE_TESTS") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))

TARGETS = [
    pytest.param("fake", id="fake"),
    pytest.param(
        "claude-scripted",
        id="claude-scripted",
        marks=pytest.mark.skipif(not HAS_CLAUDE_SDK, reason="claude-agent-sdk is not installed"),
    ),
    pytest.param(
        "claude-live",
        id="claude-live",
        marks=[
            pytest.mark.live,
            pytest.mark.skipif(
                not (LIVE and HAS_CLAUDE_SDK), reason="set SAMA_LIVE_TESTS=1 and ANTHROPIC_API_KEY"
            ),
        ],
    ),
]


@pytest.fixture(scope="session")
def tool_server() -> Iterator[tuple[ToolServer, str]]:
    tools = ToolServer()
    with serve(tools.app()) as url:
        yield tools, url


@pytest.fixture(scope="session")
def scripted_model() -> Iterator[tuple[ScriptedModel, str]]:
    model = ScriptedModel()
    with serve(model.app()) as url:
        yield model, url


def _suite_gate() -> ToolGate:
    """Asks for the gated tools and allows the others."""
    from sama.runtime.claude import GateDecision

    class SuiteGate:
        async def check(
            self, scope: RunScope, tool_name: str, arguments: Mapping[str, Any]
        ) -> GateDecision:
            if is_gated(tool_name):
                return GateDecision("ask", "the suite gates this tool")
            return GateDecision("allow")

    return SuiteGate()


@pytest.fixture(params=TARGETS)
async def target(
    request: pytest.FixtureRequest,
    tool_server: tuple[ToolServer, str],
    tmp_path: Path,
) -> AsyncIterator[Target]:
    tools, tools_url = tool_server
    tools.notes.clear()
    tools.calls.clear()
    if request.param == "fake":
        yield FakeTarget(tools=tools)
        return

    from sama.runtime.claude import ClaudeRuntime, ClaudeSettings

    live = request.param == "claude-live"
    model_url = None
    if not live:
        _, model_url = request.getfixturevalue("scripted_model")
    workdir = tmp_path / "work"
    workdir.mkdir()
    settings = ClaudeSettings(cwd=str(workdir), env=claude_env(tmp_path / "claude", model_url))
    runtime = ClaudeRuntime(gate=_suite_gate(), settings=settings)
    config = RunConfig(
        runtime=RuntimeKind.CLAUDE_AGENT_SDK,
        model=LIVE_MODEL if live else "claude-sonnet-4-5",
        soul="You are a test agent. Follow the user's instructions exactly.",
        mcp_servers=mcp_config(tools, tools_url),
    )
    yield ClaudeTarget(
        name=request.param,
        runtime=runtime,
        config=config,
        tools=tools,
        prompts=LIVE_PROMPTS if live else SCRIPTED_PROMPTS,
        workdir=workdir,
    )
    await runtime.aclose()
