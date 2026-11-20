"""The tool gate for the run of an agent step of a workflow, with Postgres (M4.5).

The run of an agent step cannot create, change, or start workflows or jobs. Its pre-approvals
apply only when the owner approved the exact version of the workflow (Section 12.3).
"""

# ruff: noqa: F811 -- the fixtures of the tests are imported, and a test names them as arguments
from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import WorkflowVersion
from sama.gate.decide import (
    CHILD_BLOCKED_TOOLSETS,
    DEFAULT_ALLOW,
    SHARED_ASK,
    TOOLSETS,
    WORKFLOW_TOOLSET,
)
from sama.gate.service import GateService
from sama.scheduler.spec import NO_PREAPPROVAL
from sama.tenancy import RunTokenClaims
from sama.workflows.engine import WorkflowEngine
from sama.workflows.service import WorkflowService
from sama.workflows.spec import WorkflowError
from sama.workflows.store import workflow_of_run
from tests.workflows.conftest import (  # noqa: F401
    Tenant,
    engine,
    runner,
    service,
    tenant,
    tools,
    workflow_database_url,
)

REPORT = {"command": "make report"}
OTHER = {"command": "rm -rf /"}

PLAIN = """
name: thinker
steps:
  - id: think
    agent: { prompt: "Think." }
"""

TOOLED = """
name: reporter
steps:
  - id: think
    agent:
      prompt: "Make the report."
      preapproved_tools:
        - tool: Bash
          limits: { command: ["make report"] }
"""

WORKFLOW_TOOLS = ("mcp__sama__workflow_manage", "mcp__sama__workflow_run")
JOB_TOOL = "mcp__sama__cronjob_manage"


def uid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


def claims_of(tenant: Tenant, session_id: Any, run_id: Any) -> RunTokenClaims:
    return RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(session_id),
        run_id=str(run_id),
        token_id="t-workflow",
        expires_at=2_000_000_000,
    )


async def agent_step_claims(
    tenant: Tenant, service: WorkflowService, runner: WorkflowEngine, source: str, *, approve: bool
) -> tuple[RunTokenClaims, uuid.UUID, uuid.UUID]:
    """The run token claims of the run of the agent step, the workflow id, and the version."""
    view = await service.create(tenant.user(), source)
    if approve:
        view = await service.approve(tenant.user(), uid(view["id"]), view["version"])
    instance = await service.start(tenant.user(), uid(view["id"]))
    await runner.run_once()
    await runner.run_once()
    [step] = (await service.instance(tenant.user(), uid(instance["id"])))["steps"]
    assert step["run_id"] is not None
    claims = claims_of(tenant, step["session_id"], step["run_id"])
    return claims, uid(view["id"]), uid(instance["id"])


def chat_claims(tenant: Tenant) -> RunTokenClaims:
    return claims_of(tenant, tenant.ids["chat"], tenant.ids["chat-run"])


# -- the toolset names ------------------------------------------------------------------------


def test_the_workflows_toolset_has_the_two_workflow_tools() -> None:
    assert WORKFLOW_TOOLSET == "workflows"
    assert TOOLSETS[WORKFLOW_TOOLSET] == frozenset({"workflow_manage", "workflow_run"})


def test_the_workflow_tools_follow_the_rules_of_the_job_tool() -> None:
    for name in ("workflow_manage", "workflow_run"):
        assert name in DEFAULT_ALLOW
        assert name in SHARED_ASK
        assert name in NO_PREAPPROVAL
    assert "cronjob_manage" in NO_PREAPPROVAL


def test_a_delegated_child_does_not_have_the_workflows_toolset() -> None:
    assert WORKFLOW_TOOLSET in CHILD_BLOCKED_TOOLSETS
    assert "cron" in CHILD_BLOCKED_TOOLSETS


# -- the run of an agent step -----------------------------------------------------------------


async def test_a_normal_chat_run_has_the_workflow_and_job_tools(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    gate = GateService(engine)
    claims = chat_claims(tenant)
    for name in (*WORKFLOW_TOOLS, JOB_TOOL):
        assert (await gate.check(claims, f"c-{name}", name, {})).action == "allow"


async def test_the_run_of_an_agent_step_loses_the_workflow_and_job_tools(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    claims, _, _ = await agent_step_claims(tenant, service, runner, PLAIN, approve=False)
    gate = GateService(engine)
    for name in (*WORKFLOW_TOOLS, JOB_TOOL):
        verdict = await gate.check(claims, f"c-{name}", name, {})
        assert verdict.action == "deny", name
    # The screen of the sama MCP server gives the same answer.
    for name in (*WORKFLOW_TOOLS, JOB_TOOL):
        assert (await gate.screen(claims, name, {})).action == "deny", name


async def test_the_run_of_an_agent_step_keeps_the_other_tools(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    claims, _, _ = await agent_step_claims(tenant, service, runner, PLAIN, approve=False)
    gate = GateService(engine)
    assert (await gate.check(claims, "c-todo", "TodoWrite", {})).action == "allow"


async def test_the_run_of_an_agent_step_without_pre_approvals_cannot_write(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    claims, _, _ = await agent_step_claims(tenant, service, runner, PLAIN, approve=False)
    verdict = await GateService(engine).check(claims, "c-bash", "Bash", REPORT)
    assert verdict.action == "deny"


# -- the pre-approvals ------------------------------------------------------------------------


async def test_the_pre_approvals_of_an_approved_version_apply_to_its_step(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    claims, _, _ = await agent_step_claims(tenant, service, runner, TOOLED, approve=True)
    gate = GateService(engine)
    assert (await gate.check(claims, "c1", "Bash", REPORT)).action == "allow"
    # Only the pre-approved limits apply.
    assert (await gate.check(claims, "c2", "Bash", OTHER)).action == "deny"
    assert (await gate.check(claims, "c3", "Write", {"file_path": "x"})).action == "deny"
    # An approved version still cannot start workflows.
    for name in (*WORKFLOW_TOOLS, JOB_TOOL):
        assert (await gate.check(claims, f"c-{name}", name, {})).action == "deny"


async def test_the_pre_approvals_do_not_apply_to_a_changed_definition(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    claims, workflow_id, _ = await agent_step_claims(tenant, service, runner, TOOLED, approve=True)
    gate = GateService(engine)
    assert (await gate.check(claims, "c1", "Bash", REPORT)).action == "allow"
    # Somebody changes the stored hash after the approval. The approval no longer matches.
    async with engine.begin() as connection:
        await connection.execute(
            update(WorkflowVersion)
            .where(
                WorkflowVersion.org_id == tenant.org,
                WorkflowVersion.workflow_id == workflow_id,
            )
            .values(approved_hash="0" * 64)
        )
    assert (await gate.check(claims, "c2", "Bash", REPORT)).action == "deny"


async def test_the_pre_approvals_do_not_apply_without_an_approved_hash(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    claims, workflow_id, _ = await agent_step_claims(tenant, service, runner, TOOLED, approve=True)
    async with engine.begin() as connection:
        await connection.execute(
            update(WorkflowVersion)
            .where(
                WorkflowVersion.org_id == tenant.org,
                WorkflowVersion.workflow_id == workflow_id,
            )
            .values(approved_hash=None, approved_by_user_id=None, approved_at=None)
        )
    assert (await GateService(engine).check(claims, "c1", "Bash", REPORT)).action == "deny"


# -- the store --------------------------------------------------------------------------------


async def test_workflow_of_run_names_the_instance_and_step(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    claims, _, instance_id = await agent_step_claims(tenant, service, runner, TOOLED, approve=True)
    async with engine.connect() as connection:
        found = await workflow_of_run(connection, tenant.org, uid(claims.run_id))
        none = await workflow_of_run(connection, tenant.org, tenant.ids["chat-run"])
    assert found is not None
    assert (found.instance_id, found.step_id) == (instance_id, "think")
    assert [item.tool for item in found.preapproved] == ["Bash"]
    assert none is None


async def test_a_version_that_waits_for_approval_does_not_start(
    tenant: Tenant, service: WorkflowService
) -> None:
    view = await service.create(tenant.user(), TOOLED)
    assert view["needs_approval"] is True
    with pytest.raises(WorkflowError):
        await service.start(tenant.user(), uid(view["id"]))
