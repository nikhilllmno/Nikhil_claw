"""Regression tests for the review findings of the context management (Section 6.3).

Archive: redaction of structured tool input and of whole entries, the eligibility check before
the work, the live authority of the run. Pins: redaction when they are shown, and the
reservation of a pin until a run saves a runtime state that has it. Queue: one lock order for
a message and a compact request.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.context import ArchiveError, ContextArchive, PinStore
from sama.context import compaction as compaction_module
from sama.context.pins import pins_for_input
from sama.db.models import Membership, Message, Org, Session, SessionCompaction
from sama.orchestrator.queue import QueueError
from sama.runtime.archive import (
    MAX_ENTRY_CHARS,
    ArchiveEntry,
    TranscriptArchive,
)
from sama.runtime.contract import FailureCode, Usage
from sama.vault.redact import default_redactor
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_compaction import (
    chat,
    claims_of,
    compact,
    running_run,
)
from tests.orchestrator.test_worker import Harness


@pytest.fixture
def harness(engine: AsyncEngine) -> Harness:
    return Harness(engine)


# A known secret with the characters that a JSON text escapes: a quote, a backslash, a new line.
HARD_SECRET = 'pa"ss\\word\nline-4711'


@pytest.fixture
def known_secret() -> Iterator[str]:
    default_redactor().add(HARD_SECRET)
    try:
        yield HARD_SECRET
    finally:
        default_redactor().forget(HARD_SECRET)


async def stored_texts(engine: AsyncEngine, tenant: Tenant, session: str = "s1") -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(Message.content["text"].astext)
            .join(Session, Session.id == Message.session_id)
            .where(Session.parent_session_id == tenant.ids[session])
            .order_by(Message.seq)
        )
        return list(rows.scalars())


def only_safe(texts: list[str], secret: str) -> None:
    joined = "\n".join(texts)
    for form in (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=True)):
        assert form not in joined
    # No fragment of the secret stays, also not a part that a cut could leave.
    assert "line-4711" not in joined
    assert 'pa\\"ss' not in joined


# -- S1: a tool input is redacted as a structure ----------------------------------------------


async def test_a_secret_with_quotes_in_a_tool_input_is_redacted(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
    known_secret: str,
) -> None:
    run_id = await running_run(harness, tenant)
    call = ArchiveEntry(
        role="assistant",
        tool_name="Bash",
        tool_input={
            "command": f"curl -H 'x: {known_secret}' https://x.test",
            "env": {"TOKEN": known_secret, known_secret: "as a key"},
            "list": [known_secret, 3, None, True],
        },
    )
    await ContextArchive(engine).archive(
        claims_of(tenant, run_id), TranscriptArchive(trigger="manual", entries=[call])
    )
    (text,) = await stored_texts(engine, tenant)
    only_safe([text], known_secret)
    assert text.startswith("[tool call Bash] {")
    assert "curl -H" in text  # the structure stays readable


async def test_a_secret_with_quotes_in_the_text_of_an_entry_is_redacted(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
    known_secret: str,
) -> None:
    run_id = await running_run(harness, tenant)
    entry = ArchiveEntry(role="tool", text=f"output {known_secret} end")
    await ContextArchive(engine).archive(
        claims_of(tenant, run_id), TranscriptArchive(trigger="manual", entries=[entry])
    )
    only_safe(await stored_texts(engine, tenant), known_secret)


# -- S2 and R4: nothing is cut before the redaction -------------------------------------------


async def test_a_secret_at_the_old_cut_points_leaves_no_fragment(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
    known_secret: str,
) -> None:
    """The runner used to cut a tool input at 4,000 characters and a text at 20,000, before
    the redaction. A secret that crossed the cut left a fragment. Now the whole value reaches
    the redaction, and the control plane cuts after it."""
    run_id = await running_run(harness, tenant)
    entries = [
        ArchiveEntry(
            role="assistant", tool_name="Write", tool_input={"d": "a" * 3_995 + known_secret}
        ),
        ArchiveEntry(role="tool", text="b" * (MAX_ENTRY_CHARS - 6) + known_secret + "c" * 500),
        ArchiveEntry(role="tool", text=known_secret + "d" * 30_000),
    ]
    await ContextArchive(engine).archive(
        claims_of(tenant, run_id), TranscriptArchive(trigger="auto", entries=entries)
    )
    texts = await stored_texts(engine, tenant)
    assert len(texts) == 3
    only_safe(texts, known_secret)
    assert all(len(t) <= MAX_ENTRY_CHARS for t in texts)


async def test_an_omitted_count_from_the_runner_marks_the_archive_as_cut(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
) -> None:
    run_id = await running_run(harness, tenant)
    body = TranscriptArchive(
        trigger="auto",
        entries=[
            ArchiveEntry(role="user", text="a"),
            ArchiveEntry(role="system", text="[an entry of 9 bytes is not in this archive]"),
        ],
        omitted=1,
    )
    answer = await ContextArchive(engine).archive(claims_of(tenant, run_id), body)
    assert answer.truncated
    async with engine.connect() as connection:
        row = (await connection.execute(select(SessionCompaction))).one()
    assert row.archive_truncated is True


async def test_an_archive_without_a_cut_is_not_marked_as_cut(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
) -> None:
    run_id = await running_run(harness, tenant)
    body = TranscriptArchive(trigger="auto", entries=[ArchiveEntry(role="user", text="a")])
    assert not (await ContextArchive(engine).archive(claims_of(tenant, run_id), body)).truncated


# -- S3: the run is checked before the work ---------------------------------------------------


async def test_a_run_that_cannot_archive_is_refused_before_the_redaction(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(entry: ArchiveEntry) -> ArchiveEntry:
        raise AssertionError("the redaction ran for a run that cannot archive")

    monkeypatch.setattr(compaction_module, "_redacted", forbidden)
    done = await chat(harness, tenant, "s1", "one")  # completed: it cannot archive
    store = ContextArchive(engine)
    body = TranscriptArchive(trigger="auto", entries=[ArchiveEntry(role="user", text="x")])
    with pytest.raises(ArchiveError, match="not running"):
        await store.authorize(claims_of(tenant, done))
    with pytest.raises(ArchiveError, match="not running"):
        await store.archive(claims_of(tenant, done), body)


async def test_authorize_accepts_a_running_run(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
) -> None:
    run_id = await running_run(harness, tenant)
    await ContextArchive(engine).authorize(claims_of(tenant, run_id))


# -- S5: the live authority of the run --------------------------------------------------------


async def revoke_mia(engine: AsyncEngine, tenant: Tenant) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            delete(Membership).where(
                Membership.user_id == tenant.ids["mia"],
                Membership.workspace_id == tenant.ids["ws"],
            )
        )


@pytest.mark.parametrize("session", ["s1", "mine"])
async def test_a_user_who_lost_access_while_the_run_runs_cannot_archive(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
    session: str,
) -> None:
    """The run token is valid and the run is ``running``, but its user cannot see the session
    any more. The archive writes nothing: the same live check as the tool gate."""
    run_id = await running_run(harness, tenant, session)
    store = ContextArchive(engine)
    body = TranscriptArchive(trigger="manual", entries=[ArchiveEntry(role="user", text="x")])
    await store.archive(claims_of(tenant, run_id, session), body)  # allowed before the change
    await revoke_mia(engine, tenant)
    with pytest.raises(ArchiveError, match="can no longer see"):
        await store.authorize(claims_of(tenant, run_id, session))
    with pytest.raises(ArchiveError, match="can no longer see"):
        await store.archive(claims_of(tenant, run_id, session), body)
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(SessionCompaction.id).where(SessionCompaction.run_id == run_id)
            )
        ).all()
        children = (
            await connection.execute(
                select(Session.id).where(Session.parent_session_id == tenant.ids[session])
            )
        ).all()
    assert len(rows) == 1  # only the archive from before the change
    assert len(children) == 1


async def test_the_authority_is_checked_again_when_the_archive_writes(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user who loses access after ``authorize`` and before the write (the body is read in
    between) gets no archive."""
    run_id = await running_run(harness, tenant)
    store = ContextArchive(engine)
    real = store.authorize

    async def authorize_then_revoke(claims: Any) -> None:
        await real(claims)
        await revoke_mia(engine, tenant)

    monkeypatch.setattr(store, "authorize", authorize_then_revoke)
    body = TranscriptArchive(trigger="manual", entries=[ArchiveEntry(role="user", text="x")])
    with pytest.raises(ArchiveError, match="can no longer see"):
        await store.archive(claims_of(tenant, run_id), body)
    assert await stored_texts(engine, tenant) == []


# -- S4: pins are redacted when they are shown ------------------------------------------------


async def test_a_pin_shows_secrets_that_the_redactor_learned_after_the_pin(
    engine: AsyncEngine,
    tenant: Tenant,
) -> None:
    store = PinStore(engine)
    late = "late-learned-secret-value-9931"
    pin = await store.add(
        org_id=tenant.org,
        session_id=tenant.ids["s1"],
        user_id=tenant.ids["mia"],
        text="x" * 100 + late + " tail",
    )
    assert late in pin.text  # unknown when the pin was stored
    default_redactor().add(late)
    try:
        (listed,) = await store.list(tenant.org, tenant.ids["s1"])
    finally:
        default_redactor().forget(late)
    assert late not in listed.text
    assert "late-learned" not in listed.text


async def test_a_chat_list_of_pins_cannot_cut_a_secret_into_parts(
    engine: AsyncEngine,
    tenant: Tenant,
) -> None:
    """The chat list shows 120 characters of a pin. The redaction comes before the cut, so a
    secret that crosses the 120th character is not shown in parts."""
    from sama.gateway.sessions import Target, pin_reply

    store = PinStore(engine)
    late = "another-late-secret-0042-abcdefghij"
    await store.add(
        org_id=tenant.org,
        session_id=tenant.ids["s1"],
        user_id=None,
        text="y" * 100 + late,
    )
    default_redactor().add(late)
    try:
        target = Target(
            org_id=tenant.org,
            user_id=tenant.ids["mia"],
            workspace_id=tenant.ids["ws"],
            agent_profile_id=tenant.ids["profile"],
            audience="shared",
        )
        shown = await pin_reply(store, target, tenant.ids["s1"], "pins", "")
    finally:
        default_redactor().forget(late)
    assert "another-late" not in shown
    assert "abcdefghij" not in shown


# -- R1: a pin is reserved until a run saves a runtime state that has it ----------------------


async def add_pin(engine: AsyncEngine, tenant: Tenant, text: str = "Pinned note.") -> None:
    await PinStore(engine).add(
        org_id=tenant.org, session_id=tenant.ids["s1"], user_id=None, text=text
    )


@pytest.mark.parametrize(
    "code", [FailureCode.MODEL_ERROR, FailureCode.CANCELLED], ids=["failed", "cancelled"]
)
async def test_a_run_that_fails_before_it_saves_state_gives_the_pin_back(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
    code: FailureCode,
) -> None:
    await add_pin(engine, tenant)
    await harness.send(tenant, "s1", "one")
    first = await harness.queue.claim("w-a")
    assert first is not None
    assert "Pinned note." in first.input
    assert await harness.queue.fail(first, "w-a", code=code, message="x", usage=Usage())
    # No runtime state has the pin, so the next run must carry it again.
    await harness.send(tenant, "s1", "two")
    second = await harness.queue.claim("w-a")
    assert second is not None
    assert "Pinned note." in second.input
    assert second.input.endswith("two")
    # This run completes with a state: the pin stays given.
    assert await harness.queue.complete(
        second, "w-a", output="ok", runtime_state=b'{"turns":1}', usage=Usage()
    )
    await harness.send(tenant, "s1", "three")
    third = await harness.queue.claim("w-a")
    assert third is not None
    assert third.input == "three"


async def test_a_run_that_completes_without_a_state_gives_the_pin_back(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
) -> None:
    """A runtime with no resumable state starts a new session for the next run: the pin must
    come again."""
    await add_pin(engine, tenant)
    await harness.send(tenant, "s1", "one")
    first = await harness.queue.claim("w-a")
    assert first is not None
    assert await harness.queue.complete(
        first, "w-a", output="ok", runtime_state=None, usage=Usage()
    )
    await harness.send(tenant, "s1", "two")
    second = await harness.queue.claim("w-a")
    assert second is not None
    assert "Pinned note." in second.input


async def test_a_run_that_is_reaped_and_started_again_keeps_the_pin(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
) -> None:
    """The reservation of the same run holds (a lost lease): the claim of that run again gets
    the pin, as before."""
    from sqlalchemy import text, update

    from sama.db.models import Run

    await add_pin(engine, tenant)
    await harness.send(tenant, "s1", "hello")
    first = await harness.queue.claim("w-a")
    assert first is not None
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == first.run_id)
            .values(lease_expires_at=text("now() - interval '1 second'"))
        )
    await harness.queue.reap()
    again = await harness.queue.claim("w-b")
    assert again is not None
    assert "Pinned note." in again.input


async def test_a_paused_run_holds_the_pin_until_it_ends_without_a_state(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
) -> None:
    await add_pin(engine, tenant)
    await harness.send(tenant, "s1", "hello")
    claimed = await harness.queue.claim("w-a")
    assert claimed is not None
    paused = await harness.queue.pause(
        claimed,
        "w-a",
        approval_id="call-1",
        tool_name="Bash",
        arguments={},
        reason="ask",
        runtime_state=b'{"turns":1}',
        usage=Usage(),
    )
    assert paused == "paused"
    other = uuid.uuid4()
    async with engine.begin() as connection:
        # The state of the paused run has the pin. Its resume gets no new pin text.
        assert await pins_for_input(connection, tenant.org, tenant.ids["s1"], other) == []
    # The approval is never decided: the run is cancelled. Its state is not resumed.
    assert await harness.queue.cancel(org_id=tenant.org, run_id=claimed.run_id) == "cancelled"
    async with engine.begin() as connection:
        assert await pins_for_input(connection, tenant.org, tenant.ids["s1"], other) == [
            "Pinned note."
        ]


# -- R2: one lock order for a message and a compact request -----------------------------------


async def test_a_message_and_a_compact_request_at_the_same_time_do_not_deadlock(
    engine: AsyncEngine,
    harness: Harness,
    tenant: Tenant,
) -> None:
    """``enqueue`` took the org rate lock and then a share lock on the session row (the run
    insert). ``enqueue_compact`` took the row first and the rate lock second. At the same time
    the database found a deadlock. Both take the rate lock first now, so one of them waits."""
    async with engine.begin() as connection:
        await connection.execute(
            update(Org).where(Org.id == tenant.org).values(limits={"user_messages_per_minute": 500})
        )
    await chat(harness, tenant, "s1", "start")
    outcomes: list[str] = []
    for round_ in range(25):
        results = await asyncio.gather(
            harness.send(tenant, "s1", f"message {round_}"),
            compact(harness, tenant, "s1"),
            return_exceptions=True,
        )
        for result in results:
            # A refusal for a busy session is fine. A database error (a deadlock) is not.
            if isinstance(result, QueueError):
                assert "busy" in str(result), str(result)
                outcomes.append("refused")
            else:
                assert not isinstance(result, BaseException), repr(result)
                outcomes.append("queued")
        while await harness.worker.run_once():
            pass
    assert outcomes.count("queued") >= 25
