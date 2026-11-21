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
they go in an ``untrusted_content`` block. The notes of sama (a blocked request, a new tab) are
fixed text and stay outside the block. The blocked URLs come from the page, so they go in the
block.

The limits on open sessions count a session from the start of ``target.start``: the service
reserves the place before the call, so calls at the same time cannot pass the limits.

The service has one or more targets (Section 14.1): ``cloud`` (Browserbase) and ``my_chrome``
(the extension of the user, M5.5). A run has at most one session on each target. A tool call
names its target, or uses the target of the last browser call of the run, or the first target
of the service. The limits count the sessions of each target.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from sama.browser.target import (
    ACTIONS_WITH_REF,
    MY_CHROME,
    UNTRUSTED_SOURCE,
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
    TargetName,
)
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.outbound import BlockedUrlError
from sama.tools.output import untrusted

log = logging.getLogger("sama.browser")

DEFAULT_IDLE_S: Final = 600.0
REAP_EVERY_S: Final = 30.0
MAX_SESSIONS_PER_ORG: Final = 5
MAX_SESSIONS: Final = 20
SOURCE: Final = UNTRUSTED_SOURCE
SAMA_PREFIX: Final = "mcp__sama__"


@dataclass(slots=True)
class _Live:
    org_id: str
    target: TargetName
    session: BrowserSession | None = None
    reserved: bool = False
    """The entry has a place in the limits: its session is open or starting."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    used: float = field(default_factory=time.monotonic)
    ended: bool = False


class BrowserService:
    """The browser sessions of the runs of this process."""

    def __init__(
        self,
        target: BrowserTarget | Sequence[BrowserTarget],
        guard: NavigationGuard,
        *,
        idle_s: float = DEFAULT_IDLE_S,
        max_sessions_per_org: int = MAX_SESSIONS_PER_ORG,
        max_sessions: int = MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        targets = [target] if not isinstance(target, Sequence) else list(target)
        if not targets:
            raise ValueError("the browser service needs a target")
        self.targets: dict[TargetName, BrowserTarget] = {item.name: item for item in targets}
        self.target = targets[0]
        """The default target: the target of a run that names none."""
        self.guard = guard
        self._idle_s = idle_s
        self._max_per_org = max_sessions_per_org
        self._max = max_sessions
        self._clock = clock
        self._live: dict[tuple[str, str, TargetName], _Live] = {}
        self._current: dict[tuple[str, str], TargetName] = {}
        """The target of the last browser call of each run."""
        self._closing: set[asyncio.Task[None]] = set()
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False

    # -- the life of a session ---------------------------------------------------------------

    def target_of(self, claims: RunTokenClaims, target: TargetName | None) -> TargetName:
        """The target of a call: the named one, else the target of the last browser call of
        the run, else the default target. Raises ``BrowserError`` for a target that this
        process does not have."""
        name = target or self._current.get((claims.org_id, claims.run_id)) or self.target.name
        if name not in self.targets:
            have = ", ".join(sorted(self.targets))
            raise BrowserError(f"the browser target {name} is not available here; use {have}")
        return name

    async def _session(self, claims: RunTokenClaims, live: _Live) -> tuple[_Live, BrowserSession]:
        """The session of the live entry, locked by the caller. It starts the session if the
        entry has none."""
        if live.session is None:
            # Check and reserve with no await between them, so concurrent starts see the place.
            self._check_limits(claims.org_id, live.target)
            live.reserved = True
            try:
                live.session = await self.targets[live.target].start(
                    org_id=claims.org_id,
                    user_id=claims.user_id,
                    run_id=claims.run_id,
                    guard=self.guard,
                    session_id=claims.session_id,
                )
            except BaseException:
                live.reserved = False
                raise
            self._start_reaper()
        live.used = self._clock()
        self._current[(claims.org_id, claims.run_id)] = live.target
        return live, live.session

    def _live_of(self, claims: RunTokenClaims, target: TargetName) -> _Live:
        if self._closed:
            raise BrowserError("the browser service is stopping")
        key = (claims.org_id, claims.run_id, target)
        live = self._live.get(key)
        if live is None or live.ended:
            live = _Live(org_id=claims.org_id, target=target, used=self._clock())
            self._live[key] = live
        return live

    def _check_limits(self, org_id: str, target: TargetName) -> None:
        open_sessions = [
            live for live in self._live.values() if live.reserved and live.target == target
        ]
        if len(open_sessions) >= self._max:
            raise BrowserError("too many browser sessions are open; try again in a few minutes")
        if sum(1 for live in open_sessions if live.org_id == org_id) >= self._max_per_org:
            raise BrowserError(
                f"the org has {self._max_per_org} open browser sessions, the limit; try again "
                "when a run ends"
            )

    def end_run(self, org_id: str, run_id: str) -> None:
        """The run-end hook of the worker. It closes the sessions of the run in the
        background."""
        self._current.pop((org_id, run_id), None)
        for key in [key for key in self._live if key[0] == org_id and key[1] == run_id]:
            live = self._live.pop(key)
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
            live.reserved = False
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
                log.info("closing an idle browser session org=%s run=%s target=%s", *key)
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
        self._current.clear()
        for live in lives:
            live.ended = True
            self._spawn_close(live)
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)
        for target in self.targets.values():
            await target.aclose()

    def open_sessions(self) -> int:
        return sum(1 for live in self._live.values() if live.session is not None)

    def sensitive_reason(
        self, org_id: str, run_id: str, tool_name: str, arguments: Mapping[str, Any]
    ) -> str | None:
        """Why a ``browser_act`` call on the ``my_chrome`` target is a sensitive action (a
        submit, a purchase, a send, a delete, or typing into a password or payment field), or
        ``None``. The tool gate asks the user for such a call even with an ``allow`` rule
        (Section 16.1). It reads the latest snapshot of the session of the run."""
        if tool_name.removeprefix(SAMA_PREFIX) != "browser_act":
            return None
        target = arguments.get("target") or self._current.get((org_id, run_id))
        if target != MY_CHROME:
            return None
        live = self._live.get((org_id, run_id, MY_CHROME))
        session = None if live is None else live.session
        check = getattr(session, "sensitive_reason", None)
        if check is None:
            return None
        reason = check(arguments)
        return None if reason is None else f"a sensitive action in the user's Chrome: {reason}"

    # -- the tools ---------------------------------------------------------------------------

    async def _checked(self, url: str, target: TargetName) -> str:
        url = url.strip()
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if not scheme or "//" not in url:
            url = f"https://{url}"
        elif scheme not in {"http", "https"}:
            raise BrowserError("the browser opens only http and https URLs")
        if target == MY_CHROME:
            # The user's Chrome is on the network of the user, not of sama: the address rules
            # of Section 16.4 do not apply. The blocked list and the consent of the user for
            # each site apply (the session and the extension check them, Section 14.2.2).
            return url
        try:
            await self.guard.check(url)
        except BlockedUrlError as error:
            raise BrowserError(f"the URL policy blocked {url}: {error}") from None
        return url

    async def open(
        self,
        claims: RunTokenClaims,
        url: str,
        *,
        snapshot: bool = True,
        target: TargetName | None = None,
    ) -> str:
        name = self.target_of(claims, target)
        url = await self._checked(url, name)
        live, session = await self._locked_session(claims, name)
        try:
            try:
                state = await session.navigate(url)
            except BlockedUrlError as error:
                raise BrowserError(str(error)) from None
            shot = await session.snapshot() if snapshot else None
        finally:
            self._release(live)
        return render_snapshot(_merged(state, shot)) if shot is not None else render_state(state)

    async def snapshot(self, claims: RunTokenClaims, *, target: TargetName | None = None) -> str:
        live, session = await self._locked_session(claims, self.target_of(claims, target))
        try:
            shot = await session.snapshot()
        finally:
            self._release(live)
        return render_snapshot(shot)

    async def act(
        self,
        claims: RunTokenClaims,
        act: Act,
        *,
        snapshot: bool = True,
        target: TargetName | None = None,
    ) -> str:
        if act.action in ACTIONS_WITH_REF and not act.ref:
            raise BrowserError(f"{act.action} needs ref: the ref of an element from the snapshot")
        if act.action == "drag" and not act.target_ref:
            raise BrowserError("drag needs target_ref: the element to drop on")
        live, session = await self._locked_session(claims, self.target_of(claims, target))
        try:
            state = await session.act(act)
            shot = await session.snapshot() if snapshot else None
        finally:
            self._release(live)
        return render_snapshot(_merged(state, shot)) if shot is not None else render_state(state)

    async def screenshot(
        self,
        claims: RunTokenClaims,
        *,
        full_page: bool = False,
        target: TargetName | None = None,
    ) -> tuple[str, Screenshot]:
        """The caption for the model and the image."""
        live, session = await self._locked_session(claims, self.target_of(claims, target))
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
        target: TargetName | None = None,
    ) -> str:
        request = extract_request(kind, ref=ref, fields=fields)
        live, session = await self._locked_session(claims, self.target_of(claims, target))
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
        target: TargetName | None = None,
    ) -> str:
        if action == "select" and index is None:
            raise BrowserError("select needs index: the index of a tab from the list")
        name = self.target_of(claims, target)
        checked = await self._checked(url, name) if url else None
        live, session = await self._locked_session(claims, name)
        try:
            try:
                tabs = await session.tabs(action, index=index, url=checked)
            except BlockedUrlError as error:
                raise BrowserError(str(error)) from None
        finally:
            self._release(live)
        return render_tabs(tabs)

    async def _locked_session(
        self, claims: RunTokenClaims, target: TargetName
    ) -> tuple[_Live, BrowserSession]:
        live = self._live_of(claims, target)
        await live.lock.acquire()
        try:
            if live.ended:
                raise BrowserError("the run ended; the browser session is closed")
            return await self._session(claims, live)
        except BaseException:
            live.lock.release()
            raise

    def _release(self, live: _Live) -> None:
        live.used = self._clock()
        live.lock.release()


# -- rendering -------------------------------------------------------------------------------


def _merged(state: PageState, shot: Snapshot) -> Snapshot:
    """The snapshot after an action, with the notes and blocked URLs of the action first. The
    snapshot reads the news of the session again, so without this the news of the action is
    lost."""
    notes = state.notes + tuple(note for note in shot.page.notes if note not in state.notes)
    page = replace(shot.page, notes=notes, blocked=state.blocked + shot.page.blocked)
    return replace(shot, page=page)


def _notes(state: PageState) -> str:
    return "".join(f"\nNote: {note}" for note in state.notes)


def _blocked(state: PageState) -> str:
    if not state.blocked:
        return ""
    return "\n\nBlocked by the URL policy:\n" + "\n".join(f"- {url}" for url in state.blocked)


def _header(state: PageState) -> str:
    return f"Tab {state.tab} of {state.tabs}."


def render_state(state: PageState) -> str:
    page = untrusted(f"Title: {state.title}{_blocked(state)}", source=SOURCE, url=state.url)
    return f"{_header(state)}{_notes(state)}\n{page}"


def render_snapshot(shot: Snapshot) -> str:
    state = shot.page
    if shot.dialog is not None:
        dialog = untrusted(
            f"A {shot.dialog.kind} dialog is open: {shot.dialog.message}{_blocked(state)}",
            source=SOURCE,
            url=state.url,
        )
        return (
            f"{_header(state)}{_notes(state)}\n{dialog}\n"
            'Answer it with browser_act action="dialog" (accept true or false).'
        )
    body = f"Title: {state.title}{_blocked(state)}\n\n{shot.tree or '(the page is empty)'}"
    return (
        f"{_header(state)} Use the [ref=...] values with browser_act; a new snapshot makes "
        f"the old refs invalid.{_notes(state)}\n{untrusted(body, source=SOURCE, url=state.url)}"
    )


def render_screenshot(shot: Screenshot) -> str:
    state = shot.page
    return (
        f"{_header(state)} A screenshot of {len(shot.data)} bytes ({shot.mime_type}) follows."
        f"{_notes(state)}\n"
        f"{untrusted(f'Title: {state.title}{_blocked(state)}', source=SOURCE, url=state.url)}"
    )


def render_extracted(result: Extracted) -> str:
    state = result.page
    body = f"Title: {state.title}{_blocked(state)}\nKind: {result.kind}\n\n{result.content}"
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
