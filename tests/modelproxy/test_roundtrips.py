"""The database round trips of one model proxy call (docs/architecture.md, Sections 4.4 and 16.6).

One round trip takes about 58 ms from the control plane on Fly to the database. Before it
forwards a call, the proxy reads the live authority of the run, the spend of the run, the limits
of the org, and the spend of the org and of the user. These tests count the statements and the
transaction commands of that part with a real database, and fail when a call needs more than its
limit. Raise a limit only with a reason in the pull request.

Before the change (M8.2, part 4), the proxy read in five transactions of its own, and the counts
before the call went to the provider were: 16 for a call with a run budget, a monthly quota, and a
daily quota, 10 with a run budget and no quota, 4 for ``count_tokens``, and 3 for the call of a run
that ended. Now one connection without a transaction reads the authority and one statement reads
the rest.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import ModelCall, Org, Run
from sama.db.trips import Trips, count_trips
from sama.modelproxy import ModelProxy, PostgresUsageStore
from sama.modelproxy.prices import Provider, TokenCounts
from sama.modelproxy.store import CallRecord, LiveState, OrgLimits, live_from_parts
from sama.modelproxy.usage import Endpoint
from sama.runtime import Budget
from sama.runtime.contract import Audience, RunScope
from sama.tenancy.run_token import RunTokenSigner
from sama.vault.store import KeySource
from tests.modelproxy.test_app import MODEL, Upstream, make_proxy
from tests.orchestrator import conftest as queue_fixtures
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness as QueueHarness

KEY = b"k" * 32

# The database fixtures of the run queue tests: a migrated database and an org with sessions.
engine = queue_fixtures.engine
queue_database_url = queue_fixtures.queue_database_url
tenant = queue_fixtures.tenant

FULL = 3
"""A call with a run budget, a monthly quota, and a daily quota: the run, the grants of its user,
and one statement for the limits, the spend of the run, and the costs of the org and the user."""
BUDGET_ONLY = 3
"""The same statement reads the limits, so a call with no quota makes the same three."""
AUTHORITY_ONLY = 2
"""``count_tokens`` reads only the authority: the run and the grants."""
ENDED = 1
"""The call of a run that ended stops after the first statement. It has no user to check."""


@pytest.fixture
async def world(
    engine: AsyncEngine, tenant: Tenant
) -> AsyncIterator[tuple[ModelProxy, Upstream, RunScope, RunTokenSigner]]:
    queue = QueueHarness(engine)
    run_id = await queue.send(tenant, "mine", "hi")
    scope = RunScope(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids["mine"]),
        run_id=str(run_id),
        audience=Audience.PRIVATE,
        user_id=str(tenant.ids["mia"]),
    )
    upstream, signer = Upstream(), RunTokenSigner(KEY)
    proxy = make_proxy(upstream, PostgresUsageStore(engine), signer)
    # Some spend, so the sums read rows.
    store = PostgresUsageStore(engine)
    await store.record(
        CallRecord(
            org_id=scope.org_id,
            workspace_id=scope.workspace_id,
            session_id=scope.session_id,
            run_id=scope.run_id,
            user_id=scope.user_id,
            provider=Provider.ANTHROPIC,
            model=MODEL,
            endpoint=Endpoint.MESSAGES,
            status_code=200,
            counts=TokenCounts(input=10, output=5),
            key_source=KeySource.SAMA,
            cost_usd=Decimal("0.01"),
            duration_ms=5,
            created_at=datetime.now(UTC),
        )
    )
    yield proxy, upstream, scope, signer
    await proxy.aclose()


async def set_limits(engine: AsyncEngine, tenant: Tenant, limits: dict[str, str]) -> None:
    async with engine.begin() as connection:
        await connection.execute(update(Org).where(Org.id == tenant.org).values(limits=limits))


async def call(
    proxy: ModelProxy,
    upstream: Upstream,
    signer: RunTokenSigner,
    scope: RunScope,
    path: str,
    budget: Budget,
) -> tuple[httpx.Response, int, Trips]:
    """The answer, and the round trips until the call reached the provider (the reads)."""
    token = signer.issue(scope, ttl_s=600, budget=budget)
    transport = httpx.ASGITransport(app=proxy.app())
    seen: list[int] = []
    original = upstream.reply

    def forward(request: httpx.Request) -> httpx.Response:
        seen.append(trips.total)
        return original(request)

    upstream.reply = forward
    async with httpx.AsyncClient(transport=transport, base_url="http://p") as client:
        with count_trips() as trips:
            response = await client.post(
                path,
                json={"model": MODEL, "max_tokens": 10, "messages": []},
                headers={"x-api-key": token},
            )
    return response, (seen[0] if seen else trips.total), trips


async def test_a_call_with_every_limit_makes_few_round_trips(
    engine: AsyncEngine,
    tenant: Tenant,
    world: tuple[ModelProxy, Upstream, RunScope, RunTokenSigner],
) -> None:
    proxy, upstream, scope, signer = world
    await set_limits(engine, tenant, {"model_usd_per_month": "100", "model_usd_per_user_day": "50"})
    budget = Budget(max_cost_usd=Decimal("5"), max_tokens=100_000)
    response, reads, trips = await call(
        proxy, upstream, signer, scope, "/anthropic/v1/messages", budget
    )
    assert response.status_code == 200
    assert len(upstream.requests) == 1
    assert reads <= FULL, trips.show()


async def test_a_call_with_a_run_budget_only(
    engine: AsyncEngine,
    tenant: Tenant,
    world: tuple[ModelProxy, Upstream, RunScope, RunTokenSigner],
) -> None:
    proxy, upstream, scope, signer = world
    await set_limits(engine, tenant, {})
    response, reads, trips = await call(
        proxy,
        upstream,
        signer,
        scope,
        "/anthropic/v1/messages",
        Budget(max_cost_usd=Decimal("5")),
    )
    assert response.status_code == 200
    assert reads <= BUDGET_ONLY, trips.show()


async def test_count_tokens_reads_only_the_authority(
    engine: AsyncEngine,
    tenant: Tenant,
    world: tuple[ModelProxy, Upstream, RunScope, RunTokenSigner],
) -> None:
    proxy, upstream, scope, signer = world
    await set_limits(engine, tenant, {"model_usd_per_month": "100"})
    response, reads, trips = await call(
        proxy, upstream, signer, scope, "/anthropic/v1/messages/count_tokens", Budget()
    )
    assert response.status_code == 200
    assert reads <= AUTHORITY_ONLY, trips.show()
    assert ModelCall.__tablename__ not in " ".join(trips.text)


async def test_an_ended_run_is_refused_with_the_authority_read_only(
    engine: AsyncEngine,
    tenant: Tenant,
    world: tuple[ModelProxy, Upstream, RunScope, RunTokenSigner],
) -> None:
    proxy, upstream, scope, signer = world
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == uuid.UUID(scope.run_id)).values(status="cancelled")
        )
    response, reads, trips = await call(
        proxy, upstream, signer, scope, "/anthropic/v1/messages", Budget()
    )
    assert response.status_code == 403
    assert upstream.requests == []
    assert reads <= ENDED, trips.show()


@pytest.mark.parametrize(
    "limits",
    [
        {},
        {"model_usd_per_month": "100"},
        {"model_usd_per_user_day": "50"},
        {"model_usd_per_month": "0.001", "model_usd_per_user_day": "0.001"},
    ],
)
@pytest.mark.parametrize("with_user", [True, False])
@pytest.mark.parametrize("recorded", [True, False])
async def test_the_combined_read_gives_what_the_single_reads_give(
    engine: AsyncEngine,
    tenant: Tenant,
    world: tuple[ModelProxy, Upstream, RunScope, RunTokenSigner],
    limits: dict[str, str],
    with_user: bool,
    recorded: bool,
) -> None:
    _, _, scope, _ = world
    await set_limits(engine, tenant, limits)
    store = PostgresUsageStore(engine)
    since = datetime.now(UTC) - timedelta(days=1)
    user_id = scope.user_id if with_user else None
    combined = await store.live(
        scope.org_id,
        scope.run_id,
        user_id=user_id,
        month_since=since,
        day_since=since,
        recorded=recorded,
    )
    single = await live_from_parts(
        store,
        scope.org_id,
        scope.run_id,
        user_id=user_id,
        month_since=since,
        day_since=since,
        recorded=recorded,
    )
    assert combined == single
    if recorded:
        assert combined.spend is not None
        assert combined.spend.cost_usd == Decimal("0.01")
    assert (combined.month_cost is not None) == ("model_usd_per_month" in limits and recorded)
    assert (combined.day_cost is not None) == (
        "model_usd_per_user_day" in limits and recorded and with_user
    )


async def test_the_read_is_scoped_to_the_org_of_the_token(
    engine: AsyncEngine,
    tenant: Tenant,
    world: tuple[ModelProxy, Upstream, RunScope, RunTokenSigner],
) -> None:
    """The run of one org is unknown to another org: the read gives no spend and no limits."""
    _, _, scope, _ = world
    store = PostgresUsageStore(engine)
    since = datetime.now(UTC) - timedelta(days=1)
    other = await store.live(
        str(uuid.uuid4()),
        scope.run_id,
        user_id=scope.user_id,
        month_since=since,
        day_since=since,
        recorded=True,
    )
    assert other.authority.problem == "the run is unknown"
    assert other.spend is None
    assert other.limits == OrgLimits()


async def test_a_store_that_cannot_read_refuses_the_call_with_503(
    engine: AsyncEngine, world: tuple[ModelProxy, Upstream, RunScope, RunTokenSigner]
) -> None:
    _, upstream, scope, signer = world

    class Down(PostgresUsageStore):
        async def live(self, *args: Any, **kwargs: Any) -> LiveState:
            raise OSError("the database is down")

    proxy = make_proxy(upstream, Down(engine), signer)
    response, _, _ = await call(proxy, upstream, signer, scope, "/anthropic/v1/messages", Budget())
    await proxy.aclose()
    assert response.status_code == 503
    assert upstream.requests == []
