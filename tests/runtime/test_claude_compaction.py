"""The compaction parts of the Claude adapter: ``/compact`` and ``PreCompact`` (Section 6.3)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import HookInput, SystemMessage, ToolUseBlock
from claude_agent_sdk.types import DeferredToolUse

from sama.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    Compacted,
    FailureCode,
    RunCompleted,
    RunFailed,
    RunRequest,
)
from sama.runtime.archive import Archiver, TranscriptArchive
from sama.runtime.claude import ClaudeRuntime, GateDecision
from sama.runtime.contract import CompactRequest, RunScope
from tests.runtime.test_claude import (
    SESSION,
    Factory,
    FakeClient,
    ScriptedGate,
    assistant,
    call_hook,
    config,
    delta,
    request,
    result,
    resume_request,
)
from tests.support import collect, make_scope


class Recorder:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.archives: list[tuple[RunScope, TranscriptArchive]] = []

    async def archive(self, scope: RunScope, archive: TranscriptArchive) -> None:
        self.archives.append((scope, archive))
        if self.error is not None:
            raise self.error


async def session_state() -> bytes:
    """The runtime state of a finished chat run, as the worker stores it."""
    done = (await collect(ClaudeRuntime(client_factory=Factory([result()])).run(chat())))[-1]
    assert isinstance(done, RunCompleted)
    assert done.runtime_state is not None
    return done.runtime_state


def chat() -> RunRequest:
    return RunRequest(scope=make_scope(), input="hi", config=config())


STATE = asyncio.run(session_state())


def compact_request(focus: str = "", state: bytes | None = STATE) -> RunRequest:
    return RunRequest(
        scope=make_scope(),
        input="",
        config=config(),
        runtime_state=state,
        compact=CompactRequest(focus=focus),
    )


def pre_compact(path: str, trigger: str = "manual") -> Callable[[FakeClient], Any]:
    """A step that runs the PreCompact hook of the options, like the CLI does."""

    async def step(client: FakeClient) -> list[Any]:
        matcher = (client.options.hooks or {})["PreCompact"][0]
        data: dict[str, Any] = {
            "hook_event_name": "PreCompact",
            "trigger": trigger,
            "custom_instructions": None,
            "transcript_path": path,
            "session_id": SESSION,
            "cwd": "/",
        }
        output = await matcher.hooks[0](cast(HookInput, data), None, {"signal": None})
        client.hook_outputs.append(dict(output))
        return []

    return step


def boundary(trigger: str) -> SystemMessage:
    return SystemMessage(
        subtype="compact_boundary",
        data={"compact_metadata": {"trigger": trigger, "pre_tokens": 900, "post_tokens": 100}},
    )


def transcript(tmp_path: Path) -> str:
    path = tmp_path / "s.jsonl"
    path.write_text(
        json.dumps({"type": "user", "message": {"content": "remember 42"}}), encoding="utf-8"
    )
    return str(path)


def test_the_capabilities_name_the_compaction_features() -> None:
    caps = ClaudeRuntime(client_factory=Factory()).capabilities()
    assert caps.manual_compaction
    assert caps.compaction_archive


async def test_a_compact_request_sends_the_slash_command() -> None:
    factory = Factory([boundary("manual"), result(text="")])
    events = await collect(
        ClaudeRuntime(client_factory=factory).run(compact_request("keep the plan"))
    )
    assert factory.clients[0].prompts == ["/compact keep the plan"]
    assert factory.clients[0].options.resume == SESSION
    assert isinstance(events[-1], RunCompleted)


async def test_a_compact_request_without_a_focus_sends_a_bare_command() -> None:
    factory = Factory([result(text="")])
    await collect(ClaudeRuntime(client_factory=factory).run(compact_request()))
    assert factory.clients[0].prompts == ["/compact"]


async def test_a_compact_request_without_state_is_invalid() -> None:
    first = Factory([result()])
    runtime = ClaudeRuntime(client_factory=first)
    events = await collect(runtime.run(compact_request(state=None)))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INVALID_REQUEST
    assert "nothing to compact" in events[-1].message
    assert first.clients == []


async def test_a_compact_run_without_a_boundary_fails() -> None:
    """The CLI can answer ``/compact`` with a normal result and an error text (for example an
    HTTP 400 on the summary call). Nothing was compacted, so the run must not complete."""
    factory = Factory([delta("API Error: 400 summary failed"), result(text="API Error: 400")])
    events = await collect(ClaudeRuntime(client_factory=factory).run(compact_request()))
    done = events[-1]
    assert isinstance(done, RunFailed)
    assert done.code is FailureCode.MODEL_ERROR
    assert "compaction did not finish" in done.message
    assert "no compaction boundary" in done.message
    assert not any(isinstance(e, Compacted) for e in events)
    assert not any(isinstance(e, RunCompleted) for e in events)


async def test_a_failed_compact_keeps_its_attempted_archive(tmp_path: Path) -> None:
    """The archive is made before the compaction. A compaction that fails leaves it as the
    record of the attempt: the hook ran, and the run failed."""
    recorder = Recorder()
    factory = Factory([pre_compact(transcript(tmp_path)), result(text="API Error: 400")])
    runtime = ClaudeRuntime(client_factory=factory, archiver=cast(Archiver, recorder))
    events = await collect(runtime.run(compact_request()))
    assert isinstance(events[-1], RunFailed)
    assert len(recorder.archives) == 1


async def test_a_chat_run_does_not_need_a_boundary() -> None:
    factory = Factory([result()])
    events = await collect(ClaudeRuntime(client_factory=factory).run(chat()))
    assert isinstance(events[-1], RunCompleted)


# -- the tokens of a local command -------------------------------------------------------------


def models(
    input_tokens: int, output_tokens: int, cache_read: int, cache_write: int
) -> dict[str, Any]:
    return {
        "claude-haiku-4-5": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cacheReadInputTokens": cache_read,
            "cacheCreationInputTokens": cache_write,
            "costUSD": 0.01,
        }
    }


async def counted_state() -> bytes:
    """The state of a chat run whose result has the tokens in ``usage`` and ``model_usage``."""
    first = result(input_tokens=10, output_tokens=5, model_usage=models(10, 5, 2, 1))
    done = (await collect(ClaudeRuntime(client_factory=Factory([first])).run(chat())))[-1]
    assert isinstance(done, RunCompleted)
    assert done.runtime_state is not None
    return done.runtime_state


async def test_a_compact_run_counts_the_tokens_of_model_usage() -> None:
    """For ``/compact`` the CLI reports zero tokens in ``usage``, but the cost and the
    ``model_usage`` of the SDK session grow. The adapter counts the difference."""
    state = await counted_state()
    zero = result(
        input_tokens=0,
        output_tokens=0,
        cache_read=0,
        cache_write=0,
        model_usage=models(40, 9, 6, 3),
        cost=0.05,
    )
    factory = Factory([boundary("manual"), zero])
    events = await collect(ClaudeRuntime(client_factory=factory).run(compact_request(state=state)))
    done = events[-1]
    assert isinstance(done, RunCompleted)
    assert (done.usage.input_tokens, done.usage.output_tokens) == (30, 4)
    assert (done.usage.cache_read_tokens, done.usage.cache_write_tokens) == (4, 2)
    assert done.usage.cost_usd == Decimal("0.04")
    # The next run counts from the new total.
    assert done.runtime_state is not None
    again = result(
        input_tokens=0, output_tokens=0, cache_read=0, cache_write=0,
        model_usage=models(50, 12, 6, 3), cost=0.06,
    )  # fmt: skip
    follow = Factory([boundary("manual"), again])
    events = await collect(
        ClaudeRuntime(client_factory=follow).run(compact_request(state=done.runtime_state))
    )
    assert isinstance(events[-1], RunCompleted)
    assert (events[-1].usage.input_tokens, events[-1].usage.output_tokens) == (10, 3)


async def test_a_normal_turn_is_not_counted_two_times() -> None:
    """A turn has its tokens in ``usage``. ``model_usage`` only gives the baseline."""
    state = await counted_state()
    turn = result(input_tokens=10, output_tokens=5, model_usage=models(20, 10, 4, 2), cost=0.02)
    events = await collect(
        ClaudeRuntime(client_factory=Factory([turn])).run(
            RunRequest(scope=make_scope(), input="more", config=config(), runtime_state=state)
        )
    )
    done = events[-1]
    assert isinstance(done, RunCompleted)
    assert (done.usage.input_tokens, done.usage.output_tokens) == (10, 5)
    assert (done.usage.cache_read_tokens, done.usage.cache_write_tokens) == (2, 1)


async def test_a_state_without_a_baseline_does_not_count_the_whole_session() -> None:
    """A state of an older version has no ``sdk_tokens``. The first difference is not known,
    so the run counts no tokens from ``model_usage``. It does not count the whole session."""
    state = json.loads((await counted_state()).decode())
    del state["sdk_tokens"]
    zero = result(
        input_tokens=0, output_tokens=0, cache_read=0, cache_write=0,
        model_usage=models(40, 9, 6, 3), cost=0.05,
    )  # fmt: skip
    events = await collect(
        ClaudeRuntime(client_factory=Factory([boundary("manual"), zero])).run(
            compact_request(state=json.dumps(state).encode())
        )
    )
    done = events[-1]
    assert isinstance(done, RunCompleted)
    assert done.usage.total_tokens == 0
    # The new state has the baseline for the next run.
    assert done.runtime_state is not None
    assert json.loads(done.runtime_state)["sdk_tokens"]["input_tokens"] == 40


# -- the count after a run that failed ---------------------------------------------------------


def zero_result(tokens: tuple[int, int, int, int], cost: float) -> Any:
    return result(
        input_tokens=0, output_tokens=0, cache_read=0, cache_write=0,
        model_usage=models(*tokens), cost=cost,
    )  # fmt: skip


async def test_a_failed_compaction_does_not_count_its_tokens_two_times() -> None:
    """Baseline 10 tokens. A compaction fails after the model call (no boundary) and the
    session total is 20. The next compaction ends at 50. It counts 30, not 40: the failed run
    has the first 10 in its own usage."""
    state = await counted_state()
    failed_run = Factory([zero_result((20, 8, 4, 2), 0.03)])
    failed = (
        await collect(ClaudeRuntime(client_factory=failed_run).run(compact_request(state=state)))
    )[-1]
    assert isinstance(failed, RunFailed)
    assert failed.usage.input_tokens == 10
    assert failed.runtime_state is not None
    # The conversation does not change: the failed state has the session of the base.
    saved = json.loads(failed.runtime_state)
    assert saved["session_id"] == json.loads(state)["session_id"]
    assert saved["pending"] is None
    assert saved["sdk_tokens"]["input_tokens"] == 20
    follow = Factory([boundary("manual"), zero_result((50, 12, 6, 3), 0.06)])
    done = (
        await collect(
            ClaudeRuntime(client_factory=follow).run(compact_request(state=failed.runtime_state))
        )
    )[-1]
    assert isinstance(done, RunCompleted)
    assert (done.usage.input_tokens, done.usage.output_tokens) == (30, 4)
    # The cost has the same rule: 0.06 - 0.03, not 0.06 - 0.01.
    assert done.usage.cost_usd == Decimal("0.03")


async def test_a_run_that_failed_after_a_pause_keeps_the_count_too() -> None:
    """A paused run has the session of the last completed run as its base. If it fails after
    the resume, the count of that session still moves on."""
    state = await counted_state()
    call = "mcp__sama__gmail_send"
    ask = result(
        stop_reason="tool_deferred",
        text=None,
        deferred=DeferredToolUse(id="call-1", name=call, input={"to": "a@b.c"}),
        input_tokens=0, output_tokens=0, cache_read=0, cache_write=0,
        model_usage=models(20, 8, 4, 2), cost=0.03,
    )  # fmt: skip
    steps = [
        assistant(ToolUseBlock(id="call-1", name=call, input={"to": "a@b.c"})),
        call_hook(call, "call-1"),
        ask,
    ]
    gate = ScriptedGate(GateDecision("ask", "write action"))
    runtime = ClaudeRuntime(gate=gate, client_factory=Factory(steps))
    paused = (await collect(runtime.run(request("go", state=state))))[-1]
    assert isinstance(paused, ApprovalRequired)
    assert paused.runtime_state is not None
    assert json.loads(paused.runtime_state)["base_session_id"] == json.loads(state)["session_id"]
    broken = result(
        is_error=True, subtype="error_during_execution", api_error_status=500,
        input_tokens=0, output_tokens=0, cache_read=0, cache_write=0,
        model_usage=models(30, 9, 5, 2), cost=0.05,
    )  # fmt: skip
    resumed = ClaudeRuntime(gate=gate, client_factory=Factory([broken]))
    failed = (await collect(resumed.resume(resume_request(paused, ApprovalDecision.APPROVE))))[-1]
    assert isinstance(failed, RunFailed)
    assert failed.runtime_state is not None
    saved = json.loads(failed.runtime_state)
    assert saved["sdk_tokens"]["input_tokens"] == 30
    assert saved["session_id"] == json.loads(state)["session_id"]


async def test_the_state_of_a_completed_run_has_no_base() -> None:
    assert json.loads(await counted_state())["base_session_id"] is None


async def test_a_first_run_that_fails_leaves_no_state() -> None:
    """There is no completed run to continue, so the next run starts a new SDK session."""
    broken = result(is_error=True, subtype="error_during_execution", api_error_status=500)
    events = await collect(ClaudeRuntime(client_factory=Factory([broken])).run(chat()))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].runtime_state is None


async def test_a_run_on_another_session_leaves_no_state() -> None:
    """The count is for the SDK session of the base. A run that moved to another session
    keeps nothing: the next run uses the state of the last completed run."""
    state = await counted_state()
    other = replace(zero_result((20, 8, 4, 2), 0.03), session_id="another-session")
    events = await collect(
        ClaudeRuntime(client_factory=Factory([other])).run(compact_request(state=state))
    )
    assert isinstance(events[-1], RunFailed)
    assert events[-1].runtime_state is None


async def test_a_result_without_model_usage_changes_nothing() -> None:
    state = await counted_state()
    zero = result(input_tokens=0, output_tokens=0, cache_read=0, cache_write=0)
    events = await collect(
        ClaudeRuntime(client_factory=Factory([boundary("manual"), zero])).run(
            compact_request(state=state)
        )
    )
    done = events[-1]
    assert isinstance(done, RunCompleted)
    assert done.usage.total_tokens == 0


async def test_a_large_transcript_goes_out_whole_with_the_omitted_count(tmp_path: Path) -> None:
    """The hook does not cut a tool input or a text. It leaves whole entries out, and it says
    how many (``omitted``), so the archive row can show ``archive_truncated``."""
    from sama.runtime.archive import MAX_WIRE_ENTRIES

    big = {"data": 'x"y' * 20_000, "secret": 'pa"ss\\word\nline'}
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "W", "input": big}]},
            }
        )
    ] + [
        json.dumps({"type": "user", "message": {"content": f"m{n}"}})
        for n in range(MAX_WIRE_ENTRIES + 50)
    ]
    path = tmp_path / "big.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")
    recorder = Recorder()
    factory = Factory([pre_compact(str(path)), boundary("manual"), result(text="")])
    runtime = ClaudeRuntime(client_factory=factory, archiver=cast(Archiver, recorder))
    await collect(runtime.run(compact_request()))
    ((_, archive),) = recorder.archives
    assert len(archive.entries) <= MAX_WIRE_ENTRIES
    assert archive.omitted > 0
    assert any(e.role == "system" and "not in this archive" in e.text for e in archive.entries)
    # The last entries stay. The first entry (a tool call) was in the 30% of the head.
    assert archive.entries[-1].text == f"m{MAX_WIRE_ENTRIES + 49}"
    call = archive.entries[0]
    assert call.tool_name == "W"
    assert call.tool_input == big


async def test_the_trigger_of_the_boundary_is_in_the_compacted_event() -> None:
    factory = Factory([boundary("manual"), result(text="")])
    events = await collect(ClaudeRuntime(client_factory=factory).run(compact_request()))
    (compacted,) = [e for e in events if isinstance(e, Compacted)]
    assert compacted.trigger == "manual"
    factory = Factory([boundary("auto"), result()])
    events = await collect(ClaudeRuntime(client_factory=factory).run(compact_request()))
    (compacted,) = [e for e in events if isinstance(e, Compacted)]
    assert compacted.trigger == "auto"


async def test_without_an_archiver_there_is_no_pre_compact_hook() -> None:
    factory = Factory([result()])
    await collect(ClaudeRuntime(client_factory=factory).run(compact_request()))
    assert set(factory.clients[0].options.hooks or {}) == {"PreToolUse"}


async def test_the_hook_archives_the_transcript_before_the_compaction(tmp_path: Path) -> None:
    recorder = Recorder()
    factory = Factory([pre_compact(transcript(tmp_path)), boundary("manual"), result(text="")])
    runtime = ClaudeRuntime(client_factory=factory, archiver=cast(Archiver, recorder))
    await collect(runtime.run(compact_request()))
    ((scope, archive),) = recorder.archives
    assert scope.run_id == "run-1"
    assert archive.trigger == "manual"
    assert [(e.role, e.text) for e in archive.entries] == [("user", "remember 42")]
    assert factory.clients[0].hook_outputs == [{}]


async def test_a_failed_archive_does_not_stop_the_compaction(tmp_path: Path) -> None:
    recorder = Recorder(RuntimeError("secret sk-ant-leak"))
    factory = Factory(
        [pre_compact(transcript(tmp_path), "auto"), boundary("auto"), result(text="")]
    )
    runtime = ClaudeRuntime(client_factory=factory, archiver=cast(Archiver, recorder))
    events = await collect(runtime.run(compact_request()))
    assert isinstance(events[-1], RunCompleted)
    assert any(isinstance(e, Compacted) for e in events)
    assert factory.clients[0].hook_outputs == [{}]


async def test_a_missing_transcript_is_not_archived(tmp_path: Path) -> None:
    recorder = Recorder()
    factory = Factory([pre_compact(str(tmp_path / "gone.jsonl")), result(text="")])
    runtime = ClaudeRuntime(client_factory=factory, archiver=cast(Archiver, recorder))
    await collect(runtime.run(compact_request()))
    assert recorder.archives == []
