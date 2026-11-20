"""The fields of a job, their checks, and the approval rule (docs/architecture.md, Section 12.1).

The tool ``cronjob_manage``, the ``/cron`` commands, and the web API all write a job through
``JobService``. They give a ``JobInput``. ``JobSpec`` is the checked form. ``spec_hash`` is the
digest that a human approval binds to.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import Job, JobFire
from sama.gate.decide import (
    META_TOOLS,
    NEEDS_A_HUMAN,
    TOOL_LOADERS,
    Preapproval,
    canonical_name,
    connection_of,
)

MAX_PROMPT_CHARS: Final = 20_000
MAX_SCRIPT_CHARS: Final = 20_000
MAX_SKILLS: Final = 20
MAX_PREAPPROVALS: Final = 20
MAX_LIMIT_KEYS: Final = 10
MAX_LIMIT_VALUES: Final = 20
MAX_LIMIT_VALUE_CHARS: Final = 500
MAX_NAME_CHARS: Final = 200

NO_PREAPPROVAL: Final = frozenset({"cronjob_manage", *NEEDS_A_HUMAN, *META_TOOLS, *TOOL_LOADERS})
"""Tools that a job cannot pre-approve: the job tool itself (a job run cannot create jobs),
tools that wait for a human, and the meta tools (a pre-approval names the real tool)."""

_SLACK_CHANNEL: Final = re.compile(r"#?[A-Za-z0-9._-]{1,80}")
_TELEGRAM_CHAT: Final = re.compile(r"-?\d{1,20}")
_EMAIL: Final = re.compile(r"[^@\s]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")
_MODEL: Final = re.compile(r"[A-Za-z0-9._:/-]{1,100}")
_SKILL: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")


class JobError(Exception):
    """The job operation cannot run. The message is safe to show to the user."""


class JobNotFoundError(JobError):
    pass


class PreapprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=200)
    connection: str | None = Field(default=None, max_length=100)
    limits: dict[str, list[str | int | float | bool]] = Field(default_factory=dict)


class JobInput(BaseModel):
    """The fields that a person or the agent gives. A field that is not set does not change
    in an update. ``schedule`` is a text (natural language or cron) or a schedule object."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    schedule: str | dict[str, Any] | None = None
    tz: str | None = Field(default=None, max_length=64)
    prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    skills: list[str] | None = Field(default=None, max_length=MAX_SKILLS)
    model: str | None = Field(default=None, max_length=100)
    mode: Literal["agent", "script"] | None = None
    script: str | None = Field(default=None, max_length=MAX_SCRIPT_CHARS)
    deliver: str | None = Field(default=None, max_length=300)
    failure_deliver: str | None = Field(default=None, max_length=300)
    preapproved_tools: list[PreapprovalIn] | None = Field(default=None, max_length=MAX_PREAPPROVALS)
    attach_to_session: bool | None = None
    agent_profile_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class JobSpec:
    """The checked fields of one job version."""

    agent_profile_id: uuid.UUID
    schedule: dict[str, Any]
    prompt: str
    skills: tuple[str, ...]
    model: str | None
    mode: str
    script: str | None
    deliver: str
    failure_deliver: str | None
    preapproved_tools: tuple[dict[str, Any], ...]
    attach_to_session: bool

    def values(self) -> dict[str, Any]:
        """The column values."""
        return {
            "agent_profile_id": self.agent_profile_id,
            "schedule": self.schedule,
            "prompt": self.prompt,
            "skills": list(self.skills),
            "model": self.model,
            "mode": self.mode,
            "script": self.script,
            "deliver": self.deliver,
            "failure_deliver": self.failure_deliver,
            "preapproved_tools": [dict(item) for item in self.preapproved_tools],
            "attach_to_session": self.attach_to_session,
        }

    @classmethod
    def of(cls, row: Any) -> JobSpec:
        """The spec of a stored job row."""
        return cls(
            agent_profile_id=row.agent_profile_id,
            schedule=dict(row.schedule),
            prompt=row.prompt,
            skills=tuple(row.skills),
            model=row.model,
            mode=row.mode,
            script=row.script,
            deliver=row.deliver,
            failure_deliver=row.failure_deliver,
            preapproved_tools=tuple(dict(item) for item in row.preapproved_tools),
            attach_to_session=bool(row.attach_to_session),
        )


def spec_hash(spec: JobSpec) -> str:
    """The SHA-256 digest of the canonical spec. A human approval stores it, and the gate and
    the scheduler compare it with the stored job, so a changed row without a new approval has
    no pre-approval."""
    values = spec.values()
    values["agent_profile_id"] = str(values["agent_profile_id"])
    text = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_target(text: str, *, field: str = "deliver") -> str:
    """The stored form of a delivery target: ``origin``, ``none``, ``slack:<channel>``,
    ``telegram:<chat id>``, or ``email:<address>``."""
    value = text.strip()
    if value in ("origin", "none"):
        return value
    kind, _, rest = value.partition(":")
    kind, rest = kind.strip().lower(), rest.strip()
    if kind == "slack" and _SLACK_CHANNEL.fullmatch(rest):
        return f"slack:{rest}"
    if kind == "telegram" and _TELEGRAM_CHAT.fullmatch(rest):
        return f"telegram:{rest}"
    if kind == "email" and _EMAIL.fullmatch(rest):
        return f"email:{rest.lower()}"
    raise JobError(
        f"{field} must be origin, none, slack:<channel>, telegram:<chat id>, or email:<address>"
    )


def _limit_value(value: str | int | float | bool) -> str | int | float | bool:
    if isinstance(value, str) and len(value) > MAX_LIMIT_VALUE_CHARS:
        raise JobError(f"a limit value has more than {MAX_LIMIT_VALUE_CHARS} characters")
    return value


def parse_preapproval(item: PreapprovalIn) -> dict[str, Any]:
    """The stored form of one pre-approval. The connection must be the connection of the
    tool, so a pre-approval cannot name one connection and use another."""
    tool = canonical_name(item.tool.strip())
    if tool in NO_PREAPPROVAL:
        raise JobError(f"a job cannot pre-approve {tool}")
    connection = connection_of(tool)
    given = item.connection.strip() if item.connection else None
    if given is not None and given != connection:
        raise JobError(f"the connection of {tool} is {connection or 'none'}, not {given}")
    if len(item.limits) > MAX_LIMIT_KEYS:
        raise JobError(f"a pre-approval can limit at most {MAX_LIMIT_KEYS} arguments")
    limits: dict[str, list[Any]] = {}
    for key, values in sorted(item.limits.items()):
        if not key or not values or len(values) > MAX_LIMIT_VALUES:
            raise JobError(f"the limit {key!r} needs 1 to {MAX_LIMIT_VALUES} values")
        unique = {(type(value).__name__, _limit_value(value)): value for value in values}
        limits[key] = [unique[item] for item in sorted(unique, key=repr)]
    return {"tool": tool, "connection": connection, "limits": limits}


def preapprovals(stored: Sequence[Mapping[str, Any]]) -> tuple[Preapproval, ...]:
    """The gate form of the stored pre-approvals."""
    return tuple(
        Preapproval(
            tool=str(item["tool"]),
            connection=item.get("connection"),
            limits=tuple(
                (str(key), tuple(values)) for key, values in sorted(item.get("limits", {}).items())
            ),
        )
        for item in stored
    )


def parse_skills(names: Sequence[str]) -> tuple[str, ...]:
    result = []
    for name in names:
        name = name.strip()
        if not _SKILL.fullmatch(name):
            raise JobError(f"the skill name {name!r} is not valid")
        result.append(name)
    return tuple(dict.fromkeys(result))


def parse_model(model: str | None) -> str | None:
    if model is None or not model.strip():
        return None
    model = model.strip()
    if not _MODEL.fullmatch(model):
        raise JobError("the model name is not valid")
    return model


def external_targets(spec: JobSpec, owner_email: str | None) -> list[str]:
    """The targets that send to a place other than the chat of the owner. ``origin``, ``none``,
    and the email address of the owner are not external."""
    found = []
    own = f"email:{owner_email.lower()}" if owner_email else None
    for target in (spec.deliver, spec.failure_deliver):
        if target is None or target in ("origin", "none") or target == own:
            continue
        found.append(target)
    return found


def needs_approval(spec: JobSpec, owner_email: str | None) -> bool:
    """A job version needs a human approval when it pre-approves tools, runs a script, or
    sends to an external target (Section 12.1)."""
    return (
        bool(spec.preapproved_tools)
        or spec.mode == "script"
        or bool(external_targets(spec, owner_email))
    )


def approved(row: Any) -> bool:
    """The stored job has a human approval for its current version and its current fields."""
    return (
        row.approved_version is not None
        and row.approved_version == row.version
        and row.approved_hash == spec_hash(JobSpec.of(row))
    )


def cleared(row: Any, owner_email: str | None) -> bool:
    """The current version of the stored job can fire as it is: it has an approval, or it
    needs none."""
    return approved(row) or not needs_approval(JobSpec.of(row), owner_email)


@dataclass(frozen=True, slots=True)
class JobRun:
    """What the gate needs to know about a run that a job fire started."""

    job_id: uuid.UUID
    preapproved: tuple[Preapproval, ...]


async def job_of_run(
    connection: AsyncConnection, org_id: uuid.UUID, run_id: uuid.UUID
) -> JobRun | None:
    """The job of a run, or ``None`` for a run that no job started. The pre-approvals apply
    only when the fire ran the approved version, and the stored fields still have the approved
    digest."""
    row = (
        await connection.execute(
            select(Job, JobFire.job_version)
            .join(JobFire, and_(JobFire.org_id == Job.org_id, JobFire.job_id == Job.id))
            .where(JobFire.org_id == org_id, JobFire.run_id == run_id)
        )
    ).one_or_none()
    if row is None:
        return None
    granted = row.job_version == row.version and approved(row)
    return JobRun(
        job_id=row.id,
        preapproved=preapprovals(row.preapproved_tools) if granted else (),
    )
