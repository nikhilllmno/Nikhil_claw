"""The media routes of the API: list the images and voice notes of a session, and read one
(docs/architecture.md, Section 11.8; roadmap M4.9)."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api.conftest import bearer
from tests.gateway.media_support import CSV, OGG, PNG, add_media, latest_run
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness

pytestmark = pytest.mark.usefixtures("tenant")


def mia(tenant: Tenant) -> dict[str, str]:
    return bearer(tenant.ids["mia_auth"])


async def media_run(engine: AsyncEngine, tenant: Tenant, session: str) -> tuple[uuid.UUID, ...]:
    harness = Harness(engine)
    await harness.send(tenant, session, "draw")
    assert await harness.worker.run_once()
    run = await latest_run(engine, tenant.org)
    return (await add_media(engine, run, "image", PNG), await add_media(engine, run, "audio", OGG))


async def test_the_list_gives_the_files_newest_first_without_the_bytes(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    image, voice = await media_run(engine, tenant, "s1")
    response = await client.get(f"/sessions/{tenant.ids['s1']}/media", headers=mia(tenant))
    assert response.status_code == 200
    files = response.json()
    assert {item["id"] for item in files} == {str(image), str(voice)}
    assert {item["kind"]: item["size_bytes"] for item in files} == {
        "image": len(PNG),
        "audio": len(OGG),
    }
    assert all("data" not in item for item in files)
    limited = await client.get(
        f"/sessions/{tenant.ids['s1']}/media", headers=mia(tenant), params={"limit": 1}
    )
    assert len(limited.json()) == 1
    bad = await client.get(
        f"/sessions/{tenant.ids['s1']}/media", headers=mia(tenant), params={"limit": 0}
    )
    assert bad.status_code == 422


async def test_a_file_gives_its_bytes_and_type(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    image, voice = await media_run(engine, tenant, "s1")
    url = f"/sessions/{tenant.ids['s1']}/media"
    png = await client.get(f"{url}/{image}", headers=mia(tenant))
    assert png.status_code == 200
    assert png.content == PNG
    assert png.headers["content-type"] == "image/png"
    assert png.headers["cache-control"] == "private, max-age=3600"
    assert "image.png" in png.headers["content-disposition"]
    ogg = await client.get(f"{url}/{voice}", headers=mia(tenant))
    assert (ogg.content, ogg.headers["content-type"]) == (OGG, "audio/ogg")


async def test_a_file_of_another_session_or_user_is_not_found(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    image, _ = await media_run(engine, tenant, "s1")
    other = await client.get(f"/sessions/{tenant.ids['s2']}/media/{image}", headers=mia(tenant))
    assert other.status_code == 404
    missing = await client.get(
        f"/sessions/{tenant.ids['s1']}/media/{uuid.uuid4()}", headers=mia(tenant)
    )
    assert missing.status_code == 404
    stranger = bearer(uuid.uuid4())
    for path in ("media", f"media/{image}"):
        response = await client.get(f"/sessions/{tenant.ids['s1']}/{path}", headers=stranger)
        assert response.status_code == 404, path
    assert (await client.get(f"/sessions/{tenant.ids['s1']}/media")).status_code == 401


async def test_a_file_of_file_send_is_a_named_download_that_no_browser_runs(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine)
    await harness.send(tenant, "s1", "make a csv and give me the file")
    assert await harness.worker.run_once()
    run = await latest_run(engine, tenant.org)
    csv = await add_media(engine, run, "file", CSV, name="top5.csv")
    page = await add_media(
        engine, run, "file", b"<script>x</script>", name="r.html", mime_type="text/html"
    )
    url = f"/sessions/{tenant.ids['s1']}/media"
    listed = {item["id"]: item for item in (await client.get(url, headers=mia(tenant))).json()}
    assert (listed[str(csv)]["kind"], listed[str(csv)]["name"]) == ("file", "top5.csv")
    assert listed[str(csv)]["run_id"] == str(run.id)
    response = await client.get(f"{url}/{csv}", headers=mia(tenant))
    assert response.status_code == 200
    assert response.content == CSV
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="top5.csv"'
    html = await client.get(f"{url}/{page}", headers=mia(tenant))
    assert html.headers["content-security-policy"].startswith("sandbox; default-src 'self'")
    assert html.headers["x-content-type-options"] == "nosniff"
    assert html.headers["content-disposition"].startswith("attachment")
