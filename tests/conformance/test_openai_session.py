"""The session items of the OpenAI adapter with the scripted Responses API: a manual compaction
keeps the context, and a transient failure of the store does not erase the history
(docs/architecture.md, Section 5.3, "OpenAI adapter notes")."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from sama.runtime import Compacted, RunCompleted, RunFailed, RunRequest, RuntimeKind
from sama.runtime.contract import CompactRequest, RunConfig, RunScope
from sama.runtime.session_items import (
    MemorySessionItems,
    SessionItemsAnswer,
    SessionItemsRequest,
)
from tests.conformance.conftest import OPENAI_MODEL
from tests.conformance.scripted_openai import ScriptedResponses
from tests.support import collect, make_scope

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("agents") is None, reason="openai-agents is not installed"
)


class FlakyItems:
    """``MemorySessionItems`` that fails the next operation of a kind one time."""

    def __init__(self) -> None:
        self.items = MemorySessionItems()
        self.fail_next: str | None = None

    async def call(self, scope: RunScope, request: SessionItemsRequest) -> SessionItemsAnswer:
        if self.fail_next == request.op:
            self.fail_next = None
            raise httpx.ConnectError("the control plane is down")
        return await self.items.call(scope, request)


@pytest.fixture
async def runtime(
    tmp_path: Path, scripted_responses: tuple[ScriptedResponses, str]
) -> AsyncIterator[tuple[object, FlakyItems]]:
    from sama.runtime.openai_agents import OpenAIRuntime, OpenAISettings

    _, url = scripted_responses
    workdir = tmp_path / "work"
    workdir.mkdir()
    items = FlakyItems()
    adapter = OpenAIRuntime(
        items=items,
        settings=OpenAISettings(cwd=str(workdir), model_url=f"{url}/v1", api_key="scripted"),
    )
    yield adapter, items
    await adapter.aclose()


def _request(text: str, state: bytes | None, compact: CompactRequest | None = None) -> RunRequest:
    config = RunConfig(runtime=RuntimeKind.OPENAI_AGENTS_SDK, model=OPENAI_MODEL, soul="Test.")
    return RunRequest(
        scope=make_scope(f"run-{uuid.uuid4().hex[:8]}"),
        input=text,
        config=config,
        runtime_state=state,
        compact=compact,
    )


async def _completed(adapter: object, request: RunRequest) -> RunCompleted:
    events = await collect(adapter.run(request))  # type: ignore[attr-defined]
    done = events[-1]
    assert isinstance(done, RunCompleted), done
    return done


async def test_the_context_after_a_manual_compaction_keeps_the_user_messages(
    runtime: tuple[object, FlakyItems],
    scripted_responses: tuple[ScriptedResponses, str],
) -> None:
    adapter, _ = runtime
    model, _ = scripted_responses
    word = f"kumquat{uuid.uuid4().hex[:6]}"
    first = await _completed(adapter, _request(f"Remember the word {word}", None))
    events = await collect(
        adapter.run(  # type: ignore[attr-defined]
            _request("", first.runtime_state, CompactRequest(focus="keep the word"))
        )
    )
    assert [e.trigger for e in events if isinstance(e, Compacted)] == ["manual"]
    compacted = events[-1]
    assert isinstance(compacted, RunCompleted)
    # The next calls get the whole answer of /responses/compact: the user message before the
    # compaction item stays in the context, for one run and for the run after it.
    recall = "What word did I ask you to remember?"
    for _ in range(2):
        compacted = await _completed(adapter, _request(recall, compacted.runtime_state))
        assert compacted.output == word
        assert model.requests[-1].input_types[0] == "message"


async def test_a_transient_store_failure_keeps_the_history(
    runtime: tuple[object, FlakyItems],
) -> None:
    adapter, items = runtime
    word = f"quince{uuid.uuid4().hex[:6]}"
    first = await _completed(adapter, _request(f"Remember the word {word}", None))
    # The first operation of the next run (the trim of ``_prepare``) fails one time.
    items.fail_next = "trim"
    events = await collect(
        adapter.run(_request("Say hello", first.runtime_state))  # type: ignore[attr-defined]
    )
    failed = events[-1]
    assert isinstance(failed, RunFailed), failed
    assert failed.retryable
    assert failed.runtime_state is not None
    # The next run starts from the failure state and still has the remembered word.
    recall = _request("What word did I ask you to remember?", failed.runtime_state)
    again = await _completed(adapter, recall)
    assert again.output == word
