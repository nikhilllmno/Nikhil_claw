"""Fake browser targets and guards for the browser tests."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field

from sama.browser.target import (
    CLOUD,
    Act,
    BrowserError,
    Extracted,
    ExtractRequest,
    Filled,
    FillRequest,
    NavigationGuard,
    PageState,
    Screenshot,
    Snapshot,
    Tab,
    TabAction,
    TargetName,
)
from sama.tools.outbound import BlockedUrlError

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
"""A valid 1x1 PNG image."""


class AllowAll:
    """A guard that allows every URL, for tests with a local web server."""

    async def check(self, url: str) -> None:
        return None

    def quick_problem(self, url: str) -> str | None:
        return None


@dataclass
class DenyHosts:
    """A guard that blocks the given hosts and allows the others."""

    hosts: frozenset[str]
    checked: list[str] = field(default_factory=list)

    async def check(self, url: str) -> None:
        self.checked.append(url)
        if any(host in url for host in self.hosts):
            raise BlockedUrlError("the host is blocked in this test")

    def quick_problem(self, url: str) -> str | None:
        return "the host is blocked in this test" if any(h in url for h in self.hosts) else None


@dataclass
class FakeSession:
    run_id: str
    url: str = "about:blank"
    title: str = ""
    calls: list[str] = field(default_factory=list)
    closed: bool = False
    gate: asyncio.Event | None = None
    """When set, ``snapshot`` waits for it, so a test can hold the lock of the session."""
    notes: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    tree: str = '- button "Go" [ref=e1]'
    error: BrowserError | None = None
    """When set, ``snapshot`` raises it."""

    @property
    def target(self) -> TargetName:
        return CLOUD

    def _state(self) -> PageState:
        notes, self.notes = self.notes, ()
        blocked, self.blocked = self.blocked, ()
        return PageState(
            url=self.url, title=self.title, tab=0, tabs=1, notes=notes, blocked=blocked
        )

    async def navigate(self, url: str) -> PageState:
        self.calls.append(f"navigate {url}")
        self.url, self.title = url, f"Page {url}"
        return self._state()

    async def snapshot(self) -> Snapshot:
        self.calls.append("snapshot")
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return Snapshot(page=self._state(), tree=self.tree)

    async def act(self, act: Act) -> PageState:
        self.calls.append(f"act {act.action} {act.ref}")
        return self._state()

    async def screenshot(self, *, full_page: bool = False) -> Screenshot:
        self.calls.append(f"screenshot {full_page}")
        return Screenshot(page=self._state(), data=PNG_1PX, mime_type="image/png")

    async def extract(self, request: ExtractRequest) -> Extracted:
        self.calls.append(f"extract {request.kind}")
        return Extracted(page=self._state(), kind=request.kind, content="Hello from the page")

    async def tabs(
        self, action: TabAction, *, index: int | None = None, url: str | None = None
    ) -> list[Tab]:
        self.calls.append(f"tabs {action} {index} {url}")
        return [Tab(index=0, url=self.url, title=self.title, active=True)]

    async def close(self) -> None:
        self.closed = True


@dataclass
class FillSession(FakeSession):
    """A fake session that can fill a saved login, like ``PlaywrightSession``."""

    fills: list[FillRequest] = field(default_factory=list)

    async def fill_login(self, request: FillRequest) -> Filled:
        if not request.site_allows(self.url):
            raise BrowserError("the field is not on the site of this login")
        self.fills.append(request)
        self.calls.append("fill_login")
        fields = ("username", "password") if request.password_ref != "none" else ("username",)
        return Filled(page=self._state(), fields=fields)


@dataclass
class FakeTarget:
    sessions: list[FakeSession] = field(default_factory=list)
    starts: list[tuple[str, str | None, str]] = field(default_factory=list)
    closed: bool = False
    fail: Exception | None = None
    gate: asyncio.Event | None = None
    """When set, ``start`` waits for it, so a test can run starts at the same time."""
    session_class: type[FakeSession] = FakeSession
    guards: list[NavigationGuard] = field(default_factory=list)

    @property
    def name(self) -> TargetName:
        return CLOUD

    async def start(
        self,
        *,
        org_id: str,
        user_id: str | None,
        run_id: str,
        guard: NavigationGuard,
        session_id: str = "",
    ) -> FakeSession:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        self.starts.append((org_id, user_id, run_id))
        self.guards.append(guard)
        session = self.session_class(run_id)
        self.sessions.append(session)
        return session

    async def aclose(self) -> None:
        self.closed = True
