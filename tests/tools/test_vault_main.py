"""The standalone tool server and the vault: it must know the vault values (Section 8.1)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, SkillVersion
from sama.runtime.contract import Audience, RunScope
from sama.tenancy.run_token import RunTokenSigner
from sama.tools.__main__ import build_app
from sama.vault.crypto import VaultKey
from sama.vault.redact import REDACTED, Redactor, default_redactor, redact
from sama.vault.store import SecretName, Vault
from tests.orchestrator.conftest import Tenant, engine, queue_database_url, tenant  # noqa: F401

RUN_KEY = "k" * 32


@pytest.fixture
def tools_env(
    monkeypatch: pytest.MonkeyPatch,
    queue_database_url: str,  # noqa: F811
) -> None:
    monkeypatch.setenv("SAMA_RUN_TOKEN_KEY", RUN_KEY)
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    monkeypatch.delenv("SAMA_VAULT_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)


@pytest.fixture
async def profile_with_skills(engine: AsyncEngine, tenant: Tenant) -> None:  # noqa: F811
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(toolsets=["core", "memory", "skills"])
        )


async def _token(engine: AsyncEngine, tenant: Tenant) -> str:  # noqa: F811
    from sama.orchestrator.queue import RunQueue

    run_id = await RunQueue(engine).enqueue(
        org_id=tenant.org, session_id=tenant.ids["mine"], text="hi", user_id=tenant.ids["mia"]
    )
    scope = RunScope(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids["mine"]),
        run_id=str(run_id),
        audience=Audience.PRIVATE,
        user_id=str(tenant.ids["mia"]),
    )
    return RunTokenSigner(RUN_KEY.encode()).issue(scope, ttl_s=600)


async def _call(client: httpx.AsyncClient, token: str, tool: str, arguments: dict[str, Any]) -> Any:
    response = await client.post(
        "/mcp",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    return json.loads(response.text)["result"]


def _text(result: Any) -> str:
    return "\n".join(part["text"] for part in result["content"])


@pytest.mark.usefixtures("tenant", "tools_env", "profile_with_skills")
async def test_the_standalone_tool_server_refuses_a_vault_value_in_memory_and_skills(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    key = VaultKey.generate()
    monkeypatch.setenv("SAMA_VAULT_KEY", key)
    # A value that no token pattern can recognize: only the vault knows it.
    value = "vault-value-" + uuid.uuid4().hex
    await Vault(engine, VaultKey.from_text(key), redactor=Redactor()).put(
        tenant.org, SecretName.BRAVE_API_KEY, value
    )
    token = await _token(engine, tenant)
    app = build_app("postgres")
    try:
        assert redact(value) == value  # not known before the start
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            assert redact(value) == REDACTED  # known before the first request
            async with httpx.AsyncClient(transport=transport, base_url="http://tools.test") as c:
                memory = await _call(
                    c, token, "memory", {"target": "user", "action": "add", "content": value}
                )
                assert memory["isError"] is True
                assert value not in _text(memory)
                body = f"---\nname: leak\ndescription: A leak\nversion: 1\n---\n\nUse {value}.\n"
                skill = await _call(
                    c,
                    token,
                    "skill_manage",
                    {"action": "create", "name": "leak", "scope": "user", "content": body},
                )
                assert skill["isError"] is True
                summary = await _call(
                    c,
                    token,
                    "skill_manage",
                    {
                        "action": "create",
                        "name": "fine",
                        "scope": "user",
                        "content": "---\nname: fine\ndescription: Fine\nversion: 1\n---\n\nOk.\n",
                        "summary": f"made with {value}",
                    },
                )
                assert summary["isError"] is True
                assert value not in _text(summary)
                viewed = await _call(c, token, "skill_view", {"name": "fine"})
                assert viewed["isError"] is True
        async with engine.connect() as connection:
            rows = (await connection.execute(select(SkillVersion.name))).scalars().all()
        assert "leak" not in rows
        assert "fine" not in rows
    finally:
        default_redactor().forget(value, key)


@pytest.mark.usefixtures("tenant", "tools_env")
async def test_the_standalone_tool_server_does_not_start_when_the_first_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAMA_VAULT_KEY", VaultKey.generate())
    monkeypatch.setenv("SAMA_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")
    app = build_app("postgres")
    with pytest.raises(OSError, match=r"."):
        async with app.router.lifespan_context(app):
            pytest.fail("the server started without the vault values")


def test_a_bad_vault_key_stops_the_start(monkeypatch: pytest.MonkeyPatch, tools_env: None) -> None:
    monkeypatch.setenv("SAMA_VAULT_KEY", "not a key")
    with pytest.raises(SystemExit, match="SAMA_VAULT_KEY"):
        build_app("postgres")


async def test_the_memory_store_has_no_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAMA_RUN_TOKEN_KEY", RUN_KEY)
    monkeypatch.setenv("SAMA_VAULT_KEY", VaultKey.generate())
    app = build_app("memory")
    async with app.router.lifespan_context(app):
        pass
