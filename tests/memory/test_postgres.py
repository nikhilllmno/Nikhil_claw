"""The Postgres memory store and the memory service with a database (Section 8)."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Membership, Memory, MemoryRevision, Session
from sama.memory import (
    MemoryChangeError,
    MemoryDeniedError,
    MemoryScope,
    MemoryService,
    Operation,
    PostgresMemoryStore,
    Target,
    Writer,
    load_snapshot,
)
from sama.memory.core import Action
from sama.memory.store import Change
from sama.orchestrator.queue import RunQueue
from sama.tenancy.run_token import RunTokenClaims
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")


def add(content: str) -> Operation:
    return Operation(action=Action.ADD, content=content)


async def run_claims(
    engine: AsyncEngine,
    tenant: Tenant,
    session: str = "s1",
    *,
    user: bool = True,
    unattended: bool = False,
) -> RunTokenClaims:
    """A run in a session of the tenant. ``user``: the user ``mia`` started the run."""
    run_id = await RunQueue(engine).enqueue(
        org_id=tenant.org,
        session_id=tenant.ids[session],
        text="hi",
        user_id=tenant.ids["mia"] if user else None,
        unattended=unattended,
    )
    return RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids[session]),
        run_id=str(run_id),
        user_id=str(tenant.ids["mia"]) if session == "mine" else None,
        token_id="t-1",
        expires_at=int(time.time()) + 600,
    )


def only(entry: str) -> Change:
    return lambda entries: [entry]


def append(entry: str) -> Change:
    return lambda entries: [*entries, entry]


def scope(tenant: Tenant, target: Target) -> MemoryScope:
    return MemoryScope(
        tenant.org,
        target,
        workspace_id=None if target is Target.USER else tenant.ids["ws"],
        agent_profile_id=tenant.ids["profile"] if target is Target.AGENT else None,
        user_id=tenant.ids["mia"] if target is Target.USER else None,
    )


WRITER = Writer("agent", session_id=uuid.uuid4(), run_id=uuid.uuid4())


async def test_a_write_stores_the_entries_and_a_revision(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    store = PostgresMemoryStore(engine)
    where = scope(tenant, Target.WORKSPACE)
    assert (await store.read(where)).revision == 0
    state, changed = await store.change(
        where, lambda e: [*e, "builds with make"], writer=WRITER, action="add", reason="why"
    )
    assert changed
    assert (state.entries, state.revision) == (("builds with make",), 1)
    assert await store.read(where) == state
    (revision,) = await store.revisions(where, 10)
    assert (revision.seq, revision.action, revision.reason) == (1, "add", "why")
    assert revision.entries == ("builds with make",)
    assert (revision.author_kind, revision.session_id, revision.run_id) == (
        "agent",
        WRITER.session_id,
        WRITER.run_id,
    )


async def test_a_change_that_alters_nothing_writes_no_revision(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    store = PostgresMemoryStore(engine)
    where = scope(tenant, Target.AGENT)
    await store.change(where, lambda e: ["a"], writer=WRITER, action="add")
    state, changed = await store.change(where, lambda e: list(e), writer=WRITER, action="add")
    assert not changed
    assert state.revision == 1
    assert len(await store.revisions(where, 10)) == 1


async def test_a_change_that_raises_writes_nothing(engine: AsyncEngine, tenant: Tenant) -> None:
    store = PostgresMemoryStore(engine)
    where = scope(tenant, Target.USER)

    def fail(entries: list[str]) -> list[str]:
        raise MemoryChangeError("no")

    with pytest.raises(MemoryChangeError):
        await store.change(where, fail, writer=WRITER, action="add")
    assert (await store.read(where)).revision == 0
    assert await store.revisions(where, 10) == []


async def test_concurrent_writes_are_not_lost(engine: AsyncEngine, tenant: Tenant) -> None:
    store = PostgresMemoryStore(engine)
    where = scope(tenant, Target.WORKSPACE)
    await asyncio.gather(
        *(store.change(where, append(f"note {n}"), writer=WRITER, action="add") for n in range(8))
    )
    state = await store.read(where)
    assert sorted(state.entries) == sorted(f"note {n}" for n in range(8))
    assert state.revision == 8
    seqs = [r.seq for r in await store.revisions(where, 100)]
    assert seqs == [8, 7, 6, 5, 4, 3, 2, 1]


async def test_the_scopes_do_not_share_entries(engine: AsyncEngine, tenant: Tenant) -> None:
    store = PostgresMemoryStore(engine)
    for target in Target:
        await store.change(scope(tenant, target), only(target.value), writer=WRITER, action="add")
    for target in Target:
        assert (await store.read(scope(tenant, target))).entries == (target.value,)
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(func.count()).select_from(Memory).where(Memory.org_id == tenant.org)
            )
        ).scalar_one()
    assert rows == 3


async def test_another_org_does_not_read_the_entries(engine: AsyncEngine, tenant: Tenant) -> None:
    store = PostgresMemoryStore(engine)
    where = scope(tenant, Target.WORKSPACE)
    await store.change(where, lambda e: ["secret fact"], writer=WRITER, action="add")
    other = MemoryScope(uuid.uuid4(), Target.WORKSPACE, workspace_id=tenant.ids["ws"])
    assert (await store.read(other)).entries == ()
    assert await store.revisions(other, 10) == []
    assert await store.revision(other, 1) is None


async def test_the_database_refuses_a_row_with_the_wrong_keys(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                insert(Memory).values(
                    org_id=tenant.org, scope="user", workspace_id=tenant.ids["ws"]
                )
            )


async def test_revert_adds_a_revision_with_the_old_entries(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    service = MemoryService(PostgresMemoryStore(engine))
    where = scope(tenant, Target.WORKSPACE)
    person = Writer("user", tenant.ids["mia"])
    await service.edit(where, ["one"], person, None)
    await service.edit(where, ["one", "two"], person, None)
    state = await service.revert(where, 1, person, "undo")
    assert (state.entries, state.revision) == (("one",), 3)
    newest = (await service.store.revisions(where, 1))[0]
    assert (newest.action, newest.reverted_to, newest.reason) == ("revert", 1, "undo")
    assert [r.seq for r in await service.store.revisions(where, 10)] == [3, 2, 1]
    with pytest.raises(MemoryChangeError, match="does not exist"):
        await service.revert(where, 9, person, None)


async def test_the_session_and_run_ids_of_a_revision_are_plain_ids(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    # The session and run ids of a revision are not foreign keys.
    store = PostgresMemoryStore(engine)
    where = scope(tenant, Target.AGENT)
    await store.change(where, lambda e: ["a"], writer=WRITER, action="add")
    async with engine.connect() as connection:
        found = (
            await connection.execute(
                select(MemoryRevision.session_id).where(MemoryRevision.org_id == tenant.org)
            )
        ).scalar_one()
    assert found == WRITER.session_id


# -- the run context and the rules of the service --------------------------------------------


async def test_the_agent_writes_its_own_notes(engine: AsyncEngine, tenant: Tenant) -> None:
    service = MemoryService(PostgresMemoryStore(engine))
    claims = await run_claims(engine, tenant)
    text = await service.write(claims, Target.AGENT, [add("uses uv")], "learned it")
    assert "Saved agent memory" in text
    assert "next session" in text
    assert "uses uv" in text
    state = await service.store.read(scope(tenant, Target.AGENT))
    assert state.entries == ("uses uv",)
    (revision,) = await service.store.revisions(scope(tenant, Target.AGENT), 1)
    assert revision.author_kind == "agent"
    assert revision.author_user_id == tenant.ids["mia"]
    assert str(revision.run_id) == claims.run_id
    assert revision.reason == "learned it"


async def test_a_batch_has_the_action_batch(engine: AsyncEngine, tenant: Tenant) -> None:
    service = MemoryService(PostgresMemoryStore(engine))
    claims = await run_claims(engine, tenant)
    await service.write(claims, Target.AGENT, [add("a"), add("b")], None)
    (revision,) = await service.store.revisions(scope(tenant, Target.AGENT), 1)
    assert revision.action == "batch"


async def test_a_member_cannot_write_workspace_notes(engine: AsyncEngine, tenant: Tenant) -> None:
    service = MemoryService(PostgresMemoryStore(engine))
    claims = await run_claims(engine, tenant)
    with pytest.raises(MemoryDeniedError, match="workspace admin"):
        await service.write(claims, Target.WORKSPACE, [add("x")], None)
    assert (await service.store.read(scope(tenant, Target.WORKSPACE))).revision == 0


async def test_a_workspace_admin_writes_workspace_notes(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(
                Membership.user_id == tenant.ids["mia"],
                Membership.workspace_id == tenant.ids["ws"],
            )
            .values(role="admin")
        )
    service = MemoryService(PostgresMemoryStore(engine))
    claims = await run_claims(engine, tenant)
    await service.write(claims, Target.WORKSPACE, [add("builds with make")], None)
    state = await service.store.read(scope(tenant, Target.WORKSPACE))
    assert state.entries == ("builds with make",)


async def test_an_org_admin_without_a_workspace_role_cannot_write_workspace_notes(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.user_id == tenant.ids["mia"], Membership.workspace_id.is_(None))
            .values(role="admin")
        )
    service = MemoryService(PostgresMemoryStore(engine))
    claims = await run_claims(engine, tenant)
    with pytest.raises(MemoryDeniedError):
        await service.write(claims, Target.WORKSPACE, [add("x")], None)


async def test_the_org_owner_writes_workspace_notes(engine: AsyncEngine, tenant: Tenant) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.user_id == tenant.ids["mia"], Membership.workspace_id.is_(None))
            .values(role="owner")
        )
    service = MemoryService(PostgresMemoryStore(engine))
    claims = await run_claims(engine, tenant)
    await service.write(claims, Target.WORKSPACE, [add("x")], None)


async def test_user_notes_need_a_private_session(engine: AsyncEngine, tenant: Tenant) -> None:
    service = MemoryService(PostgresMemoryStore(engine))
    shared = await run_claims(engine, tenant, "s1")
    with pytest.raises(MemoryDeniedError, match="private session"):
        await service.write(shared, Target.USER, [add("likes tea")], None)
    private = await run_claims(engine, tenant, "mine")
    await service.write(private, Target.USER, [add("likes tea")], None)
    state = await service.store.read(scope(tenant, Target.USER))
    assert state.entries == ("likes tea",)


async def test_an_unattended_run_cannot_write(engine: AsyncEngine, tenant: Tenant) -> None:
    service = MemoryService(PostgresMemoryStore(engine))
    claims = await run_claims(engine, tenant, unattended=True)
    with pytest.raises(MemoryDeniedError, match="no person"):
        await service.write(claims, Target.AGENT, [add("x")], None)


async def test_a_delegated_child_run_cannot_write(engine: AsyncEngine, tenant: Tenant) -> None:
    child = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Session).values(
                id=child,
                org_id=tenant.org,
                workspace_id=tenant.ids["ws"],
                agent_profile_id=tenant.ids["profile"],
                audience="shared",
                lineage="delegation",
                parent_session_id=tenant.ids["s1"],
            )
        )
    tenant.ids["child"] = child
    service = MemoryService(PostgresMemoryStore(engine))
    claims = await run_claims(engine, tenant, "child")
    with pytest.raises(MemoryDeniedError, match="delegated"):
        await service.write(claims, Target.AGENT, [add("x")], None)


async def test_a_run_that_does_not_exist_cannot_write(engine: AsyncEngine, tenant: Tenant) -> None:
    service = MemoryService(PostgresMemoryStore(engine))
    claims = (await run_claims(engine, tenant)).model_copy(update={"run_id": str(uuid.uuid4())})
    with pytest.raises(MemoryChangeError, match="run does not exist"):
        await service.write(claims, Target.AGENT, [add("x")], None)


async def test_a_run_of_another_workspace_cannot_use_the_token(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    service = MemoryService(PostgresMemoryStore(engine))
    claims = (await run_claims(engine, tenant)).model_copy(
        update={"workspace_id": str(uuid.uuid4())}
    )
    with pytest.raises(MemoryChangeError, match="run does not exist"):
        await service.write(claims, Target.AGENT, [add("x")], None)


async def test_a_full_memory_is_refused_and_nothing_is_saved(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    service = MemoryService(PostgresMemoryStore(engine))
    claims = await run_claims(engine, tenant)
    await service.write(claims, Target.AGENT, [add("keep me")], None)
    with pytest.raises(MemoryChangeError, match="memory is full") as error:
        await service.write(claims, Target.AGENT, [add("x" * 2_200)], None)
    assert "keep me" in str(error.value)
    state = await service.store.read(scope(tenant, Target.AGENT))
    assert state.entries == ("keep me",)


# -- the snapshot ----------------------------------------------------------------------------


async def test_the_snapshot_has_the_notes_of_the_run(engine: AsyncEngine, tenant: Tenant) -> None:
    store = PostgresMemoryStore(engine)
    for target, entry in (
        (Target.AGENT, "uses uv"),
        (Target.USER, "likes tea"),
        (Target.WORKSPACE, "builds with make"),
    ):
        await store.change(scope(tenant, target), only(entry), writer=WRITER, action="add")
    async with engine.connect() as connection:
        kwargs = {
            "org_id": tenant.org,
            "workspace_id": tenant.ids["ws"],
            "agent_profile_id": tenant.ids["profile"],
        }
        private = await load_snapshot(connection, user_id=tenant.ids["mia"], **kwargs)
        shared = await load_snapshot(connection, user_id=None, **kwargs)
    assert "uses uv" in private
    assert "likes tea" in private
    assert "builds with make" in private
    assert "uses uv" in shared
    assert "likes tea" not in shared
    assert "User profile" not in shared
