"""Fakes for the GitHub tests: a local git server in place of GitHub, and a memory store."""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from sama.github.proxy import UpstreamResponse, fetch_into
from sama.github.push import git_env
from sama.github.service import AllowedRepo

GIT = (
    "git",
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@example.test",
    "-c",
    "init.defaultBranch=main",
)


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        [*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    )
    return done.stdout.strip()


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _nothing() -> None:
    return None


class LocalUpstream:
    """``git http-backend`` over the bare repositories in ``root/<owner>/<name>.git``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pushes = 0

    def path(self, repo: AllowedRepo) -> Path:
        return self.root / repo.owner / f"{repo.name}.git"

    async def _cgi(
        self,
        repo: AllowedRepo,
        method: str,
        path: str,
        query: str,
        body: bytes,
        content_type: str,
        git_protocol: str | None,
    ) -> UpstreamResponse:
        env = git_env(
            {
                "GIT_PROJECT_ROOT": str(self.root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "REMOTE_USER": "sama",
                "REQUEST_METHOD": method,
                "PATH_INFO": f"/{repo.owner}/{repo.name}.git/{path}",
                "QUERY_STRING": query,
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(len(body)),
            }
        )
        if git_protocol:
            env["GIT_PROTOCOL"] = git_protocol
        process = await asyncio.create_subprocess_exec(
            "git",
            "http-backend",
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await process.communicate(body)
        head, _, rest = out.partition(b"\r\n\r\n")
        status, kind = 200, "application/octet-stream"
        for line in head.decode().split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.strip().split(" ")[0])
            elif name.lower() == "content-type":
                kind = value.strip()
        return UpstreamResponse(status, kind, _chunks(rest), _nothing)

    async def advertise(
        self, repo: AllowedRepo, service: str, git_protocol: str | None, user_agent: str
    ) -> UpstreamResponse:
        return await self._cgi(
            repo, "GET", "info/refs", f"service={service}", b"", "", git_protocol
        )

    async def rpc(
        self,
        repo: AllowedRepo,
        service: str,
        body: bytes,
        git_protocol: str | None,
        user_agent: str,
    ) -> UpstreamResponse:
        if service == "git-receive-pack":
            self.pushes += 1
        return await self._cgi(
            repo, "POST", service, "", body, f"application/x-{service}-request", git_protocol
        )

    async def is_ancestor(self, repo: AllowedRepo, old: str, new: str) -> bool:
        process = await asyncio.create_subprocess_exec(
            "git",
            "merge-base",
            "--is-ancestor",
            old,
            new,
            cwd=self.path(repo),
            env=git_env(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await process.wait() == 0

    async def fetch(self, repo: AllowedRepo, git_dir: Path, source: str, target: str) -> bool:
        return await fetch_into(git_dir, f"file://{self.path(repo)}", source, target)


@dataclass
class Grant:
    repo_id: uuid.UUID
    ref: str
    commit: str
    used: bool = False


@dataclass
class MemoryStore:
    repos: dict[tuple[uuid.UUID, str], AllowedRepo] = field(default_factory=dict)
    grants: list[Grant] = field(default_factory=list)

    def add(self, repo: AllowedRepo) -> None:
        self.repos[(repo.org_id, repo.full_name.lower())] = repo

    async def allowed(self, org_id: uuid.UUID, owner: str, name: str) -> AllowedRepo | None:
        return self.repos.get((org_id, f"{owner}/{name}".lower()))

    async def take_grant(self, repo: AllowedRepo, ref: str, commit: str) -> bool:
        for grant in self.grants:
            if (grant.repo_id, grant.ref, grant.commit, grant.used) == (
                repo.id,
                ref,
                commit,
                False,
            ):
                grant.used = True
                return True
        return False
