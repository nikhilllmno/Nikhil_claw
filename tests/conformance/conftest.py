"""Fixtures of the conformance suite. ``target`` runs each test once per runtime target.

- ``fake`` and ``claude-scripted`` run in CI. ``claude-scripted`` needs the Claude SDK and
  its bundled CLI (the default ``runtime-claude`` group).
- ``claude-runner`` runs in CI. It starts the runner in a local sandbox folder. Its model
  calls go through the model proxy to the scripted model, as in production (Section 16.6).
- ``claude-docker`` runs only with ``SAMA_DOCKER_IMAGE``, the tag of the sama image
  (``sandbox/Dockerfile``). The container uses the host network to reach the test servers.
- ``claude-live`` runs only with ``SAMA_LIVE_TESTS=1`` and ``ANTHROPIC_API_KEY``. It has the
  ``live`` marker, so ``pytest -m live`` selects it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from sama.modelproxy import MemoryUsageStore, ModelProxy, PriceTable, ProxySettings
from sama.orchestrator.sandboxed import RUNNER_ARGV, SandboxedRuntime, SandboxSettings
from sama.runtime import RunConfig, RunScope, RuntimeKind
from sama.runtime.runner import GatePolicy
from sama.sandbox import SandboxProvider
from sama.sandbox.docker import DockerProvider
from sama.sandbox.local import LocalProvider
from tests.conformance.scripted_model import ScriptedModel, serve
from tests.conformance.targets import (
    GATED_TOOL_NAMES,
    LIVE_MODEL,
    LIVE_PROMPTS,
    SCRIPTED_PROMPTS,
    ClaudeTarget,
    FakeTarget,
    HostFiles,
    SandboxFiles,
    Target,
    claude_env,
    is_gated,
    mcp_config,
)
from tests.conformance.tool_server import ToolServer

if TYPE_CHECKING:
    from sama.runtime.claude import ToolGate

HAS_CLAUDE_SDK = importlib.util.find_spec("claude_agent_sdk") is not None
DOCKER_IMAGE = os.environ.get("SAMA_DOCKER_IMAGE", "")
LIVE = os.environ.get("SAMA_LIVE_TESTS") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))

TARGETS = [
    pytest.param("fake", id="fake"),
    pytest.param(
        "claude-scripted",
        id="claude-scripted",
        marks=pytest.mark.skipif(not HAS_CLAUDE_SDK, reason="claude-agent-sdk is not installed"),
    ),
    pytest.param(
        "claude-runner",
        id="claude-runner",
        marks=pytest.mark.skipif(not HAS_CLAUDE_SDK, reason="claude-agent-sdk is not installed"),
    ),
    pytest.param(
        "claude-docker",
        id="claude-docker",
        marks=pytest.mark.skipif(not DOCKER_IMAGE, reason="set SAMA_DOCKER_IMAGE"),
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


@pytest.fixture(scope="session")
def model_proxy(
    scripted_model: tuple[ScriptedModel, str], tool_server: tuple[ToolServer, str]
) -> Iterator[tuple[MemoryUsageStore, str]]:
    """The model proxy in front of the scripted model. It accepts the run tokens of the suite."""
    _, model_url = scripted_model
    tools, _ = tool_server
    store = MemoryUsageStore()
    proxy = ModelProxy(
        signer=tools.run_tokens,
        store=store,
        prices=PriceTable.load(),
        settings=ProxySettings(anthropic_url=model_url, anthropic_key=SecretStr("scripted-model")),
    )
    with serve(proxy.app()) as url:
        yield store, url


def _suite_gate() -> ToolGate:
    """Asks for the gated tools and allows the others."""
    from sama.runtime.claude import GateDecision

    class SuiteGate:
        async def check(
            self,
            scope: RunScope,
            tool_name: str,
            arguments: Mapping[str, Any],
            *,
            call_id: str,
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

    if request.param in {"claude-runner", "claude-docker"}:
        store, proxy_url = request.getfixturevalue("model_proxy")
        sandboxed = _sandboxed_runtime(request.param, tools, proxy_url, tmp_path)
        yield ClaudeTarget(
            name=request.param,
            runtime=sandboxed,
            config=_scripted_config(tools, tools_url, "claude-sonnet-4-5"),
            tools=tools,
            prompts=SCRIPTED_PROMPTS,
            files=SandboxFiles(sandboxed),
            model_calls=store,
        )
        await sandboxed.close_sandboxes(kill=True)
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
    yield ClaudeTarget(
        name=request.param,
        runtime=runtime,
        config=_scripted_config(tools, tools_url, LIVE_MODEL if live else "claude-sonnet-4-5"),
        tools=tools,
        prompts=LIVE_PROMPTS if live else SCRIPTED_PROMPTS,
        files=HostFiles(workdir),
    )
    await runtime.aclose()


def _scripted_config(tools: ToolServer, tools_url: str, model: str) -> RunConfig:
    return RunConfig(
        runtime=RuntimeKind.CLAUDE_AGENT_SDK,
        model=model,
        soul="You are a test agent. Follow the user's instructions exactly.",
        mcp_servers=mcp_config(tools, tools_url),
    )


def _sandboxed_runtime(
    name: str, tools: ToolServer, model_url: str, tmp_path: Path
) -> SandboxedRuntime:
    """The orchestrator side. The runner gets only the run token and the model URL."""
    provider: SandboxProvider
    if name == "claude-docker":
        provider = DockerProvider(image=DOCKER_IMAGE, network="host")
        argv = RUNNER_ARGV
    else:
        provider = LocalProvider(tmp_path / "sandboxes")
        argv = (sys.executable, "-m", "sama.runtime.runner")
    return SandboxedRuntime(
        kind=RuntimeKind.CLAUDE_AGENT_SDK,
        provider=provider,
        signer=tools.run_tokens,
        settings=SandboxSettings(template=name, model_url=model_url, runner_argv=argv),
        gate=GatePolicy(ask=GATED_TOOL_NAMES),
    )
