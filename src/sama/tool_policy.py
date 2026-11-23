"""The tool policy per tool, for the web app (docs/architecture.md, Section 16.1; roadmap M7.10).

The tool gate reads the rows of ``tool_policies``. This module shows them for one agent profile
and for the org, with the default verdict of each tool, and it changes them:

- A change of the rules of a profile goes through ``sama.profile_changes.write_change`` with
  the kind ``policy``, so the history of the agent has it (and a revert undoes it) and the audit
  entry ``profile.updated`` exists. It applies from the next session for a looser rule, and at
  once for a stricter rule (the gate reads the live rules and the frozen snapshot).
- A change of the org rules writes the audit entry ``tool_policy.updated``.

A rule is ``allow``, ``ask``, or ``deny``. ``None`` removes the rule. A tool is an exact name or a
prefix that ends with ``*``. A change has 1 to ``MAX_RULES`` rules.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AuditEntry
from sama.gate.decide import TOOLSETS, always_asks, policy_verdict
from sama.profile_changes import MAX_RULES, check_rules, profile_rules, set_rule, write_change
from sama.profiles import ProfileError, lock_admin
from sama.subagents import lock_profile


def tool_list(toolsets: Sequence[str]) -> list[dict[str, Any]]:
    """Every tool of ``TOOLSETS``, sorted by toolset and name. ``enabled`` is true when the
    toolset is one of ``toolsets``. ``default`` is the verdict of the gate in a private chat when
    no rule matches. ``shared_default`` is that verdict in a shared session, and
    ``unattended_default`` in a job, workflow, or webhook run before a pre-approval: some tools
    that a private chat allows ask there (``SHARED_ASK`` and ``SANDBOX_TOOLS``, M8.4)."""
    enabled = set(toolsets)
    out: list[dict[str, Any]] = []
    for toolset, names in sorted(TOOLSETS.items()):
        for name in sorted(names):
            out.append(
                {
                    "name": name,
                    "toolset": toolset,
                    "enabled": toolset in enabled,
                    "default": policy_verdict((), name).action,
                    "shared_default": policy_verdict((), name, shared=True).action,
                    "unattended_default": policy_verdict((), name, unattended=True).action,
                    "always_ask": always_asks(name),
                }
            )
    return out


def _wanted(rules: Sequence[tuple[str, str | None]]) -> dict[str, str | None]:
    """The checked rules of a change. Raises ``ProfileError`` for a tool that is two times in
    the change, a tool name or an action that is not valid, or too many rules."""
    wanted: dict[str, str | None] = {}
    for tool, action in rules:
        name = tool.strip()
        if name in wanted:
            raise ProfileError("a tool is in the rules two times")
        wanted[name] = action
    return check_rules(wanted)


def _sorted_rules(rules: dict[str, str]) -> list[dict[str, str]]:
    return [{"tool": tool, "action": action} for tool, action in sorted(rules.items())]


async def profile_view(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    toolsets: Sequence[str],
    *,
    editable: bool,
) -> dict[str, Any]:
    """The answer of ``GET .../agent-profiles/{id}/tool-policy``."""
    async with engine.connect() as connection:
        rules = await profile_rules(connection, org_id, profile_id)
        org = await profile_rules(connection, org_id, None)
    return {
        "editable": editable,
        "max_rules": MAX_RULES,
        "rules": _sorted_rules(rules),
        "org_rules": _sorted_rules(org),
        "tools": tool_list(toolsets),
    }


async def org_view(engine: AsyncEngine, org_id: uuid.UUID, *, editable: bool) -> dict[str, Any]:
    """The answer of ``GET .../orgs/{id}/tool-policy``."""
    async with engine.connect() as connection:
        org = await profile_rules(connection, org_id, None)
    return {"editable": editable, "max_rules": MAX_RULES, "rules": _sorted_rules(org)}


async def set_profile_rules(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    rules: Sequence[tuple[str, str | None]],
    *,
    actor_user_id: uuid.UUID,
) -> None:
    """Sets the rules of the profile. A rule that the profile has already is not a change, and
    a change with no real change writes nothing. Raises ``ProfileError`` for a value that is
    not valid, ``NotAdminError`` when the actor is not an org owner or admin now, and
    ``SubagentError`` when the profile does not exist."""
    wanted = _wanted(rules)
    async with engine.begin() as connection:
        # The lock of the profile comes first, as in ``write_change``. The rule rows are locked
        # when they are read, so the ``before`` values are the values of the write.
        await lock_profile(connection, org_id, profile_id)
        await lock_admin(connection, org_id, actor_user_id)
        current = await profile_rules(connection, org_id, profile_id, list(wanted), lock=True)
        changed = {tool: action for tool, action in wanted.items() if current.get(tool) != action}
        if not changed:
            return
        await write_change(
            connection,
            org_id=org_id,
            profile_id=profile_id,
            kind="policy",
            before={"rules": {tool: current.get(tool) for tool in changed}},
            after={"rules": changed},
            actor_user_id=actor_user_id,
            source="edit",
            admin_only=True,
        )


async def set_org_rules(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    rules: Sequence[tuple[str, str | None]],
    *,
    actor_user_id: uuid.UUID,
) -> None:
    """Sets the org rules and writes the audit entry ``tool_policy.updated``. A rule that the
    org has already is not a change, and a change with no real change writes nothing. Raises
    ``ProfileError`` for a value that is not valid and ``NotAdminError`` when the actor is not
    an org owner or admin now."""
    wanted = _wanted(rules)
    async with engine.begin() as connection:
        await lock_admin(connection, org_id, actor_user_id)
        current = await profile_rules(connection, org_id, None, list(wanted), lock=True)
        changed = {tool: action for tool, action in wanted.items() if current.get(tool) != action}
        if not changed:
            return
        for tool, action in changed.items():
            await set_rule(connection, org_id, None, tool, action, actor_user_id)
        await connection.execute(
            insert(AuditEntry).values(
                org_id=org_id,
                actor_kind="user",
                actor_user_id=actor_user_id,
                action="tool_policy.updated",
                target_type="org",
                target_id=str(org_id),
                details={"tools": changed},
            )
        )
