"""Helpers for building contract objects in tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import SecretStr

from claw.runtime import (
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
                name="claw", url="https://mcp.claw.test/mcp", bearer_token=SecretStr(token)
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
