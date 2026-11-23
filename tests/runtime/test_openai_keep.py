"""Tests of the parallel MCP connect and of the kept MCP connections of the OpenAI runtime
(docs/architecture.md, Section 4.4)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

import anyio
import pytest

pytest.importorskip("agents")

from sama.runtime import McpServerConfig, RunConfig, RuntimeKind
from sama.runtime.openai_agents import (
    OpenAIKeeper,
    OpenAISettings,
    _mcp_servers,
    _Owned,
    _ServerSet,
)
from sama.runtime.relay import TokenRelay

pytestmark = pytest.mark.asyncio


class FakeServer:
    """An MCP client that enters a cancel scope when it connects, as the SDK does. The scope
    must exit in the task that entered it."""

    def __init__(self, name: str, connect_s: float = 0.0, fail: bool = False) -> None:
        self.name = name
        self.connect_s = connect_s
        self.fail = fail
        self.connected = False
        self.cleaned = False
        self._stack = contextlib.AsyncExitStack()

    async def connect(self) -> None:
        await asyncio.sleep(self.connect_s)
        if self.fail:
            raise ConnectionError("no route")
        await self._stack.enter_async_context(anyio.create_task_group())
        self.connected = True

    async def cleanup(self) -> None:
        await self._stack.aclose()
        self.cleaned = True


def owned(*servers: FakeServer) -> list[_Owned]:
    return [_Owned(cast(Any, server)) for server in servers]


async def test_the_servers_connect_at_the_same_time() -> None:
    servers = [FakeServer(f"s{i}", connect_s=0.2) for i in range(3)]
    group = _ServerSet("key", owned(*servers))
    loop = asyncio.get_running_loop()
    started = loop.time()
    await group.ready()
    assert loop.time() - started < 0.45  # three servers one after the other take 0.6 seconds
    assert all(server.connected for server in servers)
    await group.close()
    assert all(server.cleaned for server in servers)


async def test_a_connection_can_be_closed_by_another_task() -> None:
    """The SDK needs the task that connects to be the task that cleans up. The owner task does
    both, so the task of a later run can close a connection that an earlier run made."""
    server = FakeServer("s")
    group = _ServerSet("key", owned(server))
    await asyncio.create_task(group.ready())
    await asyncio.create_task(group.close())
    assert server.cleaned


async def test_a_failed_connect_raises_and_cleans_up_the_others() -> None:
    good, bad = FakeServer("good"), FakeServer("bad", fail=True)
    group = _ServerSet("key", owned(good, bad))
    with pytest.raises(ConnectionError):
        await group.ready()
    await group.close()
    assert good.cleaned


async def test_a_slow_connect_stops_at_the_limit_and_close_ends_it() -> None:
    slow = FakeServer("slow", connect_s=30)
    group = _ServerSet("key", owned(slow))
    with pytest.raises(TimeoutError):
        await group.ready(timeout_s=0.05)
    await asyncio.wait_for(group.close(), timeout=2)
    assert not slow.connected


async def test_the_keeper_gives_the_set_back_for_the_same_key() -> None:
    keeper = OpenAIKeeper()
    server = FakeServer("s")
    group = _ServerSet("key", owned(server))
    await group.ready()
    keeper.put(group)
    assert keeper.holds
    assert keeper.take("key") is group
    assert not keeper.holds
    assert not server.cleaned
    await group.close()


async def test_the_keeper_closes_the_set_for_another_key() -> None:
    keeper = OpenAIKeeper()
    server = FakeServer("s")
    group = _ServerSet("key", owned(server))
    await group.ready()
    keeper.put(group)
    assert keeper.take("other") is None
    await keeper.close()
    assert server.cleaned


async def test_the_keeper_closes_an_idle_set() -> None:
    keeper = OpenAIKeeper(idle_s=0.05)
    server = FakeServer("s")
    group = _ServerSet("key", owned(server))
    await group.ready()
    keeper.put(group)
    await asyncio.sleep(0.1)
    assert keeper.take("key") is None
    await keeper.close()
    assert server.cleaned


async def test_a_new_set_replaces_the_kept_one() -> None:
    keeper = OpenAIKeeper()
    old, new = FakeServer("old"), FakeServer("new")
    first, second = _ServerSet("a", owned(old)), _ServerSet("b", owned(new))
    await first.ready()
    await second.ready()
    keeper.put(first)
    keeper.put(second)
    await keeper.close()
    assert old.cleaned
    assert new.cleaned


async def test_a_keeper_needs_a_relay() -> None:
    with pytest.raises(ValueError, match="relay"):
        OpenAISettings(keeper=OpenAIKeeper())


async def test_the_clients_of_a_kept_runtime_hold_only_the_local_secret() -> None:
    relay = TokenRelay("http://127.0.0.1:1")
    await relay.start()
    try:
        config = RunConfig(
            runtime=RuntimeKind.OPENAI_AGENTS_SDK,
            model="gpt-6-luna",
            mcp_servers=(McpServerConfig(name="sama", url="http://tools/mcp"),),
        )
        (server,) = _mcp_servers(config, relay)
        params = cast(Any, server).params
        assert params["url"] == relay.mcp_url("sama")
        assert params["headers"] == {"Authorization": f"Bearer {relay.secret}"}
        keeper = OpenAIKeeper()
        client = keeper.client(
            OpenAISettings(model_url="http://m/openai/v1", api_key="run-token", relay=relay)
        )
        assert client.api_key == relay.secret
        assert str(client.base_url).startswith(relay.openai_url)
        assert keeper.client(OpenAISettings(relay=relay)) is client
        await keeper.close()
    finally:
        await relay.aclose()
