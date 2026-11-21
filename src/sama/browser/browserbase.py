"""The ``cloud`` browser target: Browserbase (docs/architecture.md, Section 14.1).

For each run that uses a browser tool, the target creates a Browserbase session and connects to
it over CDP with Playwright. A private run uses the persistent context of its user
(``browserSettings.context`` with ``persist``), so cookies and logins stay from run to run. A
shared run gets a new context that is not kept, so no login of a person reaches a group.

The API key stays in the control plane: it goes only to the Browserbase API. The session has a
hard ``timeout`` at Browserbase, so a session that sama fails to release still ends.

Browserbase saves a persistent context when its session ends, and it needs a few seconds for
that. Two sessions that write one context at the same time can lose a login. So the target:

- lets one session at a time write a context (``persist``). A second run of the same user at
  the same time gets the saved state without ``persist``, and a note tells the agent that its
  logins are not kept;
- waits ``CONTEXT_SAVE_S`` after the release of a writer before a new session of the same
  context starts.

This coordination is for one process. Two control-plane processes can still write one context
at the same time; the last save wins.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Final, Protocol

import httpx
from pydantic import SecretStr

from sama.browser.cdp import CLOSE_TIMEOUT_S, PlaywrightSession
from sama.browser.contexts import BrowserContextStore
from sama.browser.target import CLOUD, BrowserError, BrowserSession, NavigationGuard, TargetName

if TYPE_CHECKING:
    from playwright.async_api import Browser, Playwright

log = logging.getLogger("sama.browser")

API_URL: Final = "https://api.browserbase.com"
API_TIMEOUT_S: Final = 30.0
CONNECT_TIMEOUT_MS: Final = 30_000
DEFAULT_SESSION_TIMEOUT_S: Final = 900
"""The hard limit of one Browserbase session. sama releases a session at the end of its run or
after it is idle, so this limit is a guard."""
MAX_MESSAGE_CHARS: Final = 200
CONTEXT_SAVE_S: Final = 10.0
"""After a writer session of a persistent context ends, the time that Browserbase can need to
save the context. A new session of the context waits for the rest of this time."""
NOT_KEPT_NOTE: Final = (
    "another run of this user uses the browser profile now (or an earlier session did not end "
    "cleanly), so the logins and cookies of this run are not kept"
)


class CdpConnector(Protocol):
    async def connect(self, url: str) -> Browser: ...

    async def aclose(self) -> None: ...


class PlaywrightConnector:
    """Connects with one Playwright driver for the process. The driver starts at the first
    connection."""

    def __init__(self, start: Callable[[], Awaitable[Playwright]] | None = None) -> None:
        self._playwright: Playwright | None = None
        self._lock = asyncio.Lock()
        self._start = start or _start_playwright

    async def _driver(self) -> Playwright:
        """The driver of the process. The lock makes sure that concurrent first connections
        start one driver, not one each."""
        async with self._lock:
            if self._playwright is None:
                self._playwright = await self._start()
            return self._playwright

    async def connect(self, url: str) -> Browser:
        playwright = await self._driver()
        return await playwright.chromium.connect_over_cdp(url, timeout=CONNECT_TIMEOUT_MS)

    async def aclose(self) -> None:
        async with self._lock:
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None


async def _start_playwright() -> Playwright:
    from playwright.async_api import async_playwright

    return await async_playwright().start()


class BrowserbaseTarget:
    """Starts Browserbase sessions."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        project_id: str,
        contexts: BrowserContextStore,
        api_url: str = API_URL,
        session_timeout_s: int = DEFAULT_SESSION_TIMEOUT_S,
        connector: CdpConnector | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        context_save_s: float = CONTEXT_SAVE_S,
        record: bool = False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._project_id = project_id
        self._record = record
        self._context_save_s = context_save_s
        self._clock = clock
        self._sleep = sleep
        self._writers: set[str] = set()
        """The contexts that a live session of this process writes."""
        self._saved_at: dict[str, float] = {}
        """For each context: the time when Browserbase has saved it (clock time)."""
        self._cleanups: set[asyncio.Task[None]] = set()
        self._contexts = contexts
        self._session_timeout_s = session_timeout_s
        self._connector = connector or PlaywrightConnector()
        self._client = httpx.AsyncClient(
            base_url=api_url,
            headers={"X-BB-API-Key": api_key.get_secret_value()},
            timeout=API_TIMEOUT_S,
            transport=transport,
            trust_env=False,
        )

    @property
    def name(self) -> TargetName:
        return CLOUD

    async def aclose(self) -> None:
        if self._cleanups:
            await asyncio.gather(*self._cleanups, return_exceptions=True)
        await self._connector.aclose()
        await self._client.aclose()

    async def start(
        self,
        *,
        org_id: str,
        user_id: str | None,
        run_id: str,
        guard: NavigationGuard,
        session_id: str = "",
    ) -> BrowserSession:
        context_id = await self._context_of(org_id, user_id) if user_id else None
        claimed: list[str] = []
        try:
            return await self._start(context_id, claimed, org_id, user_id, run_id, guard)
        except BaseException:
            # A start that failed before Browserbase made its session writes no context. After
            # that, the release of the session frees the context (``claimed`` is empty then).
            self._writers.difference_update(claimed)
            raise

    def _claim(self, context_id: str | None, claimed: list[str]) -> bool:
        """Makes this session the writer of the context if no other session writes it and no
        unconfirmed session can still write it. There is no await between the check and the
        claim."""
        if context_id is None or context_id in self._writers or self._quarantined(context_id):
            return False
        self._writers.add(context_id)
        claimed.append(context_id)
        return True

    async def _start(
        self,
        context_id: str | None,
        claimed: list[str],
        org_id: str,
        user_id: str | None,
        run_id: str,
        guard: NavigationGuard,
    ) -> BrowserSession:
        persist = self._claim(context_id, claimed)
        await self._wait_saved(context_id)
        response = await self._create_session(context_id, persist, claimed, org_id, run_id)
        if context_id is not None and user_id is not None and response.status_code == 404:
            # Browserbase lost the context: forget it and start a new one.
            await self._contexts.forget(org_id, user_id, CLOUD, context_id)
            if persist:
                self._writers.discard(context_id)
                claimed.remove(context_id)
            context_id = await self._context_of(org_id, user_id)
            persist = self._claim(context_id, claimed)
            await self._wait_saved(context_id)
            response = await self._create_session(context_id, persist, claimed, org_id, run_id)
        body = self._ok(response, "create a browser session")
        session_id = str(body["id"])
        connect_url = str(body["connectUrl"])
        written = context_id if persist else None
        # From here the release of this session frees the context, not the caller.
        claimed.clear()
        notes = (NOT_KEPT_NOTE,) if context_id is not None and not persist else ()
        browser: Browser | None = None
        try:
            browser = await self._connector.connect(connect_url)
            session = await PlaywrightSession.attach(
                browser,
                guard,
                target=CLOUD,
                on_close=self._releaser(session_id, written),
                notes=notes,
            )
        except BaseException as error:
            # Also a cancelled start ends the remote session before it frees the context.
            cleanup = asyncio.ensure_future(self._abandon(browser, session_id, written))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                if isinstance(error, Exception):
                    raise
            if not isinstance(error, Exception):
                raise
            if browser is None:
                log.warning("the CDP connection failed session=%s", session_id, exc_info=True)
                raise BrowserError("the browser did not connect; try again") from error
            raise BrowserError("the browser did not start; try again") from error
        log.info("browser session started org=%s run=%s session=%s", org_id, run_id, session_id)
        return session

    async def _abandon(self, browser: Browser | None, session_id: str, written: str | None) -> None:
        if browser is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(CLOSE_TIMEOUT_S):
                    await browser.close()
        await self._release(session_id, written)

    def _quarantined(self, context_id: str) -> bool:
        """True while a session whose release Browserbase did not confirm can still write the
        context (until its hard timeout)."""
        return self._saved_at.get(context_id, 0.0) - self._clock() > self._context_save_s

    async def _wait_saved(self, context_id: str | None) -> None:
        if context_id is None:
            return
        wait = self._saved_at.get(context_id, 0.0) - self._clock()
        if 0 < wait <= self._context_save_s:  # a quarantined context makes a reader; no wait
            await self._sleep(wait)

    def _releaser(self, session_id: str, written: str | None) -> Callable[[], Any]:
        async def release() -> None:
            await self._release(session_id, written)

        return release

    async def _context_of(self, org_id: str, user_id: str) -> str:
        known = await self._contexts.get(org_id, user_id, CLOUD)
        if known is not None:
            return known
        try:
            response = await self._client.post("/v1/contexts", json={"projectId": self._project_id})
        except httpx.HTTPError as error:
            raise BrowserError(f"Browserbase did not answer: {type(error).__name__}") from None
        created = str(self._ok(response, "create a browser context")["id"])
        stored = await self._contexts.claim(org_id, user_id, CLOUD, created)
        if stored != created:
            # Another run of the user stored a context first. Use it, and delete the new one.
            await self._delete_context(created)
        return stored

    async def _create_session(
        self, context_id: str | None, persist: bool, claimed: list[str], org_id: str, run_id: str
    ) -> httpx.Response:
        """Creates the session. When the caller is cancelled during the request, Browserbase can
        still make the session, so a task waits for the answer, ends that session, and then
        frees the context."""
        request = asyncio.ensure_future(self._post_session(context_id, persist, org_id, run_id))
        try:
            return await asyncio.shield(request)
        except asyncio.CancelledError:
            written = context_id if persist else None
            if written is not None and written in claimed:
                claimed.remove(written)  # the task frees it
            cleanup = asyncio.ensure_future(self._end_unowned(request, written))
            self._cleanups.add(cleanup)
            cleanup.add_done_callback(self._cleanups.discard)
            raise

    async def _end_unowned(
        self, request: asyncio.Future[httpx.Response], written: str | None
    ) -> None:
        session_id: str | None = None
        try:
            response = await request
            if response.is_success:
                session_id = str(response.json()["id"])
        except Exception:
            log.warning("a cancelled browser session start failed", exc_info=True)
        if session_id is not None:
            await self._release(session_id, written)
        elif written is not None:
            self._writers.discard(written)

    async def _post_session(
        self, context_id: str | None, persist: bool, org_id: str, run_id: str
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            "projectId": self._project_id,
            "timeout": self._session_timeout_s,
            "userMetadata": {"sama_org": org_id, "sama_run": run_id},
        }
        # No recording and no logs at Browserbase by default: a recording can show a page with a
        # login, and a log can keep a form body with a password (M5.3).
        settings: dict[str, Any] = {"recordSession": self._record, "logSession": self._record}
        if context_id is not None:
            settings["context"] = {"id": context_id, "persist": persist}
        payload["browserSettings"] = settings
        try:
            return await self._client.post("/v1/sessions", json=payload)
        except httpx.HTTPError as error:
            raise BrowserError(f"Browserbase did not answer: {type(error).__name__}") from None

    async def _release(self, session_id: str, written: str | None = None) -> None:
        """Asks Browserbase to end the session. For a writer of a context, the context is
        free again after the save time. When Browserbase does not confirm the release, the
        session can live until its hard timeout, so no other session writes the context
        until then."""
        released = False
        try:
            released = await self._request_release(session_id)
        finally:
            if written is not None:
                hold = self._context_save_s
                if not released:
                    hold += self._session_timeout_s
                self._saved_at[written] = self._clock() + hold
                self._writers.discard(written)

    async def _request_release(self, session_id: str) -> bool:
        try:
            response = await self._client.post(
                f"/v1/sessions/{session_id}",
                json={"projectId": self._project_id, "status": "REQUEST_RELEASE"},
            )
        except httpx.HTTPError:
            log.warning("the browser session release failed session=%s", session_id, exc_info=True)
            return False
        if response.is_error:
            log.warning(
                "the browser session release failed session=%s status=%d",
                session_id,
                response.status_code,
            )
            return False
        return True

    async def delete_contexts(self, context_ids: Sequence[str]) -> None:
        """Deletes contexts at Browserbase, for the purge worker after a user or org delete
        (Section 17.4). A context that Browserbase does not know counts as deleted. Raises
        ``BrowserError`` when a delete fails, so the worker tries again later."""
        for context_id in context_ids:
            try:
                response = await self._client.delete(f"/v1/contexts/{context_id}")
            except httpx.HTTPError as error:
                raise BrowserError(f"Browserbase did not answer: {type(error).__name__}") from None
            if not (response.is_success or response.status_code == 404):
                raise BrowserError(f"Browserbase did not delete a context ({response.status_code})")

    async def _delete_context(self, context_id: str) -> None:
        try:
            await self._client.delete(f"/v1/contexts/{context_id}")
        except httpx.HTTPError:
            log.warning("an extra browser context was not deleted context=%s", context_id)

    def _ok(self, response: httpx.Response, what: str) -> dict[str, Any]:
        if response.is_success:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                return body
        message = ""
        try:
            detail = response.json()
            if isinstance(detail, dict):
                message = str(detail.get("message") or detail.get("error") or "")
        except ValueError:
            pass
        suffix = f": {message[:MAX_MESSAGE_CHARS]}" if message else ""
        raise BrowserError(
            f"Browserbase did not {what} (HTTP {response.status_code}){suffix}"
        ) from None
