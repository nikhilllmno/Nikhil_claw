"""Fixtures for the API tests. They use the database and the tenants of the queue tests."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.tools.clarify import ClarifyBroker
from tests.orchestrator.conftest import engine, queue_database_url, tenant  # noqa: F401

SECRET = "test-secret-with-at-least-32-bytes!!"
ISSUER = "https://project.example.test/auth/v1"


def token(
    auth_user_id: uuid.UUID | str,
    *,
    secret: str = SECRET,
    expires_in: float = 300,
    **claims: Any,
) -> str:
    body: dict[str, Any] = {
        "sub": str(auth_user_id),
        "aud": "authenticated",
        "role": "authenticated",
        "iss": ISSUER,
        "exp": int(time.time() + expires_in),
        "email": "mia@example.test",
    }
    body.update(claims)
    return jwt.encode({k: v for k, v in body.items() if v is not None}, secret, algorithm="HS256")


def bearer(auth_user_id: uuid.UUID | str, **claims: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(auth_user_id, **claims)}"}


@pytest.fixture
def broker() -> ClarifyBroker:
    return ClarifyBroker(wait_s=5)


@pytest.fixture
def api(engine: AsyncEngine, broker: ClarifyBroker) -> SamaApi:  # noqa: F811
    return SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        clarify=broker,
        settings=ApiSettings(
            supabase_url="https://project.example.test",
            supabase_anon_key="anon-key",
            poll_s=0.02,
            heartbeat_s=0.05,
            max_stream_s=0.3,
        ),
    )


@pytest.fixture
async def client(api: SamaApi) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client
