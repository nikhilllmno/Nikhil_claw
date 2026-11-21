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
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final, Protocol

import httpx
from pydantic import SecretStr

from sama.browser.cdp import PlaywrightSession
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
    "another run of this user uses the browser profile now, so the logins and cookies of this "
    "run are not kept"
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
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._project_id = project_id
        self._context_save_s = context_save_s
        self._clock = clock
        self._sleep = sleep
        self._writers: set[str] = set()
        """The contexts that a live session of this process writes."""
        self._saved_at: dict[str, float] = {}
        """For each context: the time when Browserbase has saved it (clock time)."""
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
        await self._connector.aclose()
        await self._client.aclose()

    async def start(
        self, *, org_id: str, user_id: str | None, run_id: str, guard: NavigationGuard
    ) -> BrowserSession:
        context_id = await self._context_of(org_id, user_id) if user_id else None
        claimed: list[str] = []
        try:
            return await self._start(context_id, claimed, org_id, user_id, run_id, guard)
        except BaseException:
            # A failed start writes no context. (A release already freed it; no await runs
            # between that release and this line, so no other session claimed it again.)
            self._writers.difference_update(claimed)
            raise

    def _claim(self, context_id: str | None, claimed: list[str]) -> bool:
        """Makes this session the writer of the context if no other session writes it. There
        is no await between the check and the claim."""
        if context_id is None or context_id in self._writers:
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
        response = await self._create_session(context_id, persist, org_id, run_id)
        if context_id is not None and user_id is not None and response.status_code == 404:
            # Browserbase lost the context: forget it and start a new one.
            await self._contexts.forget(org_id, user_id, CLOUD, context_id)
            if persist:
                self._writers.discard(context_id)
                claimed.remove(context_id)
            context_id = await self._context_of(org_id, user_id)
            persist = self._claim(context_id, claimed)
            await self._wait_saved(context_id)
            response = await self._create_session(context_id, persist, org_id, run_id)
        body = self._ok(response, "create a browser session")
        session_id = str(body["id"])
        connect_url = str(body["connectUrl"])
        written = context_id if persist else None
        try:
            browser = await self._connector.connect(connect_url)
        except Exception as error:
            await self._release(session_id, written)
            log.warning("the CDP connection failed session=%s", session_id, exc_info=True)
            raise BrowserError("the browser did not connect; try again") from error
        log.info("browser session started org=%s run=%s session=%s", org_id, run_id, session_id)
        notes = (NOT_KEPT_NOTE,) if context_id is not None and not persist else ()
        try:
            return await PlaywrightSession.attach(
                browser,
                guard,
                target=CLOUD,
                on_close=self._releaser(session_id, written),
                notes=notes,
            )
        except Exception as error:
            await self._release(session_id, written)
            raise BrowserError("the browser did not start; try again") from error

    async def _wait_saved(self, context_id: str | None) -> None:
        if context_id is None:
            return
        wait = self._saved_at.get(context_id, 0.0) - self._clock()
        if wait > 0:
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
        self, context_id: str | None, persist: bool, org_id: str, run_id: str
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            "projectId": self._project_id,
            "timeout": self._session_timeout_s,
            "userMetadata": {"sama_org": org_id, "sama_run": run_id},
        }
        if context_id is not None:
            payload["browserSettings"] = {"context": {"id": context_id, "persist": persist}}
        try:
            return await self._client.post("/v1/sessions", json=payload)
        except httpx.HTTPError as error:
            raise BrowserError(f"Browserbase did not answer: {type(error).__name__}") from None

    async def _release(self, session_id: str, written: str | None = None) -> None:
        """Asks Browserbase to end the session. For a writer of a context, the context is
        free again after the save time."""
        try:
            await self._request_release(session_id)
        finally:
            if written is not None:
                self._saved_at[written] = self._clock() + self._context_save_s
                self._writers.discard(written)

    async def _request_release(self, session_id: str) -> None:
        try:
            response = await self._client.post(
                f"/v1/sessions/{session_id}",
                json={"projectId": self._project_id, "status": "REQUEST_RELEASE"},
            )
            if response.is_error:
                log.warning(
                    "the browser session release failed session=%s status=%d",
                    session_id,
                    response.status_code,
                )
        except httpx.HTTPError:
            log.warning("the browser session release failed session=%s", session_id, exc_info=True)

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
