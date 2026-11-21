"""Fixtures for the coding agent tests: a fake ``claude`` and ``codex`` on ``PATH``, a local
sandbox, and a git repository in its work folder."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from sama.orchestrator.sandboxed import SandboxedRuntime, SandboxSettings
from sama.runtime.contract import Budget, RuntimeKind
from sama.sandbox.local import LocalProvider, LocalSandbox
from sama.tenancy import RunTokenSigner
from tests.support import make_scope

KEY = b"k" * 32
FAKE = Path(__file__).with_name("fake_cli.py")


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Puts ``claude`` and ``codex`` scripts first on ``PATH``. The local sandbox gives its
    processes the ``PATH`` of the host."""
    folder = tmp_path / "bin"
    folder.mkdir()
    for name in ("claude", "codex"):
        script = folder / name
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" {name} "$@"\n')
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    return folder


@pytest.fixture
def signer() -> RunTokenSigner:
    return RunTokenSigner(KEY)


@pytest.fixture
async def sandboxes(
    tmp_path: Path, fake_bin: Path, signer: RunTokenSigner
) -> AsyncIterator[SandboxedRuntime]:
    runtime = SandboxedRuntime(
        kind=RuntimeKind.CLAUDE_AGENT_SDK,
        provider=LocalProvider(tmp_path / "sandboxes"),
        signer=signer,
        settings=SandboxSettings(template="local", model_url="http://proxy.test"),
    )
    yield runtime
    await runtime.close_sandboxes(kill=True)


async def session_sandbox(runtime: SandboxedRuntime, run_id: str = "run-1") -> LocalSandbox:
    """The sandbox of the test session, made now."""
    sandbox = await runtime.acquire(make_scope(run_id), Budget())
    await runtime.release(make_scope(run_id), sandbox)
    assert isinstance(sandbox, LocalSandbox)
    return sandbox


def make_repo(path: Path) -> str:
    """A git repository with one commit. Returns the commit."""
    path.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],  # noqa: S607 -- git on the PATH of the test
            cwd=path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--quiet", "--initial-branch", "main")
    (path / "README.md").write_text("hello\n")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "first")
    return git("rev-parse", "HEAD")


def set_mode(sandbox: LocalSandbox, mode: str) -> None:
    (sandbox.root / "home" / "fake.json").write_text(json.dumps({"mode": mode}))


def calls(sandbox: LocalSandbox) -> list[dict[str, object]]:
    path = sandbox.root / "home" / "calls.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]
