"""The MCP tools of delegation (``delegate_task``, ``delegate_status``) and of the task board
(``task_*``). See docs/architecture.md, Sections 12.4 and 13.2.

The tool server calls ``register_delegation`` and ``register_tasks`` when it has the services.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from sama.db.models import MAX_TASK_DEPENDENCIES, TASK_STATUSES
from sama.delegation import (
    MAX_CHILD_COST_USD,
    MAX_CHILD_TURNS,
    MAX_CHILDREN,
    MAX_CONTEXT_CHARS,
    MAX_GOAL_CHARS,
    MAX_STEER_CHARS,
    MAX_WAIT_S,
    ChildTask,
    DelegationError,
    DelegationService,
    render_agents,
    render_started,
    render_statuses,
)
from sama.tasks.runs import TOOL_LIST_LIMIT, TaskTools, render_task, render_tasks
from sama.tasks.service import MAX_BODY_CHARS, MAX_COMMENT_CHARS, MAX_RESULT_CHARS, MAX_TITLE_CHARS
from sama.tenancy.run_token import RunTokenClaims

DELEGATE_TASK: Final = "delegate_task"
DELEGATE_STATUS: Final = "delegate_status"
TASK_CREATE: Final = "task_create"
TASK_LIST: Final = "task_list"
TASK_COMPLETE: Final = "task_complete"
TASK_BLOCK: Final = "task_block"
TASK_COMMENT: Final = "task_comment"

Call = Callable[
    [Context, str, Mapping[str, Any], Callable[[RunTokenClaims], Awaitable[str]]],
    Awaitable[str],
]
_Id = Annotated[str, Field(min_length=32, max_length=36)]


class DelegateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: Annotated[str, Field(min_length=1, max_length=MAX_GOAL_CHARS)]
    context: Annotated[str, Field(max_length=MAX_CONTEXT_CHARS)] = ""
    max_turns: Annotated[int | None, Field(ge=1, le=MAX_CHILD_TURNS)] = None
    max_cost_usd: Annotated[
        Decimal | None, Field(gt=0, le=MAX_CHILD_COST_USD, max_digits=8, decimal_places=4)
    ] = None
    agent: Annotated[str | None, Field(min_length=1, max_length=40)] = None


def _child_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        raise DelegationError("child_id is not a valid id") from None


def register_delegation(server: MCPServer, call: Call, service: DelegationService) -> None:
    @server.tool(
        name=DELEGATE_TASK,
        description=(
            f"Give 1 to {MAX_CHILDREN} independent tasks to child agents. Each child gets its "
            "own session and sandbox, the same agent profile, and none of this conversation: "
            "put all that it needs in goal and context. A child cannot delegate, use the task "
            "board, ask the user, or save memory. Each child has a budget: max_turns (default "
            f"30, at most {MAX_CHILD_TURNS}) and max_cost_usd (default 1, at most "
            f"{MAX_CHILD_COST_USD}). A run can start {MAX_CHILDREN} children in total. With "
            "wait=true the call returns when each child ends or after timeout_s; with "
            "wait=false it returns at once. Use it for parallel work, not for one small step. "
            "agent: the name of a library sub-agent; the child then runs as that agent, with its "
            "prompt, toolsets, model, and limits."
        ),
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False),
        structured_output=False,
    )
    async def delegate_task(
        tasks: Annotated[list[DelegateTask], Field(min_length=1, max_length=MAX_CHILDREN)],
        ctx: Context,
        wait: bool = True,
        timeout_s: Annotated[int, Field(ge=0, le=MAX_WAIT_S)] = 300,
    ) -> str:
        async def body(claims: RunTokenClaims) -> str:
            children = await service.start(
                claims,
                [
                    ChildTask(
                        goal=task.goal,
                        context=task.context,
                        max_turns=task.max_turns,
                        max_cost_usd=task.max_cost_usd,
                        agent=task.agent,
                    )
                    for task in tasks
                ],
            )
            text = render_started(children)
            if not wait:
                return text
            statuses = await service.wait(claims, children, timeout_s)
            return text + "\n\n" + render_statuses(statuses, waited=True)

        arguments = {
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "wait": wait,
            "timeout_s": timeout_s,
        }
        return await call(ctx, DELEGATE_TASK, arguments, body)

    @server.tool(
        name=DELEGATE_STATUS,
        description=(
            "See or control the child agents of this session. Actions: status (all children, "
            "or one with child_id: status, cost, and result), cancel (child_id: stop the "
            "child), steer (child_id and message: the child gets the message after its "
            "current run), agents (the library agents that delegate_task can start with agent)."
        ),
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False),
        structured_output=False,
    )
    async def delegate_status(
        ctx: Context,
        action: Literal["status", "cancel", "steer", "agents"] = "status",
        child_id: _Id | None = None,
        message: Annotated[str | None, Field(min_length=1, max_length=MAX_STEER_CHARS)] = None,
    ) -> str:
        async def body(claims: RunTokenClaims) -> str:
            if action == "agents":
                return render_agents(await service.agents(claims))
            if action == "status":
                child = None if child_id is None else _child_id(child_id)
                return render_statuses(await service.status(claims, child))
            if child_id is None:
                raise DelegationError(f"give child_id to {action} a child")
            child = _child_id(child_id)
            if action == "cancel":
                outcome = await service.cancel(claims, child)
                return f"The child {child} is {outcome}."
            if message is None or not message.strip():
                raise DelegationError("give message to steer a child")
            run_id = await service.steer(claims, child, message)
            return f"The child {child} gets the message in run {run_id}."

        arguments = {"action": action, "child_id": child_id, "message": message}
        return await call(ctx, DELEGATE_STATUS, arguments, body)


def register_tasks(server: MCPServer, call: Call, tasks: TaskTools) -> None:
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

    @server.tool(
        name=TASK_CREATE,
        description=(
            "Add a card to the task board of this workspace. People see the board in the web "
            "app. assignee: self (default: an agent with your profile does the card in a new "
            "session), agent (profile_id), person (the user of this run), or none. A card "
            "with an agent assignee starts when each card in depends_on is done. In a task "
            "run, the new card is a child of the current card unless you give parent_task_id."
        ),
        annotations=write,
        structured_output=False,
    )
    async def task_create(
        title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE_CHARS)],
        ctx: Context,
        body: Annotated[str, Field(max_length=MAX_BODY_CHARS)] = "",
        assignee: Literal["self", "agent", "person", "none"] = "self",
        profile_id: _Id | None = None,
        depends_on: Annotated[list[_Id], Field(max_length=MAX_TASK_DEPENDENCIES)] | None = None,
        parent_task_id: _Id | None = None,
    ) -> str:
        async def run(claims: RunTokenClaims) -> str:
            task = await tasks.create(
                claims,
                title=title,
                body=body,
                assignee=assignee,
                profile_id=profile_id,
                depends_on=depends_on or (),
                parent_task_id=parent_task_id,
            )
            return render_task(task, "Created")

        arguments = {
            "title": title,
            "body": body,
            "assignee": assignee,
            "profile_id": profile_id,
            "depends_on": depends_on,
            "parent_task_id": parent_task_id,
        }
        return await call(ctx, TASK_CREATE, arguments, run)

    @server.tool(
        name=TASK_LIST,
        description=(
            "List the cards of the task board of this workspace, newest first. Give statuses "
            "to filter: " + ", ".join(TASK_STATUSES) + "."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        structured_output=False,
    )
    async def task_list(
        ctx: Context,
        statuses: Annotated[
            list[Literal["todo", "running", "blocked", "done", "cancelled"]] | None,
            Field(max_length=5),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=TOOL_LIST_LIMIT)] = 20,
    ) -> str:
        async def run(claims: RunTokenClaims) -> str:
            found, current = await tasks.list(claims, statuses=statuses or (), limit=limit)
            return render_tasks(found, current)

        return await call(ctx, TASK_LIST, {"statuses": statuses, "limit": limit}, run)

    @server.tool(
        name=TASK_COMPLETE,
        description=(
            "Mark a card done and save its result. Cards that depend on it can then start, "
            "and they get the result. task_id defaults to the card of this task run."
        ),
        annotations=write,
        structured_output=False,
    )
    async def task_complete(
        result: Annotated[str, Field(min_length=1, max_length=MAX_RESULT_CHARS)],
        ctx: Context,
        task_id: _Id | None = None,
    ) -> str:
        async def run(claims: RunTokenClaims) -> str:
            task = await tasks.finish(claims, status="done", text=result, task_id=task_id)
            return render_task(task, "Completed")

        return await call(ctx, TASK_COMPLETE, {"result": result, "task_id": task_id}, run)

    @server.tool(
        name=TASK_BLOCK,
        description=(
            "Mark a card blocked when you cannot finish it, and give the reason. A person "
            "sees the reason on the board. task_id defaults to the card of this task run."
        ),
        annotations=write,
        structured_output=False,
    )
    async def task_block(
        reason: Annotated[str, Field(min_length=1, max_length=MAX_COMMENT_CHARS)],
        ctx: Context,
        task_id: _Id | None = None,
    ) -> str:
        async def run(claims: RunTokenClaims) -> str:
            task = await tasks.finish(claims, status="blocked", text=reason, task_id=task_id)
            return render_task(task, "Blocked")

        return await call(ctx, TASK_BLOCK, {"reason": reason, "task_id": task_id}, run)

    @server.tool(
        name=TASK_COMMENT,
        description=(
            "Add a comment to a card, for example progress or a question for a person. "
            "task_id defaults to the card of this task run."
        ),
        annotations=write,
        structured_output=False,
    )
    async def task_comment(
        body: Annotated[str, Field(min_length=1, max_length=MAX_COMMENT_CHARS)],
        ctx: Context,
        task_id: _Id | None = None,
    ) -> str:
        async def run(claims: RunTokenClaims) -> str:
            await tasks.comment(claims, body=body, task_id=task_id)
            return "Added the comment."

        return await call(ctx, TASK_COMMENT, {"body": body, "task_id": task_id}, run)
