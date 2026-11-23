"""Tests of the token relay (src/sama/runtime/relay.py)."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

import sama.runtime.relay as relay_module
from sama.runtime.relay import TokenRelay, relay_env
from tests.conformance.scripted_model import serve

pytestmark = pytest.mark.asyncio


class Upstream:
    """A model proxy and a tool server in one app. It records what the relay sent."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def echo(self, request: Request) -> JSONResponse:
        self.seen.append(
            {
                "path": request.url.path,
                "query": request.url.query,
                "key": request.headers.get("x-api-key"),
                "bearer": request.headers.get("authorization"),
                "version": request.headers.get("anthropic-version"),
                "body": (await request.body()).decode(),
            }
        )
        return JSONResponse({"ok": True}, headers={"x-extra": "1"})

    async def stream(self, request: Request) -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            for word in (b"one", b"two", b"three"):
                yield word + b"\n"
                await asyncio.sleep(0.05)

        return StreamingResponse(chunks(), media_type="text/event-stream")

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/anthropic/v1/messages", self.echo, methods=["POST"]),
                Route("/openai/v1/responses", self.echo, methods=["POST"]),
                Route("/tools/mcp", self.echo, methods=["POST"]),
                Route("/anthropic/stream", self.stream, methods=["POST"]),
            ]
        )


@pytest.fixture
def upstream() -> Any:
    model = Upstream()
    with serve(model.app()) as url:
        yield model, url


@pytest.fixture
async def relay(upstream: Any) -> AsyncIterator[TokenRelay]:
    _, url = upstream
    relay = TokenRelay(url)
    relay.set_routes({"sama": f"{url}/tools/mcp"})
    await relay.start()
    yield relay
    await relay.aclose()


def local(relay: TokenRelay) -> dict[str, str]:
    return {"x-api-key": relay.secret}


async def test_a_call_gets_the_token_of_the_active_run(upstream: Any, relay: TokenRelay) -> None:
    model, _ = upstream
    relay.set_token("run-token-1")
    async with httpx.AsyncClient() as client:
        answer = await client.post(
            f"{relay.anthropic_url}/v1/messages?beta=true",
            headers={**local(relay), "anthropic-version": "2023-06-01"},
            content=b'{"hi": 1}',
        )
    assert answer.status_code == 200
    assert answer.headers["x-extra"] == "1"
    (call,) = model.seen
    assert call["key"] == "run-token-1"
    assert call["bearer"] == "Bearer run-token-1"
    assert call["version"] == "2023-06-01"
    assert call["query"] == "beta=true"
    assert call["body"] == '{"hi": 1}'


async def test_the_next_run_gets_its_own_token(upstream: Any, relay: TokenRelay) -> None:
    model, _ = upstream
    async with httpx.AsyncClient() as client:
        for token in ("run-a", "run-b"):
            relay.set_token(token)
            await client.post(f"{relay.openai_url}/responses", headers=local(relay), content=b"{}")
    assert [call["bearer"] for call in model.seen] == ["Bearer run-a", "Bearer run-b"]


async def test_the_local_secret_never_goes_upstream(upstream: Any, relay: TokenRelay) -> None:
    model, _ = upstream
    relay.set_token("run-token")
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{relay.url}/mcp/sama",
            headers={"authorization": f"Bearer {relay.secret}"},
            content=b"{}",
        )
    (call,) = model.seen
    assert call["path"] == "/tools/mcp"
    assert relay.secret not in str(call)
    assert call["bearer"] == "Bearer run-token"


async def test_no_call_passes_without_a_run(upstream: Any, relay: TokenRelay) -> None:
    model, _ = upstream
    async with httpx.AsyncClient() as client:
        idle = await client.post(f"{relay.anthropic_url}/v1/messages", headers=local(relay))
        relay.set_token("run-token")
        relay.clear_token()
        cleared = await client.post(f"{relay.anthropic_url}/v1/messages", headers=local(relay))
    assert idle.status_code == cleared.status_code == 401
    assert not relay.active
    assert model.seen == []


async def test_a_wrong_secret_gets_401(upstream: Any, relay: TokenRelay) -> None:
    model, _ = upstream
    relay.set_token("run-token")
    async with httpx.AsyncClient() as client:
        wrong = await client.post(
            f"{relay.anthropic_url}/v1/messages", headers={"x-api-key": "run-token"}
        )
        none = await client.post(f"{relay.anthropic_url}/v1/messages")
    assert wrong.status_code == none.status_code == 401
    assert model.seen == []


async def test_a_secret_that_is_not_ascii_gets_401(relay: TokenRelay) -> None:
    relay.set_token("run-token")
    async with httpx.AsyncClient() as client:
        answer = await client.post(
            f"{relay.anthropic_url}/v1/messages",
            headers=[(b"x-api-key", "caf\xe9".encode("latin-1"))],
        )
    assert answer.status_code == 401


async def test_an_unknown_route_gets_404(relay: TokenRelay) -> None:
    relay.set_token("run-token")
    async with httpx.AsyncClient() as client:
        other = await client.post(f"{relay.url}/other", headers=local(relay))
        mcp = await client.post(f"{relay.url}/mcp/unknown", headers=local(relay))
    assert other.status_code == mcp.status_code == 404


async def test_a_stream_is_passed_on_as_it_comes(relay: TokenRelay) -> None:
    relay.set_token("run-token")
    async with (
        httpx.AsyncClient() as client,
        client.stream("POST", f"{relay.anthropic_url}/stream", headers=local(relay)) as answer,
    ):
        loop = asyncio.get_running_loop()
        first = None
        start = loop.time()
        lines = []
        async for line in answer.aiter_lines():
            if line and first is None:
                first = loop.time() - start
            lines.append(line)
    assert [line for line in lines if line] == ["one", "two", "three"]
    assert first is not None
    assert first < 0.12  # the first chunk does not wait for the end of the stream


async def test_an_unreachable_upstream_gets_502() -> None:
    relay = TokenRelay("http://127.0.0.1:1")
    await relay.start()
    try:
        relay.set_token("run-token")
        async with httpx.AsyncClient() as client:
            answer = await client.post(f"{relay.anthropic_url}/v1/messages", headers=local(relay))
        assert answer.status_code == 502
    finally:
        await relay.aclose()


async def test_the_shell_prefix_sets_the_token_of_the_active_run(relay: TokenRelay) -> None:
    def run(command: str) -> str:
        env = {"PATH": os.environ["PATH"], "SAMA_RUN_TOKEN": "stale"}
        out = subprocess.run(
            [relay.shell_prefix, command], capture_output=True, text=True, env=env, check=True
        )
        return out.stdout.strip()

    command = "printf '%s|' \"$SAMA_RUN_TOKEN\"; echo \"a 'b' c\" && cd /tmp && pwd"
    assert run(command) == "|a 'b' c\n/tmp"
    relay.set_token("run-one")
    assert run(command).startswith("run-one|")
    relay.set_token("run-two")
    assert run(command).startswith("run-two|")
    relay.clear_token()
    assert run(command).startswith("|")


def exists(path: str) -> bool:
    return os.path.exists(path)


def shell(relay: TokenRelay, command: str) -> str:
    """Runs a command through the shell prefix, as the CLI does."""
    env = {"PATH": os.environ["PATH"]}
    return subprocess.run(
        [relay.shell_prefix, command], capture_output=True, text=True, env=env, check=True
    ).stdout


def token_files(relay: TokenRelay) -> list[str]:
    folder = os.path.dirname(relay.shell_prefix)
    return sorted(name for name in os.listdir(folder) if name.startswith(("token.", "current")))


async def test_the_token_file_is_private_and_for_one_run(relay: TokenRelay) -> None:
    folder = os.path.dirname(relay.shell_prefix)
    relay.set_token("run-token")
    first = token_files(relay)
    assert len(first) == 2  # the token file of the run and the pointer to it
    for name in first:
        assert os.stat(os.path.join(folder, name)).st_mode & 0o777 == 0o600
    assert os.stat(folder).st_mode & 0o777 == 0o700
    relay.set_token("run-token-2")
    second = token_files(relay)
    assert len(second) == 2
    assert set(first) & set(second) == {"current"}  # a new name for each run, no old file
    relay.clear_token()
    assert token_files(relay) == []


async def test_a_reader_that_is_late_gets_no_token(relay: TokenRelay) -> None:
    """A process of the last run that reads the token file after its run ended gets nothing,
    and a path that it kept does not hold the token of the next run."""
    relay.set_token("run-one")
    folder = os.path.dirname(relay.shell_prefix)
    (old_name,) = [n for n in token_files(relay) if n.startswith("token.")]
    relay.clear_token()
    assert not exists(os.path.join(folder, old_name))
    relay.set_token("run-two")
    assert not exists(os.path.join(folder, old_name))  # the next run has its own file
    assert shell(relay, 'printf "%s" "$SAMA_RUN_TOKEN"') == "run-two"
    relay.clear_token()
    assert shell(relay, 'printf "%s" "$SAMA_RUN_TOKEN"') == ""


async def test_the_routes_of_a_job_replace_the_routes_of_the_last_job(
    upstream: Any, relay: TokenRelay
) -> None:
    _, url = upstream
    relay.set_token("run-token")
    relay.set_routes({"other": f"{url}/tools/mcp", "session-items": f"{url}/tools/mcp"})
    async with httpx.AsyncClient() as client:
        gone = await client.post(f"{relay.url}/mcp/sama", headers=local(relay), content=b"{}")
        kept = await client.post(f"{relay.url}/mcp/other", headers=local(relay), content=b"{}")
        session = await client.post(
            f"{relay.url}/mcp/session-items", headers=local(relay), content=b"{}"
        )
    assert gone.status_code == 404  # a server of the last job has no route now
    assert kept.status_code == session.status_code == 200
    relay.set_routes({})
    async with httpx.AsyncClient() as client:
        none = await client.post(
            f"{relay.url}/mcp/session-items", headers=local(relay), content=b"{}"
        )
    assert none.status_code == 404


async def test_a_chunked_body_over_the_limit_is_refused_before_it_is_read_all(
    upstream: Any, relay: TokenRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, _ = upstream
    monkeypatch.setattr(relay_module, "MAX_BODY_BYTES", 1_000)
    relay.set_token("run-token")
    sent = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal sent
        for _ in range(1_000):
            sent += 1
            yield b"x" * 100
            await asyncio.sleep(0)

    assert relay.app is not None
    # In process, the request body is pulled by the app only: a relay that reads to the end
    # would pull all 1,000 chunks.
    transport = httpx.ASGITransport(app=relay.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://relay") as client:
        answer = await client.post("/anthropic/v1/messages", headers=local(relay), content=body())
    assert answer.status_code == 413
    assert sent < 100  # the relay stopped reading at the limit and not at the end of the body
    assert model.seen == []
    assert relay._buffered == 0


async def test_all_the_calls_together_cannot_buffer_more_than_the_limit(
    upstream: Any, relay: TokenRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(relay_module, "MAX_BUFFERED_BYTES", 150)
    relay.set_token("run-token")
    relay._buffered = 100  # other calls hold 100 bytes now
    async with httpx.AsyncClient() as client:
        answer = await client.post(
            f"{relay.anthropic_url}/v1/messages", headers=local(relay), content=b"y" * 100
        )
    assert answer.status_code == 503
    assert relay._buffered == 100  # the refused call gave back its bytes


async def test_close_removes_the_files(relay: TokenRelay) -> None:
    prefix = relay.shell_prefix
    relay.set_token("run-token")
    await relay.aclose()
    assert not await asyncio.to_thread(os.path.exists, prefix)
    assert not relay.active


async def test_the_cli_env_points_at_the_relay(relay: TokenRelay) -> None:
    env = relay_env(relay)
    assert env["ANTHROPIC_API_KEY"] == relay.secret
    assert env["ANTHROPIC_BASE_URL"] == f"{relay.url}/anthropic"
    assert env["CLAUDE_CODE_SHELL_PREFIX"] == relay.shell_prefix
    assert env["SAMA_RUN_TOKEN"] == ""


@pytest.mark.parametrize("same_token", [False, True])
async def test_a_call_of_an_earlier_run_never_gets_the_token_of_the_next_run(
    upstream: Any, relay: TokenRelay, same_token: bool
) -> None:
    """A call that the relay admitted for run N, whose body comes after run N+1 started,
    gets ``401`` and does not reach the upstream: not with the token of N+1, and not on a
    route that N+1 does not have."""
    model, url = upstream
    relay.set_token("run-n")
    started, release = asyncio.Event(), asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield b'{"part": 1'
        started.set()
        await release.wait()
        yield b"}"

    assert relay.app is not None
    transport = httpx.ASGITransport(app=relay.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://relay") as client:
        call = asyncio.create_task(client.post("/mcp/sama", headers=local(relay), content=body()))
        await asyncio.wait_for(started.wait(), timeout=5)
        relay.clear_token()
        relay.set_routes({"other": f"{url}/tools/mcp"})
        # Even the same token value must not pass: the generation changed.
        relay.set_token("run-n" if same_token else "run-n-plus-1")
        release.set()
        answer = await asyncio.wait_for(call, timeout=5)
    assert answer.status_code == 401
    assert model.seen == []
    assert relay._buffered == 0
