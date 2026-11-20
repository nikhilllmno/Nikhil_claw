"""The transcript archive of the Claude adapter with the real CLI and the scripted model
(docs/architecture.md, Section 6.3). The CLI runs the ``PreCompact`` hook before it compacts.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest

from sama.runtime import Compacted, RunCompleted, RunRequest, RuntimeKind
from sama.runtime.archive import TranscriptArchive
from sama.runtime.claude import ClaudeRuntime, ClaudeSettings
from sama.runtime.contract import CompactRequest, RunScope
from tests.conformance.scripted_model import ScriptedModel
from tests.conformance.targets import claude_env, mcp_config
from tests.conformance.tool_server import ToolServer
from tests.support import collect, make_config, make_scope

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("claude_agent_sdk") is None,
    reason="claude-agent-sdk is not installed",
)


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.archives: list[tuple[RunScope, TranscriptArchive]] = []

    async def archive(self, scope: RunScope, archive: TranscriptArchive) -> None:
        self.archives.append((scope, archive))
        if self.fail:
            raise RuntimeError("the control plane is down")


async def start(
    tmp_path: Path, model_url: str, archiver: Recorder, tools: ToolServer, tools_url: str
) -> tuple[ClaudeRuntime, RunRequest]:
    workdir = tmp_path / "work"
    workdir.mkdir()
    settings = ClaudeSettings(cwd=str(workdir), env=claude_env(tmp_path / "claude", model_url))
    runtime = ClaudeRuntime(settings=settings, archiver=archiver)
    config = make_config(RuntimeKind.CLAUDE_AGENT_SDK).model_copy(
        update={"mcp_servers": mcp_config(tools, tools_url), "model": "claude-sonnet-4-5"}
    )
    scope = make_scope(f"run-{uuid.uuid4().hex[:8]}")
    first = RunRequest(scope=scope, input="Say apples", config=config)
    events = await collect(runtime.run(first))
    done = events[-1]
    assert isinstance(done, RunCompleted), done
    request = RunRequest(
        scope=make_scope(f"run-{uuid.uuid4().hex[:8]}"),
        input="",
        config=config,
        runtime_state=done.runtime_state,
        compact=CompactRequest(focus="keep the fruit"),
    )
    return runtime, request


@pytest.mark.parametrize("fail", [False, True], ids=["archive", "failed-archive"])
async def test_the_hook_gives_the_transcript_to_the_archiver_before_the_compaction(
    tmp_path: Path,
    scripted_model: tuple[ScriptedModel, str],
    tool_server: tuple[ToolServer, str],
    fail: bool,
) -> None:
    tools, tools_url = tool_server
    _, model_url = scripted_model
    archiver = Recorder(fail=fail)
    runtime, request = await start(tmp_path, model_url, archiver, tools, tools_url)
    events = await collect(runtime.run(request))
    await runtime.aclose()
    # The compaction happens, with an archive or without one.
    assert [e.trigger for e in events if isinstance(e, Compacted)] == ["manual"]
    assert isinstance(events[-1], RunCompleted)
    ((scope, archive),) = archiver.archives
    assert scope.run_id == request.scope.run_id
    assert archive.trigger == "manual"
    # The archive has the turns from before the compaction.
    texts = [(e.role, e.text) for e in archive.entries]
    assert ("user", "Say apples") in texts
