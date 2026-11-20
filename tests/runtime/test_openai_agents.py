"""Unit tests of the OpenAI Agents SDK adapter. The conformance suite runs the adapter end to
end (targets openai-scripted and openai-runner); these tests cover the helpers."""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

pytest.importorskip("agents")

from agents import MaxTurnsExceeded

from sama.runtime.contract import (
    Budget,
    FailureCode,
    RunScope,
    RuntimeKind,
    Usage,
)
from sama.runtime.openai_agents import (
    OpenAIRuntime,
    OpenAISettings,
    StoreFailure,
    _Job,
    _kept_count,
    _new_conversation,
    _PendingCall,
    _State,
    _Store,
    _WindowCompaction,
    archive_entries,
    compact_focus,
    reject_text,
    sama_arguments,
)
from sama.runtime.session_items import (
    CONVERSATION_PATTERN,
    MemorySessionItems,
    SessionItemsAnswer,
    SessionItemsError,
    SessionItemsRequest,
)
from tests.support import make_config, make_scope


def test_compact_focus_reads_only_the_command() -> None:
    assert compact_focus("/compact") == ""
    assert compact_focus("  /compact keep the plan  ") == "keep the plan"
    assert compact_focus("/compaction") is None
    assert compact_focus("please /compact") is None


def test_the_shell_call_is_a_bash_call_for_the_gate() -> None:
    assert sama_arguments("exec_command", {"cmd": "ls", "workdir": "/w", "yield": 1}) == {
        "command": "ls",
        "workdir": "/w",
    }
    assert sama_arguments("exec_command", {}) == {"command": ""}
    assert sama_arguments("sama__todo", {"items": []}) == {"items": []}


def test_the_reject_text_names_the_tool_and_the_reason() -> None:
    assert "Bash" in reject_text("Bash", None)
    assert "Reason from the user: too risky" in reject_text("Bash", "too risky")


def test_the_archive_has_the_readable_items_only() -> None:
    items: list[dict[str, Any]] = [
        {"role": "user", "content": "hello"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi"}],
        },
        {"type": "reasoning", "summary": []},
        {"type": "compaction", "encrypted_content": "x"},
        {"type": "function_call", "name": "exec_command", "arguments": '{"cmd": "ls"}'},
        {"type": "function_call", "name": "sama__todo", "arguments": "not json"},
        {"type": "function_call_output", "output": "a.txt"},
        {"role": "developer", "content": "rules"},
        {"role": "user", "content": ""},
    ]
    entries, omitted = archive_entries(items)
    assert omitted == 0
    assert [(e.role, e.text, e.tool_name) for e in entries] == [
        ("user", "hello", None),
        ("assistant", "hi", None),
        ("assistant", "", "Bash"),
        ("assistant", "", "sama__todo"),
        ("tool", "a.txt", None),
        ("system", "rules", None),
    ]
    assert entries[2].tool_input == {"cmd": "ls"}
    assert entries[3].tool_input == {"raw": "not json"}


def test_a_new_conversation_is_stable_and_valid() -> None:
    first = _new_conversation("run-1")
    assert first == _new_conversation("run-1")
    assert first != _new_conversation("run-2")
    assert re.match(CONVERSATION_PATTERN, first)


def test_the_settings_hide_the_key() -> None:
    settings = OpenAISettings(model_url="http://p/openai/v1", api_key="run-token-secret")
    assert "run-token-secret" not in repr(settings)


def test_the_capabilities_say_what_the_adapter_does() -> None:
    runtime = OpenAIRuntime(items=MemorySessionItems())
    assert runtime.kind is RuntimeKind.OPENAI_AGENTS_SDK
    capabilities = runtime.capabilities()
    assert capabilities.resumable_sessions
    assert capabilities.manual_compaction
    assert capabilities.compaction_archive
    assert not capabilities.native_subagents


class BrokenItems:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def call(self, scope: RunScope, request: SessionItemsRequest) -> SessionItemsAnswer:
        raise self.error


async def test_the_store_marks_a_refusal_as_final_and_a_fault_as_retryable() -> None:
    refused = _Store(BrokenItems(SessionItemsError("too many")), make_scope(), "c-1")
    with pytest.raises(StoreFailure, match="too many") as caught:
        await refused.add_items([{"role": "user", "content": "x"}])
    assert not caught.value.retryable
    response = httpx.Response(503, request=httpx.Request("POST", "http://t/s"))
    fault = httpx.HTTPStatusError("token in here", request=response.request, response=response)
    broken = _Store(BrokenItems(fault), make_scope(), "c-1")
    with pytest.raises(StoreFailure, match="HTTP 503") as caught:
        await broken.get_items()
    assert caught.value.retryable
    assert "token in here" not in str(caught.value)


async def test_the_store_is_an_sdk_session_over_the_items() -> None:
    items = MemorySessionItems()
    store = _Store(items, make_scope(), "c-1")
    await store.add_items([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    assert await store.count() == 2
    assert await store.get_items(limit=1) == [{"role": "user", "content": "b"}]
    assert await store.pop_item() == {"role": "user", "content": "b"}
    assert await store.replace([{"role": "user", "content": "c"}]) == 1
    assert await store.trim(0) == 0
    await store.clear_session()
    assert await store.pop_item() is None


def _job() -> _Job:
    config = make_config(RuntimeKind.OPENAI_AGENTS_SDK)
    return _Job(
        scope=make_scope(),
        config=config,
        budget=Budget(),
        conversation="c-1",
        base_count=0,
        start_usage=Usage(output_tokens=3),
    )


def test_failures_map_to_the_contract_codes() -> None:
    runtime = OpenAIRuntime(items=MemorySessionItems())
    job = _job()
    try:
        try:
            raise StoreFailure("HTTP 503", retryable=True)
        except StoreFailure as inner:
            raise RuntimeError("the sdk wrapped it") from inner
    except RuntimeError as outer:
        wrapped = outer
    failed = runtime._failure(wrapped, job)
    assert failed.code is FailureCode.INTERNAL
    assert failed.retryable
    assert failed.message == "the session items failed: HTTP 503"
    assert failed.usage.output_tokens == 3
    final = runtime._failure(StoreFailure("too many"), job)
    assert not final.retryable
    assert runtime._failure(MaxTurnsExceeded("x"), job).code is FailureCode.BUDGET_EXCEEDED
    unknown = runtime._failure(KeyError("secret"), job)
    assert unknown.code is FailureCode.INTERNAL
    assert "secret" not in unknown.message


def test_the_window_of_a_manual_compaction_stays_whole() -> None:
    user = {"type": "message", "role": "user", "content": "Remember the word kumquat"}
    compaction = {"type": "compaction", "encrypted_content": "x"}
    later = {"type": "message", "role": "user", "content": "And now?"}
    context: list[Any] = [user, compaction, later]
    # The first two items are the answer of /responses/compact: nothing is cut.
    assert _WindowCompaction(floor=2).process_context(context) == context
    # A compaction item after the window cuts the context, as the SDK capability does.
    server = [*context, compaction, later]
    assert _WindowCompaction(floor=2).process_context(server) == [compaction, later]
    assert _WindowCompaction().process_context(context) == [compaction, later]


def test_a_new_run_keeps_the_items_of_the_run_before() -> None:
    assert _kept_count(None) == 0
    done = _State(conversation="c-1", run_id="r-1", item_count=4)
    assert _kept_count(done) == 4
    pending = _PendingCall(call_id="call-1", tool_name="Bash")
    paused = done.model_copy(update={"pending": pending, "paused_count": 7})
    assert _kept_count(paused) == 7
