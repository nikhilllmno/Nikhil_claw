"""The ``cloud`` target with a fake Browserbase API and a fake CDP browser (Section 14.1)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from pydantic import SecretStr

from sama.browser.browserbase import NOT_KEPT_NOTE, BrowserbaseTarget, PlaywrightConnector
from sama.browser.cdp import PlaywrightSession
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

    async def route_web_socket(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.routed = True

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        return None

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page


@dataclass
class FakeCdp:
    sent: list[tuple[str, Any]] = field(default_factory=list)

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        return None

    async def send(self, method: str, params: Any = None) -> dict[str, Any]:
        self.sent.append((method, params))
        return {}

    def remove_listener(self, event: str, callback: Callable[..., Any]) -> None:
        return None


@dataclass
class FakeBrowser:
    contexts: list[FakeContext] = field(default_factory=lambda: [FakeContext()])
    closed: bool = False
    cdp: FakeCdp = field(default_factory=FakeCdp)

    async def new_browser_cdp_session(self) -> FakeCdp:
        return self.cdp

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeConnector:
    fail: bool = False
    urls: list[str] = field(default_factory=list)
    browsers: list[FakeBrowser] = field(default_factory=list)
    closed: bool = False
    gate: asyncio.Event | None = None

    async def connect(self, url: str) -> Browser:
        self.urls.append(url)
        if self.gate is not None:
            await self.gate.wait()
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
    release_status: int = 200
    session_gate: asyncio.Event | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
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
            if self.session_gate is not None:
                await self.session_gate.wait()
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
            if self.release_status != 200:
                return httpx.Response(self.release_status, json={})
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1]})
        return httpx.Response(500)

    def calls(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _ in self.requests]


class FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_target(
    api: FakeApi,
    connector: FakeConnector,
    contexts: Any = None,
    time: FakeTime | None = None,
) -> BrowserbaseTarget:
    time = time or FakeTime()
    return BrowserbaseTarget(
        api_key=SecretStr(KEY),
        project_id=PROJECT,
        contexts=contexts if contexts is not None else MemoryContextStore(),
        connector=connector,
        transport=httpx.MockTransport(api.handler),
        clock=time.clock,
        sleep=time.sleep,
    )


def persist_of(api: FakeApi, index: int) -> Any:
    return api.requests[index][2]["browserSettings"]["context"]["persist"]


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
    cdp = connector.browsers[0].cdp
    assert cdp.sent == [("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})]

    # The next run of the user uses the same context.
    await session.close()
    await target.start(org_id="org-1", user_id="user-1", run_id="run-2", guard=AllowAll())
    assert api.calls()[2:] == [("POST", "/v1/sessions/sess-2"), ("POST", "/v1/sessions")]
    assert api.requests[3][2]["browserSettings"]["context"] == {"id": "ctx-1", "persist": True}
    await target.aclose()
    assert connector.closed


async def test_one_session_at_a_time_writes_a_context() -> None:
    api, connector, time = FakeApi(), FakeConnector(), FakeTime()
    target = make_target(api, connector, time=time)
    first = await target.start(org_id="org-1", user_id="user-1", run_id="run-1", guard=AllowAll())
    second = await target.start(org_id="org-1", user_id="user-1", run_id="run-2", guard=AllowAll())
    assert persist_of(api, 1) is True
    assert persist_of(api, 2) is False  # reads the saved state, writes nothing
    assert isinstance(second, PlaywrightSession)
    assert NOT_KEPT_NOTE in second._notes  # the agent reads it with the first state
    await second.close()  # a reader frees nothing and needs no save time
    assert time.slept == []
    await first.close()
    time.now += 3
    third = await target.start(org_id="org-1", user_id="user-1", run_id="run-3", guard=AllowAll())
    assert time.slept == [7.0]  # the rest of the save time of the context
    assert persist_of(api, -1) is True
    await third.close()
    await target.aclose()


async def test_a_failed_start_frees_the_context() -> None:
    api, connector = FakeApi(), FakeConnector(fail=True)
    target = make_target(api, connector)
    with pytest.raises(BrowserError, match="did not connect"):
        await target.start(org_id="org-1", user_id="user-1", run_id="run-1", guard=AllowAll())
    connector.fail = False
    await target.start(org_id="org-1", user_id="user-1", run_id="run-2", guard=AllowAll())
    assert persist_of(api, -1) is True
    await target.aclose()


async def test_concurrent_first_connections_start_one_driver() -> None:
    started: list[object] = []
    stopped: list[object] = []
    gate = asyncio.Event()

    class Chromium:
        async def connect_over_cdp(self, url: str, **options: float) -> str:
            return url

    class Driver:
        chromium = Chromium()

        async def stop(self) -> None:
            stopped.append(self)

    async def start() -> Any:
        await gate.wait()
        driver = Driver()
        started.append(driver)
        return driver

    connector = PlaywrightConnector(start=start)
    calls = [asyncio.ensure_future(connector.connect(f"wss://{n}")) for n in range(3)]
    await asyncio.sleep(0.01)
    gate.set()
    assert [str(b) for b in await asyncio.gather(*calls)] == ["wss://0", "wss://1", "wss://2"]
    await connector.aclose()
    assert len(started) == 1
    assert stopped == started


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


async def wait_until(condition: Callable[[], bool]) -> None:
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition did not become true")


async def test_a_cancelled_connect_ends_the_session_before_it_frees_the_context() -> None:
    api, time = FakeApi(), FakeTime()
    connector = FakeConnector(gate=asyncio.Event())
    target = make_target(api, connector, time=time)
    start = asyncio.ensure_future(
        target.start(org_id="org-1", user_id="user-1", run_id="run-1", guard=AllowAll())
    )
    await wait_until(lambda: bool(connector.urls))
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert api.calls()[-1] == ("POST", "/v1/sessions/sess-2")  # the release
    connector.gate = None
    await target.start(org_id="org-1", user_id="user-1", run_id="run-2", guard=AllowAll())
    assert persist_of(api, -1) is True
    assert time.slept == [10.0]  # the save time after the release
    await target.aclose()


async def test_a_cancelled_create_ends_the_late_session() -> None:
    api, connector = FakeApi(session_gate=asyncio.Event()), FakeConnector()
    target = make_target(api, connector)
    start = asyncio.ensure_future(
        target.start(org_id="org-1", user_id="user-1", run_id="run-1", guard=AllowAll())
    )
    await wait_until(lambda: ("POST", "/v1/sessions") in api.calls())
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    # The context stays claimed until Browserbase answers and the late session ends.
    gate, api.session_gate = api.session_gate, None
    await target.start(org_id="org-1", user_id="user-1", run_id="run-2", guard=AllowAll())
    assert persist_of(api, -1) is False
    assert gate is not None
    gate.set()
    await target.aclose()  # waits for the cleanup
    assert ("POST", "/v1/sessions/sess-3") in api.calls()  # the late session ended


async def test_a_failed_release_keeps_other_writers_away_until_the_hard_timeout() -> None:
    api, connector, time = FakeApi(release_status=500), FakeConnector(), FakeTime()
    target = make_target(api, connector, time=time)
    first = await target.start(org_id="org-1", user_id="user-1", run_id="run-1", guard=AllowAll())
    await first.close()
    second = await target.start(org_id="org-1", user_id="user-1", run_id="run-2", guard=AllowAll())
    assert persist_of(api, -1) is False
    assert time.slept == []  # a reader does not wait for the hard timeout
    await second.close()
    time.now += 900 + 10 + 1
    await target.start(org_id="org-1", user_id="user-1", run_id="run-3", guard=AllowAll())
    assert persist_of(api, -1) is True
    await target.aclose()


class BlockingGuard(AllowAll):
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def check(self, url: str) -> None:
        self.entered.set()
        await asyncio.Event().wait()


async def test_close_stops_the_request_checks() -> None:
    api, connector, guard = FakeApi(), FakeConnector(), BlockingGuard()
    target = make_target(api, connector)
    session = await target.start(org_id="org-1", user_id=None, run_id="run-1", guard=guard)
    assert isinstance(session, PlaywrightSession)
    event = {"requestId": "r1", "resourceType": "Document", "request": {"url": "https://a.test/"}}
    session._on_paused(event)
    await guard.entered.wait()
    await session.close()
    assert not session._checks  # cancelled and awaited
    session._on_paused({**event, "requestId": "r2"})  # a late event after the close
    assert not session._checks
    cdp = connector.browsers[0].cdp
    assert [method for method, _ in cdp.sent] == ["Fetch.enable"]
    await target.aclose()
