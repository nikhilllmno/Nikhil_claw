"""Session search, export, and run traces of the web API (docs/architecture.md, Section 11.4)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api.history import MAX_TRACE_TEXT_CHARS, export_markdown, merge_steps
from sama.db.models import Message, ModelCall, Run, RunEvent, ToolCall
from tests.api.conftest import bearer
from tests.api.test_app import add_user, mia
from tests.orchestrator.conftest import Tenant, add_tenant

pytestmark = pytest.mark.usefixtures("tenant")

# Built from parts, so the secret scanner does not see a key in the source.
FAKE_KEY = "sk-" + "a1b2c3d4" * 4


async def say(
    engine: AsyncEngine,
    tenant: Tenant,
    session: str,
    text: str,
    *,
    role: str = "user",
    run_id: uuid.UUID | None = None,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            insert(Message).values(
                org_id=tenant.org,
                session_id=tenant.ids[session],
                run_id=run_id,
                role=role,
                content={"text": text},
            )
        )


async def add_run(engine: AsyncEngine, tenant: Tenant, session: str, **values: Any) -> uuid.UUID:
    run_id = uuid.uuid4()
    row: dict[str, Any] = {
        "id": run_id,
        "org_id": tenant.org,
        "session_id": tenant.ids[session],
        "agent_profile_id": tenant.ids["profile"],
        "triggered_by_user_id": tenant.ids["mia"],
        "status": "completed",
        "runtime": "fake",
        "attempts": 1,
    }
    async with engine.begin() as connection:
        await connection.execute(insert(Run).values(**(row | values)))
    return run_id


async def add_trace(engine: AsyncEngine, tenant: Tenant, session: str, run_id: uuid.UUID) -> None:
    keys = {"org_id": tenant.org, "session_id": tenant.ids[session], "run_id": run_id}
    async with engine.begin() as connection:
        for seq, (kind, payload) in enumerate(
            [
                ("text_delta", {"text": "Hello "}),
                ("text_delta", {"text": "there."}),
                ("tool_call_started", {"call_id": "t1", "name": "web_search"}),
                ("tool_call_finished", {"call_id": "t1", "output_preview": "found it"}),
                ("text_delta", {"text": "Done."}),
                ("run_completed", {"runtime_state": {"opaque": True}, "stop": "end"}),
            ],
            start=1,
        ):
            await connection.execute(
                insert(RunEvent).values(seq=seq, type=kind, payload=payload, **keys)
            )
        await connection.execute(
            insert(ToolCall).values(
                call_id="t1",
                tool_name="web_search",
                arguments={"query": "news", "token": FAKE_KEY},
                decision="allow",
                status="finished",
                result_preview=f"key {FAKE_KEY}",
                **keys,
            )
        )
        for cost in ("0.01", "0.02"):
            await connection.execute(
                insert(ModelCall).values(
                    call_id=uuid.uuid4(),
                    workspace_id=tenant.ids["ws"],
                    provider="anthropic",
                    model="claude-x",
                    endpoint="messages",
                    status_code=200,
                    input_tokens=100,
                    output_tokens=20,
                    cost_usd=Decimal(cost),
                    duration_ms=900,
                    **keys,
                )
            )


# -- unit ------------------------------------------------------------------------------------


class _Event:
    def __init__(self, seq: int, type_: str, payload: dict[str, Any]) -> None:
        self.seq = seq
        self.type = type_
        self.payload = payload
        self.created_at = datetime(2026, 1, 1, tzinfo=UTC)


def test_merge_steps_joins_text_and_drops_the_runtime_state() -> None:
    steps = merge_steps(
        [
            _Event(1, "text_delta", {"text": "a"}),
            _Event(2, "text_delta", {"text": "b"}),
            _Event(3, "tool_call_started", {"call_id": "t1"}),
            _Event(4, "text_delta", {"text": "c"}),
            _Event(5, "run_completed", {"runtime_state": {"x": 1}, "stop": "end"}),
        ]
    )
    assert [(s.seq, s.type) for s in steps] == [
        (1, "text"),
        (3, "tool_call_started"),
        (4, "text"),
        (5, "run_completed"),
    ]
    assert steps[0].payload["text"] == "ab"
    assert steps[3].payload == {"stop": "end"}


def test_merge_steps_cuts_long_text_and_redacts_a_split_secret() -> None:
    half = len(FAKE_KEY) // 2
    steps = merge_steps(
        [
            _Event(1, "text_delta", {"text": "key " + FAKE_KEY[:half]}),
            _Event(2, "text_delta", {"text": FAKE_KEY[half:]}),
        ]
    )
    assert FAKE_KEY not in steps[0].payload["text"]
    assert "[REDACTED]" in steps[0].payload["text"]
    long = merge_steps([_Event(i, "text_delta", {"text": "x" * 1000}) for i in range(40)])
    assert len(long[0].payload["text"]) <= MAX_TRACE_TEXT_CHARS + 50


def test_export_markdown_has_a_heading_per_message() -> None:
    body = export_markdown(
        {
            "id": "s-1",
            "title": None,
            "audience": "private",
            "created_at": "2026-01-01T00:00:00+00:00",
            "exported_at": "2026-01-02T00:00:00+00:00",
        },
        [
            {"role": "user", "created_at": "t1", "text": "Hi"},
            {"role": "assistant", "created_at": "t2", "text": "**Hello**\n"},
        ],
        truncated=True,
    )
    assert body.startswith("# Untitled session\n")
    assert "## User (t1)\n\nHi" in body
    assert "## Assistant (t2)\n\n**Hello**" in body
    assert "- Note: the export has only the first" in body


# -- search ----------------------------------------------------------------------------------


async def test_the_search_finds_shared_and_own_private_sessions(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await say(engine, tenant, "s1", "the pgbouncer pool size")
    await say(engine, tenant, "mine", "my pgbouncer notes")
    await say(engine, tenant, "s2", "nothing to see")
    response = await client.get(
        f"/orgs/{tenant.org}/session-search", headers=mia(tenant), params={"q": "pgbouncer"}
    )
    assert response.status_code == 200
    found = {hit["session_id"] for hit in response.json()}
    assert found == {str(tenant.ids["s1"]), str(tenant.ids["mine"])}
    snippet = response.json()[0]["snippets"][0]["text"]
    assert "pgbouncer" in snippet

    # Another member of the workspace sees the shared session, not the private one of mia.
    zed = bearer(await add_user(engine, tenant, workspace_member=True))
    seen = await client.get(
        f"/orgs/{tenant.org}/session-search", headers=zed, params={"q": "pgbouncer"}
    )
    assert {hit["session_id"] for hit in seen.json()} == {str(tenant.ids["s1"])}


async def test_the_search_answers_404_outside_the_org_or_the_workspace(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    other = await add_tenant(engine)
    stranger = await client.get(
        f"/orgs/{tenant.org}/session-search", headers=bearer(other.ids["mia_auth"])
    )
    assert stranger.status_code == 404
    closed = await client.get(
        f"/orgs/{tenant.org}/session-search",
        headers=mia(tenant),
        params={"workspace_id": str(other.ids["ws"])},
    )
    assert closed.status_code == 404
    too_long = await client.get(
        f"/orgs/{tenant.org}/session-search", headers=mia(tenant), params={"q": "x" * 1000}
    )
    assert too_long.status_code == 422


async def test_an_empty_search_gives_the_latest_sessions(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    response = await client.get(
        f"/orgs/{tenant.org}/session-search",
        headers=mia(tenant),
        params={"workspace_id": str(tenant.ids["ws"]), "limit": 10},
    )
    assert response.status_code == 200
    assert {hit["session_id"] for hit in response.json()} == {
        str(tenant.ids[s]) for s in ("s1", "s2", "s3", "mine")
    }


# -- export ----------------------------------------------------------------------------------


async def test_the_export_gives_markdown_and_json_with_redacted_text(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await say(engine, tenant, "mine", "Hi, my key is " + FAKE_KEY)
    await say(engine, tenant, "mine", "I will not repeat it.", role="assistant")
    path = f"/sessions/{tenant.ids['mine']}/export"

    markdown = await client.get(path, headers=mia(tenant))
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.headers["cache-control"] == "no-store"
    stem = f"session-{str(tenant.ids['mine'])[:8]}"
    assert markdown.headers["content-disposition"] == f'attachment; filename="{stem}.md"'
    assert "## User (" in markdown.text
    assert "## Assistant (" in markdown.text
    assert FAKE_KEY not in markdown.text
    assert "[REDACTED]" in markdown.text

    exported = await client.get(path, headers=mia(tenant), params={"format": "json"})
    assert exported.status_code == 200
    assert exported.headers["content-disposition"] == f'attachment; filename="{stem}.json"'
    body = json.loads(exported.text)
    assert body["format"] == "sama.session.v1"
    assert body["session"]["id"] == str(tenant.ids["mine"])
    assert body["truncated"] is False
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert FAKE_KEY not in exported.text

    bad = await client.get(path, headers=mia(tenant), params={"format": "pdf"})
    assert bad.status_code == 422


async def test_the_export_answers_404_for_a_private_session_of_another_user(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    zed = bearer(await add_user(engine, tenant, workspace_member=True))
    response = await client.get(f"/sessions/{tenant.ids['mine']}/export", headers=zed)
    assert response.status_code == 404
    missing = await client.get(f"/sessions/{uuid.uuid4()}/export", headers=mia(tenant))
    assert missing.status_code == 404


# -- runs and traces -------------------------------------------------------------------------


async def test_the_runs_of_a_session_have_the_usage_of_each_run(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    first = await add_run(engine, tenant, "mine")
    await add_trace(engine, tenant, "mine", first)
    second = await add_run(
        engine, tenant, "mine", status="failed", failure_code="x", failure_message=FAKE_KEY
    )
    response = await client.get(f"/sessions/{tenant.ids['mine']}/runs", headers=mia(tenant))
    assert response.status_code == 200
    runs = {run["id"]: run for run in response.json()}
    assert set(runs) == {str(first), str(second)}
    assert runs[str(first)]["input_tokens"] == 200
    assert runs[str(first)]["output_tokens"] == 40
    assert Decimal(runs[str(first)]["cost_usd"]) == Decimal("0.03")
    assert runs[str(first)]["model_calls"] == 2
    assert runs[str(second)]["model_calls"] == 0
    assert runs[str(second)]["failure_message"] == "[REDACTED]"


async def test_the_trace_shows_steps_tools_and_model_calls(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    run_id = await add_run(engine, tenant, "mine")
    await add_trace(engine, tenant, "mine", run_id)
    response = await client.get(f"/runs/{run_id}/trace", headers=mia(tenant))
    assert response.status_code == 200
    trace = response.json()
    assert trace["run"]["id"] == str(run_id)
    assert trace["run"]["model_calls"] == 2
    assert [s["type"] for s in trace["steps"]] == [
        "text",
        "tool_call_started",
        "tool_call_finished",
        "text",
        "run_completed",
    ]
    assert trace["steps"][0]["payload"]["text"] == "Hello there."
    assert "runtime_state" not in trace["steps"][-1]["payload"]
    assert trace["truncated"] is False
    [tool] = trace["tool_calls"]
    assert tool["tool_name"] == "web_search"
    assert tool["arguments"]["query"] == "news"
    assert FAKE_KEY not in json.dumps(tool)
    assert len(trace["model_calls"]) == 2
    assert trace["model_calls"][0]["model"] == "claude-x"


async def test_the_trace_answers_404_for_a_run_that_the_user_cannot_read(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    run_id = await add_run(engine, tenant, "mine")
    zed = bearer(await add_user(engine, tenant, workspace_member=True))
    assert (await client.get(f"/runs/{run_id}/trace", headers=zed)).status_code == 404
    assert (
        await client.get(f"/sessions/{tenant.ids['mine']}/runs", headers=zed)
    ).status_code == 404
    other = await add_tenant(engine)
    stranger = bearer(other.ids["mia_auth"])
    assert (await client.get(f"/runs/{run_id}/trace", headers=stranger)).status_code == 404
    missing = await client.get(f"/runs/{uuid.uuid4()}/trace", headers=mia(tenant))
    assert missing.status_code == 404


async def test_a_shared_session_trace_is_open_to_a_workspace_member(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    run_id = await add_run(engine, tenant, "s1")
    await add_trace(engine, tenant, "s1", run_id)
    zed = bearer(await add_user(engine, tenant, workspace_member=True))
    response = await client.get(f"/runs/{run_id}/trace", headers=zed)
    assert response.status_code == 200
    outsider = bearer(await add_user(engine, tenant, workspace_member=False))
    assert (await client.get(f"/runs/{run_id}/trace", headers=outsider)).status_code == 404
