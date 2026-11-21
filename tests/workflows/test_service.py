"""The workflow service (docs/architecture.md, Section 12.3): create, change, approve, start,
and steer a workflow. These tests use Postgres."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import (
    AuditEntry,
    Membership,
    Run,
    User,
    Workflow,
    WorkflowStep,
    WorkflowVersion,
)
from sama.scheduler.jobs import Actor
from sama.workflows import service as service_module
from sama.workflows.engine import WorkflowEngine
from sama.workflows.service import MAX_WORKFLOWS_PER_USER, WorkflowService
from sama.workflows.spec import MAX_DEPTH, WorkflowError, WorkflowNotFoundError
from tests.workflows.conftest import Tenant

READ = """
name: reader
inputs:
  topic: { default: tea }
  who: {}
steps:
  - id: look
    tool: notes_read
    args: { topic: "{{inputs.topic}}", who: "{{inputs.who}}" }
"""

WRITE = """
name: writer
steps:
  - id: post
    tool: notes_write
    args: { text: hi }
"""

AGENT_STEP = """
name: thinker
steps:
  - id: think
    agent: { prompt: "Think." }
"""

SCHEDULED = """
name: ticker
triggers:
  - schedule: every 2 hours
steps:
  - id: look
    tool: notes_read
"""

WAITER = """
name: waiter
steps:
  - id: hold
    wait: { event: go }
  - id: ask
    approval: { prompt: "Go on?" }
"""


def uid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


async def _create(service: WorkflowService, tenant: Tenant, source: str) -> dict[str, Any]:
    return await service.create(tenant.user(), source)


async def _approved(service: WorkflowService, tenant: Tenant, source: str) -> dict[str, Any]:
    view = await _create(service, tenant, source)
    if view["needs_approval"]:
        view = await service.approve(tenant.user(), uid(view["id"]), view["version"])
    return view


async def _versions(engine: AsyncEngine, workflow_id: str) -> list[Any]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    select(WorkflowVersion)
                    .where(WorkflowVersion.workflow_id == uid(workflow_id))
                    .order_by(WorkflowVersion.version)
                )
            ).all()
        )


async def _audit(engine: AsyncEngine, tenant: Tenant) -> list[str]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    select(AuditEntry.action)
                    .where(AuditEntry.org_id == tenant.org, AuditEntry.target_type == "workflow")
                    .order_by(AuditEntry.created_at, AuditEntry.id)
                )
            ).scalars()
        )


async def _second_user(tenant: Tenant) -> Actor:
    """A second member of the org of ``tenant``, in the same workspace."""
    user_id = uuid.uuid4()
    async with tenant.engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=user_id,
                org_id=tenant.org,
                auth_user_id=uuid.uuid4(),
                email="bo@example.test",
                display_name="bo",
            )
        )
        await connection.execute(
            insert(Membership).values(org_id=tenant.org, user_id=user_id, role="member")
        )
        await connection.execute(
            insert(Membership).values(
                org_id=tenant.org, user_id=user_id, workspace_id=tenant.ids["ws"], role="member"
            )
        )
    return tenant.user(user_id=user_id)


# -- validate ---------------------------------------------------------------------------------


def test_validate_reports_the_hash_and_the_reasons_for_an_approval(
    service: WorkflowService,
) -> None:
    report = service.validate(WRITE)
    assert report["needs_approval"] is True
    assert report["approval_reasons"] == ["step post calls notes_write (write)"]
    assert len(report["hash"]) == 64
    assert report["definition"]["name"] == "writer"
    assert report["next_fire_at"] is None


def test_validate_of_a_read_workflow_needs_no_approval(service: WorkflowService) -> None:
    report = service.validate(SCHEDULED)
    assert report["needs_approval"] is False
    assert report["approval_reasons"] == []
    assert report["next_fire_at"] is not None


def test_validate_gives_the_pre_approvals_of_an_agent_step_as_a_reason(
    service: WorkflowService,
) -> None:
    report = service.validate(
        "name: a\nsteps:\n  - id: s\n    agent:\n      prompt: go\n"
        "      preapproved_tools: [{ tool: mcp__notes__write }]\n"
    )
    assert report["needs_approval"] is True
    assert report["approval_reasons"] == ["step s pre-approves tools: mcp__notes__write"]


def test_validate_refuses_a_bad_text(service: WorkflowService) -> None:
    with pytest.raises(WorkflowError, match="needs steps"):
        service.validate("name: x\n")


def test_validate_refuses_a_secret(service: WorkflowService) -> None:
    source = READ + "\n# sk-ant-api03-" + "a" * 40 + "\n"
    with pytest.raises(WorkflowError, match="contains a secret"):
        service.validate(source)


# -- create -----------------------------------------------------------------------------------


async def test_create_a_read_workflow_is_active(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, READ)
    assert view["name"] == "reader"
    assert view["version"] == 1
    assert view["status"] == "active"
    assert view["needs_approval"] is False
    assert view["approved"] is False
    assert view["source"] == READ
    assert view["created_by_kind"] == "user"
    assert view["workspace_id"] == str(tenant.ids["ws"])
    assert view["agent_profile_id"] == str(tenant.ids["profile"])
    assert view["definition"]["steps"][0]["tool"] == "notes_read"
    [version] = await _versions(engine, view["id"])
    assert version.hash == view["hash"]
    assert version.needs_approval is False
    assert await _audit(engine, tenant) == ["workflow.created"]


async def test_create_a_scheduled_workflow_has_a_next_fire(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, SCHEDULED)
    assert view["status"] == "active"
    assert view["next_fire_at"] is not None
    assert view["triggers"][0]["kind"] == "interval"
    assert view["triggers"][0]["text"]


async def test_create_a_workflow_that_writes_waits_for_the_approval(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, WRITE)
    assert view["status"] == "pending_approval"
    assert view["needs_approval"] is True
    assert view["approved"] is False
    assert view["next_fire_at"] is None
    assert view["approval_reasons"] == ["step post calls notes_write (write)"]
    [version] = await _versions(engine, view["id"])
    assert version.needs_approval is True
    assert version.approved_hash is None


async def test_create_an_agent_step_that_pre_approves_tools_waits_for_the_approval(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(
        service,
        tenant,
        "name: a\nsteps:\n  - id: s\n    agent:\n      prompt: go\n"
        "      preapproved_tools: [{ tool: mcp__notes__write }]\n",
    )
    assert view["status"] == "pending_approval"


async def test_a_workflow_that_writes_cannot_start_before_the_approval(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, WRITE)
    with pytest.raises(WorkflowError, match="needs the approval of its owner"):
        await service.start(tenant.user(), uid(view["id"]))
    assert await service.instances(tenant.user()) == []


async def test_create_refuses_a_name_in_use(tenant: Tenant, service: WorkflowService) -> None:
    await _create(service, tenant, READ)
    with pytest.raises(WorkflowError, match="a workflow with the name reader"):
        await _create(service, tenant, READ)


async def test_two_users_can_use_the_same_name(tenant: Tenant, service: WorkflowService) -> None:
    other = await _second_user(tenant)
    await _create(service, tenant, READ)
    view = await service.create(other, READ)
    assert view["name"] == "reader"


async def test_create_refuses_a_workspace_that_the_owner_cannot_see(
    tenant: Tenant, service: WorkflowService
) -> None:
    with pytest.raises(WorkflowError, match="cannot use that workspace"):
        await service.create(tenant.user(), READ, workspace_id=uuid.uuid4())


async def test_create_refuses_a_missing_workspace_for_an_org_admin(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    """An org admin sees every workspace, so the service checks that the workspace exists in
    the org. A missing workspace is a user error, not a foreign key error."""
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.org_id == tenant.org, Membership.workspace_id.is_(None))
            .values(role="admin")
        )
    with pytest.raises(WorkflowError, match="cannot use that workspace"):
        await service.create(tenant.user(), READ, workspace_id=uuid.uuid4())


async def test_create_refuses_a_profile_that_does_not_exist(
    tenant: Tenant, service: WorkflowService
) -> None:
    with pytest.raises(WorkflowError, match="agent profile does not exist"):
        await service.create(tenant.user(), READ, agent_profile_id=uuid.uuid4())


async def test_create_refuses_a_definition_with_a_secret(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    secret = "sk-ant-api03-" + "a" * 40
    source = READ.replace("default: tea", f"default: {secret}")
    with pytest.raises(WorkflowError, match="contains a secret") as error:
        await _create(service, tenant, source)
    assert secret not in str(error.value)
    assert await service.list(tenant.user()) == []


async def test_update_refuses_a_definition_with_a_secret(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, READ)
    source = READ.replace("default: tea", "default: sk-ant-api03-" + "a" * 40)
    with pytest.raises(WorkflowError, match="contains a secret"):
        await service.update(tenant.user(), uid(view["id"]), source)
    assert (await service.get(tenant.user(), uid(view["id"])))["version"] == 1


async def test_create_refuses_the_workflow_over_the_limit(
    monkeypatch: pytest.MonkeyPatch, tenant: Tenant, service: WorkflowService
) -> None:
    assert MAX_WORKFLOWS_PER_USER == 50
    monkeypatch.setattr(service_module, "MAX_WORKFLOWS_PER_USER", 2)
    first = await _create(service, tenant, READ)
    await _create(service, tenant, READ.replace("name: reader", "name: reader-2"))
    with pytest.raises(WorkflowError, match="you have 2 workflows"):
        await _create(service, tenant, READ.replace("name: reader", "name: reader-3"))
    # A removed workflow does not count, and another user has a limit of their own.
    await service.remove(tenant.user(), uid(first["id"]))
    await _create(service, tenant, READ.replace("name: reader", "name: reader-3"))
    other = await _second_user(tenant)
    await service.create(other, READ)


async def test_the_limit_is_fifty_workflows_for_one_user(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    async with engine.begin() as connection:
        for number in range(MAX_WORKFLOWS_PER_USER):
            await connection.execute(
                insert(Workflow).values(
                    org_id=tenant.org,
                    workspace_id=tenant.ids["ws"],
                    owner_user_id=tenant.ids["ana"],
                    agent_profile_id=tenant.ids["profile"],
                    name=f"bulk-{number}",
                    description="",
                    version=1,
                    status="active",
                    created_by_kind="user",
                )
            )
    with pytest.raises(WorkflowError, match="you have 50 workflows"):
        await _create(service, tenant, READ)


# -- approve ----------------------------------------------------------------------------------


async def test_the_owner_approves_a_version_and_the_workflow_starts(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, WRITE)
    approved = await service.approve(tenant.user(), uid(view["id"]), view["version"])
    assert approved["status"] == "active"
    assert approved["needs_approval"] is False
    assert approved["approved"] is True
    [version] = await _versions(engine, view["id"])
    assert version.approved_hash == version.hash
    assert version.approved_by_user_id == tenant.ids["ana"]
    assert version.approved_at is not None
    instance = await service.start(tenant.user(), uid(view["id"]))
    assert instance["status"] == "running"
    assert instance["trigger"] == "manual"
    assert "workflow.approved" in await _audit(engine, tenant)


async def test_an_agent_cannot_approve(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, WRITE)
    with pytest.raises(WorkflowError, match="only a person can approve"):
        await service.approve(tenant.agent(), uid(view["id"]), view["version"])
    again = await service.get(tenant.user(), uid(view["id"]))
    assert again["status"] == "pending_approval"
    [version] = await _versions(engine, view["id"])
    assert version.approved_hash is None


async def test_an_old_version_cannot_be_approved(tenant: Tenant, service: WorkflowService) -> None:
    view = await _create(service, tenant, WRITE)
    changed = await service.update(
        tenant.user(), uid(view["id"]), WRITE.replace("text: hi", "text: bye")
    )
    assert changed["version"] == 2
    with pytest.raises(WorkflowError, match="it is version 2 now, not 1"):
        await service.approve(tenant.user(), uid(view["id"]), 1)
    approved = await service.approve(tenant.user(), uid(view["id"]), 2)
    assert approved["status"] == "active"


async def test_another_user_cannot_approve_a_workflow(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, WRITE)
    other = await _second_user(tenant)
    with pytest.raises(WorkflowNotFoundError):
        await service.approve(other, uid(view["id"]), view["version"])


async def test_an_approval_of_a_version_that_changed_in_the_database_does_not_hold(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    """The approval binds the hash. A stored definition that no longer has the hash cannot run."""
    view = await _approved(service, tenant, WRITE)
    [version] = await _versions(engine, view["id"])
    async with engine.begin() as connection:
        await connection.execute(
            update(WorkflowVersion)
            .where(WorkflowVersion.id == version.id)
            .values(definition={**version.definition, "description": "changed"})
        )
    shown = await service.get(tenant.user(), uid(view["id"]))
    assert shown["approved"] is False
    assert shown["needs_approval"] is True
    with pytest.raises(WorkflowError, match="needs the approval of its owner"):
        await service.start(tenant.user(), uid(view["id"]))


async def test_approve_of_a_read_workflow_is_allowed_and_keeps_it_active(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, READ)
    approved = await service.approve(tenant.user(), uid(view["id"]), 1)
    assert approved["status"] == "active"
    assert approved["approved"] is True


# -- update -----------------------------------------------------------------------------------


async def test_an_edit_makes_a_new_version_and_a_write_needs_the_approval_again(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    view = await _approved(service, tenant, WRITE)
    assert view["status"] == "active"
    changed = await service.update(
        tenant.user(), uid(view["id"]), WRITE.replace("text: hi", "text: bye")
    )
    assert changed["version"] == 2
    assert changed["status"] == "pending_approval"
    assert changed["needs_approval"] is True
    assert changed["approved"] is False
    assert changed["next_fire_at"] is None
    first, second = await _versions(engine, view["id"])
    assert first.approved_hash is not None
    assert second.approved_hash is None
    assert first.hash != second.hash
    with pytest.raises(WorkflowError, match="version 2 needs the approval"):
        await service.start(tenant.user(), uid(view["id"]))


async def test_an_edit_that_changes_nothing_keeps_the_version(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    view = await _approved(service, tenant, WRITE)
    same = await service.update(tenant.user(), uid(view["id"]), WRITE + "\n# a comment\n")
    assert same["version"] == 1
    assert same["status"] == "active"
    assert len(await _versions(engine, view["id"])) == 1


async def test_an_edit_of_a_read_workflow_stays_active(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, READ)
    changed = await service.update(
        tenant.user(), uid(view["id"]), READ.replace("default: tea", "default: coffee")
    )
    assert changed["version"] == 2
    assert changed["status"] == "active"
    assert changed["definition"]["inputs"]["topic"]["default"] == "coffee"


async def test_an_edit_can_rename_a_workflow_but_not_to_a_name_in_use(
    tenant: Tenant, service: WorkflowService
) -> None:
    first = await _create(service, tenant, READ)
    await _create(service, tenant, READ.replace("name: reader", "name: other"))
    with pytest.raises(WorkflowError, match="a workflow with the name other"):
        await service.update(tenant.user(), uid(first["id"]), READ.replace("reader", "other"))
    renamed = await service.update(
        tenant.user(), uid(first["id"]), READ.replace("name: reader", "name: renamed")
    )
    assert renamed["name"] == "renamed"


async def test_an_edit_of_a_paused_workflow_keeps_it_paused(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, SCHEDULED)
    await service.pause(tenant.user(), uid(view["id"]))
    changed = await service.update(
        tenant.user(), uid(view["id"]), SCHEDULED.replace("every 2 hours", "every 3 hours")
    )
    assert changed["version"] == 2
    assert changed["status"] == "paused"
    assert changed["next_fire_at"] is None


async def test_an_instance_keeps_the_version_that_it_started_with(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, READ)
    instance = await service.start(tenant.user(), uid(view["id"]), {"who": "Ana"})
    await service.update(
        tenant.user(), uid(view["id"]), READ.replace("default: tea", "default: coffee")
    )
    assert (await service.instance(tenant.user(), uid(instance["id"])))["version"] == 1
    newer = await service.start(tenant.user(), uid(view["id"]), {"who": "Ana"})
    assert newer["version"] == 2
    assert newer["inputs"]["topic"] == "coffee"


# -- pause, resume, remove --------------------------------------------------------------------


async def test_pause_and_resume(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, SCHEDULED)
    assert view["next_fire_at"] is not None
    paused = await service.pause(tenant.user(), uid(view["id"]))
    assert paused["status"] == "paused"
    assert paused["next_fire_at"] is None
    resumed = await service.resume(tenant.user(), uid(view["id"]))
    assert resumed["status"] == "active"
    assert resumed["next_fire_at"] is not None
    assert await _audit(engine, tenant) == [
        "workflow.created",
        "workflow.pause",
        "workflow.resume",
    ]


async def test_a_paused_workflow_starts_by_hand_but_not_by_a_trigger(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, READ)
    await service.pause(tenant.user(), uid(view["id"]))
    instance = await service.start(tenant.user(), uid(view["id"]), {"who": "Ana"})
    assert instance["status"] == "running"
    async with engine.begin() as connection:
        with pytest.raises(WorkflowError, match="is paused"):
            await service.start_in(
                connection,
                org_id=tenant.org,
                workflow_id=uid(view["id"]),
                inputs={"who": "Ana"},
                trigger="event",
            )


async def test_a_resume_does_not_approve_a_version(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, WRITE)
    await service.pause(tenant.user(), uid(view["id"]))
    resumed = await service.resume(tenant.user(), uid(view["id"]))
    assert resumed["status"] == "pending_approval"
    assert resumed["approved"] is False
    with pytest.raises(WorkflowError, match="needs the approval"):
        await service.start(tenant.user(), uid(view["id"]))


async def test_a_resume_of_an_approved_version_makes_it_active(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _approved(service, tenant, WRITE)
    await service.pause(tenant.user(), uid(view["id"]))
    resumed = await service.resume(tenant.user(), uid(view["id"]))
    assert resumed["status"] == "active"


async def test_remove_hides_the_workflow(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, READ)
    await service.remove(tenant.user(), uid(view["id"]))
    assert await service.list(tenant.user()) == []
    with pytest.raises(WorkflowNotFoundError):
        await service.get(tenant.user(), uid(view["id"]))
    with pytest.raises(WorkflowNotFoundError):
        await service.start(tenant.user(), uid(view["id"]), {"who": "Ana"})
    with pytest.raises(WorkflowNotFoundError):
        await service.remove(tenant.user(), uid(view["id"]))
    assert "workflow.remove" in await _audit(engine, tenant)
    # The name is free again.
    await _create(service, tenant, READ)


# -- list, get, find --------------------------------------------------------------------------


async def test_list_shows_only_the_workflows_of_the_actor(
    tenant: Tenant, service: WorkflowService
) -> None:
    await _create(service, tenant, READ)
    await _create(service, tenant, WRITE)
    other = await _second_user(tenant)
    await service.create(other, READ.replace("name: reader", "name: theirs"))
    names = [item["name"] for item in await service.list(tenant.user())]
    assert names == ["reader", "writer"]
    assert [item["name"] for item in await service.list(other)] == ["theirs"]
    first = (await service.list(tenant.user()))[0]
    assert set(first) >= {"id", "name", "version", "status", "next_fire_at", "workspace_id"}
    assert "definition" not in first


async def test_get_shows_the_recent_instances(tenant: Tenant, service: WorkflowService) -> None:
    view = await _create(service, tenant, READ)
    for _ in range(3):
        await service.start(tenant.user(), uid(view["id"]), {"who": "Ana"})
    shown = await service.get(tenant.user(), uid(view["id"]))
    assert len(shown["instances"]) == 3
    assert shown["instances"][0]["workflow"] == "reader"


async def test_get_of_a_workflow_of_another_user_is_not_found(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, READ)
    other = await _second_user(tenant)
    with pytest.raises(WorkflowNotFoundError):
        await service.get(other, uid(view["id"]))
    with pytest.raises(WorkflowNotFoundError):
        await service.pause(other, uid(view["id"]))
    with pytest.raises(WorkflowNotFoundError):
        await service.update(other, uid(view["id"]), READ)
    with pytest.raises(WorkflowNotFoundError):
        await service.start(other, uid(view["id"]), {"who": "x"})


async def test_get_of_an_unknown_workflow_is_not_found(
    tenant: Tenant, service: WorkflowService
) -> None:
    with pytest.raises(WorkflowNotFoundError, match="does not exist"):
        await service.get(tenant.user(), uuid.uuid4())


async def test_find_by_id_or_name(tenant: Tenant, service: WorkflowService) -> None:
    view = await _create(service, tenant, READ)
    assert await service.find(tenant.user(), view["id"]) == uid(view["id"])
    assert await service.find(tenant.user(), "reader") == uid(view["id"])
    with pytest.raises(WorkflowNotFoundError):
        await service.find(tenant.user(), "missing")
    with pytest.raises(WorkflowNotFoundError):
        await service.find(tenant.user(), str(uuid.uuid4()))
    other = await _second_user(tenant)
    with pytest.raises(WorkflowNotFoundError):
        await service.find(other, "reader")
    with pytest.raises(WorkflowNotFoundError):
        await service.find(other, view["id"])


# -- start ------------------------------------------------------------------------------------


async def test_start_with_inputs_and_defaults(tenant: Tenant, service: WorkflowService) -> None:
    view = await _create(service, tenant, READ)
    instance = await service.start(tenant.user(), uid(view["id"]), {"who": "Ana"})
    assert instance["inputs"] == {"topic": "tea", "who": "Ana"}
    assert instance["status"] == "running"
    assert instance["workflow"] == "reader"
    assert instance["steps"] == []
    assert instance["receipts"] == []
    assert instance["depth"] == 0
    assert instance["parent_instance_id"] is None
    given = await service.start(tenant.user(), uid(view["id"]), {"who": "Ana", "topic": "jam"})
    assert given["inputs"]["topic"] == "jam"


async def test_start_refuses_a_missing_or_unknown_input(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, READ)
    with pytest.raises(WorkflowError, match="the input who is required"):
        await service.start(tenant.user(), uid(view["id"]))
    with pytest.raises(WorkflowError, match="unknown inputs: zed"):
        await service.start(tenant.user(), uid(view["id"]), {"who": "Ana", "zed": 1})
    assert await service.instances(tenant.user()) == []


async def test_start_by_an_agent_outside_a_workflow_is_allowed(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, READ)
    instance = await service.start(tenant.agent(), uid(view["id"]), {"who": "Ana"})
    assert instance["status"] == "running"
    assert instance["workflow_id"] == view["id"]


async def test_start_in_gives_the_event_as_an_input_and_skips_a_repeated_fire(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(
        service,
        tenant,
        "name: evt\ninputs:\n  event: { default: '' }\nsteps:\n  - id: a\n    tool: notes_read\n",
    )
    fire = datetime(2030, 1, 1, tzinfo=UTC)
    async with engine.begin() as connection:
        first = await service.start_in(
            connection,
            org_id=tenant.org,
            workflow_id=uid(view["id"]),
            inputs=None,
            trigger="schedule",
            scheduled_for=fire,
            toolsets=["core"],
            event='{"a": 1}',
        )
        again = await service.start_in(
            connection,
            org_id=tenant.org,
            workflow_id=uid(view["id"]),
            inputs=None,
            trigger="schedule",
            scheduled_for=fire,
        )
    assert first is not None
    assert again is None
    instance = await service.instance(tenant.user(), first)
    assert instance["inputs"] == {"event": '{"a": 1}'}
    assert instance["trigger"] == "schedule"
    assert instance["scheduled_for"] is not None


async def test_start_in_refuses_a_workflow_that_does_not_exist(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    async with engine.begin() as connection:
        with pytest.raises(WorkflowNotFoundError):
            await service.start_in(
                connection,
                org_id=tenant.org,
                workflow_id=uuid.uuid4(),
                inputs=None,
                trigger="event",
            )


async def test_start_in_does_not_start_a_workflow_that_waits_for_the_approval(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, WRITE)
    async with engine.begin() as connection:
        with pytest.raises(WorkflowError, match="needs the approval"):
            await service.start_in(
                connection,
                org_id=tenant.org,
                workflow_id=uid(view["id"]),
                inputs=None,
                trigger="event",
            )


# -- instances --------------------------------------------------------------------------------


async def test_list_and_get_instances(tenant: Tenant, service: WorkflowService) -> None:
    reader = await _create(service, tenant, READ)
    other = await _create(service, tenant, SCHEDULED)
    first = await service.start(tenant.user(), uid(reader["id"]), {"who": "a"})
    second = await service.start(tenant.user(), uid(reader["id"]), {"who": "b"})
    third = await service.start(tenant.user(), uid(other["id"]))
    everything = await service.instances(tenant.user())
    assert [item["id"] for item in everything] == [third["id"], second["id"], first["id"]]
    only = await service.instances(tenant.user(), uid(reader["id"]))
    assert [item["id"] for item in only] == [second["id"], first["id"]]
    assert len(await service.instances(tenant.user(), limit=1)) == 1
    assert len(await service.instances(tenant.user(), limit=0)) == 1
    shown = await service.instance(tenant.user(), uid(first["id"]))
    assert shown["inputs"]["who"] == "a"
    assert set(shown) >= {"steps", "receipts", "status", "workflow_id", "version"}


async def test_instances_of_another_user_are_hidden(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, READ)
    instance = await service.start(tenant.user(), uid(view["id"]), {"who": "a"})
    other = await _second_user(tenant)
    assert await service.instances(other) == []
    with pytest.raises(WorkflowNotFoundError, match="the instance does not exist"):
        await service.instance(other, uid(instance["id"]))
    with pytest.raises(WorkflowNotFoundError):
        await service.cancel(other, uid(instance["id"]))
    with pytest.raises(WorkflowNotFoundError):
        await service.signal(other, uid(instance["id"]), "go")
    with pytest.raises(WorkflowNotFoundError):
        await service.decide(other, uid(instance["id"]), "ask", approve=True)
    with pytest.raises(WorkflowNotFoundError):
        await service.resolve(other, uid(instance["id"]), "look", "done")


async def test_an_instance_view_clips_a_long_output(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    view = await _create(service, tenant, READ)
    instance = await service.start(tenant.user(), uid(view["id"]), {"who": "a"})
    await runner.run_once()
    await runner.run_once()
    async with engine.begin() as connection:
        await connection.execute(
            update(WorkflowStep)
            .where(WorkflowStep.instance_id == uid(instance["id"]))
            .values(output="y" * 6000)
        )
    shown = await service.instance(tenant.user(), uid(instance["id"]))
    [step] = shown["steps"]
    assert isinstance(step["output"], str)
    assert len(step["output"]) == service_module.MAX_VIEW_CHARS
    assert step["output"].endswith("…")


# -- cancel -----------------------------------------------------------------------------------


async def test_cancel_ends_an_open_instance_and_its_steps(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    view = await _create(service, tenant, WAITER)
    instance = await service.start(tenant.user(), uid(view["id"]))
    await runner.run_once()
    shown = await service.instance(tenant.user(), uid(instance["id"]))
    assert shown["status"] == "waiting"
    assert [step["status"] for step in shown["steps"]] == ["waiting"]
    cancelled = await service.cancel(tenant.user(), uid(instance["id"]))
    assert cancelled["status"] == "cancelled"
    assert cancelled["error"] == "cancelled"
    assert cancelled["finished_at"] is not None
    assert [step["status"] for step in cancelled["steps"]] == ["cancelled"]
    assert "workflow.cancelled" in await _audit(engine, tenant)


async def test_cancel_refuses_an_instance_that_ended(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, WAITER)
    instance = await service.start(tenant.user(), uid(view["id"]))
    await service.cancel(tenant.user(), uid(instance["id"]))
    with pytest.raises(WorkflowError, match="the instance is cancelled"):
        await service.cancel(tenant.user(), uid(instance["id"]))
    with pytest.raises(WorkflowError, match="the instance is cancelled"):
        await service.signal(tenant.user(), uid(instance["id"]), "go")


async def test_cancel_cancels_the_run_of_an_agent_step(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    view = await _create(service, tenant, AGENT_STEP)
    instance = await service.start(tenant.user(), uid(view["id"]))
    await runner.run_once()
    await runner.run_once()
    [step] = (await service.instance(tenant.user(), uid(instance["id"])))["steps"]
    assert step["run_id"] is not None
    await service.cancel(tenant.user(), uid(instance["id"]))
    async with engine.connect() as connection:
        status = (
            await connection.execute(select(Run.status).where(Run.id == uid(step["run_id"])))
        ).scalar_one()
    assert status == "cancelled"


# -- signal -----------------------------------------------------------------------------------


async def test_signal_ends_the_wait_step_for_that_event(
    tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    view = await _create(service, tenant, WAITER)
    instance = await service.start(tenant.user(), uid(view["id"]))
    await runner.run_once()
    assert await service.signal(tenant.user(), uid(instance["id"]), "other") == 0
    assert await service.signal(tenant.user(), uid(instance["id"]), "go", {"ok": 1}) == 1
    # The step ended, so a second signal finds nothing to end.
    assert await service.signal(tenant.user(), uid(instance["id"]), "go") == 0
    shown = await service.instance(tenant.user(), uid(instance["id"]))
    [hold] = shown["steps"]
    assert hold["status"] == "completed"
    assert hold["output"] == {"ok": 1}
    await runner.run_once()
    await runner.run_once()
    shown = await service.instance(tenant.user(), uid(instance["id"]))
    assert {step["step_id"]: step["status"] for step in shown["steps"]} == {
        "hold": "completed",
        "ask": "waiting",
    }


# -- decide and resolve -----------------------------------------------------------------------


async def test_decide_needs_a_waiting_approval_step(
    tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    view = await _create(service, tenant, WAITER)
    instance = await service.start(tenant.user(), uid(view["id"]))
    await runner.run_once()
    with pytest.raises(WorkflowError, match="step ask does not wait for an approval"):
        await service.decide(tenant.user(), uid(instance["id"]), "ask", approve=True)
    with pytest.raises(WorkflowError, match="step nope does not wait for an approval"):
        await service.decide(tenant.user(), uid(instance["id"]), "nope", approve=False)


async def test_resolve_needs_a_person_and_a_valid_action(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await _create(service, tenant, READ)
    instance = await service.start(tenant.user(), uid(view["id"]), {"who": "a"})
    with pytest.raises(WorkflowError, match="only a person can resolve"):
        await service.resolve(tenant.agent(), uid(instance["id"]), "look", "done")
    with pytest.raises(WorkflowError, match="must be done, retry, or fail"):
        await service.resolve(
            tenant.user(),
            uid(instance["id"]),
            "look",
            "bogus",  # type: ignore[arg-type]
        )
    with pytest.raises(WorkflowError, match="step look does not wait for a person"):
        await service.resolve(tenant.user(), uid(instance["id"]), "look", "done")


async def test_decide_by_an_agent_is_refused(tenant: Tenant, service: WorkflowService) -> None:
    view = await _create(service, tenant, WAITER)
    instance = await service.start(tenant.user(), uid(view["id"]))
    with pytest.raises(WorkflowError, match="only a person can decide"):
        await service.decide(tenant.agent(), uid(instance["id"]), "ask", approve=True)


# -- the recursion guard ----------------------------------------------------------------------


async def _agent_step_actor(
    tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> Actor:
    """The actor of an agent step of a workflow: its run belongs to a step of an instance."""
    view = await _create(service, tenant, AGENT_STEP)
    instance = await service.start(tenant.user(), uid(view["id"]))
    await runner.run_once()
    await runner.run_once()
    [step] = (await service.instance(tenant.user(), uid(instance["id"])))["steps"]
    assert step["run_id"] is not None
    return await service.agent_actor(
        org_id=tenant.org,
        workspace_id=tenant.ids["ws"],
        session_id=uid(step["session_id"]),
        run_id=uid(step["run_id"]),
        user_id=None,
    )


async def test_an_agent_step_cannot_create_change_or_start_a_workflow(
    tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    actor = await _agent_step_actor(tenant, service, runner)
    assert actor.kind == "agent"
    assert actor.user_id == tenant.ids["ana"]
    assert await service.in_a_workflow_run(tenant.org, actor.run_id or uuid.uuid4()) is True
    victim = await _create(service, tenant, READ)
    guard = "an agent step of a workflow cannot change or start workflows"
    with pytest.raises(WorkflowError, match=guard):
        await service.create(actor, READ.replace("name: reader", "name: sneaky"))
    with pytest.raises(WorkflowError, match=guard):
        await service.update(actor, uid(victim["id"]), READ.replace("tea", "jam"))
    with pytest.raises(WorkflowError, match=guard):
        await service.start(actor, uid(victim["id"]), {"who": "a"})
    with pytest.raises(WorkflowError, match=guard):
        await service.pause(actor, uid(victim["id"]))
    with pytest.raises(WorkflowError, match=guard):
        await service.resume(actor, uid(victim["id"]))
    with pytest.raises(WorkflowError, match=guard):
        await service.remove(actor, uid(victim["id"]))
    with pytest.raises(WorkflowError, match=guard):
        await service.cancel(actor, uid(victim["id"]))
    with pytest.raises(WorkflowError, match=guard):
        await service.signal(actor, uid(victim["id"]), "go")
    # A read is allowed.
    assert (await service.get(actor, uid(victim["id"])))["version"] == 1
    assert len(await service.list(actor)) == 2


async def test_an_agent_of_a_normal_chat_run_is_not_in_a_workflow_run(
    tenant: Tenant, service: WorkflowService
) -> None:
    assert await service.in_a_workflow_run(tenant.org, tenant.ids["chat-run"]) is False


async def test_agent_actor_uses_the_user_of_the_run(
    tenant: Tenant, service: WorkflowService
) -> None:
    actor = await service.agent_actor(
        org_id=tenant.org,
        workspace_id=tenant.ids["ws"],
        session_id=tenant.ids["chat"],
        run_id=tenant.ids["chat-run"],
        user_id=None,
    )
    assert actor.kind == "agent"
    assert actor.user_id == tenant.ids["ana"]
    assert actor.run_id == tenant.ids["chat-run"]
    assert actor.agent_profile_id == tenant.ids["profile"]


async def test_agent_actor_refuses_a_run_that_does_not_exist(
    tenant: Tenant, service: WorkflowService
) -> None:
    with pytest.raises(WorkflowError, match="the run does not exist"):
        await service.agent_actor(
            org_id=tenant.org,
            workspace_id=tenant.ids["ws"],
            session_id=tenant.ids["chat"],
            run_id=uuid.uuid4(),
            user_id=None,
        )


async def test_agent_actor_refuses_a_run_with_no_person(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == tenant.ids["chat-run"]).values(triggered_by_user_id=None)
        )
    with pytest.raises(WorkflowError, match="needs a person"):
        await service.agent_actor(
            org_id=tenant.org,
            workspace_id=tenant.ids["ws"],
            session_id=tenant.ids["chat"],
            run_id=tenant.ids["chat-run"],
            user_id=None,
        )


async def test_sub_workflows_stop_at_the_depth_limit(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    """A workflow that calls itself runs MAX_DEPTH levels deep, and then it fails."""
    view = await _create(service, tenant, "name: loop\nsteps:\n  - id: again\n    workflow: loop\n")
    top = await service.start(tenant.user(), uid(view["id"]))
    for _ in range(MAX_DEPTH * 6):
        await runner.run_once()
    instances = await service.instances(tenant.user(), uid(view["id"]), limit=50)
    assert sorted(item["depth"] for item in instances) == list(range(MAX_DEPTH + 1))
    assert {item["status"] for item in instances} == {"failed"}
    deepest = next(item for item in instances if item["depth"] == MAX_DEPTH)
    assert f"at most {MAX_DEPTH} levels deep" in deepest["error"]
    shown = await service.instance(tenant.user(), uid(top["id"]))
    assert shown["status"] == "failed"
    async with engine.connect() as connection:
        steps = (
            await connection.execute(select(func.count()).where(WorkflowStep.org_id == tenant.org))
        ).scalar_one()
    assert steps == MAX_DEPTH + 1
