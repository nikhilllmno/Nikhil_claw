"""The deletes and the exports of M6.8 (docs/architecture.md, Section 17.3) on a real
database. Each test seeds its own two orgs, because a delete changes them."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.datacontrols.delete import (
    TRACE_DELAY,
    DataBusyError,
    DataNotFoundError,
    LastOwnerError,
    delete_sessions,
    delete_user,
)
from sama.datacontrols.export import SECRET_COLUMNS, export_tables
from sama.datacontrols.service import DataControls
from sama.db.engine import as_user
from sama.db.models import (
    AuditEntry,
    Base,
    DataPurge,
    Membership,
    OrgInvite,
    Run,
    Session,
    User,
)
from tests.db.world import World


async def org_counts(engine: AsyncEngine, org_id: uuid.UUID) -> dict[str, int]:
    """The rows of the org in each table with ``org_id``, and the org row."""
    counts: dict[str, int] = {}
    async with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name == "orgs":
                where = table.c.id == org_id
            elif "org_id" in table.c:
                where = table.c.org_id == org_id
            else:
                continue
            counts[table.name] = (
                await connection.execute(select(func.count()).select_from(table).where(where))
            ).scalar_one()
    return counts


async def set_running(engine: AsyncEngine, run_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                status="running",
                lease_owner="worker-1",
                lease_expires_at=text("now() + interval '1 hour'"),
            )
        )


async def test_an_org_delete_removes_every_row_of_the_org_and_no_row_of_the_other(
    engine: AsyncEngine, fresh: World
) -> None:
    org_a, org_b = fresh.ids["org-a"], fresh.ids["org-b"]
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == fresh.ids["run:b1-shared"])
            .values(trace_exported_at=func.now())
        )
    before_a = await org_counts(engine, org_a)
    before_b = await org_counts(engine, org_b)
    assert before_b["sessions"] == 2
    assert before_b["audit_log"] >= 1

    removed = await DataControls(engine).delete_org(org_b, actor_user_id=fresh.ids["bob"])

    assert len(removed.sessions) == 2
    after_b = await org_counts(engine, org_b)
    kept = {name: count for name, count in after_b.items() if count and name != "data_purges"}
    assert kept == {}
    assert await org_counts(engine, org_a) == before_a
    async with engine.connect() as connection:
        purges = (
            await connection.execute(
                select(DataPurge.kind, DataPurge.refs).where(
                    DataPurge.org_id == org_b, DataPurge.done_at.is_(None)
                )
            )
        ).all()
    refs = {(row.kind, json.dumps(row.refs, sort_keys=True)) for row in purges}
    assert ("sandboxes", '{"all": true}') in refs
    trace = fresh.ids["run:b1-shared"].hex
    assert ("traces", json.dumps({"trace_ids": [trace]})) in refs
    async with engine.connect() as connection:
        due = (
            await connection.execute(
                select(func.min(DataPurge.next_attempt_at)).where(
                    DataPurge.org_id == org_b, DataPurge.kind == "traces"
                )
            )
        ).scalar_one()
    # The exporter can send a claimed trace now. The purge comes after it.
    assert due > datetime.now(UTC) + TRACE_DELAY - timedelta(minutes=5)


async def test_a_user_delete_removes_the_private_data_and_keeps_the_shared_data(
    engine: AsyncEngine, fresh: World
) -> None:
    org_a = fresh.ids["org-a"]
    alice = fresh.ids["alice"]
    before_b = await org_counts(engine, fresh.ids["org-b"])

    removed = await DataControls(engine).delete_user(org_a, alice, actor_user_id=alice)

    assert removed.sessions == [fresh.ids["a1-alice"]]
    async with engine.connect() as connection:
        sessions = set(
            (await connection.execute(select(Session.id).where(Session.org_id == org_a))).scalars()
        )
        users = set(
            (await connection.execute(select(User.id).where(User.org_id == org_a))).scalars()
        )
        members = (
            await connection.execute(
                select(func.count()).select_from(Membership).where(Membership.user_id == alice)
            )
        ).scalar_one()
        audit = (
            await connection.execute(
                select(AuditEntry.details).where(
                    AuditEntry.org_id == org_a, AuditEntry.action == "user.deleted"
                )
            )
        ).scalar_one()
        purge = (
            await connection.execute(
                select(DataPurge.refs).where(
                    DataPurge.org_id == org_a, DataPurge.kind == "sandboxes"
                )
            )
        ).scalar_one()
    assert sessions == {fresh.ids["a1-shared"], fresh.ids["a2-shared"]}
    assert users == {fresh.ids["carol"], fresh.ids["dave"]}
    assert members == 0
    assert audit["sessions"] == 1
    assert purge == {"session_ids": [str(fresh.ids["a1-alice"])]}
    assert await org_counts(engine, fresh.ids["org-b"]) == before_b
    # Alice has no user row in the org now, so RLS shows her nothing of it.
    async with as_user(engine, fresh.auth["alice"]) as connection:
        seen = (await connection.execute(select(func.count()).select_from(Session))).scalar_one()
    assert seen == 0


async def test_a_user_delete_removes_the_invites_of_the_user_and_keeps_sent_ones(
    engine: AsyncEngine, fresh: World
) -> None:
    org_a, alice = fresh.ids["org-a"], fresh.ids["alice"]
    async with engine.begin() as connection:
        email = (await connection.execute(select(User.email).where(User.id == alice))).scalar_one()
        accepted, pending, sent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        base = {"org_id": org_a, "role": "member", "expires_at": text("now() + interval '1 day'")}
        await connection.execute(
            insert(OrgInvite).values(
                id=accepted,
                email="old-" + (email or "alice@example.test").lower(),
                token_hash=uuid.uuid4().bytes * 2,
                accepted_at=func.now(),
                accepted_user_id=alice,
                **base,
            )
        )
        if email is not None:
            await connection.execute(
                insert(OrgInvite).values(
                    id=pending, email=email.lower(), token_hash=uuid.uuid4().bytes * 2, **base
                )
            )
        await connection.execute(
            insert(OrgInvite).values(
                id=sent,
                email="friend@example.test",
                token_hash=uuid.uuid4().bytes * 2,
                invited_by_user_id=alice,
                **base,
            )
        )

    await DataControls(engine).delete_user(org_a, alice, actor_user_id=fresh.ids["carol"])

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(OrgInvite.id, OrgInvite.invited_by_user_id).where(
                    OrgInvite.id.in_([accepted, pending, sent])
                )
            )
        ).all()
    # The accepted invite must name its user, so it goes. The sent invite stays without alice.
    assert [(row.id, row.invited_by_user_id) for row in rows] == [(sent, None)]


async def test_the_last_owner_cannot_be_deleted(engine: AsyncEngine, fresh: World) -> None:
    with pytest.raises(LastOwnerError):
        await DataControls(engine).delete_user(
            fresh.ids["org-b"], fresh.ids["bob"], actor_user_id=fresh.ids["bob"]
        )
    async with engine.connect() as connection:
        found = (
            await connection.execute(select(User.id).where(User.id == fresh.ids["bob"]))
        ).scalar_one_or_none()
    assert found == fresh.ids["bob"]


async def test_a_user_of_another_org_is_not_found(engine: AsyncEngine, fresh: World) -> None:
    with pytest.raises(DataNotFoundError):
        await DataControls(engine).delete_user(
            fresh.ids["org-a"], fresh.ids["bob"], actor_user_id=fresh.ids["carol"]
        )


async def test_a_running_run_blocks_the_delete_and_is_asked_to_stop(
    engine: AsyncEngine, fresh: World
) -> None:
    run_id = fresh.ids["run:a1-alice"]
    await set_running(engine, run_id)
    with pytest.raises(DataBusyError, match="asked them to stop"):
        await DataControls(engine).delete_user(
            fresh.ids["org-a"], fresh.ids["alice"], actor_user_id=fresh.ids["carol"]
        )
    async with engine.connect() as connection:
        cancel = (
            await connection.execute(select(Run.cancel_requested_at).where(Run.id == run_id))
        ).scalar_one()
        user = (
            await connection.execute(select(User.id).where(User.id == fresh.ids["alice"]))
        ).scalar_one_or_none()
    assert cancel is not None
    assert user is not None


async def started_by(engine: AsyncEngine, world: World, run: str, user: str, status: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == world.ids[run])
            .values(triggered_by_user_id=world.ids[user], status=status)
        )


async def run_state(engine: AsyncEngine, run_id: uuid.UUID) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(Run.status, Run.triggered_by_user_id, Run.cancel_requested_at).where(
                    Run.id == run_id
                )
            )
        ).one()


async def test_a_user_delete_cancels_the_waiting_shared_runs_of_the_user(
    engine: AsyncEngine, fresh: World
) -> None:
    await started_by(engine, fresh, "run:a1-shared", "alice", "queued")
    await started_by(engine, fresh, "run:a2-shared", "carol", "queued")

    await DataControls(engine).delete_user(
        fresh.ids["org-a"], fresh.ids["alice"], actor_user_id=fresh.ids["carol"]
    )

    # A run with no user has no user limits, so the run of alice must not go on.
    cancelled = await run_state(engine, fresh.ids["run:a1-shared"])
    assert (cancelled.status, cancelled.triggered_by_user_id) == ("cancelled", None)
    assert (await run_state(engine, fresh.ids["run:a2-shared"])).status == "queued"


async def test_a_running_shared_run_of_the_user_blocks_the_delete(
    engine: AsyncEngine, fresh: World
) -> None:
    run_id = fresh.ids["run:a1-shared"]
    await started_by(engine, fresh, "run:a1-shared", "alice", "queued")
    await set_running(engine, run_id)
    with pytest.raises(DataBusyError, match="asked them to stop"):
        await DataControls(engine).delete_user(
            fresh.ids["org-a"], fresh.ids["alice"], actor_user_id=fresh.ids["carol"]
        )
    state = await run_state(engine, run_id)
    assert state.triggered_by_user_id == fresh.ids["alice"]
    assert state.cancel_requested_at is not None
    # The delete checks again under its locks: a run that starts after the first check.
    async with engine.begin() as connection:
        removed = await delete_user(
            connection, fresh.ids["org-a"], fresh.ids["alice"], actor_user_id=fresh.ids["carol"]
        )
        await connection.rollback()
    assert removed.running == [run_id]


async def test_an_org_delete_waits_for_running_runs(engine: AsyncEngine, fresh: World) -> None:
    await set_running(engine, fresh.ids["run:b1-shared"])
    with pytest.raises(DataBusyError):
        await DataControls(engine).delete_org(fresh.ids["org-b"], actor_user_id=fresh.ids["bob"])
    assert (await org_counts(engine, fresh.ids["org-b"]))["sessions"] == 2


async def test_a_session_delete_takes_compaction_children_and_detaches_branches(
    engine: AsyncEngine, fresh: World
) -> None:
    org_a, root = fresh.ids["org-a"], fresh.ids["a1-shared"]
    base = {
        "org_id": org_a,
        "workspace_id": fresh.ids["a1"],
        "agent_profile_id": fresh.ids["profile-a"],
        "audience": "shared",
        "parent_session_id": root,
    }
    compaction, branch = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Session).values(id=compaction, lineage="compaction", title="c", **base)
        )
        await connection.execute(
            insert(Session).values(id=branch, lineage="branch", title="b", **base)
        )
        removed = await delete_sessions(connection, org_a, [root])
    assert set(removed.sessions) == {root, compaction}
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(Session.parent_session_id, Session.lineage).where(Session.id == branch)
            )
        ).one()
    assert (row.parent_session_id, row.lineage) == (None, None)


def _read(path: str) -> tuple[dict[str, object], dict[str, list[dict[str, object]]], set[str]]:
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        tables = {
            name.removeprefix("tables/").removesuffix(".jsonl"): [
                json.loads(line) for line in io.TextIOWrapper(archive.open(name), "utf-8")
            ]
            for name in archive.namelist()
            if name.startswith("tables/")
        }
        files = {name for name in archive.namelist() if name.startswith("files/")}
    return manifest, tables, files


def _org_ids(tables: Mapping[str, list[dict[str, object]]]) -> set[object]:
    return {
        row.get("org_id", row.get("id") if name == "orgs" else None)
        for name, rows in tables.items()
        for row in rows
    }


async def test_an_org_export_has_the_rows_of_the_org_and_no_secret(
    engine: AsyncEngine, fresh: World
) -> None:
    org_a = fresh.ids["org-a"]
    result = await DataControls(engine).export(org_a, kind="org", actor_user_id=fresh.ids["carol"])
    try:
        manifest, tables, files = _read(str(result.path))
    finally:
        result.path.unlink()
    assert manifest["format"] == "sama-export/1"
    assert _org_ids(tables) == {str(org_a)}
    assert {table.name for table in export_tables()} == set(tables)
    counts = await org_counts(engine, org_a)
    # The export wrote its audit row after the snapshot.
    assert tables["audit_log"]
    assert len(tables["audit_log"]) == counts["audit_log"] - 1
    for name, rows in tables.items():
        if name != "audit_log":
            assert len(rows) == counts[name], name
    for name, columns in SECRET_COLUMNS.items():
        for row in tables[name]:
            assert not columns & set(row), name
    assert "search_vector" not in tables["messages"][0]
    assert "embedding" not in tables["message_embeddings"][0]
    attachment = tables["attachments"][0]
    assert attachment["data_file"] in files
    assert "data" not in attachment
    assert "secret of b1" not in json.dumps(tables)


async def test_a_user_export_has_the_private_data_of_the_user_only(
    engine: AsyncEngine, fresh: World
) -> None:
    org_a, alice = fresh.ids["org-a"], fresh.ids["alice"]
    result = await DataControls(engine).export(
        org_a, kind="user", user_id=alice, actor_user_id=alice
    )
    try:
        manifest, tables, _ = _read(str(result.path))
    finally:
        result.path.unlink()
    assert manifest["user_id"] == str(alice)
    assert [row["id"] for row in tables["users"]] == [str(alice)]
    assert {row["id"] for row in tables["sessions"]} == {str(fresh.ids["a1-alice"])}
    assert {row["session_id"] for row in tables["messages"]} == {str(fresh.ids["a1-alice"])}
    assert "vault_secrets" not in tables
    assert _org_ids(tables) == {str(org_a)}
    async with engine.connect() as connection:
        audit = (
            await connection.execute(
                select(AuditEntry.details).where(
                    AuditEntry.org_id == org_a, AuditEntry.action == "data.exported"
                )
            )
        ).scalar_one()
    assert audit["kind"] == "user"


async def test_a_user_export_of_a_user_of_another_org_is_not_found(
    engine: AsyncEngine, fresh: World
) -> None:
    with pytest.raises(DataNotFoundError):
        await DataControls(engine).export(
            fresh.ids["org-a"],
            kind="user",
            user_id=fresh.ids["bob"],
            actor_user_id=fresh.ids["carol"],
        )


async def test_the_retention_setting_is_checked_and_audited(
    engine: AsyncEngine, fresh: World
) -> None:
    data = DataControls(engine)
    org_a = fresh.ids["org-a"]
    assert (await data.retention(org_a)).effective_days is None
    view = await data.set_retention(org_a, 30, actor_user_id=fresh.ids["carol"])
    assert view.effective_days == 30
    assert (await data.retention(org_a)).retention_days == 30
    assert (await data.retention(fresh.ids["org-b"])).retention_days is None
