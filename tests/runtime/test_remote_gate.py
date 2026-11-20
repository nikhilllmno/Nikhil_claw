"""The remote tool gate of the runner (docs/architecture.md, Section 16.1)."""

from __future__ import annotations

import json

import httpx
import pytest

from sama.gate.wire import GateRequest
from tests.support import make_scope

pytest.importorskip("claude_agent_sdk")

from sama.runtime.remote_gate import RemoteGate

TOKEN = "run-token-value"


def gate_with(handler: httpx.MockTransport) -> RemoteGate:
    return RemoteGate("http://tools.test/gate", TOKEN, transport=handler)


async def test_the_gate_posts_the_call_with_the_run_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"action": "ask", "reason": "needs a human", "step": "policy"}
        )

    gate = gate_with(httpx.MockTransport(handler))
    decision = await gate.check(make_scope(), "Bash", {"command": "ls"}, call_id="toolu_1")
    assert decision.action == "ask"
    assert decision.reason == "needs a human"
    [request] = seen
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    body = GateRequest.model_validate(json.loads(request.content))
    assert (body.call_id, body.tool_name, body.arguments) == ("toolu_1", "Bash", {"command": "ls"})


async def test_the_child_flag_goes_to_the_control_plane() -> None:
    seen: list[GateRequest] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(GateRequest.model_validate(json.loads(request.content)))
        return httpx.Response(200, json={"action": "deny", "reason": "no", "step": "policy"})

    gate = gate_with(httpx.MockTransport(handler))
    await gate.check(make_scope(), "Bash", {}, call_id="toolu_1")
    await gate.check(make_scope(), "Bash", {}, call_id="toolu_2", child=True)
    assert [request.child for request in seen] == [False, True]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"error": "no gate"}),
        httpx.Response(302, headers={"location": "http://elsewhere.test/"}),
        httpx.Response(200, json={"action": "maybe"}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_a_bad_answer_raises(response: httpx.Response) -> None:
    gate = gate_with(httpx.MockTransport(lambda request: response))
    with pytest.raises((httpx.HTTPError, ValueError)):
        await gate.check(make_scope(), "Bash", {}, call_id="toolu_1")


def test_the_token_is_not_in_the_repr() -> None:
    assert TOKEN not in repr(RemoteGate("http://tools.test/gate", TOKEN))
