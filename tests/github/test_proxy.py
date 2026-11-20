"""The sama git proxy with the real git client (docs/architecture.md, Section 15.1).

A local ``git http-backend`` takes the place of GitHub, and a memory store takes the place of
the database. Each test pushes with ``git push`` through the proxy, as the agent does in the
sandbox.
"""

from __future__ import annotations

import secrets
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from sama.github.proxy import GitProxy
from sama.github.push import git_env
from sama.github.service import AllowedRepo
from sama.runtime.contract import Audience, RunScope
from sama.tenancy import RunTokenSigner
from tests.conformance.scripted_model import serve
from tests.github.support import GIT, Grant, LocalUpstream, MemoryStore, git

ORG = uuid.uuid4()
OTHER_ORG = uuid.uuid4()
HELPER = (
    '!f() { test "$1" = get || exit 0; echo username=sama; echo "password=${SAMA_RUN_TOKEN}"; }; f'
)


@dataclass
class World:
    url: str
    upstream: LocalUpstream
    store: MemoryStore
    repo: AllowedRepo
    bare: Path
    tmp: Path
    token: str
    signer: RunTokenSigner

    def env(self, token: str | None = None) -> dict[str, str]:
        return git_env({"SAMA_RUN_TOKEN": self.token if token is None else token})

    def clone(self, folder: str = "work", token: str | None = None, repo: str = "acme/demo") -> Path:
        target = self.tmp / folder
        run(
            self.tmp,
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper={HELPER}",
            "clone",
            "--quiet",
            f"{self.url}/{repo}.git",
            str(target),
            env=self.env(token),
        )
        run(target, "config", "credential.helper", "")
        run(target, "config", "--add", "credential.helper", HELPER)
        return target

    def push(self, work: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*GIT, "push", "--porcelain", "origin", *args],
            cwd=work,
            capture_output=True,
            text=True,
            env=self.env(),
        )

    def upstream_ref(self, ref: str) -> str | None:
        done = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=self.bare,
            capture_output=True,
            text=True,
        )
        return done.stdout.strip() or None


def run(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        [*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True, env=env or git_env()
    )
    return done.stdout.strip()


def commit(work: Path, files: dict[str, str], message: str = "change") -> str:
    for name, text in files.items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    run(work, "add", "-A")
    run(work, "commit", "-q", "-m", message)
    return run(work, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q")
    commit(seed, {"app.py": "print(1)\n", ".github/workflows/ci.yml": "on: push\n"}, "first")
    git(seed, "checkout", "-q", "-b", "other")
    commit(seed, {".github/workflows/ci.yml": "on: push\nrun: steal\n"}, "evil")
    git(seed, "checkout", "-q", "main")
    root = tmp_path / "github"
    (root / "acme").mkdir(parents=True)
    git(root / "acme", "clone", "-q", "--bare", str(seed), "demo.git")
    bare = root / "acme" / "demo.git"
    git(bare, "fetch", "-q", str(seed), "other:other")
    for key in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):
        git(bare, "config", key, "true")
    repo = AllowedRepo(uuid.uuid4(), ORG, 7, "acme", "demo", ("secrets/",))
    store = MemoryStore()
    store.add(repo)
    signer = RunTokenSigner(secrets.token_bytes(32))
    token = signer.issue(_scope(ORG), ttl_s=600)
    upstream = LocalUpstream(root)
    proxy = GitProxy(signer, store, upstream)
    with serve(proxy.app()) as url:
        yield World(url, upstream, store, repo, bare, tmp_path, token, signer)


def _scope(org: uuid.UUID) -> RunScope:
    return RunScope(
        org_id=str(org),
        workspace_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        audience=Audience.PRIVATE,
        user_id=str(uuid.uuid4()),
    )


# -- fetch and an allowed push ----------------------------------------------------------------


def test_clone_and_push_a_sama_branch(world: World) -> None:
    work = world.clone()
    assert (work / "app.py").read_text() == "print(1)\n"
    run(work, "checkout", "-q", "-b", "sama/fix")
    first = commit(work, {"app.py": "print(2)\n"})
    done = world.push(work, "sama/fix")
    assert done.returncode == 0, done.stderr
    assert world.upstream_ref("refs/heads/sama/fix") == first
    second = commit(work, {"app.py": "print(3)\n"})
    done = world.push(work, "sama/fix")
    assert done.returncode == 0, done.stderr
    assert world.upstream_ref("refs/heads/sama/fix") == second


def test_fetch_after_clone_uses_the_proxy(world: World) -> None:
    work = world.clone()
    seed = world.tmp / "seed"
    git(seed, "checkout", "-q", "main")
    new = commit(seed, {"more.txt": "x\n"})
    git(world.bare, "fetch", "-q", str(seed), "main:main")
    run(work, "fetch", "--quiet", "origin", env=world.env())
    assert run(work, "rev-parse", "origin/main") == new


# -- refused pushes ---------------------------------------------------------------------------


def _refused(done: subprocess.CompletedProcess[str], text: str) -> None:
    assert done.returncode != 0
    assert text in done.stdout + done.stderr, done.stdout + done.stderr


def test_push_to_the_default_branch_is_refused(world: World) -> None:
    work = world.clone()
    before = world.upstream_ref("refs/heads/main")
    commit(work, {"app.py": "print(2)\n"})
    _refused(world.push(work, "main"), "sama can push only to branches named sama/<name>")
    assert world.upstream_ref("refs/heads/main") == before
    assert world.upstream.pushes == 0


@pytest.mark.parametrize("ref", ["refs/tags/v1", "refs/heads/feature", "refs/heads/samafoo"])
def test_push_to_other_refs_is_refused(world: World, ref: str) -> None:
    work = world.clone()
    commit(work, {"app.py": "print(2)\n"})
    _refused(world.push(work, f"HEAD:{ref}"), "sama can push only to branches named")
    assert world.upstream.pushes == 0


def test_delete_is_refused(world: World) -> None:
    work = world.clone()
    run(work, "checkout", "-q", "-b", "sama/gone")
    tip = commit(work, {"app.py": "print(2)\n"})
    assert world.push(work, "sama/gone").returncode == 0
    _refused(world.push(work, ":sama/gone"), "sama cannot delete a branch")
    assert world.upstream_ref("refs/heads/sama/gone") == tip


def test_force_push_is_refused(world: World) -> None:
    work = world.clone()
    run(work, "checkout", "-q", "-b", "sama/fix")
    tip = commit(work, {"app.py": "print(2)\n"})
    assert world.push(work, "sama/fix").returncode == 0
    run(work, "reset", "-q", "--hard", "HEAD~1")
    commit(work, {"app.py": "print(3)\n"})
    _refused(world.push(work, "--force", "sama/fix"), "sama cannot force-push")
    assert world.upstream_ref("refs/heads/sama/fix") == tip


def test_force_push_to_an_older_commit_is_refused(world: World) -> None:
    work = world.clone()
    run(work, "checkout", "-q", "-b", "sama/fix")
    commit(work, {"app.py": "print(2)\n"})
    tip = commit(work, {"app.py": "print(3)\n"})
    assert world.push(work, "sama/fix").returncode == 0
    _refused(world.push(work, "--force", "HEAD~1:sama/fix"), "sama cannot force-push")
    assert world.upstream_ref("refs/heads/sama/fix") == tip


@pytest.mark.parametrize(
    "files",
    [
        {".github/workflows/ci.yml": "on: push\nrun: steal\n"},
        {".github/workflows/new.yml": "on: push\n"},
        {".github/actions/a/action.yml": "runs: x\n"},
        {"CODEOWNERS": "* @someone\n"},
        {".GitHub/Workflows/x.yml": "on: push\n"},
        {"secrets/key.txt": "x\n"},
    ],
)
def test_protected_path_needs_an_approval(world: World, files: dict[str, str]) -> None:
    work = world.clone()
    run(work, "checkout", "-q", "-b", "sama/ci")
    tip = commit(work, files)
    done = world.push(work, "sama/ci")
    _refused(done, "Call repo_push_approval with repo=acme/demo branch=sama/ci")
    assert tip in done.stdout
    assert world.upstream_ref("refs/heads/sama/ci") is None
    assert world.upstream.pushes == 0


def test_a_symlink_in_place_of_the_github_folder_needs_an_approval(world: World) -> None:
    work = world.clone()
    run(work, "checkout", "-q", "-b", "sama/link")
    run(work, "rm", "-q", "-r", ".github")
    (work / ".github").symlink_to("elsewhere")
    run(work, "add", "-A")
    run(work, "commit", "-q", "-m", "link")
    _refused(world.push(work, "sama/link"), "repo_push_approval")


def test_a_deleted_workflow_needs_an_approval(world: World) -> None:
    work = world.clone()
    run(work, "checkout", "-q", "-b", "sama/rm")
    run(work, "rm", "-q", ".github/workflows/ci.yml")
    run(work, "commit", "-q", "-m", "rm")
    _refused(world.push(work, "sama/rm"), ".github/workflows/ci.yml")


def test_a_grant_lets_one_push_through(world: World) -> None:
    work = world.clone()
    run(work, "checkout", "-q", "-b", "sama/ci")
    tip = commit(work, {".github/workflows/ci.yml": "on: pull_request\n"})
    world.store.grants.append(Grant(world.repo.id, "refs/heads/sama/ci", tip))
    done = world.push(work, "sama/ci")
    assert done.returncode == 0, done.stderr
    assert world.upstream_ref("refs/heads/sama/ci") == tip
    assert world.store.grants[0].used
    # The branch has the change now, so a later push without a workflow change needs no grant.
    later = commit(work, {"app.py": "print(5)\n"})
    assert world.push(work, "sama/ci").returncode == 0
    assert world.upstream_ref("refs/heads/sama/ci") == later


def test_a_grant_for_another_commit_does_not_help(world: World) -> None:
    work = world.clone()
    run(work, "checkout", "-q", "-b", "sama/ci")
    first = commit(work, {".github/workflows/ci.yml": "on: pull_request\n"})
    world.store.grants.append(Grant(world.repo.id, "refs/heads/sama/ci", first))
    commit(work, {".github/workflows/ci.yml": "run: steal\n"})
    _refused(world.push(work, "sama/ci"), "repo_push_approval")
    assert not world.store.grants[0].used


def test_a_change_that_is_put_back_needs_no_approval(world: World) -> None:
    work = world.clone()
    run(work, "checkout", "-q", "-b", "sama/back")
    commit(work, {".github/workflows/ci.yml": "run: steal\n"})
    tip = commit(work, {".github/workflows/ci.yml": "on: push\n"})
    assert world.push(work, "sama/back").returncode == 0
    assert world.upstream_ref("refs/heads/sama/back") == tip


def test_a_branch_from_an_older_default_commit_needs_no_approval(world: World) -> None:
    work = world.clone()
    seed = world.tmp / "seed"
    git(seed, "checkout", "-q", "main")
    commit(seed, {".github/workflows/ci.yml": "on: [push, pull_request]\n"}, "human change")
    git(world.bare, "fetch", "-q", str(seed), "main:main")
    run(work, "checkout", "-q", "-b", "sama/old-base")
    tip = commit(work, {"app.py": "print(9)\n"})
    done = world.push(work, "sama/old-base")
    assert done.returncode == 0, done.stdout + done.stderr
    assert world.upstream_ref("refs/heads/sama/old-base") == tip


def test_a_commit_from_another_branch_needs_an_approval(world: World) -> None:
    work = world.clone()
    evil = world.upstream_ref("refs/heads/other")
    assert evil is not None
    run(work, "fetch", "--quiet", "origin", "other", env=world.env())
    done = world.push(work, f"{evil}:refs/heads/sama/evil")
    _refused(done, "repo_push_approval")
    assert world.upstream_ref("refs/heads/sama/evil") is None


def test_a_bad_ref_refuses_the_whole_push(world: World) -> None:
    work = world.clone()
    run(work, "checkout", "-q", "-b", "sama/ok")
    commit(work, {"app.py": "print(2)\n"})
    done = world.push(work, "sama/ok", "HEAD:main")
    _refused(done, "another ref of the push was rejected")
    assert world.upstream_ref("refs/heads/sama/ok") is None
    assert world.upstream.pushes == 0


# -- access -----------------------------------------------------------------------------------


def test_a_bad_token_cannot_clone(world: World) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        world.clone(token="not-a-token")


def test_a_token_of_another_org_cannot_clone(world: World) -> None:
    token = world.signer.issue(_scope(OTHER_ORG), ttl_s=600)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        world.clone(token=token)
    assert "not on the allowlist" in str(raised.value.stderr) or raised.value.returncode != 0


def test_a_repo_off_the_allowlist_cannot_clone(world: World) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        world.clone(repo="acme/secret")


def test_a_dumb_http_request_is_refused(world: World) -> None:
    import httpx

    answer = httpx.get(
        f"{world.url}/acme/demo.git/info/refs",
        headers={"authorization": f"Bearer {world.token}"},
    )
    assert answer.status_code == 403
    answer = httpx.get(f"{world.url}/acme/demo.git/info/refs?service=git-upload-pack")
    assert answer.status_code == 401
    assert answer.headers["www-authenticate"] == 'Basic realm="sama"'
