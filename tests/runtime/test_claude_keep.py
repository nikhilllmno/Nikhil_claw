"""Tests of the kept Claude CLI client (docs/architecture.md, Section 4.4).

A fake SDK client replaces the CLI. The tests check when a run reuses the client of the last
run and when it starts a new one, and that the callbacks and the token of a kept client are
those of the active run.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from typing import Any, cast

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import ClaudeAgentOptions, HookInput, ToolUseBlock
from claude_agent_sdk.types import PermissionResultDeny, ToolPermissionContext

from sama.runtime import (
    Budget,
    FailureCode,
    RunCompleted,
    RunFailed,
    RunRequest,
    RuntimeKind,
)
from sama.runtime.claude import (
    ClaudeClient,
    ClaudeKeeper,
    ClaudeRuntime,
    ClaudeSettings,
    GateDecision,
)
from sama.runtime.relay import TokenRelay
from tests.runtime.test_claude import (
    ScriptedGate,
    assistant,
    call_hook,
    config,
    delta,
    result,
)
from tests.support import collect, make_config, make_scope

pytestmark = pytest.mark.asyncio


class KeptClient:
    """A fake CLI process that serves more than one query. Each query plays the next script."""

    def __init__(self, options: ClaudeAgentOptions, scripts: list[Sequence[Any]]) -> None:
        self.options = options
        self.scripts = scripts
        self.prompts: list[str] = []
        self.connects = 0
        self.disconnected = False
        self.interrupted = asyncio.Event()
        self.status_error: Exception | None = None
        self.mcp_status: dict[str, Any] = {"mcpServers": [{"name": "sama", "status": "connected"}]}
        self._current: Sequence[Any] = ()
        self.hook_outputs: list[dict[str, Any]] = []

    async def connect(self, prompt: str | None = None) -> None:
        self.connects += 1

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)
        self._current = self.scripts.pop(0)

    async def receive_messages(self) -> AsyncIterator[Any]:
        for step in self._current:
            messages = await step(self) if callable(step) else [step]
            for message in messages:
                yield message
        if not self._current:
            await self.interrupted.wait()
            yield result(stop_reason=None, terminal_reason="aborted_streaming", text=None)

    async def interrupt(self) -> None:
        self.interrupted.set()

    async def disconnect(self) -> None:
        self.disconnected = True

    async def get_mcp_status(self) -> dict[str, Any]:
        if self.status_error is not None:
            raise self.status_error
        return self.mcp_status


class Factory:
    """Starts a fake CLI process for each call. ``scripts`` is the list of queries for the
    process that it starts next."""

    def __init__(self, *processes: list[Sequence[Any]]) -> None:
        self.processes = list(processes)
        self.clients: list[KeptClient] = []

    def __call__(self, options: ClaudeAgentOptions) -> ClaudeClient:
        client = KeptClient(options, self.processes[len(self.clients)])
        self.clients.append(client)
        return cast(ClaudeClient, client)


def ok(text: str = "done") -> list[Any]:
    return [delta(text), result(text=text)]


@pytest.fixture
async def relay() -> AsyncIterator[TokenRelay]:
    relay = TokenRelay("http://127.0.0.1:1")
    await relay.start()
    yield relay
    await relay.aclose()


def make_runtime(
    factory: Factory,
    relay: TokenRelay,
    keeper: ClaudeKeeper,
    gate: ScriptedGate | None = None,
    **settings: Any,
) -> ClaudeRuntime:
    return ClaudeRuntime(
        gate=gate,
        settings=ClaudeSettings(relay=relay, keeper=keeper, **settings),
        client_factory=factory,
    )


def req(
    run_id: str,
    state: bytes | None,
    *,
    text: str = "hello",
    budget: Budget | None = None,
    changed: bool = False,
) -> RunRequest:
    conf = make_config(RuntimeKind.CLAUDE_AGENT_SDK, soul="other") if changed else config()
    return RunRequest(
        scope=make_scope(run_id),
        input=text,
        config=conf,
        runtime_state=state,
        budget=budget or Budget(),
    )


async def finish(runtime: ClaudeRuntime, request: RunRequest) -> RunCompleted:
    events = await collect(runtime.run(request))
    assert isinstance(events[-1], RunCompleted), events[-1]
    return events[-1]


async def test_the_next_run_uses_the_client_of_the_last_run(relay: TokenRelay) -> None:
    keeper = ClaudeKeeper()
    factory = Factory([ok("one"), ok("two")])
    runtime = make_runtime(factory, relay, keeper)
    first = await finish(runtime, req("run-1", None))
    assert keeper.holds
    assert not factory.clients[0].disconnected
    second = await finish(runtime, req("run-2", first.runtime_state))
    assert len(factory.clients) == 1
    assert factory.clients[0].prompts == ["hello", "hello"]
    assert factory.clients[0].connects == 1
    assert second.output == "two"
    await runtime.aclose()
    await keeper.close()
    assert factory.clients[0].disconnected


async def test_a_changed_config_starts_a_new_process(relay: TokenRelay) -> None:
    keeper = ClaudeKeeper()
    factory = Factory([ok()], [ok()])
    runtime = make_runtime(factory, relay, keeper)
    first = await finish(runtime, req("run-1", None))
    await finish(runtime, req("run-2", first.runtime_state, changed=True))
    assert len(factory.clients) == 2
    await keeper.close()
    assert all(client.disconnected for client in factory.clients)


async def test_a_changed_turn_limit_starts_a_new_process(relay: TokenRelay) -> None:
    keeper = ClaudeKeeper()
    factory = Factory([ok()], [ok()])
    runtime = make_runtime(factory, relay, keeper)
    first = await finish(runtime, req("run-1", None, budget=Budget(max_turns=10)))
    await finish(runtime, req("run-2", first.runtime_state, budget=Budget(max_turns=20)))
    assert len(factory.clients) == 2
    assert factory.clients[1].options.max_turns == 20
    await keeper.close()


async def test_a_state_that_the_client_did_not_leave_starts_a_new_process(
    relay: TokenRelay,
) -> None:
    """Another process served the session in between, so its state is not the one of the
    client: the history of the client is old."""
    keeper = ClaudeKeeper()
    factory = Factory([ok()], [ok()])
    runtime = make_runtime(factory, relay, keeper)
    first = await finish(runtime, req("run-1", None))
    other = req("run-2", first.runtime_state)
    assert first.runtime_state is not None
    stale = first.runtime_state.replace(b"run-1", b"run-9")
    await finish(runtime, other.model_copy(update={"runtime_state": stale}))
    assert len(factory.clients) == 2
    assert factory.clients[0].disconnected or await _stopped(factory.clients[0])
    await keeper.close()


async def _stopped(client: KeptClient) -> bool:
    for _ in range(50):
        if client.disconnected:
            return True
        await asyncio.sleep(0.01)
    return False


async def test_an_idle_client_is_not_used_after_the_idle_timeout(relay: TokenRelay) -> None:
    keeper = ClaudeKeeper(idle_s=0.05)
    factory = Factory([ok()], [ok()])
    runtime = make_runtime(factory, relay, keeper)
    first = await finish(runtime, req("run-1", None))
    await asyncio.sleep(0.1)
    await finish(runtime, req("run-2", first.runtime_state))
    assert len(factory.clients) == 2
    assert await _stopped(factory.clients[0])
    await keeper.close()


async def test_a_failed_run_does_not_keep_the_client(relay: TokenRelay) -> None:
    keeper = ClaudeKeeper()
    factory = Factory([[result(is_error=True, subtype="error_during_execution", text=None)]])
    runtime = make_runtime(factory, relay, keeper)
    events = await collect(runtime.run(req("run-1", None)))
    assert isinstance(events[-1], RunFailed)
    assert not keeper.holds
    await runtime.aclose()
    assert factory.clients[0].disconnected


async def test_a_cancelled_run_does_not_keep_the_client(relay: TokenRelay) -> None:
    keeper = ClaudeKeeper()
    factory = Factory([[]])  # no result: the fake waits for the interrupt
    runtime = make_runtime(factory, relay, keeper, cancel_grace_s=1.0)
    stream = runtime.run(req("run-1", None))
    task = asyncio.create_task(collect(stream))
    for _ in range(100):
        if factory.clients and factory.clients[0].prompts:
            break
        await asyncio.sleep(0.01)
    await runtime.cancel("run-1")
    events = await task
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED
    assert not keeper.holds
    await runtime.aclose()
    assert factory.clients[0].disconnected


async def test_a_run_with_a_background_task_does_not_keep_the_client(relay: TokenRelay) -> None:
    keeper = ClaudeKeeper()
    call = ToolUseBlock(
        id="t1", name="Bash", input={"command": "sleep 9", "run_in_background": True}
    )
    factory = Factory([[assistant(call), result()]])
    runtime = make_runtime(factory, relay, keeper)
    await finish(runtime, req("run-1", None))
    assert not keeper.holds
    await runtime.aclose()
    assert factory.clients[0].disconnected


async def test_a_dead_kept_client_is_replaced(relay: TokenRelay) -> None:
    keeper = ClaudeKeeper()
    factory = Factory([ok()], [ok()])
    runtime = make_runtime(factory, relay, keeper)
    first = await finish(runtime, req("run-1", None))
    factory.clients[0].status_error = ConnectionError("the CLI is gone")
    await finish(runtime, req("run-2", first.runtime_state))
    assert len(factory.clients) == 2
    await keeper.close()


async def test_a_client_with_a_failed_mcp_server_is_replaced(relay: TokenRelay) -> None:
    keeper = ClaudeKeeper()
    factory = Factory([ok()], [ok()])
    runtime = make_runtime(factory, relay, keeper)
    first = await finish(runtime, req("run-1", None))
    factory.clients[0].mcp_status = {"mcpServers": [{"name": "sama", "status": "failed"}]}
    await finish(runtime, req("run-2", first.runtime_state))
    assert len(factory.clients) == 2
    await keeper.close()


async def test_the_callbacks_of_a_kept_client_are_those_of_the_active_run(
    relay: TokenRelay,
) -> None:
    keeper = ClaudeKeeper()
    gate = ScriptedGate(GateDecision("allow"))
    factory = Factory(
        [
            [call_hook("mcp__sama__lookup", "call-1"), delta("one"), result(text="one")],
            [call_hook("mcp__sama__lookup", "call-2"), delta("two"), result(text="two")],
        ]
    )
    runtime = make_runtime(factory, relay, keeper, gate)
    first = await finish(runtime, req("run-1", None))
    await finish(runtime, req("run-2", first.runtime_state))
    assert [c[0] for c in gate.calls] == ["mcp__sama__lookup", "mcp__sama__lookup"]
    assert all(
        output["hookSpecificOutput"]["permissionDecision"] == "allow"
        for output in factory.clients[0].hook_outputs
    )
    assert len(factory.clients[0].hook_outputs) == 2


async def test_a_kept_client_denies_every_call_while_no_run_is_active(
    relay: TokenRelay,
) -> None:
    keeper = ClaudeKeeper()
    gate = ScriptedGate(GateDecision("allow"))
    factory = Factory([ok()])
    runtime = make_runtime(factory, relay, keeper, gate)
    await finish(runtime, req("run-1", None))
    options = factory.clients[0].options
    assert options.hooks is not None
    output = await options.hooks["PreToolUse"][0].hooks[0](
        cast(HookInput, {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}}),
        "call-x",
        {"signal": None},
    )
    specific = cast(dict[str, Any], dict(output)["hookSpecificOutput"])
    assert specific["permissionDecision"] == "deny"
    assert gate.calls == []
    assert options.can_use_tool is not None
    denied = await options.can_use_tool("Bash", {}, ToolPermissionContext(tool_use_id="call-x"))
    assert isinstance(denied, PermissionResultDeny)
    await keeper.close()


async def test_a_kept_client_holds_no_run_token_and_no_cost_flag(relay: TokenRelay) -> None:
    """The CLI gets the local secret of the relay. The run token never reaches it."""
    keeper = ClaudeKeeper()
    factory = Factory([ok()])
    runtime = make_runtime(factory, relay, keeper, env={"SAMA_WORKDIR": "/work"})
    request = req("run-1", None, budget=Budget(max_turns=5, max_cost_usd=Decimal(2)))
    request = request.model_copy(
        update={
            "config": request.config.model_copy(
                update={"mcp_servers": make_config_servers("run-token-secret")}
            )
        }
    )
    await finish(runtime, request)
    options = factory.clients[0].options
    assert options.env["ANTHROPIC_API_KEY"] == relay.secret
    assert options.env["ANTHROPIC_BASE_URL"] == f"{relay.url}/anthropic"
    assert options.env["SAMA_RUN_TOKEN"] == ""
    assert options.env["CLAUDE_CODE_SHELL_PREFIX"] == relay.shell_prefix
    servers = cast(dict[str, Any], options.mcp_servers)
    assert servers["sama"]["url"] == relay.mcp_url("sama")
    assert servers["sama"]["headers"] == {"Authorization": f"Bearer {relay.secret}"}
    assert "run-token-secret" not in repr(options)
    assert options.max_turns == 5
    assert options.max_budget_usd is None  # the CLI counts the cost of all runs of its process
    assert options.stderr is not None
    await keeper.close()


async def test_a_run_without_a_keeper_has_the_flags_and_the_token_as_before() -> None:
    factory = Factory([ok()])
    runtime = ClaudeRuntime(
        settings=ClaudeSettings(env={"ANTHROPIC_API_KEY": "run-token"}), client_factory=factory
    )
    await finish(runtime, req("run-1", None, budget=Budget(max_cost_usd=Decimal(2))))
    options = factory.clients[0].options
    assert options.env["ANTHROPIC_API_KEY"] == "run-token"
    assert options.max_budget_usd == pytest.approx(2.0)
    assert options.stderr is None


async def test_a_keeper_needs_a_relay() -> None:
    with pytest.raises(ValueError, match="relay"):
        ClaudeSettings(keeper=ClaudeKeeper())


async def test_close_stops_the_kept_client(relay: TokenRelay) -> None:
    keeper = ClaudeKeeper()
    factory = Factory([ok()], [ok()])
    runtime = make_runtime(factory, relay, keeper)
    first = await finish(runtime, req("run-1", None))
    started = time.monotonic()
    await keeper.close()
    assert factory.clients[0].disconnected
    assert time.monotonic() - started < 1.0
    assert first.runtime_state is not None


def make_config_servers(token: str) -> Any:
    from pydantic import SecretStr

    from sama.runtime import McpServerConfig

    return (McpServerConfig(name="sama", url="http://tools/mcp", bearer_token=SecretStr(token)),)


async def test_a_variable_of_the_sandbox_provider_does_not_change_the_key(
    relay: TokenRelay,
) -> None:
    """E2B gives a resumed sandbox a new ``E2B_TEMPLATE_ID``. The CLI is the same. A change of
    another variable is a change of the CLI."""
    keeper = ClaudeKeeper()
    factory = Factory([ok(), ok()], [ok()])
    first = await finish(
        make_runtime(factory, relay, keeper, env={"E2B_TEMPLATE_ID": "a", "HOME": "/h"}),
        req("run-1", None),
    )
    second = await finish(
        make_runtime(factory, relay, keeper, env={"E2B_TEMPLATE_ID": "b", "HOME": "/h"}),
        req("run-2", first.runtime_state),
    )
    assert len(factory.clients) == 1
    await finish(
        make_runtime(factory, relay, keeper, env={"HOME": "/other"}),
        req("run-3", second.runtime_state),
    )
    assert len(factory.clients) == 2
    await keeper.close()
