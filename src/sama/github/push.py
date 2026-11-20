"""The push checks of the sama git proxy (docs/architecture.md, Section 15.1).

The proxy sends a push to GitHub only when every ref update passes:

- The ref is a branch ``refs/heads/sama/<name>``.
- The update does not delete the ref.
- The update is a fast-forward: the old commit is an ancestor of the new commit.
- The protected paths of the new commit are the same as in a trusted base, or a person
  approved the push. The trusted bases are the old commit of the branch and the commit of the
  default branch. GitHub runs the workflows of the pushed commit, so the check looks at the
  tree of that commit, not at each commit of the push.

The proxy advertises ``no-thin``, so the pack of the push has every delta base. ``git
index-pack`` reads it in an empty temporary repository. The proxy fetches the trees of the
bases, without blobs, from GitHub. If a check cannot read an object, the proxy refuses the push.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from sama.github.pktline import PushRequest, RefUpdate
from sama.github.service import (
    SAMA_BRANCH_PREFIX,
    SAMA_REF_PREFIX,
    AllowedRepo,
    is_protected,
    valid_sama_branch,
)

MAX_PUSH_COMMITS: Final = 2_000
"""A push with more new commits than this is refused."""
MAX_BOUNDARY: Final = 20
"""The most parent commits outside the pack that one ref update can have."""
GIT_TIMEOUT_S: Final = 120.0
MAX_LISTED_PATHS: Final = 10
DEFAULT_BASE: Final = "refs/sama/default"


class PushCheckError(Exception):
    """The push cannot be checked. The proxy refuses it. No secret in the message."""


class TreeSource(Protocol):
    """What the checks need from GitHub."""

    async def is_ancestor(self, repo: AllowedRepo, old: str, new: str) -> bool: ...

    async def fetch(self, repo: AllowedRepo, git_dir: Path, source: str, target: str) -> bool:
        """Fetches the commit ``source`` (a commit id or ``HEAD``) and its tree, without
        blobs and without history, to the ref ``target``. False if GitHub has no such commit."""
        ...


class GrantStore(Protocol):
    async def take_grant(self, repo: AllowedRepo, ref: str, commit: str) -> bool: ...


@dataclass(slots=True)
class PushVerdict:
    rejected: dict[str, str] = field(default_factory=dict)
    """The reason for each refused ref."""
    granted: list[RefUpdate] = field(default_factory=list)
    """The updates that used a grant."""

    @property
    def ok(self) -> bool:
        return not self.rejected


def ref_problem(update: RefUpdate) -> str | None:
    """Why the proxy refuses the ref update before it reads the pack, or ``None``."""
    if not update.ref.startswith(SAMA_REF_PREFIX) or not valid_sama_branch(
        update.ref.removeprefix("refs/heads/")
    ):
        return f"sama can push only to branches named {SAMA_BRANCH_PREFIX}<name>"
    if update.deletes:
        return "sama cannot delete a branch"
    return None


def approval_hint(repo: AllowedRepo, update: RefUpdate, paths: Sequence[str]) -> str:
    listed = ", ".join(paths[:MAX_LISTED_PATHS])
    if len(paths) > MAX_LISTED_PATHS:
        listed += f" and {len(paths) - MAX_LISTED_PATHS} more"
    branch = update.ref.removeprefix("refs/heads/")
    return (
        f"the push changes protected paths ({listed}). Call repo_push_approval with "
        f"repo={repo.full_name} branch={branch} commit={update.new}, then push again"
    )


FORCE_PUSH = (
    "sama cannot force-push. Fetch the branch, put your commits on top of it, and push again"
)


async def check_push(
    push: PushRequest,
    repo: AllowedRepo,
    *,
    trees: TreeSource,
    grants: GrantStore,
) -> PushVerdict:
    verdict = PushVerdict()
    for update in push.updates:
        problem = ref_problem(update)
        if problem is not None:
            verdict.rejected[update.ref] = problem
    if verdict.rejected:
        return verdict
    touched: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory(prefix="sama-push-") as folder:
        git_dir = Path(folder) / "repo.git"
        await _git(Path(folder), "init", "--quiet", "--bare", str(git_dir))
        if push.pack:
            await _git(git_dir, "index-pack", "--stdin", stdin=push.pack)
        parents = await _commits(git_dir)
        walks: dict[str, set[str]] = {}
        for update in push.updates:
            visited, boundary = _walk(update.new, parents)
            walks[update.ref] = boundary
            if len(boundary) > MAX_BOUNDARY:
                verdict.rejected[update.ref] = "the push has too many parent commits to check"
            elif not update.creates and not await _fast_forward(
                update, visited, boundary, repo, trees
            ):
                verdict.rejected[update.ref] = FORCE_PUSH
        if verdict.rejected:
            return verdict
        default: str | None = None
        if await trees.fetch(repo, git_dir, "HEAD", DEFAULT_BASE):
            default = (await _git(git_dir, "rev-parse", DEFAULT_BASE)).decode().strip()
        for index, update in enumerate(push.updates):
            boundary = walks[update.ref]
            if update.new not in parents and not await trees.fetch(
                repo, git_dir, update.new, f"refs/sama/new{index}"
            ):
                raise PushCheckError(f"GitHub has no commit {update.new}")
            # The parents of the new commits hold the objects that the pack does not have.
            parents_out = sorted(boundary - {update.new})
            fetched = [
                commit
                for number, commit in enumerate(parents_out)
                if await trees.fetch(repo, git_dir, commit, f"refs/sama/p{index}-{number}")
            ]
            bases: list[str] = []
            if not update.creates and (
                update.old in fetched
                or await trees.fetch(repo, git_dir, update.old, f"refs/sama/old{index}")
            ):
                bases.append(update.old)
            if default is not None:
                bases.append(default)
            paths = await _protected_changes(
                git_dir,
                bases,
                update.new,
                repo.protected_paths,
                _history(fetched, default, repo, trees),
            )
            if paths:
                touched[update.ref] = paths
    for update in push.updates:
        paths = touched.get(update.ref, [])
        if not paths:
            continue
        if await grants.take_grant(repo, update.ref, update.new):
            verdict.granted.append(update)
        else:
            verdict.rejected[update.ref] = approval_hint(repo, update, paths)
    return verdict


async def _history(
    commits: Sequence[str], default: str | None, repo: AllowedRepo, trees: TreeSource
) -> AsyncIterator[str]:
    """The parent commits of the push that are in the history of the default branch. They are
    trusted bases too: a branch that starts from an older commit of the default branch has the
    protected paths of that commit."""
    if default is None:
        return
    for commit in commits:
        if await trees.is_ancestor(repo, commit, default):
            yield commit


async def _protected_changes(
    git_dir: Path,
    bases: Sequence[str],
    new: str,
    protected: Sequence[str],
    more: AsyncIterator[str],
) -> list[str]:
    """The protected paths that differ between ``new`` and the closest trusted base. An empty
    list if one base has the same protected paths. With no base (a new branch in a repository
    with no default branch), every protected path in ``new`` counts. The bases in ``more``
    are read only when the first bases do not match."""
    found: list[str] | None = None

    async def compare(base: str | None) -> bool:
        nonlocal found
        try:
            paths = await _changed_paths(git_dir, base, new)
        except PushCheckError:
            return False
        changed = sorted(path for path in paths if _protects(path, protected))
        if found is None or len(changed) < len(found):
            found = changed
        return not changed

    if not bases:
        await compare(None)
    for base in bases:
        if await compare(base):
            return []
    async for base in more:
        if await compare(base):
            return []
    if found is None:
        raise PushCheckError("sama cannot read the trees of the push")
    return found


def _protects(path: str, protected: Iterable[str]) -> bool:
    """A protected path, a path in a protected folder, or a folder that holds a protected
    path (a symlink in place of ``.github`` changes what ``.github/workflows`` is)."""
    if is_protected(path, protected):
        return True
    folder = path.lower().rstrip("/") + "/"
    return any(rule.lower().startswith(folder) for rule in protected)


def _walk(new: str, parents: Mapping[str, tuple[str, ...]]) -> tuple[set[str], set[str]]:
    """The commits of the pack that ``new`` reaches, and the first commits outside the pack."""
    visited: set[str] = set()
    boundary: set[str] = set()
    stack = [new]
    while stack:
        commit = stack.pop()
        if commit in visited or commit in boundary:
            continue
        if commit not in parents:
            boundary.add(commit)
            continue
        visited.add(commit)
        stack.extend(parents[commit])
    return visited, boundary


async def _fast_forward(
    update: RefUpdate,
    visited: set[str],
    boundary: set[str],
    repo: AllowedRepo,
    trees: TreeSource,
) -> bool:
    if update.old in visited or update.old in boundary:
        return True
    for commit in sorted(boundary):
        if await trees.is_ancestor(repo, update.old, commit):
            return True
    return False


async def _commits(git_dir: Path) -> dict[str, tuple[str, ...]]:
    """The commits of the repository and their parents."""
    listed = await _git(
        git_dir,
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname) %(objecttype)",
    )
    commits = [
        line.split(" ")[0] for line in listed.decode().splitlines() if line.endswith(" commit")
    ]
    if len(commits) > MAX_PUSH_COMMITS:
        raise PushCheckError(f"the push has more than {MAX_PUSH_COMMITS} new commits")
    if not commits:
        return {}
    raw = await _git(git_dir, "cat-file", "--batch", stdin="\n".join(commits).encode() + b"\n")
    parents: dict[str, tuple[str, ...]] = {}
    offset = 0
    while offset < len(raw):
        end = raw.index(b"\n", offset)
        header = raw[offset:end].decode().split(" ")
        size = int(header[2])
        body = raw[end + 1 : end + 1 + size]
        offset = end + 1 + size + 1
        found: list[str] = []
        for line in body.split(b"\n"):
            if not line:
                break
            if line.startswith(b"parent "):
                found.append(line[7:].decode())
        parents[header[0]] = tuple(found)
    return parents


async def _changed_paths(git_dir: Path, base: str | None, new: str) -> set[str]:
    """The paths that differ between the trees of ``base`` and ``new``. With no base, every
    path of ``new``."""
    if base is None:
        args = ["ls-tree", "-r", "--name-only", "-z", new]
    else:
        args = ["diff-tree", "-r", "--no-renames", "--name-only", "-z", base, new]
    raw = await _git(git_dir, *args)
    return {token.decode("utf-8", "surrogateescape") for token in raw.split(b"\0") if token}


def git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A clean environment for git on the control plane: no user or system config, no
    prompt, and no hooks."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tempfile.gettempdir(),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "LC_ALL": "C",
    }
    if extra:
        env.update(extra)
    return env


async def _git(
    cwd: Path,
    *args: str,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> bytes:
    return await run_git(cwd, list(args), stdin=stdin, env=env)


async def run_git(
    cwd: Path,
    args: list[str],
    *,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        *args,
        cwd=cwd,
        env=git_env(env),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(GIT_TIMEOUT_S):
            out, err = await process.communicate(stdin)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise PushCheckError(f"git {args[0]} took too long") from None
    if process.returncode != 0:
        detail = err.decode("utf-8", "replace").strip().splitlines()
        last = detail[-1][:300] if detail else "no output"
        raise PushCheckError(f"git {args[0]} failed: {last}")
    return out
