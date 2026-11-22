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

Secrets never go through chat. Since M8.1, ``connection_request`` starts a sign-in connector
(Composio or MCP OAuth) and gives the sign-in link of the provider. A form connector (an API key,
a database) still gets a link to the form of the web app. The secret fields of a form go to the
vault entry ``connection:<id>``. The row, the logs, and the API answers never hold them. The
sign-in comes back to a page of the control plane, and a finished sign-in continues the session
that asked (``sama.connectors.chat``).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import urlencode, urlsplit

from pydantic import SecretStr, ValidationError
from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.connectors.api import TEST_METHODS, ApiDriver
from sama.connectors.chat import MAX_LINK_IN_TEXT, card, link_text, origin_of
from sama.connectors.composio import (
    ACTIVE,
    EXPIRED_STATUSES,
    PENDING_STATUSES,
    ComposioClient,
    ComposioError,
)
from sama.connectors.database import DatabaseGateway
from sama.connectors.dbsettings import writes_on
from sama.connectors.errors import ConnectorError, ConnectorUnknownError, short_error
from sama.connectors.host import ConnectorHosts, HostedServer
from sama.connectors.http_api import (
    DriverError,
    DriverUnavailableError,
    FormDriver,
    HttpApiDriver,
)
from sama.connectors.manifest import (
    CONNECTOR_NAME,
    ApiSpec,
    Catalog,
    Manifest,
    McpAuth,
    McpHosted,
    McpRemote,
    Scope,
    ToolSpec,
    api_fields,
    check_patterns,
    hosted_fields,
    mcp_fields,
    normalize_alias,
)
from sama.connectors.mcp_driver import McpDriver, McpNotReadyError, McpStateError
from sama.connectors.mcp_remote import (
    TOOL_PREFIX,
    ExposedTool,
    McpAuthError,
    McpError,
    McpUnavailableError,
    RemoteTool,
    exposed_tools,
    render_result,
    sama_name,
    selected,
    tool_access,
)
from sama.connectors.mcp_stdio import McpProcessEndedError
from sama.connectors.openapi import (
    ApiImportError,
    ApiOperation,
    Imported,
    parse_endpoints,
    parse_openapi,
)
from sama.db.models import AuditEntry, Connection, Membership, Run, Session, User
from sama.gate.decide import ToolAccess
from sama.integrations.guide import channel_guide, channel_name
from sama.tenancy.access import ADMIN_ROLES, Grants, load_grants
from sama.tenancy.authority import run_user
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.deferred import MAX_DESCRIPTION_CHARS as MAX_DEFERRED_DESCRIPTION
from sama.tools.deferred import DeferredCatalog, DeferredTool
from sama.tools.outbound import BlockedUrlError, FetchError
from sama.tools.output import untrusted
from sama.vault.crypto import VaultError
from sama.vault.redact import redact
from sama.vault.store import OrgKeys, SecretName, Vault, connection_secret

log: Final = logging.getLogger("sama.connectors")

INITIATED_TTL: Final = timedelta(minutes=30)
"""An ``initiated`` connection becomes ``expired`` after this time without a finished sign-in."""
RESUME_TTL: Final = timedelta(hours=2)
"""A sign-in that finishes later than this after ``connection_request`` does not continue the
session: the note would come too late to make sense."""
POLL_LIMIT: Final = 50
"""The most open chat sign-ins that one poll checks."""
GO_PATH: Final = "/connections/go"
"""A short link of the control plane to a long sign-in link (M8.1)."""
RETURN_PATH: Final = "/connections/return"
"""The page of the control plane where Composio sends the user after a sign-in (M8.1)."""
OAUTH_RETURN_PATH: Final = "/connections/oauth/return"
"""The OAuth redirect URI of the control plane for remote MCP servers (M8.1)."""
SIGN_IN_KINDS: Final = frozenset({"composio"})
"""Connector kinds whose sign-in is on a provider page (with ``mcp_remote`` and OAuth)."""


class Resumer(Protocol):
    """Continues the session that asked for a connection, once its sign-in connects (M8.1).

    ``queue`` runs in the transaction that removes the origin of the connection, so the note and
    its run exist exactly when the origin is gone: a crash between them loses nothing. It
    returns the run, or ``None`` when the session cannot take one (archived, over a quota).
    ``deliver`` runs after the commit and sends the run to the chat of the session (best
    effort: the web app shows the run anyway)."""

    async def queue(
        self,
        connection: AsyncConnection,
        org_id: uuid.UUID,
        view: ConnectionView,
        origin: Mapping[str, str],
    ) -> uuid.UUID | None: ...

    async def deliver(
        self, org_id: uuid.UUID, view: ConnectionView, origin: Mapping[str, str], run_id: uuid.UUID
    ) -> None: ...


MCP_STALE: Final = timedelta(hours=6)
"""A remote MCP connection reads its tool list again in the background after this time."""
MAX_ERROR_CHARS: Final = 500
MAX_FIELD_CHARS: Final = 8_000
"""A form field. A service account key (JSON) or a private key (PEM) is up to about 4,000."""
MAX_ANSWER_CHARS: Final = 200_000
CHAT_WARNING: Final = (
    "Do not send passwords, API keys, or tokens in a chat. Give them only on the sama web page."
)

API_PREFIX: Final = "api_"
"""The prefix of the tools of a custom API (Section 10.3.3)."""
CUSTOM_KEYS: Final[Mapping[str, str]] = {
    "mcp_remote": "mcp",
    "mcp_hosted": "hosted",
    "http_api": "api",
}
"""The config key of a custom connection, by kind."""
KIND_LABELS: Final[Mapping[str, str]] = {
    "mcp_remote": "remote MCP server",
    "mcp_hosted": "hosted MCP server",
    "http_api": "HTTP API",
}

CONNECTOR_TOOLSET: Final = "integrations"
"""The toolset of the connector tools. A profile grants it."""

State = Literal["pending", "initiated", "connected", "failed", "expired", "disabled"]


@dataclass(frozen=True, slots=True)
class McpTool:
    """A tool of a connection with a tool list (a remote or a hosted MCP server, or a custom
    API), under its sama name."""

    name: str
    remote: RemoteTool
    access: ToolAccess
    enabled: bool
    """``False`` if a filter of the connection drops it."""
    operation: ApiOperation | None = None
    """The request of a custom API tool."""


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
    config: Mapping[str, Any] = field(default_factory=dict)
    """The row config. It has no secret: the vault holds them."""
    tool_cache: Mapping[str, Any] | None = None
    """The tools of a remote MCP server from its last check."""

    @classmethod
    def of(cls, row: Any) -> ConnectionView:
        config = row.config or {}
        raw = config.get("fields")
        fields: dict[Any, Any] = raw if isinstance(raw, dict) else {}
        cache = row.tool_cache
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
            config=config if isinstance(config, dict) else {},
            tool_cache=cache if isinstance(cache, dict) else None,
        )

    @property
    def filters(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The include and the exclude patterns that the user set for the connection."""
        raw = self.config.get("filters")
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        return _strings(data.get("include")), _strings(data.get("exclude"))

    @property
    def has_tool_list(self) -> bool:
        """A remote or a hosted MCP server, or a custom API: the tools are in ``tool_cache``,
        not in a manifest."""
        if self.kind in ("mcp_remote", "mcp_hosted"):
            return True
        return self.kind == "http_api" and self.custom() is not None

    def custom(self) -> Mapping[str, Any] | None:
        """The stored spec of a connection that a person added (not in the catalog)."""
        key = CUSTOM_KEYS.get(self.kind)
        value = self.config.get(key) if key is not None else None
        return value if isinstance(value, Mapping) else None

    def mcp_tools(self) -> list[McpTool]:
        """The tools of the last check (or of the import of an API), in their order. The check
        applied the filters and the access of the manifest. ``enabled`` applies the filters
        of the connection."""
        cache = self.tool_cache or {}
        entries = cache.get("tools")
        if not self.has_tool_list or not isinstance(entries, list):
            return []
        api = self.kind == "http_api"
        include, exclude = self.filters
        out: list[McpTool] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            remote = RemoteTool.from_stored(entry)
            access = entry.get("access")
            if remote is None or access not in ("read", "write", "destructive"):
                continue
            operation = ApiOperation.from_stored(entry) if api else None
            if api and operation is None:
                continue
            name = sama_name(self.connector, remote.name, prefix=API_PREFIX if api else TOOL_PREFIX)
            if name is None or name in seen:
                continue
            seen.add(name)
            enabled = selected(remote.name, include, exclude)
            out.append(McpTool(name, remote, cast(ToolAccess, access), enabled, operation))
        return out

    def public(self, can_manage: bool | None = None) -> dict[str, Any]:
        """The view for the API. ``can_manage`` is whether the caller can change the connection
        (``ConnectionService.can_manage``). It is in the view only when the caller gives it."""
        out: dict[str, Any] = {
            "id": str(self.id),
            "workspace_id": str(self.workspace_id),
            "connector": self.connector,
            "kind": self.kind,
            "scope": self.scope,
            "owner_user_id": None if self.owner_user_id is None else str(self.owner_user_id),
            "state": self.state,
            # Redacted again: the vault can know a secret now that it did not know at write time.
            "last_error": None if self.last_error is None else redact(self.last_error),
            "last_health_at": _iso(self.last_health_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "fields": dict(self.fields),
            "has_secret": self.has_secret,
            "mcp": None,
        }
        if can_manage is not None:
            out["can_manage"] = can_manage
        if self.has_tool_list:
            include, exclude = self.filters
            cache = self.tool_cache or {}
            out["mcp"] = {
                "custom": _custom_public(self.kind, self.custom()),
                "include": list(include),
                "exclude": list(exclude),
                "discovered_at": cache.get("discovered_at"),
                "skipped": cache.get("skipped"),
                "tools": [
                    {
                        "name": tool.name,
                        "remote_name": tool.remote.name,
                        "description": _short(tool.remote.description),
                        "access": tool.access,
                        "enabled": tool.enabled,
                        "method": None if tool.operation is None else tool.operation.method,
                        "path": None if tool.operation is None else tool.operation.path,
                    }
                    for tool in self.mcp_tools()
                ],
            }
        return out


@dataclass(frozen=True, slots=True)
class Started:
    """The answer to a new connection: the Composio sign-in link, or the form to fill."""

    connection: ConnectionView
    redirect_url: str | None

    def public(self, can_manage: bool | None = None) -> dict[str, Any]:
        return {
            "connection": self.connection.public(can_manage),
            "redirect_url": self.redirect_url,
        }


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
    - ``database``: the gateway of the ``database`` connectors (Section 10.4). It also tests
      a database connection.
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
        mcp: McpDriver | None = None,
        hosts: ConnectorHosts | None = None,
        api: ApiDriver | None = None,
        web_base: str | None = None,
        database: DatabaseGateway | None = None,
        return_base: str | None = None,
    ) -> None:
        self._engine = engine
        self.catalog = catalog
        self._keys = keys
        self._vault = vault
        self._composio = composio or ComposioClient()
        self._drivers: dict[str, FormDriver] = (
            dict(drivers) if drivers is not None else {"http_api": HttpApiDriver()}
        )
        self._database = database or DatabaseGateway()
        self._drivers.setdefault("database", self._database)
        self._mcp = mcp or McpDriver(vault)
        self._hosts = hosts
        """The connector hosts of stdio MCP servers. ``None`` without an E2B key."""
        self._api = api or ApiDriver()
        self.web_base = web_base.rstrip("/") if web_base else None
        self.return_base = return_base.rstrip("/") if return_base else None
        """The public address of the control plane, for the sign-in return pages (M8.1).
        Without it, a sign-in comes back to the Connections page of the web app."""
        self.on_connected: Resumer | None = None
        """Continues the session that asked for a connection, when its sign-in connects."""
        self._refreshing: set[uuid.UUID] = set()
        self._background: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        for task in list(self._background):
            task.cancel()
        await self._composio.aclose()
        await self._mcp.aclose()
        await self._api.aclose()
        if self._hosts is not None:
            await self._hosts.aclose()
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
        *,
        custom: Manifest | None = None,
        tool_cache: Mapping[str, Any] | None = None,
        via: Literal["web", "chat"] = "web",
        origin: Mapping[str, str] | None = None,
    ) -> Started:
        """Makes (or restarts) the connection of the user or the workspace. A Composio
        connector gets a new sign-in link. A form connector waits for its form. A remote MCP
        server gets an OAuth sign-in link, waits for its form, or connects at once.

        ``custom`` is the manifest of a connection that is not in the catalog (``add_mcp``,
        ``add_stdio``, ``add_api``), and ``tool_cache`` the operations of a custom API.
        Without it, a name that is not in the catalog restarts the custom
        connection of the user or the workspace with that name.

        ``via`` is where the sign-in started: after a ``web`` sign-in the return page sends the
        browser to the Connections page; after a ``chat`` sign-in it shows a page that says
        \"go back to the chat\". ``origin`` (``session_id``, ``run_id``, ``user_id``) is the
        session that asked: when the sign-in connects, ``on_connected`` continues it (M8.1).

        A backend that is missing (no Composio key, no vault, no E2B for a stdio server) gives a
        ``409`` before sama writes a row, so no connection stays ``pending``."""
        owner = grants.user_id if scope == "personal" else None
        manifest = custom or self.catalog.get(connector)
        if manifest is None:
            manifest = await self._stored_custom(org_id, workspace_id, connector, owner)
        if scope not in manifest.scopes:
            raise ConnectorError(f"{manifest.title} cannot have a {scope} connection")
        if not grants.sees_workspace(workspace_id):
            raise ConnectorError("the workspace does not exist", status_code=404)
        if scope == "workspace" and not grants.admins_workspace(workspace_id):
            raise ConnectorError(
                "only a workspace admin or the org owner can connect for the workspace",
                status_code=403,
            )
        if problem := await self.availability(org_id, manifest):
            raise ConnectorError(problem, status_code=409)
        token = secrets.token_urlsafe(24)
        try:
            row_id, old_account = await self._claim(
                org_id, workspace_id, grants.user_id, manifest, scope, owner, token
            )
        except IntegrityError:
            raise ConnectorError(
                "another change of this connection is running. Try again", status_code=409
            ) from None
        extra: dict[str, Any] = {"via": via, "by": str(grants.user_id), "return_token": token}
        if origin:
            extra["origin"] = {**origin, "at": datetime.now(UTC).isoformat()}
        try:
            return await self._start_kind(
                org_id,
                workspace_id,
                row_id,
                manifest,
                scope,
                owner,
                grants.user_id,
                old_account,
                tool_cache=tool_cache,
                extra=extra,
            )
        except ConnectorError as error:
            await self._fail_start(org_id, row_id, str(error), extra["return_token"])
            raise
        except Exception:
            await self._fail_start(
                org_id, row_id, "The connection did not start. Try again.", extra["return_token"]
            )
            raise

    async def _fail_start(
        self, org_id: uuid.UUID, row_id: uuid.UUID, text: str, token: str
    ) -> None:
        """A start that failed leaves ``failed`` with the reason, never a ``pending`` row that
        the page cannot explain. Only the row of this start changes: a row whose config has the
        token of another start (a newer start that won) stays as it is."""
        current = await self.get(org_id, row_id)
        if current is None or current.state not in ("pending", "initiated"):
            return
        if current.config.get("return_token") != token:
            return
        await self._write(org_id, row_id, state="failed", error=text, expect=current)

    async def _start_kind(
        self,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        row_id: uuid.UUID,
        manifest: Manifest,
        scope: Scope,
        owner: uuid.UUID | None,
        actor: uuid.UUID,
        old_account: str | None,
        *,
        tool_cache: Mapping[str, Any] | None,
        extra: Mapping[str, Any],
    ) -> Started:
        if manifest.kind == "mcp_remote":
            return await self._mcp_start(org_id, row_id, manifest, actor=actor, extra=extra)
        if manifest.kind == "mcp_hosted" or manifest.api is not None:
            return await self._tools_start(
                org_id, row_id, manifest, actor=actor, tool_cache=tool_cache
            )
        if manifest.kind != "composio":
            view = await self._set(org_id, row_id, state="pending", error=None)
            return Started(view, None)
        try:
            key = await self._composio_key(org_id)
            if manifest.composio is None:
                raise ConnectorError(f"{manifest.title} has no Composio toolkit", status_code=500)
            entity = entity_id(org_id, scope, owner, workspace_id)
            config_id = await self._composio.auth_config(key, manifest.composio.toolkit)
            link = await self._composio.link(
                key,
                auth_config_id=config_id,
                user_id=entity,
                callback_url=self._composio_return(workspace_id, row_id, extra),
            )
        except ComposioError as error:
            raise ConnectorError(str(error), status_code=502) from None
        config = {
            "account_id": link.account_id,
            "entity": entity,
            **extra,
            "sign_in_url": link.redirect_url,
        }
        view = await self._set(org_id, row_id, state="initiated", error=None, config=config)
        if old_account and old_account != link.account_id:
            await self._forget_account(org_id, old_account)
        return Started(view, link.redirect_url)

    def _composio_return(
        self, workspace_id: uuid.UUID, row_id: uuid.UUID, extra: Mapping[str, Any]
    ) -> str:
        """Where Composio sends the browser after the sign-in: the return page of the control
        plane, else (no public address) the Connections page of the web app."""
        if self.return_base is not None:
            query = {"connection": str(row_id), "t": str(extra["return_token"])}
            return f"{self.return_base}{RETURN_PATH}?{urlencode(query)}"
        if self.web_base is None:
            raise ConnectorError("the sama web app address is not set", status_code=409)
        return f"{self.web_base}/connections?" + urlencode(
            {"workspace": str(workspace_id), "connection": str(row_id)}
        )

    async def availability(self, org_id: uuid.UUID, manifest: Manifest) -> str | None:
        """Why the connector cannot connect in this org now (a backend is missing), in plain
        words for the user, or ``None``. ``connections_list``, ``connection_request``, the
        Connections page, and ``start`` use it (M8.1)."""
        if manifest.kind == "composio":
            try:
                await self._composio_key(org_id)
            except ConnectorError:
                return (
                    f"{manifest.title} signs in through Composio, and Composio is not set up for"
                    " this org. An org owner or admin adds the Composio API key in Settings >"
                    " Org keys."
                )
            if self.return_base is None and self.web_base is None:
                return "The sama web app address is not set. Ask the operator of sama."
            return None
        if manifest.kind == "mcp_hosted" and self._hosts is None:
            return (
                f"{manifest.title} runs as a hosted MCP server, and hosted servers are off in"
                " this sama (no E2B key)."
            )
        oauth = manifest.mcp is not None and manifest.mcp.auth == "oauth"
        if oauth and self.return_base is None and self.web_base is None:
            return "The sama web app address is not set. Ask the operator of sama."
        if manifest.fields and self._vault is None:
            return f"{manifest.title} needs a form, and the vault is off in this sama."
        return None

    async def _claim(
        self,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        actor: uuid.UUID,
        manifest: Manifest,
        scope: Scope,
        owner: uuid.UUID | None,
        token: str,
    ) -> tuple[uuid.UUID, str | None]:
        """The row of the connection (a new one if there is none), and its old Composio
        account. The row takes the return token of this start at once, so a later failure of
        an older start can see that a newer start owns the row (M8.1)."""
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
                            config={"return_token": token},
                        )
                        .returning(Connection.id)
                    )
                ).scalar_one()
            else:
                stored = row.config or {}
                custom = any(isinstance(stored.get(k), dict) for k in CUSTOM_KEYS.values())
                if custom != manifest.custom or (custom and row.kind != manifest.kind):
                    raise ConnectorError(
                        f"a connection with the name {manifest.name} already exists",
                        status_code=409,
                    )
                row_id = row.id
                account = (row.config or {}).get("account_id")
                old_account = account if isinstance(account, str) else None
                await connection.execute(
                    update(Connection)
                    .where(Connection.org_id == org_id, Connection.id == row_id)
                    .values(config={**stored, "return_token": token})
                )
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
        manifest = self._manifest_of(view)
        cache: dict[str, Any] | None = None
        if manifest.kind == "composio":
            state, error = await self._composio_state(org_id, view)
        elif manifest.kind == "mcp_remote":
            state, error, cache = await self._mcp_state(org_id, view, manifest)
        elif manifest.kind == "mcp_hosted":
            state, error, cache = await self._hosted_state(org_id, view, manifest)
        elif manifest.api is not None:
            state, error = await self._api_state(org_id, view, manifest)
        else:
            state, error = await self._form_state(org_id, view, manifest)
        if view.state == "expired" and state == "initiated":
            # The link is older than ``INITIATED_TTL``. Only a new link starts the time again.
            state, error = "expired", view.last_error
        changed = state != view.state
        # Only a change moves ``updated_at``: an ``initiated`` connection expires 30 minutes
        # after its link, also when the web page checks it again and again.
        result = await self._write(
            org_id,
            view.id,
            state=state,
            error=error,
            health=True,
            touch=changed,
            expect=view,
            tool_cache=cache,
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
        if result.state == "connected" and origin_of(result.config) is not None:
            await self._continue(org_id, result)
        return result

    async def _continue(self, org_id: uuid.UUID, view: ConnectionView) -> None:
        """Takes the origin of a connection that connected and continues its session once
        (M8.1). Under a lock of the row, the row must still be ``connected`` and belong to the
        start of ``view`` (the same return token), so an old answer cannot take the origin of a
        newer sign-in. The note and its run go in the same transaction as the removal of the
        origin. A failure keeps the origin, and the next check or poll tries again."""
        resumer = self.on_connected
        if resumer is None:
            return
        token = view.config.get("return_token")
        try:
            run_id, current, origin = await self._take_origin(org_id, view, token, resumer)
        except Exception:
            # The transaction rolled back: the origin stays, and the next check or poll retries.
            log.exception("the session of connection=%s did not continue", view.id)
            return
        if run_id is not None and origin is not None and current is not None:
            try:
                await resumer.deliver(org_id, current, origin, run_id)
            except Exception:
                log.exception("the chat of connection=%s does not follow run=%s", view.id, run_id)

    async def _take_origin(
        self, org_id: uuid.UUID, view: ConnectionView, token: object, resumer: Resumer
    ) -> tuple[uuid.UUID | None, ConnectionView | None, dict[str, str] | None]:
        """Removes the origin and queues the note in one transaction (``_continue``)."""
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(Connection.config, Connection.state)
                    .where(Connection.org_id == org_id, Connection.id == view.id)
                    .with_for_update()
                )
            ).one_or_none()
            stored = (row.config or {}) if row is not None else {}
            origin = origin_of(stored)
            if row is None or origin is None or row.state != "connected":
                return None, None, None
            if stored.get("return_token") != token:
                return None, None, None  # Another start owns the row now.
            config = {k: v for k, v in stored.items() if k != "origin"}
            await connection.execute(
                update(Connection)
                .where(Connection.org_id == org_id, Connection.id == view.id)
                .values(config=config)
            )
            current = replace_config(view, config)
            run_id = None
            if _fresh(origin):
                run_id = await resumer.queue(connection, org_id, current, origin)
        return run_id, current, origin

    async def sign_in_link(self, connection_id: uuid.UUID, token: str) -> str:
        """The provider sign-in link of an open sign-in, for the short link
        (``GET /connections/go``). The token of the start is the proof."""
        found = await self._by_id(connection_id)
        config = {} if found is None else found[1].config
        expected = config.get("return_token")
        url = config.get("sign_in_url")
        if (
            found is None
            or not isinstance(expected, str)
            or not hmac.compare_digest(expected.encode(), token.encode())
            or found[1].state != "initiated"
            or not isinstance(url, str)
            or not url.startswith("https://")
        ):
            raise ConnectorError(INVALID_RETURN, status_code=403)
        return url

    async def finish_return(
        self, connection_id: uuid.UUID, token: str
    ) -> tuple[ConnectionView, str]:
        """The Composio return page (``GET /connections/return``): checks the connection when
        the token matches the one of its last start. The page needs no sama session: the token
        is the proof, and a check only reads the state from Composio. Returns the connection and
        where the sign-in started (``web`` or ``chat``)."""
        found = await self._by_id(connection_id)
        expected = None if found is None else found[1].config.get("return_token")
        if (
            found is None
            or not isinstance(expected, str)
            or not hmac.compare_digest(expected.encode(), token.encode())
        ):
            # One answer for an unknown connection and a wrong token: the page tells nothing.
            raise ConnectorError(INVALID_RETURN, status_code=403)
        org_id, view = found
        checked = await self.check(org_id, view.id, actor=_starter(view))
        return checked, _via(view)

    async def finish_oauth_return(
        self, *, state: str, code: str | None, error: str | None
    ) -> tuple[ConnectionView, str]:
        """The OAuth return page of a remote MCP server (``GET /connections/oauth/return``).
        The state is ``<connection id>.<random>``. ``finish_oauth`` compares the whole state
        with the stored one and uses it and the PKCE verifier once, so a forged or a replayed
        return changes nothing."""
        try:
            connection_id = uuid.UUID(state.split(".", 1)[0])
        except ValueError:
            raise ConnectorError(INVALID_RETURN, status_code=400) from None
        found = await self._by_id(connection_id)
        actor = None if found is None else _starter(found[1])
        if found is None or actor is None:
            raise ConnectorError(INVALID_RETURN, status_code=400)
        org_id, view = found
        # The proof first: before the state matches, the page says nothing about the connection
        # and changes nothing.
        if not await self._mcp.state_matches(org_id, view.id, state):
            raise ConnectorError(INVALID_RETURN, status_code=400)
        done = await self.finish_oauth(
            org_id, view.id, state=state, code=code, error=error, actor=actor
        )
        return done, _via(view)

    async def _by_id(self, connection_id: uuid.UUID) -> tuple[uuid.UUID, ConnectionView] | None:
        """A connection of any org, by its id (only for the return pages)."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(select(Connection).where(Connection.id == connection_id))
            ).one_or_none()
        return None if row is None else (row.org_id, ConnectionView.of(row))

    async def poll_signins(self) -> int:
        """Checks the open Composio sign-ins that a chat started, so a sign-in connects and
        continues its session also when the browser never reaches the return page. Returns
        how many it checked."""
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(Connection.org_id, Connection.id)
                    .where(
                        Connection.config.has_key("origin"),
                        or_(
                            and_(
                                Connection.state == "initiated",
                                Connection.kind.in_(SIGN_IN_KINDS),
                                Connection.updated_at > func.now() - INITIATED_TTL,
                            ),
                            # A connected row that still has its origin: its resume failed.
                            and_(
                                Connection.state == "connected",
                                Connection.updated_at > func.now() - RESUME_TTL,
                            ),
                        ),
                    )
                    # The row checked longest ago first: a check sets ``last_health_at``, so
                    # open sign-ins that nobody finishes do not hold the batch.
                    .order_by(Connection.last_health_at.asc().nulls_first(), Connection.id)
                    .limit(POLL_LIMIT)
                )
            ).all()
        for row in rows:
            try:
                await self.check(row.org_id, row.id, actor=None)
            except ConnectorError as problem:
                log.info("a sign-in poll of connection=%s: %s", row.id, problem)
            except Exception:
                log.exception("a sign-in poll of connection=%s failed", row.id)
        return len(rows)

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
        manifest = self._manifest_of(view)
        if manifest.kind == "composio":
            raise ConnectorError(f"{manifest.title} connects on the Composio page, not a form")
        if not manifest.fields:
            raise ConnectorError(f"{manifest.title} has no form")
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
                    # A remote MCP connection keeps its filters and its custom server.
                    config={**view.config, "fields": public},
                    state="pending",
                    last_error=None,
                    updated_at=func.now(),
                )
            )
            await _audit(connection, org_id, actor, "connection.form_set", view.id, view.scope)
        self._mcp.forget(view.id)
        self._api.forget(view.id)
        if self._hosts is not None and view.kind == "mcp_hosted":
            await self._hosts.restart(str(view.id))  # The server gets the new values.
        return await self.check(org_id, view.id, actor=actor)

    async def disable(
        self, org_id: uuid.UUID, connection_id: uuid.UUID, *, actor: uuid.UUID
    ) -> ConnectionView:
        view = await self._set(org_id, connection_id, state="disabled", error=None)
        async with self._engine.begin() as connection:
            await _audit(connection, org_id, actor, "connection.disabled", view.id, view.scope)
        return view

    async def enable(
        self, org_id: uuid.UUID, connection_id: uuid.UUID, *, actor: uuid.UUID
    ) -> ConnectionView:
        """Turns a ``disabled`` connection on again. The connection leaves ``disabled`` and the
        backend then checks it, so the state that the caller gets is the state of the backend
        (it can stay ``pending`` or ``failed``). Raises ``ConnectorError`` with ``409`` when the
        connection is not ``disabled``."""
        view = await self.get(org_id, connection_id)
        if view is None:
            raise ConnectorError("the connection does not exist", status_code=404)
        if view.state != "disabled":
            raise ConnectorError("the connection is not disabled", status_code=409)
        # ``expect`` makes the write fail when another request changed the row in the meantime.
        moved = await self._write(org_id, view.id, state="pending", error=None, expect=view)
        if moved is None:
            raise ConnectorError("the connection changed: try again", status_code=409)
        async with self._engine.begin() as connection:
            await _audit(connection, org_id, actor, "connection.enabled", view.id, view.scope)
        return await self.check(org_id, view.id, actor=actor)

    async def delete(
        self, org_id: uuid.UUID, connection_id: uuid.UUID, *, actor: uuid.UUID
    ) -> bool:
        """Deletes the row and its vault entry, then the Composio account."""
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    delete(Connection)
                    .where(Connection.org_id == org_id, Connection.id == connection_id)
                    .returning(
                        Connection.config, Connection.secret_ref, Connection.scope, Connection.kind
                    )
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
        self._mcp.forget(connection_id)
        self._api.forget(connection_id)
        if self._hosts is not None and row.kind == "mcp_hosted":
            try:
                await self._hosts.stop(str(connection_id))
            except Exception as error:  # the row is gone; the host pauses and expires
                log.warning("the host of connection=%s did not stop: %s", connection_id, error)
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
        async with self._engine.begin() as connection:
            await _expire_stale(connection, scope.org_id)
            return await _rows_for(connection, scope)

    async def usable(self, scope: RunScope, connector: str) -> ConnectionView | None:
        """The connected connection that a run uses for the connector, or ``None``."""
        found = [v for v in await self.for_run(scope) if v.connector == connector]
        return _pick(found, connected=True)

    async def list_for_agent(self, claims: RunTokenClaims) -> str:
        """``connections_list``: each connector and its state for this run, the role of the
        user, and why a connector cannot connect now (M8.1)."""
        scope = await self.run_scope(claims)
        views = await self.for_run(scope)
        grants = await self._grants(scope)
        lines = []
        for manifest in self._manifests_for(views):
            mine = [v for v in views if v.connector == manifest.name]
            used = _pick(mine, connected=True) or _pick(mine, connected=False)
            problem: str | None = None
            if used is None or used.state != "connected":
                problem = await self.availability(scope.org_id, manifest)
            if problem is not None:
                state = f"not available: {problem}"
            elif used is None:
                state = "not connected"
            else:
                state = f"{used.state} ({used.scope})"
                if used.state != "connected" and used.last_error:
                    state += f": {used.last_error}"
            via = _VIA.get(manifest.kind, "")
            if manifest.kind in ("mcp_remote", "mcp_hosted") or manifest.api is not None:
                tools = [t for t in used.mcp_tools() if t.enabled] if used else []
                if used is None or used.state != "connected":
                    count = "Tools: the server lists them after the connection"
                else:
                    reads = sum(1 for t in tools if t.access == "read")
                    count = f"Tools: {len(tools)} ({reads} read only)"
                kind = KIND_LABELS[manifest.kind]
                if manifest.custom:
                    kind += (
                        " added by URL" if manifest.kind == "mcp_remote" else " added by an admin"
                    )
                lines.append(
                    f"- {manifest.name} ({manifest.title}, {kind}): {state}. "
                    f"{manifest.description} {count}."
                )
                continue
            reads = sum(1 for t in manifest.tools if t.access == "read")
            lines.append(
                f"- {manifest.name} ({manifest.title}{via}): {state}. {manifest.description} "
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
            + f"\n{note}\n{_role_line(grants, scope.workspace_id)}\n"
            "Composio runs the sign-in of the connectors marked 'via Composio'. A connected "
            "connector has tools: find them with tool_search. To connect one, call "
            "connection_request: it starts the sign-in and gives a Connect button. To add a "
            "remote MCP server by its URL, call connection_add_mcp. Chat channels (people talk "
            "to sama there) are slack_app, telegram, whatsapp, and email_channel."
        )

    async def request_for_agent(
        self, claims: RunTokenClaims, connector: str, scope: Scope | None
    ) -> str:
        """``connection_request``: starts the connection and gives the sign-in link of the
        provider with a connect card (M8.1). A form connector gets the link of its form on the
        web page. The agent never gets or gives a credential."""
        run = await self.run_scope(claims)
        views = await self.for_run(run)
        manifest, choices = self._agent_resolve(connector, views)
        if manifest is None:
            if choices:
                names = ", ".join(f"{m.title} ({m.name})" for m in choices)
                return (
                    f"'{connector}' can mean: {names}. Ask the user which one, then call "
                    "connection_request again with that name."
                )
            names = ", ".join(m.name for m in self._manifests_for(views)) or "none"
            raise ConnectorError(
                f"sama has no connector {connector.strip().lower()}. Connectors: {names}. To "
                "add a remote MCP server, use connection_add_mcp with its URL",
                status_code=404,
            )
        chosen = scope or _default_scope(manifest, run.shared)
        if chosen is None or chosen not in manifest.scopes:
            raise ConnectorError(f"{manifest.title} cannot have this kind of connection")
        if run.shared and chosen == "personal":
            raise ConnectorError(
                "a shared session cannot use a personal connection. Ask for a workspace "
                "connection, or connect in a private session"
            )
        if run.user_id is None:
            raise ConnectorError("this run has no user, so it cannot start a sign-in")
        if chosen == "workspace" and scope is None and _signs_in(manifest):
            # The gate asks a person for a workspace connection only when the call says so.
            return (
                f"{manifest.title} would be a workspace connection (for all members). Call "
                "connection_request again with scope 'workspace': a person approves it first."
            )
        grants = await self._grants(run)
        if chosen == "workspace" and (
            grants is None or not grants.admins_workspace(run.workspace_id)
        ):
            admins = await self._admin_names(run.org_id)
            await self._audit_request(run, manifest.name, chosen)
            who = ", ".join(admins) if admins else "a workspace admin or the org owner"
            return (
                f"The user cannot connect {manifest.title} for the whole workspace: only a "
                f"workspace admin or the org owner can. Tell the user to ask {who}. sama recorded "
                "the request in the audit log. In a private session the user can connect it "
                "for themselves only (scope personal)."
            )
        if problem := await self.availability(run.org_id, manifest):
            return f"{manifest.title} cannot connect now. {problem} Tell the user this."
        current = next(
            (v for v in views if v.connector == manifest.name and v.scope == chosen), None
        )
        if current is not None and current.state == "connected":
            return (
                f"{manifest.title} is already connected ({chosen} connection). Its tools are "
                "in tool_search: use them now."
            )
        if not _signs_in(manifest):
            return self._form_reply(run, manifest, chosen, current)
        assert grants is not None  # noqa: S101 - run.user_id is set, so grants are loaded
        origin = {
            "session_id": claims.session_id,
            "run_id": claims.run_id,
            "user_id": str(run.user_id),
        }
        started = await self.start(
            run.org_id,
            run.workspace_id,
            grants,
            manifest.name,
            chosen,
            via="chat",
            origin=origin,
        )
        return self._started_reply(manifest, chosen, started)

    def _form_reply(
        self,
        run: RunScope,
        manifest: Manifest,
        scope: Scope,
        current: ConnectionView | None,
    ) -> str:
        """A form connector: the link to its form on the web page. Secrets never go in chat."""
        link = self.connect_link(run.workspace_id, manifest.name, scope)
        if link is None:
            raise ConnectorError("the sama web app address is not set. Ask an admin")
        who = "A workspace admin" if scope == "workspace" else "The user"
        connection_id = "" if current is None else str(current.id)
        # The card goes first: the preview of a long answer keeps its start (M8.1).
        line = card(
            connector=manifest.name,
            title=manifest.title,
            url=link,
            connection_id=connection_id,
            state="form",
            scope=scope,
        )
        return (
            line
            + f"\n{manifest.title} needs a form with a key or a password. {who} fills it on the "
            f"sama web page. The user sees a Connect {manifest.title} button; also give this "
            f"link in one short line: {link_text(link, line)}\nDo not ask for passwords, API "
            "keys, or tokens in the chat. After the user saves the form, call connection_test."
        )

    def _started_reply(self, manifest: Manifest, scope: Scope, started: Started) -> str:
        view = started.connection
        title = manifest.title
        url = started.redirect_url
        token = view.config.get("return_token")
        if (
            url is not None
            and len(url) > MAX_LINK_IN_TEXT
            and self.return_base is not None
            and isinstance(token, str)
        ):
            # A long provider link goes through a short link of the control plane, so the card
            # and the text stay short and the button always works (M8.1).
            query = urlencode({"connection": str(view.id), "t": token})
            url = f"{self.return_base}{GO_PATH}?{query}"
            started = Started(view, url)
        if url is None and view.state == "pending" and manifest.fields:
            url = self.connect_link(view.workspace_id, manifest.name, scope)
        # The card goes first: the preview of a long answer keeps its start (M8.1).
        line = card(
            connector=manifest.name,
            title=title,
            url=url if view.state != "connected" else None,
            connection_id=str(view.id),
            state="initiated" if started.redirect_url is not None else view.state,
            scope=scope,
        )
        if started.redirect_url is not None:
            text = (
                f"The sign-in of {title} ({scope} connection) is open. The user sees a Connect "
                f"{title} button; also give this link in one short line: "
                f"{link_text(started.redirect_url, line)}\nWhen the user finishes the sign-in, "
                f"sama connects {title} and continues this request by itself in a new turn. Do "
                "not ask the user to tell you when they are done. Stop here and wait."
            )
        elif view.state == "connected":
            tools = [t for t in view.mcp_tools() if t.enabled]
            count = f" It has {len(tools)} tools." if view.has_tool_list else ""
            text = (
                f"{title} is connected now ({scope} connection).{count} Find its tools with "
                "tool_search and continue the request."
            )
        elif view.state == "pending" and manifest.fields:
            text = (
                f"{title} needs a form with a key. The user fills it on the sama web page: "
                f"{link_text(url or '', line)}\nDo not ask for secrets in the chat."
            )
        else:
            text = (
                f"{title} did not connect: {view.last_error or view.state}. Tell the user, and "
                "do not try again in a loop."
            )
        return line + "\n" + text

    async def add_mcp_for_agent(
        self,
        claims: RunTokenClaims,
        *,
        url: str,
        name: str | None,
        title: str | None,
        auth: McpAuth,
        scope: Scope | None,
    ) -> str:
        """``connection_add_mcp``: adds a remote MCP server by its URL from the chat (M8.1).
        The gate asks a person first. Only a workspace admin or the org owner can add one. An
        OAuth server gets a sign-in link; a server without sign-in connects and lists its
        tools; a server with a key gets the link of its form."""
        run = await self.run_scope(claims)
        if run.user_id is None:
            raise ConnectorError("this run has no user, so it cannot add a server")
        grants = await self._grants(run)
        if grants is None or not grants.admins_workspace(run.workspace_id):
            admins = await self._admin_names(run.org_id)
            await self._audit_request(run, f"mcp:{_host(url)}", scope or "workspace")
            who = ", ".join(admins) if admins else "a workspace admin or the org owner"
            return (
                "Only a workspace admin or the org owner can add an MCP server, because the "
                f"server gets the calls of the agents. Tell the user to ask {who}. sama recorded "
                "the request in the audit log."
            )
        chosen: Scope = scope or ("workspace" if run.shared else "personal")
        if run.shared and chosen == "personal":
            raise ConnectorError("a shared session cannot add a personal connection")
        server = (name or "").strip().lower() or _server_name(url)
        if self.catalog.get(server) is not None:
            server = f"{server}-mcp"[:40]
        for manifest in self.catalog:
            if manifest.mcp is not None and manifest.mcp.url.rstrip("/") == url.strip().rstrip("/"):
                # A catalog server: connect it from the catalog, with its tested settings.
                return await self.request_for_agent(claims, manifest.name, chosen)
        started = await self.add_mcp(
            run.org_id,
            run.workspace_id,
            grants,
            name=server,
            url=url,
            auth=auth,
            header=None,
            oauth_scope=None,
            title=title,
            scope=chosen,
            via="chat",
            origin={
                "session_id": claims.session_id,
                "run_id": claims.run_id,
                "user_id": str(run.user_id),
            },
        )
        manifest = self._manifest_of(started.connection)
        return self._started_reply(manifest, chosen, started)

    async def channel_for_agent(self, claims: RunTokenClaims, connector: str) -> str | None:
        """``connection_request`` with a chat channel (``slack_app``, ``telegram``,
        ``whatsapp...``, ``email_channel``): the steps for an org owner or admin, else the
        admins to ask. ``None`` when ``connector`` is not a channel (Section 11.9, M8.1)."""
        if channel_name(connector) is None:
            return None
        run = await self.run_scope(claims)
        grants = await self._grants(run)
        admin = grants is not None and grants.org_admin
        admins: tuple[str, ...] = ()
        if not admin:
            admins = tuple(await self._admin_names(run.org_id))
            await self._audit_request(run, f"channel:{channel_name(connector)}", "org")
        return channel_guide(connector, self.web_base, admin=admin, admins=admins)

    async def _grants(self, run: RunScope) -> Grants | None:
        if run.user_id is None:
            return None
        async with self._engine.connect() as connection:
            return await load_grants(connection, run.org_id, run.user_id)

    async def _admin_names(self, org_id: uuid.UUID, limit: int = 5) -> list[str]:
        """The names of the org owner and the org admins, for a member to ask."""
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                select(User.display_name)
                .join(
                    Membership,
                    and_(Membership.org_id == User.org_id, Membership.user_id == User.id),
                )
                .where(
                    User.org_id == org_id,
                    Membership.workspace_id.is_(None),
                    Membership.role.in_(ADMIN_ROLES),
                )
                .order_by(Membership.role.desc(), User.display_name)
                .limit(limit)
            )
            return [str(name) for (name,) in rows if name]

    async def _audit_request(self, run: RunScope, connector: str, scope: str) -> None:
        """A member asked for a connection that only an admin can make (M8.1)."""
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=run.org_id,
                    workspace_id=run.workspace_id,
                    actor_kind="user" if run.user_id else "system",
                    actor_user_id=run.user_id,
                    action="connection.requested",
                    target_type="connector",
                    target_id=connector,
                    details={"scope": scope},
                )
            )

    async def test_for_agent(self, claims: RunTokenClaims, connector: str) -> str:
        """``connection_test``: checks the connection that the run would use."""
        run = await self.run_scope(claims)
        views = await self.for_run(run)
        manifest = self._agent_manifest(connector, views)
        mine = [v for v in views if v.connector == manifest.name]
        view = _pick(mine, connected=True) or _pick(mine, connected=False)
        if view is None:
            return f"{manifest.title} is not connected. Use connection_request."
        checked = await self.check(run.org_id, view.id, actor=run.user_id)
        text = f"{manifest.title} ({checked.scope} connection): {checked.state}."
        if checked.state == "connected" and checked.has_tool_list:
            tools = [t for t in checked.mcp_tools() if t.enabled]
            text += f" The server has {len(tools)} tools for sama: find them with tool_search."
        if checked.state == "initiated":
            text += " The sign-in is still open: sama continues by itself when it finishes."
        elif checked.state != "connected":
            text += f" {checked.last_error or ''} Use connection_request to connect again."
        return text.strip()

    def _agent_resolve(
        self, connector: str, views: list[ConnectionView]
    ) -> tuple[Manifest | None, list[Manifest]]:
        """The connector that the agent (or the user) means: a name, a title, or an alias such
        as ``email`` or ``calendar`` (M8.1). Several connectors with the alias: ``(None,
        choices)``."""
        name = connector.strip().lower()
        for manifest in self._manifests_for(views):
            if manifest.name == name:
                return manifest, []
        found = self.catalog.resolve(connector)
        if len(found) == 1:
            return found[0], []
        if found:
            return None, found
        squashed = normalize_alias(connector).replace(" ", "")
        for manifest in self._manifests_for(views):
            if manifest.custom and manifest.name.replace("-", "").replace("_", "") == squashed:
                return manifest, []
        return None, []

    def _manifests_for(self, views: list[ConnectionView]) -> list[Manifest]:
        """The catalog, then the servers that admins added by URL to the connections of the
        run."""
        out = list(self.catalog)
        names = {m.name for m in out}
        for view in views:
            if view.custom() is not None and view.connector not in names:
                try:
                    out.append(self._manifest_of(view))
                except ConnectorError as error:
                    log.warning("a connection is skipped: %s", error)
                    continue
                names.add(view.connector)
        return out

    def _agent_manifest(self, connector: str, views: list[ConnectionView]) -> Manifest:
        name = connector.strip().lower()
        for manifest in self._manifests_for(views):
            if manifest.name == name:
                return manifest
        raise ConnectorError(f"sama has no connector {name}", status_code=404)

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
        views = await self.for_run(scope)
        connected = {v.connector for v in views if v.state == "connected"}

        def usable(found: tuple[Manifest, ToolSpec] | None) -> bool:
            if found is None:
                return True
            manifest, tool = found
            if manifest.name not in connected:
                return False
            if manifest.operation(tool) == "execute":
                # The write tool of a database shows only when the connection allows writes.
                used = _pick([v for v in views if v.connector == manifest.name], connected=True)
                return used is not None and writes_on(used.fields)
            return True

        return frozenset(n for n, found in tools.items() if usable(found))

    async def call(self, claims: RunTokenClaims, name: str, arguments: Mapping[str, Any]) -> str:
        """Runs a connector tool with the connection that the run may use."""
        found = self.catalog.tool(name)
        if found is None:
            raise ConnectorError(f"no connector has the tool {name}")
        text = await self.call_as(await self.run_scope(claims), name, arguments, claims=claims)
        return untrusted(text, source=f"connector:{found[0].name}")

    async def call_as(
        self,
        scope: RunScope,
        name: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        claims: RunTokenClaims | None = None,
    ) -> str:
        """Runs a connector tool for a scope, with the connection that the scope may use. The
        answer is clipped and not marked: the caller marks it. Raises ``ConnectorUnknownError``
        when the result is not known. A workflow tool step calls this without ``claims``
        (Section 12.3). A database tool needs the claims of a run: it saves files in the
        session of the run."""
        found = self.catalog.tool(name)
        if found is None:
            raise ConnectorError(f"no connector has the tool {name}")
        manifest, tool = found
        if manifest.kind == "database" and claims is None:
            raise ConnectorError(
                f"{name} is a database tool, and a workflow tool step cannot call it. "
                "Use an agent step."
            )
        view = await self.usable(scope, manifest.name)
        if view is None:
            where = "workspace connection" if scope.shared else "connection"
            raise ConnectorError(
                f"{manifest.title} has no {where} that this session can use. "
                "Use connection_request."
            )
        if manifest.kind == "composio":
            text = await self._composio_call(scope.org_id, view, tool, arguments)
        elif manifest.kind == "database" and claims is not None:
            text = await self._database_call(claims, scope, view, manifest, tool, arguments)
        else:
            text = await self._form_call(
                scope.org_id, view, manifest, tool, arguments, idempotency_key=idempotency_key
            )
        if len(text) > MAX_ANSWER_CHARS:
            text = text[:MAX_ANSWER_CHARS] + "\n[cut]"
        return text

    def tool_access(self, name: str) -> str | None:
        """The access of a connector tool (``read``, ``write``, ``destructive``), or ``None``
        when no connector has the tool."""
        found = self.catalog.tool(name)
        return None if found is None else found[1].access

    def idempotent(self, name: str) -> bool:
        """The tool takes an idempotency key: an ``http_api`` connector with
        ``http.idempotency_header``. A Composio tool has no key."""
        found = self.catalog.tool(name)
        if found is None:
            return False
        manifest = found[0]
        # A Composio manifest has no ``http`` section.
        return manifest.http is not None and manifest.http.idempotency_header is not None

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

    # -- remote MCP servers ----------------------------------------------------------------

    async def add_mcp(
        self,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        grants: Grants,
        *,
        name: str,
        url: str,
        auth: McpAuth,
        header: str | None,
        oauth_scope: str | None,
        title: str | None,
        scope: Scope,
        via: Literal["web", "chat"] = "web",
        origin: Mapping[str, str] | None = None,
    ) -> Started:
        """Adds a remote MCP server that is not in the catalog, by its URL. Only a workspace
        admin or the org owner can add one: the server gets the calls of the agents."""
        _require_admin(grants, workspace_id, "add an MCP server by URL")
        name = self._custom_name(name)
        try:
            spec = McpRemote(url=url.strip(), auth=auth, header=header, scope=oauth_scope)
            manifest = _custom_manifest(name, (title or "").strip() or name, scope, spec)
        except ValidationError as error:
            raise _invalid(error) from None
        await self._check_url(spec.url)
        return await self.start(
            org_id, workspace_id, grants, name, scope, custom=manifest, via=via, origin=origin
        )

    async def add_stdio(
        self,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        grants: Grants,
        *,
        name: str,
        title: str | None,
        scope: Scope,
        package: Mapping[str, Any] | None,
        command: list[str] | None,
        env: list[str],
        egress: list[str],
        memory_mb: int | None = None,
        idle_s: int | None = None,
    ) -> Started:
        """Adds a stdio MCP server that sama runs in its own connector host (Section 10.3.2).
        A workspace admin can add a pinned package with its default program and no arguments.
        Only the org owner can add a custom command, or a package with a ``bin`` or ``args``,
        because a program name such as ``python`` or ``bash`` with arguments runs any code.
        Each ``env`` name becomes a secret field of the form, and the server gets the value as
        that environment variable."""
        _require_admin(grants, workspace_id, "add a stdio MCP server")
        if not grants.org_owner:
            if command is not None:
                raise ConnectorError(
                    "only the org owner can add a stdio MCP server with a custom command",
                    status_code=403,
                )
            if package is not None and (package.get("bin") or package.get("args")):
                raise ConnectorError(
                    "only the org owner can add a stdio MCP server with a custom bin or args",
                    status_code=403,
                )
        if self._hosts is None:
            raise ConnectorError(
                "this sama cannot run stdio MCP servers: E2B is not set up", status_code=409
            )
        name = self._custom_name(name)
        try:
            hosted = McpHosted.model_validate(
                {
                    "package": package,
                    "command": command,
                    "env": {variable: variable.lower() for variable in env},
                    "egress": egress,
                    **({} if memory_mb is None else {"memory_mb": memory_mb}),
                    **({} if idle_s is None else {"idle_s": idle_s}),
                }
            )
            manifest = _hosted_manifest(name, (title or "").strip() or name, scope, hosted)
        except ValidationError as error:
            raise _invalid(error) from None
        return await self.start(org_id, workspace_id, grants, name, scope, custom=manifest)

    async def add_api(
        self,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        grants: Grants,
        *,
        name: str,
        title: str | None,
        scope: Scope,
        auth: Mapping[str, Any],
        base_url: str | None = None,
        openapi: str | None = None,
        openapi_url: str | None = None,
        endpoints: list[Mapping[str, Any]] | None = None,
        test: str | None = None,
    ) -> Started:
        """Adds an HTTP API from an OpenAPI 3 document (the text or its URL) or a list of
        endpoints (Section 10.3.3). Each operation becomes a tool ``api_<name>_<tool>``. A
        ``GET`` tool reads, so the gate allows it. The other tools ask first."""
        _require_admin(grants, workspace_id, "add an HTTP API")
        name = self._custom_name(name)
        if sum(x is not None for x in (openapi, openapi_url, endpoints)) != 1:
            raise ConnectorError(
                "give one of: an OpenAPI document, its URL, or a list of endpoints",
                status_code=422,
            )
        max_name = 64 - len(f"{API_PREFIX}{name.replace('-', '_')}_")
        try:
            imported: Imported
            if endpoints is not None:
                imported = parse_endpoints(endpoints, max_name=max_name)
            else:
                if openapi is None:
                    try:
                        openapi = await self._api.fetch_document(str(openapi_url))
                    except DriverError as error:
                        raise ConnectorError(str(error), status_code=422) from None
                imported = parse_openapi(openapi, max_name=max_name)
        except ApiImportError as error:
            raise ConnectorError(str(error), status_code=422) from None
        if not imported.operations:
            raise ConnectorError("the import has no operations that sama can use", status_code=422)
        url = (base_url or "").strip() or imported.base_url
        if not url:
            raise ConnectorError(
                "give the base URL: the document has no https server", status_code=422
            )
        try:
            spec = ApiSpec.model_validate({"base_url": url, "auth": auth, "test": test})
            manifest = _api_manifest(name, (title or "").strip() or name, scope, spec)
        except ValidationError as error:
            raise _invalid(error) from None
        if test is not None:
            chosen = next((op for op in imported.operations if op.tool.name == test), None)
            if chosen is None:
                raise ConnectorError(f"the import has no operation {test}", status_code=422)
            # A check runs without approval, so the test operation must only read.
            if chosen.method not in TEST_METHODS:
                raise ConnectorError(
                    f"the test operation {test} must be a GET or HEAD operation", status_code=422
                )
        await self._check_url(spec.base_url, api=True)
        if spec.auth.token_url is not None:
            await self._check_url(spec.auth.token_url, api=True)
        cache = {
            "discovered_at": datetime.now(UTC).isoformat(),
            "skipped": imported.skipped,
            "tools": [
                {**op.stored(), "access": tool_access(op.tool, {})} for op in imported.operations
            ],
        }
        return await self.start(
            org_id, workspace_id, grants, name, scope, custom=manifest, tool_cache=cache
        )

    def _custom_name(self, name: str) -> str:
        name = name.strip().lower()
        if CONNECTOR_NAME.fullmatch(name) is None:
            raise ConnectorError(
                "the name has 1 to 40 lower-case letters, digits, _ and -, and starts with a "
                "letter",
                status_code=422,
            )
        if self.catalog.get(name) is not None:
            raise ConnectorError(
                f"{name} is a catalog connector. Connect it from the catalog", status_code=409
            )
        return name

    async def finish_oauth(
        self,
        org_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        state: str,
        code: str | None,
        error: str | None,
        actor: uuid.UUID,
    ) -> ConnectionView:
        """The end of an OAuth sign-in: the web app sends the ``code`` and the ``state`` that
        the authorization server gave in the redirect. The state is used once."""
        async with self._engine.begin() as connection:
            await _expire_stale(connection, org_id)
        view = await self.get(org_id, connection_id)
        if view is None:
            raise ConnectorError("the connection does not exist", status_code=404)
        manifest = self._manifest_of(view)
        if manifest.mcp is None or manifest.mcp.auth != "oauth":
            raise ConnectorError(f"{manifest.title} does not use OAuth")
        if view.state != "initiated":
            raise ConnectorError(
                "the sign-in of this connection is not open. Connect again", status_code=409
            )

        async def claim() -> bool:
            # Only one callback can move the row out of ``initiated``: the row must be the
            # one that was read above (state and updated_at).
            won = await self._write(org_id, view.id, state="pending", error=None, expect=view)
            return won is not None

        try:
            await self._mcp.finish_oauth(
                org_id,
                view.id,
                manifest.mcp,
                view.config,
                state=state,
                code=None if error else code,
                actor=actor,
                claim=claim,
            )
        except McpStateError as problem:
            raise ConnectorError(str(problem), status_code=400) from None
        except McpError as problem:
            await self._fail(org_id, view, actor, str(problem))
            raise ConnectorError(str(problem), status_code=502) from None
        if error or not code:
            reason = (error or "no code")[:200]
            return await self._fail(org_id, view, actor, f"The sign-in did not finish: {reason}.")
        return await self.check(org_id, view.id, actor=actor)

    async def _fail(
        self, org_id: uuid.UUID, view: ConnectionView, actor: uuid.UUID, text: str
    ) -> ConnectionView:
        """Sets the state ``failed`` with the error, and audits it."""
        done = await self._set(org_id, view.id, state="failed", error=text)
        async with self._engine.begin() as connection:
            await _audit(connection, org_id, actor, "connection.failed", view.id, view.scope)
        return done

    async def set_filters(
        self,
        org_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        include: tuple[str, ...],
        exclude: tuple[str, ...],
        actor: uuid.UUID,
    ) -> ConnectionView:
        """Sets the tools of a remote MCP connection that runs can use, by name patterns
        (``*`` and ``?``). A tool must match an include pattern (if there is one) and no
        exclude pattern. The change applies to the next tool search, also in a running
        session."""
        try:
            check_patterns(include)
            check_patterns(exclude)
        except ValueError as error:
            raise ConnectorError(str(error), status_code=422) from None
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(Connection.kind, Connection.config, Connection.scope)
                    .where(Connection.org_id == org_id, Connection.id == connection_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise ConnectorError("the connection does not exist", status_code=404)
            if row.kind not in CUSTOM_KEYS:
                raise ConnectorError("only a connection with a tool list has tool filters")
            config = dict(row.config or {})
            config["filters"] = {"include": list(include), "exclude": list(exclude)}
            await connection.execute(
                update(Connection)
                .where(Connection.org_id == org_id, Connection.id == connection_id)
                .values(config=config)
            )
            await _audit(
                connection, org_id, actor, "connection.filters_set", connection_id, row.scope
            )
        view = await self.get(org_id, connection_id)
        if view is None:  # pragma: no cover - the row was there a moment ago
            raise ConnectorError("the connection does not exist", status_code=404)
        return view

    async def mcp_tools(self, claims: RunTokenClaims) -> list[DeferredTool]:
        """The tools of the connected remote MCP servers that the run may use, as deferred
        tools of the toolset ``integrations``. The list is read for each search, so a new
        server or a new tool is usable at once, also in a running session."""
        try:
            scope = await self.run_scope(claims)
        except ConnectorError:
            return []
        tools: list[DeferredTool] = []
        for view, tool in self._mcp_index(await self.for_run(scope), refresh_org=scope.org_id):
            title = self._mcp_title(view)
            description = f"{title} ({KIND_LABELS[view.kind]}): {tool.remote.description}"
            try:
                tools.append(
                    DeferredTool(
                        name=tool.name,
                        description=description[:MAX_DEFERRED_DESCRIPTION],
                        toolset=CONNECTOR_TOOLSET,
                        handler=self._mcp_handler(tool.name),
                        input_schema=tool.remote.input_schema,
                        tags=(view.connector, title, tool.remote.name),
                        access=tool.access,
                        source=f"connector:{view.connector}",
                    )
                )
            except ValueError as error:
                log.warning("a remote MCP tool is not usable: %s", error)
        return tools

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
        """The toolsets and the access of the remote MCP tools of a run, for the tool gate.
        It reads in the transaction of the gate."""
        if CONNECTOR_TOOLSET not in toolsets:
            return {}, {}
        views = await _rows_for(connection, RunScope(org_id, workspace_id, user_id, shared))
        index = self._mcp_index(views)
        return (
            {CONNECTOR_TOOLSET: frozenset(tool.name for _, tool in index)},
            {tool.name: tool.access for _, tool in index},
        )

    async def call_mcp(
        self, claims: RunTokenClaims, name: str, arguments: Mapping[str, Any]
    ) -> str:
        """Runs a remote MCP tool with the connection that the run may use now."""
        scope = await self.run_scope(claims)
        found = next(
            ((v, t) for v, t in self._mcp_index(await self.for_run(scope)) if t.name == name),
            None,
        )
        if found is None:
            raise ConnectorError(
                f"no connected MCP server of this session has the tool {name}. Use tool_search"
            )
        view, tool = found
        manifest = self._manifest_of(view)
        if view.kind == "mcp_hosted":
            text = await self._hosted_call(scope.org_id, view, manifest, tool, arguments)
        elif view.kind == "http_api":
            text = await self._api_call(scope.org_id, view, manifest, tool, arguments)
        else:
            text = await self._remote_call(scope.org_id, view, manifest, tool, arguments)
        if len(text) > MAX_ANSWER_CHARS:
            text = text[:MAX_ANSWER_CHARS] + "\n[cut]"
        return untrusted(text, source=f"connector:{view.connector}")

    async def _remote_call(
        self,
        org_id: uuid.UUID,
        view: ConnectionView,
        manifest: Manifest,
        tool: McpTool,
        arguments: Mapping[str, Any],
    ) -> str:
        spec = _mcp_spec(manifest)
        name = tool.name
        try:
            return await self._mcp.call(
                org_id, view.id, spec, view.config, tool.remote.name, arguments
            )
        except (McpAuthError, McpNotReadyError) as error:
            state: State = "expired" if spec.auth == "oauth" else "failed"
            await self._write(org_id, view.id, state=state, error=str(error))
            raise ConnectorError(
                f"{manifest.title} refused the credential: {error}. Use connection_request to "
                "connect again"
            ) from None
        except McpError as error:
            raise ConnectorError(f"{name} failed: {error}") from None

    async def _hosted_call(
        self,
        org_id: uuid.UUID,
        view: ConnectionView,
        manifest: Manifest,
        tool: McpTool,
        arguments: Mapping[str, Any],
    ) -> str:
        server = await self._server(org_id, view, manifest)
        if server is None or self._hosts is None:
            raise ConnectorError("the connection is not complete. Use connection_request")
        try:
            return render_result(await self._hosts.call(server, tool.remote.name, arguments))
        except McpError as error:
            raise ConnectorError(f"{tool.name} failed: {error}") from None

    async def _api_call(
        self,
        org_id: uuid.UUID,
        view: ConnectionView,
        manifest: Manifest,
        tool: McpTool,
        arguments: Mapping[str, Any],
    ) -> str:
        values = await self._values(org_id, view, manifest)
        if values is None or manifest.api is None or tool.operation is None:
            raise ConnectorError("the connection is not complete. Use connection_request")
        try:
            return await self._api.call(view.id, manifest.api, tool.operation, values, arguments)
        except DriverError as error:
            raise ConnectorError(f"{tool.name} failed: {error}") from None

    def _mcp_handler(
        self, name: str
    ) -> Callable[[RunTokenClaims, Mapping[str, Any]], Awaitable[str]]:
        async def handler(claims: RunTokenClaims, arguments: Mapping[str, Any]) -> str:
            return await self.call_mcp(claims, name, arguments)

        return handler

    def _mcp_index(
        self, views: list[ConnectionView], *, refresh_org: uuid.UUID | None = None
    ) -> list[tuple[ConnectionView, McpTool]]:
        """The enabled tools of the remote MCP connections that a run uses: for each
        connector, the personal connection first, then the workspace connection. A stale
        tool list starts a check in the background.

        Two connections can give the same sama name (``a-b`` and ``a_b``, or the connector
        ``a`` with the tool ``b_c`` and the connector ``a_b`` with the tool ``c``). Such a
        name is ambiguous, so no connection gets it: a name in a policy or an approval always
        means one tool of one connection."""
        found: list[tuple[ConnectionView, McpTool]] = []
        for connector in sorted({v.connector for v in views if v.has_tool_list}):
            view = _pick([v for v in views if v.connector == connector], connected=True)
            if view is None or not view.has_tool_list:
                continue
            # An imported HTTP API has a fixed tool list, so only MCP servers refresh.
            if refresh_org is not None and view.kind in ("mcp_remote", "mcp_hosted"):
                self._refresh_if_stale(refresh_org, view)
            found.extend((view, tool) for tool in view.mcp_tools() if tool.enabled)
        owners: dict[str, set[uuid.UUID]] = {}
        for view, tool in found:
            owners.setdefault(tool.name, set()).add(view.id)
        ambiguous = {name for name, ids in owners.items() if len(ids) > 1}
        if ambiguous:
            log.warning("remote MCP tools with an ambiguous name are hidden: %s", sorted(ambiguous))
        return [(view, tool) for view, tool in found if tool.name not in ambiguous]

    def _refresh_if_stale(self, org_id: uuid.UUID, view: ConnectionView) -> None:
        if view.id in self._refreshing:
            return
        checked = view.last_health_at
        if checked is not None and datetime.now(UTC) - checked < MCP_STALE:
            return
        self._refreshing.add(view.id)

        async def refresh() -> None:
            try:
                await self.check(org_id, view.id, actor=None)
            except Exception as error:  # a background check must not stop a run
                log.warning("a background check of connection=%s failed: %s", view.id, error)
            finally:
                self._refreshing.discard(view.id)

        task = asyncio.get_running_loop().create_task(refresh())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _mcp_title(self, view: ConnectionView) -> str:
        custom = view.custom()
        if custom is not None:
            return str(custom.get("title") or view.connector)
        manifest = self.catalog.get(view.connector)
        return manifest.title if manifest is not None else view.connector

    async def _mcp_start(
        self,
        org_id: uuid.UUID,
        row_id: uuid.UUID,
        manifest: Manifest,
        *,
        actor: uuid.UUID,
        extra: Mapping[str, Any] | None = None,
    ) -> Started:
        spec = _mcp_spec(manifest)
        old = await self._config(org_id, row_id)
        config: dict[str, Any] = {k: v for k, v in old.items() if k in ("filters", "fields")}
        # ``via`` and the token of the return page. The origin only matters for a sign-in:
        # a server without one connects in this call, and the agent sees the result at once.
        config.update({k: v for k, v in (extra or {}).items() if k != "origin"})
        if manifest.custom:
            config["mcp"] = {
                "title": manifest.title,
                "url": spec.url,
                "auth": spec.auth,
                "header": spec.header,
                "scope": spec.scope,
            }
            before = old.get("mcp")
            if isinstance(before, Mapping) and any(
                before.get(key) != config["mcp"][key] for key in ("url", "auth", "header")
            ):
                # A credential never goes to a server that it was not given for.
                await self._drop_secret(org_id, row_id, actor=actor)
        self._mcp.forget(row_id)
        if spec.auth != "oauth":
            view = await self._set(org_id, row_id, state="pending", error=None, config=config)
            if spec.auth == "none":
                view = await self.check(org_id, row_id, actor=actor)
            return Started(view, None)
        if self.return_base is not None:
            # The return page of the control plane needs no web session (M8.1).
            redirect = f"{self.return_base}{OAUTH_RETURN_PATH}"
        elif self.web_base is not None:
            redirect = f"{self.web_base}/connections"
        else:
            raise ConnectorError("the sama web app address is not set", status_code=409)
        if extra and "origin" in extra:
            config["origin"] = extra["origin"]
        try:
            config["oauth"], link = await self._mcp.begin_oauth(
                org_id, row_id, spec, redirect_uri=redirect, actor=actor
            )
        except McpError as error:
            # ``start`` sets ``failed`` only if this start still owns the row.
            raise ConnectorError(str(error), status_code=502) from None
        config["sign_in_url"] = link
        view = await self._set(
            org_id, row_id, state="initiated", error=None, config=config, secret=True
        )
        return Started(view, link)

    async def _tools_start(
        self,
        org_id: uuid.UUID,
        row_id: uuid.UUID,
        manifest: Manifest,
        *,
        actor: uuid.UUID,
        tool_cache: Mapping[str, Any] | None,
    ) -> Started:
        """Starts a hosted MCP server or a custom API connection. A custom one stores its spec.
        A new target of the credential (another program, egress, URL, or auth) deletes the
        stored values: a credential never goes to a target that it was not given for."""
        old = await self._config(org_id, row_id)
        config: dict[str, Any] = {k: v for k, v in old.items() if k in ("filters", "fields")}
        if manifest.custom:
            key = CUSTOM_KEYS[manifest.kind]
            spec = manifest.hosted if manifest.hosted is not None else manifest.api
            assert spec is not None  # noqa: S101 - the manifest validator makes sure
            config[key] = {"title": manifest.title, "spec": spec.model_dump(mode="json")}
            before = old.get(key)
            if isinstance(before, Mapping) and _target(manifest.kind, before.get("spec")) != (
                _target(manifest.kind, config[key]["spec"])
            ):
                await self._drop_secret(org_id, row_id, actor=actor)
                config.pop("fields", None)
        self._api.forget(row_id)
        if self._hosts is not None and manifest.kind == "mcp_hosted":
            await self._hosts.restart(str(row_id))
        view = await self._set(
            org_id, row_id, state="pending", error=None, config=config, tool_cache=tool_cache
        )
        if not manifest.fields or view.has_secret:
            view = await self.check(org_id, row_id, actor=actor)
        return Started(view, None)

    async def _values(
        self, org_id: uuid.UUID, view: ConnectionView, manifest: Manifest
    ) -> dict[str, str] | None:
        """The form values of a connection, or ``{}`` if the connector has no form."""
        if not manifest.fields:
            return dict(view.fields)
        return await self._form_values(org_id, view)

    async def _server(
        self, org_id: uuid.UUID, view: ConnectionView, manifest: Manifest
    ) -> HostedServer | None:
        """What the host of a hosted connection runs, with its environment from the vault."""
        hosted = manifest.hosted
        values = await self._values(org_id, view, manifest)
        if hosted is None or values is None:
            return None
        env = {
            variable: values[field] for variable, field in hosted.env.items() if values.get(field)
        }
        return HostedServer(str(view.id), str(org_id), hosted, env)

    async def _hosted_state(
        self, org_id: uuid.UUID, view: ConnectionView, manifest: Manifest
    ) -> tuple[State, str | None, dict[str, Any] | None]:
        """The health check of a hosted MCP server: start it if needed, and list its tools."""
        if self._hosts is None:
            return "failed", "this sama cannot run stdio MCP servers: E2B is not set up", None
        server = await self._server(org_id, view, manifest)
        if server is None:
            return "pending", None, None
        try:
            tools = await self._hosts.list_tools(server)
        except McpProcessEndedError as error:
            return "failed", str(error), None
        except McpUnavailableError as error:
            # The check did not get an answer: the state stays, and the row keeps the error.
            return cast(State, view.state), str(error), None
        except McpError as error:
            return "failed", str(error), None
        hosted = server.hosted
        exposed = exposed_tools(
            view.connector,
            tools,
            include=hosted.include,
            exclude=hosted.exclude,
            access=hosted.access,
        )
        return "connected", None, _cache(exposed)

    async def _api_state(
        self, org_id: uuid.UUID, view: ConnectionView, manifest: Manifest
    ) -> tuple[State, str | None]:
        """The health check of a custom API: the OAuth 2 token and one ``GET``."""
        values = await self._values(org_id, view, manifest)
        if values is None or manifest.api is None:
            return "pending", None
        operations = [t.operation for t in view.mcp_tools() if t.operation is not None]
        try:
            await self._api.test(view.id, manifest.api, operations, values)
        except DriverUnavailableError as error:
            return cast(State, view.state), str(error)
        except DriverError as error:
            return "failed", str(error)
        return "connected", None

    async def _drop_secret(self, org_id: uuid.UUID, row_id: uuid.UUID, *, actor: uuid.UUID) -> None:
        """Deletes the vault entry and the tool list of a connection."""
        async with self._engine.begin() as connection:
            if self._vault is not None:
                await self._vault.delete(
                    org_id, connection_secret(row_id), actor_user_id=actor, connection=connection
                )
            await connection.execute(
                update(Connection)
                .where(Connection.org_id == org_id, Connection.id == row_id)
                .values(secret_ref=None, tool_cache=None)
            )

    async def _mcp_state(
        self, org_id: uuid.UUID, view: ConnectionView, manifest: Manifest
    ) -> tuple[State, str | None, dict[str, Any] | None]:
        """The health check of a remote MCP server: a new session and the tool list."""
        spec = _mcp_spec(manifest)
        try:
            tools = await self._mcp.discover(org_id, view.id, spec, view.config)
        except McpNotReadyError:
            if view.state in ("initiated", "expired"):
                return cast(State, view.state), view.last_error, None
            return "pending", None, None
        except McpUnavailableError as error:
            # The check did not get an answer: the state stays, and the row keeps the error.
            return cast(State, view.state), str(error), None
        except McpAuthError as error:
            if spec.auth == "oauth":
                return "expired", f"The server refused the sign-in: {error}. Connect again.", None
            return "failed", f"The server refused the credential: {error}.", None
        except McpError as error:
            return "failed", str(error), None
        exposed = exposed_tools(
            view.connector, tools, include=spec.include, exclude=spec.exclude, access=spec.access
        )
        return "connected", None, _cache(exposed)

    async def _check_url(self, url: str, *, api: bool = False) -> None:
        """Refuses a URL that the outbound URL policy blocks (Section 16.4), before the row is
        made. Every request checks it again. ``api`` uses the resolver of the API driver."""
        try:
            await (self._api if api else self._mcp).check_url(url)
        except (BlockedUrlError, FetchError) as error:
            raise ConnectorError(f"sama cannot use this URL: {error}", status_code=422) from None

    async def _stored_custom(
        self,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        connector: str,
        owner: uuid.UUID | None,
    ) -> Manifest:
        async with self._engine.connect() as connection:
            owner_clause = (
                Connection.owner_user_id.is_(None)
                if owner is None
                else Connection.owner_user_id == owner
            )
            row = (
                await connection.execute(
                    select(Connection).where(
                        Connection.org_id == org_id,
                        Connection.workspace_id == workspace_id,
                        Connection.connector == connector,
                        owner_clause,
                    )
                )
            ).one_or_none()
        if row is None:
            raise ConnectorError(f"sama has no connector {connector}", status_code=404)
        return self._manifest_of(ConnectionView.of(row))

    def _manifest_of(self, view: ConnectionView) -> Manifest:
        """The manifest of a connection: the catalog, or the server or the API that an admin
        added."""
        custom = view.custom()
        if custom is None:
            return self._manifest(view.connector)
        if view.kind != "mcp_remote":
            return _stored_manifest(view, custom)
        try:
            spec = McpRemote.model_validate(
                {key: custom.get(key) for key in ("url", "auth", "header", "scope")}
            )
            title = str(custom.get("title") or view.connector)[:60]
            return _custom_manifest(view.connector, title, cast(Scope, view.scope), spec)
        except ValidationError:
            raise ConnectorError(
                f"the stored server of {view.connector} is not valid", status_code=500
            ) from None

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
            # The start stopped before Composio gave a link (for example, no Composio key).
            return "failed", view.last_error or "The sign-in did not start. Connect again."
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
            if error.unknown:
                raise ConnectorUnknownError(f"{tool.name} has no known result: {error}") from None
            raise ConnectorError(f"{tool.name} failed: {error}") from None
        return json.dumps(data, ensure_ascii=False, default=str, indent=1)

    async def _form_call(
        self,
        org_id: uuid.UUID,
        view: ConnectionView,
        manifest: Manifest,
        tool: ToolSpec,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        values = await self._form_values(org_id, view)
        driver = self._drivers.get(manifest.kind)
        if values is None or driver is None:
            raise ConnectorError("the connection is not complete. Use connection_request")
        try:
            return await driver.call(
                manifest, tool, values, arguments, idempotency_key=idempotency_key
            )
        except DriverUnavailableError as error:
            raise ConnectorUnknownError(f"{tool.name} has no known result: {error}") from None
        except DriverError as error:
            raise ConnectorError(f"{tool.name} failed: {error}") from None

    async def _database_call(
        self,
        claims: RunTokenClaims,
        scope: RunScope,
        view: ConnectionView,
        manifest: Manifest,
        tool: ToolSpec,
        arguments: Mapping[str, Any],
    ) -> str:
        values = await self._form_values(scope.org_id, view)
        if values is None:
            raise ConnectorError("the connection is not complete. Use connection_request")
        try:
            return await self._database.run(claims, view.id, manifest, tool, values, arguments)
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
        secret: bool = False,
        tool_cache: Mapping[str, Any] | None = None,
    ) -> ConnectionView:
        view = await self._write(
            org_id,
            connection_id,
            state=state,
            error=error,
            config=config,
            secret=secret,
            tool_cache=tool_cache,
        )
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
        tool_cache: Mapping[str, Any] | None = None,
        secret: bool = False,
    ) -> ConnectionView | None:
        """Sets the state, and returns ``None`` if the row is not there. With ``expect``, sets
        it only if the row did not change since ``expect`` was read. ``secret`` sets the
        vault entry of the row."""
        values: dict[str, Any] = {
            "state": state,
            # A provider can echo a credential in its error: redact it before the row has it.
            "last_error": None if error is None else short_error(error),
        }
        if touch:
            values["updated_at"] = func.now()
        if config is not None:
            values["config"] = dict(config)
        if health:
            values["last_health_at"] = func.now()
        if tool_cache is not None:
            values["tool_cache"] = dict(tool_cache)
        if secret:
            values["secret_ref"] = str(connection_secret(connection_id))
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


_VIA: Final[Mapping[str, str]] = {"composio": ", via Composio"}


def _signs_in(manifest: Manifest) -> bool:
    """The connector signs in on a provider page (Composio, MCP OAuth) or needs no sign-in, so
    the chat can start it. A connector with a form (a key, a database) cannot."""
    if manifest.kind == "composio":
        return True
    if manifest.mcp is not None:
        return manifest.mcp.auth in ("oauth", "none")
    return False


def _role_line(grants: Grants | None, workspace_id: uuid.UUID) -> str:
    if grants is None:
        return "The run has no user: it cannot start a sign-in."
    role = grants.org_role or "member"
    admin = grants.admins_workspace(workspace_id)
    return f"The user is the org {role}. " + (
        "The user can connect for the whole workspace and set up chat channels."
        if admin
        else "The user connects for themselves only; a workspace connection or a chat "
        "channel needs an admin."
    )


def _host(url: str) -> str:
    return (urlsplit(url.strip()).hostname or "server")[:60]


def _server_name(url: str) -> str:
    """A connection name from the host of an MCP server URL: ``mcp.linear.app`` gives
    ``linear``."""
    host = _host(url).lower()
    parts = [
        p for p in host.split(".") if p not in ("www", "mcp", "api", "com", "app", "dev", "io")
    ]
    base = (parts[0] if parts else "server").replace("_", "-")
    base = "".join(c for c in base if c.isalnum() or c == "-").strip("-") or "server"
    if not base[0].isalpha():
        base = f"mcp-{base}"
    return base[:40]


INVALID_RETURN: Final = "This sign-in link is not valid or has ended. Connect again from sama."


def _fresh(origin: Mapping[str, str]) -> bool:
    """The request is recent enough to continue (``RESUME_TTL``)."""
    try:
        at = datetime.fromisoformat(origin.get("at") or "")
    except ValueError:
        return False
    return datetime.now(UTC) - at <= RESUME_TTL


def replace_config(view: ConnectionView, config: Mapping[str, Any]) -> ConnectionView:
    """The view with another config (the config is not a secret)."""
    return ConnectionView(
        id=view.id,
        workspace_id=view.workspace_id,
        connector=view.connector,
        kind=view.kind,
        scope=view.scope,
        owner_user_id=view.owner_user_id,
        state=view.state,
        last_error=view.last_error,
        last_health_at=view.last_health_at,
        created_at=view.created_at,
        updated_at=view.updated_at,
        fields=view.fields,
        has_secret=view.has_secret,
        config=dict(config),
        tool_cache=view.tool_cache,
    )


def _starter(view: ConnectionView) -> uuid.UUID | None:
    """The user who started the last sign-in of the connection."""
    for value in (view.config.get("by"), (origin_of(view.config) or {}).get("user_id")):
        if isinstance(value, str):
            try:
                return uuid.UUID(value)
            except ValueError:
                continue
    return view.owner_user_id


def _via(view: ConnectionView) -> str:
    return "chat" if view.config.get("via") == "chat" else "web"


def _custom_manifest(name: str, title: str, scope: Scope, spec: McpRemote) -> Manifest:
    """The manifest of a remote MCP server that a workspace admin added by URL."""
    return Manifest(
        name=name,
        title=title,
        description=f"A remote MCP server at {urlsplit(spec.url).hostname}.",
        kind="mcp_remote",
        scopes=(scope,),
        mcp=spec,
        fields=mcp_fields(spec.auth, spec.header),
        custom=True,
    )


def _require_admin(grants: Grants, workspace_id: uuid.UUID, what: str) -> None:
    if not grants.sees_workspace(workspace_id):
        raise ConnectorError("the workspace does not exist", status_code=404)
    if not grants.admins_workspace(workspace_id):
        raise ConnectorError(f"only a workspace admin or the org owner can {what}", status_code=403)


def _invalid(error: ValidationError) -> ConnectorError:
    """The first problem of a validation, as a 422 answer."""
    problem = error.errors()[0]
    where = ".".join(str(part) for part in problem.get("loc", ()))
    text = f"{where}: {problem['msg']}" if where else str(problem["msg"])
    return ConnectorError(text, status_code=422)


def _cache(exposed: list[ExposedTool]) -> dict[str, Any]:
    """The ``tool_cache`` of a check: the tools that a run can use, with their access."""
    return {
        "discovered_at": datetime.now(UTC).isoformat(),
        "tools": [{**tool.remote.stored(), "access": tool.access} for tool in exposed],
    }


def _hosted_manifest(name: str, title: str, scope: Scope, hosted: McpHosted) -> Manifest:
    """The manifest of a stdio MCP server that a workspace admin added."""
    if hosted.package is not None:
        description = (
            f"A stdio MCP server: the {hosted.package.registry} package {hosted.package.pinned}."
        )
    else:
        description = "A stdio MCP server with a custom command."
    return Manifest(
        name=name,
        title=title,
        description=description,
        kind="mcp_hosted",
        scopes=(scope,),
        hosted=hosted,
        fields=hosted_fields(tuple(hosted.env)),
        custom=True,
    )


def _api_manifest(name: str, title: str, scope: Scope, api: ApiSpec) -> Manifest:
    """The manifest of an HTTP API that a workspace admin added."""
    return Manifest(
        name=name,
        title=title,
        description=f"An HTTP API at {urlsplit(api.base_url).hostname}.",
        kind="http_api",
        scopes=(scope,),
        api=api,
        fields=api_fields(api.auth),
        custom=True,
    )


def _target(kind: str, spec: object) -> object:
    """The part of a custom spec that a credential is given for. A change deletes the
    credential."""
    if not isinstance(spec, Mapping):
        return None
    keys = ("package", "command", "env", "egress") if kind == "mcp_hosted" else ("base_url", "auth")
    return {key: spec.get(key) for key in keys}


def _stored_manifest(view: ConnectionView, custom: Mapping[str, Any]) -> Manifest:
    """The manifest of a stored hosted server or API (``add_stdio``, ``add_api``)."""
    title = str(custom.get("title") or view.connector)[:60]
    scope = cast(Scope, view.scope)
    spec = custom.get("spec")
    try:
        if view.kind == "mcp_hosted":
            return _hosted_manifest(view.connector, title, scope, McpHosted.model_validate(spec))
        return _api_manifest(view.connector, title, scope, ApiSpec.model_validate(spec))
    except ValidationError:
        raise ConnectorError(
            f"the stored spec of {view.connector} is not valid", status_code=500
        ) from None


def _custom_public(kind: str, custom: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The custom spec for the web app. It has no secret: the vault holds the values."""
    if custom is None:
        return None
    if kind == "mcp_remote":
        return {key: custom.get(key) for key in ("title", "url", "auth", "header")}
    spec = custom.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    if kind == "mcp_hosted":
        env = spec.get("env")
        return {
            "title": custom.get("title"),
            "package": spec.get("package"),
            "command": spec.get("command"),
            "env": sorted(env) if isinstance(env, Mapping) else [],
            "egress": spec.get("egress") or [],
            "memory_mb": spec.get("memory_mb"),
            "idle_s": spec.get("idle_s"),
        }
    auth = spec.get("auth")
    auth = auth if isinstance(auth, Mapping) else {}
    return {
        "title": custom.get("title"),
        "base_url": spec.get("base_url"),
        "auth": auth.get("type"),
        "auth_name": auth.get("name"),
        "auth_location": auth.get("location"),
        "token_url": auth.get("token_url"),
    }


def _mcp_spec(manifest: Manifest) -> McpRemote:
    if manifest.mcp is None:
        raise ConnectorError(f"{manifest.title} is not a remote MCP server", status_code=500)
    return manifest.mcp


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
            select(Connection.id, Connection.config, Connection.kind)
            .where(
                Connection.org_id == org_id,
                Connection.workspace_id == workspace_id,
                Connection.connector == connector,
                owner_clause,
            )
            .with_for_update()
        )
    ).one_or_none()


async def _rows_for(connection: AsyncConnection, scope: RunScope) -> list[ConnectionView]:
    owners = [Connection.scope == "workspace"]
    if not scope.shared and scope.user_id is not None:
        owners.append(Connection.owner_user_id == scope.user_id)
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


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


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
