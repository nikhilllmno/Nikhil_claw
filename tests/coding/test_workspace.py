"""The folder, the diff, and the pull request of a coding request (``sama.coding.workspace``),
and the redaction of the text that leaves sama."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sama.coding.agents import CodingAgent
from sama.coding.events import ClaudeStream, CliToolStart
from sama.coding.service import CodingOutcome, CodingRequest, _scrub, pull_body
from sama.coding.workspace import (
    MAX_DIFF_BYTES,
    MAX_DIFF_READ_BYTES,
    TARGET_CONFIG,
    Workspace,
    WorkspaceError,
    capped_patch,
    diff,
    open_pull,
    prepare,
)
from sama.orchestrator.sandboxed import SandboxedRuntime
from sama.runtime.contract import Budget
from sama.tenancy import RunTokenSigner
from sama.tools.github import GitHubToolError
from sama.tools.repos import RepoRef
from tests.coding.conftest import make_repo, session_sandbox
from tests.support import make_scope

# A token of the JWT form, built at run time so that the secret scan of the repository does not
# see a token in the source.
JWT = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiJydW4tMSIsImV4cCI6MX0", "c2lnbmF0dXJlLXZhbHVl"])


def _git(path: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(
        ["git", *args],  # noqa: S607 -- git on the PATH of the test
        cwd=path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class FakeGitHub:
    def __init__(self) -> None:
        self.pulls: list[dict[str, Any]] = []

    async def create_pull(self, claims: Any, repo: str, **fields: Any) -> str:
        if any(p["head"] == fields["head"] for p in self.pulls):
            raise GitHubToolError(
                "GitHub did not open the pull request: A pull request already exists"
            )
        self.pulls.append({"repo": repo, **fields})
        return f"Opened pull request #{len(self.pulls)} in {repo}."


def test_redaction_comes_before_the_cut() -> None:
    stream = ClaudeStream("claude-sonnet-4-6")
    line = json.dumps(
        {
            "type": "assistant",
            "parent_tool_use_id": None,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "x" * 150 + " " + JWT},
                    }
                ]
            },
        }
    )
    (event,) = stream.feed(line)
    assert isinstance(event, CliToolStart)
    assert "eyJ" not in event.summary


def test_the_pull_request_text_has_no_secret() -> None:
    token = "plain-run-token-value-123"  # a value that no pattern knows
    request = CodingRequest(CodingAgent.CLAUDE_CODE, f"fix it with {token}")
    outcome = CodingOutcome(CodingAgent.CLAUDE_CODE, "m", text=f"done {JWT} and {token}")
    body = _scrub(pull_body(request, outcome), token)
    assert token not in body
    assert JWT not in body


async def test_a_folder_of_another_repository_is_refused(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    sandbox = await session_sandbox(sandboxes)
    folder = Path(sandbox.workdir) / "demo"
    make_repo(folder)
    _git(folder, "remote", "add", "origin", "https://github.com/alice/demo.git")
    claims = signer.verify(sandboxes.issue_token(make_scope(), Budget()))
    with pytest.raises(WorkspaceError, match="another repository"):
        await prepare(sandbox, claims, "t", repo="bob/demo", cloner=None)
    same = await prepare(sandbox, claims, "t", repo="Alice/demo", cloner=None)
    assert same.path == str(folder)


async def test_a_second_pull_request_request_pushes_to_the_same_branch(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner, tmp_path: Path
) -> None:
    sandbox = await session_sandbox(sandboxes)
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    make_repo(seed)
    _git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(origin))
    folder = Path(sandbox.workdir) / "demo"
    _git(tmp_path, "clone", "--quiet", str(origin), str(folder))
    claims = signer.verify(sandboxes.issue_token(make_scope(), Budget()))
    github = FakeGitHub()

    async def request(text: str) -> str:
        space = await prepare(sandbox, claims, "t", repo=None, cloner=None, folder=str(folder))
        space = Workspace(
            space.path, RepoRef("o", "demo"), space.base, space.branch, True, space.target
        )
        (folder / "change.txt").write_text(text)
        changed = await diff(sandbox, space)
        assert changed is not None
        assert changed.files == 1
        return await open_pull(
            sandbox,
            claims,
            "t",
            space,
            github=github,  # type: ignore[arg-type]
            branch="sama/claude-1",
            title=text,
            body="b",
        )

    first = await request("one")
    assert first.startswith("Opened pull request #1")
    assert github.pulls[0]["base"] == "main"
    assert _git(folder, "config", "--get", TARGET_CONFIG) == "main"
    second = await request("two")
    assert "Pushed the new commits to sama/claude-1" in second
    assert len(github.pulls) == 1
    assert _git(origin, "log", "--format=%s", "sama/claude-1").splitlines()[:2] == ["two", "one"]


def _key_block() -> list[str]:
    lines = ["+-----BEGIN " + "PRIVATE KEY-----"]
    lines += ["+" + "A" * 64 for _ in range(30)]
    lines += ["+-----END " + "PRIVATE KEY-----"]
    return lines


def test_a_key_block_across_the_cap_is_not_in_the_diff() -> None:
    # The block crosses the cap: the redaction before the cut removes it whole.
    filler = ["+" + "x" * 99] * (MAX_DIFF_BYTES // 100 - 10)
    raw = "\n".join([*filler, *_key_block(), *filler]) + "\n"
    patch, cut = capped_patch(raw)
    assert cut
    assert "AAAAAAAA" not in patch
    assert len(patch.encode()) <= MAX_DIFF_BYTES
    # The block crosses the end of the read: it has no end line, so the cut removes it.
    filler = ["+" + "x" * 99] * (MAX_DIFF_READ_BYTES // 100 - 10)
    patch, cut = capped_patch("\n".join([*filler, *_key_block()]) + "\n")
    assert cut
    assert "AAAAAAAA" not in patch
    small, small_cut = capped_patch("\n".join(_key_block()) + "\n")
    assert not small_cut
    assert "AAAAAAAA" not in small


def test_a_secret_across_a_field_cut_of_the_body_is_scrubbed() -> None:
    token = "plain-run-token-value-123"
    text = "x" * (8_000 - 10) + token + " more"
    request = CodingRequest(CodingAgent.CLAUDE_CODE, "y" * (4_000 - 5) + token)
    outcome = CodingOutcome(CodingAgent.CLAUDE_CODE, "m", text=text)
    body = pull_body(request, outcome, token)
    assert "plain-run" not in body
