"""``browser_task`` on the sama MCP tool server (docs/architecture.md, Section 14.1, M5.6)."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from sama.browser.service import BrowserService
from sama.browser.task import BrowserTaskRunner
from sama.modelproxy.prices import PriceTable
from sama.tenancy import RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from sama.tools.browser_task import BROWSER_TASK
from tests.browser.fakes import DenyHosts, FakeTarget
from tests.browser.test_task import ScriptedModel, done, message, tool_use
from tests.conformance.scripted_model import serve
from tests.support import make_scope
from tests.tools.test_server import connect, text_of


@dataclass
class World:
    url: str
    signer: RunTokenSigner
    target: FakeTarget
    model: ScriptedModel

    def token(self, run_id: str = "run-1") -> str:
        return self.signer.issue(make_scope(run_id), ttl_s=600)


@pytest.fixture
def world() -> Iterator[World]:
    signer = RunTokenSigner(secrets.token_bytes(32))
    target = FakeTarget()
    service = BrowserService(target, DenyHosts(frozenset({"10.0.0.1"})))
    model = ScriptedModel([])
    runner = BrowserTaskRunner(service, model, PriceTable.load(), model="claude-sonnet-5-5")
    server = ToolServer(
        signer=signer, todos=MemoryTodoStore(), browser=service, browser_tasks=runner
    )
    with serve(server.app()) as base:
        yield World(base + "/mcp", signer, target, model)


async def test_the_server_lists_browser_task(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    task = tools[BROWSER_TASK]
    assert task.annotations is not None
    assert task.annotations.destructive_hint
    assert task.input_schema["required"] == ["goal"]
    assert set(task.input_schema["properties"]) == {
        "goal",
        "start_url",
        "max_steps",
        "max_cost_usd",
        "timeout_s",
    }


async def test_a_call_runs_the_sub_agent_with_the_run_token(world: World) -> None:
    world.model.responses.extend(
        [message(tool_use("open", {"url": "example.com"})), done("Example Domain")]
    )
    token = world.token()
    async with connect(world.url, token) as client:
        result = await client.call_tool(
            BROWSER_TASK, {"goal": "Read the title of example.com", "max_steps": 4}
        )
    assert not result.is_error, text_of(result)
    text = text_of(result)
    assert text.startswith("browser_task done. Steps: 2 of 4.")
    assert "Example Domain" in text
    assert world.model.tokens == [token, token]
    assert world.target.sessions[0].calls[0] == "navigate https://example.com"


async def test_a_bad_budget_is_an_error(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool(BROWSER_TASK, {"goal": "g", "max_steps": 99})
    assert result.is_error


async def test_a_server_without_a_browser_has_no_browser_task() -> None:
    signer = RunTokenSigner(secrets.token_bytes(32))
    service = BrowserService(FakeTarget(), DenyHosts(frozenset()))
    runner = BrowserTaskRunner(
        service, ScriptedModel([]), PriceTable.load(), model="claude-sonnet-5-5"
    )
    server = ToolServer(signer=signer, todos=MemoryTodoStore(), browser_tasks=runner)
    with serve(server.app()) as base:
        async with connect(base + "/mcp", signer.issue(make_scope(), ttl_s=600)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
    assert BROWSER_TASK not in names
