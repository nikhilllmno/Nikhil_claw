"""Tests of the task board: the service, the dispatcher, and the run tools
(docs/architecture.md, Section 12.4)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Message, Org, Run, Session, Task, TaskComment
from sama.orchestrator.queue import ClaimedRun, RunQueue
from sama.runtime.contract import FailureCode, Usage
from sama.tasks.dispatcher import TaskDispatcher, task_prompt
from sama.tasks.runs import TaskTools, render_tasks
from sama.tasks.service import (
    MAX_DEPTH,
    Change,
    NewTask,
    TaskError,
    TaskNotFoundError,
    TaskView,
    Viewer,
    add_comment,
    change_task,
    create_task,
    list_comments,
    list_tasks,
)
from sama.tenancy import RunTokenClaims
from tests.orchestrator.conftest import Tenant, add_tenant

OWNER = "worker-t"


def person(tenant: Tenant) -> Viewer:
    return Viewer(org_id=tenant.org, workspace_id=tenant.ids["ws"], owner_user_id=tenant.ids["mia"])


def nobody(tenant: Tenant) -> Viewer:
    return Viewer(org_id=tenant.org, workspace_id=tenant.ids["ws"], owner_user_id=None)


async def create(engine: AsyncEngine, tenant: Tenant, **values: Any) -> TaskView:
    values.setdefault("title", "Write the report")
    values.setdefault("created_by_user_id", tenant.ids["mia"])
    if values.get("audience") == "private":
        values.setdefault("owner_user_id", tenant.ids["mia"])
    async with engine.begin() as connection:
        return await create_task(connection, person(tenant), NewTask(**values))


async def change(engine: AsyncEngine, tenant: Tenant, task_id: uuid.UUID, **values: Any) -> Any:
    async with engine.begin() as connection:
        return await change_task(connection, person(tenant), task_id, Change(**values))


async def card(engine: AsyncEngine, task_id: uuid.UUID) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(select(Task).where(Task.id == task_id))).one()


async def comments(engine: AsyncEngine, task_id: uuid.UUID) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(TaskComment.body)
            .where(TaskComment.task_id == task_id)
            .order_by(TaskComment.created_at)
        )
        return [row.body for row in rows]


async def prompt_of(engine: AsyncEngine, run_id: uuid.UUID) -> str:
    async with engine.connect() as connection:
        content = (
            await connection.execute(select(Message.content).where(Message.run_id == run_id))
        ).scalar_one()
    return str(content["text"])


def run_claims(tenant: Tenant, session_id: uuid.UUID, run_id: uuid.UUID) -> RunTokenClaims:
    return RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(session_id),
        run_id=str(run_id),
        token_id="t-1",
        expires_at=2_000_000_000,
    )


async def claim(queue: RunQueue, run_id: uuid.UUID) -> ClaimedRun:
    claimed = await queue.claim(OWNER)
    assert claimed is not None
    assert claimed.run_id == run_id
    return claimed


# -- the service ------------------------------------------------------------------------------


async def test_a_private_card_is_visible_only_to_its_owner(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    shared = await create(engine, tenant, title="shared")
    private = await create(engine, tenant, title="private", audience="private")
    async with engine.connect() as connection:
        mine = {t.id for t in await list_tasks(connection, person(tenant))}
        others = {t.id for t in await list_tasks(connection, nobody(tenant))}
        stranger = Viewer(org_id=uuid.uuid4(), workspace_id=tenant.ids["ws"], owner_user_id=None)
        assert await list_tasks(connection, stranger) == []
    assert mine == {shared.id, private.id}
    assert others == {shared.id}


async def test_the_service_checks_the_links_and_the_assignee(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    private = await create(engine, tenant, title="private", audience="private")
    with pytest.raises(TaskError, match="dependency does not exist"):
        await create(engine, tenant, depends_on=[private.id])
    with pytest.raises(TaskError, match="parent task does not exist"):
        await create(engine, tenant, parent_task_id=private.id)
    with pytest.raises(TaskError, match="two times"):
        await create(engine, tenant, title="p", audience="private", depends_on=[private.id] * 2)
    with pytest.raises(TaskError, match="agent profile does not exist"):
        await create(engine, tenant, assignee_profile_id=uuid.uuid4())
    with pytest.raises(TaskError, match="not a member"):
        await create(engine, tenant, assignee_user_id=uuid.uuid4())
    with pytest.raises(TaskError, match="title"):
        await create(engine, tenant, title="   ")
    parent: uuid.UUID | None = None
    for depth in range(MAX_DEPTH + 1):
        parent = (await create(engine, tenant, title=f"level {depth}", parent_task_id=parent)).id
    with pytest.raises(TaskError, match="parent tasks at most"):
        await create(engine, tenant, title="too deep", parent_task_id=parent)


async def test_a_person_cannot_set_running_and_leaving_running_cancels_the_run(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    task = await create(engine, tenant)
    with pytest.raises(TaskError, match="status is one of"):
        await change(engine, tenant, task.id, status="running")
    run_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            update(Task).where(Task.id == task.id).values(status="running", run_id=run_id)
        )
    changed = await change(engine, tenant, task.id, status="todo")
    assert changed.cancel_run_id == run_id
    assert (changed.task.status, changed.task.run_id) == ("todo", None)
    done = await change(engine, tenant, task.id, status="done", result="ok")
    assert done.task.finished_at is not None
    assert done.cancel_run_id is None
    with pytest.raises(TaskNotFoundError):
        await change(engine, tenant, uuid.uuid4(), status="done")


async def test_comments_need_a_visible_card(engine: AsyncEngine, tenant: Tenant) -> None:
    private = await create(engine, tenant, title="private", audience="private")
    async with engine.begin() as connection:
        await add_comment(
            connection, person(tenant), private.id, "looks good", author_user_id=tenant.ids["mia"]
        )
        with pytest.raises(TaskNotFoundError):
            await add_comment(connection, nobody(tenant), private.id, "x", author_user_id=None)
        [comment] = await list_comments(connection, person(tenant), private.id)
    assert (comment.author_kind, comment.body) == ("user", "looks good")


# -- the dispatcher ---------------------------------------------------------------------------


async def test_the_dispatcher_starts_a_card_in_a_new_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    task = await create(
        engine,
        tenant,
        title="Sum the invoices",
        body="Use the March files.",
        audience="private",
        assignee_profile_id=tenant.ids["profile"],
    )
    nobody_card = await create(engine, tenant, title="for nobody")
    result = await TaskDispatcher(engine, queue).run_once()
    assert result.started == 1
    row = await card(engine, task.id)
    assert row.status == "running"
    assert (await card(engine, nobody_card.id)).status == "todo"
    async with engine.connect() as connection:
        session = (
            await connection.execute(select(Session).where(Session.id == row.session_id))
        ).one()
        run = (await connection.execute(select(Run).where(Run.id == row.run_id))).one()
    assert (session.audience, session.owner_user_id) == ("private", tenant.ids["mia"])
    assert session.lineage is None
    assert run.unattended
    assert run.triggered_by_user_id == tenant.ids["mia"]
    text = await prompt_of(engine, row.run_id)
    assert str(task.id) in text
    assert "Use the March files." in text
    assert "task_complete" in text
    # A second pass starts nothing more.
    assert (await TaskDispatcher(engine, queue).run_once()).started == 0


async def test_a_card_waits_for_its_dependencies_and_reads_their_results(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    first = await create(engine, tenant, title="Collect the data")
    second = await create(
        engine,
        tenant,
        title="Write the summary",
        depends_on=[first.id],
        assignee_profile_id=tenant.ids["profile"],
    )
    dispatcher = TaskDispatcher(engine, queue)
    assert (await dispatcher.run_once()).started == 0
    assert (await card(engine, second.id)).status == "todo"
    await change(engine, tenant, first.id, status="done", result="There are 12 rows.")
    assert (await dispatcher.run_once()).started == 1
    row = await card(engine, second.id)
    text = await prompt_of(engine, row.run_id)
    assert "There are 12 rows." in text
    assert "untrusted" in text


async def test_a_cancelled_dependency_blocks_the_card(engine: AsyncEngine, tenant: Tenant) -> None:
    first = await create(engine, tenant, title="first")
    second = await create(
        engine, tenant, depends_on=[first.id], assignee_profile_id=tenant.ids["profile"]
    )
    await change(engine, tenant, first.id, status="cancelled")
    assert (await TaskDispatcher(engine, RunQueue(engine)).run_once()).blocked == 1
    assert (await card(engine, second.id)).status == "blocked"
    assert await comments(engine, second.id) == ["a dependency was cancelled or deleted"]


async def test_the_dispatcher_keeps_the_running_cap_of_an_org(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    for n in range(3):
        await create(engine, tenant, title=f"card {n}", assignee_profile_id=tenant.ids["profile"])
    dispatcher = TaskDispatcher(engine, RunQueue(engine), max_running_per_org=2)
    assert (await dispatcher.run_once()).started == 2
    assert (await dispatcher.run_once()).started == 0


async def test_the_dispatcher_ends_a_card_when_its_run_ends(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    good = await create(engine, tenant, title="good", assignee_profile_id=tenant.ids["profile"])
    dispatcher = TaskDispatcher(engine, queue)
    await dispatcher.run_once()
    good_row = await card(engine, good.id)
    run = await claim(queue, good_row.run_id)
    await queue.complete(run, OWNER, output="All done: 3 files.", runtime_state=None, usage=Usage())
    assert (await dispatcher.run_once()).finished == 1
    row = await card(engine, good.id)
    assert (row.status, row.result) == ("done", "All done: 3 files.")
    assert row.finished_at is not None

    bad = await create(engine, tenant, title="bad", assignee_profile_id=tenant.ids["profile"])
    await dispatcher.run_once()
    bad_row = await card(engine, bad.id)
    run = await claim(queue, bad_row.run_id)
    await queue.fail(run, OWNER, code=FailureCode.TIMEOUT, message="too slow", usage=Usage())
    assert (await dispatcher.run_once()).finished == 1
    assert (await card(engine, bad.id)).status == "blocked"
    assert await comments(engine, bad.id) == ["the run failed: too slow"]


async def test_waiting_cards_do_not_stop_the_cards_of_another_org(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    human = await create(engine, tenant, title="a person does this first")
    for n in range(4):
        await create(
            engine,
            tenant,
            title=f"waits {n}",
            depends_on=[human.id],
            assignee_profile_id=tenant.ids["profile"],
        )
    other = await add_tenant(engine)
    try:
        ready = await create(engine, other, title="ready", assignee_profile_id=other.ids["profile"])
        dispatcher = TaskDispatcher(engine, RunQueue(engine), batch=2)
        assert (await dispatcher.run_once()).started == 1
        assert (await card(engine, ready.id)).status == "running"
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                update(Run)
                .where(Run.org_id == other.org, Run.status == "queued")
                .values(status="cancelled", finished_at=func.now())
            )
            await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
            await connection.execute(delete(Org).where(Org.id == other.org))


async def test_two_dispatchers_keep_the_running_cap_of_an_org(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    for n in range(6):
        await create(engine, tenant, title=f"card {n}", assignee_profile_id=tenant.ids["profile"])
    queue = RunQueue(engine)
    first = TaskDispatcher(engine, queue, batch=1, max_running_per_org=1)
    second = TaskDispatcher(engine, queue, batch=1, max_running_per_org=1)
    for _ in range(3):
        await asyncio.gather(first.dispatch(), second.dispatch())
    async with engine.connect() as connection:
        running = (
            await connection.execute(
                select(func.count()).where(Task.org_id == tenant.org, Task.status == "running")
            )
        ).scalar_one()
    assert running == 1


async def test_a_new_attempt_does_not_keep_the_old_result(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    task = await create(engine, tenant, title="retry", assignee_profile_id=tenant.ids["profile"])
    dispatcher = TaskDispatcher(engine, queue)
    for answer in ("old result", "new result"):
        await dispatcher.run_once()
        row = await card(engine, task.id)
        assert (row.status, row.result) == ("running", None)
        run = await claim(queue, row.run_id)
        await queue.complete(run, OWNER, output=answer, runtime_state=None, usage=Usage())
        assert (await dispatcher.run_once()).finished == 1
        assert (await card(engine, task.id)).result == answer
        await change(engine, tenant, task.id, status="todo")


def test_the_prompt_cuts_long_dependency_results() -> None:
    parent = type("Card", (), {"id": uuid.uuid4(), "title": "t", "body": ""})()
    dependency = type("Dep", (), {"id": uuid.uuid4(), "title": "d", "result": "x" * 5_000})()
    text = task_prompt(parent, [dependency])
    assert "[cut: 3500 chars]" in text


# -- the run tools ----------------------------------------------------------------------------


async def test_a_task_run_completes_its_own_card(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    task = await create(engine, tenant, title="t", assignee_profile_id=tenant.ids["profile"])
    dispatcher = TaskDispatcher(engine, queue)
    await dispatcher.run_once()
    row = await card(engine, task.id)
    run = await claim(queue, row.run_id)
    tools = TaskTools(engine)
    me = run_claims(tenant, row.session_id, row.run_id)

    child = await tools.create(me, title="A follow-up", assignee="none")
    assert child.parent_task_id == task.id
    assert child.audience == "shared"
    found, current = await tools.list(me)
    assert current == task.id
    assert "(this run's task)" in render_tasks(found, current)

    await tools.comment(me, body="half way", task_id=None)
    done = await tools.finish(me, status="done", text="The result.")
    assert (done.status, done.result) == ("done", "The result.")
    await queue.complete(run, OWNER, output="final words", runtime_state=None, usage=Usage())
    await dispatcher.run_once()
    # The dispatcher does not change a card that the run ended itself.
    assert (await card(engine, task.id)).result == "The result."
    assert await comments(engine, task.id) == ["half way"]


async def test_a_run_cannot_stop_the_run_of_another_card(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    other = await create(engine, tenant, title="other", assignee_profile_id=tenant.ids["profile"])
    await TaskDispatcher(engine, queue).run_once()
    chat_run = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["s1"], text="hi", user_id=tenant.ids["mia"]
    )
    me = run_claims(tenant, tenant.ids["s1"], chat_run)
    with pytest.raises(TaskError, match="Only a person"):
        await TaskTools(engine).finish(me, status="blocked", text="x", task_id=str(other.id))
    assert (await card(engine, other.id)).status == "running"
    with pytest.raises(TaskError, match="not a task run"):
        await TaskTools(engine).finish(me, status="done", text="x")


async def test_a_shared_session_does_not_see_private_cards(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    private = await create(engine, tenant, title="private", audience="private")
    shared_run = await queue.enqueue(org_id=tenant.org, session_id=tenant.ids["s1"], text="hi")
    private_run = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["mine"], text="hi", user_id=tenant.ids["mia"]
    )
    tools = TaskTools(engine)
    shared_view, _ = await tools.list(run_claims(tenant, tenant.ids["s1"], shared_run))
    private_view, _ = await tools.list(run_claims(tenant, tenant.ids["mine"], private_run))
    assert private.id not in {t.id for t in shared_view}
    assert private.id in {t.id for t in private_view}
    with pytest.raises(TaskNotFoundError):
        await tools.comment(
            run_claims(tenant, tenant.ids["s1"], shared_run), body="x", task_id=str(private.id)
        )
    # A card from the private session is private to its owner, and "person" is that owner.
    card_ = await tools.create(
        run_claims(tenant, tenant.ids["mine"], private_run), title="mine", assignee="person"
    )
    assert (card_.audience, card_.owner_user_id) == ("private", tenant.ids["mia"])
    assert card_.assignee_user_id == tenant.ids["mia"]
