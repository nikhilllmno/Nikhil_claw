"""Compaction, pins, and the archive in the run queue and the worker (Section 6.3)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.context import ArchiveError, ContextArchive, PinStore, list_compactions
from sama.db.models import (
    AuditEntry,
    Message,
    Org,
    Run,
    RunEvent,
    Session,
    SessionCompaction,
)
from sama.orchestrator.queue import QueueError, RateLimitedError
from sama.runtime.archive import ArchiveEntry, TranscriptArchive
from sama.runtime.contract import Capabilities, FailureCode, RunRequest
from sama.runtime.fake import FakeRuntime
from sama.tenancy import RunTokenClaims
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness, until


@pytest.fixture
def harness(engine: AsyncEngine) -> Harness:
    return Harness(engine)


async def chat(harness: Harness, tenant: Tenant, session: str, text: str) -> uuid.UUID:
    run_id = await harness.send(tenant, session, text)
    assert await harness.worker.run_once()
    assert await harness.status_is(run_id, "completed")
    return run_id


async def compact(harness: Harness, tenant: Tenant, session: str, focus: str = "") -> uuid.UUID:
    return await harness.queue.enqueue_compact(
        org_id=tenant.org,
        session_id=tenant.ids[session],
        user_id=tenant.ids["mia"],
        focus=focus,
    )


def inputs(harness: Harness) -> list[str]:
    return [r.input for r in harness.runtime.requests if isinstance(r, RunRequest)]


async def compactions(engine: AsyncEngine, tenant: Tenant, session: str) -> list[Any]:
    async with engine.connect() as connection:
        return await list_compactions(connection, tenant.org, tenant.ids[session])


# -- /compact in the queue ---------------------------------------------------------------------


async def test_a_session_with_no_finished_run_has_nothing_to_compact(
    harness: Harness, tenant: Tenant
) -> None:
    with pytest.raises(QueueError, match="nothing to compact"):
        await compact(harness, tenant, "s1")


async def test_a_busy_session_refuses_compact(harness: Harness, tenant: Tenant) -> None:
    await chat(harness, tenant, "s1", "one")
    await harness.send(tenant, "s1", "two")  # queued: the session is busy
    with pytest.raises(QueueError, match="busy"):
        await compact(harness, tenant, "s1")


async def test_a_running_session_refuses_compact(harness: Harness, tenant: Tenant) -> None:
    await chat(harness, tenant, "s1", "one")
    await harness.send(tenant, "s1", "two")
    assert await harness.queue.claim("w-busy") is not None
    with pytest.raises(QueueError, match="busy"):
        await compact(harness, tenant, "s1")


async def test_an_archived_or_unknown_session_refuses_compact(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    await chat(harness, tenant, "s1", "one")
    async with engine.begin() as connection:
        await connection.execute(
            update(Session).where(Session.id == tenant.ids["s1"]).values(status="archived")
        )
    with pytest.raises(QueueError, match="not active"):
        await compact(harness, tenant, "s1")
    with pytest.raises(QueueError, match="does not exist"):
        await harness.queue.enqueue_compact(
            org_id=tenant.org, session_id=uuid.uuid4(), user_id=tenant.ids["mia"]
        )


async def test_compact_counts_as_a_message_for_the_rate_limit(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Org).where(Org.id == tenant.org).values(limits={"user_messages_per_minute": 2})
        )
    await chat(harness, tenant, "s1", "one")
    await compact(harness, tenant, "s1")
    await harness.worker.run_once()
    with pytest.raises(RateLimitedError, match="you sent 2 messages"):
        await compact(harness, tenant, "s1")


async def test_compact_stores_a_redacted_focus_and_no_message(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    await chat(harness, tenant, "s1", "one")
    run_id = await compact(harness, tenant, "s1", "keep key sk-ant-api03-" + "a" * 40)
    row = await harness.run(run_id)
    assert row.compact
    assert "sk-ant-api03" not in (row.compact_focus or "")
    async with engine.connect() as connection:
        count = (
            await connection.execute(
                select(func.count()).select_from(Message).where(Message.run_id == run_id)
            )
        ).scalar_one()
    assert count == 0


# -- /compact in the worker --------------------------------------------------------------------


async def test_a_compact_run_records_the_compaction(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    await chat(harness, tenant, "s1", "one")
    run_id = await compact(harness, tenant, "s1", "keep the plan")
    assert await harness.worker.run_once()
    assert await harness.status_is(run_id, "completed")
    request = harness.runtime.requests[-1]
    assert isinstance(request, RunRequest)
    assert request.input == ""
    assert request.compact is not None
    assert request.compact.focus == "keep the plan"
    assert request.runtime_state is not None
    events = await harness.events(run_id)
    assert [kind for _, kind in events] == ["compacted", "run_completed"]
    (row,) = await compactions(engine, tenant, "s1")
    assert (row.trigger, row.tokens_before, row.tokens_after) == ("manual", 1000, 500)
    assert row.run_id == run_id
    assert row.completed_at is not None
    assert row.archive_session_id is None
    async with engine.connect() as connection:
        actions = (
            await connection.execute(
                select(AuditEntry.action).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.run_id == run_id
                )
            )
        ).scalars()
        assert "context.compacted" in set(actions)
        # No assistant message and no user turn: the session reads the same as before.
        messages = (
            await connection.execute(
                select(Message.role).where(Message.session_id == tenant.ids["s1"])
            )
        ).scalars()
        assert sorted(messages) == ["assistant", "user"]


async def test_the_session_continues_after_a_compaction(harness: Harness, tenant: Tenant) -> None:
    await chat(harness, tenant, "s1", "one")
    await compact(harness, tenant, "s1")
    assert await harness.worker.run_once()
    await chat(harness, tenant, "s1", "two")
    request = harness.runtime.requests[-1]
    assert isinstance(request, RunRequest)
    assert request.compact is None
    assert request.runtime_state is not None
    assert request.input == "two"


async def test_a_runtime_that_cannot_compact_fails_the_run(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    class NoCompact(FakeRuntime):
        def capabilities(self) -> Capabilities:
            return super().capabilities().model_copy(update={"manual_compaction": False})

    harness = Harness(engine)
    harness.worker._runtimes._adapters[harness.runtime.kind] = NoCompact()
    await chat(harness, tenant, "s1", "one")
    run_id = await compact(harness, tenant, "s1")
    assert await harness.worker.run_once()
    row = await harness.run(run_id)
    assert row.status == "failed"
    assert row.failure_code == FailureCode.INVALID_REQUEST.value


async def test_a_compact_run_ends_without_a_message_to_any_hook(
    harness: Harness, tenant: Tenant
) -> None:
    await chat(harness, tenant, "s1", "one")
    harness.ended.clear()
    run_id = await compact(harness, tenant, "s1")
    await harness.worker.run_once()
    await until(lambda: harness.ended_count(1))
    assert harness.ended == [(str(tenant.org), str(run_id))]


# -- pins --------------------------------------------------------------------------------------


async def test_a_pin_goes_in_front_of_the_next_message_once(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    pins = PinStore(engine)
    await pins.add(
        org_id=tenant.org,
        session_id=tenant.ids["s1"],
        user_id=tenant.ids["mia"],
        text="Deploy only on Friday.",
    )
    await chat(harness, tenant, "s1", "first")
    await chat(harness, tenant, "s1", "second")
    first, second = inputs(harness)
    assert first.startswith("<pinned_context>")
    assert "Deploy only on Friday." in first
    assert first.endswith("first")
    assert second == "second"
    # The stored message is the text of the person.
    async with engine.connect() as connection:
        stored = (
            await connection.execute(
                select(Message.content["text"].astext).where(
                    Message.session_id == tenant.ids["s1"], Message.role == "user"
                )
            )
        ).scalars()
        assert sorted(stored) == ["first", "second"]


async def test_a_compaction_makes_the_runtime_forget_pins_and_they_come_again(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    pins = PinStore(engine)
    await pins.add(
        org_id=tenant.org, session_id=tenant.ids["s1"], user_id=None, text="Use metric units."
    )
    await chat(harness, tenant, "s1", "one")
    await compact(harness, tenant, "s1", "short")
    await harness.worker.run_once()
    compact_request = harness.runtime.requests[-1]
    assert isinstance(compact_request, RunRequest)
    assert compact_request.compact is not None
    # The summary must keep the pins.
    assert "Use metric units." in compact_request.compact.focus
    assert compact_request.compact.focus.startswith("short")
    await chat(harness, tenant, "s1", "two")
    assert "Use metric units." in inputs(harness)[-1]


async def test_a_pin_of_another_session_is_not_injected(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    await PinStore(engine).add(
        org_id=tenant.org, session_id=tenant.ids["s2"], user_id=None, text="Only for s2."
    )
    await chat(harness, tenant, "s1", "hello")
    assert inputs(harness) == ["hello"]


async def test_the_pins_of_a_claim_stay_after_a_lost_lease(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    """A run that is claimed again (its worker died) gets the same pins again."""
    await PinStore(engine).add(
        org_id=tenant.org, session_id=tenant.ids["s1"], user_id=None, text="Pinned note."
    )
    await harness.send(tenant, "s1", "hello")
    first = await harness.queue.claim("w-a")
    assert first is not None
    assert "Pinned note." in first.input
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == first.run_id)
            .values(lease_expires_at=text("now() - interval '1 second'"))
        )
    await harness.queue.reap()
    again = await harness.queue.claim("w-b")
    assert again is not None
    assert again.run_id == first.run_id
    assert "Pinned note." in again.input


# -- the archive -------------------------------------------------------------------------------


def claims_of(tenant: Tenant, run_id: uuid.UUID, session: str = "s1") -> RunTokenClaims:
    return RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids[session]),
        run_id=str(run_id),
        token_id="t-1",
        expires_at=2_000_000_000,
    )


def archive_of(*texts: str, trigger: str = "manual") -> TranscriptArchive:
    return TranscriptArchive(
        trigger=trigger,  # type: ignore[arg-type]
        entries=[
            ArchiveEntry(role="user" if i % 2 == 0 else "assistant", text=t)
            for i, t in enumerate(texts)
        ],
    )


async def running_run(harness: Harness, tenant: Tenant, session: str = "s1") -> uuid.UUID:
    await harness.send(tenant, session, "go")
    claimed = await harness.queue.claim("w-arch")
    assert claimed is not None
    return claimed.run_id


async def test_the_archive_is_a_child_session_with_the_audience_of_the_live_session(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    run_id = await running_run(harness, tenant, "mine")
    answer = await ContextArchive(engine).archive(
        claims_of(tenant, run_id, "mine"), archive_of("remember 42", "ok", "and 43")
    )
    assert answer.entries == 3
    async with engine.connect() as connection:
        child = (
            await connection.execute(
                select(Session).where(Session.parent_session_id == tenant.ids["mine"])
            )
        ).one()
        assert child.lineage == "compaction"
        assert child.status == "archived"
        assert (child.audience, child.owner_user_id) == ("private", tenant.ids["mia"])
        assert child.workspace_id == tenant.ids["ws"]
        assert child.title is not None
        assert child.title.startswith("Before compaction 1")
        texts = (
            await connection.execute(
                select(Message.content["text"].astext)
                .where(Message.session_id == child.id)
                .order_by(Message.seq)
            )
        ).scalars()
        assert list(texts) == ["remember 42", "ok", "and 43"]
        row = (
            await connection.execute(
                select(SessionCompaction).where(SessionCompaction.archive_session_id == child.id)
            )
        ).one()
        assert row.completed_at is None
        assert (row.archive_entries, row.archive_truncated) == (3, False)


async def test_the_archive_redacts_secrets(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    run_id = await running_run(harness, tenant)
    secret = "sk-ant-api03-" + "b" * 40
    await ContextArchive(engine).archive(claims_of(tenant, run_id), archive_of(f"key {secret}"))
    async with engine.connect() as connection:
        body = (
            await connection.execute(
                select(Message.content["text"].astext)
                .join(Session, Session.id == Message.session_id)
                .where(Session.parent_session_id == tenant.ids["s1"])
            )
        ).scalar_one()
    assert secret not in body


async def test_an_empty_archive_makes_no_session(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    run_id = await running_run(harness, tenant)
    answer = await ContextArchive(engine).archive(claims_of(tenant, run_id), archive_of(" ", ""))
    assert answer.entries == 0
    async with engine.connect() as connection:
        count = (
            await connection.execute(
                select(func.count())
                .select_from(Session)
                .where(Session.parent_session_id == tenant.ids["s1"])
            )
        ).scalar_one()
    assert count == 0


async def test_only_a_running_run_can_archive(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    run_id = await chat(harness, tenant, "s1", "one")  # completed
    with pytest.raises(ArchiveError, match="not running"):
        await ContextArchive(engine).archive(claims_of(tenant, run_id), archive_of("x"))
    with pytest.raises(ArchiveError, match="not running"):
        await ContextArchive(engine).archive(claims_of(tenant, uuid.uuid4()), archive_of("x"))


async def test_a_run_has_a_limit_of_archives(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    from sama.context.compaction import MAX_ARCHIVES_PER_RUN

    run_id = await running_run(harness, tenant)
    store = ContextArchive(engine)
    for n in range(MAX_ARCHIVES_PER_RUN):
        await store.archive(claims_of(tenant, run_id), archive_of(f"entry {n}"))
    with pytest.raises(ArchiveError, match="too many"):
        await store.archive(claims_of(tenant, run_id), archive_of("one more"))


async def test_the_compacted_event_completes_the_open_archive_row(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    await chat(harness, tenant, "s1", "one")
    run_id = await compact(harness, tenant, "s1")
    claimed = await harness.queue.claim("w-arch")
    assert claimed is not None
    await ContextArchive(engine).archive(claims_of(tenant, run_id), archive_of("a", "b"))
    await harness.queue.add_events(
        claimed,
        "w-arch",
        [(0, "compacted", {"trigger": "manual", "tokens_before": 9, "tokens_after": 3})],
    )
    (row,) = await compactions(engine, tenant, "s1")
    assert (row.tokens_before, row.tokens_after, row.archive_entries) == (9, 3, 2)
    assert row.archive_session_id is not None
    assert row.completed_at is not None
    async with engine.connect() as connection:
        event = (
            await connection.execute(select(RunEvent.type).where(RunEvent.run_id == run_id))
        ).scalar_one()
    assert event == "compacted"


async def test_a_compaction_with_no_archive_still_has_a_record(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    """An automatic compaction in a chat run, when the archive failed or the runtime has none."""
    await harness.send(tenant, "s1", "go")
    claimed = await harness.queue.claim("w-auto")
    assert claimed is not None
    await harness.queue.add_events(
        claimed, "w-auto", [(0, "compacted", {"trigger": "auto", "tokens_before": 90})]
    )
    (row,) = await compactions(engine, tenant, "s1")
    assert (row.trigger, row.tokens_before, row.tokens_after) == ("auto", 90, 0)
    assert row.archive_session_id is None
    assert row.completed_at is not None
