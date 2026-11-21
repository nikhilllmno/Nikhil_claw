"""The ``cloud`` browser target: Browserbase (docs/architecture.md, Section 14.1).

For each run that uses a browser tool, the target creates a Browserbase session and connects to
it over CDP with Playwright. A private run uses the persistent context of its user
(``browserSettings.context`` with ``persist``), so cookies and logins stay from run to run. A
shared run gets a new context that is not kept, so no login of a person reaches a group.

The API key stays in the control plane: it goes only to the Browserbase API. The session has a
hard ``timeout`` at Browserbase, so a session that sama fails to release still ends.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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


class CdpConnector(Protocol):
    async def connect(self, url: str) -> Browser: ...

    async def aclose(self) -> None: ...


class PlaywrightConnector:
    """Connects with one Playwright driver for the process. The driver starts at the first
    connection."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None

    async def connect(self, url: str) -> Browser:
        if self._playwright is None:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
        return await self._playwright.chromium.connect_over_cdp(url, timeout=CONNECT_TIMEOUT_MS)

    async def aclose(self) -> None:
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


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
    ) -> None:
        self._project_id = project_id
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
        response = await self._create_session(context_id, org_id, run_id)
        if context_id is not None and user_id is not None and response.status_code == 404:
            # Browserbase lost the context: forget it and start a new one.
            await self._contexts.forget(org_id, user_id, CLOUD, context_id)
            context_id = await self._context_of(org_id, user_id)
            response = await self._create_session(context_id, org_id, run_id)
        body = self._ok(response, "create a browser session")
        session_id = str(body["id"])
        connect_url = str(body["connectUrl"])
        try:
            browser = await self._connector.connect(connect_url)
        except Exception as error:
            await self._release(session_id)
            log.warning("the CDP connection failed session=%s", session_id, exc_info=True)
            raise BrowserError("the browser did not connect; try again") from error
        log.info("browser session started org=%s run=%s session=%s", org_id, run_id, session_id)
        try:
            return await PlaywrightSession.attach(
                browser, guard, target=CLOUD, on_close=self._releaser(session_id)
            )
        except Exception as error:
            await self._release(session_id)
            raise BrowserError("the browser did not start; try again") from error

    def _releaser(self, session_id: str) -> Callable[[], Any]:
        async def release() -> None:
            await self._release(session_id)

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
        self, context_id: str | None, org_id: str, run_id: str
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            "projectId": self._project_id,
            "timeout": self._session_timeout_s,
            "userMetadata": {"sama_org": org_id, "sama_run": run_id},
        }
        if context_id is not None:
            payload["browserSettings"] = {"context": {"id": context_id, "persist": True}}
        try:
            return await self._client.post("/v1/sessions", json=payload)
        except httpx.HTTPError as error:
            raise BrowserError(f"Browserbase did not answer: {type(error).__name__}") from None

    async def _release(self, session_id: str) -> None:
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
