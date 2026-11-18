"""Helpers for building contract objects in tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import httpx
from pydantic import SecretStr

from sama.runtime import (
    Audience,
    ContextFile,
    McpServerConfig,
    RunConfig,
    RunEvent,
    RunRequest,
    RunScope,
    RuntimeKind,
    SkillIndexEntry,
    ToolSchema,
)
from sama.tenancy import RunTokenClaims


def make_scope(run_id: str = "run-1", *, shared: bool = False) -> RunScope:
    if shared:
        return RunScope(
            org_id="org-1",
            workspace_id="ws-1",
            session_id="sess-1",
            run_id=run_id,
            audience=Audience.SHARED,
        )
    return RunScope(
        org_id="org-1",
        workspace_id="ws-1",
        session_id="sess-1",
        run_id=run_id,
        audience=Audience.PRIVATE,
        user_id="user-1",
    )


def make_config(
    runtime: RuntimeKind = RuntimeKind.FAKE, *, token: str = "run-token-1", soul: str = "You help."
) -> RunConfig:
    return RunConfig(
        runtime=runtime,
        model="claude-sonnet-4-5",
        soul=soul,
        context_files=(ContextFile(path="AGENTS.md", content="Use uv."),),
        skill_index=(SkillIndexEntry(name="deploy", description="Deploy to Fly.io."),),
        memory_snapshot="The user is in IST.",
        tool_schemas=(
            ToolSchema(
                name="web_search",
                description="Search the web.",
                input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            ),
        ),
        mcp_servers=(
            McpServerConfig(
                name="sama", url="https://mcp.sama.test/mcp", bearer_token=SecretStr(token)
            ),
        ),
    )


def make_request(
    text: str = "hello world",
    run_id: str = "run-1",
    *,
    runtime_state: bytes | None = None,
    config: RunConfig | None = None,
) -> RunRequest:
    return RunRequest(
        scope=make_scope(run_id),
        input=text,
        config=config or make_config(),
        runtime_state=runtime_state,
    )


async def collect(stream: AsyncIterator[RunEvent]) -> list[RunEvent]:
    return [event async for event in stream]


def make_claims(
    run_id: str = "run-1", *, org: str = "org-1", session: str = "sess-1"
) -> RunTokenClaims:
    return RunTokenClaims(
        org_id=org,
        workspace_id="ws-1",
        session_id=session,
        run_id=run_id,
        user_id="user-1",
        token_id="t-1",
        expires_at=2_000_000_000,
    )


@dataclass
class RecordingSpill:
    """A ``Spill`` that keeps the files in memory."""

    files: dict[str, bytes] = field(default_factory=dict)
    fail: bool = False

    async def save(self, claims: RunTokenClaims, name: str, data: bytes) -> str | None:
        if self.fail:
            raise OSError("disk full")
        path = f"/work/.sama/spill/{name}"
        self.files[path] = data
        return path


class _WireStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._data


def wire_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    """A mock transport whose response bodies are not read yet, as on a real connection.
    ``httpx.Response(content=...)`` is read at once, so ``aiter_raw`` cannot stream it."""

    def send(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        return httpx.Response(
            response.status_code, headers=response.headers, stream=_WireStream(response.content)
        )

    return httpx.MockTransport(send)
