"""Tests of ``SandboxedRuntime`` with the local provider and the fake runtime in the runner."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

import sama.orchestrator.sandboxed as sandboxed_module
from sama.orchestrator.sandboxed import CAPABILITIES, SandboxedRuntime, SandboxSettings
from sama.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    Budget,
    Compacted,
    FailureCode,
    ResumeRequest,
    RunCompleted,
    RunConfig,
    RunEvent,
    RunFailed,
    RunPhase,
    RunPhaseName,
    RunRequest,
    RunScope,
    RuntimeKind,
    TextDelta,
    Usage,
    validate_event_stream,
)
from sama.runtime.contract import CompactRequest
from sama.runtime.fake import FakeRuntime, FakeScript
from sama.runtime.runner import RunnerJob
from sama.sandbox import ExecResult, SandboxError, SandboxProcess, SandboxSpec
from sama.sandbox.base import DEFAULT_IDLE_TIMEOUT_S
from sama.sandbox.local import LocalProvider, LocalSandbox
from sama.sandbox.process import SubprocessProcess
from sama.tenancy import RunTokenSigner
from tests.support import collect, make_scope

KEY = b"k" * 32
RUNNER = (sys.executable, "-m", "sama.runtime.runner")
CONFIG = RunConfig(runtime=RuntimeKind.FAKE, model="fake")


@dataclass
class RecordingSandbox(LocalSandbox):
    """Records each ``keep_alive`` call, and each provider call in ``calls``: ``start``,
    ``exec``, and ``write_file``."""

    keep_alives: list[int] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    async def keep_alive(self, seconds: int) -> None:
        self.keep_alives.append(seconds)

    async def start(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: str | None = None
    ) -> SandboxProcess:
        self.calls.append("start")
        return await super().start(argv, env=env, cwd=cwd)

    async def exec(self, argv: Sequence[str], **kwargs: Any) -> ExecResult:
        self.calls.append("exec")
        return await super().exec(argv, **kwargs)

    async def write_file(self, path: str, data: bytes) -> None:
        self.calls.append("write_file")
        await super().write_file(path, data)


class CountingProvider(LocalProvider):
    """Counts creates and resumes, and records ``keep_alive`` calls and the idle timeouts of
    the specs of creates and resumes (``spec_timeouts``). ``create_gate`` holds
    each create until it is set, to test a slow start. ``fail_resumes`` fails that many
    resumes."""

    creates: int = 0
    resumes: int = 0
    fail_resumes: int = 0

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.keep_alives: list[int] = []
        self.spec_timeouts: list[int] = []
        self.calls: list[str] = []
        self.create_gate: asyncio.Event | None = None
        self.create_entered = asyncio.Event()

    def _record(self, sandbox: LocalSandbox) -> LocalSandbox:
        return RecordingSandbox(sandbox.root, keep_alives=self.keep_alives, calls=self.calls)

    async def create(self, spec: SandboxSpec) -> LocalSandbox:
        self.creates += 1
        self.spec_timeouts.append(spec.idle_timeout_s)
        self.create_entered.set()
        if self.create_gate is not None:
            await self.create_gate.wait()
        return self._record(await super().create(spec))

    async def resume(self, sandbox_id: str, spec: SandboxSpec) -> LocalSandbox:
        self.resumes += 1
        self.spec_timeouts.append(spec.idle_timeout_s)
        if self.fail_resumes:
            self.fail_resumes -= 1
            raise SandboxError("the provider is down")
        return self._record(await super().resume(sandbox_id, spec))


@pytest.fixture
def provider(tmp_path: Path) -> CountingProvider:
    return CountingProvider(tmp_path / "sandboxes")


def _sandboxed(
    provider: CountingProvider,
    argv: tuple[str, ...] = RUNNER,
    signer: RunTokenSigner | None = None,
) -> SandboxedRuntime:
    return SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=provider,
        signer=signer or RunTokenSigner(KEY),
        settings=SandboxSettings(template="local", runner_argv=argv),
    )


@pytest.fixture
async def runtime(provider: CountingProvider) -> AsyncIterator[SandboxedRuntime]:
    sandboxed = _sandboxed(provider)
    yield sandboxed
    await sandboxed.close_sandboxes(kill=True)


def _reply(text: str) -> FakeScript:
    usage = Usage(output_tokens=1)
    return FakeScript(
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


class FakeSkills:
    def __init__(self, files: dict[str, str] | None = None, *, fail: bool = False) -> None:
        self.files_by_scope = files or {}
        self.fail = fail
        self.calls: list[RunScope] = []

    async def files(self, scope: RunScope) -> dict[str, str]:
        self.calls.append(scope)
        if self.fail:
            raise RuntimeError("the library is down")
        return self.files_by_scope


async def test_the_skills_of_the_session_are_written_before_the_run(
    provider: CountingProvider,
) -> None:
    skills = FakeSkills({"notes/SKILL.md": "---\nname: notes\n---\n", "notes/scripts/a.py": "x"})
    runtime = SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=provider,
        signer=RunTokenSigner(KEY),
        settings=SandboxSettings(template="local", runner_argv=RUNNER),
        skills=skills,
    )
    try:
        runtime.fake_scripts.append(_reply("ok"))
        events = await collect(runtime.run(_request(make_scope("run-1"))))
        assert isinstance(events[-1], RunCompleted)
        sandbox = await runtime.session_sandbox("org-1", "sess-1")
        assert sandbox is not None
        base = f"{sandbox.workdir}/.sama/skills"
        assert await sandbox.read_file(f"{base}/notes/scripts/a.py") == b"x"
        assert await sandbox.read_file(f"{base}/notes/SKILL.md") == b"---\nname: notes\n---\n"
        # The next run syncs again and removes a file that left the library.
        skills.files_by_scope = {"notes/SKILL.md": "---\nname: notes\n---\n"}
        runtime.fake_scripts.append(_reply("ok"))
        await collect(runtime.run(_request(make_scope("run-2"))))
        with pytest.raises(FileNotFoundError):
            await sandbox.read_file(f"{base}/notes/scripts/a.py")
        assert [c.run_id for c in skills.calls] == ["run-1", "run-2"]
    finally:
        await runtime.close_sandboxes(kill=True)


async def test_unchanged_skills_cost_no_sandbox_call_before_the_runner(
    provider: CountingProvider, caplog: pytest.LogCaptureFixture
) -> None:
    skills = FakeSkills({"notes/SKILL.md": "---\nname: notes\n---\n"})
    runtime = SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=provider,
        signer=RunTokenSigner(KEY),
        settings=SandboxSettings(template="local", runner_argv=RUNNER),
        skills=skills,
    )
    caplog.set_level("INFO", logger="sama.orchestrator")
    try:
        # The first run finds no skills folder: the check command does not run the job, the
        # control plane writes the files, and it starts the runner again. The job runs once.
        runtime.fake_scripts.append(_reply("first"))
        events = await collect(runtime.run(_request(make_scope("run-1"))))
        assert isinstance(events[-1], RunCompleted)
        assert events[-1].output == "first"
        assert provider.calls == ["start", "write_file", "exec", "start"]
        assert "skills=changed" in caplog.text
        # The next run has the same skills: one command checks them and starts the runner.
        provider.calls.clear()
        caplog.clear()
        runtime.fake_scripts.append(_reply("second"))
        events = await collect(runtime.run(_request(make_scope("run-2"))))
        assert isinstance(events[-1], RunCompleted)
        assert events[-1].output == "second"
        assert provider.calls == ["start"]
        assert "skills=same" in caplog.text
        assert "acquire_s=" in caplog.text
        assert "first_event_s=" in caplog.text
    finally:
        await runtime.close_sandboxes(kill=True)


async def test_a_link_in_place_of_the_skills_parent_does_not_fail_the_run(
    provider: CountingProvider,
) -> None:
    runtime = SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=provider,
        signer=RunTokenSigner(KEY),
        settings=SandboxSettings(template="local", runner_argv=RUNNER),
        skills=FakeSkills({"notes/SKILL.md": "x"}),
    )
    try:
        runtime.fake_scripts.append(_reply("ok"))
        await collect(runtime.run(_request(make_scope("run-1"))))
        sandbox = await runtime.session_sandbox("org-1", "sess-1")
        assert sandbox is not None
        outside = provider.root / "outside"
        outside.mkdir()
        sama = Path(sandbox.workdir) / ".sama"
        await sandbox.exec(["rm", "-rf", str(sama)])
        sama.symlink_to(outside)
        # The check refuses the link, and the sync fails, but the run goes on.
        runtime.fake_scripts.append(_reply("again"))
        events = await collect(runtime.run(_request(make_scope("run-2"))))
        assert isinstance(events[-1], RunCompleted)
        assert events[-1].output == "again"
        assert not (outside / "skills").exists()
    finally:
        await runtime.close_sandboxes(kill=True)


async def test_a_failed_skill_sync_does_not_fail_the_run(provider: CountingProvider) -> None:
    runtime = SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=provider,
        signer=RunTokenSigner(KEY),
        settings=SandboxSettings(template="local", runner_argv=RUNNER),
        skills=FakeSkills(fail=True),
    )
    try:
        runtime.fake_scripts.append(_reply("ok"))
        events = await collect(runtime.run(_request(make_scope("run-1"))))
        assert isinstance(events[-1], RunCompleted)
    finally:
        await runtime.close_sandboxes(kill=True)


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


class DictDirectory:
    """A ``SandboxDirectory`` in memory. ``broken`` makes ``put`` fail."""

    def __init__(self, *, broken: bool = False) -> None:
        self.ids: dict[tuple[str, str], str] = {}
        self.broken = broken

    async def get(self, org_id: str, session_id: str) -> str | None:
        return self.ids.get((org_id, session_id))

    async def put(self, org_id: str, session_id: str, sandbox_id: str) -> None:
        if self.broken:
            raise RuntimeError("the database is down")
        self.ids[(org_id, session_id)] = sandbox_id


async def test_another_worker_finds_the_sandbox_in_the_directory(
    provider: CountingProvider,
) -> None:
    directory = DictDirectory()
    settings = SandboxSettings(template="local", runner_argv=RUNNER)
    first, second = (
        SandboxedRuntime(
            kind=RuntimeKind.FAKE,
            provider=provider,
            signer=RunTokenSigner(KEY),
            settings=settings,
            directory=directory,
        )
        for _ in range(2)
    )
    first.fake_scripts.append(_reply("ok"))
    await collect(first.run(_request(make_scope("run-1"))))
    assert directory.ids == {("org-1", "sess-1"): first.sandbox_id("org-1", "sess-1")}
    assert await second.session_sandbox("org-1", "sess-1") is not None
    second.fake_scripts.append(_reply("ok"))
    events = await collect(second.run(_request(make_scope("run-2"))))
    assert isinstance(events[-1], RunCompleted)
    assert provider.creates == 1
    await first.close_sandboxes(kill=True)


async def test_a_deploy_with_a_new_template_replaces_the_session_sandbox(
    provider: CountingProvider,
) -> None:
    """A control plane of a new commit does not resume a sandbox with the runner of the old
    commit (Section 16.2). It removes that sandbox and creates one from its own template."""
    directory = DictDirectory()
    old, new = (
        SandboxedRuntime(
            kind=RuntimeKind.FAKE,
            provider=provider,
            signer=RunTokenSigner(KEY),
            settings=SandboxSettings(template=template, runner_argv=RUNNER),
            directory=directory,
        )
        for template in ("sama-runner-old", "sama-runner-new")
    )
    old.fake_scripts.append(_reply("ok"))
    await collect(old.run(_request(make_scope("run-1"))))
    first = old.sandbox_id("org-1", "sess-1")
    assert first is not None
    new.fake_scripts.append(_reply("ok"))
    events = await collect(new.run(_request(make_scope("run-2"))))
    assert isinstance(events[-1], RunCompleted)
    assert provider.creates == 2
    second = new.sandbox_id("org-1", "sess-1")
    assert second not in (None, first)
    assert directory.ids == {("org-1", "sess-1"): second}
    assert await old.session_sandbox("org-1", "sess-1") is None, "the old sandbox is removed"
    await new.close_sandboxes(kill=True)


def _two_templates(
    provider: CountingProvider, directory: DictDirectory, **settings: Any
) -> tuple[SandboxedRuntime, SandboxedRuntime]:
    old, new = (
        SandboxedRuntime(
            kind=RuntimeKind.FAKE,
            provider=provider,
            signer=RunTokenSigner(KEY),
            settings=SandboxSettings(template=template, runner_argv=RUNNER, **settings),
            directory=directory,
        )
        for template in ("sama-runner-old", "sama-runner-new")
    )
    return old, new


async def test_a_new_template_moves_the_cli_state_to_the_new_sandbox(
    provider: CountingProvider,
) -> None:
    """The transcripts of the CLI go to the new sandbox, so the next run resumes the same
    conversation. Other files stay behind."""
    directory = DictDirectory()
    old, new = _two_templates(provider, directory)
    old.fake_scripts.append(_reply("ok"))
    await collect(old.run(_request(make_scope("run-1"))))
    first = await old.session_sandbox("org-1", "sess-1")
    assert first is not None
    made = await first.exec(
        [
            "sh",
            "-c",
            'mkdir -p "$HOME/.claude/projects/w" && echo hello > "$HOME/.claude/projects/w/s.jsonl"'
            ' && echo other > "$HOME/other.txt"',
        ]
    )
    assert made.exit_code == 0, made.stderr
    new.fake_scripts.append(_reply("ok"))
    events = await collect(new.run(_request(make_scope("run-2"))))
    assert isinstance(events[-1], RunCompleted)
    second = await new.session_sandbox("org-1", "sess-1")
    assert second is not None
    assert second.id != first.id
    moved = await second.exec(
        ["sh", "-c", 'cat "$HOME/.claude/projects/w/s.jsonl"; test ! -e "$HOME/other.txt"']
    )
    assert (moved.exit_code, moved.stdout) == (0, "hello\n")
    with pytest.raises(FileNotFoundError):
        await second.read_file(f"{second.workdir}/.sama-carry.tgz")
    await new.close_sandboxes(kill=True)


async def test_a_failed_move_of_the_cli_state_does_not_fail_the_run(
    provider: CountingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = DictDirectory()
    old, new = _two_templates(provider, directory)
    old.fake_scripts.append(_reply("ok"))
    await collect(old.run(_request(make_scope("run-1"))))
    first = await old.session_sandbox("org-1", "sess-1")
    assert first is not None
    await first.exec(["sh", "-c", 'mkdir -p "$HOME/.claude" && echo x > "$HOME/.claude/a"'])

    async def broken(self: LocalSandbox, path: str, *, max_bytes: int | None = None) -> bytes:
        raise OSError("read failed")

    monkeypatch.setattr(LocalSandbox, "read_file", broken)
    new.fake_scripts.append(_reply("ok"))
    events = await collect(new.run(_request(make_scope("run-2"))))
    assert isinstance(events[-1], RunCompleted)
    assert provider.creates == 2
    assert await old.session_sandbox("org-1", "sess-1") is None, "the old sandbox is removed"
    await new.close_sandboxes(kill=True)


async def test_an_archive_that_grows_after_the_size_check_does_not_move(
    provider: CountingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sandbox can change the archive after ``stat``. The read stops at the limit, so the
    control plane never holds a larger archive."""
    monkeypatch.setattr(sandboxed_module, "CARRY_MAX_BYTES", 4_096)
    directory = DictDirectory()
    old, new = _two_templates(provider, directory)
    old.fake_scripts.append(_reply("ok"))
    await collect(old.run(_request(make_scope("run-1"))))
    first = await old.session_sandbox("org-1", "sess-1")
    assert first is not None
    await first.exec(["sh", "-c", 'mkdir -p "$HOME/.claude" && echo x > "$HOME/.claude/a"'])
    real_exec = LocalSandbox.exec

    async def grow(self: LocalSandbox, argv: Sequence[str], **kwargs: Any) -> ExecResult:
        result = await real_exec(self, argv, **kwargs)
        archive = (kwargs.get("env") or {}).get("CARRY")
        if self.id == first.id and archive:
            grow_argv = ["sh", "-c", 'head -c 10000 /dev/zero >> "$CARRY"']
            await real_exec(self, grow_argv, env={"CARRY": archive})
        return result

    monkeypatch.setattr(LocalSandbox, "exec", grow)
    new.fake_scripts.append(_reply("ok"))
    events = await collect(new.run(_request(make_scope("run-2"))))
    assert isinstance(events[-1], RunCompleted)
    second = await new.session_sandbox("org-1", "sess-1")
    assert second is not None
    monkeypatch.setattr(LocalSandbox, "exec", real_exec)
    moved = await second.exec(["sh", "-c", 'test ! -e "$HOME/.claude/a"'])
    assert moved.exit_code == 0, "the archive over the limit did not move"
    await new.close_sandboxes(kill=True)


async def test_a_lookup_of_an_outdated_sandbox_does_not_pause_it(
    provider: CountingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool can look up the session sandbox while a run moves its state. The lookup must not
    stop the old sandbox, or the move fails."""
    directory = DictDirectory()
    old, new = _two_templates(provider, directory)
    old.fake_scripts.append(_reply("ok"))
    await collect(old.run(_request(make_scope("run-1"))))
    paused: list[str] = []

    async def pause(self: LocalSandbox) -> None:
        paused.append(self.id)

    monkeypatch.setattr(LocalSandbox, "pause", pause)
    assert await new.session_sandbox("org-1", "sess-1") is None
    assert paused == []
    await old.close_sandboxes(kill=True)


async def test_a_directory_error_does_not_fail_the_run(provider: CountingProvider) -> None:
    runtime = SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=provider,
        signer=RunTokenSigner(KEY),
        settings=SandboxSettings(template="local", runner_argv=RUNNER),
        directory=DictDirectory(broken=True),
    )
    runtime.fake_scripts.append(_reply("ok"))
    events = await collect(runtime.run(_request(make_scope("run-1"))))
    assert isinstance(events[-1], RunCompleted)
    await runtime.close_sandboxes(kill=True)


async def test_the_wrong_runtime_kind_is_refused(runtime: SandboxedRuntime) -> None:
    config = RunConfig(runtime=RuntimeKind.CLAUDE_AGENT_SDK, model="m")
    events = await collect(runtime.run(_request(make_scope(), config)))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INVALID_REQUEST


async def test_an_approval_resumes_only_once(runtime: SandboxedRuntime) -> None:
    usage = Usage(output_tokens=1)
    paused = ApprovalRequired(approval_id="a1", call_id="c1", tool_name="t", usage=usage)
    done = (RunCompleted(output="ok", usage=usage),)
    runtime.fake_scripts.append(FakeScript(events=(paused,), on_approve=done, on_reject=done))
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
    slow = FakeScript(events=(*words, RunCompleted(output="x", usage=words[-1].usage)), delay_s=0.1)
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
    sandboxed = _sandboxed(provider, (sys.executable, "-c", "pass"))
    events = await collect(sandboxed.run(_request(make_scope())))
    validate_event_stream(events)
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.SANDBOX_ERROR
    assert events[-1].retryable
    await sandboxed.close_sandboxes(kill=True)


async def test_a_runner_that_does_not_start_is_a_sandbox_error(
    provider: CountingProvider,
) -> None:
    sandboxed = _sandboxed(provider, ("/no/such/runner",))
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
    sandboxed = _sandboxed(provider, (sys.executable, "-c", script), signer)
    scope = make_scope("run-7")
    events = await collect(sandboxed.run(_request(scope)))
    last = events[-1]
    assert isinstance(last, RunCompleted), last
    names, token = last.output.split("|")
    assert "ANTHROPIC_API_KEY" not in names
    assert signer.verify(token).matches(scope)
    await sandboxed.close_sandboxes(kill=True)


async def test_the_idle_timeout_is_set_back_after_the_run(
    runtime: SandboxedRuntime, provider: CountingProvider
) -> None:
    runtime.fake_scripts.append(_reply("ok"))
    await collect(runtime.run(_request(make_scope("run-1"))))
    await runtime.wait_for_cleanup()
    idle = SandboxSpec(org_id="o", session_id="s", template="local").idle_timeout_s
    # The create sets a timeout long enough for the run, so no keep_alive comes before it.
    assert provider.spec_timeouts == [int(sandboxed_module.DEFAULT_RUN_WALL_TIME_S + 600)]
    assert provider.spec_timeouts[0] > idle
    assert provider.keep_alives == [idle]


async def test_cancel_during_a_slow_start_ends_the_stream(
    runtime: SandboxedRuntime, provider: CountingProvider
) -> None:
    provider.create_gate = asyncio.Event()
    scope = make_scope("run-1")
    collecting = asyncio.ensure_future(collect(runtime.run(_request(scope))))
    await asyncio.wait_for(provider.create_entered.wait(), timeout=5)
    async with asyncio.timeout(2):
        await runtime.cancel(scope.run_id)
        events = await collecting
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED
    # The late sandbox gets the idle timeout back.
    provider.create_gate.set()
    await runtime.wait_for_cleanup()
    idle = SandboxSpec(org_id="o", session_id="s", template="local").idle_timeout_s
    assert provider.keep_alives[-1] == idle


async def test_cancel_kills_a_runner_that_does_not_stop(
    provider: CountingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandboxed_module, "CANCEL_KILL_AFTER_S", 0.5)
    sandboxed = _sandboxed(provider, (sys.executable, "-c", "import time; time.sleep(60)"))
    scope = make_scope("run-1")
    collecting = asyncio.ensure_future(collect(sandboxed.run(_request(scope))))
    await asyncio.sleep(0.5)  # the runner has started
    async with asyncio.timeout(3):
        await sandboxed.cancel(scope.run_id)
        events = await collecting
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED
    await sandboxed.close_sandboxes(kill=True)


async def test_cancel_ends_the_stream_while_the_job_write_hangs(
    provider: CountingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang(self: SubprocessProcess, data: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(SubprocessProcess, "write", hang)
    monkeypatch.setattr(sandboxed_module, "CANCEL_DEADLINE_S", 0.5)
    monkeypatch.setattr(sandboxed_module, "PROVIDER_CALL_S", 1.0)
    sandboxed = _sandboxed(provider)
    scope = make_scope("run-1")
    collecting = asyncio.ensure_future(collect(sandboxed.run(_request(scope))))
    await asyncio.sleep(0.3)  # the worker waits in the job write
    async with asyncio.timeout(2):
        await sandboxed.cancel(scope.run_id)
        events = await collecting
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED
    await sandboxed.close_sandboxes(kill=True)
    idle = SandboxSpec(org_id="o", session_id="s", template="local").idle_timeout_s
    assert provider.keep_alives[-1] == idle


async def test_a_resume_with_a_failed_job_write_uses_the_approval(
    runtime: SandboxedRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner can have the job after a failed write, so a retry can run the call twice."""
    usage = Usage(output_tokens=1)
    paused = ApprovalRequired(approval_id="a1", call_id="c1", tool_name="t", usage=usage)
    done = (RunCompleted(output="ok", usage=usage),)
    runtime.fake_scripts.append(FakeScript(events=(paused,), on_approve=done, on_reject=done))
    scope = make_scope("run-1")
    await collect(runtime.run(_request(scope)))

    async def lost(self: SubprocessProcess, data: str) -> None:
        raise SandboxError("the connection dropped")

    monkeypatch.setattr(SubprocessProcess, "write", lost)
    resume = ResumeRequest(
        scope=scope, config=CONFIG, approval_id="a1", decision=ApprovalDecision.APPROVE
    )
    failed = await collect(runtime.resume(resume))
    assert isinstance(failed[-1], RunFailed)
    assert failed[-1].code is FailureCode.SANDBOX_ERROR
    assert not failed[-1].retryable
    again = await collect(runtime.resume(resume))
    assert isinstance(again[-1], RunFailed)
    assert "already used" in again[-1].message


async def test_a_cancelled_consumer_does_not_leak_the_session_run(
    runtime: SandboxedRuntime, provider: CountingProvider
) -> None:
    provider.create_gate = asyncio.Event()
    collecting = asyncio.ensure_future(collect(runtime.run(_request(make_scope("run-1")))))
    await asyncio.wait_for(provider.create_entered.wait(), timeout=5)
    collecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await collecting
    provider.create_gate.set()
    await runtime.wait_for_cleanup()
    idle = SandboxSpec(org_id="o", session_id="s", template="local").idle_timeout_s
    assert provider.keep_alives[-1] == idle
    # The next run of the session works and sets the timeout back again.
    runtime.fake_scripts.append(_reply("ok"))
    events = await collect(runtime.run(_request(make_scope("run-2"))))
    assert isinstance(events[-1], RunCompleted)
    await runtime.wait_for_cleanup()
    assert provider.keep_alives[-1] == idle


async def test_a_stream_error_is_a_sandbox_error(
    runtime: SandboxedRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: SubprocessProcess) -> AsyncIterator[str]:
        raise SandboxError("the connection dropped")
        yield ""  # pragma: no cover -- makes this a generator

    monkeypatch.setattr(SubprocessProcess, "lines", broken)
    runtime.fake_scripts.append(_reply("ok"))
    events = await collect(runtime.run(_request(make_scope("run-1"))))
    validate_event_stream(events)
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.SANDBOX_ERROR
    assert events[-1].retryable


async def test_a_resume_that_fails_to_start_can_be_retried(
    runtime: SandboxedRuntime, provider: CountingProvider
) -> None:
    usage = Usage(output_tokens=1)
    paused = ApprovalRequired(approval_id="a1", call_id="c1", tool_name="t", usage=usage)
    done = (RunCompleted(output="ok", usage=usage),)
    runtime.fake_scripts.append(FakeScript(events=(paused,), on_approve=done, on_reject=done))
    scope = make_scope("run-1")
    await collect(runtime.run(_request(scope)))
    resume = ResumeRequest(
        scope=scope, config=CONFIG, approval_id="a1", decision=ApprovalDecision.APPROVE
    )
    provider.fail_resumes = 1
    failed = await collect(runtime.resume(resume))
    assert isinstance(failed[-1], RunFailed)
    assert failed[-1].code is FailureCode.SANDBOX_ERROR
    # The run still waits for approval, and the approval is not used.
    again = await collect(runtime.run(_request(scope)))
    assert isinstance(again[-1], RunFailed)
    assert "already active" in again[-1].message
    retry = await collect(runtime.resume(resume))
    assert isinstance(retry[-1], RunFailed)
    assert "already used" not in retry[-1].message  # the fake runner refuses it, see above


async def test_cancel_wins_over_a_buffered_approval(runtime: SandboxedRuntime) -> None:
    usage = Usage(output_tokens=1)
    paused = ApprovalRequired(approval_id="a1", call_id="c1", tool_name="t", usage=usage)
    done = (RunCompleted(output="ok", usage=usage),)
    first = TextDelta(text="w", usage=usage)
    runtime.fake_scripts.append(
        FakeScript(events=(first, paused), on_approve=done, on_reject=done, delay_s=0.05)
    )
    scope = make_scope("run-1")
    stream = cast(AsyncGenerator[RunEvent], runtime.run(_request(scope)))
    events: list[RunEvent] = []
    async with contextlib.aclosing(stream):
        events.append(await anext(stream))
        await asyncio.sleep(0.5)  # the runner has written the approval
        async with asyncio.timeout(10):
            await runtime.cancel(scope.run_id)
            events.extend([e async for e in stream])
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED
    resume = ResumeRequest(
        scope=scope, config=CONFIG, approval_id="a1", decision=ApprovalDecision.APPROVE
    )
    refused = await collect(runtime.resume(resume))
    assert isinstance(refused[-1], RunFailed)
    assert "cancelled" in refused[-1].message


def test_the_capabilities_match_the_adapters() -> None:
    assert CAPABILITIES[RuntimeKind.FAKE] == FakeRuntime().capabilities()
    claude = pytest.importorskip("sama.runtime.claude")
    assert CAPABILITIES[RuntimeKind.CLAUDE_AGENT_SDK] == claude.ClaudeRuntime().capabilities()
    openai = pytest.importorskip("sama.runtime.openai_agents")
    from sama.runtime.session_items import MemorySessionItems

    adapter = openai.OpenAIRuntime(items=MemorySessionItems())
    assert CAPABILITIES[RuntimeKind.OPENAI_AGENTS_SDK] == adapter.capabilities()


def test_another_kind_shares_the_sandboxes_and_the_runs(runtime: SandboxedRuntime) -> None:
    other = runtime.for_kind(RuntimeKind.OPENAI_AGENTS_SDK)
    assert other.kind is RuntimeKind.OPENAI_AGENTS_SDK
    assert runtime.kind is RuntimeKind.FAKE
    assert other.capabilities() == CAPABILITIES[RuntimeKind.OPENAI_AGENTS_SDK]
    # One state: a session keeps its sandbox when its runtime changes, and a cancel finds the
    # run in either object.
    assert other._sandbox_ids is runtime._sandbox_ids
    assert other._active is runtime._active
    assert other._used_approvals is runtime._used_approvals


async def test_a_compact_request_reaches_the_runner(runtime: SandboxedRuntime) -> None:
    first = await collect(runtime.run(_request(make_scope("run-1"))))
    done = first[-1]
    assert isinstance(done, RunCompleted)
    assert done.runtime_state is not None
    compact = RunRequest(
        scope=make_scope("run-2"),
        input="",
        config=CONFIG,
        runtime_state=done.runtime_state,
        compact=CompactRequest(focus="keep the plan"),
    )
    events = await collect(runtime.run(compact))
    validate_event_stream(events)
    events = [event for event in events if not isinstance(event, RunPhase)]
    assert [type(event) for event in events] == [Compacted, RunCompleted]
    assert isinstance(events[0], Compacted)
    assert (events[0].tokens_before, events[0].tokens_after, events[0].trigger) == (
        1000,
        500,
        "manual",
    )


def test_the_capabilities_of_the_sandboxed_runtimes_name_the_compaction_features() -> None:
    assert CAPABILITIES[RuntimeKind.FAKE].manual_compaction
    assert not CAPABILITIES[RuntimeKind.FAKE].compaction_archive
    assert CAPABILITIES[RuntimeKind.CLAUDE_AGENT_SDK].manual_compaction
    assert CAPABILITIES[RuntimeKind.CLAUDE_AGENT_SDK].compaction_archive


def test_the_settings_can_name_the_archive_endpoint() -> None:
    assert SandboxSettings(template="t").archive_url is None
    assert SandboxSettings(template="t", archive_url="http://tools/archive").archive_url


_SLOW_EXIT = (
    "import sys, time\n"
    "sys.stdin.readline()\n"
    'print(\'{"type": "run_completed", "output": "ok"}\', flush=True)\n'
    "time.sleep(1.0)\n"
)


async def test_the_terminal_event_does_not_wait_for_the_runner_to_exit(
    provider: CountingProvider,
) -> None:
    sandboxed = _sandboxed(provider, (sys.executable, "-c", _SLOW_EXIT))
    started = asyncio.get_running_loop().time()
    events = await collect(sandboxed.run(_request(make_scope())))
    assert isinstance(events[-1], RunCompleted)
    assert asyncio.get_running_loop().time() - started < 0.9
    # The runner still stops, and the idle timeout is set back only after that.
    assert provider.keep_alives == []
    await sandboxed.wait_for_cleanup()
    assert asyncio.get_running_loop().time() - started >= 1.0
    assert len(provider.keep_alives) == 1
    await sandboxed.close_sandboxes(kill=True)


async def test_the_timing_line_of_the_runner_is_logged(
    runtime: SandboxedRuntime, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="sama.orchestrator")
    runtime.fake_scripts.append(_reply("ok"))
    scope = make_scope("run-timing")
    await collect(runtime.run(_request(scope)))
    await runtime.wait_for_cleanup()
    lines = [r.getMessage() for r in caplog.records if "runner timing" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("runner timing mode=cold job_s=")
    assert "terminal_s=" in lines[0]
    assert lines[0].endswith("run=run-timing")


async def test_a_job_script_runs_in_the_session_sandbox(runtime: SandboxedRuntime) -> None:
    scope = make_scope("fire-1")
    result = await runtime.exec_script(
        scope, 'echo "out $((1 + 2))"; echo oops >&2; exit 3', timeout_s=30
    )
    assert (result.exit_code, result.stdout.strip(), result.stderr.strip()) == (3, "out 3", "oops")
    again = await runtime.exec_script(scope, "pwd", timeout_s=30)
    sandbox = await runtime.session_sandbox(scope.org_id, scope.session_id)
    assert sandbox is not None
    assert again.stdout.strip() == sandbox.workdir


class _Preparer:
    """A coding step (Section 15.5) that records its calls and gives a fixed note."""

    def __init__(self, note: str) -> None:
        self.note = note
        self.calls: list[tuple[str, str]] = []

    async def prepare(self, sandbox: Any, scope: RunScope, token: str) -> str:
        self.calls.append((scope.run_id, token))
        return self.note


async def test_the_coding_step_adds_its_note_to_the_input(
    runtime: SandboxedRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs: list[Any] = []
    write = sandboxed_module._write_job

    async def spy(process: SandboxProcess, job: Any) -> None:
        jobs.append(job)
        await write(process, job)

    monkeypatch.setattr(sandboxed_module, "_write_job", spy)
    preparer = _Preparer("[sama coding mode] note")
    runtime.set_preparer(preparer)
    runtime.fake_scripts.append(_reply("ok"))
    events = await collect(runtime.run(_request(make_scope("run-1"))))
    assert isinstance(events[-1], RunCompleted)
    assert jobs[-1].run.input == "hi\n\n[sama coding mode] note"
    ((run_id, token),) = preparer.calls
    assert run_id == "run-1"
    assert RunTokenSigner(KEY).verify(token).run_id == "run-1"

    # An empty note keeps the input, and a /compact run has no coding step.
    preparer.note = ""
    runtime.fake_scripts.append(_reply("ok"))
    await collect(runtime.run(_request(make_scope("run-2"))))
    assert jobs[-1].run.input == "hi"
    compact = RunRequest(
        scope=make_scope("run-3"),
        input="",
        config=CONFIG,
        runtime_state=b"state",
        compact=CompactRequest(focus=""),
    )
    job = RunnerJob(op="run", run=compact)
    assert await runtime._prepare(job, cast(Any, None), compact.scope, "token") is job
    assert len(preparer.calls) == 2


# -- run phases and the session warm (Section 4.4) ----------------------------------------------


async def test_a_run_sends_the_start_phases_before_the_runner_events(
    runtime: SandboxedRuntime,
) -> None:
    runtime.fake_scripts.append(_reply("hello"))
    events = await collect(runtime.run(_request(make_scope("run-1"))))
    validate_event_stream(events)
    phases = [e.phase for e in events if isinstance(e, RunPhase)]
    assert phases == [RunPhaseName.STARTING_SANDBOX, RunPhaseName.STARTING_RUNTIME]
    assert all(isinstance(e, RunPhase) for e in events[:2])
    assert isinstance(events[2], TextDelta)


async def test_a_resume_also_sends_the_start_phases(runtime: SandboxedRuntime) -> None:
    usage = Usage(input_tokens=5)
    paused = ApprovalRequired(approval_id="a1", call_id="c1", tool_name="Bash", usage=usage)
    done = (RunCompleted(output="ok", usage=usage),)
    runtime.fake_scripts.append(FakeScript(events=(paused,), on_approve=done, on_reject=done))
    scope = make_scope("run-1")
    await collect(runtime.run(_request(scope)))
    resume = ResumeRequest(
        scope=scope, config=CONFIG, approval_id="a1", decision=ApprovalDecision.APPROVE
    )
    # The fake runner keeps paused runs in memory, so it refuses the resume. The phases of
    # the start come first all the same.
    events = await collect(runtime.resume(resume))
    assert [e.phase for e in events if isinstance(e, RunPhase)] == [
        RunPhaseName.STARTING_SANDBOX,
        RunPhaseName.STARTING_RUNTIME,
    ]
    assert isinstance(events[-1], RunFailed)


async def test_a_warm_starts_the_session_sandbox_and_the_next_run_uses_it(
    runtime: SandboxedRuntime, provider: CountingProvider
) -> None:
    assert await runtime.warm("org-1", "sess-1") == "ready"
    assert provider.creates == 1
    # The warm uses the idle timeout, and it sets the idle timeout back at the end.
    assert provider.spec_timeouts == [DEFAULT_IDLE_TIMEOUT_S]
    assert provider.keep_alives == [DEFAULT_IDLE_TIMEOUT_S]
    # The runner is not a warm runner here, so the warm starts no process.
    assert "exec" not in provider.calls
    runtime.fake_scripts.append(_reply("ok"))
    await collect(runtime.run(_request(make_scope("run-1"))))
    assert (provider.creates, provider.resumes) == (1, 1)


async def test_a_warm_with_the_warm_runner_starts_the_warm_server(
    provider: CountingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _sandboxed(provider, argv=(sys.executable, "-m", "sama.warm"))
    started: list[Sequence[str]] = []

    async def fake_exec(_self: RecordingSandbox, argv: Sequence[str], **_: Any) -> ExecResult:
        started.append(list(argv))
        return ExecResult(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(RecordingSandbox, "exec", fake_exec)
    try:
        assert await runtime.warm("org-1", "sess-1") == "ready"
    finally:
        await runtime.close_sandboxes(kill=True)
    assert started == [[sys.executable, "-m", "sama.warm", "--start"]]


async def test_a_warm_during_a_run_does_not_touch_the_sandbox(
    runtime: SandboxedRuntime, provider: CountingProvider
) -> None:
    scope = make_scope("run-1")
    sandbox = await runtime.acquire(scope, Budget())
    try:
        resumes = provider.resumes
        assert await runtime.warm(scope.org_id, scope.session_id) == "busy"
        assert provider.resumes == resumes
    finally:
        await runtime.release(scope, sandbox)


async def test_a_failed_warm_reports_failed_and_does_not_raise(
    runtime: SandboxedRuntime, provider: CountingProvider
) -> None:
    assert await runtime.warm("org-1", "sess-1") == "ready"
    provider.fail_resumes = 1
    assert await runtime.warm("org-1", "sess-1") == "failed"
