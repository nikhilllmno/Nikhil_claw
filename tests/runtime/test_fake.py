from __future__ import annotations

import asyncio
import json
import time

import pytest

from sama.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    ContractViolation,
    FailureCode,
    FakeRuntime,
    FakeScript,
    ResumeRequest,
    RunCompleted,
    RunEvent,
    RunFailed,
    RuntimeKind,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    Usage,
    validate_event_stream,
)
from tests.support import collect, make_config, make_request, make_scope


def _approval_script(delay_s: float = 0.0) -> FakeScript:
    return FakeScript(
        events=[
            ToolCallStarted(call_id="c1", name="gmail_send", usage=Usage(input_tokens=5)),
            ApprovalRequired(
                approval_id="ap-1",
                call_id="c1",
                tool_name="gmail_send",
                reason="write action",
                usage=Usage(input_tokens=5),
            ),
        ],
        on_approve=[
            ToolCallFinished(call_id="c1", usage=Usage(input_tokens=5)),
            RunCompleted(output="sent", usage=Usage(input_tokens=5, output_tokens=1)),
        ],
        on_reject=[RunCompleted(output="not sent", usage=Usage(input_tokens=5, output_tokens=2))],
        delay_s=delay_s,
    )


def _resume(
    decision: ApprovalDecision, approval_id: str = "ap-1", run_id: str = "run-1"
) -> ResumeRequest:
    return ResumeRequest(
        scope=make_scope(run_id),
        config=make_config(),
        approval_id=approval_id,
        decision=decision,
    )


# --- echo --------------------------------------------------------------------------------------


async def test_echo_streams_text_and_ends_with_one_terminal_event() -> None:
    events = await collect(FakeRuntime().run(make_request("hello world")))
    terminal = validate_event_stream(events)
    assert isinstance(terminal, RunCompleted)
    assert terminal.output == "echo: hello world"
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == terminal.output
    assert terminal.usage == Usage(input_tokens=2, output_tokens=3)


async def test_echo_keeps_session_state_between_runs() -> None:
    runtime = FakeRuntime()
    first = (await collect(runtime.run(make_request(run_id="r1"))))[-1]
    assert isinstance(first, RunCompleted)
    assert first.runtime_state is not None
    second = (
        await collect(runtime.run(make_request(run_id="r2", runtime_state=first.runtime_state)))
    )[-1]
    assert isinstance(second, RunCompleted)
    assert second.runtime_state is not None
    assert json.loads(second.runtime_state) == {"turns": 2}


# --- scripts -----------------------------------------------------------------------------------


async def test_scripts_replay_in_order_then_echo() -> None:
    one = FakeScript(events=[RunCompleted(output="one")])
    two = FakeScript(events=[RunFailed(code=FailureCode.MODEL_ERROR, message="boom")])
    runtime = FakeRuntime(scripts=[one, two])
    assert (await collect(runtime.run(make_request(run_id="a")))) == [RunCompleted(output="one")]
    assert (await collect(runtime.run(make_request(run_id="b"))))[-1] == RunFailed(
        code=FailureCode.MODEL_ERROR, message="boom"
    )
    last = (await collect(runtime.run(make_request("x", run_id="c"))))[-1]
    assert isinstance(last, RunCompleted)
    assert last.output == "echo: x"
    assert len(runtime.requests) == 3


async def test_add_script() -> None:
    runtime = FakeRuntime()
    runtime.add_script(FakeScript(events=[RunCompleted(output="added")]))
    assert (await collect(runtime.run(make_request()))) == [RunCompleted(output="added")]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"events": [TextDelta(text="x")]}, "exactly one terminal"),
        (
            {"events": [RunCompleted(output="x")], "on_approve": [RunCompleted(output="y")]},
            "need events",
        ),
        (
            {
                "events": [ApprovalRequired(approval_id="a", call_id="c", tool_name="t")],
                "on_approve": [ApprovalRequired(approval_id="b", call_id="c", tool_name="t")],
                "on_reject": [RunCompleted(output="n")],
            },
            "on_approve must end",
        ),
        (
            {
                "events": [ApprovalRequired(approval_id="a", call_id="c", tool_name="t")],
                "on_approve": [RunCompleted(output="y")],
            },
            "no events",
        ),
        ({"events": [RunCompleted(output="x")], "delay_s": -1}, "negative"),
    ],
)
def test_bad_scripts_are_rejected(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises((ValueError, ContractViolation), match=message):
        FakeScript(**kwargs)  # type: ignore[arg-type]


async def test_wrong_runtime_in_config_fails() -> None:
    request = make_request(config=make_config(RuntimeKind.CLAUDE_AGENT_SDK))
    events = await collect(FakeRuntime().run(request))
    assert len(events) == 1
    assert isinstance(events[0], RunFailed)
    assert events[0].code is FailureCode.INVALID_REQUEST


# --- approvals ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "output"),
    [(ApprovalDecision.APPROVE, "sent"), (ApprovalDecision.REJECT, "not sent")],
)
async def test_approval_pauses_and_resumes(decision: ApprovalDecision, output: str) -> None:
    runtime = FakeRuntime(scripts=[_approval_script()])
    paused = await collect(runtime.run(make_request()))
    terminal = validate_event_stream(paused)
    assert isinstance(terminal, ApprovalRequired)
    assert terminal.approval_id == "ap-1"

    resumed = await collect(runtime.resume(_resume(decision)))
    final = validate_event_stream(resumed)
    assert isinstance(final, RunCompleted)
    assert final.output == output


async def test_an_approval_resumes_only_once() -> None:
    runtime = FakeRuntime(scripts=[_approval_script()])
    await collect(runtime.run(make_request()))
    await collect(runtime.resume(_resume(ApprovalDecision.APPROVE)))
    again = await collect(runtime.resume(_resume(ApprovalDecision.APPROVE)))
    assert isinstance(again[-1], RunFailed)
    assert again[-1].code is FailureCode.INVALID_REQUEST


async def test_resume_rejects_an_unknown_approval_or_another_run() -> None:
    runtime = FakeRuntime(scripts=[_approval_script()])
    await collect(runtime.run(make_request()))
    unknown = await collect(runtime.resume(_resume(ApprovalDecision.APPROVE, approval_id="nope")))
    other_run = await collect(runtime.resume(_resume(ApprovalDecision.APPROVE, run_id="run-2")))
    for events in (unknown, other_run):
        assert len(events) == 1
        assert isinstance(events[0], RunFailed)
        assert events[0].code is FailureCode.INVALID_REQUEST
    # The pending approval is still there for the right run.
    ok = await collect(runtime.resume(_resume(ApprovalDecision.APPROVE)))
    assert isinstance(ok[-1], RunCompleted)


# --- cancel ------------------------------------------------------------------------------------


async def test_cancel_ends_the_stream_with_cancelled() -> None:
    events: list[RunEvent] = [
        TextDelta(text=str(i), usage=Usage(output_tokens=i)) for i in range(100)
    ]
    script = FakeScript(
        events=[*events, RunCompleted(output="never", usage=Usage(output_tokens=100))], delay_s=1.0
    )
    runtime = FakeRuntime(scripts=[script])
    request = make_request()
    seen: list[RunEvent] = []

    async def consume() -> None:
        async for event in runtime.run(request):
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let the run start
    started = time.monotonic()
    await runtime.cancel(request.scope.run_id)
    await asyncio.wait_for(task, timeout=10)
    assert time.monotonic() - started < 10
    terminal = validate_event_stream(seen)
    assert isinstance(terminal, RunFailed)
    assert terminal.code is FailureCode.CANCELLED


async def test_cancel_of_an_unknown_run_does_nothing() -> None:
    await FakeRuntime().cancel("missing")


async def test_the_same_run_cannot_stream_twice_at_once() -> None:
    script = FakeScript(events=[TextDelta(text="a"), RunCompleted(output="a")], delay_s=0.05)
    runtime = FakeRuntime(scripts=[script])
    first = runtime.run(make_request())
    first_event = await anext(first)
    second = await collect(runtime.run(make_request()))
    assert isinstance(second[-1], RunFailed)
    assert second[-1].code is FailureCode.INVALID_REQUEST
    rest = [first_event, *[e async for e in first]]
    assert isinstance(validate_event_stream(rest), RunCompleted)


# --- lifecycle edge cases ----------------------------------------------------------------------


async def test_resume_works_before_the_paused_stream_is_closed() -> None:
    runtime = FakeRuntime(scripts=[_approval_script()])
    paused = runtime.run(make_request())
    events = [await anext(paused), await anext(paused)]
    assert isinstance(events[-1], ApprovalRequired)
    # The caller resumes at once and does not advance or close the paused stream first.
    resumed = await collect(runtime.resume(_resume(ApprovalDecision.APPROVE)))
    assert isinstance(validate_event_stream(resumed, start=events[-1].usage), RunCompleted)
    assert [e async for e in paused] == []  # the paused stream ends cleanly


async def test_a_rejected_duplicate_run_does_not_use_a_script() -> None:
    slow = FakeScript(events=[TextDelta(text="a"), RunCompleted(output="a")], delay_s=0.05)
    nxt = FakeScript(events=[RunCompleted(output="next")])
    runtime = FakeRuntime(scripts=[slow, nxt])
    first = runtime.run(make_request())
    await anext(first)
    duplicate = await collect(runtime.run(make_request()))
    assert isinstance(duplicate[-1], RunFailed)
    assert isinstance([e async for e in first][-1], RunCompleted)
    assert await collect(runtime.run(make_request(run_id="r2"))) == [RunCompleted(output="next")]


async def test_usage_does_not_go_down_across_a_resume() -> None:
    runtime = FakeRuntime(scripts=[_approval_script()])
    paused = await collect(runtime.run(make_request()))
    resumed = await collect(runtime.resume(_resume(ApprovalDecision.APPROVE)))
    validate_event_stream(resumed, start=paused[-1].usage)


def test_a_branch_with_lower_usage_than_the_pause_is_rejected() -> None:
    pause = ApprovalRequired(
        approval_id="a", call_id="c", tool_name="t", usage=Usage(input_tokens=5)
    )
    with pytest.raises(ContractViolation, match="usage went down"):
        FakeScript(
            events=[pause],
            on_approve=[RunCompleted(output="y")],
            on_reject=[RunCompleted(output="n", usage=Usage(input_tokens=5))],
        )


async def test_cancel_during_a_resume_reports_the_usage_so_far() -> None:
    runtime = FakeRuntime(scripts=[_approval_script(delay_s=0.01)])
    request = make_request()
    paused = await collect(runtime.run(request))
    resume_task = asyncio.create_task(collect(runtime.resume(_resume(ApprovalDecision.APPROVE))))
    await asyncio.sleep(0)  # let the resume start, then cancel before its first event
    await runtime.cancel(request.scope.run_id)
    resumed = await asyncio.wait_for(resume_task, timeout=10)
    terminal = validate_event_stream(resumed, start=paused[-1].usage)
    assert isinstance(terminal, RunFailed)
    assert terminal.code is FailureCode.CANCELLED
    assert terminal.usage == paused[-1].usage


async def test_cancel_of_a_paused_run_makes_its_approval_invalid() -> None:
    runtime = FakeRuntime(scripts=[_approval_script()])
    await collect(runtime.run(make_request()))
    await runtime.cancel("run-1")
    events = await collect(runtime.resume(_resume(ApprovalDecision.APPROVE)))
    assert len(events) == 1
    assert isinstance(events[0], RunFailed)
    assert events[0].code is FailureCode.INVALID_REQUEST
