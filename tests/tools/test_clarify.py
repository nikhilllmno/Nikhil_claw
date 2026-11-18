"""The clarify broker (docs/architecture.md, Section 16.7)."""

from __future__ import annotations

import asyncio

import pytest

from sama.tenancy import RunTokenClaims
from sama.tools.clarify import (
    MAX_ANSWER_CHARS,
    MAX_OPEN_PER_SESSION,
    ClarifyBroker,
    ClarifyError,
    ClarifyQuestion,
    ClarifyRequest,
    render_answers,
)

QUESTION = ClarifyQuestion(question="Which color?", choices=("Red", "Blue"))


def claims(run: str = "run-1", org: str = "org-1") -> RunTokenClaims:
    return RunTokenClaims(
        org_id=org,
        workspace_id="ws-1",
        session_id="sess-1",
        run_id=run,
        user_id="user-1",
        token_id="t-1",
        expires_at=2_000_000_000,
    )


async def open_request(broker: ClarifyBroker, org: str = "org-1") -> ClarifyRequest:
    for _ in range(100):
        pending = broker.pending(org, "sess-1")
        if pending:
            return pending[-1]
        await asyncio.sleep(0)
    raise AssertionError("no open request")


async def test_an_answer_ends_the_wait() -> None:
    broker = ClarifyBroker()
    asking = asyncio.create_task(broker.ask(claims(), [QUESTION]))
    request = await open_request(broker)
    assert request.questions == (QUESTION,)
    assert request.user_id == "user-1"
    broker.answer("org-1", request.id, [" Blue "])
    assert await asking == ("Blue",)
    assert broker.pending("org-1", "sess-1") == []


async def test_no_answer_in_time_returns_none() -> None:
    broker = ClarifyBroker(wait_s=0.01)
    assert await broker.ask(claims(), [QUESTION]) is None
    assert broker.pending("org-1", "sess-1") == []


async def test_cancel_run_ends_only_that_run() -> None:
    broker = ClarifyBroker()
    first = asyncio.create_task(broker.ask(claims("run-1"), [QUESTION]))
    second = asyncio.create_task(broker.ask(claims("run-2"), [QUESTION]))
    await asyncio.sleep(0)
    broker.cancel_run("org-1", "run-1")
    assert await first is None
    assert not second.done()
    request = await open_request(broker)
    broker.answer("org-1", request.id, ["Red"])
    assert await second == ("Red",)


async def test_another_org_cannot_answer_or_see() -> None:
    broker = ClarifyBroker(wait_s=1)
    asking = asyncio.create_task(broker.ask(claims(), [QUESTION]))
    request = await open_request(broker)
    assert broker.pending("org-2", "sess-1") == []
    with pytest.raises(ClarifyError, match="not open"):
        broker.answer("org-2", request.id, ["Red"])
    broker.cancel_run("org-2", "run-1")
    assert not asking.done()
    broker.cancel_run("org-1", "run-1")
    assert await asking is None


async def test_answer_checks() -> None:
    broker = ClarifyBroker(wait_s=1)
    asking = asyncio.create_task(broker.ask(claims(), [QUESTION]))
    request = await open_request(broker)
    with pytest.raises(ClarifyError, match="give 1 answers"):
        broker.answer("org-1", request.id, ["a", "b"])
    with pytest.raises(ClarifyError, match="1 to"):
        broker.answer("org-1", request.id, ["  "])
    with pytest.raises(ClarifyError, match="1 to"):
        broker.answer("org-1", request.id, ["x" * (MAX_ANSWER_CHARS + 1)])
    with pytest.raises(ClarifyError, match="not open"):
        broker.answer("org-1", "no-such-id", ["Red"])
    broker.answer("org-1", request.id, ["Red"])
    assert await asking == ("Red",)


async def test_question_checks() -> None:
    broker = ClarifyBroker()
    with pytest.raises(ClarifyError, match="1 to 5"):
        await broker.ask(claims(), [])
    with pytest.raises(ClarifyError, match="1 to 5"):
        await broker.ask(claims(), [QUESTION] * 6)
    with pytest.raises(ClarifyError, match="2 or more choices"):
        await broker.ask(claims(), [ClarifyQuestion(question="?", choices=("Only",))])


async def test_the_open_limit_per_session() -> None:
    broker = ClarifyBroker(wait_s=1)
    tasks = [
        asyncio.create_task(broker.ask(claims(f"run-{n}"), [QUESTION]))
        for n in range(MAX_OPEN_PER_SESSION)
    ]
    await asyncio.sleep(0)
    with pytest.raises(ClarifyError, match="too many open"):
        await broker.ask(claims("run-x"), [QUESTION])
    for n in range(MAX_OPEN_PER_SESSION):
        broker.cancel_run("org-1", f"run-{n}")
    assert await asyncio.gather(*tasks) == [None] * MAX_OPEN_PER_SESSION


def test_render_answers() -> None:
    assert render_answers([QUESTION], ("Blue",)) == (
        "The user answered:\n1. Which color?\n   Answer: Blue"
    )
    assert render_answers([QUESTION], None).startswith("The user did not answer.")
