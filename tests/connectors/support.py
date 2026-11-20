"""Fakes of the Composio API and of a form driver, for the connection tests (Section 10.3)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from sama.connectors.api import ApiDriver
from sama.connectors.composio import ComposioClient
from sama.connectors.host import ConnectorHosts
from sama.connectors.http_api import DriverError, DriverUnavailableError
from sama.connectors.manifest import Catalog, Manifest, ToolSpec
from sama.connectors.mcp_driver import McpDriver
from sama.connectors.service import ConnectionService
from sama.vault.store import OrgKeys, SecretName, Vault

CATALOG = Path(__file__).resolve().parents[2] / "connectors" / "catalog"
WEB = "https://app.sama.test"
NOTION_TOKEN = "ntn_secret_value_123"


class FakeComposio:
    """The Composio API: one auth config, link, account state, and execute."""

    def __init__(self) -> None:
        self.status = "INITIATED"
        self.accounts = 0
        self.deleted: list[str] = []
        self.executed: list[dict[str, Any]] = []
        self.triggers: dict[str, dict[str, Any]] = {}
        """The trigger instances: id to the upsert body and the slug."""
        self.deleted_triggers: list[str] = []
        self.subscription: dict[str, Any] | None = None
        self.subscription_calls: list[str] = []
        self.trigger_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/auth_configs":
            toolkit = request.url.params["toolkit_slug"]
            item = {"id": f"ac_{toolkit}", "toolkit": {"slug": toolkit}, "status": "ENABLED"}
            return httpx.Response(200, json={"items": [item]})
        if path == "/api/v3.1/connected_accounts/link":
            self.accounts += 1
            return httpx.Response(
                200,
                json={
                    "connected_account_id": f"ca_{self.accounts}",
                    "redirect_url": f"https://connect.composio.dev/link/{self.accounts}",
                },
            )
        if path.startswith("/api/v3.1/connected_accounts/"):
            account = path.rsplit("/", 1)[-1]
            if request.method == "DELETE":
                self.deleted.append(account)
                return httpx.Response(200, json={})
            if self.status == "GONE":
                return httpx.Response(404, json={"error": {"message": "no account"}})
            if self.status == "DOWN":
                return httpx.Response(503, json={"error": {"message": "down"}})
            return httpx.Response(200, json={"status": self.status})
        if path.startswith("/api/v3.1/trigger_instances/manage/"):
            self.deleted_triggers.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={})
        if path.startswith("/api/v3.1/trigger_instances/") and path.endswith("/upsert"):
            if self.trigger_status != 200:
                return httpx.Response(self.trigger_status, json={"error": {"message": "no"}})
            body = json.loads(request.content)
            item = {"slug": path.split("/")[-2], **body}
            # Composio gives the existing instance for the same account, type, and config.
            for existing_id, existing in self.triggers.items():
                if existing == item:
                    return httpx.Response(200, json={"trigger_id": existing_id})
            trigger_id = f"ti_{len(self.triggers) + 1}"
            self.triggers[trigger_id] = item
            return httpx.Response(200, json={"trigger_id": trigger_id})
        if path.startswith("/api/v3.1/webhook_subscriptions"):
            self.subscription_calls.append(request.method)
            if request.method == "GET":
                items = [self.subscription] if self.subscription else []
                return httpx.Response(200, json={"items": items})
            body = json.loads(request.content)
            if request.method == "POST":
                self.subscription = {"id": "ws_1", "secret": "composio-hook-secret-1", **body}
            else:
                assert self.subscription is not None
                self.subscription.update(body)
            return httpx.Response(200, json=self.subscription)
        if path.startswith("/api/v3.1/tools/execute/"):
            self.executed.append(json.loads(request.content))
            return httpx.Response(200, json={"successful": True, "data": {"slug": path}})
        return httpx.Response(404)


class FakeForm:
    def __init__(self) -> None:
        self.good = NOTION_TOKEN
        self.down = False
        self.during_test: Callable[[], Awaitable[object]] | None = None
        """Runs while the API "answers", to change the connection at the same time."""
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def test(self, manifest: Manifest, values: Mapping[str, str]) -> None:
        if self.during_test is not None:
            await self.during_test()
        if self.down:
            raise DriverUnavailableError("the API cannot be reached: ConnectError")
        if values.get("token") != self.good:
            raise DriverError("the API answered 401")

    async def call(
        self, manifest: Manifest, tool: ToolSpec, values: Mapping[str, str], arguments: Any
    ) -> str:
        await self.test(manifest, values)
        self.calls.append((tool.name, dict(arguments)))
        return '{"results": []}'


def service(
    engine: Any,
    vault: Vault,
    fake: FakeComposio,
    form: FakeForm,
    mcp: McpDriver | None = None,
    *,
    hosts: ConnectorHosts | None = None,
    api: ApiDriver | None = None,
) -> ConnectionService:
    """A connection service with the catalog of the repo and the fakes."""
    return ConnectionService(
        engine,
        Catalog.load(CATALOG),
        keys=OrgKeys(vault, {SecretName.COMPOSIO_API_KEY: SecretStr("ck-process")}),
        vault=vault,
        composio=ComposioClient(transport=httpx.MockTransport(fake)),
        drivers={"http_api": form},
        mcp=mcp,
        hosts=hosts,
        api=api,
        web_base=WEB + "/",
    )
