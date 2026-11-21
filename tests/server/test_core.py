"""The shared operations of sama as a server: profiles, skills, and workflows (Section 10.7)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.apikeys import ApiKeyStore, KeyContext
from sama.db.models import AgentProfile, Session
from sama.orchestrator.queue import RunQueue
from sama.scheduler.jobs import Actor
from sama.server import ServerCore, ServerError
from sama.server.core import Profile, model_ids
from sama.skills import BuiltinSkills
from sama.skills.service import SkillLibrary
from sama.workflows.service import WorkflowService
from tests.orchestrator.conftest import Tenant
from tests.server.conftest import FAST, Keys
from tests.workflows.conftest import FakeTools

pytestmark = pytest.mark.usefixtures("admin")

READ = """
name: reader
inputs:
  topic: { default: tea }
steps:
  - id: look
    tool: notes_read
    args: { topic: "{{inputs.topic}}" }
"""


async def context(engine: AsyncEngine, keys: Keys, **options: object) -> KeyContext:
    key = await keys.make(**options)  # type: ignore[arg-type]
    return await ApiKeyStore(engine).verify(key, "mcp")


def test_a_name_that_two_profiles_have_uses_the_id() -> None:
    one, two, three = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    profiles = model_ids(
        [
            Profile(one, "Helper", "m", "fake", 0),
            Profile(two, "helper", "m", "fake", 0),
            Profile(three, "Coder", "m", "fake", 0),
        ]
    )
    assert [profile.model_id for profile in profiles] == [str(one), str(two), "Coder"]


async def test_a_key_with_a_profile_sees_only_that_profile(
    engine: AsyncEngine, keys: Keys, admin: Tenant
) -> None:
    other = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(AgentProfile).values(
                id=other, org_id=admin.org, name="second", runtime="fake", model="fake-model"
            )
        )
    core = ServerCore(engine, settings=FAST)
    free = await context(engine, keys)
    assert [profile.model_id for profile in await core.profiles(free)] == ["p", "second"]
    assert (await core.resolve(free, "SECOND")).id == other
    assert (await core.resolve(free, str(other))).id == other
    assert (await core.resolve(free, None)).id == admin.ids["profile"]
    bound = await context(engine, keys, profile=True)
    assert [profile.model_id for profile in await core.profiles(bound)] == ["p"]
    with pytest.raises(ServerError) as error:
        await core.resolve(bound, "second")
    assert error.value.status == 404


async def test_skills_list_the_skills_of_the_workspace(engine: AsyncEngine, keys: Keys) -> None:
    core = ServerCore(engine, skills=SkillLibrary(engine, BuiltinSkills().all()), settings=FAST)
    skills = await core.skills(await context(engine, keys))
    assert skills
    assert all({"name", "description", "scope"} <= set(skill) for skill in skills)


async def test_a_workflow_of_the_key_user_starts_and_shows(
    engine: AsyncEngine, keys: Keys, admin: Tenant
) -> None:
    tools = FakeTools()
    workflows = WorkflowService(engine, RunQueue(engine), tools.access)
    actor = Actor(org_id=admin.org, user_id=admin.ids["mia"], kind="user")
    view = await workflows.create(actor, READ, workspace_id=admin.ids["ws"])
    core = ServerCore(engine, workflows=workflows, settings=FAST)
    key = await context(engine, keys)
    listed = await core.workflows(key)
    assert [row["name"] for row in listed] == ["reader"]
    instance = await core.start_workflow(key, uuid.UUID(view["id"]), {"topic": "coffee"})
    shown = await core.workflow_instance(key, uuid.UUID(instance["id"]))
    assert shown["workflow_id"] == view["id"]
    with pytest.raises(ServerError) as error:
        await core.start_workflow(key, uuid.uuid4(), {})
    assert error.value.status == 404


async def test_a_profile_bound_key_searches_only_its_profile(
    engine: AsyncEngine, keys: Keys, admin: Tenant
) -> None:
    other = uuid.uuid4()
    hidden = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(AgentProfile).values(
                id=other, org_id=admin.org, name="second", runtime="fake", model="fake-model"
            )
        )
        await connection.execute(
            insert(Session).values(
                id=hidden,
                org_id=admin.org,
                workspace_id=admin.ids["ws"],
                agent_profile_id=other,
                audience="shared",
                title="secret plans",
            )
        )
    core = ServerCore(engine, settings=FAST)
    free = {hit["session_id"] for hit in await core.search(await context(engine, keys), "", 10)}
    assert str(hidden) in free
    bound = await context(engine, keys, profile=True)
    found = {hit["session_id"] for hit in await core.search(bound, "", 10)}
    assert found
    assert str(hidden) not in found


async def test_a_profile_bound_key_cannot_use_workflows(engine: AsyncEngine, keys: Keys) -> None:
    tools = FakeTools()
    core = ServerCore(
        engine, workflows=WorkflowService(engine, RunQueue(engine), tools.access), settings=FAST
    )
    bound = await context(engine, keys, profile=True)
    for call in (
        core.workflows(bound),
        core.start_workflow(bound, uuid.uuid4(), {}),
        core.workflow_instance(bound, uuid.uuid4()),
    ):
        with pytest.raises(ServerError) as error:
            await call
        assert error.value.status == 403
