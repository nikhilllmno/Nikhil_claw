"""The token relay: a local forwarder that puts the token of the current run on each call.

A kept runtime (Section 4.4, "Kept runtime") lives longer than one run. Its Claude Code CLI
and its MCP connections are made once. But the run token is per run (decision D6): the
model proxy and the sama tool server read the scope, the budget, and the live authority of
the run from it. So a kept process must not hold the token of an old run, and it must not
need a new connection for a new token.

The relay solves both. It listens on ``127.0.0.1`` in the runner process. The kept CLI and
the kept MCP clients call the relay with one fixed local secret. The relay adds the token of
the run that is active now and forwards the call to the model proxy or to the tool server
over one pool of connections that stays open.

- ``/anthropic/...`` and ``/openai/...`` go to the same path of the model proxy.
- ``/mcp/<name>`` goes to the URL that ``set_routes`` registered for the server ``<name>``.
- Without a token (no run is active), every call gets ``401``. ``set_token`` and
  ``clear_token`` are the only places where a token enters or leaves the relay.

The relay also writes the token to a private file and makes a shell prefix script. The CLI
runs each shell command of the agent through the prefix (``CLAUDE_CODE_SHELL_PREFIX``). The
script reads the file and sets ``SAMA_RUN_TOKEN`` for that command. This keeps the git
credential helper of a proxy clone working (Section 15.5), because the environment of a kept
CLI cannot change. The file is for one run: it has a new random name for each run, a pointer
file names it, and both are deleted when the run ends, so a reader that is late gets nothing.

The relay is not a new trust boundary. A process in the sandbox could already read the token
of the run from the environment of the CLI. The local secret keeps other local users out. A
process that a run left behind must not reach the relay in a later run: the runner kills it
before the next token is active (``sama.runtime.fence``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shlex
import shutil
import socket
import tempfile
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any, Final

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

log: Final = logging.getLogger("sama.runtime.relay")

SHELL_PREFIX_ENV: Final = "CLAUDE_CODE_SHELL_PREFIX"
"""The CLI switch that runs each shell command of the agent through a script."""
RUN_TOKEN_ENV: Final = "SAMA_RUN_TOKEN"  # noqa: S105 -- the name of a variable
MAX_BODY_BYTES: Final = 64 * 1024 * 1024
"""The largest request body that the relay forwards. The model proxy refuses more than 32 MB."""
MAX_BUFFERED_BYTES: Final = 192 * 1024 * 1024
"""The most bytes of request bodies that the relay holds at one time, for all calls. A call that
would pass it gets ``503``."""
KEEPALIVE_EXPIRY_S: Final = 60.0
"""An idle upstream connection that is older than this is not used again. A sandbox that
paused and resumed has dead connections, and a new connection costs less than a failed call."""
_HOP_BY_HOP: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
_CREDENTIALS: Final = frozenset({"authorization", "x-api-key"})
_METHODS: Final = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class _QuietServer(uvicorn.Server):
    """A uvicorn server that does not take the signals of the process. The runner owns them."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class TokenRelay:
    """See the module docstring. One relay for each runner process."""

    def __init__(self, model_url: str | None = None) -> None:
        self._model_url = model_url.rstrip("/") if model_url else None
        self.secret = secrets.token_urlsafe(24)
        """The only credential that the kept CLI and the kept MCP clients hold."""
        self._token: str | None = None
        self._routes: dict[str, str] = {}
        self._buffered = 0
        self._epoch: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._server: _QuietServer | None = None
        self._task: asyncio.Task[None] | None = None
        self._sock: socket.socket | None = None
        self._folder: str | None = None
        self.url = ""
        self.app: Starlette | None = None
        """The root URL of the relay, for example ``http://127.0.0.1:41233``."""

    # -- life ------------------------------------------------------------------------------

    async def start(self) -> None:
        if self._server is not None:
            return
        self._folder = tempfile.mkdtemp(prefix="sama-relay-")  # mode 0700
        self._write_prefix()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self._sock = sock
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0),
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=KEEPALIVE_EXPIRY_S),
            follow_redirects=False,
        )
        app = Starlette(routes=[Route("/{path:path}", self._handle, methods=_METHODS)])
        self.app = app
        config = uvicorn.Config(
            app, log_level="warning", access_log=False, lifespan="off", ws="none", interface="asgi3"
        )
        server = _QuietServer(config)
        self._server = server
        self._task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if self._task.done():
                await self._task  # raises the start error
            await asyncio.sleep(0.005)

    async def aclose(self) -> None:
        self._token = None
        server, task = self._server, self._task
        self._server = self._task = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=5)
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._folder is not None:
            shutil.rmtree(self._folder, ignore_errors=True)
            self._folder = None

    # -- the token and the routes ----------------------------------------------------------

    @property
    def active(self) -> bool:
        """True while a run has a token in the relay."""
        return self._token is not None

    def set_token(self, token: str) -> None:
        """Gives the relay the token of the run that starts now. The file of the token is for
        this run only: a new name, and a pointer file that names it."""
        self._token = token
        if self._folder is not None:
            self._remove_files()
            self._epoch = secrets.token_hex(8)
            name = f"token.{self._epoch}"
            self._write(os.path.join(self._folder, name), token)
            # The pointer comes last, so a reader never sees a name without its file.
            self._write(self._pointer_path(), name)

    def clear_token(self) -> None:
        """Takes the token away when the run ends. The next call gets ``401``, and the files
        are gone."""
        self._token = None
        if self._folder is not None:
            self._remove_files()

    def _write(self, path: str, text: str) -> None:
        temporary = f"{path}.new"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(temporary, path)

    def _remove_files(self) -> None:
        if self._folder is None:
            return
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._pointer_path())
        if self._epoch is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.path.join(self._folder, f"token.{self._epoch}"))
            self._epoch = None

    def set_routes(self, routes: Mapping[str, str]) -> None:
        """Replaces all routes: the upstream URL of each server by name. A name that is not in
        ``routes`` gets ``404`` from now on. The relay sends the token only to a URL that its
        runner registered for the job that runs now."""
        self._routes = dict(routes)

    def mcp_url(self, name: str) -> str:
        return f"{self.url}/mcp/{name}"

    @property
    def anthropic_url(self) -> str:
        return f"{self.url}/anthropic"

    @property
    def openai_url(self) -> str:
        return f"{self.url}/openai/v1"

    @property
    def shell_prefix(self) -> str:
        """The path of the script for ``CLAUDE_CODE_SHELL_PREFIX``."""
        assert self._folder is not None  # noqa: S101 -- only after start
        return os.path.join(self._folder, "shell.sh")

    def _pointer_path(self) -> str:
        assert self._folder is not None  # noqa: S101 -- only after start
        return os.path.join(self._folder, "current")

    def _write_prefix(self) -> None:
        folder = shlex.quote(self._folder or "")
        script = (
            "#!/bin/sh\n"
            f"unset {RUN_TOKEN_ENV}\n"
            f"name=$(cat {folder}/current 2>/dev/null) && "
            f'{RUN_TOKEN_ENV}=$(cat {folder}/"$name" 2>/dev/null) && export {RUN_TOKEN_ENV}\n'
            'exec /bin/bash -c "$1"\n'
        )
        path = self.shell_prefix
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
        with os.fdopen(fd, "w") as handle:
            handle.write(script)

    # -- forwarding ------------------------------------------------------------------------

    def _authorised(self, request: Request) -> bool:
        key = request.headers.get("x-api-key", "").encode()
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ").encode()
        secret = self.secret.encode()
        return secrets.compare_digest(key, secret) or secrets.compare_digest(bearer, secret)

    def _upstream(self, path: str) -> str | None:
        kind, _, rest = path.partition("/")
        if kind == "mcp":
            return self._routes.get(rest)
        if kind in {"anthropic", "openai"} and self._model_url is not None:
            return f"{self._model_url}/{path}"
        return None

    async def _handle(self, request: Request) -> Response:
        if not self._authorised(request):
            return JSONResponse({"error": "the local secret is not valid"}, status_code=401)
        token = self._token
        client = self._client
        if token is None or client is None:
            return JSONResponse({"error": "no run is active"}, status_code=401)
        path = str(request.path_params["path"])
        url = self._upstream(path)
        if url is None:
            return JSONResponse({"error": "unknown route"}, status_code=404)
        if request.url.query:
            url = f"{url}?{request.url.query}"
        length = request.headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"error": "the request is too large"}, status_code=413)
        read = await self._read_body(request)
        if isinstance(read, Response):
            return read
        body, held = read
        token = self._token  # the run can have ended while the body came
        if token is None:
            self._buffered -= held
            return JSONResponse({"error": "no run is active"}, status_code=401)
        headers = {
            name: value
            for name, value in request.headers.items()
            if name not in _HOP_BY_HOP and name not in _CREDENTIALS
        }
        # The model proxy reads x-api-key or the bearer. The tool server reads the bearer.
        headers["authorization"] = f"Bearer {token}"
        headers["x-api-key"] = token
        try:
            upstream = await client.send(
                client.build_request(request.method, url, headers=headers, content=body),
                stream=True,
            )
        except httpx.HTTPError as error:
            log.warning("the relay could not reach the upstream: %s", type(error).__name__)
            return JSONResponse({"error": "the upstream is not reachable"}, status_code=502)
        finally:
            self._buffered -= held
        response_headers = {
            name: value for name, value in upstream.headers.items() if name not in _HOP_BY_HOP
        }
        return StreamingResponse(
            _stream(upstream),
            status_code=upstream.status_code,
            headers=response_headers,
        )

    async def _read_body(self, request: Request) -> tuple[bytes, int] | Response:
        """Reads the body in pieces. It stops at ``MAX_BODY_BYTES`` for this call and at
        ``MAX_BUFFERED_BYTES`` for all calls, and it never holds more than it needs to refuse.
        Returns the body and the bytes that it counted, or the refusal."""
        chunks: list[bytes] = []
        held = 0
        try:
            async for chunk in request.stream():
                held += len(chunk)
                self._buffered += len(chunk)
                if held > MAX_BODY_BYTES:
                    self._buffered -= held
                    return JSONResponse({"error": "the request is too large"}, status_code=413)
                if self._buffered > MAX_BUFFERED_BYTES:
                    self._buffered -= held
                    return JSONResponse({"error": "the relay is busy"}, status_code=503)
                chunks.append(chunk)
        except BaseException:
            self._buffered -= held
            raise
        return b"".join(chunks), held


async def _stream(upstream: httpx.Response) -> AsyncIterator[bytes]:
    """Passes the body on as it comes. A client that leaves closes the upstream call."""
    try:
        async for chunk in upstream.aiter_raw():
            yield chunk
    finally:
        await upstream.aclose()


def relay_env(relay: TokenRelay) -> dict[str, Any]:
    """The CLI settings that point the Claude CLI at the relay."""
    return {
        "ANTHROPIC_API_KEY": relay.secret,
        "ANTHROPIC_BASE_URL": relay.anthropic_url,
        SHELL_PREFIX_ENV: relay.shell_prefix,
        RUN_TOKEN_ENV: "",
    }
