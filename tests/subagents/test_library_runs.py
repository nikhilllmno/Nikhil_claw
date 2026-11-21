"""Library agents in runs (docs/architecture.md, Section 13.3, M7.4): a profile names library
agents, the config of a session freezes them, a member's personal agents apply to the member's
private sessions, ``delegate_task(agent=...)`` starts a library agent as a child, and a session
that runs as a library agent keeps its toolsets, limits, and model."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, Membership, Run, Session, Workspace
from sama.delegation import ChildTask, DelegationError, DelegationService, render_agents
from sama.gate.decide import run_limit
from sama.orchestrator.queue import RunQueue
from sama.profiles import ProfileChanges, ProfileError, update_profile
from sama.prompt import PromptAssembler
from sama.subagent_library import SubagentLibrary, parse_spec
from sama.tenancy import RunTokenClaims
from sama.tenancy.access import Grants, load_grants
from tests.orchestrator.conftest import Tenant
from tests.subagents.test_store import assemble, configure

PROMPT = "You research with care."


async def mia_grants(engine: AsyncEngine, tenant: Tenant) -> Grants:
    async with engine.connect() as connection:
        return await load_grants(connection, tenant.org, tenant.ids["mia"])


async def make_admin(engine: AsyncEngine, tenant: Tenant) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(
                Membership.org_id == tenant.org,
                Membership.user_id == tenant.ids["mia"],
                Membership.workspace_id.is_(None),
            )
            .values(role="admin")
        )


async def add_agent(
    engine: AsyncEngine,
    tenant: Tenant,
    scope: str,
    name: str,
    *,
    workspace_id: uuid.UUID | None = None,
    **fields: Any,
) -> uuid.UUID:
    values: dict[str, Any] = {
        "name": name,
        "description": f"The {name} agent.",
        "prompt": PROMPT,
        "toolsets": ["web"],
    }
    values.update(fields)
    return await SubagentLibrary(engine).create(
        tenant.org,
        await mia_grants(engine, tenant),
        scope=scope,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        spec=parse_spec(values),
    )


async def use(engine: AsyncEngine, tenant: Tenant, *ids: uuid.UUID) -> None:
    await update_profile(
        engine,
        tenant.org,
        tenant.ids["profile"],
        ProfileChanges(library_subagents=[str(i) for i in ids]),
        actor_user_id=tenant.ids["mia"],
    )


@pytest.fixture
async def claude(engine: AsyncEngine, tenant: Tenant) -> None:
    await configure(engine, tenant)
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(model="claude-sonnet-5-5")
        )
    await make_admin(engine, tenant)


# -- the profile -------------------------------------------------------------------------------


@pytest.mark.usefixtures("claude")
async def test_a_profile_uses_library_agents_and_the_config_freezes_them(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    org_agent = await add_agent(engine, tenant, "org", "researcher", max_turns=3)
    await use(engine, tenant, org_agent)
    config = await assemble(engine, tenant, "s1")  # a shared session
    assert config.subagents == ()  # the group set has no subagents toolset
    config = await assemble(engine, tenant, "mine")
    [definition] = config.subagents
    assert (definition.name, definition.prompt, definition.max_turns) == (
        "researcher",
        PROMPT,
        3,
    )
    assert "web_search" in definition.tools
    # A change of the agent applies from the next session: the frozen config stays.
    await SubagentLibrary(engine).update(
        tenant.org, await mia_grants(engine, tenant), org_agent, {"prompt": "New text."}
    )
    again = await assemble(engine, tenant, "mine")
    assert again.subagents[0].prompt == PROMPT


@pytest.mark.usefixtures("claude")
async def test_the_personal_agents_of_a_member_apply_to_the_private_sessions_of_the_member(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await add_agent(engine, tenant, "user", "my-helper", toolsets=["web", "memory"])
    [definition] = (await assemble(engine, tenant, "mine")).subagents
    assert definition.name == "my-helper"
    # The toolset memory is not in the run, so the sub-agent does not get it.
    assert "memory" not in definition.tools


@pytest.mark.usefixtures("claude")
async def test_a_workspace_agent_of_another_workspace_is_left_out(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    other = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Workspace).values(id=other, org_id=tenant.org, name="w2", slug="w2")
        )
    agent = await add_agent(engine, tenant, "workspace", "elsewhere", workspace_id=other)
    await use(engine, tenant, agent)  # the profile is of the whole org
    assert (await assemble(engine, tenant, "mine")).subagents == ()


@pytest.mark.usefixtures("claude")
async def test_the_profile_refuses_bad_library_references(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    first = await add_agent(engine, tenant, "org", "researcher")
    with pytest.raises(ProfileError, match="does not exist"):
        await use(engine, tenant, uuid.uuid4())
    with pytest.raises(ProfileError, match="two times"):
        await use(engine, tenant, first, first)
    # A name of a library agent must not be the name of an inline sub-agent.
    await update_profile(
        engine,
        tenant.org,
        tenant.ids["profile"],
        ProfileChanges(
            subagents=[{"name": "researcher", "description": "d", "prompt": "p"}],
        ),
        actor_user_id=tenant.ids["mia"],
    )
    with pytest.raises(ProfileError, match="same name"):
        await use(engine, tenant, first)
    # A profile without the toolset subagents cannot use library agents.
    await update_profile(
        engine,
        tenant.org,
        tenant.ids["profile"],
        ProfileChanges(subagents=[], toolsets=["core", "web"]),
        actor_user_id=tenant.ids["mia"],
    )
    with pytest.raises(ProfileError, match="needs the toolset subagents"):
        await use(engine, tenant, first)


# -- a session that runs as a library agent ----------------------------------------------------


def claims(tenant: Tenant, session_id: uuid.UUID, run_id: uuid.UUID) -> RunTokenClaims:
    return RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(session_id),
        run_id=str(run_id),
        token_id="t-1",
        expires_at=2_000_000_000,
    )


@pytest.mark.usefixtures("claude")
async def test_delegate_task_starts_a_library_agent_with_its_limits_tools_and_prompt(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await add_agent(
        engine,
        tenant,
        "org",
        "researcher",
        toolsets=["web", "core"],
        max_turns=7,
        max_cost_usd="0.40",
        model="claude-haiku-5-5",
    )
    queue = RunQueue(engine)
    run_id = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["mine"], text="hi", user_id=tenant.ids["mia"]
    )
    parent = await queue.claim("w-library")
    assert parent is not None
    assert parent.run_id == run_id
    service = DelegationService(engine, queue, poll_s=0.01)
    me = claims(tenant, tenant.ids["mine"], run_id)
    assert "researcher (org)" in render_agents(await service.agents(me))
    with pytest.raises(DelegationError, match="no library agent"):
        await service.start(me, [ChildTask(goal="Look", agent="nobody")])
    [child] = await service.start(
        me, [ChildTask(goal="Find the facts", agent="researcher", max_cost_usd=Decimal("5"))]
    )
    async with engine.connect() as connection:
        session = (
            await connection.execute(select(Session).where(Session.id == child.session_id))
        ).one()
        run = (await connection.execute(select(Run).where(Run.id == child.run_id))).one()
    assert session.library_agent["name"] == "researcher"
    assert session.library_agent["prompt"] == PROMPT
    assert session.title.startswith("researcher: ")
    # The limits of the agent: the task cannot ask for more cost than the agent allows.
    assert run.budget["max_turns"] == 7
    assert run.budget["max_cost_usd"] == "0.4"
    assert run.model == "claude-haiku-5-5"
    assert sorted(run_limit(run.toolsets) or []) == ["core", "web"]
    # The config: the prompt of the agent is the soul, and it has no sub-agents.
    claimed = await queue.claim("w-library")
    assert claimed is not None
    assert claimed.run_id == child.run_id
    config = await PromptAssembler(engine)(claimed)
    assert PROMPT in config.soul
    assert config.subagents == ()
    async with engine.connect() as connection:
        snapshot = (
            await connection.execute(
                select(Session.profile_snapshot).where(Session.id == child.session_id)
            )
        ).scalar_one()
    assert sorted(snapshot["toolsets"]) == ["core", "web"]
    # A later run of the child (a steer) keeps the limits of the agent.
    steer = await service.steer(me, child.session_id, "Also check the dates.")
    async with engine.connect() as connection:
        later = (await connection.execute(select(Run).where(Run.id == steer))).one()
    assert later.budget["max_turns"] == 7
    assert sorted(run_limit(later.toolsets) or []) == ["core", "web"]
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id.in_([run_id, child.run_id, steer]))
            .values(status="cancelled", lease_owner=None)
        )


@pytest.mark.usefixtures("claude")
async def test_a_name_with_spaces_still_starts_the_library_agent(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await add_agent(engine, tenant, "org", "researcher", max_turns=4)
    queue = RunQueue(engine)
    run_id = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["mine"], text="hi", user_id=tenant.ids["mia"]
    )
    assert (await queue.claim("w-library")) is not None
    service = DelegationService(engine, queue, poll_s=0.01)
    [child] = await service.start(
        claims(tenant, tenant.ids["mine"], run_id),
        [ChildTask(goal="Look", agent="  researcher ")],
    )
    async with engine.begin() as connection:
        agent = (
            await connection.execute(
                select(Session.library_agent).where(Session.id == child.session_id)
            )
        ).scalar_one()
        await connection.execute(
            update(Run)
            .where(Run.id.in_([run_id, child.run_id]))
            .values(status="cancelled", lease_owner=None)
        )
    assert agent is not None
    assert agent["name"] == "researcher"


@pytest.mark.usefixtures("claude")
async def test_a_delete_waits_for_a_profile_write_that_adds_the_agent(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    import asyncio

    from sama.profiles import check_library
    from sama.subagent_library import LibraryConflictError
    from sama.subagents import lock_profile

    agent = await add_agent(engine, tenant, "org", "researcher")
    library = SubagentLibrary(engine)
    grants = await mia_grants(engine, tenant)
    async with engine.connect() as writer:
        await writer.begin()
        profile = await lock_profile(writer, tenant.org, tenant.ids["profile"])
        values = {"library_subagents": [str(agent)]}
        await check_library(writer, tenant.org, profile, values, tenant.ids["mia"])
        await writer.execute(
            update(AgentProfile).where(AgentProfile.id == tenant.ids["profile"]).values(**values)
        )
        # The delete waits for the share lock of the profile write.
        delete = asyncio.create_task(library.delete(tenant.org, grants, agent))
        await asyncio.sleep(0.5)
        assert not delete.done()
        await writer.commit()
    with pytest.raises(LibraryConflictError):
        await delete


async def test_a_runtime_without_subagents_cannot_run_a_library_agent(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    # The fake runtime has no native sub-agents, as a coding agent CLI runtime.
    await make_admin(engine, tenant)
    await add_agent(engine, tenant, "org", "researcher")
    queue = RunQueue(engine)
    run_id = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["mine"], text="hi", user_id=tenant.ids["mia"]
    )
    assert (await queue.claim("w-library")) is not None
    service = DelegationService(engine, queue, poll_s=0.01)
    with pytest.raises(DelegationError, match="cannot run a library agent"):
        await service.start(
            claims(tenant, tenant.ids["mine"], run_id), [ChildTask(goal="Look", agent="researcher")]
        )
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == run_id).values(status="cancelled", lease_owner=None)
        )
