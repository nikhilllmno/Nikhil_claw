"""The data control routes of M6.8 (docs/architecture.md, Section 17.3)."""

from __future__ import annotations

import io
import json
import os
import uuid
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.datacontrols.service import DataControls
from sama.db.models import Membership, Org, Session, User
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.db.world import World


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
async def client(engine: AsyncEngine, export_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        settings=ApiSettings(
            supabase_url="https://project.example.test", supabase_anon_key="anon-key"
        ),
        data=DataControls(engine, export_dir=export_dir),
    )
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as http:
        yield http


def as_(world: World, name: str) -> dict[str, str]:
    return bearer(world.auth[name])


async def slug_of(engine: AsyncEngine, org_id: uuid.UUID) -> str:
    async with engine.connect() as connection:
        return (await connection.execute(select(Org.slug).where(Org.id == org_id))).scalar_one()


async def test_a_user_exports_their_own_data_as_a_zip(
    client: httpx.AsyncClient, fresh: World, export_dir: Path
) -> None:
    org_a, alice = fresh.ids["org-a"], fresh.ids["alice"]
    response = await client.get(f"/orgs/{org_a}/users/{alice}/export", headers=as_(fresh, "alice"))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["kind"] == "user"
    assert manifest["user_id"] == str(alice)
    assert not os.listdir(export_dir)


async def test_only_the_user_or_an_owner_can_export_a_user(
    client: httpx.AsyncClient, fresh: World
) -> None:
    org_a, alice = fresh.ids["org-a"], fresh.ids["alice"]
    path = f"/orgs/{org_a}/users/{alice}/export"
    assert (await client.get(path, headers=as_(fresh, "dave"))).status_code == 403
    assert (await client.get(path, headers=as_(fresh, "carol"))).status_code == 403
    assert (await client.get(path, headers=as_(fresh, "stranger"))).status_code == 404
    bob_path = f"/orgs/{fresh.ids['org-b']}/users/{alice}/export"
    assert (await client.get(bob_path, headers=as_(fresh, "bob"))).status_code == 404


async def test_only_an_owner_exports_the_org(client: httpx.AsyncClient, fresh: World) -> None:
    org_a, org_b = fresh.ids["org-a"], fresh.ids["org-b"]
    assert (
        await client.get(f"/orgs/{org_a}/export", headers=as_(fresh, "carol"))
    ).status_code == 403
    response = await client.get(f"/orgs/{org_b}/export", headers=as_(fresh, "bob"))
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        sessions = [
            json.loads(line) for line in archive.read("tables/sessions.jsonl").decode().splitlines()
        ]
    assert {row["org_id"] for row in sessions} == {str(org_b)}


async def test_a_member_cannot_delete_another_user(client: httpx.AsyncClient, fresh: World) -> None:
    org_a, alice = fresh.ids["org-a"], fresh.ids["alice"]
    response = await client.delete(f"/orgs/{org_a}/users/{alice}", headers=as_(fresh, "dave"))
    assert response.status_code == 403


async def test_an_admin_deletes_a_member_but_not_an_owner(
    client: httpx.AsyncClient, engine: AsyncEngine, fresh: World
) -> None:
    org_a, alice, dave = fresh.ids["org-a"], fresh.ids["alice"], fresh.ids["dave"]
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.user_id == dave, Membership.workspace_id.is_(None))
            .values(role="owner")
        )
    owner = await client.delete(f"/orgs/{org_a}/users/{dave}", headers=as_(fresh, "carol"))
    assert owner.status_code == 403

    response = await client.delete(f"/orgs/{org_a}/users/{alice}", headers=as_(fresh, "carol"))
    assert response.status_code == 200
    assert response.json() == {"sessions": 1, "runs": 1}
    async with engine.connect() as connection:
        found = (await connection.execute(select(User.id).where(User.id == alice))).first()
    assert found is None


async def test_the_last_owner_cannot_delete_themself(
    client: httpx.AsyncClient, fresh: World
) -> None:
    org_b, bob = fresh.ids["org-b"], fresh.ids["bob"]
    response = await client.delete(f"/orgs/{org_b}/users/{bob}", headers=as_(fresh, "bob"))
    assert response.status_code == 409


async def test_an_org_delete_needs_the_owner_and_the_slug(
    client: httpx.AsyncClient, engine: AsyncEngine, fresh: World
) -> None:
    org_a, org_b = fresh.ids["org-a"], fresh.ids["org-b"]
    slug = await slug_of(engine, org_b)
    path = f"/orgs/{org_b}"
    a_slug = await slug_of(engine, org_a)
    assert (
        await client.delete(f"/orgs/{org_a}?confirm={a_slug}", headers=as_(fresh, "carol"))
    ).status_code == 403
    assert (await client.delete(path, headers=as_(fresh, "bob"))).status_code == 422
    wrong = await client.delete(f"{path}?confirm=nope", headers=as_(fresh, "bob"))
    assert wrong.status_code == 400

    response = await client.delete(f"{path}?confirm={slug}", headers=as_(fresh, "bob"))

    assert response.status_code == 200
    async with engine.connect() as connection:
        orgs = set(
            (await connection.execute(select(Org.id).where(Org.id.in_([org_a, org_b])))).scalars()
        )
        a_sessions = (
            await connection.execute(select(Session.id).where(Session.org_id == org_a))
        ).all()
    assert orgs == {org_a}
    assert len(a_sessions) == 3


async def test_the_retention_setting_is_read_by_members_and_set_by_admins(
    client: httpx.AsyncClient, fresh: World
) -> None:
    path = f"/orgs/{fresh.ids['org-a']}/data-settings"
    read = await client.get(path, headers=as_(fresh, "dave"))
    assert read.status_code == 200
    assert read.json()["retention_days"] is None

    body = {"retention_days": 90}
    assert (await client.put(path, json=body, headers=as_(fresh, "dave"))).status_code == 403
    assert (
        await client.put(path, json={"retention_days": 0}, headers=as_(fresh, "carol"))
    ).status_code == 422
    assert (await client.put(path, json={}, headers=as_(fresh, "carol"))).status_code == 422
    changed = await client.put(path, json=body, headers=as_(fresh, "carol"))
    assert changed.status_code == 200
    assert changed.json()["retention_days"] == 90
    assert changed.json()["effective_days"] == 90
    cleared = await client.put(path, json={"retention_days": None}, headers=as_(fresh, "carol"))
    assert cleared.json()["effective_days"] is None
