"""``repo_clone`` through the sama git proxy (docs/architecture.md, Section 15.1).

The control plane clones in the sandbox with the run token in the environment of ``git clone``
only. The clone keeps the proxy credential helper, which reads ``$SAMA_RUN_TOKEN`` when git
runs. The token is not in the git config of the clone.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from sama.github.proxy import GitProxy
from sama.github.service import AllowedRepo
from sama.runtime.contract import Audience, RunScope
from sama.sandbox import Sandbox, SandboxSpec
from sama.sandbox.local import LocalProvider
from sama.tenancy import RunTokenClaims, RunTokenSigner
from sama.tools import RepoCloner, RepoError
from sama.tools.repos import PROXY_CREDENTIAL_HELPER, render
from tests.conformance.scripted_model import serve
from tests.github.support import LocalUpstream, MemoryStore, git


class Access:
    """The allowlist of ``MemoryStore``, in the shape that ``RepoCloner`` reads."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.asked: list[tuple[str, str, str]] = []

    async def allowed(self, org_id: str, owner: str, name: str) -> bool:
        self.asked.append((org_id, owner, name))
        return await self.store.allowed(uuid.UUID(org_id), owner, name) is not None


@dataclass
class World:
    sandbox: Sandbox
    cloner: RepoCloner
    claims: RunTokenClaims
    token: str
    bare: Path
    access: Access


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q")
    (seed / "app.py").write_text("print(1)\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "first")
    root = tmp_path / "github"
    (root / "acme").mkdir(parents=True)
    for name in ("demo", "public"):
        git(root / "acme", "clone", "-q", "--bare", str(seed), f"{name}.git")
    bare = root / "acme" / "demo.git"
    for key in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):
        git(bare, "config", key, "true")

    signer = RunTokenSigner(secrets.token_bytes(32))
    scope = RunScope(
        org_id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        audience=Audience.PRIVATE,
        user_id=str(uuid.uuid4()),
    )
    token = signer.issue(scope, ttl_s=600)
    claims = signer.verify(token)
    store = MemoryStore()
    store.add(AllowedRepo(uuid.uuid4(), uuid.UUID(scope.org_id), 7, "acme", "demo", ()))
    access = Access(store)

    (tmp_path / "sandboxes").mkdir()
    sandbox = await LocalProvider(tmp_path / "sandboxes").create(
        SandboxSpec(org_id=scope.org_id, session_id=scope.session_id, template="t")
    )

    async def session_sandbox(org_id: str, session_id: str) -> Sandbox | None:
        if (org_id, session_id) == (scope.org_id, scope.session_id):
            return sandbox
        return None

    proxy = GitProxy(signer, store, LocalUpstream(root))
    with serve(proxy.app()) as url:
        cloner = RepoCloner(
            session_sandbox, source=f"file://{root}", proxy=f"{url}/", access=access
        )
        yield World(sandbox, cloner, claims, token, bare, access)
    await sandbox.kill()


async def test_an_allowlisted_repo_clones_through_the_proxy(world: World) -> None:
    cloned = await world.cloner.clone(world.claims, "acme/demo", token=world.token)
    assert cloned.proxied is True
    assert world.access.asked == [(world.claims.org_id, "acme", "demo")]
    path = Path(cloned.path)
    assert (path / "app.py").read_text() == "print(1)\n"
    config = (path / ".git" / "config").read_text()
    assert world.token not in config, "the run token is not written to the clone"
    remote = await world.sandbox.exec(["git", "remote", "get-url", "origin"], cwd=cloned.path)
    assert remote.stdout.strip().endswith("/acme/demo.git")
    assert not remote.stdout.strip().startswith("file://")
    helpers = await world.sandbox.exec(
        ["git", "config", "--get-all", "credential.helper"], cwd=cloned.path
    )
    assert helpers.stdout.splitlines() == ["", PROXY_CREDENTIAL_HELPER]

    text = render(cloned)
    assert "sama git proxy" in text
    assert "sama/<short-name>" in text
    assert "repo_push_approval" in text
    assert "You cannot push" not in text


async def test_a_later_fetch_reads_the_token_from_the_environment(world: World) -> None:
    cloned = await world.cloner.clone(world.claims, "acme/demo", token=world.token)
    env = {"GIT_TERMINAL_PROMPT": "0", "SAMA_RUN_TOKEN": world.token}
    fetched = await world.sandbox.exec(["git", "fetch", "--quiet"], cwd=cloned.path, env=env)
    assert fetched.exit_code == 0, fetched.stderr
    no_token = await world.sandbox.exec(
        ["git", "fetch", "--quiet"], cwd=cloned.path, env={"GIT_TERMINAL_PROMPT": "0"}
    )
    assert no_token.exit_code != 0


async def test_a_proxied_clone_needs_the_run_token(world: World) -> None:
    with pytest.raises(RepoError, match="no run token"):
        await world.cloner.clone(world.claims, "acme/demo")
    folder = await world.sandbox.exec(["test", "-e", "demo"])
    assert folder.exit_code != 0, "no folder is left"


async def test_a_bad_token_fails_through_the_proxy_without_the_token(world: World) -> None:
    bad = world.token[:-4] + "AAAA"
    with pytest.raises(RepoError, match="sama git proxy") as error:
        await world.cloner.clone(world.claims, "acme/demo", token=bad)
    assert bad not in str(error.value)


async def test_a_repo_off_the_allowlist_clones_without_credentials(world: World) -> None:
    cloned = await world.cloner.clone(world.claims, "acme/public", token=world.token)
    assert cloned.proxied is False
    config = (Path(cloned.path) / ".git" / "config").read_text()
    assert world.token not in config
    assert "SAMA_RUN_TOKEN" not in config
    assert "You cannot push this repository" in render(cloned)
