"""Org regions (docs/architecture.md, Section 17.3, M6.7).

Each org has a region (``orgs.region``). One sama deployment serves one region: its home region
(``SAMA_REGION``). The deployment runs only the runs of the orgs in its home region, and its API
refuses the org data of an org in another region with ``421`` and the address of the deployment
that serves the org (``SAMA_REGION_URLS``).

This is the routing seam. A second region needs a second deployment and a second database; the
operator steps are in Section 17.3. The home region is a setting of the process:
``controlplane.build`` sets it once at start, like ``default_redactor``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from sqlalchemy import ColumnElement, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import InstrumentedAttribute

from sama.db.models import REGION_PATTERN, AuditEntry, Org, User

DEFAULT_REGION: Final = "ap-south-1"
"""The region of the first deployment: Supabase in ``ap-south-1`` (Section 19.3)."""

_REGION: Final = re.compile(REGION_PATTERN)


class RegionError(ValueError):
    """A region id or a region address is not valid."""


def check_region(region: str) -> str:
    """Gives ``region`` if it is a region id, such as ``ap-south-1``."""
    if not _REGION.fullmatch(region):
        raise RegionError(f"{region!r} is not a region id such as {DEFAULT_REGION}")
    return region


def _url(region: str, value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise RegionError(f"the address of {region} must be an http or https URL")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise RegionError(f"the address of {region} must have no path, query, or fragment")
    return value.rstrip("/")


def parse_region_urls(value: str) -> dict[str, str]:
    """``SAMA_REGION_URLS``: ``region=url`` pairs, with commas between them."""
    urls: dict[str, str] = {}
    for item in value.split(","):
        if not item.strip():
            continue
        region, sep, url = item.partition("=")
        region = region.strip()
        if not sep:
            raise RegionError("SAMA_REGION_URLS must be region=url pairs, with commas")
        check_region(region)
        if region in urls:
            raise RegionError(f"SAMA_REGION_URLS names {region} two times")
        urls[region] = _url(region, url.strip())
    return urls


@dataclass(frozen=True, slots=True)
class Regions:
    """The home region of this deployment and the address of each known region."""

    home: str = DEFAULT_REGION
    urls: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_region(self.home)
        for region, url in self.urls.items():
            check_region(region)
            _url(region, url)

    @classmethod
    def of(cls, home: str, public_url: str | None, others: Mapping[str, str]) -> Regions:
        """The regions of a deployment: ``others`` and the home region at ``public_url``."""
        urls = dict(others)
        if public_url is not None:
            own = _url(home, public_url)
            if home in urls and urls[home].rstrip("/") != own:
                raise RegionError(
                    f"SAMA_REGION_URLS gives {home} an address that is not SAMA_PUBLIC_URL"
                )
            urls[home] = own
        return cls(home, urls)

    def known(self) -> list[str]:
        """The regions that an operator can give to an org: the home region and the regions
        with an address."""
        return sorted({self.home, *self.urls})

    def here(self, region: str) -> bool:
        """True if this deployment serves ``region``."""
        return region == self.home

    def url(self, region: str) -> str | None:
        """The address of the deployment that serves ``region``, if it is known."""
        return self.urls.get(region)


def regions_from_env(env: Mapping[str, str], public_url: str | None = None) -> Regions:
    """The regions of a deployment from ``SAMA_REGION`` and ``SAMA_REGION_URLS``. The control
    plane and the admin CLI read the variables here, so they read them the same way."""
    return Regions.of(
        env.get("SAMA_REGION", "").strip() or DEFAULT_REGION,
        public_url,
        parse_region_urls(env.get("SAMA_REGION_URLS", "")),
    )


class OrgInOtherRegion(Exception):
    """The org is in a region that this deployment does not serve. The API answers ``421``."""

    def __init__(self, region: str, url: str | None) -> None:
        super().__init__(f"the org is served in region {region}")
        self.region = region
        self.url = url


_regions = Regions()


def home_regions() -> Regions:
    """The regions of the process. ``controlplane.build`` sets them at start."""
    return _regions


def set_home_regions(regions: Regions) -> Regions:
    """Sets the regions of the process. Gives the regions from before, for tests."""
    global _regions
    before, _regions = _regions, regions
    return before


def check_here(region: str) -> None:
    """Raises ``OrgInOtherRegion`` if this deployment does not serve ``region``."""
    regions = home_regions()
    if not regions.here(region):
        raise OrgInOtherRegion(region, regions.url(region))


def served_here(org_id: InstrumentedAttribute[uuid.UUID]) -> ColumnElement[bool]:
    """A filter for the rows of the orgs that this deployment serves. A list that spans orgs
    (sessions, linked accounts) uses it, so an org in another region does not fail the list."""
    return org_id.in_(select(Org.id).where(Org.region == home_regions().home))


async def member_user_id(
    connection: AsyncConnection, auth_user_id: uuid.UUID, org_id: uuid.UUID
) -> uuid.UUID | None:
    """The user of the person in the org, or ``None`` if the person has no user there.

    Raises ``OrgInOtherRegion`` for a user of an org in another region: the data of the org is
    in the deployment of that region. A person without a user never learns the region."""
    row = (
        await connection.execute(
            select(User.id, Org.region)
            .join(Org, Org.id == User.org_id)
            .where(User.org_id == org_id, User.auth_user_id == auth_user_id)
        )
    ).one_or_none()
    if row is None:
        return None
    check_here(row.region)
    user_id: uuid.UUID = row.id
    return user_id


async def org_region(engine: AsyncEngine, org_id: uuid.UUID) -> str | None:
    """The region of the org, or ``None`` if the org does not exist. Runs as the owner of the
    database: only an operator command calls it."""
    async with engine.connect() as connection:
        region: str | None = await connection.scalar(select(Org.region).where(Org.id == org_id))
        return region


async def set_org_region(
    engine: AsyncEngine, org_id: uuid.UUID, region: str, regions: Regions
) -> str:
    """Moves the org to ``region``, one of ``regions.known()``. Gives the region from before.

    Only the setting changes. The operator moves the data of the org first (Section 17.3). The
    change writes the audit entry ``org.region_changed``."""
    if region not in regions.known():
        raise RegionError(
            f"{region} is not a known region; the known regions are {', '.join(regions.known())}"
        )
    async with engine.begin() as connection:
        before: str | None = await connection.scalar(
            select(Org.region).where(Org.id == org_id).with_for_update()
        )
        if before is None:
            raise RegionError("the org does not exist")
        if before != region:
            await connection.execute(update(Org).where(Org.id == org_id).values(region=region))
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    actor_kind="system",
                    action="org.region_changed",
                    target_type="org",
                    target_id=str(org_id),
                    details={"from": before, "to": region},
                )
            )
        return before
