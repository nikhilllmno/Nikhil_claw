"""The same tests for each sandbox provider (docs/architecture.md, Section 16.2).

- ``local`` runs always.
- ``docker`` runs with ``SAMA_DOCKER_IMAGE``, the tag of the sama image.
- ``e2b`` runs with ``SAMA_LIVE_TESTS=1`` and ``E2B_API_KEY``. It uses the template in
  ``SAMA_E2B_TEMPLATE`` (default ``sama-runner``), so build the template first.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final, cast

import pytest

from sama.orchestrator.sandboxed import RUNNER_ARGV, SandboxedRuntime, SandboxSettings
from sama.runtime import RunCompleted, RunConfig, RunRequest, RuntimeKind, Usage
from sama.runtime.fake import FakeScript
from sama.runtime.runner import forbidden_env
from sama.sandbox import (
    Sandbox,
    SandboxError,
    SandboxFileTooLargeError,
    SandboxNotFoundError,
    SandboxOutdatedError,
    SandboxProvider,
    SandboxSpec,
)
from sama.tenancy import RunTokenSigner
from tests.support import collect, make_scope

DOCKER_IMAGE = os.environ.get("SAMA_DOCKER_IMAGE", "")
E2B_KEY = os.environ.get("E2B_API_KEY", "")
E2B_TEMPLATE = os.environ.get("SAMA_E2B_TEMPLATE", "sama-runner")
LIVE_E2B = (
    os.environ.get("SAMA_LIVE_TESTS") == "1"
    and bool(E2B_KEY)
    and importlib.util.find_spec("e2b") is not None
)

PROVIDERS = [
    pytest.param("local", id="local"),
    pytest.param(
        "docker",
        id="docker",
        marks=pytest.mark.skipif(not DOCKER_IMAGE, reason="set SAMA_DOCKER_IMAGE"),
    ),
    pytest.param(
        "e2b",
        id="e2b",
        marks=[
            pytest.mark.live,
            pytest.mark.skipif(not LIVE_E2B, reason="set SAMA_LIVE_TESTS=1 and E2B_API_KEY"),
        ],
    ),
]


class Harness:
    def __init__(self, name: str, provider: SandboxProvider, runner: tuple[str, ...]) -> None:
        self.name = name
        self.provider = provider
        self.runner = runner
        self.created: list[Sandbox] = []

    def spec(
        self, session_id: str = "", allow_out: tuple[str, ...] = (), *, open_egress: bool = False
    ) -> SandboxSpec:
        return SandboxSpec(
            org_id="org-test",
            session_id=session_id or f"sess-{uuid.uuid4().hex[:8]}",
            template=E2B_TEMPLATE if self.name == "e2b" else "sama",
            allow_out=allow_out,
            open_egress=open_egress,
        )

    async def create(self, spec: SandboxSpec) -> Sandbox:
        sandbox = await self.provider.create(spec)
        self.created.append(sandbox)
        return sandbox


@pytest.fixture(params=PROVIDERS)
async def harness(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Harness]:
    provider: SandboxProvider
    runner = RUNNER_ARGV
    if request.param == "local":
        from sama.sandbox.local import LocalProvider

        provider = LocalProvider(tmp_path / "sandboxes")
        runner = (sys.executable, "-m", "sama.runtime.runner")
    elif request.param == "docker":
        from sama.sandbox.docker import DockerProvider

        provider = DockerProvider(image=DOCKER_IMAGE)
    else:
        from sama.sandbox.e2b import E2BProvider

        provider = E2BProvider(api_key=E2B_KEY)
    item = Harness(request.param, provider, runner)
    yield item
    for sandbox in item.created:
        with contextlib.suppress(SandboxError):
            await sandbox.kill()


async def test_exec_runs_in_the_work_folder(harness: Harness) -> None:
    sandbox = await harness.create(harness.spec())
    result = await sandbox.exec(["sh", "-c", "pwd; echo $GREETING"], env={"GREETING": "hello"})
    assert result.exit_code == 0, result.stderr
    assert result.stdout.split() == [sandbox.workdir, "hello"]


async def test_exec_reports_the_exit_code(harness: Harness) -> None:
    sandbox = await harness.create(harness.spec())
    result = await sandbox.exec(["sh", "-c", "echo oops >&2; exit 7"])
    assert result.exit_code == 7
    assert "oops" in result.stderr


async def test_files_round_trip(harness: Harness) -> None:
    sandbox = await harness.create(harness.spec())
    path = f"{sandbox.workdir}/dir/a.bin"
    await sandbox.write_file(path, b"\x00binary\xff")
    assert await sandbox.read_file(path) == b"\x00binary\xff"
    await sandbox.remove_file(path)
    with pytest.raises(FileNotFoundError):
        await sandbox.read_file(path)


async def test_a_bounded_read_stops_after_the_limit(harness: Harness) -> None:
    sandbox = await harness.create(harness.spec())
    path = f"{sandbox.workdir}/ten.bin"
    await sandbox.write_file(path, b"0123456789")
    assert await sandbox.read_file(path, max_bytes=10) == b"0123456789"
    with pytest.raises(SandboxFileTooLargeError):
        await sandbox.read_file(path, max_bytes=9)
    with pytest.raises(FileNotFoundError):
        await sandbox.read_file(f"{sandbox.workdir}/none.bin", max_bytes=9)


async def test_pause_and_resume_keep_the_files(harness: Harness) -> None:
    spec = harness.spec()
    sandbox = await harness.create(spec)
    path = f"{sandbox.workdir}/kept.txt"
    await sandbox.write_file(path, b"kept")
    await sandbox.pause()
    again = await harness.provider.resume(sandbox.id, spec)
    assert again.id == sandbox.id
    assert await again.read_file(path) == b"kept"


async def test_resume_checks_the_session(harness: Harness) -> None:
    sandbox = await harness.create(harness.spec())
    with pytest.raises(SandboxError):
        await harness.provider.resume(sandbox.id, harness.spec(session_id="sess-other"))


async def test_resume_of_a_sandbox_from_another_template_gives_it_to_the_caller(
    harness: Harness,
) -> None:
    """The provider does not resume an outdated sandbox as the session sandbox. It gives the
    running sandbox to the caller, which moves the state and kills it (Section 16.2)."""
    spec = harness.spec()
    sandbox = await harness.create(spec)
    path = f"{sandbox.workdir}/kept.txt"
    await sandbox.write_file(path, b"state")
    newer = dataclasses.replace(spec, template=f"{spec.template}-newer")
    with pytest.raises(SandboxOutdatedError) as caught:
        await harness.provider.resume(sandbox.id, newer)
    outdated = caught.value.sandbox
    assert outdated.id == sandbox.id
    assert await outdated.read_file(path) == b"state", "the sandbox still runs"
    await outdated.kill()
    with pytest.raises(SandboxNotFoundError):
        await harness.provider.resume(sandbox.id, spec)


async def test_resume_of_a_lost_sandbox_is_not_found(harness: Harness) -> None:
    spec = harness.spec()
    sandbox = await harness.provider.create(spec)
    await sandbox.kill()
    with pytest.raises(SandboxNotFoundError):
        await harness.provider.resume(sandbox.id, spec)


async def test_a_process_streams_lines_and_reads_stdin(harness: Harness) -> None:
    sandbox = await harness.create(harness.spec())
    process = await sandbox.start(["sh", "-c", "read a; echo got-$a; echo err >&2; exit 3"])
    await process.write("x\n")
    lines = [line async for line in process.lines()]
    assert lines == ["got-x"]
    assert await process.wait() == 3
    assert "err" in process.stderr_tail()


_ALIVE: Final = (
    'pid=$(cat "$0") && [ -d "/proc/$pid" ] && [ "$(cut -d" " -f3 "/proc/$pid/stat")" != Z ]'
)


async def test_kill_stops_the_process_in_the_sandbox(harness: Harness) -> None:
    """The kill stops the command in the sandbox, not only the client on the host."""
    sandbox = await harness.create(harness.spec())
    pid_file = f"{sandbox.workdir}/sleep.pid"
    process = await sandbox.start(["sh", "-c", 'echo $$ > "$0"; exec sleep 300', pid_file])
    wait = 'for _ in $(seq 50); do [ -s "$0" ] && exit 0; sleep 0.1; done; exit 1'
    assert (await sandbox.exec(["sh", "-c", wait, pid_file])).exit_code == 0
    assert (await sandbox.exec(["sh", "-c", _ALIVE, pid_file])).exit_code == 0
    await process.kill()
    assert await process.wait() != 0
    assert (await sandbox.exec(["sh", "-c", _ALIVE, pid_file])).exit_code != 0


async def test_kill_works_after_the_docker_client_ended(harness: Harness) -> None:
    """A lost ``docker exec`` client does not stop the command in the container."""
    if harness.name != "docker":
        pytest.skip("only Docker has a host client")
    from sama.sandbox.docker import DockerProcess

    sandbox = await harness.create(harness.spec())
    pid_file = f"{sandbox.workdir}/sleep.pid"
    process = await sandbox.start(["sh", "-c", 'echo $$ > "$0"; exec sleep 300', pid_file])
    wait = 'for _ in $(seq 50); do [ -s "$0" ] && exit 0; sleep 0.1; done; exit 1'
    assert (await sandbox.exec(["sh", "-c", wait, pid_file])).exit_code == 0
    assert isinstance(process, DockerProcess)
    await process._client.kill()  # the connection is lost
    assert (await sandbox.exec(["sh", "-c", _ALIVE, pid_file])).exit_code == 0
    await process.kill()
    assert (await sandbox.exec(["sh", "-c", _ALIVE, pid_file])).exit_code != 0


async def test_the_runner_runs_in_the_sandbox(harness: Harness) -> None:
    """The whole path: the orchestrator starts the runner, which runs the fake runtime."""
    usage = Usage(output_tokens=1)
    runtime = SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=harness.provider,
        signer=RunTokenSigner(b"t" * 32),
        settings=SandboxSettings(template=harness.spec().template, runner_argv=harness.runner),
    )
    runtime.fake_scripts.append(FakeScript(events=(RunCompleted(output="hi", usage=usage),)))
    scope = make_scope(f"run-{uuid.uuid4().hex[:8]}").model_copy(
        update={"session_id": f"sess-{uuid.uuid4().hex[:8]}"}
    )
    config = RunConfig(runtime=RuntimeKind.FAKE, model="fake")
    try:
        events = await collect(runtime.run(RunRequest(scope=scope, input="hi", config=config)))
    finally:
        await runtime.close_sandboxes(kill=True)
    assert isinstance(events[-1], RunCompleted), events[-1]
    assert events[-1].output == "hi"


_ENV_DUMP: Final = (
    "import json, os; "
    "print(json.dumps({'type': 'run_completed', 'output': json.dumps(dict(os.environ)),"
    " 'usage': {}}))"
)


async def test_the_sandbox_has_no_provider_key(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roadmap M1.7: the host has provider keys, and the runner in the sandbox sees none of them.
    It has the run token and the model proxy URL."""
    secrets = {name: f"sk-host-{name.lower()}" for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    runtime = SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=harness.provider,
        signer=RunTokenSigner(b"t" * 32),
        settings=SandboxSettings(
            template=harness.spec().template,
            runner_argv=(harness.runner[0], "-c", _ENV_DUMP),
            model_url="http://proxy.test:8787",
        ),
    )
    scope = make_scope(f"run-{uuid.uuid4().hex[:8]}").model_copy(
        update={"session_id": f"sess-{uuid.uuid4().hex[:8]}"}
    )
    config = RunConfig(runtime=RuntimeKind.FAKE, model="fake")
    try:
        events = await collect(runtime.run(RunRequest(scope=scope, input="hi", config=config)))
    finally:
        await runtime.close_sandboxes(kill=True)
    last = events[-1]
    assert isinstance(last, RunCompleted), last
    env: dict[str, str] = json.loads(last.output)
    assert not forbidden_env(env)
    assert not [name for name, value in env.items() if value in secrets.values()]
    assert env["SAMA_MODEL_URL"] == "http://proxy.test:8787"
    assert env["SAMA_RUN_TOKEN"]


_CURL: Final = ("curl", "-sS", "-I", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10")
"""A HEAD request: the test needs the status, not the body."""


@pytest.mark.live
@pytest.mark.skipif(not LIVE_E2B, reason="set SAMA_LIVE_TESTS=1 and E2B_API_KEY")
async def test_e2b_blocks_hosts_that_are_not_allowed(tmp_path: Path) -> None:
    from sama.sandbox.e2b import E2BProvider

    harness = Harness("e2b", E2BProvider(api_key=E2B_KEY), RUNNER_ARGV)
    sandbox = await harness.create(harness.spec(allow_out=("pypi.org",)))
    try:
        allowed = await sandbox.exec([*_CURL, "https://pypi.org/simple/pip/"], timeout_s=30)
        blocked = await sandbox.exec([*_CURL, "https://example.com/"], timeout_s=30)
    finally:
        await sandbox.kill()
    assert allowed.exit_code == 0, allowed.stderr
    assert allowed.stdout == "200"
    assert blocked.exit_code != 0


@pytest.mark.live
@pytest.mark.skipif(not LIVE_E2B, reason="set SAMA_LIVE_TESTS=1 and E2B_API_KEY")
async def test_e2b_with_no_allowlist_blocks_all_egress() -> None:
    from sama.sandbox.e2b import E2BProvider

    harness = Harness("e2b", E2BProvider(api_key=E2B_KEY), RUNNER_ARGV)
    sandbox = await harness.create(harness.spec())
    try:
        blocked = await sandbox.exec([*_CURL, "https://pypi.org/simple/pip/"], timeout_s=30)
    finally:
        await sandbox.kill()
    assert blocked.exit_code != 0


def test_e2b_network_options() -> None:
    pytest.importorskip("e2b")
    from sama.sandbox.e2b import ALL_TRAFFIC, network_options

    harness = Harness("e2b", cast(SandboxProvider, None), RUNNER_ARGV)
    assert network_options(harness.spec()) == {"deny_out": [ALL_TRAFFIC]}
    assert network_options(harness.spec(allow_out=("pypi.org",))) == {
        "deny_out": [ALL_TRAFFIC],
        "allow_out": ["pypi.org"],
    }
    assert network_options(harness.spec(allow_out=("pypi.org",), open_egress=True)) is None


def test_the_labels_name_the_template_and_a_sandbox_without_it_is_outdated() -> None:
    spec = SandboxSpec(org_id="org-test", session_id="sess-1", template="sama-runner-abc")
    labels = spec.labels()
    assert labels["sama.template"] == "sama-runner-abc"
    assert not spec.outdated(labels)
    assert spec.outdated({"sama.org_id": "org-test", "sama.session_id": "sess-1"})
    assert spec.outdated({**labels, "sama.template": "sama-runner-old"})
    # A caller cannot set the template label through metadata.
    forged = SandboxSpec(
        org_id="org-test",
        session_id="sess-1",
        template="sama-runner-abc",
        metadata={"sama.template": "other"},
    )
    assert forged.labels()["sama.template"] == "sama-runner-abc"
