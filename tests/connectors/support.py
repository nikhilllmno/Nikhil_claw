"""Fakes of the Composio API and of a form driver, for the connection tests (Section 10.3)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from sama.connectors.composio import ComposioClient
from sama.connectors.http_api import DriverError, DriverUnavailableError
from sama.connectors.manifest import Catalog, Manifest, ToolSpec
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


def service(engine: Any, vault: Vault, fake: FakeComposio, form: FakeForm) -> ConnectionService:
    """A connection service with the catalog of the repo and the fakes."""
    return ConnectionService(
        engine,
        Catalog.load(CATALOG),
        keys=OrgKeys(vault, {SecretName.COMPOSIO_API_KEY: SecretStr("ck-process")}),
        vault=vault,
        composio=ComposioClient(transport=httpx.MockTransport(fake)),
        drivers={"http_api": form},
        web_base=WEB + "/",
    )
