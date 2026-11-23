"""A browser session over the Chrome DevTools Protocol, with Playwright (Section 14.1).

``PlaywrightSession`` does the page work for a target that gives a Playwright ``Browser``: the
``cloud`` target connects to a Browserbase session with ``connect_over_cdp``. It needs the
Playwright client only, no browser binaries.

- A snapshot is the accessibility tree of the active tab in the ``ai`` mode of Playwright. Each
  element that an action can use has ``[ref=eN]``. A ref is good until the next snapshot.
- The URL guard sees every request of the browser, also each redirect hop. A browser-level CDP
  session enables the ``Fetch`` domain, so Chrome pauses each request of each target (tabs,
  popups, frames in other processes, workers) and each redirect hop until sama answers. A
  document request passes the full check with DNS. Every other request passes the check
  without DNS. A blocked request fails before it leaves the browser. (Playwright ``route``
  sees only the first request of a redirect chain, so sama does not use it.)
- ``route_web_socket`` checks each WebSocket that a page opens before the handshake. The
  WebSockets of workers do not pass this check.
- A blocked navigation becomes a note for the agent. The blocked URL comes from the page, so it
  goes in the untrusted block, not in the note.
- A JavaScript dialog stays open until ``act`` with ``action="dialog"``. An action that opens a
  dialog returns when the dialog opens, and it ends after the answer. While the dialog is open,
  the snapshot shows the dialog and the other actions fail with a clear error.
- A blocked navigation leaves an error page in the tab. The session goes back to the page
  before it.
- A tab that a page opens (a popup or ``target=_blank``) becomes the active tab.
- ``fill_login`` fills a saved login (M5.3) into the password field and the user name field of
  the active tab, only in a document of the site of the login. A snapshot shows the value of a
  password field as ``[hidden]``, and an extract of a password field gives ``[hidden]``.
- Vision mode (M5.2): ``click``, ``type``, ``hover``, ``drag``, and ``scroll`` can act at a point
  (``x``, ``y``) instead of a ref, with the mouse and the keyboard of Playwright (CDP
  ``Input`` events). A point is in CSS pixels of the viewport. A viewport screenshot has the
  scale ``css``, so one pixel of the image is one CSS pixel, and the agent can give a point of
  the image. A point outside the viewport fails before the browser acts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar, Final

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from sama.browser.guard import BLANK, PAGE_SCHEMES, FreshGuard
from sama.browser.target import (
    ACTIONS_WITH_REF,
    Act,
    BrowserError,
    Dialog,
    Extracted,
    ExtractRequest,
    Filled,
    FillRequest,
    LiveView,
    NavigationGuard,
    PageState,
    Screenshot,
    Snapshot,
    Tab,
    TabAction,
    TargetName,
)
from sama.tools.outbound import BlockedUrlError

if TYPE_CHECKING:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        CDPSession,
        ElementHandle,
        Locator,
        Page,
        WebSocketRoute,
    )
    from playwright.async_api import (
        Dialog as PlaywrightDialog,
    )

log = logging.getLogger("sama.browser")
LiveUrls = Callable[[int], Awaitable[list[dict[str, Any]]]]
"""Gives the tabs of a remote browser session with their live view URLs, for a time to live in
seconds (Browserbase ``GET /v1/sessions/{id}/debug``, field ``pages``)."""
LIVE_TITLE_TIMEOUT_S: Final = 1.0
MAX_LIVE_TITLE_CHARS: Final = 300

NAVIGATION_TIMEOUT_MS: Final = 30_000
ACTION_TIMEOUT_MS: Final = 10_000
SETTLE_TIMEOUT_MS: Final = 5_000
"""After an action, the longest wait for a navigation that the action started."""
SETTLE_DELAY_S: Final = 0.5
"""After an action that can open a page (click, press, submit), the time for the browser to
start the navigation or open the new tab."""
ERROR_PAGE: Final = "chrome-error://"
CLOSE_TIMEOUT_S: Final = 10.0
MAX_TABS: Final = 10
MAX_WAIT_S: Final = 10.0
MAX_TEXT_CHARS: Final = 100_000
MAX_LINKS: Final = 500
MAX_TABLES: Final = 20
MAX_ROWS: Final = 200
DRAG_STEPS: Final = 12
"""The mouse moves of a drag at points. A page that draws while the mouse moves (a canvas)
sees the path, not one jump."""
MAX_SCREENSHOT_HEIGHT: Final = 6_000
"""A full-page screenshot stops at this height. A model cannot read a very tall image."""
SCREENSHOT_QUALITY: Final = 70
MAX_ERROR_CHARS: Final = 300
MAX_BLOCKED: Final = 10
"""The most blocked URLs that one state reports."""
DOCUMENT: Final = "Document"
"""The CDP resource type of a navigation request (a tab or a frame)."""
WS_CLOSE_POLICY: Final = 1008
BLOCKED_NOTE: Final = (
    "the URL policy blocked a request of the page; the blocked URLs are in the untrusted block"
)
_OTHER_SITE: Final = (
    "the field is not on the site of this login; sama fills a login only on its own site"
)
REDIRECT_BLOCKED: Final = (
    "the URL policy blocked the navigation (for example a redirect to a blocked address); the "
    "page did not open"
)

_LINKS_JS: Final = """(root, limit) => {
  const out = [];
  for (const a of root.querySelectorAll('a[href]')) {
    if (out.length >= limit) break;
    const text = (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
    out.push({text: text.slice(0, 200), href: a.href});
  }
  return out;
}"""
_TABLES_JS: Final = """(root, limits) => {
  const [maxTables, maxRows] = limits;
  const out = [];
  const tables = root.tagName === 'TABLE' ? [root] : root.querySelectorAll('table');
  for (const table of tables) {
    if (out.length >= maxTables) break;
    const rows = [];
    for (const row of table.rows) {
      if (rows.length >= maxRows) break;
      rows.push(Array.from(row.cells, c => c.innerText.trim().replace(/\\s+/g, ' ').slice(0, 500)));
    }
    out.push(rows);
  }
  return out;
}"""
_FIELD_JS: Final = """el => {
  const tag = el.tagName;
  if (tag === 'INPUT' && el.type === 'password') return el.value ? '[hidden]' : '';
  if (tag === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) return el.checked;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return el.value;
  if (tag === 'SELECT') return Array.from(el.selectedOptions, o => o.label).join(', ');
  return (el.innerText || '').trim();
}"""
_HEIGHT_JS: Final = "() => document.documentElement.scrollHeight"
HIDDEN: Final = "[hidden]"
"""What a snapshot shows in place of the value of a password field."""
FILL_MARK: Final = "data-sama-login"
"""A short-lived attribute that marks the user name field that the page script found."""
_PASSWORD_VALUES_JS: Final = """() => Array.from(
  document.querySelectorAll('input[type=password]'), e => e.value).filter(v => v)"""  # noqa: S105
_IS_PASSWORD_JS: Final = "el => el.tagName === 'INPUT' && el.type === 'password'"  # noqa: S105
_IS_TEXT_JS: Final = """el => (el.tagName === 'INPUT'
  && ['text', 'email', 'tel', ''].includes(el.getAttribute('type') || ''))
  || el.tagName === 'TEXTAREA'"""
_MARK_USERNAME_JS: Final = """(pw) => {
  const mark = 'data-sama-login';
  for (const old of document.querySelectorAll('[' + mark + ']')) old.removeAttribute(mark);
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
    && getComputedStyle(el).visibility !== 'hidden' && !el.disabled && !el.readOnly;
  const textual = el => el.tagName === 'INPUT'
    && ['text', 'email', 'tel', ''].includes(el.getAttribute('type') || '');
  const scope = (pw && pw.form) || document;
  const inputs = Array.from(scope.querySelectorAll('input'))
    .filter(el => textual(el) && visible(el));
  const named = inputs.find(
    el => /(^|\\s)username(\\s|$)/.test(el.getAttribute('autocomplete') || ''))
    || inputs.find(el => el.type === 'email');
  let found = named;
  if (!found && pw) {
    const before = inputs.filter(
      el => el.compareDocumentPosition(pw) & Node.DOCUMENT_POSITION_FOLLOWING);
    found = before[before.length - 1];
  }
  if (!found) {
    found = inputs.find(el => /user|email|login|account|identifier/i.test(
      (el.name || '') + ' ' + (el.id || '') + ' ' + (el.getAttribute('aria-label') || '')));
  }
  if (!found) return false;
  found.setAttribute(mark, 'username');
  return true;
}"""
_SET_VALUE_JS: Final = """(el, value) => {
  if (el.disabled || el.readOnly) return false;
  const proto = el.tagName === 'TEXTAREA'
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}"""
_UNMARK_JS: Final = """() => {
  const mark = 'data-sama-login';
  for (const el of document.querySelectorAll('[' + mark + ']')) el.removeAttribute(mark);
}"""


async def _handle_allowed(handle: ElementHandle, request: FillRequest) -> bool:
    """True if the frame of the element, as the browser reports it, is of the site. The page
    cannot change this answer with JavaScript."""
    try:
        frame = await handle.owner_frame()
    except PlaywrightError:
        return False
    return frame is not None and not frame.is_detached() and request.site_allows(frame.url)


def hide_values(tree: str, values: list[str]) -> str:
    """Replaces the value of each password field in a snapshot with ``[hidden]``. A snapshot
    shows the value of a text box after its name (``- textbox "Password" [ref=e6]: value``)."""
    if not values:
        return tree
    lines = tree.split("\n")
    for number, line in enumerate(lines):
        if "textbox" not in line:
            continue
        for value in values:
            suffix = f": {value}"
            if line.endswith(suffix):
                lines[number] = line[: -len(suffix)] + f": {HIDDEN}"
                break
    return "\n".join(lines)


_VIEWPORT_JS: Final = "() => [window.innerWidth, window.innerHeight]"
_SOF_MARKERS: Final = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
"""The JPEG start-of-frame markers: they have the size of the image."""


def _opens_pages(act: Act) -> bool:
    return act.action in {"click", "press"} or (act.action == "type" and act.submit)


def _ignore_result(task: asyncio.Future[object]) -> None:
    """Reads the result of a background action, so asyncio does not log it."""
    if not task.cancelled():
        task.exception()


def _http_url(url: str) -> str:
    """The http form of a WebSocket URL, for the URL guard."""
    scheme, sep, rest = url.partition(":")
    mapped = {"ws": "http", "wss": "https"}.get(scheme.lower())
    return f"{mapped}{sep}{rest}" if mapped else url


def jpeg_size(data: bytes) -> tuple[int, int]:
    """The width and height of a JPEG image, or ``(0, 0)`` when the data is not a JPEG."""
    if data[:2] != b"\xff\xd8":
        return (0, 0)
    at = 2
    while at + 9 < len(data):
        if data[at] != 0xFF:
            return (0, 0)
        marker = data[at + 1]
        if marker == 0xFF:  # a fill byte
            at += 1
            continue
        length = int.from_bytes(data[at + 2 : at + 4], "big")
        if marker in _SOF_MARKERS:
            height = int.from_bytes(data[at + 5 : at + 7], "big")
            width = int.from_bytes(data[at + 7 : at + 9], "big")
            return (width, height)
        at += 2 + length
    return (0, 0)


def _short(error: Exception) -> str:
    """The first line of a Playwright error, without the call log."""
    text = str(getattr(error, "message", None) or error).strip().splitlines()
    first = text[0] if text else type(error).__name__
    return first[:MAX_ERROR_CHARS]


class PlaywrightSession:
    """One browser of one run. The service calls it one call at a time."""

    vision: ClassVar[bool] = True
    """The session can act at a point (vision mode, M5.2)."""

    def __init__(
        self,
        browser: Browser,
        context: BrowserContext,
        guard: NavigationGuard,
        *,
        target: TargetName,
        on_close: Callable[[], Awaitable[None]] | None = None,
        live: LiveUrls | None = None,
    ) -> None:
        self._live = live
        """Gives the live view URLs of the remote session (Browserbase), or ``None``."""
        self._browser = browser
        self._context = context
        self._guard = guard
        self._target: TargetName = target
        self._on_close = on_close
        self._active: Page | None = None
        self._dialogs: dict[Page, PlaywrightDialog] = {}
        self._dialog_opened: asyncio.Event | None = None
        self._pending: asyncio.Future[object] | None = None
        """An action that waits for the answer to a dialog."""
        self._notes: list[str] = []
        self._blocked = False
        """A navigation was blocked since the last state."""
        self._blocked_urls: list[str] = []
        """The blocked URLs since the last state, each with the reason. Text of the page."""
        self._cdp: CDPSession | None = None
        self._checks: set[asyncio.Task[None]] = set()
        self._closed = False

    @classmethod
    async def attach(
        cls,
        browser: Browser,
        guard: NavigationGuard,
        *,
        target: TargetName,
        on_close: Callable[[], Awaitable[None]] | None = None,
        notes: tuple[str, ...] = (),
        live: LiveUrls | None = None,
    ) -> PlaywrightSession:
        """Uses the first context of the browser (the persistent context of a Browserbase
        session), or a new one. ``notes`` go to the agent with the first state. ``live`` gives
        the live view URLs of the remote session (M8.3)."""
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        session = cls(browser, context, guard, target=target, on_close=on_close, live=live)
        session._notes.extend(notes)
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        cdp = await browser.new_browser_cdp_session()
        session._cdp = cdp
        cdp.on("Fetch.requestPaused", session._on_paused)
        await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
        await context.route_web_socket("**/*", session._route_web_socket)
        context.on("page", session._on_page)
        for page in context.pages:
            session._watch(page)
        session._active = context.pages[-1] if context.pages else await context.new_page()
        return session

    @property
    def target(self) -> TargetName:
        return self._target

    async def live_view(self, ttl_s: int) -> LiveView | None:
        """The live view of the active tab (M8.3), or ``None`` without a live source or a tab.
        The remote session lists each tab with its URL and its live view URL. The tab with
        the URL of the active page wins, else the last tab."""
        page = self._active
        if self._live is None or page is None or self._closed:
            return None
        pages = await self._live(ttl_s)
        if not pages:
            return None
        url = page.url
        chosen = next((p for p in reversed(pages) if p.get("url") == url), pages[-1])
        view = str(chosen.get("debuggerFullscreenUrl") or "")
        if not view.startswith("https://"):
            return None
        title = str(chosen.get("title") or "")
        with contextlib.suppress(Exception):
            async with asyncio.timeout(LIVE_TITLE_TIMEOUT_S):
                title = await page.title()
        joiner = "&" if "?" in view else "?"
        return LiveView(
            view_url=f"{view}{joiner}navbar=false",
            page_url=url,
            title=title[:MAX_LIVE_TITLE_CHARS],
            tabs=len(pages),
            page_id=str(chosen.get("id") or ""),
        )

    # -- events ------------------------------------------------------------------------------

    def _watch(self, page: Page) -> None:
        page.on("dialog", lambda dialog: self._on_dialog(page, dialog))
        page.on("close", lambda _: self._on_page_close(page))

    def _on_page(self, page: Page) -> None:
        self._watch(page)
        if not self._closed:
            self._active = page
            self._notes.append("a new tab opened and is now the active tab")

    def _on_dialog(self, page: Page, dialog: PlaywrightDialog) -> None:
        self._dialogs[page] = dialog
        self._notes.append(f"a {dialog.type} dialog opened; answer it with action=dialog")
        if self._dialog_opened is not None:
            self._dialog_opened.set()

    def _on_page_close(self, page: Page) -> None:
        self._dialogs.pop(page, None)
        if self._active is page:
            pages = self._context.pages
            self._active = pages[-1] if pages else None

    def _on_paused(self, event: dict[str, Any]) -> None:
        """Chrome paused a request (or a redirect hop) of a target. sama must answer each one,
        so the check runs as a task. A closed session takes no new checks."""
        if self._closed:
            return
        task = asyncio.get_running_loop().create_task(self._check_paused(event))
        self._checks.add(task)
        task.add_done_callback(self._checks.discard)

    async def _check_paused(self, event: dict[str, Any]) -> None:
        cdp = self._cdp
        request_id = event.get("requestId")
        if cdp is None or not isinstance(request_id, str):
            return
        request = event.get("request") or {}
        url = str(request.get("url", ""))
        navigation = event.get("resourceType") == DOCUMENT
        try:
            if navigation:
                await self._guard.check(url)
            else:
                if isinstance(self._guard, FreshGuard):
                    await self._guard.fresh()  # the request waits for an old policy to load
                problem = self._guard.quick_problem(url)
                if problem is not None:
                    raise BlockedUrlError(problem)
        except Exception as error:  # the check fails closed
            reason = str(error) if isinstance(error, BlockedUrlError) else "the check failed"
            if navigation:
                self._blocked = True
                self._block(url, reason)
            with contextlib.suppress(Exception):
                await cdp.send(
                    "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                )
            return
        with contextlib.suppress(Exception):
            await cdp.send("Fetch.continueRequest", {"requestId": request_id})

    async def _route_web_socket(self, ws: WebSocketRoute) -> None:
        try:
            await self._guard.check(_http_url(ws.url))
        except Exception as error:  # the check fails closed
            reason = str(error) if isinstance(error, BlockedUrlError) else "the check failed"
            self._block(ws.url, reason)
            with contextlib.suppress(PlaywrightError):
                await ws.close(code=WS_CLOSE_POLICY, reason="blocked by the URL policy")
            return
        ws.connect_to_server()

    def _block(self, url: str, reason: str) -> None:
        if len(self._blocked_urls) < MAX_BLOCKED:
            self._blocked_urls.append(f"{url[:500]} ({reason[:MAX_ERROR_CHARS]})")

    # -- helpers -----------------------------------------------------------------------------

    async def _page(self) -> Page:
        if self._closed:
            raise BrowserError("the browser session ended; call browser_open again")
        if self._active is None or self._active.is_closed():
            pages = self._context.pages
            self._active = pages[-1] if pages else await self._context.new_page()
        return self._active

    def _dialog_of(self, page: Page) -> PlaywrightDialog | None:
        return self._dialogs.get(page)

    async def _state(self, page: Page) -> PageState:
        """The state of the active tab after a change. A final URL that the policy refuses
        (for example after a redirect that the server sent) is closed."""
        if page.url.startswith(ERROR_PAGE):
            await self._leave_error_page(page)
        self._blocked = False
        url = page.url
        if url.split(":", 1)[0].lower() in PAGE_SCHEMES:
            try:
                await self._guard.check(url)
            except BlockedUrlError as error:
                self._block(url, str(error))
                self._notes.append(
                    "the URL policy blocked the address of the tab; the tab is blank"
                )
                with contextlib.suppress(PlaywrightError):
                    await page.goto(BLANK)
        title = ""
        if self._dialog_of(page) is None:
            with contextlib.suppress(PlaywrightError, TimeoutError):
                title = await asyncio.wait_for(page.title(), timeout=ACTION_TIMEOUT_MS / 1000)
        pages = self._context.pages
        index = pages.index(page) if page in pages else 0
        if self._blocked_urls and BLOCKED_NOTE not in self._notes:
            self._notes.append(BLOCKED_NOTE)
        notes, blocked = tuple(self._notes), tuple(self._blocked_urls)
        self._notes.clear()
        self._blocked_urls.clear()
        return PageState(
            url=page.url, title=title, tab=index, tabs=len(pages), notes=notes, blocked=blocked
        )

    async def _leave_error_page(self, page: Page) -> None:
        """After a blocked navigation, goes back to the page before it. Another error page (a
        network error) stays, with a note."""
        if not self._blocked:
            self._notes.append("the page did not load (a network error or a refused connection)")
            return
        with contextlib.suppress(PlaywrightError):
            await page.go_back(wait_until="domcontentloaded", timeout=SETTLE_TIMEOUT_MS)
        if page.url.startswith(ERROR_PAGE):
            with contextlib.suppress(PlaywrightError):
                await page.goto(BLANK)

    async def _locator(self, page: Page, ref: str | None) -> Locator:
        """The element of a ref from the latest snapshot. A ref that the page does not have
        fails at once, not after the action timeout."""
        if not ref:
            raise BrowserError("this action needs the ref of an element from browser_snapshot")
        ref = ref.removeprefix("ref=").strip()
        locator = page.get_by_ref(ref)
        try:
            count = await locator.count()
        except PlaywrightError:
            count = 0
        if count == 0:
            raise BrowserError(f"the ref {ref} is not in the latest snapshot; take a new snapshot")
        return locator

    async def _until_dialog(self, work: Awaitable[object]) -> None:
        """Runs ``work``. If a dialog opens first, it returns, and ``work`` ends after the
        agent answers the dialog: the page waits for the answer."""
        task = asyncio.ensure_future(work)
        opened = asyncio.Event()
        self._dialog_opened = opened
        waiter = asyncio.ensure_future(opened.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._dialog_opened = None
            waiter.cancel()
        if task.done():
            task.result()
            return
        task.add_done_callback(_ignore_result)
        self._pending = task

    async def _finish_pending(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            with contextlib.suppress(Exception, TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), timeout=SETTLE_TIMEOUT_MS / 1000)

    async def _settle(self, page: Page) -> None:
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("domcontentloaded", timeout=SETTLE_TIMEOUT_MS)

    async def _viewport(self, page: Page) -> tuple[int, int]:
        """The width and height of the viewport in CSS pixels."""
        size = page.viewport_size
        if size is not None:
            return (size["width"], size["height"])
        # A browser over CDP (Browserbase) can have no viewport emulation: ask the page.
        width, height = await page.evaluate(_VIEWPORT_JS)
        return (int(width), int(height))

    async def _in_viewport(self, page: Page, point: tuple[float, float]) -> tuple[float, float]:
        width, height = await self._viewport(page)
        x, y = point
        if not (0 <= x < width and 0 <= y < height):
            raise BrowserError(
                f"the point ({x:g}, {y:g}) is outside the viewport ({width}x{height}); take a "
                "new screenshot (full_page=false) and use a point in it"
            )
        return point

    def _no_dialog(self, page: Page) -> None:
        dialog = self._dialog_of(page)
        if dialog is not None:
            raise BrowserError(
                f"a {dialog.type} dialog is open; take a snapshot to read it, then accept or "
                'dismiss it with browser_act action="dialog"'
            )

    # -- the session -------------------------------------------------------------------------

    async def navigate(self, url: str) -> PageState:
        page = await self._page()
        self._no_dialog(page)
        try:
            await self._until_dialog(page.goto(url, wait_until="domcontentloaded"))
        except PlaywrightTimeoutError:
            self._notes.append("the page did not finish loading in 30 seconds")
        except PlaywrightError as error:
            if self._blocked_urls:
                detail = "\n".join(self._blocked_urls)
                self._blocked_urls.clear()
                self._notes.clear()
                # Chrome shows its error page for the blocked request. Wait for it, so it does
                # not stop the next navigation.
                with contextlib.suppress(PlaywrightError):
                    await page.wait_for_url(f"{ERROR_PAGE}**", timeout=SETTLE_TIMEOUT_MS)
                if page.url.startswith(ERROR_PAGE):
                    await self._leave_error_page(page)
                self._blocked = False
                self._notes.clear()
                raise BrowserError(REDIRECT_BLOCKED, detail=detail) from None
            raise BrowserError("the page did not open", detail=_short(error)) from None
        return await self._state(page)

    async def snapshot(self) -> Snapshot:
        page = await self._page()
        dialog = self._dialog_of(page)
        if dialog is not None:
            state = await self._state(page)
            return Snapshot(page=state, tree="", dialog=Dialog(dialog.type, dialog.message))
        try:
            tree = await page.aria_snapshot(mode="ai")
        except PlaywrightError as error:
            raise BrowserError("the snapshot failed", detail=_short(error)) from None
        tree = hide_values(tree, await self._password_values(page))
        return Snapshot(page=await self._state(page), tree=tree)

    async def _password_values(self, page: Page) -> list[str]:
        """The values of the password fields of all frames of the page, longest first, so a
        value that ends another value does not hide only a part."""
        values: set[str] = set()
        for frame in page.frames:
            with contextlib.suppress(PlaywrightError):
                found = await frame.evaluate(_PASSWORD_VALUES_JS)
                values.update(str(v) for v in found if v)
        return sorted(values, key=len, reverse=True)

    async def act(self, act: Act) -> PageState:
        page = await self._page()
        if act.action == "dialog":
            return await self._answer_dialog(page, act)
        self._no_dialog(page)
        try:
            await self._until_dialog(self._do(page, act))
        except PlaywrightTimeoutError:
            raise BrowserError(
                f"the {act.action} did not finish in time; the element can be hidden, covered, "
                "or disabled. Take a new snapshot."
            ) from None
        except PlaywrightError as error:
            raise BrowserError(f"the {act.action} failed", detail=_short(error)) from None
        if self._dialog_of(page) is None:
            if _opens_pages(act):
                await asyncio.sleep(SETTLE_DELAY_S)
            await self._settle(await self._page())
        return await self._state(await self._page())

    async def _do(self, page: Page, act: Act) -> None:
        if act.point is not None:
            await self._do_at_point(page, act, act.point)
            return
        if act.action in ACTIONS_WITH_REF:
            locator = await self._locator(page, act.ref)
            if act.action == "click":
                await locator.click(click_count=act.clicks)
            elif act.action == "type":
                await locator.fill(act.text or "")
                if act.submit:
                    await locator.press("Enter")
            elif act.action == "select":
                if not act.values:
                    raise BrowserError("select needs values")
                await locator.select_option(list(act.values))
            elif act.action == "hover":
                await locator.hover()
            else:
                await locator.drag_to(await self._locator(page, act.target_ref))
            return
        if act.action == "scroll":
            if act.ref:
                await (await self._locator(page, act.ref)).scroll_into_view_if_needed()
                return
            dx, dy = {
                "down": (0, act.amount),
                "up": (0, -act.amount),
                "right": (act.amount, 0),
                "left": (-act.amount, 0),
            }[act.direction]
            await page.mouse.wheel(dx, dy)
        elif act.action == "press":
            if not act.text:
                raise BrowserError("press needs text: the key, for example Enter")
            if act.ref:
                await (await self._locator(page, act.ref)).press(act.text)
            else:
                await page.keyboard.press(act.text)
        elif act.action == "wait":
            await asyncio.sleep(max(0.0, min(act.seconds, MAX_WAIT_S)))
        elif act.action == "back":
            await page.go_back(wait_until="domcontentloaded")
        elif act.action == "forward":
            await page.go_forward(wait_until="domcontentloaded")
        elif act.action == "reload":
            await page.reload(wait_until="domcontentloaded")
        else:  # pragma: no cover - the tool schema allows only the known actions
            raise BrowserError(f"unknown action {act.action}")

    async def _do_at_point(self, page: Page, act: Act, point: tuple[float, float]) -> None:
        """An action at a point of the viewport (vision mode). The service checked that the
        action can use a point."""
        x, y = await self._in_viewport(page, point)
        mouse = page.mouse
        if act.action == "click":
            await mouse.click(x, y, click_count=act.clicks)
        elif act.action == "type":
            await mouse.click(x, y)
            await page.keyboard.type(act.text or "")
            if act.submit:
                await page.keyboard.press("Enter")
        elif act.action == "hover":
            await mouse.move(x, y)
        elif act.action == "drag":
            if act.to_point is None:
                raise BrowserError("drag at a point needs to_x and to_y: the point to drop on")
            to_x, to_y = await self._in_viewport(page, act.to_point)
            await mouse.move(x, y)
            await mouse.down()
            try:
                await mouse.move(to_x, to_y, steps=DRAG_STEPS)
            finally:
                await mouse.up()
        elif act.action == "scroll":
            dx, dy = {
                "down": (0, act.amount),
                "up": (0, -act.amount),
                "right": (act.amount, 0),
                "left": (-act.amount, 0),
            }[act.direction]
            await mouse.move(x, y)
            await mouse.wheel(dx, dy)
        else:
            raise BrowserError(f"{act.action} cannot use a point; use a ref")

    async def _answer_dialog(self, page: Page, act: Act) -> PageState:
        dialog = self._dialogs.pop(page, None)
        if dialog is None:
            raise BrowserError("no dialog is open")
        try:
            if act.accept:
                await dialog.accept(act.prompt_text or "")
            else:
                await dialog.dismiss()
        except PlaywrightError as error:
            raise BrowserError("the dialog did not close", detail=_short(error)) from None
        await self._finish_pending()
        await self._settle(page)
        return await self._state(await self._page())

    async def fill_login(self, request: FillRequest) -> Filled:
        """Fills a saved login. Each field must be in a document that ``site_allows`` accepts.

        The fields are pinned element handles, not locators: a locator finds its element again
        at each call, so a frame that navigates between the check and the fill could give it an
        element of another site. A handle of a document that is gone fails. The site check uses
        the frame of the element as the browser reports it, not page JavaScript, which a page
        can change. An error has fixed text only: a Playwright error can hold a part of the
        value."""
        page = await self._page()
        self._no_dialog(page)
        try:
            password = await self._password_field(page, request)
            username = await self._username_field(page, request, password)
        finally:
            for frame in page.frames:
                with contextlib.suppress(PlaywrightError):
                    await frame.evaluate(_UNMARK_JS)
        if password is None and username is None:
            raise BrowserError(
                "no login field of the site of this login is on the page; open the sign-in page"
                " of the site, or give username_ref and password_ref from a snapshot"
            )
        fields: list[str] = []
        last: ElementHandle | None = None
        try:
            for name, handle, value in (
                ("username", username, request.username),
                ("password", password, request.password),
            ):
                if handle is None:
                    continue
                # Check again just before the fill: the document must still be of the site.
                if not await _handle_allowed(handle, request):
                    raise BrowserError(_OTHER_SITE)
                # A write to the pinned element itself, not keyboard input to the focused
                # element: a page cannot move the focus to its own field during the fill.
                if not await handle.evaluate(_SET_VALUE_JS, value.get_secret_value()):
                    raise BrowserError("the field is disabled or read-only; take a new snapshot")
                fields.append(name)
                last = handle
            if request.submit and last is not None:
                if not await _handle_allowed(last, request):
                    raise BrowserError(_OTHER_SITE)
                await self._until_dialog(last.press("Enter", timeout=ACTION_TIMEOUT_MS))
        except PlaywrightTimeoutError:
            raise BrowserError(
                "the fill did not finish in time; the field can be hidden, covered, or disabled."
                " Take a new snapshot."
            ) from None
        except PlaywrightError:
            raise BrowserError("the fill failed; take a new snapshot and try again") from None
        if request.submit and self._dialog_of(page) is None:
            await asyncio.sleep(SETTLE_DELAY_S)
            await self._settle(await self._page())
        return Filled(page=await self._state(await self._page()), fields=tuple(fields))

    async def _pin(self, locator: Locator, request: FillRequest) -> ElementHandle | None:
        """The element of ``locator`` as a pinned handle, if its frame is of the site."""
        try:
            handle = await locator.element_handle(timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError:
            return None
        if handle is None or not await _handle_allowed(handle, request):
            return None
        return handle

    async def _password_field(self, page: Page, request: FillRequest) -> ElementHandle | None:
        if request.password_ref:
            locator = await self._locator(page, request.password_ref)
            if not await locator.evaluate(_IS_PASSWORD_JS):
                raise BrowserError("password_ref must be a password field")
            handle = await self._pin(locator, request)
            if handle is None:
                raise BrowserError(_OTHER_SITE)
            return handle
        for frame in page.frames:
            if not request.site_allows(frame.url):
                continue
            locator = frame.locator("input[type=password]:visible").first
            with contextlib.suppress(PlaywrightError):
                if await locator.count():
                    handle = await self._pin(locator, request)
                    if handle is not None:
                        return handle
        return None

    async def _username_field(
        self, page: Page, request: FillRequest, password: ElementHandle | None
    ) -> ElementHandle | None:
        if request.username_ref:
            locator = await self._locator(page, request.username_ref)
            if not await locator.evaluate(_IS_TEXT_JS):
                raise BrowserError("username_ref must be a text field")
            handle = await self._pin(locator, request)
            if handle is None:
                raise BrowserError(_OTHER_SITE)
            return handle
        if password is not None:
            # The user name field is in the form (or the document) of the password field.
            try:
                frame = await password.owner_frame()
                if frame is None or not await password.evaluate(_MARK_USERNAME_JS):
                    return None
            except PlaywrightError:
                return None
            return await self._pin(frame.locator(f"[{FILL_MARK}=username]").first, request)
        # No password field: a first step that asks only for the user name.
        for frame in page.frames:
            if not request.site_allows(frame.url):
                continue
            with contextlib.suppress(PlaywrightError):
                if await frame.evaluate(_MARK_USERNAME_JS, None):
                    handle = await self._pin(
                        frame.locator(f"[{FILL_MARK}=username]").first, request
                    )
                    if handle is not None:
                        return handle
        return None

    async def screenshot(self, *, full_page: bool = False) -> Screenshot:
        page = await self._page()
        self._no_dialog(page)
        try:
            if full_page:
                height = int(await page.evaluate(_HEIGHT_JS))
                width, _ = await self._viewport(page)
                data = await page.screenshot(
                    type="jpeg",
                    quality=SCREENSHOT_QUALITY,
                    scale="css",
                    full_page=True,
                    clip={
                        "x": 0,
                        "y": 0,
                        "width": width,
                        "height": max(1, min(height, MAX_SCREENSHOT_HEIGHT)),
                    },
                )
            else:
                # The scale "css" makes one pixel of the image one CSS pixel of the viewport,
                # so a point of the image is a point for an action (vision mode).
                data = await page.screenshot(type="jpeg", quality=SCREENSHOT_QUALITY, scale="css")
        except PlaywrightError as error:
            raise BrowserError("the screenshot failed", detail=_short(error)) from None
        width, height = jpeg_size(data)
        return Screenshot(
            page=await self._state(page),
            data=data,
            mime_type="image/jpeg",
            width=width,
            height=height,
            full_page=full_page,
        )

    async def extract(self, request: ExtractRequest) -> Extracted:
        page = await self._page()
        self._no_dialog(page)
        try:
            content = await self._extract(page, request)
        except PlaywrightError as error:
            raise BrowserError("the extract failed", detail=_short(error)) from None
        return Extracted(page=await self._state(page), kind=request.kind, content=content)

    async def _extract(self, page: Page, request: ExtractRequest) -> str:
        if request.kind == "fields":
            if not request.fields:
                raise BrowserError("fields needs a ref for each name")
            values: dict[str, Any] = {}
            for name, ref in request.fields.items():
                values[name] = await (await self._locator(page, ref)).evaluate(_FIELD_JS)
            return json.dumps(values, ensure_ascii=False, indent=1)
        root = await self._locator(page, request.ref) if request.ref else page.locator("body")
        if request.kind == "text":
            text = await root.inner_text()
            return text[:MAX_TEXT_CHARS]
        if request.kind == "links":
            links = await root.evaluate(_LINKS_JS, MAX_LINKS)
            return json.dumps(links, ensure_ascii=False, indent=1)
        tables = await root.evaluate(_TABLES_JS, [MAX_TABLES, MAX_ROWS])
        return json.dumps(tables, ensure_ascii=False, indent=1)

    async def tabs(
        self, action: TabAction, *, index: int | None = None, url: str | None = None
    ) -> list[Tab]:
        if action == "new":
            if len(self._context.pages) >= MAX_TABS:
                raise BrowserError(f"a session can have {MAX_TABS} tabs; close a tab first")
            page = await self._context.new_page()
            self._notes.clear()  # the new tab is the requested change, not news
            self._active = page
            if url:
                await self.navigate(url)
        elif action in {"select", "close"}:
            pages = self._context.pages
            chosen = await self._page()
            if index is not None:
                if not 0 <= index < len(pages):
                    raise BrowserError(
                        f"there is no tab {index}; the tabs are 0 to {len(pages) - 1}"
                    )
                chosen = pages[index]
            if action == "select":
                self._active = chosen
                with contextlib.suppress(PlaywrightError):
                    await chosen.bring_to_front()
            else:
                with contextlib.suppress(PlaywrightError):
                    await chosen.close()
                if not self._context.pages:
                    self._active = await self._context.new_page()
        return await self._list()

    async def _list(self) -> list[Tab]:
        active = await self._page()
        tabs: list[Tab] = []
        for number, page in enumerate(self._context.pages):
            title = ""
            if self._dialog_of(page) is None:
                with contextlib.suppress(PlaywrightError, TimeoutError):
                    title = await asyncio.wait_for(page.title(), timeout=ACTION_TIMEOUT_MS / 1000)
            tabs.append(Tab(index=number, url=page.url, title=title, active=page is active))
        return tabs

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pending is not None:
            self._pending.cancel()
        if self._cdp is not None:
            with contextlib.suppress(Exception):
                self._cdp.remove_listener("Fetch.requestPaused", self._on_paused)
        checks = list(self._checks)
        for task in checks:
            task.cancel()
        try:
            await asyncio.gather(*checks, return_exceptions=True)
            async with asyncio.timeout(CLOSE_TIMEOUT_S):
                await self._browser.close()
        except Exception:
            log.warning("the browser did not close cleanly", exc_info=True)
        finally:
            if self._on_close is not None:
                # The release frees the context of the session, so it runs also when the
                # close is cancelled.
                try:
                    await asyncio.shield(self._on_close())
                except Exception:
                    log.warning("the browser release failed", exc_info=True)
