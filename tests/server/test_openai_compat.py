"""The OpenAI-compatible chat endpoint (docs/architecture.md, Section 10.7, M7.6)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import httpx2
import openai
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AuditEntry, Message, Run, Session
from sama.runtime.contract import FailureCode, RunFailed
from sama.runtime.fake import FakeScript
from sama.server.openai import RUN_HEADER, SESSION_HEADER, compose
from sama.server.openai import ChatMessage as Msg
from tests.api.conftest import bearer
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness
from tests.server.conftest import Keys, answer, reply

pytestmark = pytest.mark.usefixtures("admin")


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def post(
    client: httpx.AsyncClient, key: str, body: dict[str, Any], **headers: str
) -> asyncio.Task[httpx.Response]:
    return asyncio.ensure_future(
        client.post("/v1/chat/completions", json=body, headers={**auth(key), **headers})
    )


async def session_row(engine: AsyncEngine, session_id: str) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(select(Session).where(Session.id == uuid.UUID(session_id)))
        ).one()


async def run_row(engine: AsyncEngine, run_id: str) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(select(Run).where(Run.id == uuid.UUID(run_id)))).one()


async def user_texts(engine: AsyncEngine, session_id: str) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(Message.content)
            .where(Message.session_id == uuid.UUID(session_id), Message.role == "user")
            .order_by(Message.seq)
        )
        return [str(row.content.get("text")) for row in rows]


# -- auth ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [None, "Bearer nope", "Basic abc", "Bearer sama_sk_" + "x" * 43],
)
async def test_a_request_without_a_valid_key_gets_401(
    chat_client: httpx.AsyncClient, header: str | None
) -> None:
    headers = {} if header is None else {"Authorization": header}
    response = await chat_client.get("/v1/models", headers=headers)
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["type"] == "authentication_error"
    assert error["code"] == "invalid_api_key"


async def test_a_user_access_token_is_not_an_api_key(
    chat_client: httpx.AsyncClient, admin: Tenant
) -> None:
    response = await chat_client.get("/v1/models", headers=bearer(admin.ids["mia_auth"]))
    assert response.status_code == 401


async def test_a_key_without_the_chat_scope_gets_401(
    chat_client: httpx.AsyncClient, keys: Keys
) -> None:
    key = await keys.make(scopes=["mcp"])
    response = await chat_client.get("/v1/models", headers=auth(key))
    assert response.status_code == 401
    assert "scope chat" in response.json()["error"]["message"]


async def test_a_revoked_key_stops_at_once(
    chat_client: httpx.AsyncClient, keys: Keys, admin: Tenant
) -> None:
    key = await keys.make()
    assert (await chat_client.get("/v1/models", headers=auth(key))).status_code == 200
    [row] = await keys.store.list(admin.org)
    await keys.store.revoke(admin.org, row["id"], actor_user_id=admin.ids["mia"])
    assert (await chat_client.get("/v1/models", headers=auth(key))).status_code == 401


# -- models -------------------------------------------------------------------------------


async def test_the_models_are_the_agent_profiles_of_the_key(
    chat_client: httpx.AsyncClient, keys: Keys, admin: Tenant
) -> None:
    key = await keys.make()
    response = await chat_client.get("/v1/models", headers=auth(key))
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [(item["id"], item["sama_profile_id"]) for item in body["data"]] == [
        ("p", str(admin.ids["profile"]))
    ]
    one = await chat_client.get("/v1/models/p", headers=auth(key))
    assert one.json()["id"] == "p"
    missing = await chat_client.get("/v1/models/nope", headers=auth(key))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found"


# -- chat ---------------------------------------------------------------------------------


async def test_a_chat_completion_runs_the_agent_in_a_new_private_session(
    chat_client: httpx.AsyncClient,
    keys: Keys,
    admin: Tenant,
    harness: Harness,
    engine: AsyncEngine,
) -> None:
    key = await keys.make()
    body = {
        "model": "p",
        "messages": [
            {"role": "system", "content": "Be short."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": [{"type": "text", "text": "What is 2+2?"}]},
        ],
        "temperature": 0.2,
        "tools": [{"type": "function", "function": {"name": "ignored"}}],
    }
    pending = await post(chat_client, key, body)
    await answer(harness, admin.org, reply("Four", "."))
    response = await pending
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "p"
    assert data["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Four.",
        "refusal": None,
    }
    assert data["choices"][0]["finish_reason"] == "stop"
    assert set(data["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}
    session_id = response.headers[SESSION_HEADER]
    run_id = response.headers[RUN_HEADER]
    assert data["sama"] == {"session_id": session_id, "run_id": run_id}
    assert data["id"] == f"chatcmpl-{uuid.UUID(run_id).hex}"

    session = await session_row(engine, session_id)
    assert (session.audience, session.owner_user_id) == ("private", admin.ids["mia"])
    assert session.workspace_id == admin.ids["ws"]
    assert session.title == "What is 2+2?"
    run = await run_row(engine, run_id)
    assert run.unattended, "a key with the approvals deny makes an unattended run"
    assert run.triggered_by_user_id == admin.ids["mia"]
    [text] = await user_texts(engine, session_id)
    assert "Be short." in text
    assert "[assistant]\nHello!" in text
    assert text.endswith("The last message:\nWhat is 2+2?")
    async with engine.connect() as connection:
        audit = (
            await connection.execute(
                select(AuditEntry.details).where(
                    AuditEntry.action == "session.created", AuditEntry.target_id == session_id
                )
            )
        ).scalar_one()
    assert audit["channel"] == "api"


async def test_the_session_header_continues_a_session_with_the_last_message(
    chat_client: httpx.AsyncClient,
    keys: Keys,
    admin: Tenant,
    harness: Harness,
    engine: AsyncEngine,
) -> None:
    key = await keys.make()
    first = await post(chat_client, key, {"messages": [{"role": "user", "content": "one"}]})
    await answer(harness, admin.org, reply("1"))
    session_id = (await first).headers[SESSION_HEADER]
    body = {
        "messages": [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "1"},
            {"role": "user", "content": "two"},
        ]
    }
    second = await post(chat_client, key, body, **{SESSION_HEADER: session_id})
    await answer(harness, admin.org, reply("2"))
    response = await second
    assert response.status_code == 200
    assert response.headers[SESSION_HEADER] == session_id
    assert response.json()["choices"][0]["message"]["content"] == "2"
    assert await user_texts(engine, session_id) == ["one", "two"]


async def test_the_session_id_in_the_body_works_like_the_header(
    chat_client: httpx.AsyncClient, keys: Keys, admin: Tenant, harness: Harness
) -> None:
    key = await keys.make()
    body = {"messages": [{"role": "user", "content": "x"}], "session_id": str(admin.ids["mine"])}
    pending = await post(chat_client, key, body)
    await answer(harness, admin.org, reply("ok"))
    response = await pending
    assert response.headers[SESSION_HEADER] == str(admin.ids["mine"])


async def test_a_session_that_the_key_cannot_use_gets_404(
    chat_client: httpx.AsyncClient, keys: Keys
) -> None:
    key = await keys.make()
    body = {"messages": [{"role": "user", "content": "x"}]}
    response = await chat_client.post(
        "/v1/chat/completions",
        json=body,
        headers={**auth(key), SESSION_HEADER: str(uuid.uuid4())},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"


async def test_a_streamed_completion_sends_the_text_deltas(
    chat_client: httpx.AsyncClient, keys: Keys, admin: Tenant, harness: Harness
) -> None:
    key = await keys.make()
    body = {
        "model": "sama",
        "messages": [{"role": "user", "content": "count"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    async def read() -> tuple[httpx.Headers, list[str]]:
        async with chat_client.stream(
            "POST", "/v1/chat/completions", json=body, headers=auth(key)
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = [line async for line in response.aiter_lines() if line.startswith("data: ")]
            return response.headers, lines

    pending = asyncio.ensure_future(read())
    await answer(harness, admin.org, reply("one ", "two"))
    headers, lines = await pending
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line.removeprefix("data: ")) for line in lines[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(
        chunk["choices"][0]["delta"].get("content", "") for chunk in chunks if chunk["choices"]
    )
    assert text == "one two"
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"] == []
    assert "usage" in chunks[-1]
    assert SESSION_HEADER in headers


async def test_a_failed_run_is_an_error(
    chat_client: httpx.AsyncClient, keys: Keys, admin: Tenant, harness: Harness
) -> None:
    key = await keys.make()
    pending = await post(chat_client, key, {"messages": [{"role": "user", "content": "x"}]})
    script = FakeScript(
        events=(RunFailed(code=FailureCode.BUDGET_EXCEEDED, message="over the budget"),)
    )
    await answer(harness, admin.org, script)
    response = await pending
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "budget_exceeded"
    assert "over the budget" in error["message"]
    assert SESSION_HEADER in response.headers


async def test_a_failed_streamed_run_sends_an_error_event(
    chat_client: httpx.AsyncClient, keys: Keys, admin: Tenant, harness: Harness
) -> None:
    key = await keys.make()
    body = {"messages": [{"role": "user", "content": "x"}], "stream": True}

    async def read() -> list[str]:
        async with chat_client.stream(
            "POST", "/v1/chat/completions", json=body, headers=auth(key)
        ) as response:
            return [line async for line in response.aiter_lines() if line.startswith("data: ")]

    pending = asyncio.ensure_future(read())
    script = FakeScript(events=(RunFailed(code=FailureCode.MODEL_ERROR, message="boom"),))
    await answer(harness, admin.org, script)
    lines = await pending
    assert lines[-1] == "data: [DONE]"
    assert json.loads(lines[-2].removeprefix("data: "))["error"]["code"] == "model_error"


async def test_a_pending_key_makes_an_attended_run(
    chat_client: httpx.AsyncClient,
    keys: Keys,
    admin: Tenant,
    harness: Harness,
    engine: AsyncEngine,
) -> None:
    key = await keys.make(approvals="pending")
    pending = await post(chat_client, key, {"messages": [{"role": "user", "content": "x"}]})
    await answer(harness, admin.org, reply("ok"))
    response = await pending
    run = await run_row(engine, response.headers[RUN_HEADER])
    assert not run.unattended


async def test_a_long_run_gets_spaces_then_the_answer(
    chat_client: httpx.AsyncClient,
    keys: Keys,
    admin: Tenant,
    harness: Harness,
    core: Any,
) -> None:
    key = await keys.make()
    core.settings = core.settings.model_copy(update={"first_wait_s": 0.0})
    pending = await post(chat_client, key, {"messages": [{"role": "user", "content": "x"}]})
    await asyncio.sleep(0.2)
    await answer(harness, admin.org, reply("late"))
    response = await pending
    assert response.status_code == 200
    assert response.text.startswith(" ")
    assert response.json()["choices"][0]["message"]["content"] == "late"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"messages": [{"role": "assistant", "content": "x"}]}, "invalid_request"),
        ({"messages": []}, "invalid_request"),
        (
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]},
            "unsupported_content",
        ),
        ({"model": "nope", "messages": [{"role": "user", "content": "x"}]}, "model_not_found"),
        ({"messages": [{"role": "user", "content": "x" * 32_001}]}, "context_length_exceeded"),
    ],
)
async def test_a_bad_request_gets_an_openai_error(
    chat_client: httpx.AsyncClient, keys: Keys, body: dict[str, Any], code: str
) -> None:
    key = await keys.make()
    response = await chat_client.post("/v1/chat/completions", json=body, headers=auth(key))
    assert response.status_code in (400, 404)
    assert response.json()["error"]["code"] == code


async def test_the_openai_python_sdk_talks_to_the_endpoint(
    chat_app: FastAPI, keys: Keys, admin: Tenant, harness: Harness
) -> None:
    key = await keys.make()
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=chat_app), timeout=30)
    sdk = openai.AsyncOpenAI(
        api_key=key, base_url="http://sama.test/v1", http_client=http, max_retries=0
    )
    models = await sdk.models.list()
    assert [model.id for model in models.data] == ["p"]

    pending = asyncio.ensure_future(
        sdk.chat.completions.create(model="p", messages=[{"role": "user", "content": "hi"}])
    )
    await answer(harness, admin.org, reply("hello"))
    completion = await pending
    assert completion.choices[0].message.content == "hello"

    async def streamed() -> str:
        stream = await sdk.chat.completions.create(
            model="p", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        parts = []
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                parts.append(chunk.choices[0].delta.content)
        return "".join(parts)

    pending_stream = asyncio.ensure_future(streamed())
    await answer(harness, admin.org, reply("str", "eam"))
    assert await pending_stream == "stream"

    with pytest.raises(openai.AuthenticationError):
        await openai.AsyncOpenAI(
            api_key="sama_sk_" + "y" * 43,
            base_url="http://sama.test/v1",
            http_client=http,
            max_retries=0,
        ).models.list()
    await http.aclose()


def test_compose_keeps_only_the_last_message_in_a_session() -> None:
    messages = [Msg(role="user", content="a"), Msg(role="user", content="b")]
    assert compose(messages, new_session=False) == ("b", "b")
    assert compose([Msg(role="user", content="only")], new_session=True) == ("only", "only")
