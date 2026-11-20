"""Connections: the accounts and endpoints of connectors in a workspace (docs/architecture.md,
Section 10.3).

A connection has a ``scope``. A ``personal`` connection belongs to one user, and a
``workspace`` connection is shared by the members of the workspace. A run uses a connection
by these rules:

- a private session uses the personal connection of the run user, else the workspace
  connection;
- a shared session uses only the workspace connection. It never uses a personal connection,
  because the other people in the session must not act with the account of one user.

The states are ``pending``, ``initiated``, ``connected``, ``failed``, ``expired``, and
``disabled``. Only a backend check sets ``connected``: the Composio account status, or the
test request of a form connector. An ``initiated`` connection that does not finish its sign-in
in ``INITIATED_TTL`` becomes ``expired``.

Secrets never go through chat. ``connection_request`` and ``/connect`` give a link to the
Connections page of the web app. There, a Composio connector opens the Composio sign-in page,
and a form connector shows a form. The secret fields of a form go to the vault entry
``connection:<id>``. The row, the logs, and the API answers never hold them.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, cast
from urllib.parse import urlencode

from pydantic import SecretStr
from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.connectors.composio import (
    ACTIVE,
    EXPIRED_STATUSES,
    PENDING_STATUSES,
    ComposioClient,
    ComposioError,
)
from sama.connectors.errors import ConnectorError
from sama.connectors.http_api import (
    DriverError,
    DriverUnavailableError,
    FormDriver,
    HttpApiDriver,
)
from sama.connectors.manifest import Catalog, Manifest, Scope, ToolSpec
from sama.db.models import AuditEntry, Connection, Run, Session
from sama.tenancy.access import Grants
from sama.tenancy.authority import run_user
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.deferred import DeferredCatalog, DeferredTool
from sama.tools.output import untrusted
from sama.vault.crypto import VaultError
from sama.vault.store import OrgKeys, SecretName, Vault, connection_secret

log: Final = logging.getLogger("sama.connectors")

INITIATED_TTL: Final = timedelta(minutes=30)
"""An ``initiated`` connection becomes ``expired`` after this time without a finished sign-in."""
MAX_ERROR_CHARS: Final = 500
MAX_FIELD_CHARS: Final = 2_000
MAX_ANSWER_CHARS: Final = 200_000
CHAT_WARNING: Final = (
    "Do not send passwords, API keys, or tokens in a chat. Give them only on the sama web page."
)

CONNECTOR_TOOLSET: Final = "integrations"
"""The toolset of the connector tools. A profile grants it."""

State = Literal["pending", "initiated", "connected", "failed", "expired", "disabled"]


@dataclass(frozen=True, slots=True)
class ConnectionView:
    """A connection without secrets."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    connector: str
    kind: str
    scope: str
    owner_user_id: uuid.UUID | None
    state: str
    last_error: str | None
    last_health_at: datetime | None
    created_at: datetime
    updated_at: datetime
    fields: Mapping[str, str]
    """The non-secret form fields."""
    has_secret: bool

    @classmethod
    def of(cls, row: Any) -> ConnectionView:
        config = row.config or {}
        raw = config.get("fields")
        fields: dict[Any, Any] = raw if isinstance(raw, dict) else {}
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            connector=row.connector,
            kind=row.kind,
            scope=row.scope,
            owner_user_id=row.owner_user_id,
            state=row.state,
            last_error=row.last_error,
            last_health_at=row.last_health_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            fields={str(k): str(v) for k, v in fields.items()},
            has_secret=row.secret_ref is not None,
        )

    def public(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "workspace_id": str(self.workspace_id),
            "connector": self.connector,
            "kind": self.kind,
            "scope": self.scope,
            "owner_user_id": None if self.owner_user_id is None else str(self.owner_user_id),
            "state": self.state,
            "last_error": self.last_error,
            "last_health_at": _iso(self.last_health_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "fields": dict(self.fields),
            "has_secret": self.has_secret,
        }


@dataclass(frozen=True, slots=True)
class Started:
    """The answer to a new connection: the Composio sign-in link, or the form to fill."""

    connection: ConnectionView
    redirect_url: str | None

    def public(self) -> dict[str, Any]:
        return {"connection": self.connection.public(), "redirect_url": self.redirect_url}


@dataclass(frozen=True, slots=True)
class RunScope:
    """Who a run acts for, for the choice of its connections."""

    org_id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID | None
    shared: bool


def entity_id(org_id: uuid.UUID, scope: str, owner: uuid.UUID | None, workspace: uuid.UUID) -> str:
    """The Composio user id (entity): one for each user and one for each workspace."""
    if scope == "personal" and owner is not None:
        return f"sama-{org_id}-u-{owner}"
    return f"sama-{org_id}-w-{workspace}"


class ConnectionService:
    """The connections of all orgs. One for the process.

    - ``keys``: the Composio API key of an org (the vault entry, else the process key).
      Without a key, a Composio connector cannot connect.
    - ``vault``: the secret fields of form connectors. Without it, a form connector cannot
      connect.
    - ``web_base``: the address of the web app, for the links of ``/connect`` and
      ``connection_request`` and for the Composio callback.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        catalog: Catalog,
        *,
        keys: OrgKeys | None = None,
        vault: Vault | None = None,
        composio: ComposioClient | None = None,
        drivers: Mapping[str, FormDriver] | None = None,
        web_base: str | None = None,
    ) -> None:
        self._engine = engine
        self.catalog = catalog
        self._keys = keys
        self._vault = vault
        self._composio = composio or ComposioClient()
        self._drivers: dict[str, FormDriver] = (
            dict(drivers) if drivers is not None else {"http_api": HttpApiDriver()}
        )
        self.web_base = web_base.rstrip("/") if web_base else None

    async def aclose(self) -> None:
        await self._composio.aclose()
        for driver in self._drivers.values():
            close = getattr(driver, "aclose", None)
            if close is not None:
                await close()

    # -- links ------------------------------------------------------------------------------

    def connect_link(self, workspace_id: uuid.UUID, connector: str, scope: str) -> str | None:
        """The Connections page of the web app, with the connector to connect."""
        if self.web_base is None:
            return None
        query = urlencode({"workspace": str(workspace_id), "connect": connector, "scope": scope})
        return f"{self.web_base}/connections?{query}"

    def connect_reply(
        self, workspace_id: uuid.UUID, name: str, *, shared: bool, admin: bool
    ) -> str:
        """The chat answer to ``/connect <name>`` (Telegram) and ``/sama connect <name>``
        (Slack). It never asks for a secret in the chat."""
        name = name.strip().lower()
        if not name:
            names = ", ".join(m.name for m in self.catalog) or "none"
            return f"Write the connector after the command. Connectors: {names}."
        manifest = self.catalog.get(name)
        if manifest is None:
            names = ", ".join(m.name for m in self.catalog) or "none"
            return f"sama has no connector {name}. Connectors: {names}."
        scope = _default_scope(manifest, shared)
        if scope is None:
            return f"{manifest.title} cannot be connected in a shared chat."
        link = self.connect_link(workspace_id, manifest.name, scope)
        if link is None:
            return "The sama web app address is not set. Ask an admin."
        who = "for this workspace" if scope == "workspace" else "for you only"
        need = " A workspace admin must do it." if scope == "workspace" and not admin else ""
        return f"Open this link to connect {manifest.title} {who}:{need}\n{link}\n{CHAT_WARNING}"

    # -- the web app -----------------------------------------------------------------------

    async def list_connections(
        self, org_id: uuid.UUID, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[ConnectionView]:
        """The workspace connections and the personal connections of the user."""
        return await self.for_run(RunScope(org_id, workspace_id, user_id, shared=False))

    async def start(
        self,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        grants: Grants,
        connector: str,
        scope: Scope,
    ) -> Started:
        """Makes (or restarts) the connection of the user or the workspace. A Composio
        connector gets a new sign-in link. A form connector waits for its form."""
        manifest = self._manifest(connector)
        if scope not in manifest.scopes:
            raise ConnectorError(f"{manifest.title} cannot have a {scope} connection")
        if not grants.sees_workspace(workspace_id):
            raise ConnectorError("the workspace does not exist", status_code=404)
        if scope == "workspace" and not grants.admins_workspace(workspace_id):
            raise ConnectorError(
                "only a workspace admin or the org owner can connect for the workspace",
                status_code=403,
            )
        owner = grants.user_id if scope == "personal" else None
        try:
            row_id, old_account = await self._claim(
                org_id, workspace_id, grants.user_id, manifest, scope, owner
            )
        except IntegrityError:
            raise ConnectorError(
                "another change of this connection is running. Try again", status_code=409
            ) from None
        if manifest.kind != "composio":
            view = await self._set(org_id, row_id, state="pending", error=None)
            return Started(view, None)
        try:
            key = await self._composio_key(org_id)
            if self.web_base is None:
                raise ConnectorError("the sama web app address is not set", status_code=409)
            if manifest.composio is None:
                raise ConnectorError(f"{manifest.title} has no Composio toolkit", status_code=500)
            entity = entity_id(org_id, scope, owner, workspace_id)
            config_id = await self._composio.auth_config(key, manifest.composio.toolkit)
            callback = f"{self.web_base}/connections?" + urlencode(
                {"workspace": str(workspace_id), "connection": str(row_id)}
            )
            link = await self._composio.link(
                key, auth_config_id=config_id, user_id=entity, callback_url=callback
            )
        except ComposioError as error:
            await self._set(org_id, row_id, state="failed", error=str(error))
            raise ConnectorError(str(error), status_code=502) from None
        config = {"account_id": link.account_id, "entity": entity}
        view = await self._set(org_id, row_id, state="initiated", error=None, config=config)
        if old_account and old_account != link.account_id:
            await self._forget_account(org_id, old_account)
        return Started(view, link.redirect_url)

    async def _claim(
        self,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        actor: uuid.UUID,
        manifest: Manifest,
        scope: Scope,
        owner: uuid.UUID | None,
    ) -> tuple[uuid.UUID, str | None]:
        """The row of the connection (a new one if there is none), and its old Composio
        account."""
        async with self._engine.begin() as connection:
            row = await _find(connection, org_id, workspace_id, manifest.name, owner)
            old_account: str | None = None
            if row is None:
                row_id: uuid.UUID = (
                    await connection.execute(
                        insert(Connection)
                        .values(
                            org_id=org_id,
                            workspace_id=workspace_id,
                            scope=scope,
                            owner_user_id=owner,
                            connector=manifest.name,
                            kind=manifest.kind,
                            created_by_user_id=actor,
                        )
                        .returning(Connection.id)
                    )
                ).scalar_one()
            else:
                row_id = row.id
                account = (row.config or {}).get("account_id")
                old_account = account if isinstance(account, str) else None
            await _audit(connection, org_id, actor, "connection.started", row_id, scope)
        return row_id, old_account

    async def get(self, org_id: uuid.UUID, connection_id: uuid.UUID) -> ConnectionView | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(Connection).where(
                        Connection.org_id == org_id, Connection.id == connection_id
                    )
                )
            ).one_or_none()
        return None if row is None else ConnectionView.of(row)

    async def check(
        self, org_id: uuid.UUID, connection_id: uuid.UUID, *, actor: uuid.UUID | None
    ) -> ConnectionView:
        """Asks the backend for the state of the connection, and stores it. This is the only
        way to ``connected``."""
        async with self._engine.begin() as connection:
            await _expire_stale(connection, org_id)
        view = await self.get(org_id, connection_id)
        if view is None:
            raise ConnectorError("the connection does not exist", status_code=404)
        if view.state == "disabled":
            return view
        manifest = self._manifest(view.connector)
        if manifest.kind == "composio":
            state, error = await self._composio_state(org_id, view)
        else:
            state, error = await self._form_state(org_id, view, manifest)
        if view.state == "expired" and state == "initiated":
            # The link is older than ``INITIATED_TTL``. Only a new link starts the time again.
            state, error = "expired", view.last_error
        changed = state != view.state
        # Only a change moves ``updated_at``: an ``initiated`` connection expires 30 minutes
        # after its link, also when the web page checks it again and again.
        result = await self._write(
            org_id, view.id, state=state, error=error, health=True, touch=changed, expect=view
        )
        if result is None:
            # The connection changed while the backend answered (for example an admin disabled
            # it, or the user started it again). That change wins, and this answer is old.
            current = await self.get(org_id, view.id)
            if current is None:
                raise ConnectorError("the connection does not exist", status_code=404)
            return current
        if changed:
            async with self._engine.begin() as connection:
                await _audit(connection, org_id, actor, f"connection.{state}", view.id, view.scope)
        return result

    async def submit_form(
        self,
        org_id: uuid.UUID,
        connection_id: uuid.UUID,
        values: Mapping[str, str],
        *,
        actor: uuid.UUID,
    ) -> ConnectionView:
        """Stores the form of a form connector (the secret fields in the vault), then tests
        it. The values never go to a log or an answer."""
        view = await self.get(org_id, connection_id)
        if view is None:
            raise ConnectorError("the connection does not exist", status_code=404)
        manifest = self._manifest(view.connector)
        if manifest.kind == "composio":
            raise ConnectorError(f"{manifest.title} connects on the Composio page, not a form")
        if self._vault is None:
            raise ConnectorError("the vault is off in this process", status_code=409)
        known = {f.name for f in manifest.fields}
        if unknown := sorted(set(values) - known):
            raise ConnectorError(f"unknown fields: {', '.join(unknown)}")
        secrets: dict[str, str] = {}
        public: dict[str, str] = {}
        for spec in manifest.fields:
            value = (values.get(spec.name) or "").strip()
            if len(value) > MAX_FIELD_CHARS:
                raise ConnectorError(f"{spec.label} is too long")
            if not value:
                if spec.required:
                    raise ConnectorError(f"{spec.label} is required")
                continue
            (secrets if spec.secret else public)[spec.name] = value
        name = connection_secret(view.id)
        async with self._engine.begin() as connection:
            await self._vault.put(
                org_id, name, json.dumps(secrets), actor_user_id=actor, connection=connection
            )
            await connection.execute(
                update(Connection)
                .where(Connection.org_id == org_id, Connection.id == view.id)
                .values(
                    secret_ref=str(name),
                    config={"fields": public},
                    state="pending",
                    last_error=None,
                    updated_at=func.now(),
                )
            )
            await _audit(connection, org_id, actor, "connection.form_set", view.id, view.scope)
        return await self.check(org_id, view.id, actor=actor)

    async def disable(
        self, org_id: uuid.UUID, connection_id: uuid.UUID, *, actor: uuid.UUID
    ) -> ConnectionView:
        view = await self._set(org_id, connection_id, state="disabled", error=None)
        async with self._engine.begin() as connection:
            await _audit(connection, org_id, actor, "connection.disabled", view.id, view.scope)
        return view

    async def delete(
        self, org_id: uuid.UUID, connection_id: uuid.UUID, *, actor: uuid.UUID
    ) -> bool:
        """Deletes the row and its vault entry, then the Composio account."""
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    delete(Connection)
                    .where(Connection.org_id == org_id, Connection.id == connection_id)
                    .returning(Connection.config, Connection.secret_ref, Connection.scope)
                )
            ).one_or_none()
            if row is None:
                return False
            if row.secret_ref is not None and self._vault is not None:
                await self._vault.delete(
                    org_id,
                    connection_secret(connection_id),
                    actor_user_id=actor,
                    connection=connection,
                )
            await _audit(connection, org_id, actor, "connection.deleted", connection_id, row.scope)
        account = (row.config or {}).get("account_id")
        if account:
            await self._forget_account(org_id, str(account))
        return True

    # -- event triggers (Section 12.2) --------------------------------------------------------

    async def add_trigger(
        self, org_id: uuid.UUID, view: ConnectionView, slug: str, config: Mapping[str, Any]
    ) -> str:
        """Makes a Composio trigger instance of the type ``slug`` on the account of a connected
        Composio connection. Returns the id of the trigger instance."""
        if view.kind != "composio" or view.state != "connected":
            raise ConnectorError("a Composio trigger needs a connected Composio connection")
        account = await self._account(org_id, view.id)
        entity = await self._entity(org_id, view.id)
        if not account or not entity:
            raise ConnectorError("the connection is not complete")
        try:
            return await self._composio.upsert_trigger(
                await self._composio_key(org_id),
                slug,
                user_id=entity,
                account_id=account,
                config=config,
            )
        except ComposioError as error:
            client_error = error.status_code is not None and 400 <= error.status_code < 500
            raise ConnectorError(
                f"Composio did not make the trigger: {error}",
                status_code=422 if client_error else 502,
            ) from None

    async def remove_trigger(self, org_id: uuid.UUID, trigger_id: str) -> None:
        """Deletes a Composio trigger instance. A failure is logged: sama deleted its row, so an
        event of the instance finds no trigger and starts no run."""
        try:
            await self._composio.delete_trigger(await self._composio_key(org_id), trigger_id)
        except (ComposioError, ConnectorError) as error:
            log.warning("a Composio trigger delete failed org=%s: %s", org_id, error)

    async def subscribe_webhook(self, org_id: uuid.UUID, url: str) -> SecretStr:
        """Points the Composio webhook of the project of the org at ``url`` and returns its
        signing secret."""
        try:
            return await self._composio.webhook_subscription(await self._composio_key(org_id), url)
        except ComposioError as error:
            raise ConnectorError(
                f"Composio did not set the webhook: {error}", status_code=502
            ) from None

    def can_manage(self, view: ConnectionView, grants: Grants) -> bool:
        """The owner manages a personal connection. A workspace admin or the org owner
        manages a workspace connection."""
        if not grants.sees_workspace(view.workspace_id):
            return False
        if view.scope == "personal":
            return view.owner_user_id == grants.user_id
        return grants.admins_workspace(view.workspace_id)

    def can_see(self, view: ConnectionView, grants: Grants) -> bool:
        if not grants.sees_workspace(view.workspace_id):
            return False
        return view.scope == "workspace" or view.owner_user_id == grants.user_id

    # -- the agent -------------------------------------------------------------------------

    async def run_scope(self, claims: RunTokenClaims) -> RunScope:
        """The org, the workspace, the run user, and the audience of the run of the claims."""
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
            raise ConnectorError("the run does not exist")
        user = run_user(row.triggered_by_user_id, row.audience, row.owner_user_id)
        return RunScope(
            org_id=org_id,
            workspace_id=uuid.UUID(claims.workspace_id),
            user_id=user,
            shared=row.audience != "private",
        )

    async def for_run(self, scope: RunScope) -> list[ConnectionView]:
        """The connections that the run may use, in the order of use: in a private session the
        personal connections of the run user first, then the workspace connections. A shared
        session gets only the workspace connections."""
        owners = [Connection.scope == "workspace"]
        if not scope.shared and scope.user_id is not None:
            owners.append(Connection.owner_user_id == scope.user_id)
        async with self._engine.begin() as connection:
            await _expire_stale(connection, scope.org_id)
            rows = await connection.execute(
                select(Connection)
                .where(
                    Connection.org_id == scope.org_id,
                    Connection.workspace_id == scope.workspace_id,
                    or_(*owners),
                )
                .order_by(Connection.connector, Connection.scope, Connection.id)
            )
            return [ConnectionView.of(row) for row in rows]

    async def usable(self, scope: RunScope, connector: str) -> ConnectionView | None:
        """The connected connection that a run uses for the connector, or ``None``."""
        found = [v for v in await self.for_run(scope) if v.connector == connector]
        return _pick(found, connected=True)

    async def list_for_agent(self, claims: RunTokenClaims) -> str:
        """``connections_list``: each connector and its state for this run."""
        scope = await self.run_scope(claims)
        views = await self.for_run(scope)
        lines = []
        for manifest in self.catalog:
            mine = [v for v in views if v.connector == manifest.name]
            used = _pick(mine, connected=True) or _pick(mine, connected=False)
            if used is None:
                state = "not connected"
            else:
                state = f"{used.state} ({used.scope})"
                if used.state != "connected" and used.last_error:
                    state += f": {used.last_error}"
            reads = sum(1 for t in manifest.tools if t.access == "read")
            lines.append(
                f"- {manifest.name} ({manifest.title}): {state}. {manifest.description} "
                f"Tools: {len(manifest.tools)} ({reads} read only)."
            )
        if not lines:
            return "sama has no connectors."
        note = (
            "This is a shared session: only workspace connections are used."
            if scope.shared
            else "Your personal connection is used first, then the workspace connection."
        )
        return (
            "Connectors:\n"
            + "\n".join(lines)
            + f"\n{note}\nA connected connector has tools: find them with tool_search. "
            "To connect one, use connection_request."
        )

    async def request_for_agent(
        self, claims: RunTokenClaims, connector: str, scope: Scope | None
    ) -> str:
        """``connection_request``: a link to the web page that connects the connector. The
        agent never gets or gives a credential."""
        run = await self.run_scope(claims)
        manifest = self._manifest(connector.strip().lower())
        chosen = scope or _default_scope(manifest, run.shared)
        if chosen is None or chosen not in manifest.scopes:
            raise ConnectorError(f"{manifest.title} cannot have this kind of connection")
        if run.shared and chosen == "personal":
            raise ConnectorError(
                "a shared session cannot use a personal connection. Ask for a workspace "
                "connection, or connect in a private session"
            )
        link = self.connect_link(run.workspace_id, manifest.name, chosen)
        if link is None:
            raise ConnectorError("the sama web app address is not set. Ask an admin")
        who = "a workspace admin" if chosen == "workspace" else "the user"
        return (
            f"Give the user this link. {who.capitalize()} opens it and connects "
            f"{manifest.title} ({chosen} connection):\n{link}\n"
            "Do not ask for passwords, API keys, or tokens in the chat. Then use "
            "connection_test to check the connection."
        )

    async def test_for_agent(self, claims: RunTokenClaims, connector: str) -> str:
        """``connection_test``: checks the connection that the run would use."""
        run = await self.run_scope(claims)
        manifest = self._manifest(connector.strip().lower())
        mine = [v for v in await self.for_run(run) if v.connector == manifest.name]
        view = _pick(mine, connected=True) or _pick(mine, connected=False)
        if view is None:
            return f"{manifest.title} is not connected. Use connection_request."
        checked = await self.check(run.org_id, view.id, actor=run.user_id)
        text = f"{manifest.title} ({checked.scope} connection): {checked.state}."
        if checked.state != "connected":
            text += f" {checked.last_error or ''} Use connection_request to connect again."
        return text.strip()

    async def usable_tools(self, claims: RunTokenClaims, names: Collection[str]) -> frozenset[str]:
        """The names without the connector tools that the run cannot use now: a connector
        tool shows only with a connected connection that the run may use."""
        tools = {n: self.catalog.tool(n) for n in names}
        if not any(found is not None for found in tools.values()):
            return frozenset(names)
        try:
            scope = await self.run_scope(claims)
        except ConnectorError:
            return frozenset(n for n, found in tools.items() if found is None)
        connected = {v.connector for v in await self.for_run(scope) if v.state == "connected"}
        return frozenset(
            n for n, found in tools.items() if found is None or found[0].name in connected
        )

    async def call(self, claims: RunTokenClaims, name: str, arguments: Mapping[str, Any]) -> str:
        """Runs a connector tool with the connection that the run may use."""
        found = self.catalog.tool(name)
        if found is None:
            raise ConnectorError(f"no connector has the tool {name}")
        manifest, tool = found
        scope = await self.run_scope(claims)
        view = await self.usable(scope, manifest.name)
        if view is None:
            where = "workspace connection" if scope.shared else "connection"
            raise ConnectorError(
                f"{manifest.title} has no {where} that this session can use. "
                "Use connection_request."
            )
        if manifest.kind == "composio":
            text = await self._composio_call(scope.org_id, view, tool, arguments)
        else:
            text = await self._form_call(scope.org_id, view, manifest, tool, arguments)
        if len(text) > MAX_ANSWER_CHARS:
            text = text[:MAX_ANSWER_CHARS] + "\n[cut]"
        return untrusted(text, source=f"connector:{manifest.name}")

    def register_tools(self, catalog: DeferredCatalog) -> None:
        """Puts the tools of every connector in the deferred catalog, in the toolset
        ``integrations``. ``tool_search`` shows a tool only when the run has a connected
        connection for its connector (``usable_tools``)."""
        for manifest in self.catalog:
            for spec in manifest.tools:
                catalog.register(
                    DeferredTool(
                        name=spec.name,
                        description=f"{manifest.title}: {spec.description}",
                        toolset=CONNECTOR_TOOLSET,
                        handler=self._handler(spec.name),
                        input_schema=spec.input_schema,
                        tags=(manifest.name, manifest.title),
                        access=spec.access,
                    )
                )

    def _handler(self, name: str) -> Callable[[RunTokenClaims, Mapping[str, Any]], Awaitable[str]]:
        async def handler(claims: RunTokenClaims, arguments: Mapping[str, Any]) -> str:
            return await self.call(claims, name, arguments)

        return handler

    # -- the drivers -----------------------------------------------------------------------

    def _manifest(self, connector: str) -> Manifest:
        manifest = self.catalog.get(connector)
        if manifest is None:
            raise ConnectorError(f"sama has no connector {connector}", status_code=404)
        return manifest

    async def _composio_key(self, org_id: uuid.UUID) -> Any:
        try:
            key = (
                None
                if self._keys is None
                else await self._keys.get(org_id, SecretName.COMPOSIO_API_KEY)
            )
        except VaultError:
            log.error("the Composio key of org=%s does not decrypt", org_id)
            key = None
        if key is None:
            raise ConnectorError("Composio is not set up for this org", status_code=409)
        return key

    async def _composio_state(
        self, org_id: uuid.UUID, view: ConnectionView
    ) -> tuple[State, str | None]:
        account = await self._account(org_id, view.id)
        if not account:
            return "pending", None
        try:
            status = await self._composio.status(await self._composio_key(org_id), account)
        except (ComposioError, ConnectorError) as error:
            if isinstance(error, ComposioError) and error.status_code == 404:
                return "expired", "Composio does not know the account now. Connect again."
            # The check did not get an answer: the state stays, and the row keeps the error.
            return cast(State, view.state), str(error)
        if status == ACTIVE:
            return "connected", None
        if status in PENDING_STATUSES:
            return "initiated", None
        if status in EXPIRED_STATUSES:
            return "expired", f"Composio says the account is {status.lower()}. Connect again."
        return "failed", f"Composio says the account is {status.lower()}."

    async def _form_state(
        self, org_id: uuid.UUID, view: ConnectionView, manifest: Manifest
    ) -> tuple[State, str | None]:
        values = await self._form_values(org_id, view)
        if values is None:
            return "pending", None
        driver = self._drivers.get(manifest.kind)
        if driver is None:
            return "failed", f"this process cannot connect {manifest.kind} connectors"
        try:
            await driver.test(manifest, values)
        except DriverUnavailableError as error:
            # The check did not get an answer: the state stays, and the row keeps the error.
            return cast(State, view.state), str(error)
        except DriverError as error:
            return "failed", str(error)
        return "connected", None

    async def _form_values(self, org_id: uuid.UUID, view: ConnectionView) -> dict[str, str] | None:
        if not view.has_secret or self._vault is None:
            return None
        try:
            stored = await self._vault.get(org_id, connection_secret(view.id))
        except VaultError:
            log.error("the vault entry of connection=%s does not decrypt", view.id)
            return None
        if stored is None:
            return None
        try:
            secrets = json.loads(stored.get_secret_value())
        except ValueError:
            return None
        if not isinstance(secrets, dict):
            return None
        return {**view.fields, **{str(k): str(v) for k, v in secrets.items()}}

    async def _composio_call(
        self,
        org_id: uuid.UUID,
        view: ConnectionView,
        tool: ToolSpec,
        arguments: Mapping[str, Any],
    ) -> str:
        account = await self._account(org_id, view.id)
        entity = await self._entity(org_id, view.id)
        if not account or not entity or tool.slug is None:
            raise ConnectorError("the connection is not complete. Use connection_request")
        try:
            data = await self._composio.execute(
                await self._composio_key(org_id),
                tool.slug,
                account_id=account,
                user_id=entity,
                arguments=arguments,
            )
        except ComposioError as error:
            raise ConnectorError(f"{tool.name} failed: {error}") from None
        return json.dumps(data, ensure_ascii=False, default=str, indent=1)

    async def _form_call(
        self,
        org_id: uuid.UUID,
        view: ConnectionView,
        manifest: Manifest,
        tool: ToolSpec,
        arguments: Mapping[str, Any],
    ) -> str:
        values = await self._form_values(org_id, view)
        driver = self._drivers.get(manifest.kind)
        if values is None or driver is None:
            raise ConnectorError("the connection is not complete. Use connection_request")
        try:
            return await driver.call(manifest, tool, values, arguments)
        except DriverError as error:
            raise ConnectorError(f"{tool.name} failed: {error}") from None

    async def _config(self, org_id: uuid.UUID, connection_id: uuid.UUID) -> dict[str, Any]:
        async with self._engine.connect() as connection:
            config = (
                await connection.execute(
                    select(Connection.config).where(
                        Connection.org_id == org_id, Connection.id == connection_id
                    )
                )
            ).scalar_one_or_none()
        return config if isinstance(config, dict) else {}

    async def _account(self, org_id: uuid.UUID, connection_id: uuid.UUID) -> str | None:
        value = (await self._config(org_id, connection_id)).get("account_id")
        return value if isinstance(value, str) else None

    async def _entity(self, org_id: uuid.UUID, connection_id: uuid.UUID) -> str | None:
        value = (await self._config(org_id, connection_id)).get("entity")
        return value if isinstance(value, str) else None

    async def _forget_account(self, org_id: uuid.UUID, account: str) -> None:
        """Deletes a Composio account that sama does not use now. A failure is logged: the
        row is gone, so the account cannot be used."""
        try:
            await self._composio.delete(await self._composio_key(org_id), account)
        except (ComposioError, ConnectorError) as error:
            log.warning("a Composio account delete failed org=%s: %s", org_id, error)

    async def _set(
        self,
        org_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        state: State,
        error: str | None,
        config: Mapping[str, Any] | None = None,
    ) -> ConnectionView:
        view = await self._write(org_id, connection_id, state=state, error=error, config=config)
        if view is None:
            raise ConnectorError("the connection does not exist", status_code=404)
        return view

    async def _write(
        self,
        org_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        state: State,
        error: str | None,
        config: Mapping[str, Any] | None = None,
        health: bool = False,
        touch: bool = True,
        expect: ConnectionView | None = None,
    ) -> ConnectionView | None:
        """Sets the state, and returns ``None`` if the row is not there. With ``expect``, sets
        it only if the row did not change since ``expect`` was read."""
        values: dict[str, Any] = {
            "state": state,
            "last_error": None if error is None else error[:MAX_ERROR_CHARS],
        }
        if touch:
            values["updated_at"] = func.now()
        if config is not None:
            values["config"] = dict(config)
        if health:
            values["last_health_at"] = func.now()
        statement = update(Connection).where(
            Connection.org_id == org_id, Connection.id == connection_id
        )
        if expect is not None:
            statement = statement.where(
                Connection.state == expect.state, Connection.updated_at == expect.updated_at
            )
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(statement.values(**values).returning(Connection))
            ).one_or_none()
        return None if row is None else ConnectionView.of(row)


def _default_scope(manifest: Manifest, shared: bool) -> Scope | None:
    preferred: Scope = "workspace" if shared else "personal"
    if preferred in manifest.scopes:
        return preferred
    if shared:
        return None
    return manifest.scopes[0]


def _pick(views: list[ConnectionView], *, connected: bool) -> ConnectionView | None:
    """The personal connection first, then the workspace connection."""
    ranked = sorted(views, key=lambda v: 0 if v.scope == "personal" else 1)
    for view in ranked:
        if (view.state == "connected") == connected:
            return view
    return None


async def _find(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    connector: str,
    owner: uuid.UUID | None,
) -> Any:
    owner_clause = (
        Connection.owner_user_id.is_(None) if owner is None else Connection.owner_user_id == owner
    )
    return (
        await connection.execute(
            select(Connection.id, Connection.config)
            .where(
                Connection.org_id == org_id,
                Connection.workspace_id == workspace_id,
                Connection.connector == connector,
                owner_clause,
            )
            .with_for_update()
        )
    ).one_or_none()


async def _expire_stale(connection: AsyncConnection, org_id: uuid.UUID) -> None:
    await connection.execute(
        update(Connection)
        .where(
            Connection.org_id == org_id,
            Connection.state == "initiated",
            Connection.updated_at < func.now() - INITIATED_TTL,
        )
        .values(
            state="expired",
            last_error="The sign-in did not finish in 30 minutes. Connect again.",
            updated_at=func.now(),
        )
    )


async def _audit(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    actor: uuid.UUID | None,
    action: str,
    connection_id: uuid.UUID,
    scope: str,
) -> None:
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            actor_kind="user" if actor else "system",
            actor_user_id=actor,
            action=action,
            target_type="connection",
            target_id=str(connection_id),
            details={"scope": scope},
        )
    )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


__all__ = [
    "CHAT_WARNING",
    "CONNECTOR_TOOLSET",
    "INITIATED_TTL",
    "ConnectionService",
    "ConnectionView",
    "ConnectorError",
    "RunScope",
    "Started",
    "entity_id",
]
