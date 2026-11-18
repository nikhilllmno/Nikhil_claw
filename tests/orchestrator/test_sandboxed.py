"""Tests of ``SandboxedRuntime`` with the local provider and the fake runtime in the runner."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import cast

import pytest

from sama.orchestrator.sandboxed import CAPABILITIES, SandboxedRuntime, SandboxSettings
from sama.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    FailureCode,
    ResumeRequest,
    RunCompleted,
    RunConfig,
    RunEvent,
    RunFailed,
    RunRequest,
    RunScope,
    RuntimeKind,
    TextDelta,
    Usage,
    validate_event_stream,
)
from sama.runtime.fake import FakeRuntime
from sama.runtime.runner import FakeScriptSpec
from sama.sandbox import Sandbox, SandboxError, SandboxSpec
from sama.sandbox.local import LocalProvider, LocalSandbox
from sama.tenancy import RunTokenSigner
from tests.support import collect, make_scope

KEY = b"k" * 32
RUNNER = (sys.executable, "-m", "sama.runtime.runner")
CONFIG = RunConfig(runtime=RuntimeKind.FAKE, model="fake")


class CountingProvider(LocalProvider):
    """Counts creates and resumes."""

    creates: int = 0
    resumes: int = 0

    async def create(self, spec: SandboxSpec) -> LocalSandbox:
        self.creates += 1
        return await super().create(spec)

    async def resume(self, sandbox_id: str, spec: SandboxSpec) -> LocalSandbox:
        self.resumes += 1
        return await super().resume(sandbox_id, spec)


@pytest.fixture
def provider(tmp_path: Path) -> CountingProvider:
    return CountingProvider(tmp_path / "sandboxes")


@pytest.fixture
async def runtime(provider: CountingProvider) -> AsyncIterator[SandboxedRuntime]:
    sandboxed = SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=provider,
        signer=RunTokenSigner(KEY),
        settings=SandboxSettings(template="local", runner_argv=RUNNER),
    )
    yield sandboxed
    await sandboxed.close_sandboxes(kill=True)


def _reply(text: str) -> FakeScriptSpec:
    usage = Usage(output_tokens=1)
    return FakeScriptSpec(
        events=(TextDelta(text=text, usage=usage), RunCompleted(output=text, usage=usage))
    )


def _request(scope: RunScope, config: RunConfig = CONFIG) -> RunRequest:
    return RunRequest(scope=scope, input="hi", config=config)


async def test_a_run_streams_the_runner_events(runtime: SandboxedRuntime) -> None:
    runtime.fake_scripts.append(_reply("hello"))
    events = await collect(runtime.run(_request(make_scope("run-1"))))
    validate_event_stream(events)
    assert isinstance(events[-1], RunCompleted)
    assert events[-1].output == "hello"


async def test_one_sandbox_per_session(
    runtime: SandboxedRuntime, provider: CountingProvider
) -> None:
    for run_id in ("run-1", "run-2"):
        runtime.fake_scripts.append(_reply("ok"))
        await collect(runtime.run(_request(make_scope(run_id))))
    assert provider.creates == 1
    assert provider.resumes == 1
    other = make_scope("run-3").model_copy(update={"session_id": "sess-2"})
    runtime.fake_scripts.append(_reply("ok"))
    await collect(runtime.run(_request(other)))
    assert provider.creates == 2
    first = runtime.sandbox_id("org-1", "sess-1")
    assert first is not None
    assert first != runtime.sandbox_id("org-1", "sess-2")


async def test_files_stay_in_the_session_sandbox(runtime: SandboxedRuntime) -> None:
    scope = make_scope("run-1")
    runtime.fake_scripts.append(_reply("ok"))
    await collect(runtime.run(_request(scope)))
    sandbox = await runtime.session_sandbox(scope.org_id, scope.session_id)
    assert sandbox is not None
    await sandbox.write_file(f"{sandbox.workdir}/a.txt", b"kept")
    runtime.fake_scripts.append(_reply("ok"))
    await collect(runtime.run(_request(make_scope("run-2"))))
    again = await runtime.session_sandbox(scope.org_id, scope.session_id)
    assert again is not None
    assert await again.read_file(f"{again.workdir}/a.txt") == b"kept"


async def test_a_lost_sandbox_is_created_again(
    runtime: SandboxedRuntime, provider: CountingProvider
) -> None:
    runtime.fake_scripts.append(_reply("ok"))
    await collect(runtime.run(_request(make_scope("run-1"))))
    sandbox = await runtime.session_sandbox("org-1", "sess-1")
    assert sandbox is not None
    await sandbox.kill()
    runtime.fake_scripts.append(_reply("ok"))
    events = await collect(runtime.run(_request(make_scope("run-2"))))
    assert isinstance(events[-1], RunCompleted)
    assert provider.creates == 2
    assert runtime.sandbox_id("org-1", "sess-1") != sandbox.id


async def test_the_wrong_runtime_kind_is_refused(runtime: SandboxedRuntime) -> None:
    config = RunConfig(runtime=RuntimeKind.CLAUDE_AGENT_SDK, model="m")
    events = await collect(runtime.run(_request(make_scope(), config)))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INVALID_REQUEST


async def test_an_approval_resumes_only_once(runtime: SandboxedRuntime) -> None:
    usage = Usage(output_tokens=1)
    paused = ApprovalRequired(approval_id="a1", call_id="c1", tool_name="t", usage=usage)
    done = (RunCompleted(output="ok", usage=usage),)
    runtime.fake_scripts.append(FakeScriptSpec(events=(paused,), on_approve=done, on_reject=done))
    scope = make_scope("run-1")
    events = await collect(runtime.run(_request(scope)))
    assert isinstance(events[-1], ApprovalRequired)
    # A second run with the same id is refused while the run waits for approval.
    again = await collect(runtime.run(_request(scope)))
    assert isinstance(again[-1], RunFailed)
    assert again[-1].code is FailureCode.INVALID_REQUEST
    resume = ResumeRequest(
        scope=scope, config=CONFIG, approval_id="a1", decision=ApprovalDecision.APPROVE
    )
    # The fake keeps paused runs in memory, so its runner refuses the resume. The check here
    # is that the orchestrator refuses the second resume before it starts a runner.
    await collect(runtime.resume(resume))
    second = await collect(runtime.resume(resume))
    assert isinstance(second[-1], RunFailed)
    assert "already used" in second[-1].message


async def test_a_cancelled_run_cannot_resume(runtime: SandboxedRuntime) -> None:
    await runtime.cancel("run-1")
    resume = ResumeRequest(
        scope=make_scope("run-1"), config=CONFIG, approval_id="a1", decision=ApprovalDecision.REJECT
    )
    events = await collect(runtime.resume(resume))
    assert isinstance(events[-1], RunFailed)
    assert "cancelled" in events[-1].message


async def test_cancel_stops_the_runner(runtime: SandboxedRuntime) -> None:
    words = [TextDelta(text="w ", usage=Usage(output_tokens=i)) for i in range(1, 101)]
    slow = FakeScriptSpec(
        events=(*words, RunCompleted(output="x", usage=words[-1].usage)), delay_s=0.1
    )
    runtime.fake_scripts.append(slow)
    scope = make_scope("run-1")
    stream = cast(AsyncGenerator[RunEvent], runtime.run(_request(scope)))
    events: list[RunEvent] = []
    async with contextlib.aclosing(stream):
        async for event in stream:
            events.append(event)
            if isinstance(event, TextDelta):
                break
        async with asyncio.timeout(10):
            await runtime.cancel(scope.run_id)
            events.extend([e async for e in stream])
    validate_event_stream(events)
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED


async def test_a_runner_that_dies_is_a_sandbox_error(provider: CountingProvider) -> None:
    sandboxed = SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=provider,
        signer=RunTokenSigner(KEY),
        settings=SandboxSettings(template="local", runner_argv=(sys.executable, "-c", "pass")),
    )
    events = await collect(sandboxed.run(_request(make_scope())))
    validate_event_stream(events)
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.SANDBOX_ERROR
    assert events[-1].retryable
    await sandboxed.close_sandboxes(kill=True)


async def test_a_runner_that_does_not_start_is_a_sandbox_error(
    provider: CountingProvider,
) -> None:
    sandboxed = SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=provider,
        signer=RunTokenSigner(KEY),
        settings=SandboxSettings(template="local", runner_argv=("/no/such/runner",)),
    )
    events = await collect(sandboxed.run(_request(make_scope())))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.SANDBOX_ERROR
    await sandboxed.close_sandboxes(kill=True)


async def test_the_runner_gets_only_the_run_token(
    provider: CountingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host has a provider key. The runner does not see it, and the token is for this run."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-host-key")
    signer = RunTokenSigner(KEY)
    script = (
        "import json, os; "
        "print(json.dumps({'type': 'run_completed', 'output': json.dumps(sorted(os.environ)) "
        "+ '|' + os.environ['SAMA_RUN_TOKEN'], 'usage': {}}))"
    )
    sandboxed = SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=provider,
        signer=signer,
        settings=SandboxSettings(template="local", runner_argv=(sys.executable, "-c", script)),
    )
    scope = make_scope("run-7")
    events = await collect(sandboxed.run(_request(scope)))
    last = events[-1]
    assert isinstance(last, RunCompleted), last
    names, token = last.output.split("|")
    assert "ANTHROPIC_API_KEY" not in names
    assert signer.verify(token).matches(scope)
    await sandboxed.close_sandboxes(kill=True)


async def test_a_sandbox_of_another_session_is_refused(provider: CountingProvider) -> None:
    spec = SandboxSpec(org_id="org-1", session_id="sess-1", template="local")
    sandbox: Sandbox = await provider.create(spec)
    other = SandboxSpec(org_id="org-2", session_id="sess-1", template="local")
    with pytest.raises(SandboxError):
        await provider.resume(sandbox.id, other)


def test_the_capabilities_match_the_adapters() -> None:
    assert CAPABILITIES[RuntimeKind.FAKE] == FakeRuntime().capabilities()
    claude = pytest.importorskip("sama.runtime.claude")
    assert CAPABILITIES[RuntimeKind.CLAUDE_AGENT_SDK] == claude.ClaudeRuntime().capabilities()


def test_an_unsupported_kind_is_refused(provider: CountingProvider) -> None:
    with pytest.raises(ValueError, match="does not support"):
        SandboxedRuntime(
            kind=RuntimeKind.OPENAI_AGENTS_SDK,
            provider=provider,
            signer=RunTokenSigner(KEY),
            settings=SandboxSettings(template="local"),
        )
