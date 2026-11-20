"""The ``image_generate`` and ``text_to_speech`` tools of the sama MCP server
(docs/architecture.md, Section 11.8; roadmap M4.9)."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.gate.decide import DEFAULT_ALLOW, TOOLSETS
from sama.runtime.contract import Audience, RunScope
from sama.tenancy import RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from tests.conformance.scripted_model import serve
from tests.media.support import PNG
from tests.media.test_generate import KEY, OGG, build
from tests.media.test_generate import Rig as ServiceRig
from tests.orchestrator.conftest import Tenant
from tests.tools.test_server import connect, text_of


@dataclass
class Rig:
    url: str
    signer: RunTokenSigner
    services: ServiceRig

    def token(self) -> str:
        claims = self.services.claims()
        scope = RunScope(
            org_id=claims.org_id,
            workspace_id=claims.workspace_id,
            session_id=claims.session_id,
            run_id=claims.run_id,
            audience=Audience.PRIVATE,
            user_id=claims.user_id,
        )
        return self.signer.issue(scope, ttl_s=600)


@pytest.fixture
async def rig(engine: AsyncEngine, tenant: Tenant) -> AsyncIterator[Rig]:
    signer = RunTokenSigner(secrets.token_bytes(32))
    services = await build(engine, tenant)
    server = ToolServer(
        signer=signer,
        todos=MemoryTodoStore(),
        images=services.images,
        speech=services.speech,
    )
    with serve(server.app()) as base:
        yield Rig(base + "/mcp", signer, services)


def test_the_tools_are_in_the_media_toolset_and_allowed_by_default() -> None:
    assert {"image_generate", "text_to_speech"} <= TOOLSETS["media"]
    assert {"image_generate", "text_to_speech"} <= DEFAULT_ALLOW


async def test_the_tools_are_hidden_without_the_services() -> None:
    signer = RunTokenSigner(secrets.token_bytes(32))
    server = ToolServer(signer=signer, todos=MemoryTodoStore())
    with serve(server.app()) as base:
        scope = RunScope(
            org_id="o", workspace_id="w", session_id="s", run_id="r", audience=Audience.SHARED
        )
        async with connect(base + "/mcp", signer.issue(scope, ttl_s=60)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
    assert not {"image_generate", "text_to_speech"} & names


async def test_the_tools_have_their_arguments(rig: Rig) -> None:
    async with connect(rig.url, rig.token()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    image = tools["image_generate"].input_schema
    speech = tools["text_to_speech"].input_schema
    assert set(image["properties"]) == {"prompt", "size"}
    assert image["required"] == ["prompt"]
    assert set(speech["properties"]) == {"text", "voice"}
    assert speech["required"] == ["text"]


async def test_the_tools_store_the_files_for_the_run(rig: Rig) -> None:
    async with connect(rig.url, rig.token()) as client:
        image = await client.call_tool("image_generate", {"prompt": "a fox"})
        voice = await client.call_tool("text_to_speech", {"text": "hello", "voice": "onyx"})
    assert not image.is_error
    assert not voice.is_error
    assert "The image is ready" in text_of(image)
    assert "The voice note is ready" in text_of(voice)
    services = rig.services
    pending = await services.store.pending(services.tenant.org, services.run_id)
    assert [m.data for m in pending] == [PNG, OGG]


async def test_a_bad_size_is_an_argument_error(rig: Rig) -> None:
    async with connect(rig.url, rig.token()) as client:
        result = await client.call_tool("image_generate", {"prompt": "a fox", "size": "1x1"})
    assert result.is_error
    assert rig.services.api.requests == []


async def test_a_provider_error_is_a_tool_error_without_the_key(rig: Rig) -> None:
    rig.services.api.status = 401
    async with connect(rig.url, rig.token()) as client:
        result = await client.call_tool("image_generate", {"prompt": "a fox"})
    assert result.is_error
    assert "refused the key of the org" in text_of(result)
    assert KEY not in text_of(result)
