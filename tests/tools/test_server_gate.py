"""The gate endpoint and the gate check of the sama tool server (Section 16.1)."""

from __future__ import annotations

import secrets
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from sama.gate import GateStep, Verdict
from sama.gate.wire import GATE_PATH, MAX_GATE_BODY_BYTES
from sama.tenancy import RunTokenClaims, RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from tests.conformance.scripted_model import serve
from tests.support import make_scope
from tests.tools.test_server import connect, text_of


@dataclass
class FakeGate:
    verdict: Verdict = field(default_factory=lambda: Verdict("allow", GateStep.POLICY, "ok"))
    fail: bool = False
    checks: list[tuple[str, str, str, dict[str, Any]]] = field(default_factory=list)
    screens: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def check(
        self, claims: RunTokenClaims, call_id: str, tool_name: str, arguments: Mapping[str, Any]
    ) -> Verdict:
        if self.fail:
            raise RuntimeError("database down")
        self.checks.append((claims.run_id, call_id, tool_name, dict(arguments)))
        return self.verdict

    async def screen(
        self, claims: RunTokenClaims, tool_name: str, arguments: Mapping[str, Any]
    ) -> Verdict:
        self.screens.append((tool_name, dict(arguments)))
        return self.verdict


@dataclass
class Server:
    base: str
    signer: RunTokenSigner
    gate: FakeGate

    def token(self) -> str:
        return self.signer.issue(make_scope("run-7"), ttl_s=600)


@pytest.fixture
def server() -> Iterator[Server]:
    signer = RunTokenSigner(secrets.token_bytes(32))
    gate = FakeGate()
    tools = ToolServer(signer=signer, todos=MemoryTodoStore(), gate=gate)
    with serve(tools.app()) as base:
        yield Server(base, signer, gate)


async def post_gate(
    base: str, token: str | None, body: Any = None, raw: bytes | None = None
) -> httpx.Response:
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as http:
        if raw is not None:
            return await http.post(base + GATE_PATH, headers=headers, content=raw)
        return await http.post(base + GATE_PATH, headers=headers, json=body)


CALL = {"call_id": "toolu_1", "tool_name": "Bash", "arguments": {"command": "ls"}}


async def test_the_gate_returns_the_verdict(server: Server) -> None:
    server.gate.verdict = Verdict("ask", GateStep.POLICY, "Bash needs a human")
    response = await post_gate(server.base, server.token(), CALL)
    assert response.status_code == 200
    assert response.json() == {"action": "ask", "reason": "Bash needs a human", "step": "policy"}
    assert server.gate.checks == [("run-7", "toolu_1", "Bash", {"command": "ls"})]


@pytest.mark.parametrize("token", [None, "nope"])
async def test_the_gate_needs_a_run_token(server: Server, token: str | None) -> None:
    response = await post_gate(server.base, token, CALL)
    assert response.status_code == 401
    assert server.gate.checks == []


@pytest.mark.parametrize(
    "body",
    [
        {"call_id": "", "tool_name": "Bash", "arguments": {}},
        {"tool_name": "Bash"},
        [1],
        {**CALL, "x": 1},
    ],
)
async def test_a_bad_gate_request_gets_422(server: Server, body: Any) -> None:
    assert (await post_gate(server.base, server.token(), body)).status_code == 422


async def test_a_large_gate_request_gets_413(server: Server) -> None:
    raw = b'{"pad": "' + b"x" * MAX_GATE_BODY_BYTES + b'"}'
    assert (await post_gate(server.base, server.token(), raw=raw)).status_code == 413


async def test_a_failed_gate_denies(server: Server) -> None:
    server.gate.fail = True
    response = await post_gate(server.base, server.token(), CALL)
    assert response.status_code == 200
    assert response.json()["action"] == "deny"
    assert "database down" not in response.text


async def test_without_a_gate_the_endpoint_answers_503() -> None:
    signer = RunTokenSigner(secrets.token_bytes(32))
    with serve(ToolServer(signer=signer, todos=MemoryTodoStore()).app()) as base:
        response = await post_gate(base, signer.issue(make_scope(), ttl_s=600), CALL)
    assert response.status_code == 503


async def test_the_server_refuses_a_tool_call_that_the_gate_denies(server: Server) -> None:
    server.gate.verdict = Verdict("deny", GateStep.CONTEXT, "an unattended run cannot ask")
    async with connect(server.base + "/mcp", server.token()) as client:
        result = await client.call_tool("todo", {})
    assert result.is_error
    assert "the tool gate denied the call: an unattended run cannot ask" in text_of(result)
    # The gate gets the arguments as the client sent them, without the defaults of the tool,
    # so the digest is the digest of the hook (and of the ticket).
    assert server.gate.screens == [("todo", {})]


async def test_the_server_runs_a_tool_call_on_ask_or_allow(server: Server) -> None:
    server.gate.verdict = Verdict("ask", GateStep.POLICY, "asked by the hook")
    async with connect(server.base + "/mcp", server.token()) as client:
        result = await client.call_tool("todo", {"todos": [{"id": "1", "content": "Plan"}]})
    assert not result.is_error
    [(name, arguments)] = server.gate.screens
    assert name == "todo"
    assert arguments["todos"][0]["content"] == "Plan"
