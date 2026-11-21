"""The tools ``workflow_manage`` and ``workflow_run`` (docs/architecture.md, Section 12.3).

The agent uses them to write, change, and start the workflows of the run user. The gate gives
both tools ``ask`` in a shared session. The run of an agent step does not have them (the gate
removes the toolset ``workflows``), and ``WorkflowService`` refuses each change from such a run
as a second check. An agent cannot approve a workflow or decide a step: a person does that in
the API or the web app.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from sama.scheduler.jobs import Actor
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.board import Call
from sama.tools.output import untrusted
from sama.workflows.service import WorkflowService
from sama.workflows.spec import WorkflowError

WORKFLOW_MANAGE: Final = "workflow_manage"
WORKFLOW_RUN: Final = "workflow_run"
MAX_YAML_CHARS: Final = 50_000
MAX_OUTPUT_CHARS: Final = 12_000

ManageAction = Literal["validate", "create", "update", "list", "get", "pause", "resume", "remove"]
RunAction = Literal["start", "status", "list", "cancel", "signal"]

MANAGE_DESCRIPTION: Final = (
    "Write and manage durable workflows. A workflow is a saved, versioned YAML definition of "
    "steps that runs again and again with no person in the run. Actions: validate (yaml: check "
    "it and show if it needs an approval, with no save), create (yaml), update (workflow and "
    "yaml: a new version), list, get (workflow: the YAML, the status, and the recent "
    "instances), pause, resume, remove. workflow is the id or the name. The YAML has name, "
    "description, inputs ({name: {default, required, description}}), triggers ([{cron, tz}] or "
    "[{schedule, tz}], optional), and steps. Each step has an id and one of: tool (a connector "
    "tool, with args), agent ({prompt, profile, model, skills, preapproved_tools}), approval "
    "({prompt}), branch ({when: {value, equals|not_equals|contains|empty}, then, else}), "
    "parallel ({branches: {name: [steps]}}), wait ({seconds} or {until} or {event, "
    "timeout_seconds}), workflow ({name, inputs}). Templates: {{inputs.x}} and "
    "{{steps.<id>.output}} (and .output.<key>). Always validate first and show the steps to "
    "the user. A workflow that writes to an outside system, or that pre-approves tools, waits "
    "until its owner approves that version in the web app or the API. Tell the user that."
)

RUN_DESCRIPTION: Final = (
    "Start and follow workflow instances. Actions: start (workflow and optional inputs: start "
    "an instance now), status (instance_id: the steps, outputs, errors, and receipts), list "
    "(the recent instances, optional workflow), cancel (instance_id), signal (instance_id, "
    "event, optional data: wake the wait steps that wait for the event). workflow is the id or "
    "the name. A step that waits for an approval, or a write whose result is not known, waits "
    "for the owner in the web app or the API. Tell the user that."
)


def _json(value: Any) -> str:
    text = json.dumps(value, indent=1, ensure_ascii=False, default=str)
    if len(text) > MAX_OUTPUT_CHARS:
        return text[: MAX_OUTPUT_CHARS - 1] + "…"
    return text


def render_workflow(view: Mapping[str, Any], *, full: bool = False) -> str:
    lines = [
        f"{view['name']} (id {view['id']}, version {view['version']})",
        f"Status: {view['status']}",
    ]
    if view.get("description"):
        lines.append(f"Description: {view['description']}")
    triggers = view.get("triggers") or []
    if triggers:
        lines.append("Triggers: " + "; ".join(str(item["text"]) for item in triggers))
        lines.append(f"Next fire: {view.get('next_fire_at') or 'none'}")
    if view.get("needs_approval"):
        lines.append(
            "Needs the approval of the owner: "
            + "; ".join(view.get("approval_reasons") or [])
            + f". The owner approves version {view['version']} in the web app or with "
            f"POST /orgs/<org>/workflows/{view['id']}/approve. It does not run before that."
        )
    if full:
        if view.get("source"):
            lines += ["YAML:", str(view["source"])]
        instances = view.get("instances") or []
        if instances:
            lines.append("Recent instances:")
            lines += [render_instance_line(item) for item in instances]
    return "\n".join(lines)


def render_workflows(views: list[dict[str, Any]]) -> str:
    if not views:
        return "You have no workflows."
    return "\n\n".join(render_workflow(view) for view in views)


def render_instance_line(view: Mapping[str, Any]) -> str:
    line = f"- {view['id']} {view['workflow']} v{view['version']}: {view['status']}"
    line += f" ({view['trigger']}, {view['created_at']})"
    if view.get("error"):
        line += f" error: {view['error']}"
    return line


def render_instance(view: Mapping[str, Any]) -> str:
    """The instance for the model. Step outputs and receipts can hold data from outside, so
    they are marked as untrusted."""
    lines = [
        f"Instance {view['id']} of {view['workflow']} (version {view['version']})",
        f"Status: {view['status']}" + (f" ({view['error']})" if view.get("error") else ""),
        f"Inputs: {_json(view.get('inputs'))}",
    ]
    waiting = [
        step
        for step in view.get("steps", [])
        if step["status"] in ("waiting_human", "needs_reconcile")
        or (step["kind"] == "approval" and step["status"] == "waiting")
    ]
    if waiting:
        lines.append(
            "Waits for the owner: "
            + ", ".join(f"{step['step_id']} ({step['status']})" for step in waiting)
            + ". The owner decides in the web app or the API."
        )
    details = {
        "output": view.get("output"),
        "steps": [
            {key: value for key, value in step.items() if value is not None}
            for step in view.get("steps", [])
        ],
        "receipts": view.get("receipts", []),
    }
    lines.append(untrusted(_json(details), source=f"workflow instance {view['id']}"))
    return "\n".join(lines)


async def _actor(service: WorkflowService, claims: RunTokenClaims) -> Actor:
    return await service.agent_actor(
        org_id=uuid.UUID(claims.org_id),
        workspace_id=uuid.UUID(claims.workspace_id),
        session_id=uuid.UUID(claims.session_id),
        run_id=uuid.UUID(claims.run_id),
        user_id=uuid.UUID(claims.user_id) if claims.user_id else None,
    )


def _instance_id(value: str | None) -> uuid.UUID:
    if value is None:
        raise WorkflowError("this action needs instance_id")
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        raise WorkflowError("instance_id is not a valid id") from None


async def manage(
    service: WorkflowService,
    claims: RunTokenClaims,
    action: ManageAction,
    *,
    workflow: str | None = None,
    yaml: str | None = None,
) -> str:
    """Runs one ``workflow_manage`` action for the user of the run."""
    if action == "validate":
        if yaml is None:
            raise WorkflowError("validate needs yaml")
        checked = service.validate(yaml)
        steps = [step["id"] for step in checked["definition"]["steps"]]
        lines = [
            f"The workflow {checked['definition']['name']} is valid.",
            "Top-level steps: " + ", ".join(steps),
        ]
        if checked["next_fire_at"]:
            lines.append(f"First fire: {checked['next_fire_at']}")
        if checked["needs_approval"]:
            lines.append(
                "It needs the approval of the owner before its first run: "
                + "; ".join(checked["approval_reasons"])
            )
        return "\n".join(lines)
    actor = await _actor(service, claims)
    if action == "list":
        return render_workflows(await service.list(actor))
    if action == "create":
        if yaml is None:
            raise WorkflowError("create needs yaml")
        return "Created the workflow.\n" + render_workflow(await service.create(actor, yaml))
    if workflow is None:
        raise WorkflowError(f"{action} needs workflow")
    workflow_id = await service.find(actor, workflow)
    if action == "get":
        return render_workflow(await service.get(actor, workflow_id), full=True)
    if action == "update":
        if yaml is None:
            raise WorkflowError("update needs yaml")
        view = await service.update(actor, workflow_id, yaml)
        return "Saved the workflow.\n" + render_workflow(view)
    if action == "pause":
        return "Paused the workflow.\n" + render_workflow(await service.pause(actor, workflow_id))
    if action == "resume":
        view = await service.resume(actor, workflow_id)
        return "Resumed the workflow.\n" + render_workflow(view)
    await service.remove(actor, workflow_id)
    return "Removed the workflow."


async def run(
    service: WorkflowService,
    claims: RunTokenClaims,
    action: RunAction,
    *,
    workflow: str | None = None,
    instance_id: str | None = None,
    inputs: dict[str, Any] | None = None,
    event: str | None = None,
    data: Any = None,
) -> str:
    """Runs one ``workflow_run`` action for the user of the run."""
    actor = await _actor(service, claims)
    if action == "list":
        workflow_id = None if workflow is None else await service.find(actor, workflow)
        rows = await service.instances(actor, workflow_id)
        if not rows:
            return "There are no workflow instances."
        return "\n".join(render_instance_line(row) for row in rows)
    if action == "start":
        if workflow is None:
            raise WorkflowError("start needs workflow")
        workflow_id = await service.find(actor, workflow)
        view = await service.start(actor, workflow_id, inputs or {})
        return "Started the workflow.\n" + render_instance(view)
    target = _instance_id(instance_id)
    if action == "status":
        return render_instance(await service.instance(actor, target))
    if action == "cancel":
        return "Cancelled the instance.\n" + render_instance(await service.cancel(actor, target))
    if not event:
        raise WorkflowError("signal needs event")
    count = await service.signal(actor, target, event, data)
    return f"Sent the event {event}. {count} wait step(s) ended."


def register_workflows(server: MCPServer, call: Call, service: WorkflowService) -> None:
    @server.tool(
        name=WORKFLOW_MANAGE,
        description=MANAGE_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False),
        structured_output=False,
    )
    async def workflow_manage(
        action: ManageAction,
        ctx: Context,
        workflow: Annotated[str | None, Field(min_length=1, max_length=100)] = None,
        yaml: Annotated[str | None, Field(min_length=1, max_length=MAX_YAML_CHARS)] = None,
    ) -> str:
        async def body(claims: RunTokenClaims) -> str:
            return await manage(service, claims, action, workflow=workflow, yaml=yaml)

        arguments = {
            key: value
            for key, value in {"action": action, "workflow": workflow, "yaml": yaml}.items()
            if value is not None
        }
        return await call(ctx, WORKFLOW_MANAGE, arguments, body)

    @server.tool(
        name=WORKFLOW_RUN,
        description=RUN_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False),
        structured_output=False,
    )
    async def workflow_run(
        action: RunAction,
        ctx: Context,
        workflow: Annotated[str | None, Field(min_length=1, max_length=100)] = None,
        instance_id: Annotated[str | None, Field(min_length=32, max_length=36)] = None,
        inputs: dict[str, Any] | None = None,
        event: Annotated[str | None, Field(min_length=1, max_length=100)] = None,
        data: Any = None,
    ) -> str:
        async def body(claims: RunTokenClaims) -> str:
            return await run(
                service,
                claims,
                action,
                workflow=workflow,
                instance_id=instance_id,
                inputs=inputs,
                event=event,
                data=data,
            )

        arguments = {
            key: value
            for key, value in {
                "action": action,
                "workflow": workflow,
                "instance_id": instance_id,
                "inputs": inputs,
                "event": event,
                "data": data,
            }.items()
            if value is not None
        }
        return await call(ctx, WORKFLOW_RUN, arguments, body)
