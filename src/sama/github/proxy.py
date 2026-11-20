"""The sama git proxy (docs/architecture.md, Section 15.1).

The sandbox clones, fetches, and pushes the repositories of the allowlist through this proxy
with the run token of the run. The proxy holds the GitHub installation tokens. The sandbox
never sees them.

- ``GET  /<owner>/<name>.git/info/refs?service=git-upload-pack|git-receive-pack``
- ``POST /<owner>/<name>.git/git-upload-pack``
- ``POST /<owner>/<name>.git/git-receive-pack``

A fetch goes to GitHub as it is. A push goes to GitHub only after the checks of
``sama.github.push``. If a check fails, the proxy answers with a ``report-status`` that
refuses every ref, so git shows the reason to the agent.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import uuid
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from sama.github.client import WEB_URL, GitHubClient, GitHubError
from sama.github.pktline import PktLineError, parse_push, report, rewrite_advertisement
from sama.github.push import GrantStore, PushCheckError, TreeSource, check_push, run_git
from sama.github.service import AllowedRepo
from sama.tenancy.run_token import InvalidRunTokenError, RunTokenClaims, RunTokenSigner

log = logging.getLogger("sama.github.proxy")

UPLOAD: Final = "git-upload-pack"
RECEIVE: Final = "git-receive-pack"
SERVICES: Final = frozenset({UPLOAD, RECEIVE})
MAX_FETCH_REQUEST_BYTES: Final = 16 * 1024 * 1024
MAX_PUSH_BYTES: Final = 100 * 1024 * 1024
"""GitHub refuses a push over 2 GB. Sama keeps pushes much smaller, because the proxy reads
the whole push before it sends it on."""
MAX_ADVERTISEMENT_BYTES: Final = 16 * 1024 * 1024
MAX_PUSH_CHECKS: Final = 4
"""The most push checks that run at the same time."""
FETCH_TIMEOUT_S: Final = 120.0
_SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,100}")
_USER_AGENT = re.compile(r"git/[ -~]{1,100}")


class RepoStore(GrantStore, Protocol):
    async def allowed(self, org_id: uuid.UUID, owner: str, name: str) -> AllowedRepo | None: ...


@dataclass(slots=True)
class UpstreamResponse:
    status: int
    content_type: str
    chunks: AsyncIterator[bytes]
    close: Callable[[], Awaitable[None]]

    async def read(self, limit: int) -> bytes:
        out = bytearray()
        try:
            async for chunk in self.chunks:
                out += chunk
                if len(out) > limit:
                    raise ProxyError(502, "the answer of GitHub is too large")
        finally:
            await self.close()
        return bytes(out)


class Upstream(TreeSource, Protocol):
    """The git server behind the proxy."""

    async def advertise(
        self, repo: AllowedRepo, service: str, git_protocol: str | None, user_agent: str
    ) -> UpstreamResponse: ...

    async def rpc(
        self,
        repo: AllowedRepo,
        service: str,
        body: bytes,
        git_protocol: str | None,
        user_agent: str,
    ) -> UpstreamResponse: ...


class ProxyError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class GitProxy:
    """The Starlette app of the proxy."""

    def __init__(self, signer: RunTokenSigner, store: RepoStore, upstream: Upstream) -> None:
        self._signer = signer
        self._store = store
        self._upstream = upstream
        self._checks = asyncio.Semaphore(MAX_PUSH_CHECKS)

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/{owner}/{name}/info/refs", self._info_refs, methods=["GET"]),
                Route("/{owner}/{name}/git-upload-pack", self._upload, methods=["POST"]),
                Route("/{owner}/{name}/git-receive-pack", self._receive, methods=["POST"]),
            ]
        )

    async def _info_refs(self, request: Request) -> Response:
        service = request.query_params.get("service", "")
        if service not in SERVICES:
            return PlainTextResponse("sama supports only the smart HTTP protocol of git", 403)
        try:
            repo = await self._repo(request)
            if service == UPLOAD:
                answer = await self._upstream.advertise(
                    repo, service, request.headers.get("git-protocol"), _agent(request)
                )
                return _stream(answer)
            # A push uses protocol v0, so the proxy can read and change the capabilities.
            answer = await self._upstream.advertise(repo, service, None, _agent(request))
            body = await answer.read(MAX_ADVERTISEMENT_BYTES)
            if answer.status != 200:
                return _upstream_problem(answer.status)
            return Response(
                rewrite_advertisement(body),
                media_type=f"application/x-{service}-advertisement",
                headers={"cache-control": "no-cache"},
            )
        except ProxyError as error:
            return _problem(error)
        except PktLineError:
            return PlainTextResponse("sama cannot read the answer of GitHub", 502)

    async def _upload(self, request: Request) -> Response:
        try:
            repo = await self._repo(request)
            body = await _body(request, UPLOAD, MAX_FETCH_REQUEST_BYTES)
            answer = await self._upstream.rpc(
                repo, UPLOAD, body, request.headers.get("git-protocol"), _agent(request)
            )
            return _stream(answer)
        except ProxyError as error:
            return _problem(error)

    async def _receive(self, request: Request) -> Response:
        try:
            claims, repo = await self._authorize(request)
            body = await _body(request, RECEIVE, MAX_PUSH_BYTES)
            try:
                push = parse_push(body)
            except PktLineError as error:
                raise ProxyError(400, str(error)) from None
            _check_capabilities(push.capabilities)
            async with self._checks:
                try:
                    verdict = await check_push(push, repo, trees=self._upstream, grants=self._store)
                except PushCheckError as error:
                    reason = f"sama cannot check the push: {error}"
                    rejected = {update.ref: reason for update in push.updates}
                else:
                    rejected = verdict.rejected
            refs = [update.ref for update in push.updates]
            if rejected:
                log.info(
                    "push refused",
                    extra={"repo": repo.full_name, "refs": refs, "run_id": claims.run_id},
                )
                return Response(
                    report(rejected.items(), push.updates),
                    media_type=f"application/x-{RECEIVE}-result",
                    headers={"cache-control": "no-cache"},
                )
            log.info(
                "push sent", extra={"repo": repo.full_name, "refs": refs, "run_id": claims.run_id}
            )
            answer = await self._upstream.rpc(repo, RECEIVE, body, None, _agent(request))
            return _stream(answer)
        except ProxyError as error:
            return _problem(error)

    async def _repo(self, request: Request) -> AllowedRepo:
        return (await self._authorize(request))[1]

    async def _authorize(self, request: Request) -> tuple[RunTokenClaims, AllowedRepo]:
        token = _credential(request)
        if token is None:
            raise ProxyError(401, "sama needs the run token")
        try:
            claims = self._signer.verify(token)
        except InvalidRunTokenError:
            raise ProxyError(401, "the run token is not valid") from None
        owner = request.path_params["owner"]
        name = str(request.path_params["name"]).removesuffix(".git")
        if not _SEGMENT.fullmatch(owner) or not _SEGMENT.fullmatch(name):
            raise ProxyError(404, "no such repository")
        try:
            org_id = uuid.UUID(claims.org_id)
        except ValueError:
            raise ProxyError(401, "the run token is not valid") from None
        repo = await self._store.allowed(org_id, owner, name)
        if repo is None:
            raise ProxyError(
                404, f"{owner}/{name} is not on the allowlist of the org. Ask an org admin"
            )
        return claims, repo


def _check_capabilities(capabilities: frozenset[str]) -> None:
    if "report-status" not in capabilities:
        raise ProxyError(400, "the git client must ask for report-status")
    for capability in capabilities:
        name, _, value = capability.partition("=")
        if name == "object-format" and value != "sha1":
            raise ProxyError(400, "sama supports only SHA-1 repositories")
        if name in {"push-options", "push-cert", "side-band", "side-band-64k", "report-status-v2"}:
            raise ProxyError(400, f"sama does not support the {name} capability")


def _credential(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(value.strip(), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return None
        _, _, password = decoded.partition(":")
        return password or None
    return None


def _agent(request: Request) -> str:
    agent = request.headers.get("user-agent", "")
    return agent if _USER_AGENT.fullmatch(agent) else "git/2.43.0"


async def _body(request: Request, service: str, limit: int) -> bytes:
    if request.headers.get("content-type", "") != f"application/x-{service}-request":
        raise ProxyError(415, f"the request must have the type application/x-{service}-request")
    encoding = request.headers.get("content-encoding", "").lower()
    if encoding not in {"", "identity", "gzip"}:
        raise ProxyError(415, "sama supports only gzip request bodies")
    inflate = zlib.decompressobj(wbits=31) if encoding == "gzip" else None
    out = bytearray()
    async for chunk in request.stream():
        if inflate is None:
            out += chunk
        else:
            try:
                out += inflate.decompress(chunk, limit + 1 - len(out))
            except zlib.error:
                raise ProxyError(400, "the gzip body is not valid") from None
            if inflate.unconsumed_tail:
                raise ProxyError(413, "the request is too large")
        if len(out) > limit:
            raise ProxyError(413, "the request is too large")
    return bytes(out)


def _stream(answer: UpstreamResponse) -> Response:
    if answer.status != 200:
        task = BackgroundTask(answer.close)
        return PlainTextResponse(
            _upstream_text(answer.status), _upstream_status(answer.status), background=task
        )
    return StreamingResponse(
        answer.chunks,
        media_type=answer.content_type,
        headers={"cache-control": "no-cache"},
        background=BackgroundTask(answer.close),
    )


def _upstream_status(status: int) -> int:
    return 404 if status == 404 else 502


def _upstream_text(status: int) -> str:
    return f"GitHub answered {status}"


def _upstream_problem(status: int) -> Response:
    return PlainTextResponse(_upstream_text(status), _upstream_status(status))


def _problem(error: ProxyError) -> Response:
    headers = {"www-authenticate": 'Basic realm="sama"'} if error.status == 401 else None
    return PlainTextResponse(str(error) + "\n", error.status, headers=headers)


class GitHubUpstream:
    """GitHub, with the installation token of the repository."""

    def __init__(
        self,
        client: GitHubClient,
        *,
        http: httpx.AsyncClient | None = None,
        web_url: str | None = None,
    ) -> None:
        self._client = client
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(FETCH_TIMEOUT_S, connect=20.0), follow_redirects=False
        )
        self._web = (web_url or client.config.web_url or WEB_URL).rstrip("/")

    async def aclose(self) -> None:
        await self._http.aclose()

    def _url(self, repo: AllowedRepo) -> str:
        return f"{self._web}/{repo.owner}/{repo.name}.git"

    async def _auth(self, repo: AllowedRepo) -> str:
        try:
            token = await self._client.installation_token(repo.installation_id)
        except GitHubError as error:
            raise ProxyError(502, str(error)) from None
        return "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()

    async def advertise(
        self, repo: AllowedRepo, service: str, git_protocol: str | None, user_agent: str
    ) -> UpstreamResponse:
        headers = {"user-agent": user_agent, "authorization": await self._auth(repo)}
        if git_protocol:
            headers["git-protocol"] = git_protocol
        return await self._send(
            "GET", f"{self._url(repo)}/info/refs?service={service}", headers, None
        )

    async def rpc(
        self,
        repo: AllowedRepo,
        service: str,
        body: bytes,
        git_protocol: str | None,
        user_agent: str,
    ) -> UpstreamResponse:
        headers = {
            "user-agent": user_agent,
            "authorization": await self._auth(repo),
            "content-type": f"application/x-{service}-request",
            "accept": f"application/x-{service}-result",
        }
        if git_protocol:
            headers["git-protocol"] = git_protocol
        return await self._send("POST", f"{self._url(repo)}/{service}", headers, body)

    async def _send(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> UpstreamResponse:
        request = self._http.build_request(method, url, headers=headers, content=body)
        try:
            response = await self._http.send(request, stream=True)
        except httpx.HTTPError as error:
            raise ProxyError(502, f"GitHub cannot be reached: {type(error).__name__}") from None
        return UpstreamResponse(
            status=response.status_code,
            content_type=response.headers.get("content-type", "application/octet-stream"),
            chunks=response.aiter_bytes(),
            close=response.aclose,
        )

    async def is_ancestor(self, repo: AllowedRepo, old: str, new: str) -> bool:
        try:
            return await self._client.is_ancestor(
                repo.installation_id, repo.owner, repo.name, old, new
            )
        except GitHubError as error:
            raise PushCheckError(str(error)) from None

    async def fetch(self, repo: AllowedRepo, git_dir: Path, source: str, target: str) -> bool:
        # The token goes to git in the environment, not in the arguments, so no process list
        # and no error message shows it.
        env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: {await self._auth(repo)}",
        }
        return await fetch_into(git_dir, self._url(repo), source, target, env=env)


async def fetch_into(
    git_dir: Path,
    url: str,
    source: str,
    target: str,
    *,
    env: dict[str, str] | None = None,
) -> bool:
    """Fetches one commit and its tree, without blobs and without history. False if the fetch
    fails."""
    try:
        await run_git(
            git_dir,
            [
                "-c",
                "protocol.allow=never",
                "-c",
                f"protocol.{url.partition(':')[0]}.allow=always",
                "fetch",
                "--quiet",
                "--depth=1",
                "--filter=blob:none",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-auto-gc",
                url,
                f"+{source}:{target}",
            ],
            env=env,
        )
    except PushCheckError:
        return False
    return True
