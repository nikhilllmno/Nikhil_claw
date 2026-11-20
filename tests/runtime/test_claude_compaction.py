"""The compaction parts of the Claude adapter: ``/compact`` and ``PreCompact`` (Section 6.3)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import HookInput, SystemMessage

from sama.runtime import Compacted, FailureCode, RunCompleted, RunFailed, RunRequest
from sama.runtime.archive import Archiver, TranscriptArchive
from sama.runtime.claude import ClaudeRuntime
from sama.runtime.contract import CompactRequest, RunScope
from tests.runtime.test_claude import SESSION, Factory, FakeClient, config, result
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
    factory = Factory([result(text="")])
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
