"""The browser tools of sama (docs/architecture.md, Section 14.1).

``BrowserService`` keeps one live browser session for each run. The first browser tool of a run
starts the session on the target, and later calls of the same run use it. The service closes a
session:

- when the run ends (the run-end hook of the worker calls ``end_run``),
- when the session is idle for ``idle_s``,
- when the process stops (``aclose``).

The target also has a hard time limit (the Browserbase session ``timeout``), so a session that
sama fails to close still ends.

The service renders the results for the agent. Page text, titles, and URLs come from the web, so
they go in an ``untrusted_content`` block. The notes of sama (a blocked URL, a new tab) stay
outside the block.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from sama.browser.target import (
    ACTIONS_WITH_REF,
    Act,
    BrowserError,
    BrowserSession,
    BrowserTarget,
    Extracted,
    ExtractKind,
    ExtractRequest,
    NavigationGuard,
    PageState,
    Screenshot,
    Snapshot,
    Tab,
    TabAction,
)
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.outbound import BlockedUrlError
from sama.tools.output import untrusted

log = logging.getLogger("sama.browser")

DEFAULT_IDLE_S: Final = 600.0
REAP_EVERY_S: Final = 30.0
MAX_SESSIONS_PER_ORG: Final = 5
MAX_SESSIONS: Final = 20
SOURCE: Final = "browser"


@dataclass(slots=True)
class _Live:
    org_id: str
    session: BrowserSession | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    used: float = field(default_factory=time.monotonic)
    ended: bool = False


class BrowserService:
    """The browser sessions of the runs of this process."""

    def __init__(
        self,
        target: BrowserTarget,
        guard: NavigationGuard,
        *,
        idle_s: float = DEFAULT_IDLE_S,
        max_sessions_per_org: int = MAX_SESSIONS_PER_ORG,
        max_sessions: int = MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.target = target
        self.guard = guard
        self._idle_s = idle_s
        self._max_per_org = max_sessions_per_org
        self._max = max_sessions
        self._clock = clock
        self._live: dict[tuple[str, str], _Live] = {}
        self._closing: set[asyncio.Task[None]] = set()
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False

    # -- the life of a session ---------------------------------------------------------------

    async def _session(self, claims: RunTokenClaims) -> tuple[_Live, BrowserSession]:
        """The live entry of the run, locked by the caller. It starts the session if the run
        has none."""
        live = self._live_of(claims)
        if live.session is None:
            self._check_limits(claims.org_id)
            live.session = await self.target.start(
                org_id=claims.org_id,
                user_id=claims.user_id,
                run_id=claims.run_id,
                guard=self.guard,
            )
            self._start_reaper()
        live.used = self._clock()
        return live, live.session

    def _live_of(self, claims: RunTokenClaims) -> _Live:
        if self._closed:
            raise BrowserError("the browser service is stopping")
        key = (claims.org_id, claims.run_id)
        live = self._live.get(key)
        if live is None or live.ended:
            live = _Live(org_id=claims.org_id, used=self._clock())
            self._live[key] = live
        return live

    def _check_limits(self, org_id: str) -> None:
        open_sessions = [live for live in self._live.values() if live.session is not None]
        if len(open_sessions) >= self._max:
            raise BrowserError("too many browser sessions are open; try again in a few minutes")
        if sum(1 for live in open_sessions if live.org_id == org_id) >= self._max_per_org:
            raise BrowserError(
                f"the org has {self._max_per_org} open browser sessions, the limit; try again "
                "when a run ends"
            )

    def end_run(self, org_id: str, run_id: str) -> None:
        """The run-end hook of the worker. It closes the session of the run in the
        background."""
        live = self._live.pop((org_id, run_id), None)
        if live is None:
            return
        live.ended = True
        self._spawn_close(live)

    def _spawn_close(self, live: _Live) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._close(live))
        except RuntimeError:  # pragma: no cover - no loop: the target time limit ends it
            return
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _close(self, live: _Live) -> None:
        async with live.lock:
            session, live.session = live.session, None
        if session is not None:
            await session.close()

    def _start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.get_running_loop().create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while self._live:
            await asyncio.sleep(min(REAP_EVERY_S, self._idle_s))
            self.reap()

    def reap(self) -> int:
        """Closes the sessions that are idle for ``idle_s``. Returns how many."""
        now = self._clock()
        idle = [
            key
            for key, live in self._live.items()
            if not live.lock.locked() and now - live.used >= self._idle_s
        ]
        for key in idle:
            live = self._live.pop(key)
            live.ended = True
            if live.session is not None:
                log.info("closing an idle browser session org=%s run=%s", key[0], key[1])
            self._spawn_close(live)
        return len(idle)

    async def aclose(self) -> None:
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        lives = list(self._live.values())
        self._live.clear()
        for live in lives:
            live.ended = True
            self._spawn_close(live)
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)
        await self.target.aclose()

    def open_sessions(self) -> int:
        return sum(1 for live in self._live.values() if live.session is not None)

    # -- the tools ---------------------------------------------------------------------------

    async def _checked(self, url: str) -> str:
        url = url.strip()
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if not scheme or "//" not in url:
            url = f"https://{url}"
        elif scheme not in {"http", "https"}:
            raise BrowserError("the browser opens only http and https URLs")
        try:
            await self.guard.check(url)
        except BlockedUrlError as error:
            raise BrowserError(f"the URL policy blocked {url}: {error}") from None
        return url

    async def open(self, claims: RunTokenClaims, url: str, *, snapshot: bool = True) -> str:
        url = await self._checked(url)
        live, session = await self._locked_session(claims)
        try:
            try:
                state = await session.navigate(url)
            except BlockedUrlError as error:
                raise BrowserError(str(error)) from None
            shot = await session.snapshot() if snapshot else None
        finally:
            self._release(live)
        return render_snapshot(shot) if shot is not None else render_state(state)

    async def snapshot(self, claims: RunTokenClaims) -> str:
        live, session = await self._locked_session(claims)
        try:
            shot = await session.snapshot()
        finally:
            self._release(live)
        return render_snapshot(shot)

    async def act(self, claims: RunTokenClaims, act: Act, *, snapshot: bool = True) -> str:
        if act.action in ACTIONS_WITH_REF and not act.ref:
            raise BrowserError(f"{act.action} needs ref: the ref of an element from the snapshot")
        if act.action == "drag" and not act.target_ref:
            raise BrowserError("drag needs target_ref: the element to drop on")
        live, session = await self._locked_session(claims)
        try:
            state = await session.act(act)
            shot = await session.snapshot() if snapshot else None
        finally:
            self._release(live)
        return render_snapshot(shot) if shot is not None else render_state(state)

    async def screenshot(
        self, claims: RunTokenClaims, *, full_page: bool = False
    ) -> tuple[str, Screenshot]:
        """The caption for the model and the image."""
        live, session = await self._locked_session(claims)
        try:
            shot = await session.screenshot(full_page=full_page)
        finally:
            self._release(live)
        return render_screenshot(shot), shot

    async def extract(
        self,
        claims: RunTokenClaims,
        kind: ExtractKind,
        *,
        ref: str | None = None,
        fields: Mapping[str, str] | None = None,
    ) -> str:
        request = extract_request(kind, ref=ref, fields=fields)
        live, session = await self._locked_session(claims)
        try:
            result = await session.extract(request)
        finally:
            self._release(live)
        return render_extracted(result)

    async def tabs(
        self,
        claims: RunTokenClaims,
        action: TabAction,
        *,
        index: int | None = None,
        url: str | None = None,
    ) -> str:
        if action == "select" and index is None:
            raise BrowserError("select needs index: the index of a tab from the list")
        checked = await self._checked(url) if url else None
        live, session = await self._locked_session(claims)
        try:
            try:
                tabs = await session.tabs(action, index=index, url=checked)
            except BlockedUrlError as error:
                raise BrowserError(str(error)) from None
        finally:
            self._release(live)
        return render_tabs(tabs)

    async def _locked_session(self, claims: RunTokenClaims) -> tuple[_Live, BrowserSession]:
        live = self._live_of(claims)
        await live.lock.acquire()
        try:
            if live.ended:
                raise BrowserError("the run ended; the browser session is closed")
            return await self._session(claims)
        except BaseException:
            live.lock.release()
            raise

    def _release(self, live: _Live) -> None:
        live.used = self._clock()
        live.lock.release()


# -- rendering -------------------------------------------------------------------------------


def _notes(state: PageState) -> str:
    return "".join(f"\nNote: {note}" for note in state.notes)


def _header(state: PageState) -> str:
    return f"Tab {state.tab} of {state.tabs}."


def render_state(state: PageState) -> str:
    page = untrusted(f"Title: {state.title}", source=SOURCE, url=state.url)
    return f"{_header(state)}{_notes(state)}\n{page}"


def render_snapshot(shot: Snapshot) -> str:
    state = shot.page
    if shot.dialog is not None:
        dialog = untrusted(
            f"A {shot.dialog.kind} dialog is open: {shot.dialog.message}",
            source=SOURCE,
            url=state.url,
        )
        return (
            f"{_header(state)}{_notes(state)}\n{dialog}\n"
            'Answer it with browser_act action="dialog" (accept true or false).'
        )
    body = f"Title: {state.title}\n\n{shot.tree or '(the page is empty)'}"
    return (
        f"{_header(state)} Use the [ref=...] values with browser_act; a new snapshot makes "
        f"the old refs invalid.{_notes(state)}\n{untrusted(body, source=SOURCE, url=state.url)}"
    )


def render_screenshot(shot: Screenshot) -> str:
    state = shot.page
    return (
        f"{_header(state)} A screenshot of {len(shot.data)} bytes ({shot.mime_type}) follows."
        f"{_notes(state)}\n{untrusted(f'Title: {state.title}', source=SOURCE, url=state.url)}"
    )


def render_extracted(result: Extracted) -> str:
    state = result.page
    body = f"Title: {state.title}\nKind: {result.kind}\n\n{result.content}"
    return f"{_header(state)}{_notes(state)}\n{untrusted(body, source=SOURCE, url=state.url)}"


def render_tabs(tabs: list[Tab]) -> str:
    rows = [
        {"index": tab.index, "active": tab.active, "url": tab.url, "title": tab.title}
        for tab in tabs
    ]
    return f"{len(tabs)} tab(s).\n" + untrusted(
        json.dumps(rows, ensure_ascii=False, indent=1), source=SOURCE
    )


def extract_request(
    kind: ExtractKind, *, ref: str | None, fields: Mapping[str, str] | None
) -> ExtractRequest:
    if kind == "fields" and not fields:
        raise BrowserError("kind fields needs fields: a ref for each name")
    return ExtractRequest(kind=kind, ref=ref, fields=dict(fields or {}))
