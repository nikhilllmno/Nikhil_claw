"""Sub-agents of a profile with Postgres: the write, the audit entry, and the prompt assembler
(docs/architecture.md, Section 13.1)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, AuditEntry, Run
from sama.orchestrator.queue import ClaimedRun, RunQueue
from sama.prompt import PromptAssembler
from sama.runtime.contract import RuntimeKind
from sama.subagents import (
    SubagentError,
    parse_subagents,
    profile_subagents,
    set_profile_subagents,
)
from tests.orchestrator.conftest import Tenant

SECRET_TEXT = "do-not-log-this-prompt-text"


def raw(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "researcher",
        "description": "Looks things up.",
        "prompt": SECRET_TEXT,
        "toolsets": ["web"],
        "max_turns": 4,
    }
    values.update(changes)
    return values


async def configure(
    engine: AsyncEngine,
    tenant: Tenant,
    *,
    runtime: str = "claude_agent_sdk",
    toolsets: list[str] | None = None,
    group_toolsets: list[str] | None = None,
) -> None:
    values: dict[str, Any] = {
        "runtime": runtime,
        "toolsets": toolsets if toolsets is not None else ["core", "web", "subagents"],
    }
    if group_toolsets is not None:
        values["group_toolsets"] = group_toolsets
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile).where(AgentProfile.id == tenant.ids["profile"]).values(**values)
        )


async def audit(engine: AsyncEngine, tenant: Tenant) -> list[Any]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    select(AuditEntry.action, AuditEntry.details, AuditEntry.target_id).where(
                        AuditEntry.org_id == tenant.org,
                        AuditEntry.action == "profile.subagents_set",
                    )
                )
            ).all()
        )


# -- set and show ------------------------------------------------------------------------------


async def test_a_new_profile_has_no_subagents(engine: AsyncEngine, tenant: Tenant) -> None:
    assert await profile_subagents(engine, tenant.org, tenant.ids["profile"]) == ()


async def test_set_writes_the_column_and_an_audit_entry_with_the_names(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await configure(engine, tenant)
    specs = parse_subagents([raw(), raw(name="writer", toolsets=["core"], model="claude-sonnet-5")])
    profile = tenant.ids["profile"]
    await set_profile_subagents(engine, tenant.org, profile, specs, actor_user_id=tenant.ids["mia"])
    assert await profile_subagents(engine, tenant.org, profile) == specs
    [entry] = await audit(engine, tenant)
    assert entry.details == {"names": ["researcher", "writer"]}
    assert entry.target_id == str(profile)
    assert SECRET_TEXT not in str(entry.details)
    # A new list replaces the old one. An empty list removes all.
    await set_profile_subagents(engine, tenant.org, profile, ())
    assert await profile_subagents(engine, tenant.org, profile) == ()
    assert len(await audit(engine, tenant)) == 2


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ({"runtime": "fake"}, "no native sub-agents"),
        ({"toolsets": ["core", "web"]}, "needs the toolset subagents"),
        ({"toolsets": ["subagents"]}, "does not have the toolset web"),
    ],
)
async def test_set_rejects_a_profile_that_cannot_use_the_specs(
    engine: AsyncEngine, tenant: Tenant, setup: dict[str, Any], message: str
) -> None:
    await configure(engine, tenant, **setup)
    with pytest.raises(SubagentError, match=message) as caught:
        await set_profile_subagents(
            engine, tenant.org, tenant.ids["profile"], parse_subagents([raw()])
        )
    assert SECRET_TEXT not in str(caught.value)
    assert await profile_subagents(engine, tenant.org, tenant.ids["profile"]) == ()
    assert await audit(engine, tenant) == []


async def test_set_rejects_a_profile_that_does_not_exist(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    with pytest.raises(SubagentError, match="does not exist"):
        await set_profile_subagents(engine, tenant.org, uuid.uuid4(), parse_subagents([raw()]))
    with pytest.raises(SubagentError, match="does not exist"):
        await profile_subagents(engine, tenant.org, uuid.uuid4())
    # The profile of another org is the same as no profile.
    with pytest.raises(SubagentError, match="does not exist"):
        await profile_subagents(engine, uuid.uuid4(), tenant.ids["profile"])


# -- the prompt assembler ----------------------------------------------------------------------


async def claimed(engine: AsyncEngine, tenant: Tenant, session: str) -> ClaimedRun:
    queue = RunQueue(engine)
    await queue.enqueue(org_id=tenant.org, session_id=tenant.ids[session], text="hi")
    run = await queue.claim("w-subagents")
    assert run is not None
    return run


async def assemble(engine: AsyncEngine, tenant: Tenant, session: str) -> Any:
    run = await claimed(engine, tenant, session)
    try:
        return await PromptAssembler(engine)(run)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                update(Run).where(Run.id == run.run_id).values(status="cancelled", lease_owner=None)
            )


async def store(engine: AsyncEngine, tenant: Tenant, value: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(subagents=value)
        )


async def test_a_private_run_gets_the_subagents_with_the_tools_of_the_run(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await configure(engine, tenant)
    specs = parse_subagents([raw(toolsets=["web", "core"])])
    await set_profile_subagents(engine, tenant.org, tenant.ids["profile"], specs)
    config = await assemble(engine, tenant, "mine")
    assert config.runtime is RuntimeKind.CLAUDE_AGENT_SDK
    [definition] = config.subagents
    assert (definition.name, definition.max_turns, definition.model) == ("researcher", 4, None)
    assert "web_search" in definition.tools
    assert "Bash" in definition.tools
    assert not {"Agent", "Task", "clarify"} & set(definition.tools)


async def test_a_shared_run_without_the_toolset_in_the_group_set_has_none(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await configure(engine, tenant, group_toolsets=["core", "web"])
    await set_profile_subagents(engine, tenant.org, tenant.ids["profile"], parse_subagents([raw()]))
    assert (await assemble(engine, tenant, "s1")).subagents == ()


async def test_a_shared_run_with_the_toolset_in_the_group_set_is_limited_to_it(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await configure(engine, tenant, group_toolsets=["web", "subagents"])
    await set_profile_subagents(
        engine, tenant.org, tenant.ids["profile"], parse_subagents([raw(toolsets=["web", "core"])])
    )
    [definition] = (await assemble(engine, tenant, "s1")).subagents
    assert definition.tools == ("web_extract", "web_search")


async def test_a_runtime_without_subagents_gets_none(engine: AsyncEngine, tenant: Tenant) -> None:
    await configure(engine, tenant)
    await set_profile_subagents(engine, tenant.org, tenant.ids["profile"], parse_subagents([raw()]))
    await configure(engine, tenant, runtime="fake")
    assert (await assemble(engine, tenant, "mine")).subagents == ()


async def test_a_profile_without_the_toolset_gets_none(engine: AsyncEngine, tenant: Tenant) -> None:
    await configure(engine, tenant)
    await set_profile_subagents(engine, tenant.org, tenant.ids["profile"], parse_subagents([raw()]))
    await configure(engine, tenant, toolsets=["core", "web"])
    assert (await assemble(engine, tenant, "mine")).subagents == ()


async def test_a_bad_column_gives_no_subagents_and_a_warning(
    engine: AsyncEngine, tenant: Tenant, caplog: pytest.LogCaptureFixture
) -> None:
    await configure(engine, tenant)
    await store(engine, tenant, [{"name": "Bad Name", "description": "d", "prompt": SECRET_TEXT}])
    with caplog.at_level("WARNING", logger="sama.prompt.assemble"):
        config = await assemble(engine, tenant, "mine")
    assert config.subagents == ()
    assert "are not valid" in caplog.text
    assert SECRET_TEXT not in caplog.text
