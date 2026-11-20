"""The skills that a session sees, and the rules for a write (docs/architecture.md, Section 9.2).

Everything here is pure. The stores (``sama.skills.store``) load the rows and save the plan.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from sama.skills.errors import SkillError
from sama.skills.lint import SKILL_FILE, name_problem, validate

SCOPES: Final = ("user", "workspace", "org", "builtin")
"""From the narrowest to the widest. A narrower scope overrides a wider one."""
MAX_INDEX: Final = 200
ACTIONS: Final = ("create", "patch", "edit", "write_file", "remove_file", "archive")
SKILLS_TOOLSET: Final = "skills"


@dataclass(frozen=True, slots=True)
class SkillMeta:
    """A skill without its text. This is enough for the index."""

    name: str
    scope: str
    description: str
    version: str
    tags: tuple[str, ...] = ()
    requires_toolsets: tuple[str, ...] = ()
    skill_id: uuid.UUID | None = None
    archived: bool = False


@dataclass(frozen=True, slots=True)
class SkillDoc(SkillMeta):
    """A skill with its ``SKILL.md`` and its files."""

    skill_md: str = ""
    files: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Actor:
    """Who reads or writes skills in a session, and what the session can use."""

    org_id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID | None
    private: bool
    """True in a private session. Only then does the session see user skills."""
    toolsets: frozenset[str] = frozenset()
    allowed: frozenset[str] = frozenset()
    """The skills of the profile. Empty means all skills."""
    workspace_admin: bool = False
    org_owner: bool = False
    session_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None

    @property
    def sees_user_skills(self) -> bool:
        return self.private and self.user_id is not None


def resolve[T: SkillMeta](records: Iterable[T], actor: Actor) -> dict[str, T]:
    """The skill of each name that a write or a read can reach: archived skills and user
    skills of a shared session are out, and the narrowest scope wins."""
    best: dict[str, T] = {}
    for record in records:
        if record.archived or (record.scope == "user" and not actor.sees_user_skills):
            continue
        current = best.get(record.name)
        if current is None or SCOPES.index(record.scope) < SCOPES.index(current.scope):
            best[record.name] = record
    return best


def usable(record: SkillMeta, actor: Actor) -> bool:
    """True if the profile of the session allows the skill (Section 9.2)."""
    return (not actor.allowed or record.name in actor.allowed) and set(
        record.requires_toolsets
    ) <= set(actor.toolsets)


def index[T: SkillMeta](records: Iterable[T], actor: Actor) -> list[T]:
    """The skills of the session, sorted by name. Empty without the toolset ``skills``."""
    if SKILLS_TOOLSET not in actor.toolsets:
        return []
    visible = resolve(records, actor)
    return [visible[name] for name in sorted(visible) if usable(visible[name], actor)][:MAX_INDEX]


def default_scope(actor: Actor) -> str:
    return "user" if actor.sees_user_skills else "workspace"


def authorize(scope: str, actor: Actor) -> None:
    """Raises ``SkillError`` if the actor cannot write in ``scope`` (Section 9.2)."""
    if scope == "builtin":
        raise SkillError(
            "Builtin skills are read-only. To change one, create a skill with the same name "
            "in the scope user or workspace. It overrides the builtin skill."
        )
    if scope == "user":
        if not actor.sees_user_skills:
            raise SkillError(
                "A shared session has no single owner, so it has no user skills. Use the "
                "scope workspace."
            )
    elif scope == "workspace":
        if not (actor.workspace_admin or actor.org_owner):
            raise SkillError(
                "Only a workspace admin or the org owner can change a workspace skill. Ask an "
                "admin to make this change, or use the scope user in a private session."
            )
    elif scope == "org":
        if not actor.org_owner:
            raise SkillError("Only the org owner can change an org skill. Ask the org owner.")
    else:
        raise SkillError(f"The scope must be one of: {', '.join(SCOPES[:3])}.")


@dataclass(frozen=True, slots=True)
class ManageRequest:
    """The arguments of ``skill_manage``."""

    action: str
    name: str
    scope: str | None = None
    content: str | None = None
    old_string: str | None = None
    new_string: str | None = None
    replace_all: bool = False
    file_path: str | None = None
    file_content: str | None = None
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class WritePlan:
    """What a write does. ``target`` is the row of the skill in ``scope``, if there is one."""

    scope: str
    target: SkillDoc | None
    change: str
    skill_md: str
    files: Mapping[str, str]
    description: str
    tags: tuple[str, ...]
    requires_toolsets: tuple[str, ...]


def _need(value: str | None, field_name: str, action: str) -> str:
    if value is None:
        raise SkillError(f"The action {action} needs {field_name}.")
    return value


def _patched(text: str, request: ManageRequest, label: str) -> str:
    old = _need(request.old_string, "old_string", "patch")
    new = _need(request.new_string, "new_string", "patch")
    if not old:
        raise SkillError("old_string must not be empty.")
    count = text.count(old)
    if count == 0:
        raise SkillError(f"old_string is not in {label}. It must match the text exactly.")
    if count > 1 and not request.replace_all:
        raise SkillError(
            f"old_string is in {label} {count} times. Add more text around it, or set replace_all."
        )
    return text.replace(old, new)


def _content(target: SkillDoc | None, request: ManageRequest) -> tuple[str, Mapping[str, str]]:
    """The new ``SKILL.md`` and the new files."""
    action = request.action
    if action == "create":
        return _need(request.content, "content", action), {}
    if target is None:
        raise SkillError("There is no such skill.")
    files = dict(target.files)
    if action == "edit":
        return _need(request.content, "content", action), files
    if action == "archive":
        return target.skill_md, files
    if action == "patch":
        path = request.file_path or SKILL_FILE
        if path == SKILL_FILE:
            return _patched(target.skill_md, request, SKILL_FILE), files
        if path not in files:
            raise SkillError(f"The skill has no file {path}.")
        files[path] = _patched(files[path], request, path)
        return target.skill_md, files
    path = _need(request.file_path, "file_path", action)
    if action == "write_file":
        files[path] = _need(request.file_content, "file_content", action)
        return target.skill_md, files
    if path not in files:
        raise SkillError(f"The skill has no file {path}.")
    del files[path]
    return target.skill_md, files


def plan_write(candidates: Sequence[SkillDoc], actor: Actor, request: ManageRequest) -> WritePlan:
    """Checks a write and computes the new text. ``candidates`` are the skills with the name
    of the request, in the scopes of the actor, archived skills included. Raises
    ``SkillError`` for a request that cannot work."""
    if request.action not in ACTIONS:
        raise SkillError(f"The action must be one of: {', '.join(ACTIONS)}.")
    name = request.name
    if request.action == "create":
        if (problem := name_problem(name)) is not None:
            raise SkillError(problem)
        scope = request.scope or default_scope(actor)
        authorize(scope, actor)
        target = next((c for c in candidates if c.scope == scope), None)
        if target is not None and not target.archived:
            raise SkillError(
                f'A skill "{name}" exists in the scope {scope}. Use the action edit or patch.'
            )
    else:
        if request.scope is None:
            target = resolve(candidates, actor).get(name)
        else:
            target = next(
                (
                    c
                    for c in candidates
                    if c.scope == request.scope
                    and not c.archived
                    and (c.scope != "user" or actor.sees_user_skills)
                ),
                None,
            )
        if target is None:
            where = f" in the scope {request.scope}" if request.scope else ""
            raise SkillError(f'There is no skill "{name}"{where}. Use skills_list to see skills.')
        scope = target.scope
        authorize(scope, actor)
    skill_md, files = _content(None if request.action == "create" else target, request)
    if request.action == "archive" and target is not None:
        # An archive must work for a skill that an older rule made invalid.
        return WritePlan(
            scope,
            target,
            "archive",
            skill_md,
            files,
            target.description,
            target.tags,
            target.requires_toolsets,
        )
    parsed = validate(name, skill_md, files)
    meta = parsed.meta
    return WritePlan(
        scope,
        target,
        request.action,
        skill_md,
        files,
        meta.description.strip(),
        meta.tags,
        meta.requires_toolsets,
    )
