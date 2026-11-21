"""Custom tools: the store, the versions, the test call, and the tools of a run
(docs/architecture.md, Section 10.8; roadmap M7.3).

Scopes work like the scopes of the skills (Section 9.2):

- A ``user`` tool belongs to one user. A write of its user applies at once. A run sees it only
  in a private session of that user.
- A ``workspace`` tool is for the members of one workspace. An ``org`` tool is for every member
  of the org. Only an admin writes them (a workspace admin, an org admin, or the org owner for a
  workspace tool; an org admin or the org owner for an org tool), and a write applies at once.
  Another member reads and uses them, and gets ``403`` for a write. This is the rule of the
  sub-agent library (Section 13.3).

Each write is a new version, and a revert copies an old version into a new one. The tool runs
its ``current_version``. The credential of an HTTP tool is in the vault (``custom_tool:<id>``),
bound to one origin. The API never returns it.

A run sees its custom tools through ``tool_search`` (deferred tools of the toolset
``custom_tools``). The narrowest scope wins for a name: ``user``, then ``workspace``, then
``org``. The policy of the tool is a policy rule of the gate: a ``user`` tool gives a user rule
and a shared tool gives an org rule, so an admin ``ask`` or any ``deny`` rule still wins.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import ValidationError
from sqlalchemy import and_, case, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.customtools.http import Credential, HttpToolRunner, ToolCallError
from sama.customtools.script import SandboxFactory, run_script
from sama.customtools.spec import (
    PREFIX,
    TOOLSET,
    ArgumentError,
    Definition,
    DefinitionError,
    access_of,
    agent_name,
    check,
    check_arguments,
    check_summary,
    name_problem,
    origin_of,
)
from sama.db.models import (
    AuditEntry,
    CustomTool,
    CustomToolVersion,
    Run,
    Session,
    VaultSecret,
)
from sama.gate.decide import PolicyRule, RuleLevel, ToolAccess
from sama.tenancy.access import Grants
from sama.tenancy.authority import run_user
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.deferred import DeferredTool, DeferredToolError
from sama.tools.output import SessionSandbox, untrusted
from sama.vault.redact import redact
from sama.vault.store import Vault, custom_tool_secret

log: Final = logging.getLogger("sama.customtools")

MAX_TOOLS_PER_RUN: Final = 200
Scope = Literal["org", "workspace", "user"]


class CustomToolError(DeferredToolError):
    """A custom tool cannot be changed, tested, or called. The message is safe to show."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class Version:
    version_no: int
    definition: Definition
    change: str
    summary: str
    review: str
    author_user_id: uuid.UUID | None
    reviewed_by_user_id: uuid.UUID | None
    reviewed_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, row: Any) -> Version:
        return cls(
            version_no=row.version_no,
            definition=Definition.of(
                row.kind, row.description, row.input_schema, row.policy, row.config
            ),
            change=row.change,
            summary=row.summary,
            review=row.review,
            author_user_id=row.author_user_id,
            reviewed_by_user_id=row.reviewed_by_user_id,
            reviewed_at=row.reviewed_at,
            created_at=row.created_at,
        )

    def public(self) -> dict[str, Any]:
        return {
            "version": self.version_no,
            **self.definition.model_dump(mode="json", exclude_none=True),
            "change": self.change,
            "summary": self.summary,
            "review": self.review,
            "author_user_id": _str(self.author_user_id),
            "reviewed_by_user_id": _str(self.reviewed_by_user_id),
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ToolView:
    id: uuid.UUID
    org_id: uuid.UUID
    scope: Scope
    workspace_id: uuid.UUID | None
    user_id: uuid.UUID | None
    name: str
    current_version: int | None
    created_at: datetime
    updated_at: datetime
    current: Version | None = None
    latest: Version | None = None
    has_credential: bool = False
    can_manage: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "scope": self.scope,
            "workspace_id": _str(self.workspace_id),
            "user_id": _str(self.user_id),
            "name": self.name,
            "tool_name": agent_name(self.name),
            "current_version": self.current_version,
            "current": None if self.current is None else self.current.public(),
            "latest": None if self.latest is None else self.latest.public(),
            "has_credential": self.has_credential,
            "can_manage": self.can_manage,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class TestResult:
    ok: bool
    result: str | None
    error: str | None
    ms: int
    version: int

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
            "ms": self.ms,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class Live:
    """A tool that a run may use: its row and the definition of its current version."""

    id: uuid.UUID
    scope: Scope
    name: str
    version_no: int
    definition: Definition

    @property
    def tool_name(self) -> str:
        return agent_name(self.name)


@dataclass(frozen=True, slots=True)
class RunScope:
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID | None
    shared: bool


@dataclass
class _Clock:
    started: float = field(default_factory=time.monotonic)

    def ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


def _str(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def can_manage(scope: str, grants: Grants, workspace_id: uuid.UUID | None, user_id: Any) -> bool:
    """True when a write of this user applies at once."""
    if scope == "user":
        return user_id == grants.user_id and grants.org_member
    if scope == "workspace":
        return workspace_id is not None and (
            grants.org_admin or grants.admins_workspace(workspace_id)
        )
    return grants.org_admin


def can_see(scope: str, grants: Grants, workspace_id: uuid.UUID | None, user_id: Any) -> bool:
    if scope == "user":
        return user_id == grants.user_id and grants.org_member
    if scope == "workspace":
        return workspace_id is not None and grants.sees_workspace(workspace_id)
    return grants.org_member


class CustomToolService:
    """The custom tools of all orgs. One for the process.

    - ``vault``: the credentials of the HTTP tools. Without it, a tool with auth cannot get a
      credential, and setting one answers ``503``.
    - ``session_sandbox``: the sandbox of a session, for the calls of script tools. Without
      it, a script tool fails.
    - ``test_sandbox``: a new sandbox for the test of a script tool. Without it, the test of a
      script tool answers ``503``.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        vault: Vault | None = None,
        *,
        http: HttpToolRunner | None = None,
        session_sandbox: SessionSandbox | None = None,
        test_sandbox: SandboxFactory | None = None,
    ) -> None:
        self._engine = engine
        self._vault = vault
        self._http = http or HttpToolRunner()
        self.session_sandbox = session_sandbox
        self.test_sandbox = test_sandbox

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- reads ------------------------------------------------------------------------------

    async def listing(
        self, org_id: uuid.UUID, grants: Grants, workspace_id: uuid.UUID
    ) -> list[ToolView]:
        """The tools of the org, of the workspace, and the user tools of this user."""
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(CustomTool)
                    .where(
                        CustomTool.org_id == org_id,
                        or_(
                            CustomTool.scope == "org",
                            and_(
                                CustomTool.scope == "workspace",
                                CustomTool.workspace_id == workspace_id,
                            ),
                            and_(CustomTool.scope == "user", CustomTool.user_id == grants.user_id),
                        ),
                    )
                    .order_by(CustomTool.name, CustomTool.scope)
                )
            ).all()
            visible = [r for r in rows if can_see(r.scope, grants, r.workspace_id, r.user_id)]
            return [await self._view(connection, row, grants) for row in visible]

    def writable_scopes(self, grants: Grants, workspace_id: uuid.UUID) -> list[Scope]:
        """The scopes in which this user can add a tool from the page of the workspace."""
        scopes: list[Scope] = ["user", "workspace", "org"]
        return [
            scope
            for scope in scopes
            if can_manage(
                scope,
                grants,
                workspace_id if scope == "workspace" else None,
                grants.user_id if scope == "user" else None,
            )
        ]

    async def get(self, org_id: uuid.UUID, tool_id: uuid.UUID, grants: Grants) -> ToolView:
        async with self._engine.connect() as connection:
            row = await self._visible_row(connection, org_id, tool_id, grants)
            return await self._view(connection, row, grants)

    async def versions(
        self, org_id: uuid.UUID, tool_id: uuid.UUID, grants: Grants
    ) -> list[Version]:
        async with self._engine.connect() as connection:
            await self._visible_row(connection, org_id, tool_id, grants)
            rows = await connection.execute(
                select(CustomToolVersion)
                .where(CustomToolVersion.org_id == org_id, CustomToolVersion.tool_id == tool_id)
                .order_by(CustomToolVersion.version_no.desc())
            )
            return [Version.of(row) for row in rows]

    async def _visible_row(
        self,
        connection: AsyncConnection,
        org_id: uuid.UUID,
        tool_id: uuid.UUID,
        grants: Grants,
        *,
        lock: bool = False,
    ) -> Any:
        query = select(CustomTool).where(CustomTool.org_id == org_id, CustomTool.id == tool_id)
        if lock:
            query = query.with_for_update()
        row = (await connection.execute(query)).one_or_none()
        if row is None or not can_see(row.scope, grants, row.workspace_id, row.user_id):
            raise CustomToolError("the tool does not exist", status_code=404)
        return row

    async def _view(self, connection: AsyncConnection, row: Any, grants: Grants) -> ToolView:
        versions = (
            await connection.execute(
                select(CustomToolVersion)
                .where(CustomToolVersion.org_id == row.org_id, CustomToolVersion.tool_id == row.id)
                .order_by(CustomToolVersion.version_no.desc())
                .limit(1)
            )
        ).one_or_none()
        current = None
        if row.current_version is not None:
            found = (
                await connection.execute(
                    select(CustomToolVersion).where(
                        CustomToolVersion.org_id == row.org_id,
                        CustomToolVersion.tool_id == row.id,
                        CustomToolVersion.version_no == row.current_version,
                    )
                )
            ).one()
            current = Version.of(found)
        has_credential = (
            await connection.execute(
                select(VaultSecret.id).where(
                    VaultSecret.org_id == row.org_id,
                    VaultSecret.name == str(custom_tool_secret(row.id)),
                )
            )
        ).first() is not None
        return ToolView(
            id=row.id,
            org_id=row.org_id,
            scope=row.scope,
            workspace_id=row.workspace_id,
            user_id=row.user_id,
            name=row.name,
            current_version=row.current_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            current=current,
            latest=None if versions is None else Version.of(versions),
            has_credential=has_credential,
            can_manage=can_manage(row.scope, grants, row.workspace_id, row.user_id),
        )

    # -- writes -----------------------------------------------------------------------------

    async def create(
        self,
        org_id: uuid.UUID,
        grants: Grants,
        *,
        scope: Scope,
        workspace_id: uuid.UUID,
        name: str,
        definition: Definition,
        summary: str = "",
        credential: Mapping[str, str] | None = None,
    ) -> ToolView:
        """Adds a tool with its first version. ``workspace_id`` is the workspace of the page:
        a ``workspace`` tool belongs to it."""
        if problem := name_problem(name):
            raise CustomToolError(problem)
        _check(definition, summary)
        tool_workspace = workspace_id if scope == "workspace" else None
        tool_user = grants.user_id if scope == "user" else None
        if not grants.sees_workspace(workspace_id) or not can_see(
            scope, grants, tool_workspace, tool_user
        ):
            raise CustomToolError("the workspace does not exist", status_code=404)
        if not can_manage(scope, grants, tool_workspace, tool_user):
            raise CustomToolError(_SHARED_ADMIN_ONLY, status_code=403)
        if definition.http is not None:
            await self._check_url(definition)
        try:
            async with self._engine.begin() as connection:
                tool_id = (
                    await connection.execute(
                        insert(CustomTool)
                        .values(
                            org_id=org_id,
                            scope=scope,
                            workspace_id=tool_workspace,
                            user_id=tool_user,
                            name=name,
                            current_version=1,
                            created_by_user_id=grants.user_id,
                        )
                        .returning(CustomTool.id)
                    )
                ).scalar_one()
                await self._add_version(
                    connection, org_id, tool_id, 1, definition, "create", summary, grants
                )
                if credential is not None:
                    await self._put_credential(
                        connection, org_id, tool_id, definition, credential, grants
                    )
                await _audit(
                    connection,
                    org_id,
                    tool_workspace,
                    grants.user_id,
                    "custom_tool.created",
                    tool_id,
                    {"name": name, "scope": scope, "version": 1, "kind": definition.kind},
                )
        except IntegrityError:
            raise CustomToolError(
                f"a {scope} tool with the name {name} exists", status_code=409
            ) from None
        return await self.get(org_id, tool_id, grants)

    async def edit(
        self,
        org_id: uuid.UUID,
        tool_id: uuid.UUID,
        grants: Grants,
        definition: Definition,
        summary: str = "",
    ) -> ToolView:
        """A new version. It applies at once."""
        _check(definition, summary)
        if definition.http is not None:
            await self._check_url(definition)
        await self._write(org_id, tool_id, grants, definition, "edit", summary)
        return await self.get(org_id, tool_id, grants)

    async def revert(
        self, org_id: uuid.UUID, tool_id: uuid.UUID, grants: Grants, version_no: int
    ) -> ToolView:
        """A new version with the definition of an old version."""
        async with self._engine.connect() as connection:
            await self._visible_row(connection, org_id, tool_id, grants)
            row = (
                await connection.execute(
                    select(CustomToolVersion).where(
                        CustomToolVersion.org_id == org_id,
                        CustomToolVersion.tool_id == tool_id,
                        CustomToolVersion.version_no == version_no,
                    )
                )
            ).one_or_none()
        if row is None:
            raise CustomToolError(f"the tool has no version {version_no}", status_code=404)
        old = Version.of(row)
        _check(old.definition, "")
        await self._write(
            org_id, tool_id, grants, old.definition, "revert", f"Revert to version {version_no}"
        )
        return await self.get(org_id, tool_id, grants)

    async def _write(
        self,
        org_id: uuid.UUID,
        tool_id: uuid.UUID,
        grants: Grants,
        definition: Definition,
        change: Literal["edit", "revert"],
        summary: str,
    ) -> None:
        async with self._engine.begin() as connection:
            row = await self._visible_row(connection, org_id, tool_id, grants, lock=True)
            if not can_manage(row.scope, grants, row.workspace_id, row.user_id):
                raise CustomToolError(_ADMIN_ONLY, status_code=403)
            number = await _next_version(connection, org_id, tool_id)
            await self._add_version(
                connection, org_id, tool_id, number, definition, change, summary, grants
            )
            await connection.execute(
                update(CustomTool)
                .where(CustomTool.org_id == org_id, CustomTool.id == tool_id)
                .values(current_version=number, updated_at=func.now())
            )
            action = f"custom_tool.{'reverted' if change == 'revert' else 'edited'}"
            await _audit(
                connection,
                org_id,
                row.workspace_id,
                grants.user_id,
                action,
                tool_id,
                {"name": row.name, "version": number, "change": change},
            )

    async def _add_version(
        self,
        connection: AsyncConnection,
        org_id: uuid.UUID,
        tool_id: uuid.UUID,
        number: int,
        definition: Definition,
        change: str,
        summary: str,
        grants: Grants,
    ) -> None:
        await connection.execute(
            insert(CustomToolVersion).values(
                org_id=org_id,
                tool_id=tool_id,
                version_no=number,
                kind=definition.kind,
                description=definition.description,
                input_schema=definition.input_schema,
                policy=definition.policy,
                config=definition.config(),
                change=change,
                summary=summary,
                # Every version is written by a person who manages the tool, so it is approved.
                review="approved",
                author_user_id=grants.user_id,
                reviewed_by_user_id=grants.user_id,
                reviewed_at=func.now(),
            )
        )

    async def set_credential(
        self, org_id: uuid.UUID, tool_id: uuid.UUID, grants: Grants, values: Mapping[str, str]
    ) -> ToolView:
        """Stores the credential for the origin of the current version."""
        async with self._engine.begin() as connection:
            row = await self._visible_row(connection, org_id, tool_id, grants, lock=True)
            if not can_manage(row.scope, grants, row.workspace_id, row.user_id):
                raise CustomToolError(_ADMIN_ONLY, status_code=403)
            if row.current_version is None:
                raise CustomToolError("approve a version of the tool first", status_code=409)
            version = (
                await connection.execute(
                    select(CustomToolVersion).where(
                        CustomToolVersion.org_id == org_id,
                        CustomToolVersion.tool_id == tool_id,
                        CustomToolVersion.version_no == row.current_version,
                    )
                )
            ).one()
            definition = Version.of(version).definition
            await self._put_credential(connection, org_id, tool_id, definition, values, grants)
        return await self.get(org_id, tool_id, grants)

    async def _put_credential(
        self,
        connection: AsyncConnection,
        org_id: uuid.UUID,
        tool_id: uuid.UUID,
        definition: Definition,
        values: Mapping[str, str],
        grants: Grants,
    ) -> None:
        if self._vault is None:
            raise CustomToolError("the vault is off in this process", status_code=503)
        http = definition.http
        if http is None or http.auth.type == "none":
            raise CustomToolError("this tool sends no credential")
        if http.auth.type == "basic":
            needed: tuple[str, ...] = ("username", "password")
        else:
            needed = ("value",)
        kept = {key: str(values.get(key) or "").strip() for key in needed}
        if missing := [key for key, value in kept.items() if not value]:
            raise CustomToolError(f"the credential needs {', '.join(missing)}")
        credential = Credential(origin=origin_of(http.url), **kept)
        await self._vault.put(
            org_id,
            custom_tool_secret(tool_id),
            json.dumps(credential.stored()),
            actor_user_id=grants.user_id,
            connection=connection,
        )
        await _audit(
            connection,
            org_id,
            None,
            grants.user_id,
            "custom_tool.credential_set",
            tool_id,
            {"origin": credential.origin},
        )

    async def delete(self, org_id: uuid.UUID, tool_id: uuid.UUID, grants: Grants) -> None:
        """Deletes the tool, its versions, and its credential."""
        async with self._engine.begin() as connection:
            row = await self._visible_row(connection, org_id, tool_id, grants, lock=True)
            if not can_manage(row.scope, grants, row.workspace_id, row.user_id):
                raise CustomToolError(_ADMIN_ONLY, status_code=403)
            if self._vault is not None:
                await self._vault.delete(
                    org_id,
                    custom_tool_secret(tool_id),
                    actor_user_id=grants.user_id,
                    connection=connection,
                )
            await connection.execute(
                delete(CustomTool).where(CustomTool.org_id == org_id, CustomTool.id == tool_id)
            )
            await _audit(
                connection,
                org_id,
                row.workspace_id,
                grants.user_id,
                "custom_tool.deleted",
                tool_id,
                {"name": row.name, "scope": row.scope},
            )

    # -- test -------------------------------------------------------------------------------

    async def test(
        self,
        org_id: uuid.UUID,
        tool_id: uuid.UUID,
        grants: Grants,
        arguments: Mapping[str, Any],
        version_no: int | None = None,
    ) -> TestResult:
        """Runs a version (default: the current one, else the newest) with the arguments, as
        the agent would. Only a user who manages the tool can test it: a test sends the
        credential. An HTTP test runs on the control plane. A script test runs in a new
        sandbox."""
        async with self._engine.connect() as connection:
            row = await self._visible_row(connection, org_id, tool_id, grants)
            if not can_manage(row.scope, grants, row.workspace_id, row.user_id):
                raise CustomToolError(_ADMIN_ONLY, status_code=403)
            number = version_no or row.current_version
            query = select(CustomToolVersion).where(
                CustomToolVersion.org_id == org_id, CustomToolVersion.tool_id == tool_id
            )
            if number is None:
                query = query.order_by(CustomToolVersion.version_no.desc()).limit(1)
            else:
                query = query.where(CustomToolVersion.version_no == number)
            found = (await connection.execute(query)).one_or_none()
        if found is None:
            raise CustomToolError(f"the tool has no version {number}", status_code=404)
        version = Version.of(found)
        clock = _Clock()
        try:
            if version.definition.script is not None and self.test_sandbox is None:
                raise CustomToolError(
                    "script tests need a sandbox, and this process has none", status_code=503
                )
            text = await self._run(org_id, tool_id, version.definition, arguments, None)
            # The test answer goes to the browser, not through the tool server: redact it here.
            result = TestResult(True, redact(text), None, clock.ms(), version.version_no)
        except (ToolCallError, ArgumentError) as error:
            result = TestResult(False, None, redact(str(error)), clock.ms(), version.version_no)
        async with self._engine.begin() as connection:
            await _audit(
                connection,
                org_id,
                row.workspace_id,
                grants.user_id,
                "custom_tool.tested",
                tool_id,
                {"name": row.name, "version": version.version_no, "ok": result.ok},
            )
        return result

    # -- runs -------------------------------------------------------------------------------

    async def run_scope(self, claims: RunTokenClaims) -> RunScope:
        org_id = uuid.UUID(claims.org_id)
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(Run.triggered_by_user_id, Session.audience, Session.owner_user_id)
                    .join(Session, and_(Session.org_id == Run.org_id, Session.id == Run.session_id))
                    .where(
                        Run.org_id == org_id,
                        Run.id == uuid.UUID(claims.run_id),
                        Run.session_id == uuid.UUID(claims.session_id),
                        Session.workspace_id == uuid.UUID(claims.workspace_id),
                    )
                )
            ).one_or_none()
        if row is None:
            raise CustomToolError("the run does not exist")
        return RunScope(
            org_id=org_id,
            workspace_id=uuid.UUID(claims.workspace_id),
            user_id=run_user(row.triggered_by_user_id, row.audience, row.owner_user_id),
            shared=row.audience != "private",
        )

    async def live(self, connection: AsyncConnection, scope: RunScope) -> list[Live]:
        """The tools that a run may use: the current version of each visible tool. The
        narrowest scope wins for a name."""
        owners = [
            CustomTool.scope == "org",
            and_(CustomTool.scope == "workspace", CustomTool.workspace_id == scope.workspace_id),
        ]
        if not scope.shared and scope.user_id is not None:
            owners.append(and_(CustomTool.scope == "user", CustomTool.user_id == scope.user_id))
        rank = case((CustomTool.scope == "user", 0), (CustomTool.scope == "workspace", 1), else_=2)
        # One row for each name, the narrowest scope first, so the limit never drops an
        # override (``DISTINCT ON`` keeps the first row of each name in this order).
        rows = await connection.execute(
            select(CustomTool.id, CustomTool.scope, CustomTool.name, CustomToolVersion)
            .join(
                CustomToolVersion,
                and_(
                    CustomToolVersion.org_id == CustomTool.org_id,
                    CustomToolVersion.tool_id == CustomTool.id,
                    CustomToolVersion.version_no == CustomTool.current_version,
                ),
            )
            .where(CustomTool.org_id == scope.org_id, or_(*owners))
            .ext(distinct_on(CustomTool.name))
            .order_by(CustomTool.name, rank)
            .limit(MAX_TOOLS_PER_RUN)
        )
        found: list[Live] = []
        for row in rows:
            try:
                definition = Definition.of(
                    row.kind, row.description, row.input_schema, row.policy, row.config
                )
            except ValidationError:
                log.warning("custom tool %s has a version that is not valid", row.id)
                continue
            found.append(Live(row.id, row.scope, row.name, row.version_no, definition))
        return found

    async def run_tools(
        self,
        connection: AsyncConnection,
        *,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID | None,
        shared: bool,
        toolsets: frozenset[str],
    ) -> tuple[Mapping[str, frozenset[str]], Mapping[str, ToolAccess]]:
        """The toolset and the access of the custom tools of a run, for the tool gate. It
        reads in the transaction of the gate."""
        if TOOLSET not in toolsets:
            return {}, {}
        tools = await self.live(connection, RunScope(org_id, workspace_id, user_id, shared))
        return (
            {TOOLSET: frozenset(t.tool_name for t in tools)},
            {t.tool_name: access_of(t.definition) for t in tools},
        )

    async def run_rules(
        self,
        connection: AsyncConnection,
        *,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID | None,
        shared: bool,
        toolsets: frozenset[str],
    ) -> Sequence[PolicyRule]:
        """The policy of each custom tool of a run as a rule. ``ask`` and ``deny`` are org rules,
        so a user rule (for example an "always allow" of an approval card) cannot loosen them.
        ``allow`` is a user rule for a ``user`` tool and an org rule for a shared tool, so a
        user can still make a tool stricter for themselves."""
        if TOOLSET not in toolsets:
            return ()
        tools = await self.live(connection, RunScope(org_id, workspace_id, user_id, shared))
        return tuple(
            PolicyRule(
                tool=t.tool_name,
                action=t.definition.policy,
                level=(
                    RuleLevel.USER
                    if t.scope == "user" and t.definition.policy == "allow"
                    else RuleLevel.ORG
                ),
            )
            for t in tools
        )

    async def tools_for(self, claims: RunTokenClaims) -> list[DeferredTool]:
        """The custom tools of a run as deferred tools. The list is read for each search, so a
        new tool is usable at once, also in a running session."""
        try:
            scope = await self.run_scope(claims)
        except CustomToolError:
            return []
        async with self._engine.connect() as connection:
            tools = await self.live(connection, scope)
        out: list[DeferredTool] = []
        for tool in tools:
            try:
                out.append(
                    DeferredTool(
                        name=tool.tool_name,
                        description=f"Custom {tool.definition.kind} tool ({tool.scope}): "
                        + tool.definition.description,
                        toolset=TOOLSET,
                        handler=self._handler(tool.tool_name),
                        input_schema=tool.definition.input_schema,
                        tags=("custom", tool.definition.kind, tool.scope, tool.name),
                        access=access_of(tool.definition),
                        source=f"custom_tool:{tool.scope}",
                    )
                )
            except ValueError as error:
                log.warning("a custom tool is not usable: %s", error)
        return out

    def _handler(self, name: str) -> Any:
        async def handler(claims: RunTokenClaims, arguments: Mapping[str, Any]) -> str:
            return await self.call(claims, name, arguments)

        return handler

    async def call(self, claims: RunTokenClaims, name: str, arguments: Mapping[str, Any]) -> str:
        """Runs a custom tool for a run. The tool server has already checked the gate."""
        if not name.startswith(PREFIX):
            raise CustomToolError(f"no tool has the name {name}")
        scope = await self.run_scope(claims)
        async with self._engine.connect() as connection:
            tool = next(
                (t for t in await self.live(connection, scope) if t.tool_name == name), None
            )
        if tool is None:
            raise CustomToolError(f"no tool has the name {name}")
        try:
            text = await self._run(scope.org_id, tool.id, tool.definition, arguments, claims)
        except (ToolCallError, ArgumentError) as error:
            raise CustomToolError(f"{name} failed: {error}") from None
        return untrusted(text, source=f"custom_tool:{name}")

    async def _run(
        self,
        org_id: uuid.UUID,
        tool_id: uuid.UUID,
        definition: Definition,
        arguments: Mapping[str, Any],
        claims: RunTokenClaims | None,
    ) -> str:
        """Runs a definition. With ``claims`` a script runs in the session sandbox, without
        them in a new test sandbox."""
        check_arguments(definition.input_schema, arguments)
        if definition.http is not None:
            credential = None
            if definition.http.auth.type != "none":
                credential = await self._credential(org_id, tool_id)
            answer = await self._http.call(definition.http, arguments, credential)
            return answer.text
        if definition.script is None:  # pragma: no cover - a definition has one config
            raise ToolCallError("the tool has no script")
        if claims is None:
            if self.test_sandbox is None:  # pragma: no cover - checked by ``test``
                raise ToolCallError("this process has no sandbox for a test")
            sandbox = await self.test_sandbox(str(org_id))
            try:
                return await run_script(sandbox, definition.script, arguments)
            finally:
                try:
                    await sandbox.kill()
                except Exception:
                    log.warning("the test sandbox of a custom tool did not stop", exc_info=True)
        if self.session_sandbox is None:
            raise ToolCallError("script tools need a sandbox, and this process has none")
        session = await self.session_sandbox(claims.org_id, claims.session_id)
        if session is None:
            raise ToolCallError("the session has no sandbox yet. Use a shell or file tool first")
        return await run_script(session, definition.script, arguments)

    async def _credential(self, org_id: uuid.UUID, tool_id: uuid.UUID) -> Credential | None:
        if self._vault is None:
            return None
        secret = await self._vault.get(org_id, custom_tool_secret(tool_id))
        if secret is None:
            return None
        try:
            data = json.loads(secret.get_secret_value())
        except ValueError:
            return None
        return Credential.of(data) if isinstance(data, dict) else None

    async def _check_url(self, definition: Definition) -> None:
        if definition.http is None:  # pragma: no cover - only HTTP tools are checked
            return
        try:
            await self._http.check_url(definition.http.url)
        except ToolCallError as error:
            raise CustomToolError(str(error)) from None


_ADMIN_ONLY: Final = "only the owner of the tool or an admin can do this"
_SHARED_ADMIN_ONLY: Final = (
    "only an admin can add a workspace or org tool. Add the tool for yourself (scope user)"
)


def _check(definition: Definition, summary: str) -> None:
    try:
        check(definition)
        check_summary(summary)
    except DefinitionError as error:
        raise CustomToolError(str(error), status_code=422) from None


async def _next_version(connection: AsyncConnection, org_id: uuid.UUID, tool_id: uuid.UUID) -> int:
    top = (
        await connection.execute(
            select(func.max(CustomToolVersion.version_no)).where(
                CustomToolVersion.org_id == org_id, CustomToolVersion.tool_id == tool_id
            )
        )
    ).scalar_one()
    return int(top or 0) + 1


async def _audit(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    actor: uuid.UUID | None,
    action: str,
    tool_id: uuid.UUID,
    details: Mapping[str, Any],
) -> None:
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            workspace_id=workspace_id,
            actor_kind="user" if actor is not None else "system",
            actor_user_id=actor,
            action=action,
            target_type="custom_tool",
            target_id=str(tool_id),
            details=dict(details),
        )
    )
