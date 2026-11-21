"""Tests of the org regions (docs/architecture.md, Section 17.3, M6.7)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AuditEntry, Org
from sama.orchestrator.queue import RunQueue
from sama.tenancy.regions import (
    DEFAULT_REGION,
    OrgInOtherRegion,
    RegionError,
    Regions,
    check_here,
    check_region,
    home_regions,
    member_user_id,
    org_region,
    parse_region_urls,
    set_home_regions,
    set_org_region,
)
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)

EU = "https://eu.sama.test"
TWO = Regions.of(DEFAULT_REGION, "https://sama.test/", {"eu-central-1": EU})


@pytest.mark.parametrize("region", ["ap-south-1", "eu-central-1", "us-east-2"])
def test_a_region_id_is_accepted(region: str) -> None:
    assert check_region(region) == region


@pytest.mark.parametrize("region", ["", "AP-SOUTH-1", "ap-south", "ap-south-12", "mars", "a-b-1"])
def test_a_value_that_is_not_a_region_id_is_refused(region: str) -> None:
    with pytest.raises(RegionError):
        check_region(region)


def test_the_region_urls_are_pairs_with_commas() -> None:
    assert parse_region_urls("") == {}
    assert parse_region_urls(f" eu-central-1 = {EU}/ , us-east-1=http://us.test ,") == {
        "eu-central-1": EU,
        "us-east-1": "http://us.test",
    }


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("eu-central-1", "region=url pairs"),
        (f"eu-central-1={EU},eu-central-1={EU}", "two times"),
        ("europe=https://eu.test", "not a region id"),
        ("eu-central-1=ftp://eu.test", "http or https"),
        ("eu-central-1=eu.test", "http or https"),
        ("eu-central-1=https://eu.test/app", "no path"),
        ("eu-central-1=https://eu.test/?a=1", "no path"),
    ],
)
def test_bad_region_urls_are_refused(value: str, message: str) -> None:
    with pytest.raises(RegionError, match=message):
        parse_region_urls(value)


def test_the_regions_of_a_deployment() -> None:
    assert TWO.home == DEFAULT_REGION
    assert TWO.known() == ["ap-south-1", "eu-central-1"]
    assert TWO.here(DEFAULT_REGION)
    assert not TWO.here("eu-central-1")
    assert (TWO.url(DEFAULT_REGION), TWO.url("eu-central-1")) == ("https://sama.test", EU)
    assert TWO.url("us-east-1") is None
    alone = Regions.of("eu-central-1", None, {})
    assert (alone.known(), alone.url("eu-central-1")) == (["eu-central-1"], None)
    same = Regions.of(DEFAULT_REGION, "https://sama.test", {DEFAULT_REGION: "https://sama.test/"})
    assert same.url(DEFAULT_REGION) == "https://sama.test"
    with pytest.raises(RegionError, match="not SAMA_PUBLIC_URL"):
        Regions.of(DEFAULT_REGION, "https://sama.test", {DEFAULT_REGION: "https://other.test"})
    with pytest.raises(RegionError):
        Regions("mars")


def test_the_process_has_one_home_region() -> None:
    assert home_regions().home == DEFAULT_REGION
    before = set_home_regions(TWO)
    assert home_regions() is TWO
    check_here(DEFAULT_REGION)
    with pytest.raises(OrgInOtherRegion) as raised:
        check_here("eu-central-1")
    assert (raised.value.region, raised.value.url) == ("eu-central-1", EU)
    assert set_home_regions(before) is TWO


async def _move(engine: AsyncEngine, org_id: uuid.UUID, region: str) -> None:  # noqa: F811
    async with engine.begin() as connection:
        await connection.execute(update(Org).where(Org.id == org_id).values(region=region))


async def test_a_member_of_an_org_in_another_region_is_sent_there(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    set_home_regions(TWO)
    async with engine.connect() as connection:
        found = await member_user_id(connection, tenant.ids["mia_auth"], tenant.org)
        assert found == tenant.ids["mia"]
        assert await member_user_id(connection, uuid.uuid4(), tenant.org) is None
    await _move(engine, tenant.org, "eu-central-1")
    async with engine.connect() as connection:
        with pytest.raises(OrgInOtherRegion) as raised:
            await member_user_id(connection, tenant.ids["mia_auth"], tenant.org)
        assert raised.value.url == EU
        # A person who is not a member does not learn the region.
        assert await member_user_id(connection, uuid.uuid4(), tenant.org) is None


async def test_the_queue_runs_only_the_runs_of_the_home_region(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    queue = RunQueue(engine)
    run_id = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["s1"], text="hi", user_id=tenant.ids["mia"]
    )
    await _move(engine, tenant.org, "eu-central-1")
    assert await queue.claim("worker-region") is None
    set_home_regions(Regions("eu-central-1"))
    claimed = await queue.claim("worker-region")
    assert claimed is not None
    assert claimed.run_id == run_id


async def test_an_operator_moves_an_org_to_a_known_region(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    assert await org_region(engine, tenant.org) == DEFAULT_REGION
    assert await org_region(engine, uuid.uuid4()) is None
    with pytest.raises(RegionError, match="not a known region"):
        await set_org_region(engine, tenant.org, "us-east-1", TWO)
    with pytest.raises(RegionError, match="does not exist"):
        await set_org_region(engine, uuid.uuid4(), "eu-central-1", TWO)
    assert await set_org_region(engine, tenant.org, "eu-central-1", TWO) == DEFAULT_REGION
    assert await set_org_region(engine, tenant.org, "eu-central-1", TWO) == "eu-central-1"
    assert await org_region(engine, tenant.org) == "eu-central-1"
    assert await set_org_region(engine, tenant.org, DEFAULT_REGION, TWO) == "eu-central-1"
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(AuditEntry.actor_kind, AuditEntry.details)
                .where(AuditEntry.org_id == tenant.org, AuditEntry.action == "org.region_changed")
                .order_by(AuditEntry.id)
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        ("system", {"from": DEFAULT_REGION, "to": "eu-central-1"}),
        ("system", {"from": "eu-central-1", "to": DEFAULT_REGION}),
    ]
