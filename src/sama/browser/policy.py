"""The URL policy of the browser of each org (docs/architecture.md, Section 16.4; roadmap M5.3).

An org admin sets two lists of hosts:

- ``allow_hosts``: when it is not empty, the browser opens only these hosts and their
  subdomains (allow list mode).
- ``block_hosts``: the browser never opens these hosts and their subdomains.

The lists add to the address rules of ``sama.browser.guard``: a private, loopback, link-local,
or metadata address is always refused, also when a list names it. The lists apply to each
navigation of a tab or a frame, to each redirect hop, and to each WebSocket. The block list also
applies to the other requests of a page (images, scripts, ``fetch``). The allow list does not,
because a page loads its scripts and styles from other hosts.

Hosts are compared in their IDNA (punycode) form, in lower case, without a final dot, as in
``OutboundClient``. ``PolicyCache`` keeps the policy of an org for ``CACHE_S`` seconds, so a
change shows in a live browser session after at most that time (at once in the process that
made the change).
"""

from __future__ import annotations

import ipaddress
import re
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from urllib.parse import urlsplit

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql import func

from sama.db.models import AuditEntry, BrowserUrlPolicy
from sama.tools.outbound import ascii_host

MAX_HOSTS: Final = 200
"""The most hosts in one list."""
MAX_HOST_CHARS: Final = 253
CACHE_S: Final = 30.0
_HOST: Final = re.compile(r"[a-z0-9_.-]+")


class PolicyError(ValueError):
    """A list of hosts that is not valid. The message is for the admin."""


@dataclass(frozen=True, slots=True)
class OrgUrlPolicy:
    """The host lists of one org, in their normal form (``normal_hosts``)."""

    allow_hosts: tuple[str, ...] = ()
    block_hosts: tuple[str, ...] = ()

    def host_problem(self, host: str) -> str | None:
        """Why the lists refuse ``host`` (in its IDNA form), or ``None``."""
        if self.allow_hosts and not host_matches(host, self.allow_hosts):
            return f"{host} is not in the allow list of the org (the org allows only listed hosts)"
        if host_matches(host, self.block_hosts):
            return f"{host} is in the block list of the org"
        return None

    def block_problem(self, host: str) -> str | None:
        """The block list alone, for the requests of a page that are not a navigation."""
        if host_matches(host, self.block_hosts):
            return f"{host} is in the block list of the org"
        return None


OPEN: Final = OrgUrlPolicy()
"""The policy of an org without lists: only the address rules apply."""


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    return any(host == pattern or host.endswith("." + pattern) for pattern in patterns)


def url_host(url: str) -> str | None:
    """The host of ``url`` in its IDNA form, or ``None`` for a URL without a host (``data:``,
    ``about:blank``). Raises ``ValueError`` for a host that is not valid."""
    host = urlsplit(url).hostname
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return ascii_host(host)


def normal_host(value: str) -> str:
    """One entry of a list in its normal form. It accepts ``example.com``, ``*.example.com``,
    ``.example.com``, and a URL such as ``https://example.com/login``: the entry is the host,
    and it covers the subdomains."""
    entry = value.strip()
    if "://" in entry:
        try:
            entry = urlsplit(entry).hostname or ""
        except ValueError:
            raise PolicyError(f"{value.strip()[:100]!r} is not a valid host") from None
    entry = entry.removeprefix("*.").removeprefix(".")
    if not entry or len(entry) > MAX_HOST_CHARS or any(c in entry for c in "/:@*? \t"):
        raise PolicyError(f"{value.strip()[:100]!r} is not a valid host")
    try:
        return str(ipaddress.ip_address(entry))
    except ValueError:
        pass
    try:
        host = ascii_host(entry)
    except ValueError:
        raise PolicyError(f"{value.strip()[:100]!r} is not a valid host") from None
    if not _HOST.fullmatch(host) or len(host) > MAX_HOST_CHARS:
        raise PolicyError(f"{value.strip()[:100]!r} is not a valid host")
    return host


def normal_hosts(values: Iterable[str]) -> tuple[str, ...]:
    """A list in its normal form: valid hosts, no duplicates, sorted. Empty lines are left
    out."""
    hosts = sorted({normal_host(value) for value in values if value.strip()})
    if len(hosts) > MAX_HOSTS:
        raise PolicyError(f"a list can have at most {MAX_HOSTS} hosts")
    return tuple(hosts)


@dataclass(frozen=True, slots=True)
class PolicyView:
    """What an org admin sees."""

    policy: OrgUrlPolicy
    updated_at: datetime | None

    def public(self) -> dict[str, object]:
        return {
            "allow_hosts": list(self.policy.allow_hosts),
            "block_hosts": list(self.policy.block_hosts),
            "mode": "allow_list" if self.policy.allow_hosts else "block_list",
            "updated_at": None if self.updated_at is None else self.updated_at.isoformat(),
        }


class PolicySource(Protocol):
    async def policy(self, org_id: str) -> OrgUrlPolicy:
        """The policy of the org. Raises an error when it cannot be read."""
        ...


class PolicyStore:
    """The URL policies in Postgres. The control plane writes as the table owner."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def policy(self, org_id: str) -> OrgUrlPolicy:
        return (await self.view(uuid.UUID(org_id))).policy

    async def view(self, org_id: uuid.UUID) -> PolicyView:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        BrowserUrlPolicy.allow_hosts,
                        BrowserUrlPolicy.block_hosts,
                        BrowserUrlPolicy.updated_at,
                    ).where(BrowserUrlPolicy.org_id == org_id)
                )
            ).first()
        if row is None:
            return PolicyView(OPEN, None)
        return PolicyView(OrgUrlPolicy(tuple(row[0]), tuple(row[1])), row[2])

    async def put(
        self,
        org_id: uuid.UUID,
        *,
        allow_hosts: Iterable[str],
        block_hosts: Iterable[str],
        actor_user_id: uuid.UUID | None,
    ) -> PolicyView:
        """Checks and stores the lists, and writes the audit entry ``browser.url_policy_set``.
        Raises ``PolicyError`` for a list that is not valid."""
        policy = OrgUrlPolicy(normal_hosts(allow_hosts), normal_hosts(block_hosts))
        values = {
            "allow_hosts": list(policy.allow_hosts),
            "block_hosts": list(policy.block_hosts),
            "updated_by_user_id": actor_user_id,
        }
        async with self._engine.begin() as connection:
            await connection.execute(
                pg_insert(BrowserUrlPolicy)
                .values(org_id=org_id, **values)
                .on_conflict_do_update(
                    index_elements=["org_id"], set_={**values, "updated_at": func.now()}
                )
            )
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    actor_kind="user" if actor_user_id else "system",
                    actor_user_id=actor_user_id,
                    action="browser.url_policy_set",
                    target_type="browser_url_policy",
                    target_id=str(org_id),
                    details={
                        "allow_hosts": list(policy.allow_hosts),
                        "block_hosts": list(policy.block_hosts),
                    },
                )
            )
        return await self.view(org_id)


class PolicyCache:
    """Keeps the policy of each org for ``cache_s`` seconds. An error is not kept."""

    def __init__(
        self,
        source: PolicySource,
        *,
        cache_s: float = CACHE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._cache_s = cache_s
        self._clock = clock
        self._known: dict[str, tuple[float, OrgUrlPolicy]] = {}

    async def policy(self, org_id: str) -> OrgUrlPolicy:
        return (await self.policy_until(org_id))[0]

    async def policy_until(self, org_id: str) -> tuple[OrgUrlPolicy, float]:
        """The policy and the clock time when the cache drops it."""
        now = self._clock()
        known = self._known.get(org_id)
        if known is not None and known[0] > now:
            return known[1], known[0]
        policy = await self._source.policy(org_id)
        self._known[org_id] = (now + self._cache_s, policy)
        return policy, now + self._cache_s

    def forget(self, org_id: str) -> None:
        """Drops the kept policy, so the next check reads the new one (after a change)."""
        self._known.pop(org_id, None)
