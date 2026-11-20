"""The skills of a session for the parts of sama that are not a tool call: the prompt
assembler (the index), the gateways and the web API (the slash commands), and the sandbox
sync (the files)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.runtime.contract import RunScope
from sama.skills import commands
from sama.skills.library import SKILLS_TOOLSET, Actor, SkillDoc, index
from sama.skills.lint import KNOWN_TOOLSETS
from sama.skills.sql import load_actor, load_docs, profile_limits


class SkillLibrary:
    """The skills that a session can use: the builtin skills and the stored skills."""

    def __init__(self, engine: AsyncEngine, builtin: Mapping[str, SkillDoc]) -> None:
        self._engine = engine
        self._builtin = builtin

    @property
    def builtin(self) -> Mapping[str, SkillDoc]:
        return self._builtin

    async def actor(
        self,
        connection: AsyncConnection,
        *,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        profile_id: uuid.UUID,
        user_id: uuid.UUID | None,
        private: bool,
        kind: str = "chat",
    ) -> Actor:
        """The actor of a session. It has no roles, so it is for reads only. A shared session
        has the group toolsets of the profile (Section 11.3)."""
        toolsets, allowed = await profile_limits(
            connection,
            org_id,
            profile_id,
            audience="private" if private else "shared",
            kind=kind,
        )
        return Actor(
            org_id=org_id,
            workspace_id=workspace_id,
            user_id=user_id,
            private=private,
            toolsets=toolsets,
            allowed=allowed,
        )

    async def index(self, connection: AsyncConnection, actor: Actor) -> list[SkillDoc]:
        """The skills of the actor, without text, sorted by name."""
        stored = await load_docs(connection, actor, full=False)
        return index([*stored, *self._builtin.values()], actor)

    async def catalog(
        self,
        connection: AsyncConnection,
        *,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[SkillDoc]:
        """The skills that a user sees in a private session of a workspace, for the web page.
        The limits of an agent profile do not apply. ``connection`` runs as the user, so the
        RLS policies apply as well."""
        actor = Actor(
            org_id=org_id,
            workspace_id=workspace_id,
            user_id=user_id,
            private=True,
            toolsets=frozenset(KNOWN_TOOLSETS),
        )
        stored = await load_docs(connection, actor, full=False)
        return index([*stored, *self._builtin.values()], actor)

    async def files(self, connection: AsyncConnection, actor: Actor) -> dict[str, str]:
        """The files of the skills of the actor for the sandbox: ``<name>/SKILL.md`` and
        ``<name>/<path>`` for each other file, with the text."""
        visible = await self.index(connection, actor)
        names = [skill.name for skill in visible if skill.scope != "builtin"]
        full = {
            (doc.name, doc.scope): doc
            for doc in await load_docs(connection, actor, names=names, full=True)
            if not doc.archived
        }
        files: dict[str, str] = {}
        for skill in visible:
            doc = (
                self._builtin[skill.name]
                if skill.scope == "builtin"
                else full.get((skill.name, skill.scope))
            )
            if doc is None:
                continue
            files[f"{skill.name}/SKILL.md"] = doc.skill_md
            for path, text in doc.files.items():
                files[f"{skill.name}/{path}"] = text
        return files

    async def expand(self, actor: Actor, text: str) -> str | None:
        """The message text for a skill command, or ``None`` if ``text`` is not a command of
        a skill that the actor can use (Section 9.5). ``/learn`` is a command of sama: it asks
        the agent for a draft skill, if the session has the skills toolset (Section 9.4)."""
        learn = commands.parse_learn(text)
        if learn is not None:
            if SKILLS_TOOLSET not in actor.toolsets:
                return None
            return commands.learn_expansion(learn)
        parsed = commands.parse(text)
        if parsed is None:
            return None
        name, request = parsed
        async with self._engine.connect() as connection:
            skills = await self.index(connection, actor)
        if all(skill.name != name for skill in skills):
            return None
        return commands.expansion(name, request)


class SessionSkillSource:
    """The ``SkillSource`` of the sandbox sync: the skills of the session of a run."""

    def __init__(self, engine: AsyncEngine, library: SkillLibrary) -> None:
        self._engine = engine
        self._library = library

    async def files(self, scope: RunScope) -> Mapping[str, str]:
        async with self._engine.connect() as connection:
            actor = await load_actor(
                connection,
                org_id=uuid.UUID(scope.org_id),
                session_id=uuid.UUID(scope.session_id),
                run_id=uuid.UUID(scope.run_id),
                token_user=scope.user_id,
            )
            return await self._library.files(connection, actor)
