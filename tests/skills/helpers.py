"""Builders for the skill tests."""

from __future__ import annotations

import uuid

from sama.skills import Actor, SkillDoc

ORG = uuid.UUID(int=1)
WORKSPACE = uuid.UUID(int=2)
USER = uuid.UUID(int=3)


def md(name: str, description: str = "Does a thing", extra: str = "", body: str = "Do it.") -> str:
    return f"---\nname: {name}\ndescription: {description}\nversion: 1\n{extra}---\n\n{body}\n"


def actor(**changes: object) -> Actor:
    base: dict[str, object] = {
        "org_id": ORG,
        "workspace_id": WORKSPACE,
        "user_id": USER,
        "private": True,
        "toolsets": frozenset({"core", "skills"}),
    }
    return Actor(**{**base, **changes})  # type: ignore[arg-type]


def doc(
    name: str,
    scope: str = "builtin",
    *,
    description: str = "A skill",
    archived: bool = False,
    requires: tuple[str, ...] = (),
    files: dict[str, str] | None = None,
) -> SkillDoc:
    return SkillDoc(
        name=name,
        scope=scope,
        description=description,
        version="1",
        requires_toolsets=requires,
        archived=archived,
        skill_md=md(name, description),
        files=files or {},
    )
