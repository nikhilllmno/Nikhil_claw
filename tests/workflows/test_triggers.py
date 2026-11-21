"""Triggers with the target ``workflow`` (docs/architecture.md, Sections 12.2 and 12.3): each
event starts one instance, with the toolset limit of the trigger and the event as an input."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import (
    Membership,
    Org,
    Run,
    Trigger,
    TriggerEvent,
    User,
    WorkflowInstance,
)
from sama.orchestrator.queue import RunQueue
from sama.tenancy.access import load_grants
from sama.triggers import signature
from sama.triggers.service import (
    NO_WORKFLOWS,
    WORKFLOW_NOT_FOUND,
    WORKFLOW_OWNER,
    NewTrigger,
    TriggerError,
    TriggerService,
)
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import Vault
from sama.workflows.engine import WorkflowEngine
from sama.workflows.service import EVENT_INPUT_MAX_BYTES, WorkflowService
from sama.workflows.spec import MAX_INPUTS_BYTES
from tests.workflows.conftest import Tenant

AGENT = """
name: triage
inputs:
  event: { default: "" }
steps:
  - id: look
    agent: { prompt: "Triage this event: {{inputs.event}}" }
"""

NO_EVENT = """
name: tick
steps:
  - id: wait
    wait: { seconds: 60 }
"""


@pytest.fixture
def vault(engine: AsyncEngine) -> Vault:
    return Vault(engine, VaultKey(b"t" * KEY_BYTES))


@pytest.fixture
def triggers(engine: AsyncEngine, vault: Vault, service: WorkflowService) -> TriggerService:
    return TriggerService(engine, queue=RunQueue(engine), vault=vault, workflows=service)


async def _grants(engine: AsyncEngine, tenant: Tenant, user: str = "ana") -> Any:
    async with engine.connect() as connection:
        return await load_grants(connection, tenant.org, tenant.ids[user])


async def _workflow(service: WorkflowService, tenant: Tenant, source: str) -> dict[str, Any]:
    view = await service.create(tenant.user(), source)
    if view["needs_approval"]:
        view = await service.approve(tenant.user(), uuid.UUID(view["id"]), view["version"])
    return view


async def _hook(
    engine: AsyncEngine,
    tenant: Tenant,
    triggers: TriggerService,
    workflow_id: str,
    **changes: Any,
) -> tuple[dict[str, Any], bytes]:
    fields: dict[str, Any] = {
        "kind": "webhook",
        "name": "Tickets",
        "workflow_id": uuid.UUID(workflow_id),
        "toolsets": ["web"],
    }
    fields.update(changes)
    created = await triggers.create(
        tenant.org, tenant.ids["ws"], await _grants(engine, tenant), NewTrigger(**fields)
    )
    return created, signature.standard_key(created["secret"])


async def _deliver(
    triggers: TriggerService, trigger: dict[str, Any], key: bytes, payload: Any, delivery: str
) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    ts = int(time.time())
    headers = {
        "webhook-id": delivery,
        "webhook-timestamp": str(ts),
        "webhook-signature": signature.sign(key, delivery, ts, body),
    }
    return await triggers.webhook(uuid.UUID(trigger["id"]), headers, body)


async def _instances(engine: AsyncEngine, workflow_id: str) -> list[Any]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    select(WorkflowInstance).where(
                        WorkflowInstance.workflow_id == uuid.UUID(workflow_id)
                    )
                )
            ).all()
        )


async def test_an_event_starts_one_instance_with_the_limit_of_the_trigger(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    triggers: TriggerService,
) -> None:
    workflow = await _workflow(service, tenant, AGENT)
    trigger, key = await _hook(engine, tenant, triggers, workflow["id"])
    assert trigger["target"] == "workflow"
    assert trigger["workflow_id"] == workflow["id"]
    assert trigger["prompt"] == "Start the workflow triage."
    assert trigger["agent_profile_id"] == str(tenant.ids["profile"])

    answer = await _deliver(triggers, trigger, key, {"ticket": 7}, "msg_1")
    assert answer["accepted"] is True
    again = await _deliver(triggers, trigger, key, {"ticket": 7}, "msg_1")
    assert again == {"duplicate": True}

    [instance] = await _instances(engine, workflow["id"])
    assert instance.trigger == "trigger"
    assert instance.toolsets == ["web"]
    assert instance.inputs == {"event": '{"ticket":7}'}
    async with engine.connect() as connection:
        event = (
            await connection.execute(
                select(TriggerEvent).where(TriggerEvent.trigger_id == uuid.UUID(trigger["id"]))
            )
        ).one()
    assert event.workflow_instance_id == instance.id
    assert event.session_id is None
    assert event.run_id is None

    # The agent step gets the toolsets of the trigger as its limit.
    for _ in range(3):
        await runner.run_once()
    async with engine.connect() as connection:
        run = (
            await connection.execute(
                select(Run).where(Run.org_id == tenant.org, Run.unattended.is_(True))
            )
        ).one()
    assert run.toolsets == ["web"]


async def test_the_event_is_an_input_only_when_the_workflow_defines_it(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, triggers: TriggerService
) -> None:
    workflow = await _workflow(service, tenant, NO_EVENT)
    trigger, key = await _hook(engine, tenant, triggers, workflow["id"])
    assert (await _deliver(triggers, trigger, key, {"a": 1}, "msg_1"))["accepted"] is True
    [instance] = await _instances(engine, workflow["id"])
    assert instance.inputs == {}


async def test_a_large_event_is_cut_to_fit_the_inputs(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, triggers: TriggerService
) -> None:
    workflow = await _workflow(service, tenant, AGENT)
    trigger, key = await _hook(engine, tenant, triggers, workflow["id"])
    payload = {"text": '"' * 15_000}
    assert (await _deliver(triggers, trigger, key, payload, "msg_1"))["accepted"] is True
    [instance] = await _instances(engine, workflow["id"])
    assert instance.inputs["event"].endswith("the rest is not shown]")
    assert len(instance.inputs["event"].encode()) < EVENT_INPUT_MAX_BYTES + 100
    assert len(json.dumps(instance.inputs, ensure_ascii=False).encode()) <= MAX_INPUTS_BYTES


async def test_a_paused_workflow_refuses_the_event_and_the_trigger_shows_why(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, triggers: TriggerService
) -> None:
    workflow = await _workflow(service, tenant, NO_EVENT)
    trigger, key = await _hook(engine, tenant, triggers, workflow["id"])
    await service.pause(tenant.user(), uuid.UUID(workflow["id"]))
    with pytest.raises(TriggerError) as error:
        await _deliver(triggers, trigger, key, {}, "msg_1")
    assert error.value.status_code == 409
    assert await _instances(engine, workflow["id"]) == []
    async with engine.connect() as connection:
        row = (
            await connection.execute(select(Trigger).where(Trigger.id == uuid.UUID(trigger["id"])))
        ).one()
        events = (
            await connection.execute(select(TriggerEvent).where(TriggerEvent.trigger_id == row.id))
        ).all()
    assert row.last_error == "workflow tick is paused"
    assert events == []

    # After a resume, the sender's retry of the same delivery starts the workflow.
    await service.resume(tenant.user(), uuid.UUID(workflow["id"]))
    assert (await _deliver(triggers, trigger, key, {}, "msg_1"))["accepted"] is True
    assert len(await _instances(engine, workflow["id"])) == 1


async def test_a_trigger_cannot_start_the_workflow_of_another_user(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, triggers: TriggerService
) -> None:
    workflow = await _workflow(service, tenant, NO_EVENT)
    ben = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=ben,
                org_id=tenant.org,
                auth_user_id=uuid.uuid4(),
                email="ben@example.test",
                display_name="ben",
            )
        )
        for workspace_id in (None, tenant.ids["ws"]):
            await connection.execute(
                insert(Membership).values(
                    org_id=tenant.org, user_id=ben, workspace_id=workspace_id, role="member"
                )
            )
    tenant.ids["ben"] = ben
    fields = NewTrigger(kind="webhook", name="x", workflow_id=uuid.UUID(workflow["id"]))
    with pytest.raises(TriggerError) as error:
        await triggers.create(
            tenant.org, tenant.ids["ws"], await _grants(engine, tenant, "ben"), fields
        )
    assert (error.value.status_code, str(error.value)) == (403, WORKFLOW_OWNER)


async def test_the_fields_of_a_workflow_trigger_are_checked(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    vault: Vault,
    triggers: TriggerService,
) -> None:
    grants = await _grants(engine, tenant)
    workflow = await _workflow(service, tenant, NO_EVENT)
    workflow_id = uuid.UUID(workflow["id"])

    async def refused(fields: NewTrigger, service: TriggerService = triggers) -> tuple[int, str]:
        with pytest.raises(TriggerError) as error:
            await service.create(tenant.org, tenant.ids["ws"], grants, fields)
        return error.value.status_code, str(error.value)

    assert await refused(NewTrigger(kind="webhook", name="x", workflow_id=uuid.uuid4())) == (
        404,
        WORKFLOW_NOT_FOUND,
    )
    other_profile = await refused(
        NewTrigger(kind="webhook", name="x", workflow_id=workflow_id, agent_profile_id=uuid.uuid4())
    )
    assert other_profile[0] == 422
    assert (await refused(NewTrigger(kind="webhook", name="x")))[0] == 422
    assert (
        await refused(
            NewTrigger(kind="webhook", name="x", workflow_id=workflow_id, toolsets=["workflows"])
        )
    )[0] == 422
    off = TriggerService(engine, queue=RunQueue(engine), vault=vault)
    assert await refused(NewTrigger(kind="webhook", name="x", workflow_id=workflow_id), off) == (
        409,
        NO_WORKFLOWS,
    )
    await service.remove(tenant.user(), workflow_id)
    assert (await refused(NewTrigger(kind="webhook", name="x", workflow_id=workflow_id)))[0] == 404


async def test_a_workflow_event_over_the_message_rate_gets_429_and_starts_nothing(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, triggers: TriggerService
) -> None:
    """A workflow start from a trigger counts toward the message rate of the owner, like a
    chat run, so a sender cannot start workflows without a limit."""
    workflow = await _workflow(service, tenant, NO_EVENT)
    trigger, key = await _hook(engine, tenant, triggers, workflow["id"])
    async with engine.begin() as connection:
        await connection.execute(
            update(Org).where(Org.id == tenant.org).values(limits={"user_messages_per_minute": 2})
        )
    # The chat run of the tenant is the first message, and this event is the second.
    assert (await _deliver(triggers, trigger, key, {"n": 1}, "msg_1"))["accepted"] is True
    with pytest.raises(TriggerError) as error:
        await _deliver(triggers, trigger, key, {"n": 2}, "msg_late")
    assert error.value.status_code == 429
    assert int(error.value.headers["Retry-After"]) >= 1
    assert len(await _instances(engine, workflow["id"])) == 1
    async with engine.connect() as connection:
        late = await connection.scalar(
            select(func.count()).where(TriggerEvent.delivery_id == "msg_late")
        )
    assert late == 0
