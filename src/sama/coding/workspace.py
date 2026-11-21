"""The folder of a coding agent in the session sandbox, its diff, and its pull request
(docs/architecture.md, Section 15.4).

- ``prepare`` gives the folder of a request. With a repository, it is ``<work>/<name>``, the
  same folder as ``repo_clone``. A folder that has a clone of the repository is used again, so
  a follow-up request sees the earlier changes. Else ``RepoCloner`` clones it, through the git
  proxy when the repository is on the allowlist of the org.
- ``diff`` gives the changes since the commit at the start of the request: the commits of the
  CLI and the changes that are not committed, with new files.
- ``open_pull`` commits the changes to a ``sama/*`` branch, pushes it through the git proxy,
  and opens a pull request with the GitHub App (Section 15.1). The proxy checks the push.
"""

from __future__ import annotations

import re
import shlex
import uuid
from dataclasses import dataclass
from typing import Final

from sama.sandbox.base import Sandbox, SandboxError
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.github import GitHubToolError, GitHubTools
from sama.tools.repos import (
    GIT_TIMEOUT_S,
    RUN_TOKEN_ENV,
    RepoCloner,
    RepoError,
    RepoRef,
    parse_repo,
)
from sama.vault.redact import redact

MAX_DIFF_BYTES: Final = 60_000
"""The diff in a result has at most this many bytes. The full diff stays in the folder."""
MAX_DIFF_READ_BYTES: Final = 4 * MAX_DIFF_BYTES
"""Sama reads this much of the diff and redacts it before the cut to ``MAX_DIFF_BYTES``."""
MAX_STAT_LINES: Final = 60
PUSH_TIMEOUT_S: Final = 300.0
COMMIT_NAME: Final = "sama coding agent"
COMMIT_EMAIL: Final = "sama-coding-agent@users.noreply.github.com"
TARGET_CONFIG: Final = "sama.target"
"""The git config key of a clone that keeps the target branch of its pull requests."""
_SHA = re.compile(r"[0-9a-f]{40}")
_GIT_ENV: Final = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"}


class WorkspaceError(Exception):
    """The folder, the diff, or the pull request failed. The message has no secret."""


@dataclass(frozen=True, slots=True)
class Workspace:
    path: str
    repo: RepoRef | None
    base: str | None
    """The commit at the start of the request. ``None`` when the folder is not a git
    repository."""
    branch: str | None
    """The branch at the start of the request, or ``None`` (a detached HEAD or no git)."""
    proxied: bool
    """The clone uses the git proxy, so a push and a pull request are possible."""
    target: str | None = None
    """The branch that a pull request goes to: the branch of the clone. After a pull request
    the folder is on its ``sama/*`` branch, and the git config ``sama.target`` keeps it."""


@dataclass(frozen=True, slots=True)
class Diff:
    stat: str
    patch: str
    files: int
    cut: bool
    """The patch has only the first ``MAX_DIFF_BYTES`` bytes."""

    @property
    def empty(self) -> bool:
        return self.files == 0


async def _git(
    sandbox: Sandbox,
    path: str,
    *args: str,
    env: dict[str, str] | None = None,
    timeout_s: float = GIT_TIMEOUT_S,
) -> tuple[int, str, str]:
    try:
        result = await sandbox.exec(
            ["git", *args], cwd=path, env={**_GIT_ENV, **(env or {})}, timeout_s=timeout_s
        )
    except SandboxError as error:
        raise WorkspaceError(f"the sandbox failed: {error}") from None
    return result.exit_code, result.stdout, result.stderr


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return (lines[-1] if lines else "no output")[:300]


async def _state(sandbox: Sandbox, path: str) -> tuple[str | None, str | None]:
    """``(commit, branch)`` of ``path``, or ``(None, None)`` when it is not a git repository
    or has no commit."""
    code, out, _ = await _git(sandbox, path, "rev-parse", "HEAD", "--abbrev-ref", "HEAD")
    parts = out.split()
    if code != 0 or len(parts) != 2 or not _SHA.fullmatch(parts[0]):
        return None, None
    return parts[0], None if parts[1] == "HEAD" else parts[1]


async def prepare(
    sandbox: Sandbox,
    claims: RunTokenClaims,
    token: str,
    *,
    repo: str | None,
    cloner: RepoCloner | None,
    folder: str | None = None,
) -> Workspace:
    """The folder of the request. ``folder`` is the folder of the last request of the session
    (the direct mode keeps it). Without ``repo`` and ``folder``, it is the work folder."""
    workdir = sandbox.workdir.rstrip("/")
    target: RepoRef | None = None
    if repo:
        try:
            target = parse_repo(repo)
        except RepoError as error:
            raise WorkspaceError(str(error)) from None
        path = f"{workdir}/{target.name}"
    else:
        path = folder if folder and folder.startswith(workdir + "/") else workdir
    proxied = False
    commit, branch = await _state(sandbox, path) if path != workdir else (None, None)
    if target is not None and commit is None:
        if cloner is None:
            raise WorkspaceError("this server has no sandbox clone, so it cannot clone a repo")
        try:
            cloned = await cloner.clone(claims, target.url, folder=target.name, token=token)
        except RepoError as error:
            raise WorkspaceError(str(error)) from None
        return Workspace(
            cloned.path, target, cloned.commit, cloned.branch, cloned.proxied, cloned.branch
        )
    base_branch: str | None = branch
    if commit is not None:
        code, out, _ = await _git(sandbox, path, "remote", "get-url", "origin")
        url = out.strip() if code == 0 else ""
        found = _repo_of(url)
        if target is not None and (
            found is None
            or (found.owner.lower(), found.name.lower())
            != (target.owner.lower(), target.name.lower())
        ):
            # The folder has another repository with the same name. Never work in it.
            raise WorkspaceError(
                f"{path} has another repository, not {target.owner}/{target.name}. Move or "
                "remove that folder first"
            )
        target = target or found
        if target is not None and cloner is not None:
            wanted = cloner.proxy_url(target)
            proxied = bool(wanted) and url == wanted and await cloner.proxied(claims, target)
        if branch is not None and branch.startswith("sama/"):
            code, out, _ = await _git(sandbox, path, "config", "--get", TARGET_CONFIG)
            base_branch = out.strip() if code == 0 and out.strip() else None
    return Workspace(path, target, commit, branch, proxied, base_branch)


def _repo_of(url: str) -> RepoRef | None:
    """The repository of an ``origin`` URL that ends with ``/<owner>/<name>.git``."""
    match = re.search(r"/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?$", url)
    if match is None:
        return None
    try:
        return parse_repo(f"{match.group(1)}/{match.group(2)}")
    except RepoError:
        return None


async def diff(sandbox: Sandbox, space: Workspace) -> Diff | None:
    """The changes since ``space.base``, with new files. ``None`` without git."""
    if space.base is None:
        return None
    # Intent-to-add makes new files show in the diff. It stages no content.
    await _git(sandbox, space.path, "add", "--all", "--intent-to-add")
    code, stat, err = await _git(
        sandbox, space.path, "diff", "--stat=100", "--find-renames", space.base
    )
    if code != 0:
        raise WorkspaceError(f"git diff failed: {_last_line(err)}")
    stat_lines = [line for line in stat.splitlines() if line.strip()]
    files = max(len(stat_lines) - 1, 0)
    if len(stat_lines) > MAX_STAT_LINES:
        stat_lines = [*stat_lines[: MAX_STAT_LINES - 1], "...", stat_lines[-1]]
    # Read more than the cap, redact the whole read, then cut. A secret that crosses the cap
    # is thus redacted whole.
    base = shlex.quote(space.base)
    script = f"git diff --find-renames {base} | head -c {MAX_DIFF_READ_BYTES + 1}"
    try:
        result = await sandbox.exec(
            ["sh", "-c", script], cwd=space.path, env=_GIT_ENV, timeout_s=GIT_TIMEOUT_S
        )
    except SandboxError as error:
        raise WorkspaceError(f"the sandbox failed: {error}") from None
    patch, cut = capped_patch(result.stdout)
    return Diff("\n".join(stat_lines), patch, files, cut)


def capped_patch(raw: str) -> tuple[str, bool]:
    """``(patch, cut)``: the redacted diff, cut at ``MAX_DIFF_BYTES`` on a line end. A read
    that hit ``MAX_DIFF_READ_BYTES`` loses its last line before the redaction. After a cut, a
    key block that has no end line is removed from its first line on, because the redactor
    finds only a whole block."""
    cut = False
    if len(raw.encode()) > MAX_DIFF_READ_BYTES:
        raw = raw.encode()[:MAX_DIFF_READ_BYTES].decode("utf-8", "ignore")
        raw = raw[: raw.rfind("\n") + 1]
        cut = True
    patch = redact(raw)
    if len(patch.encode()) > MAX_DIFF_BYTES:
        patch = patch.encode()[:MAX_DIFF_BYTES].decode("utf-8", "ignore")
        patch = patch[: patch.rfind("\n") + 1]
        cut = True
    if cut:
        begin = patch.rfind("-----BEGIN")
        if begin != -1 and "-----END" not in patch[begin:]:
            patch = patch[: patch.rfind("\n", 0, begin) + 1]
    return patch, cut


def branch_name(agent: str, run_id: str) -> str:
    """``sama/claude-<id>`` or ``sama/codex-<id>``: one branch per request."""
    short = "claude" if agent == "claude_code" else agent
    try:
        suffix = uuid.UUID(run_id).hex[:8]
    except ValueError:
        suffix = uuid.uuid4().hex[:8]
    return f"sama/{short}-{suffix}"


async def open_pull(
    sandbox: Sandbox,
    claims: RunTokenClaims,
    token: str,
    space: Workspace,
    *,
    github: GitHubTools | None,
    branch: str,
    title: str,
    body: str,
) -> str:
    """Commits the changes to ``branch``, pushes it through the git proxy, and opens a pull
    request to the branch of the start. Returns the text of ``pr_create``."""
    if github is None:
        raise WorkspaceError("the sama GitHub App is not set up, so sama cannot open a PR")
    if space.repo is None or space.base is None:
        raise WorkspaceError("the request has no repository, so there is no pull request")
    if not space.proxied:
        raise WorkspaceError(
            f"{space.repo.owner}/{space.repo.name} is not on the allowlist of this org, so "
            "sama cannot push it. An org admin can add it"
        )
    base = space.target
    if not base or base.startswith("sama/"):
        raise WorkspaceError("the clone has no base branch to open a pull request against")
    path = space.path
    current = space.branch
    if current is not None and current.startswith("sama/"):
        # A follow-up of an earlier pull request: its branch gets the new commits.
        branch = current
    else:
        code, _, err = await _git(sandbox, path, "switch", "--quiet", "--create", branch)
        if code != 0:
            raise WorkspaceError(f"git cannot make the branch {branch}: {_last_line(err)}")
        await _git(sandbox, path, "config", TARGET_CONFIG, base)
    await _git(sandbox, path, "add", "--all")
    code, _, _ = await _git(sandbox, path, "diff", "--cached", "--quiet")
    if code != 0:
        identity = [
            "-c",
            f"user.name={COMMIT_NAME}",
            "-c",
            f"user.email={COMMIT_EMAIL}",
        ]
        code, _, err = await _git(
            sandbox, path, *identity, "commit", "--quiet", "--no-verify", "-m", title[:200]
        )
        if code != 0:
            raise WorkspaceError(f"git commit failed: {_last_line(err)}")
    code, out, _ = await _git(sandbox, path, "rev-list", "--count", f"{space.base}..HEAD")
    if code != 0 or out.strip() in ("", "0"):
        raise WorkspaceError("the request made no change, so there is no pull request")
    code, _, err = await _git(
        sandbox,
        path,
        "push",
        "--quiet",
        "origin",
        f"HEAD:refs/heads/{branch}",
        env={RUN_TOKEN_ENV: token},
        timeout_s=PUSH_TIMEOUT_S,
    )
    if code != 0:
        raise WorkspaceError(f"git push through the sama git proxy failed: {_last_line(err)}")
    try:
        return await github.create_pull(
            claims,
            f"{space.repo.owner}/{space.repo.name}",
            head=branch,
            base=base,
            title=title,
            body=body,
            draft=False,
        )
    except GitHubToolError as error:
        if branch == current and "already exists" in str(error).lower():
            return (
                f"Pushed the new commits to {branch}. Its open pull request to {base} has "
                "them. A person reviews and merges it."
            )
        raise WorkspaceError(str(error)) from None
