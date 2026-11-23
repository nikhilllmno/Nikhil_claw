"""The browser of a session and the Chrome extension card (M8.3, Section 14.2.4)."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.api.extension import PLACEHOLDER_API_URL, ExtensionError, build_for
from sama.db.models import AuditEntry, Session
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.orchestrator.conftest import Tenant, engine, queue_database_url, tenant

__all__ = ["engine", "queue_database_url", "tenant"]

API = "https://sama-staging.fly.dev"


def make_zip(path: Path) -> Path:
    manifest = {
        "manifest_version": 3,
        "host_permissions": [
            "https://sama-staging.fly.dev/*",
            "https://sama-production.fly.dev/*",
            f"{PLACEHOLDER_API_URL}/*",
        ],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("background.js", f'const API = "{PLACEHOLDER_API_URL}";')
        archive.writestr("icons/16.png", b"\x89PNG" + PLACEHOLDER_API_URL.encode())
    return path


@pytest.fixture
async def client(
    engine: AsyncEngine,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        settings=ApiSettings(
            supabase_url="https://p.test",
            supabase_anon_key="anon",
            public_url=API,
            extension_zip=str(make_zip(tmp_path / "ext.zip")),
        ),
    )
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


async def test_the_owner_sets_the_browser_of_the_session(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    tenant: Tenant,
) -> None:
    mine = tenant.ids["mine"]
    headers = bearer(tenant.ids["mia_auth"])
    body = (await client.get(f"/sessions/{mine}", headers=headers)).json()
    assert body["browser_target"] is None
    changed = await client.patch(
        f"/sessions/{mine}", headers=headers, json={"browser": "my_chrome"}
    )
    assert changed.status_code == 200
    assert changed.json()["browser_target"] == "my_chrome"
    bad = await client.patch(f"/sessions/{mine}", headers=headers, json={"browser": "firefox"})
    assert bad.status_code == 422
    back = await client.patch(f"/sessions/{mine}", headers=headers, json={"browser": None})
    assert back.json()["browser_target"] is None
    async with engine.connect() as connection:
        stored = (
            await connection.execute(select(Session.browser_target).where(Session.id == mine))
        ).scalar_one()
        actions = (
            await connection.execute(
                select(AuditEntry.details).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.action == "session.browser_changed"
                )
            )
        ).scalars()
        details = list(actions)
    assert stored is None
    assert details == [{"from": None, "to": "my_chrome"}, {"from": "my_chrome", "to": None}]


async def test_a_member_cannot_change_the_browser_of_a_private_session_of_another(
    client: httpx.AsyncClient,
    tenant: Tenant,
) -> None:
    stranger = bearer(uuid.uuid4())
    response = await client.patch(
        f"/sessions/{tenant.ids['mine']}", headers=stranger, json={"browser": "cloud"}
    )
    assert response.status_code in (403, 404)


async def test_the_chrome_state_and_the_download(
    client: httpx.AsyncClient,
    tenant: Tenant,
) -> None:
    headers = bearer(tenant.ids["mia_auth"])
    state = (await client.get("/me/chrome", headers=headers)).json()
    assert state == {"connected": False, "connections": 0, "download": True}
    response = await client.get("/extension/download", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        script = archive.read("background.js").decode()
        icon = archive.read("icons/16.png")
    assert manifest["host_permissions"] == [
        "https://sama-staging.fly.dev/*",
        "https://sama-production.fly.dev/*",
    ]
    assert script == f'const API = "{API}";'
    assert PLACEHOLDER_API_URL.encode() in icon  # binary files stay as they are
    assert (await client.get("/extension/download")).status_code == 401


def test_the_build_needs_a_file_and_an_https_address(tmp_path: Path) -> None:
    with pytest.raises(ExtensionError, match="not in this image"):
        build_for(str(tmp_path / "missing.zip"), API)
    path = make_zip(tmp_path / "ext.zip")
    with pytest.raises(ExtensionError, match="public address"):
        build_for(str(path), "http://insecure.test")
    with zipfile.ZipFile(tmp_path / "evil.zip", "w") as archive:
        archive.writestr("../escape.js", "x")
    with pytest.raises(ExtensionError, match="not valid"):
        build_for(str(tmp_path / "evil.zip"), API)
