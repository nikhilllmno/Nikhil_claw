"""``POST /gate/ask``: the questions of the native ``AskUserQuestion`` tool (M8.3, Section 16.7)."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from sama.gate import GateStep, Verdict
from sama.gate.wire import ASK_PATH
from sama.tenancy import RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from sama.tools.clarify import (
    ClarifyBroker,
    ClarifyError,
    NativeQuestion,
    native_answers,
    native_questions,
)
from tests.conformance.scripted_model import serve
from tests.support import make_scope
from tests.tools.test_server_gate import FakeGate

INPUT: dict[str, Any] = {
    "questions": [
        {
            "question": "Which dates?",
            "header": "Dates",
            "options": [
                {"label": "Tonight", "description": "1 night"},
                {"label": "Weekend", "description": "Fri to Sun"},
            ],
            "multiSelect": False,
        },
        {
            "question": "Which extras?",
            "header": "Extras",
            "options": [{"label": "Parking"}, {"label": "Breakfast", "description": "7 to 10"}],
            "multiSelect": True,
        },
    ]
}


def test_native_questions_keep_the_fields_of_claude_code() -> None:
    dates, extras = native_questions(INPUT)
    assert isinstance(dates, NativeQuestion)
    assert (dates.question, dates.header, dates.choices) == (
        "Which dates?",
        "Dates",
        ("Tonight", "Weekend"),
    )
    assert dates.descriptions == ("1 night", "Fri to Sun")
    assert not dates.multi_select
    assert extras.multi_select
    assert extras.descriptions == ("", "7 to 10")
    assert native_answers([dates, extras], ["Weekend", "Parking, Breakfast"]) == {
        "Which dates?": "Weekend",
        "Which extras?": "Parking, Breakfast",
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"questions": []},
        {"questions": ["no"]},
        {"questions": [{"question": "Q", "options": [{"label": "only one"}]}]},
        {"questions": [{"question": "", "options": [{"label": "a"}, {"label": "b"}]}]},
        {"questions": [{"question": "Q", "options": [{"label": "a"}, {"nolabel": 1}]}]},
        {
            "questions": [
                {"question": "Q", "options": [{"label": "a"}, {"label": "b"}]},
                {"question": "Q", "options": [{"label": "a"}, {"label": "b"}]},
            ]
        },
    ],
)
def test_native_questions_refuse_input_that_does_not_fit(arguments: dict[str, Any]) -> None:
    with pytest.raises(ClarifyError):
        native_questions(arguments)


@dataclass
class Server:
    base: str
    signer: RunTokenSigner
    gate: FakeGate
    broker: ClarifyBroker

    def token(self) -> str:
        return self.signer.issue(make_scope("run-7"), ttl_s=600)


@pytest.fixture
def server() -> Iterator[Server]:
    signer = RunTokenSigner(secrets.token_bytes(32))
    gate = FakeGate()
    broker = ClarifyBroker(wait_s=5)
    tools = ToolServer(signer=signer, todos=MemoryTodoStore(), gate=gate, clarify=broker)
    with serve(tools.app()) as base:
        yield Server(base, signer, gate, broker)


async def post_ask(server: Server, body: Any, token: str | None = "") -> httpx.Response:
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token or server.token()}"
    async with httpx.AsyncClient(timeout=10) as http:
        return await http.post(server.base + ASK_PATH, headers=headers, json=body)


async def answer_when_open(broker: ClarifyBroker, answers: list[str]) -> None:
    claims = make_scope("run-7")
    for _ in range(400):
        pending = broker.pending(claims.org_id, claims.session_id)
        if pending:
            broker.answer(claims.org_id, pending[0].id, answers)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no question")


async def test_the_answers_come_back_by_question_text(server: Server) -> None:
    asking = asyncio.create_task(post_ask(server, {"call_id": "q1", "arguments": INPUT}))
    await answer_when_open(server.broker, ["Weekend", "Parking, Breakfast"])
    response = await asking
    assert response.status_code == 200
    assert response.json() == {
        "answers": {"Which dates?": "Weekend", "Which extras?": "Parking, Breakfast"},
        "error": "",
    }
    [(run_id, call_id, tool, arguments)] = server.gate.checks
    assert (run_id, call_id, tool, arguments) == ("run-7", "q1", "AskUserQuestion", INPUT)


async def test_no_answer_in_time_gives_null(server: Server) -> None:
    server.broker.wait_s = 0.05
    response = await post_ask(server, {"call_id": "q1", "arguments": INPUT})
    assert response.status_code == 200
    assert response.json()["answers"] is None


async def test_the_gate_must_allow_the_question(server: Server) -> None:
    server.gate.verdict = Verdict("deny", GateStep.CONTEXT, "no human in this run")
    response = await post_ask(server, {"call_id": "q1", "arguments": INPUT})
    assert response.status_code == 403
    assert response.json()["error"] == "no human in this run"
    assert server.broker.pending("org-1", "sess-1") == []


async def test_a_question_needs_a_token_and_a_valid_body(server: Server) -> None:
    assert (await post_ask(server, {"call_id": "q1", "arguments": INPUT}, None)).status_code == 401
    bad = await post_ask(server, {"call_id": "q1", "arguments": {"questions": []}})
    assert bad.status_code == 422
    assert "ask 1 to 4 questions" in bad.json()["error"]
    assert server.gate.checks == []
