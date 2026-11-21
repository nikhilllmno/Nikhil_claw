"""Changes of an agent profile with a check of the old values: the apply step of an approved
``change_propose`` proposal, the revert in the web app, the diff for an approval card, and the
session snapshot (docs/architecture.md, Sections 9.4.2 and 11.4).

A change is two JSON objects with the same keys: ``before`` (the values that the change
expects now) and ``after`` (the new values). The keys are fields of the profile
(``sama.profiles.PROFILE_FIELDS``) or ``rules``: the policy rules of the profile, an object
that maps a tool name or a prefix (``web_*``) to ``allow``, ``ask``, ``deny``, or ``null`` for
no rule. ``write_change`` writes the change only when the profile still has the ``before``
values. Else a person changed the profile after the agent saw it, and the change fails.

A session freezes its soul, runtime, model, and sub-agents (Section 6.1), and the snapshot in
``sessions.profile_snapshot`` keeps its toolsets and its profile policy rules. The gate allows
only what the snapshot and the profile of now both allow. So an approved change applies from the
next session, and a removal of a permission applies at once.
"""

from __future__ import annotations

import difflib
import json
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Final

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import AgentProfile, AuditEntry, ProfileChange, ToolPolicy, User
from sama.profiles import (
    KIND_FIELDS,
    ProfileError,
    lock_admin,
    planned_values,
    profile_value,
    record_change,
)
from sama.subagents import LockedProfile, lock_profile
from sama.vault.redact import default_redactor

POLICY_ACTIONS: Final = ("allow", "ask", "deny")
MAX_RULES: Final = 20
"""The most rules that one proposal can change."""
TOOL_MAX_CHARS: Final = 200
DIFF_MAX_CHARS: Final = 1500
"""The longest diff on an approval card. A longer diff is cut, and the full change is in the
proposal."""
HISTORY_LIMIT: Final = 50
POLICY_KEY: Final = "uq_tool_policies_org_id_agent_profile_id_user_id_tool"


class StaleChangeError(ProfileError):
    """The profile does not have the ``before`` values of the change now."""


class RevertError(ProfileError):
    """The change cannot be reverted. The text is safe to show."""


def check_rule_tool(tool: str) -> str:
    """A tool of a policy rule: an exact name or a prefix that ends with ``*``."""
    tool = tool.strip()
    if not 1 <= len(tool) <= TOOL_MAX_CHARS:
        raise ProfileError(f"a tool name has 1 to {TOOL_MAX_CHARS} characters")
    if any(char.isspace() for char in tool) or "*" in tool[:-1]:
        raise ProfileError(f"the tool {tool!r} is not valid: use a name, or a prefix with * last")
    return tool


def check_rules(rules: Mapping[str, Any]) -> dict[str, str | None]:
    """Checks a ``rules`` object. Raises ``ProfileError``."""
    if len(rules) > MAX_RULES:
        raise ProfileError(f"change at most {MAX_RULES} rules at a time")
    out: dict[str, str | None] = {}
    for tool, action in rules.items():
        if action is not None and action not in POLICY_ACTIONS:
            raise ProfileError(f"the action {action!r} is not known: use allow, ask, or deny")
        out[check_rule_tool(tool)] = action
    return out


async def profile_rules(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    tools: Iterable[str] | None = None,
    *,
    lock: bool = False,
) -> dict[str, str]:
    """The policy rules of the profile (not of a user) as ``{tool: action}``. With ``tools``,
    only the rules of those tools."""
    query = select(ToolPolicy.tool, ToolPolicy.action).where(
        ToolPolicy.org_id == org_id,
        ToolPolicy.agent_profile_id == profile_id,
        ToolPolicy.user_id.is_(None),
    )
    if tools is not None:
        query = query.where(ToolPolicy.tool.in_(list(tools)))
    if lock:
        query = query.with_for_update()
    return {str(tool): str(action) for tool, action in await connection.execute(query)}


async def current_values(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    profile: LockedProfile,
    fields: Mapping[str, Any],
) -> dict[str, Any]:
    """The values of the profile now for the keys of ``fields`` (a ``before`` or ``after``). The
    policy rules are locked until the end of the transaction."""
    out: dict[str, Any] = {}
    for field, value in fields.items():
        if field == "rules":
            tools = list(value or {})
            found = await profile_rules(connection, org_id, profile_id, tools, lock=True)
            out["rules"] = {tool: found.get(tool) for tool in tools}
        else:
            out[field] = profile_value(profile, field)
    return out


def _same(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


async def write_change(
    connection: AsyncConnection,
    *,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    kind: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    actor_user_id: uuid.UUID,
    source: str,
    proposal_id: uuid.UUID | None = None,
    reverts_change_id: uuid.UUID | None = None,
    summary: str | None = None,
    admin_only: bool = False,
) -> uuid.UUID:
    """Writes the change on ``connection`` (in the transaction of the caller), the audit entry,
    and the history row. Returns the id of the history row.

    Raises ``StaleChangeError`` when the profile does not have the ``before`` values now,
    ``ProfileError`` or ``SubagentError`` when the ``after`` values are not valid now, and
    ``NotAdminError`` with ``admin_only`` when the actor is not an org owner or admin now.
    """
    if set(before) != set(after) or not set(after) <= set(KIND_FIELDS[kind]):
        raise ProfileError("the change is not valid")
    profile = await lock_profile(connection, org_id, profile_id)
    if admin_only:
        await lock_admin(connection, org_id, actor_user_id)
    rules = check_rules(after["rules"]) if "rules" in after else None
    live = await current_values(connection, org_id, profile_id, profile, before)
    if not _same(live, dict(before)):
        raise StaleChangeError(
            "the profile changed after this change was made. Make a new change from the values"
            " of now"
        )
    values = planned_values(profile, {key: value for key, value in after.items() if key != "rules"})
    if values:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.org_id == org_id, AgentProfile.id == profile_id)
            .values(**values, updated_at=func.now())
        )
    for tool, action in (rules or {}).items():
        await _set_rule(connection, org_id, profile_id, tool, action, actor_user_id)
    change_id = await record_change(
        connection,
        org_id=org_id,
        workspace_id=profile.workspace_id,
        profile_id=profile_id,
        kind=kind,
        source=source,
        before=before,
        after=after,
        actor_user_id=actor_user_id,
        proposal_id=proposal_id,
        reverts_change_id=reverts_change_id,
        summary=summary,
    )
    details: dict[str, Any] = {"fields": sorted(after), "change_id": str(change_id)}
    if rules is not None:
        details["tools"] = sorted(rules)
    if proposal_id is not None:
        details["proposal_id"] = str(proposal_id)
    if reverts_change_id is not None:
        details["reverts_change_id"] = str(reverts_change_id)
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            workspace_id=profile.workspace_id,
            actor_kind="user",
            actor_user_id=actor_user_id,
            action="profile.updated",
            target_type="agent_profile",
            target_id=str(profile_id),
            details=details,
        )
    )
    return change_id


async def _set_rule(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    tool: str,
    action: str | None,
    actor_user_id: uuid.UUID,
) -> None:
    if action is None:
        await connection.execute(
            delete(ToolPolicy).where(
                ToolPolicy.org_id == org_id,
                ToolPolicy.agent_profile_id == profile_id,
                ToolPolicy.user_id.is_(None),
                ToolPolicy.tool == tool,
            )
        )
        return
    statement = pg_insert(ToolPolicy).values(
        org_id=org_id,
        agent_profile_id=profile_id,
        user_id=None,
        tool=tool,
        action=action,
        created_by_user_id=actor_user_id,
    )
    await connection.execute(
        statement.on_conflict_do_update(
            constraint=POLICY_KEY, set_={"action": action, "updated_at": func.now()}
        )
    )


async def revert_change(
    connection: AsyncConnection,
    *,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    change_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> uuid.UUID:
    """Reverts one change of the history: writes its ``before`` values, when the profile still
    has its ``after`` values. Returns the id of the new history row. Raises ``RevertError``,
    ``StaleChangeError``, ``NotAdminError``, ``ProfileError``, or ``SubagentError``."""
    row = (
        await connection.execute(
            select(ProfileChange)
            .where(
                ProfileChange.org_id == org_id,
                ProfileChange.agent_profile_id == profile_id,
                ProfileChange.id == change_id,
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise LookupError("the change does not exist")
    if row.reverted_by_change_id is not None:
        raise RevertError("the change is already reverted")
    if row.redacted:
        raise RevertError(
            "the change had a secret, so sama did not keep its values. Edit the profile instead"
        )
    new_id = await write_change(
        connection,
        org_id=org_id,
        profile_id=profile_id,
        kind=row.kind,
        before=row.after,
        after=row.before,
        actor_user_id=actor_user_id,
        source="revert",
        reverts_change_id=row.id,
        summary=f"revert of the change of {row.created_at:%Y-%m-%d %H:%M} UTC",
        admin_only=True,
    )
    await connection.execute(
        update(ProfileChange)
        .where(ProfileChange.org_id == org_id, ProfileChange.id == row.id)
        .values(reverted_by_change_id=new_id)
    )
    return new_id


async def list_changes(
    connection: AsyncConnection, org_id: uuid.UUID, profile_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The newest changes of the profile, for the history view. On a connection of a user, the
    row level security of ``profile_changes`` limits the rows. The values are redacted again
    before the diff is made, because a diff line (``-`` or ``+`` in front of each line) can
    hide a secret of many lines from the redactor."""
    redactor = default_redactor()
    rows = await connection.execute(
        select(ProfileChange, User.display_name.label("actor_name"))
        .outerjoin(
            User, and_(User.org_id == ProfileChange.org_id, User.id == ProfileChange.actor_user_id)
        )
        .where(ProfileChange.org_id == org_id, ProfileChange.agent_profile_id == profile_id)
        .order_by(ProfileChange.created_at.desc(), ProfileChange.id.desc())
        .limit(HISTORY_LIMIT)
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        before, after = redactor.json(dict(row.before)), redactor.json(dict(row.after))
        out.append(
            {
                "id": str(row.id),
                "kind": row.kind,
                "source": row.source,
                "summary": row.summary,
                "before": before,
                "after": after,
                "diff": change_diff(row.kind, before, after, cut=False),
                "redacted": row.redacted,
                "actor_user_id": None if row.actor_user_id is None else str(row.actor_user_id),
                "actor_name": row.actor_name,
                "proposal_id": None if row.proposal_id is None else str(row.proposal_id),
                "reverts_change_id": (
                    None if row.reverts_change_id is None else str(row.reverts_change_id)
                ),
                "reverted_by_change_id": (
                    None if row.reverted_by_change_id is None else str(row.reverted_by_change_id)
                ),
                "created_at": row.created_at.isoformat(),
            }
        )
    return out


def _short(value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return ", ".join(value) or "(none)"
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def change_diff(
    kind: str, before: Mapping[str, Any], after: Mapping[str, Any], *, cut: bool = True
) -> str:
    """A short text diff of a change for a person. A soul gets a unified line diff. A list of
    toolsets gets ``+`` and ``-`` lines. Other fields get ``field: old -> new`` lines. With
    ``cut``, the text has at most ``DIFF_MAX_CHARS`` characters."""
    lines: list[str] = []
    for field in sorted(after):
        old, new = before.get(field), after.get(field)
        if field == "soul":
            lines.extend(
                difflib.unified_diff(
                    str(old or "").splitlines(),
                    str(new or "").splitlines(),
                    "soul (before)",
                    "soul (after)",
                    lineterm="",
                )
            )
        elif field in ("toolsets", "group_toolsets"):
            old_set, new_set = list(old or []), list(new or [])
            lines.append(f"{field}:")
            lines.extend(f"+ {name}" for name in new_set if name not in old_set)
            lines.extend(f"- {name}" for name in old_set if name not in new_set)
        elif field == "rules":
            lines.append("policy rules of the profile:")
            for tool in sorted(set(old or {}) | set(new or {})):
                was = (old or {}).get(tool) or "no rule"
                will = (new or {}).get(tool) or "no rule"
                lines.append(f"  {tool}: {was} -> {will}")
        elif field == "subagents":
            old_names = [str(item.get("name")) for item in old or [] if isinstance(item, dict)]
            new_names = [str(item.get("name")) for item in new or [] if isinstance(item, dict)]
            lines.append(f"subagents: {_short(old_names)} -> {_short(new_names)}")
            for spec in new or []:
                if isinstance(spec, dict) and spec not in (old or []):
                    lines.append(f"+ {_short(spec)}")
        else:
            lines.append(f"{field}: {_short(old)} -> {_short(new)}")
    text = "\n".join(lines)
    if cut and len(text) > DIFF_MAX_CHARS:
        text = text[: DIFF_MAX_CHARS - 2].rstrip() + "\n…"
    return text


async def session_snapshot(
    connection: AsyncConnection, org_id: uuid.UUID, profile_id: uuid.UUID
) -> dict[str, Any] | None:
    """The value of ``sessions.profile_snapshot`` for a session that freezes its config now."""
    row = (
        await connection.execute(
            select(AgentProfile.toolsets, AgentProfile.group_toolsets).where(
                AgentProfile.org_id == org_id, AgentProfile.id == profile_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    rules = await profile_rules(connection, org_id, profile_id)
    return {
        "toolsets": [str(name) for name in row.toolsets or []],
        "group_toolsets": [str(name) for name in row.group_toolsets or []],
        "rules": [{"tool": tool, "action": action} for tool, action in sorted(rules.items())],
    }
