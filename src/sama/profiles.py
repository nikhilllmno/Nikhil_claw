"""Edits to an agent profile from the web app (docs/architecture.md, Section 11.4).

``update_profile`` changes the name, the soul, the runtime, the model, the toolsets, and the
sub-agents of a profile in one transaction. It writes one audit entry with the names of the
changed fields and the names of the sub-agents, never the text of the soul or of a prompt.
It also writes the history of the profile (``profile_changes``), which the web app shows with a
revert button (``sama.profile_changes``).

A session keeps the soul, the runtime, the model, and the sub-agents that it froze, so those
changes apply from the next session. A session also keeps a snapshot of the toolsets, and the
gate allows only the toolsets that the snapshot and the profile of now both have: a removed
toolset applies at the next tool call, and a new toolset applies from the next session.

The agent can propose the same changes with ``change_propose`` (M3.2 and M6.1,
``sama.review.change``), and a person approves them on an approval card.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Final

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.admin.bootstrap import DEFAULT_MODEL, default_profile
from sama.db.models import (
    BUSY_MODES,
    MAX_PARALLEL_RUNS_CAP,
    AgentProfile,
    AuditEntry,
    Membership,
    Org,
    ProfileChange,
    Workspace,
)
from sama.gate.decide import TOOLSETS
from sama.orgs.plans import plan_of
from sama.runtime.contract import (
    BUDGET_KEYS,
    REASONING_EFFORTS,
    SUBAGENT_DESCRIPTION_MAX_CHARS,
    SUBAGENT_MAX_TURNS,
    SUBAGENT_NAME_MAX_CHARS,
    SUBAGENT_NAME_PATTERN,
    SUBAGENT_PROMPT_MAX_CHARS,
    RuntimeKind,
)
from sama.subagents import (
    MAX_SUBAGENTS,
    LockedProfile,
    known_models,
    lock_profile,
    model_is_known,
    parse_subagents,
    subagents_value,
    supports_subagents,
    validate_subagents,
)
from sama.tenancy.access import ADMIN_ROLES
from sama.vault.redact import default_redactor, redact

EDITABLE_RUNTIMES: Final = (
    RuntimeKind.CLAUDE_AGENT_SDK,
    RuntimeKind.OPENAI_AGENTS_SDK,
    RuntimeKind.CLAUDE_CODE,
    RuntimeKind.CODEX,
)
"""The runtimes that an editor can choose. ``fake`` is for tests only. ``claude_code`` and
``codex`` send each message of a session of the profile to the coding agent CLI (Section
15.4)."""
NAME_MAX_CHARS: Final = 80
SOUL_MAX_CHARS: Final = 20_000
PROFILE_FIELDS: Final = (
    "name",
    "soul",
    "runtime",
    "model",
    "subagents",
    "toolsets",
    "group_toolsets",
    "budget",
    "reasoning_effort",
    "busy_mode",
    "max_parallel_runs",
)
"""The fields of a profile that an edit, a proposal, or a revert can change."""
ADMIN_ONLY_FIELDS: Final = ("budget", "reasoning_effort")
"""The fields that only a person changes in the web app. The agent cannot propose them: an agent
must not raise its own budget."""
KIND_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "soul": ("soul",),
    "profile": (
        "name",
        "runtime",
        "model",
        "subagents",
        "budget",
        "reasoning_effort",
        "busy_mode",
        "max_parallel_runs",
    ),
    "toolsets": ("toolsets", "group_toolsets"),
    "policy": ("rules",),
}
"""The fields of each kind of change. ``rules`` are the policy rules of the profile."""
MAX_TURNS_LIMIT: Final = 10_000
MAX_WALL_TIME_LIMIT: Final = 86_400.0
MAX_PROFILES_PER_ORG: Final = 20
"""An org has at most this many agent profiles (M6.9). The web app makes new profiles."""
_CREATE_LOCK: Final = "sama.profiles.create"


class ProfileError(ValueError):
    """The change is not valid. The text is safe to show to the caller."""


class NotAdminError(Exception):
    """The actor is not an org owner or admin any more when the write runs."""


class ProfileLimitError(Exception):
    """The org has ``MAX_PROFILES_PER_ORG`` profiles. The text is safe to show."""


class Unset(Enum):
    """The marker of a field that a change does not set. ``None`` is a value for some fields."""

    UNSET = "unset"


UNSET: Final = Unset.UNSET


@dataclass(frozen=True, slots=True)
class ProfileChanges:
    """The fields to change. ``None`` means: keep the value."""

    name: str | None = None
    soul: str | None = None
    runtime: RuntimeKind | None = None
    model: str | None = None
    toolsets: Sequence[str] | None = None
    subagents: Sequence[Any] | None = None
    """The raw JSON of the specs. ``parse_subagents`` checks it."""
    budget: Mapping[str, Any] | None = None
    """The whole run budget. A key that is missing or ``None`` is no limit."""
    reasoning_effort: str | Unset | None = UNSET
    """``None`` sets the default of the model, so ``None`` is a value and ``UNSET`` keeps it."""
    busy_mode: str | None = None
    """``steer``, ``parallel``, or ``queue`` (Section 4.3, M7.1)."""
    max_parallel_runs: int | None = None
    """The most open chat runs of one session, 1 to ``MAX_PARALLEL_RUNS_CAP``."""


def profile_options(runtime: RuntimeKind, plan: str | None = None) -> dict[str, Any]:
    """What an editor of a profile with this runtime can choose, and the limits. ``plan`` is the
    plan of the org: its run budget gives ``budget_caps``."""
    caps = plan_of(plan).run_budget if plan is not None else {}
    return {
        "toolsets": [
            {"name": name, "tools": sorted(tools)} for name, tools in sorted(TOOLSETS.items())
        ],
        "models": known_models(runtime),
        "subagents": supports_subagents(runtime),
        "reasoning_efforts": list(REASONING_EFFORTS),
        "budget_caps": {
            "max_turns": caps.get("max_turns"),
            "max_cost_usd": None if caps.get("max_cost_usd") is None else str(caps["max_cost_usd"]),
            "max_wall_time_s": caps.get("max_wall_time_s"),
        },
        "runtimes": [
            {
                "name": kind.value,
                "models": known_models(kind),
                "subagents": supports_subagents(kind),
            }
            for kind in EDITABLE_RUNTIMES
        ],
        "limits": {
            "name_max_chars": NAME_MAX_CHARS,
            "soul_max_chars": SOUL_MAX_CHARS,
            "max_subagents": MAX_SUBAGENTS,
            "subagent_name_max_chars": SUBAGENT_NAME_MAX_CHARS,
            "subagent_name_pattern": SUBAGENT_NAME_PATTERN,
            "subagent_description_max_chars": SUBAGENT_DESCRIPTION_MAX_CHARS,
            "subagent_prompt_max_chars": SUBAGENT_PROMPT_MAX_CHARS,
            "subagent_max_turns": SUBAGENT_MAX_TURNS,
            "max_parallel_runs": MAX_PARALLEL_RUNS_CAP,
        },
        "busy_modes": list(BUSY_MODES),
    }


def _check_toolsets(toolsets: Sequence[str]) -> list[str]:
    if len(set(toolsets)) != len(toolsets):
        raise ProfileError("a toolset is in the list two times")
    unknown = [name for name in toolsets if name not in TOOLSETS]
    if unknown:
        raise ProfileError("the toolset is not known: " + ", ".join(sorted(set(unknown))[:5]))
    return list(toolsets)


def _positive_int(key: str, value: Any, limit: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ProfileError(f"{key} is a whole number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ProfileError(f"{key} is a whole number") from None
    if not number.is_finite() or number != number.to_integral_value():
        raise ProfileError(f"{key} is a whole number")
    if number <= 0:
        raise ProfileError(f"{key} is more than 0")
    if limit is not None and number > limit:
        raise ProfileError(f"{key} is at most {limit}")
    return int(number)


def _positive_decimal(key: str, value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise ProfileError(f"{key} is a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ProfileError(f"{key} is a number") from None
    if not number.is_finite():
        raise ProfileError(f"{key} is a number")
    if number <= 0:
        raise ProfileError(f"{key} is more than 0")
    return number


def check_budget(raw: Mapping[str, Any], plan: str | None) -> dict[str, Any]:
    """The stored form of a run budget: only the keys with a limit. A key that is missing or
    ``None`` is no limit, but not when the plan of the org has a cap for the key: then the
    budget must give a value, so an org cannot remove the cap of its plan. Every value is more
    than 0, ``max_turns`` is at most ``MAX_TURNS_LIMIT``, and ``max_wall_time_s`` is at most
    ``MAX_WALL_TIME_LIMIT``. A value must not be more than the cap of the plan of the org
    (``sama.orgs.plans``). Raises ``ProfileError``."""
    unknown = set(raw) - set(BUDGET_KEYS)
    if unknown:
        raise ProfileError("the budget key is not known: " + ", ".join(sorted(unknown)[:5]))
    caps = plan_of(plan).run_budget if plan is not None else {}
    out: dict[str, Any] = {}
    for key in BUDGET_KEYS:
        value = raw.get(key)
        cap = caps.get(key)
        if value is None:
            if cap is not None:
                raise ProfileError(f"The plan of the org needs a limit for {key}: at most {cap}.")
            continue
        if key in ("max_turns", "max_tokens"):
            checked: Any = _positive_int(
                key, value, MAX_TURNS_LIMIT if key == "max_turns" else None
            )
            stored: Any = checked
        elif key == "max_cost_usd":
            checked = _positive_decimal(key, value)
            stored = format(checked, "f")
        else:
            checked = _positive_decimal(key, value)
            if checked > Decimal(str(MAX_WALL_TIME_LIMIT)):
                raise ProfileError(f"{key} is at most {MAX_WALL_TIME_LIMIT:g}")
            stored = float(checked)
        if cap is not None and Decimal(str(checked)) > Decimal(str(cap)):
            raise ProfileError(f"{key} is at most {cap} on the {plan} plan")
        out[key] = stored
    return out


def budget_json(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """The run budget as the web app and the history show it: only the keys with a limit, with
    the cost as text. A stored value that is not a number is left out."""
    out: dict[str, Any] = {}
    for key in BUDGET_KEYS:
        value = (raw or {}).get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            continue
        if key == "max_cost_usd":
            out[key] = format(number, "f")
        elif key == "max_wall_time_s":
            out[key] = float(number)
        else:
            out[key] = int(number)
    return out


def _same_budget(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """True when two budgets have the same limits (``1.0`` and ``1.00`` are the same)."""
    return {k: Decimal(str(v)) for k, v in budget_json(left).items()} == {
        k: Decimal(str(v)) for k, v in budget_json(right).items()
    }


def planned_values(profile: LockedProfile, after: Mapping[str, Any]) -> dict[str, Any]:
    """The column values that set the fields of ``after`` on ``profile``, checked.

    ``after`` maps a field (``name``, ``soul``, ``runtime``, ``model``, ``subagents``,
    ``toolsets``, ``group_toolsets``) to its new JSON value. A field with the value that the
    profile has now is not a change. Raises ``ProfileError`` when a value is not valid, and
    ``SubagentError`` when the sub-agents do not fit the profile after the change (for example,
    a toolset that a sub-agent uses is not in the new toolsets).

    A change of the runtime needs a model of the new runtime: the new model of the change, or
    the model that the profile has now. A review model that the new runtime does not know goes
    back to the default of the runtime. The stored sub-agents must fit the new runtime.
    """
    unknown = set(after) - set(PROFILE_FIELDS)
    if unknown:
        raise ProfileError("the field is not known: " + ", ".join(sorted(unknown)))
    values: dict[str, Any] = {}
    if "name" in after:
        name = str(after["name"]).strip()
        if not 1 <= len(name) <= NAME_MAX_CHARS:
            raise ProfileError(f"the name has 1 to {NAME_MAX_CHARS} characters")
        if name != profile.name:
            values["name"] = name
    if "soul" in after:
        soul = str(after["soul"])
        if len(soul) > SOUL_MAX_CHARS:
            raise ProfileError(f"the soul has at most {SOUL_MAX_CHARS} characters")
        if redact(soul) != soul:
            raise ProfileError("the soul contains a secret")
        if soul != profile.soul:
            values["soul"] = soul
    runtime = profile.runtime
    if "runtime" in after:
        try:
            wanted = RuntimeKind(str(after["runtime"]))
        except ValueError:
            raise ProfileError(f"the runtime {after['runtime']} is not known") from None
        if wanted not in EDITABLE_RUNTIMES:
            raise ProfileError(f"the runtime {wanted.value} cannot be chosen")
        if wanted != profile.runtime:
            runtime = wanted
            values["runtime"] = runtime.value
    new_model = None if after.get("model") is None else str(after["model"])
    if "runtime" in values:
        model = profile.model if new_model is None else new_model
        if model not in known_models(runtime):
            raise ProfileError("choose a model of the new runtime")
        if model != profile.model:
            values["model"] = model
        if profile.review_model is not None and not model_is_known(runtime, profile.review_model):
            values["review_model"] = None
    elif new_model is not None and new_model != profile.model:
        # The stored model can be a model that the table no longer lists. Only a new value must
        # be in the list.
        if new_model not in known_models(runtime):
            raise ProfileError("the model is not known for the runtime of the profile")
        values["model"] = new_model
    for field in ("toolsets", "group_toolsets"):
        if field in after:
            toolsets = _check_toolsets([str(name) for name in after[field] or []])
            if toolsets != getattr(profile, field):
                values[field] = toolsets
    if "budget" in after:
        raw = after["budget"]
        if not isinstance(raw, Mapping):
            raise ProfileError("the budget is an object")
        budget = check_budget(raw, profile.plan)
        if not _same_budget(budget, profile.budget or {}):
            values["budget"] = budget
    if "reasoning_effort" in after:
        effort = after["reasoning_effort"]
        if effort is not None and effort not in REASONING_EFFORTS:
            raise ProfileError("the reasoning effort is low, medium, or high")
        if effort != profile.reasoning_effort:
            values["reasoning_effort"] = effort
    if "busy_mode" in after:
        mode = str(after["busy_mode"])
        if mode not in BUSY_MODES:
            raise ProfileError("the busy mode is steer, parallel, or queue")
        if mode != profile.busy_mode:
            values["busy_mode"] = mode
    if "max_parallel_runs" in after:
        limit = after["max_parallel_runs"]
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_PARALLEL_RUNS_CAP
        ):
            raise ProfileError(f"the parallel run limit is 1 to {MAX_PARALLEL_RUNS_CAP}")
        if limit != profile.max_parallel_runs:
            values["max_parallel_runs"] = limit
    final_toolsets = values.get("toolsets", profile.toolsets)
    if "subagents" in after:
        specs = parse_subagents(list(after["subagents"] or []))
        validate_subagents(specs, toolsets=final_toolsets, runtime=runtime)
        value = subagents_value(specs)
        if value != profile.subagents:
            values["subagents"] = value
    elif "toolsets" in values or "runtime" in values:
        # The toolsets or the runtime can change, so the stored sub-agents must still fit.
        validate_subagents(
            parse_subagents(profile.subagents), toolsets=final_toolsets, runtime=runtime
        )
    return values


def profile_value(profile: LockedProfile, field: str) -> Any:
    """The JSON value of a field of the profile now."""
    if field == "runtime":
        return profile.runtime.value
    if field == "budget":
        return budget_json(profile.budget)
    value = getattr(profile, field)
    return list(value) if isinstance(value, list) else value


def split_change(
    profile: LockedProfile, values: Mapping[str, Any], fields: Sequence[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ``before`` and ``after`` values of the ``fields`` that ``values`` changes."""
    changed = [field for field in fields if field in values]
    return (
        {field: profile_value(profile, field) for field in changed},
        {field: values[field] for field in changed},
    )


async def lock_admin(
    connection: AsyncConnection, org_id: uuid.UUID, actor_user_id: uuid.UUID
) -> None:
    """Raises ``NotAdminError`` when the actor is not an org owner or admin now. Call it after
    the lock of the profile: the share lock keeps the membership until the end of the
    transaction, so a demotion that is waiting cannot slip in before the write."""
    admin = await connection.execute(
        select(Membership.id)
        .where(
            Membership.org_id == org_id,
            Membership.user_id == actor_user_id,
            Membership.workspace_id.is_(None),
            Membership.role.in_(ADMIN_ROLES),
        )
        .with_for_update(read=True)
    )
    if admin.first() is None:
        raise NotAdminError


async def update_profile(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    changes: ProfileChanges,
    *,
    actor_user_id: uuid.UUID,
) -> None:
    """Applies the changes, writes the audit entry and the history (``profile_changes``).

    A field with the value that the profile has now is not a change. Without a change, the
    function writes nothing. Raises ``ProfileError`` when a value is not valid, and
    ``SubagentError`` when the profile does not exist or the sub-agents do not fit the profile
    after the change. Raises ``NotAdminError`` when the actor is not an org owner or admin at
    the time of the write: the caller checked the role earlier, and the role can change while
    the write waits for the lock of the profile. See ``planned_values`` for the rules.
    """
    if changes.runtime is not None and changes.runtime not in EDITABLE_RUNTIMES:
        raise ProfileError(f"the runtime {changes.runtime.value} cannot be chosen")
    after: dict[str, Any] = {}
    if changes.name is not None:
        after["name"] = changes.name
    if changes.soul is not None:
        after["soul"] = changes.soul
    if changes.runtime is not None:
        after["runtime"] = changes.runtime.value
    if changes.model is not None:
        after["model"] = changes.model
    if changes.toolsets is not None:
        after["toolsets"] = list(changes.toolsets)
    if changes.subagents is not None:
        after["subagents"] = list(changes.subagents)
    if changes.budget is not None:
        after["budget"] = dict(changes.budget)
    if not isinstance(changes.reasoning_effort, Unset):
        after["reasoning_effort"] = changes.reasoning_effort
    if changes.busy_mode is not None:
        after["busy_mode"] = changes.busy_mode
    if changes.max_parallel_runs is not None:
        after["max_parallel_runs"] = changes.max_parallel_runs
    if "soul" in after:
        # Checked before the lock, so a bad soul does not wait for the lock.
        if len(after["soul"]) > SOUL_MAX_CHARS:
            raise ProfileError(f"the soul has at most {SOUL_MAX_CHARS} characters")
        if redact(after["soul"]) != after["soul"]:
            raise ProfileError("the soul contains a secret")
    if "toolsets" in after:
        _check_toolsets(after["toolsets"])
    if "subagents" in after:
        parse_subagents(after["subagents"])

    async with engine.begin() as connection:
        profile = await lock_profile(connection, org_id, profile_id)
        await lock_admin(connection, org_id, actor_user_id)
        values = planned_values(profile, after)
        if not values:
            return
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.org_id == org_id, AgentProfile.id == profile_id)
            .values(**values, updated_at=func.now())
        )
        subagent_names = (
            [str(spec["name"]) for spec in values["subagents"]] if "subagents" in values else []
        )
        await connection.execute(
            insert(AuditEntry).values(
                org_id=org_id,
                workspace_id=profile.workspace_id,
                actor_kind="user",
                actor_user_id=actor_user_id,
                action="profile.updated",
                target_type="agent_profile",
                target_id=str(profile_id),
                details={
                    "fields": sorted(values),
                    "subagents": subagent_names,
                },
            )
        )
        for kind, fields in KIND_FIELDS.items():
            before, after = split_change(profile, values, fields)
            if not after:
                continue
            await record_change(
                connection,
                org_id=org_id,
                workspace_id=profile.workspace_id,
                profile_id=profile_id,
                kind=kind,
                source="edit",
                before=before,
                after=after,
                actor_user_id=actor_user_id,
            )


async def record_change(
    connection: AsyncConnection,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    profile_id: uuid.UUID,
    kind: str,
    source: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    actor_user_id: uuid.UUID | None,
    proposal_id: uuid.UUID | None = None,
    reverts_change_id: uuid.UUID | None = None,
    summary: str | None = None,
) -> uuid.UUID:
    """Writes one row of the history of a profile on ``connection``. Returns its id.

    The row has the redacted values: a member can read the history (RLS), so a known secret,
    for example one that an admin removes from the soul, does not stay in it."""
    redactor = default_redactor()
    safe_before, safe_after = redactor.json(dict(before)), redactor.json(dict(after))
    redacted = safe_before != dict(before) or safe_after != dict(after)
    return (
        await connection.execute(
            insert(ProfileChange)
            .values(
                org_id=org_id,
                workspace_id=workspace_id,
                agent_profile_id=profile_id,
                kind=kind,
                source=source,
                before=safe_before,
                after=safe_after,
                redacted=redacted,
                actor_user_id=actor_user_id,
                proposal_id=proposal_id,
                reverts_change_id=reverts_change_id,
                summary=None if summary is None else redactor.text(summary),
            )
            .returning(ProfileChange.id)
        )
    ).scalar_one()


@dataclass(frozen=True, slots=True)
class NewProfile:
    """A new agent profile (M6.9). ``workspace_id`` ``None`` makes a profile of the whole org.
    Without ``runtime`` and ``model``, the profile gets the defaults of a new org."""

    name: str
    soul: str = ""
    runtime: RuntimeKind | None = None
    model: str | None = None
    workspace_id: uuid.UUID | None = None


async def create_profile(
    engine: AsyncEngine, org_id: uuid.UUID, new: NewProfile, *, actor_user_id: uuid.UUID
) -> uuid.UUID:
    """Makes an agent profile with the toolsets of a new org and the run budget of the plan of
    the org (Section 17.2), and writes the audit entry ``profile.created``.

    Raises ``ProfileError`` for a value that is not valid or a workspace of another org,
    ``ProfileLimitError`` when the org has ``MAX_PROFILES_PER_ORG`` profiles, and
    ``NotAdminError`` when the actor is not an org owner or admin at the time of the write. The
    caller makes sure that the redactor knows the secrets of the org before a soul is checked.
    """
    name = new.name.strip()
    if not 1 <= len(name) <= NAME_MAX_CHARS:
        raise ProfileError(f"the name has 1 to {NAME_MAX_CHARS} characters")
    if len(new.soul) > SOUL_MAX_CHARS:
        raise ProfileError(f"the soul has at most {SOUL_MAX_CHARS} characters")
    if redact(new.soul) != new.soul:
        raise ProfileError("the soul contains a secret")
    runtime = new.runtime or RuntimeKind.CLAUDE_AGENT_SDK
    if runtime not in EDITABLE_RUNTIMES:
        raise ProfileError(f"the runtime {runtime.value} cannot be chosen")
    models = known_models(runtime)
    if new.model is not None:
        model = new.model
    elif runtime is RuntimeKind.CLAUDE_AGENT_SDK and DEFAULT_MODEL in models:
        model = DEFAULT_MODEL
    elif models:
        model = models[0]
    else:
        raise ProfileError("the runtime has no model")
    if model not in models:
        raise ProfileError("the model is not known for the runtime")

    async with engine.begin() as connection:
        # One create at a time in an org, so two parallel requests cannot pass the cap.
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key), hashtext(:org))"),
            {"key": _CREATE_LOCK, "org": str(org_id)},
        )
        await lock_admin(connection, org_id, actor_user_id)
        plan = (
            await connection.execute(select(Org.plan).where(Org.id == org_id))
        ).scalar_one_or_none()
        if plan is None:
            raise ProfileError("the org does not exist")
        if new.workspace_id is not None:
            found = (
                await connection.execute(
                    select(Workspace.id).where(
                        Workspace.org_id == org_id, Workspace.id == new.workspace_id
                    )
                )
            ).scalar_one_or_none()
            if found is None:
                raise ProfileError("the workspace is not in the org")
        count = (
            await connection.execute(
                select(func.count()).select_from(AgentProfile).where(AgentProfile.org_id == org_id)
            )
        ).scalar_one()
        if count >= MAX_PROFILES_PER_ORG:
            raise ProfileLimitError(f"an org has at most {MAX_PROFILES_PER_ORG} agents")
        values = default_profile(org_id, name=name, model=model, budget=plan_of(plan).run_budget)
        values.update(workspace_id=new.workspace_id, soul=new.soul, runtime=runtime.value)
        profile_id: uuid.UUID = (
            await connection.execute(
                insert(AgentProfile).values(**values).returning(AgentProfile.id)
            )
        ).scalar_one()
        await connection.execute(
            insert(AuditEntry).values(
                org_id=org_id,
                workspace_id=new.workspace_id,
                actor_kind="user",
                actor_user_id=actor_user_id,
                action="profile.created",
                target_type="agent_profile",
                target_id=str(profile_id),
                details={"runtime": runtime.value, "model": model, "soul": bool(new.soul)},
            )
        )
    return profile_id
