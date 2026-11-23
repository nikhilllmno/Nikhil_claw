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

M5.3 adds two parts:

- The URL policy of each org (``sama.browser.policy``). The ``cloud`` sessions and the URL
  checks of the ``cloud`` target use the ``OrgGuard`` of the org of the run. The ``my_chrome``
  target keeps its own rules (Section 14.2.2).
- The browser vault (``vault_list`` and ``vault_fill``). The session fills a saved login into the
  page. The user name and the password never go to the agent: each result of the run has them
  replaced with ``[REDACTED]``, also when the page shows them later.

Vision mode (M5.2): ``browser_act`` can name a point (``x``, ``y``) of the viewport instead of a
ref, for pages that have no usable accessibility tree (a ``<canvas>`` app). The service checks
the arguments of a point before the session starts, and refuses a point on a session that has
no vision mode. The caption of a screenshot gives the size of the image, so the agent knows the
range of the points.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Final, Literal
from urllib.parse import quote, quote_plus

from sama.browser.guard import BLANK, OrgGuard
from sama.browser.logins import Login, LoginError, LoginSource, site_allows
from sama.browser.policy import PolicySource
from sama.browser.target import (
    ACTIONS_WITH_POINT,
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
    FillRequest,
    LiveView,
    LoginFiller,
    NavigationGuard,
    PageState,
    Screenshot,
    Snapshot,
    Tab,
    TabAction,
    TargetName,
    has_live_view,
    has_vision,
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
REDACTED: Final = "[REDACTED]"
MIN_SCRUB_CHARS: Final = 3
"""A filled user name shorter than this is not replaced in the results: a short word is in
too much page text. A filled password is always replaced."""
SCREENSHOT_AFTER_FILL: Final = (
    "screenshots are off for the rest of this run after a saved login was filled, because an "
    "image can show the login; use browser_snapshot or browser_extract"
)
NO_LOGINS: Final = (
    "There are no saved logins for you. An org admin adds the logins of the org, and each user "
    "adds personal logins, in the web app (Settings, Browser)."
)
Control = Literal["agent", "pending", "user"]
ControlResult = Literal["done", "busy", "missing"]
TargetPreference = Callable[[RunTokenClaims], Awaitable["TargetName | None"]]
"""Gives the browser that the user chose for the session of the run (M8.3)."""
LIVE_VIEW_TTL_S: Final = 600
"""The live view URL of the web app ends after 10 minutes. The web app asks for a new one
before that."""
CONTROL_WAIT_S: Final = 300.0
"""The longest wait of one browser call while a person has control of the browser."""
HANDOFF_S: Final = 35.0
"""A takeover waits at most this long for the browser call that acts now (a navigation can
take 30 seconds)."""
USER_HAS_CONTROL: Final = (
    "the user has control of the browser now; wait for the user, or ask the user to click Give back"
)
VISION_HINT_REFS: Final = 2
"""A snapshot with this many refs or fewer gets the vision hint."""
VISION_HINT: Final = (
    "the page has few elements with refs (for example a canvas); to act on it, take "
    "browser_screenshot and use browser_act with x and y (vision mode)"
)


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
    agent_turn: asyncio.Event = field(default_factory=asyncio.Event)
    """Set while the agent may act. Clear while a person has control or takes it (M8.3)."""
    handing_over: bool = False
    """A takeover waits for the call that acts now. The person does not have control yet."""

    def __post_init__(self) -> None:
        self.agent_turn.set()

    @property
    def user_control(self) -> bool:
        """True while the agent may not act: a person has control, or a takeover runs."""
        return not self.agent_turn.is_set()

    @property
    def control(self) -> Control:
        if self.agent_turn.is_set():
            return "agent"
        return "pending" if self.handing_over else "user"


@dataclass(frozen=True, slots=True)
class LiveState:
    """The live browser of a run, for the web app (M8.3, Section 14.1)."""

    run_id: str
    target: TargetName
    view: LiveView | None
    """The live view, or ``None`` when the target has none (``my_chrome``) or it failed."""
    control: Control
    """``agent``, ``pending`` (a takeover waits for the call that acts now), or ``user``."""

    @property
    def user_control(self) -> bool:
        return self.control == "user"


class BrowserService:
    """The browser sessions of the runs of this process."""

    def __init__(
        self,
        target: BrowserTarget | Sequence[BrowserTarget],
        guard: NavigationGuard,
        *,
        policies: PolicySource | None = None,
        logins: LoginSource | None = None,
        idle_s: float = DEFAULT_IDLE_S,
        max_sessions_per_org: int = MAX_SESSIONS_PER_ORG,
        max_sessions: int = MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
        control_wait_s: float = CONTROL_WAIT_S,
        handoff_s: float = HANDOFF_S,
        preference: TargetPreference | None = None,
    ) -> None:
        targets = [target] if not isinstance(target, Sequence) else list(target)
        self._control_wait_s = control_wait_s
        self._handoff_s = handoff_s
        self._preference = preference
        """The browser that the user chose for the session of a run (M8.3), or ``None``."""
        if not targets:
            raise ValueError("the browser service needs a target")
        self.targets: dict[TargetName, BrowserTarget] = {item.name: item for item in targets}
        self.target = targets[0]
        """The default target: the target of a run that names none."""
        self.guard = guard
        self.policies = policies
        self.logins = logins
        self._guards: dict[str, NavigationGuard] = {}
        self._secrets: dict[tuple[str, str], set[str]] = {}
        """For each run: the forms of the filled user names and passwords that the results must
        not show."""
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

    async def resolve(self, claims: RunTokenClaims, target: TargetName | None) -> TargetName:
        """The target of a call: the named one, else the target of the last browser call of the
        run, else the browser that the user chose for the session (M8.3), else the default
        target. A failed read of the choice uses the default."""
        if target is None and (claims.org_id, claims.run_id) not in self._current:
            chosen: TargetName | None = None
            if self._preference is not None:
                try:
                    chosen = await self._preference(claims)
                except Exception as error:
                    log.warning(
                        "the browser choice of the session failed run=%s error=%s",
                        claims.run_id,
                        type(error).__name__,
                    )
            if chosen in self.targets:
                target = chosen
        return self.target_of(claims, target)

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
                guard = self.guard if live.target == MY_CHROME else self.guard_for(claims.org_id)
                if isinstance(guard, OrgGuard):
                    # The session starts with the current policy of the org, or not at all.
                    try:
                        await guard.load()
                    except BlockedUrlError as error:
                        raise BrowserError(str(error)) from None
                live.session = await self.targets[live.target].start(
                    org_id=claims.org_id,
                    user_id=claims.user_id,
                    run_id=claims.run_id,
                    guard=guard,
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

    def guard_for(self, org_id: str) -> NavigationGuard:
        """The URL guard of the org for the ``cloud`` target: the host lists of the org and the
        address rules. Without a policy source, the address rules alone."""
        if self.policies is None:
            return self.guard
        guard = self._guards.get(org_id)
        if guard is None:
            guard = self._guards[org_id] = OrgGuard(self.guard, org_id, self.policies)
        return guard

    def forget_policy(self, org_id: str) -> None:
        """Drops the kept URL policy of the org, after an admin changed it."""
        forget = getattr(self.policies, "forget", None)
        if forget is not None:
            forget(org_id)
        guard = self._guards.get(org_id)
        if isinstance(guard, OrgGuard):
            guard.invalidate()

    def end_run(self, org_id: str, run_id: str) -> None:
        """The run-end hook of the worker. It closes the sessions of the run in the
        background."""
        self._secrets.pop((org_id, run_id), None)
        self._current.pop((org_id, run_id), None)
        for key in [key for key in self._live if key[0] == org_id and key[1] == run_id]:
            live = self._live.pop(key)
            live.ended = True
            live.agent_turn.set()  # a call that waits for control ends at once
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
            if not live.lock.locked() and not live.user_control and now - live.used >= self._idle_s
        ]
        for key in idle:
            live = self._live.pop(key)
            live.ended = True
            live.agent_turn.set()
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
            live.agent_turn.set()
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

    async def _checked(self, claims: RunTokenClaims, url: str, target: TargetName) -> str:
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
            await self.guard_for(claims.org_id).check(url)
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
        name = await self.resolve(claims, target)
        url = await self._checked(claims, url, name)
        live, session = await self._locked_session(claims, name)
        try:
            try:
                state = await session.navigate(url)
            except BlockedUrlError as error:
                raise BrowserError(str(error)) from None
            shot = await session.snapshot() if snapshot else None
        except BrowserError as error:
            raise self._clean_error(claims, error) from None
        finally:
            self._release(live)
        text = render_snapshot(_merged(state, shot)) if shot is not None else render_state(state)
        return self._clean(claims, text)

    async def snapshot(self, claims: RunTokenClaims, *, target: TargetName | None = None) -> str:
        live, session = await self._locked_session(claims, await self.resolve(claims, target))
        try:
            shot = await session.snapshot()
        except BrowserError as error:
            raise self._clean_error(claims, error) from None
        finally:
            self._release(live)
        return self._clean(claims, render_snapshot(shot))

    async def act(
        self,
        claims: RunTokenClaims,
        act: Act,
        *,
        snapshot: bool = True,
        target: TargetName | None = None,
    ) -> str:
        check_act(act)
        live, session = await self._locked_session(claims, await self.resolve(claims, target))
        try:
            if act.point is not None and not has_vision(session):
                raise BrowserError(
                    f"the {session.target} browser cannot act at a point (x, y) yet; use a ref "
                    "from browser_snapshot"
                )
            if act.clicks != 1 and not has_vision(session):
                # ``clicks`` came with vision mode: a session without it can ignore it.
                raise BrowserError(
                    f"the {session.target} browser cannot do clicks={act.clicks} yet"
                )
            state = await session.act(act)
            shot = await session.snapshot() if snapshot else None
        except BrowserError as error:
            raise self._clean_error(claims, error) from None
        finally:
            self._release(live)
        text = render_snapshot(_merged(state, shot)) if shot is not None else render_state(state)
        return self._clean(claims, text)

    async def screenshot(
        self,
        claims: RunTokenClaims,
        *,
        full_page: bool = False,
        target: TargetName | None = None,
    ) -> tuple[str, Screenshot]:
        """The caption for the model and the image."""
        if self._secrets.get((claims.org_id, claims.run_id)):
            # An image cannot be scrubbed: the page can show the user name, or the password
            # after a click on "show password". So no screenshot after a fill in this run.
            raise BrowserError(SCREENSHOT_AFTER_FILL)
        live, session = await self._locked_session(claims, await self.resolve(claims, target))
        try:
            shot = await session.screenshot(full_page=full_page)
        except BrowserError as error:
            raise self._clean_error(claims, error) from None
        finally:
            self._release(live)
        return self._clean(claims, render_screenshot(shot)), shot

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
        live, session = await self._locked_session(claims, await self.resolve(claims, target))
        try:
            result = await session.extract(request)
        except BrowserError as error:
            raise self._clean_error(claims, error) from None
        finally:
            self._release(live)
        return self._clean(claims, render_extracted(result))

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
        name = await self.resolve(claims, target)
        checked = await self._checked(claims, url, name) if url else None
        live, session = await self._locked_session(claims, name)
        try:
            try:
                tabs = await session.tabs(action, index=index, url=checked)
            except BlockedUrlError as error:
                raise BrowserError(str(error)) from None
        except BrowserError as error:
            raise self._clean_error(claims, error) from None
        finally:
            self._release(live)
        return self._clean(claims, render_tabs(tabs))

    # -- the vault ---------------------------------------------------------------------------

    async def _usable(self, claims: RunTokenClaims) -> tuple[LoginSource, list[Login]]:
        if self.logins is None:
            raise BrowserError("the browser vault is off in this process")
        if claims.user_id is None:
            raise BrowserError("saved logins work only in a private session of a user")
        try:
            return self.logins, await self.logins.usable(claims.org_id, claims.user_id)
        except LoginError as error:
            raise BrowserError(error.message) from None

    async def vault_list(self, claims: RunTokenClaims) -> str:
        """The names and sites of the logins that the user can use. No secret."""
        _, logins = await self._usable(claims)
        if not logins:
            return NO_LOGINS
        rows = [{"name": login.name, "site": login.site, "scope": login.scope} for login in logins]
        return (
            f"{len(rows)} saved login(s). Fill one with browser_vault action=fill and its name, "
            "on a page of its site. sama fills the user name and the password; you never see "
            "them.\n" + json.dumps(rows, ensure_ascii=False, indent=1)
        )

    async def vault_fill(
        self,
        claims: RunTokenClaims,
        name: str,
        *,
        username_ref: str | None = None,
        password_ref: str | None = None,
        submit: bool = False,
        snapshot: bool = True,
        target: TargetName | None = None,
    ) -> str:
        """Fills the saved login ``name`` into the active tab. The login of the user wins over
        an org login with the same name."""
        source, logins = await self._usable(claims)
        wanted = " ".join(name.split()).casefold()
        matches = [login for login in logins if login.name.casefold() == wanted]
        if not matches:
            raise BrowserError(
                "there is no saved login with this name; call browser_vault action=list"
            )
        login = sorted(matches, key=lambda item: item.user_id is None)[0]
        try:
            credentials = await source.credentials(login)
        except LoginError as error:
            raise BrowserError(error.message) from None
        self._remember(claims, credentials.username.get_secret_value(), username=True)
        self._remember(claims, credentials.password.get_secret_value(), username=False)
        live, session = await self._locked_session(claims, await self.resolve(claims, target))
        try:
            if not isinstance(session, LoginFiller):
                raise BrowserError(
                    "this browser target cannot fill saved logins; use the cloud browser"
                )
            filled = await session.fill_login(
                FillRequest(
                    username=credentials.username,
                    password=credentials.password,
                    site_allows=partial(site_allows, login.site),
                    username_ref=username_ref,
                    password_ref=password_ref,
                    submit=submit,
                )
            )
            shot = await session.snapshot() if snapshot else None
        except BrowserError as error:
            raise self._clean_error(claims, error) from None
        finally:
            self._release(live)
        what = " and the ".join("user name" if f == "username" else f for f in filled.fields)
        note = (
            f"sama filled the {what} of the saved login {json.dumps(login.name)} "
            f"({login.site}); the values are not shown"
            + ("; it pressed Enter to submit" if submit else "")
        )
        state = replace(filled.page, notes=(note, *filled.page.notes))
        text = render_snapshot(_merged(state, shot)) if shot is not None else render_state(state)
        return self._clean(claims, text)

    # -- the secrets of a run ----------------------------------------------------------------

    def _remember(self, claims: RunTokenClaims, value: str, *, username: bool) -> None:
        if not value or (username and len(value) < MIN_SCRUB_CHARS):
            return
        self._secrets.setdefault((claims.org_id, claims.run_id), set()).update(_forms(value))

    def _clean(self, claims: RunTokenClaims, text: str) -> str:
        """Replaces each filled user name and password of the run with ``[REDACTED]``. The
        longest form first, so a value inside another value does not leave a part."""
        secrets = self._secrets.get((claims.org_id, claims.run_id))
        if not secrets:
            return text
        for value in sorted(secrets, key=len, reverse=True):
            text = text.replace(value, REDACTED)
        return text

    def _clean_error(self, claims: RunTokenClaims, error: BrowserError) -> BrowserError:
        if not self._secrets.get((claims.org_id, claims.run_id)):
            return error
        detail = self._clean(claims, error.detail) if error.detail else error.detail
        return BrowserError(self._clean(claims, error.message), detail=detail)

    async def _locked_session(
        self, claims: RunTokenClaims, target: TargetName
    ) -> tuple[_Live, BrowserSession]:
        """The session of the run, with its lock. While a person has control (M8.3), the call
        waits before it takes the lock, so the person's takeover only waits for a call that
        already acts, and a waiting call does not hold the lock."""
        live = self._live_of(claims, target)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._control_wait_s
        while True:
            if live.user_control:
                try:
                    async with asyncio.timeout(max(0.0, deadline - loop.time())):
                        await live.agent_turn.wait()
                except TimeoutError:
                    raise BrowserError(USER_HAS_CONTROL) from None
            await live.lock.acquire()
            if live.user_control and not live.ended:
                # A person took control while the call waited for the lock.
                live.lock.release()
                continue
            try:
                if live.ended:
                    raise BrowserError("the run ended; the browser session is closed")
                return await self._session(claims, live)
            except BaseException:
                live.lock.release()
                raise

    # -- the live view (M8.3) ----------------------------------------------------------------

    async def live_state(self, org_id: str, run_id: str) -> LiveState | None:
        """The live browser of a run: the open session of the target of its last browser
        call (else any open session of the run). ``None`` when the run has no open session."""
        lives = {
            key[2]: live
            for key, live in self._live.items()
            if key[0] == org_id and key[1] == run_id and live.session is not None and not live.ended
        }
        if not lives:
            return None
        target = self._current.get((org_id, run_id))
        name = target if target in lives else next(iter(lives))
        live = lives[name]
        view: LiveView | None = None
        if live.session is not None and has_live_view(live.session):
            try:
                view = await live.session.live_view(LIVE_VIEW_TTL_S)  # type: ignore[attr-defined]
            except Exception as error:
                log.warning(
                    "the live view failed org=%s run=%s error=%s",
                    org_id,
                    run_id,
                    type(error).__name__,
                )
        return LiveState(run_id=run_id, target=name, view=view, control=live.control)

    async def set_control(self, org_id: str, run_id: str, *, user: bool) -> ControlResult:
        """Gives the browser of the run to a person (``user=True``) or back to the agent.

        A takeover first stops new browser calls of the run (``pending``), then waits until a
        call that acts now ends (at most ``handoff_s``). Only then does the person have control
        (``done``), so the agent sends no more input. When the call does not end in time, the
        takeover is undone (``busy``). While a person has control, the browser calls of the run
        wait (at most ``control_wait_s`` each), and the idle reaper keeps the session.
        ``missing`` when the run has no open session."""
        lives = [
            live
            for key, live in self._live.items()
            if key[0] == org_id and key[1] == run_id and not live.ended and live.session is not None
        ]
        if not lives:
            return "missing"
        for live in lives:
            live.used = self._clock()
        if not user:
            for live in lives:
                live.handing_over = False
                live.agent_turn.set()
            return "done"
        for live in lives:
            live.handing_over = True
            live.agent_turn.clear()
        try:
            for live in lives:
                async with asyncio.timeout(self._handoff_s):
                    await live.lock.acquire()
                live.lock.release()
        except TimeoutError:
            for live in lives:
                live.handing_over = False
                live.agent_turn.set()
            return "busy"
        for live in lives:
            live.handing_over = False
        return "done"

    def _release(self, live: _Live) -> None:
        live.used = self._clock()
        live.lock.release()


# -- rendering -------------------------------------------------------------------------------


def _forms(value: str) -> set[str]:
    """The forms of a value in a result: as it is, escaped in JSON (an extract), escaped in
    HTML (a URL in an attribute), and encoded in a URL (a form that sends with GET, or a script
    that encodes it)."""
    forms = {
        value,
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        html.escape(value, quote=True),
        quote(value, safe=""),
        quote_plus(value, safe=""),
        _form_encoded(value),
        _form_encoded(value).replace("+", "%20"),
        quote(value, safe="~!*'()"),  # encodeURIComponent of JavaScript
    }
    return {form for form in forms if form}


def _form_encoded(value: str) -> str:
    """The ``application/x-www-form-urlencoded`` form of a browser (a form that sends with
    GET): only letters, digits, and ``*-._`` stay; a space is ``+``. Python's ``quote_plus``
    keeps ``~``, and a browser does not."""
    out = []
    for char in value:
        if char.isascii() and (char.isalnum() or char in "*-._"):
            out.append(char)
        elif char == " ":
            out.append("+")
        else:
            out.append("".join(f"%{byte:02X}" for byte in char.encode()))
    return "".join(out)


def check_act(act: Act) -> None:
    """Refuses the arguments of ``browser_act`` that do not fit the action, before a session
    starts. An action names its element with a ref or with a point (vision mode), not both."""
    has_x, has_y = act.x is not None, act.y is not None
    if has_x != has_y:
        raise BrowserError("a point needs both x and y")
    if (act.to_x is None) != (act.to_y is None):
        raise BrowserError("a drop point needs both to_x and to_y")
    point = act.point is not None
    if point:
        if act.action not in ACTIONS_WITH_POINT:
            allowed = ", ".join(sorted(ACTIONS_WITH_POINT))
            raise BrowserError(f"x and y work only with {allowed}; {act.action} does not use them")
        if act.ref:
            raise BrowserError("give ref or x and y, not both")
    if act.to_point is not None and not (act.action == "drag" and point):
        raise BrowserError("to_x and to_y work only with drag at a point (x and y)")
    if act.clicks != 1 and act.action != "click":
        raise BrowserError("clicks works only with click")
    if act.action == "drag":
        if point:
            if act.target_ref:
                raise BrowserError("a drag at a point needs to_x and to_y, not target_ref")
            if act.to_point is None:
                raise BrowserError("drag at a point needs to_x and to_y: the point to drop on")
            return
        if not act.target_ref and act.ref:
            raise BrowserError("drag needs target_ref: the element to drop on")
    if act.action in ACTIONS_WITH_REF and not act.ref and not point:
        how = " or x and y (vision mode)" if act.action in ACTIONS_WITH_POINT else ""
        raise BrowserError(f"{act.action} needs ref: the ref of an element from the snapshot{how}")


def _merged(state: PageState, shot: Snapshot) -> Snapshot:
    """The snapshot after an action, with the notes and blocked URLs of the action first. The
    snapshot reads the news of the session again, so without this the news of the action is
    lost."""
    notes = state.notes + tuple(note for note in shot.page.notes if note not in state.notes)
    page = replace(shot.page, notes=notes, blocked=state.blocked + shot.page.blocked)
    return replace(shot, page=page)


BOT_CHECK_TITLE: Final = re.compile(
    r"access denied|just a moment|attention required|are you a (robot|human)|captcha|"
    r"verify you are human|request blocked|pardon our interruption|^40[3-9]\b|forbidden",
    re.IGNORECASE,
)
"""Titles of the pages that sites show to a browser that they block (a bot check)."""
BOT_CHECK_NOTE: Final = (
    "this page looks like a bot check or a block of the site, not the content; tell the user "
    "in one line that the site blocks the cloud browser, then try another site, or offer the "
    "user's own Chrome (target my_chrome)"
)


def _notes(state: PageState) -> str:
    notes = list(state.notes)
    if state.title and BOT_CHECK_TITLE.search(state.title.strip()):
        notes.append(BOT_CHECK_NOTE)
    return "".join(f"\nNote: {note}" for note in notes)


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
        f"the old refs invalid.{_notes(state)}{_vision_hint(shot)}\n"
        f"{untrusted(body, source=SOURCE, url=state.url)}"
    )


def _vision_hint(shot: Snapshot) -> str:
    """A hint for a page with few refs, for example a ``<canvas>`` app: the agent can look at a
    screenshot and act at points. A blank tab gets no hint."""
    if shot.page.url == BLANK or shot.tree.count("[ref=") > VISION_HINT_REFS:
        return ""
    return f"\nNote: {VISION_HINT}"


def render_screenshot(shot: Screenshot) -> str:
    state = shot.page
    return (
        f"{_header(state)} A screenshot of {len(shot.data)} bytes ({shot.mime_type}) follows."
        f"{_size(shot)}{_notes(state)}\n"
        f"{untrusted(f'Title: {state.title}{_blocked(state)}', source=SOURCE, url=state.url)}"
    )


def _size(shot: Screenshot) -> str:
    """The size of the image, and how a point of it maps to ``browser_act`` (vision mode)."""
    if not (shot.width and shot.height):
        return ""
    size = f" The image is {shot.width}x{shot.height} pixels."
    if shot.full_page:
        return (
            f"{size} It is the full page, so its points are not viewport points: for browser_act "
            "with x and y, take a screenshot with full_page=false."
        )
    return (
        f"{size} It is the viewport: browser_act takes x (0 to {shot.width - 1}) and y (0 to "
        f"{shot.height - 1}) in these pixels, from the top left corner."
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
