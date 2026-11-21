"""The ``cloud`` target with a fake Browserbase API and a fake CDP browser (Section 14.1)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from pydantic import SecretStr

from sama.browser.browserbase import BrowserbaseTarget
from sama.browser.contexts import MemoryContextStore
from sama.browser.target import CLOUD, BrowserError, TargetName
from tests.browser.fakes import AllowAll

if TYPE_CHECKING:
    from playwright.async_api import Browser

KEY = "bb_test_key_do_not_leak"
PROJECT = "proj-1"


@dataclass
class FakePage:
    def on(self, event: str, callback: Callable[..., Any]) -> None:
        return None

    def is_closed(self) -> bool:
        return False


@dataclass
class FakeContext:
    pages: list[FakePage] = field(default_factory=list)
    routed: bool = False

    def set_default_timeout(self, ms: float) -> None:
        return None

    def set_default_navigation_timeout(self, ms: float) -> None:
        return None

    async def route(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.routed = True

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        return None

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page


@dataclass
class FakeBrowser:
    contexts: list[FakeContext] = field(default_factory=lambda: [FakeContext()])
    closed: bool = False

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeConnector:
    fail: bool = False
    urls: list[str] = field(default_factory=list)
    browsers: list[FakeBrowser] = field(default_factory=list)
    closed: bool = False

    async def connect(self, url: str) -> Browser:
        self.urls.append(url)
        if self.fail:
            raise RuntimeError("WebSocket error: 401")
        browser = FakeBrowser()
        self.browsers.append(browser)
        return cast("Browser", browser)

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class FakeApi:
    """The parts of the Browserbase API that the target uses."""

    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    lost_contexts: set[str] = field(default_factory=set)
    session_status: int = 201
    session_body: Any = None
    contexts_error: bool = False
    count: int = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["x-bb-api-key"] == KEY
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        path = request.url.path
        if request.method == "POST" and path == "/v1/contexts":
            if self.contexts_error:
                raise httpx.ConnectError("connection refused")
            self.count += 1
            return httpx.Response(201, json={"id": f"ctx-{self.count}"})
        if request.method == "DELETE" and path.startswith("/v1/contexts/"):
            return httpx.Response(204)
        if request.method == "POST" and path == "/v1/sessions":
            context = (body.get("browserSettings") or {}).get("context") or {}
            if context.get("id") in self.lost_contexts:
                return httpx.Response(404, json={"message": "Context not found"})
            if self.session_status != 201:
                return httpx.Response(self.session_status, json=self.session_body)
            self.count += 1
            return httpx.Response(
                201, json={"id": f"sess-{self.count}", "connectUrl": f"wss://connect/{self.count}"}
            )
        if request.method == "POST" and path.startswith("/v1/sessions/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1]})
        return httpx.Response(500)

    def calls(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _ in self.requests]


def make_target(
    api: FakeApi,
    connector: FakeConnector,
    contexts: Any = None,
) -> BrowserbaseTarget:
    return BrowserbaseTarget(
        api_key=SecretStr(KEY),
        project_id=PROJECT,
        contexts=contexts if contexts is not None else MemoryContextStore(),
        connector=connector,
        transport=httpx.MockTransport(api.handler),
    )


async def test_a_private_run_gets_the_persistent_context_of_its_user() -> None:
    api, connector, store = FakeApi(), FakeConnector(), MemoryContextStore()
    target = make_target(api, connector, store)
    session = await target.start(org_id="org-1", user_id="user-1", run_id="run-1", guard=AllowAll())
    assert session.target == CLOUD
    assert api.calls() == [("POST", "/v1/contexts"), ("POST", "/v1/sessions")]
    assert api.requests[0][2] == {"projectId": PROJECT}
    assert api.requests[1][2] == {
        "projectId": PROJECT,
        "timeout": 900,
        "userMetadata": {"sama_org": "org-1", "sama_run": "run-1"},
        "browserSettings": {"context": {"id": "ctx-1", "persist": True}},
    }
    assert connector.urls == ["wss://connect/2"]
    assert store.rows == {("org-1", "user-1", "cloud"): "ctx-1"}
    assert connector.browsers[0].contexts[0].routed

    # The next run of the user uses the same context.
    await target.start(org_id="org-1", user_id="user-1", run_id="run-2", guard=AllowAll())
    assert api.calls()[2:] == [("POST", "/v1/sessions")]
    assert api.requests[2][2]["browserSettings"]["context"]["id"] == "ctx-1"
    await target.aclose()
    assert connector.closed


async def test_a_shared_run_gets_a_new_context_that_is_not_kept() -> None:
    api, connector, store = FakeApi(), FakeConnector(), MemoryContextStore()
    target = make_target(api, connector, store)
    await target.start(org_id="org-1", user_id=None, run_id="run-1", guard=AllowAll())
    assert api.calls() == [("POST", "/v1/sessions")]
    assert "browserSettings" not in api.requests[0][2]
    assert store.rows == {}
    await target.aclose()


class RacingStore(MemoryContextStore):
    """Another run of the user stores its context between ``get`` and ``claim``."""

    async def claim(
        self, org_id: str, user_id: str, target: TargetName, provider_context_id: str
    ) -> str:
        self.rows.setdefault((org_id, user_id, target), "ctx-other")
        return await super().claim(org_id, user_id, target, provider_context_id)


async def test_the_context_of_the_first_run_wins_a_race() -> None:
    api, connector = FakeApi(), FakeConnector()
    target = make_target(api, connector, RacingStore())
    await target.start(org_id="org-1", user_id="user-1", run_id="run-1", guard=AllowAll())
    assert api.calls() == [
        ("POST", "/v1/contexts"),
        ("DELETE", "/v1/contexts/ctx-1"),
        ("POST", "/v1/sessions"),
    ]
    assert api.requests[2][2]["browserSettings"]["context"]["id"] == "ctx-other"
    await target.aclose()


async def test_a_lost_context_is_replaced_once() -> None:
    api, connector, store = (
        FakeApi(lost_contexts={"ctx-gone"}),
        FakeConnector(),
        MemoryContextStore(),
    )
    store.rows[("org-1", "user-1", "cloud")] = "ctx-gone"
    target = make_target(api, connector, store)
    await target.start(org_id="org-1", user_id="user-1", run_id="run-1", guard=AllowAll())
    assert api.calls() == [
        ("POST", "/v1/sessions"),
        ("POST", "/v1/contexts"),
        ("POST", "/v1/sessions"),
    ]
    assert store.rows == {("org-1", "user-1", "cloud"): "ctx-1"}
    await target.aclose()


async def test_a_failed_connection_releases_the_session() -> None:
    api, connector = FakeApi(), FakeConnector(fail=True)
    target = make_target(api, connector)
    with pytest.raises(BrowserError, match="the browser did not connect; try again"):
        await target.start(org_id="org-1", user_id=None, run_id="run-1", guard=AllowAll())
    assert api.requests[-1] == (
        "POST",
        "/v1/sessions/sess-1",
        {"projectId": PROJECT, "status": "REQUEST_RELEASE"},
    )
    await target.aclose()


async def test_close_ends_the_browser_and_releases_the_session() -> None:
    api, connector = FakeApi(), FakeConnector()
    target = make_target(api, connector)
    session = await target.start(org_id="org-1", user_id=None, run_id="run-1", guard=AllowAll())
    await session.close()
    await session.close()  # a second close does nothing
    assert connector.browsers[0].closed
    assert api.calls()[1:] == [("POST", "/v1/sessions/sess-1")]
    with pytest.raises(BrowserError, match="the browser session ended"):
        await session.snapshot()
    await target.aclose()


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (
            402,
            {"message": "Plan limit reached " + "x" * 400},
            r"Browserbase did not create a browser session \(HTTP 402\): Plan limit reached x+$",
        ),
        (429, {"error": "Too many sessions"}, r"\(HTTP 429\): Too many sessions$"),
        (500, None, r"\(HTTP 500\)$"),
        (200, ["not", "an", "object"], r"\(HTTP 200\)$"),
    ],
)
async def test_an_api_error_becomes_a_clear_message(status: int, body: Any, message: str) -> None:
    api = FakeApi(session_status=status, session_body=body)
    target = make_target(api, FakeConnector())
    with pytest.raises(BrowserError, match=message) as caught:
        await target.start(org_id="org-1", user_id=None, run_id="run-1", guard=AllowAll())
    assert len(str(caught.value)) < 300
    assert KEY not in str(caught.value)
    await target.aclose()


async def test_a_network_error_becomes_a_clear_message() -> None:
    api = FakeApi(contexts_error=True)
    target = make_target(api, FakeConnector())
    with pytest.raises(BrowserError, match="Browserbase did not answer: ConnectError"):
        await target.start(org_id="org-1", user_id="user-1", run_id="run-1", guard=AllowAll())
    await target.aclose()
