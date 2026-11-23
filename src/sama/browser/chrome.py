"""The ``my_chrome`` browser target (docs/architecture.md, Section 14.2.2, roadmap M5.5).

The browser tools with ``target="my_chrome"`` act in the Chrome of the user, through the sama
extension. The extension holds a WebSocket to the API (Section 14.2.1). The API keeps the open
connections of each user in the ``ChromeHub``. A ``ChromeSession`` sends each browser call as a
``tool_call`` frame on the newest connection of the user of the run, and waits for the
``tool_result`` frame.

The extension does the page work with ``chrome.debugger`` (CDP), draws the agent cursor, asks the
user for consent for each site, asks for a click before a sensitive action, and stops or pauses
when the user says so. This module checks the URL against the blocked list before it sends a
navigation, keeps the sensitive refs of the latest snapshot for the tool gate (Section 16.1),
and turns the answers of the extension into the data classes of ``sama.browser.target``.

The answers of the extension can hold text of the page (titles, URLs, the tree, and error
details). The service puts that text in an ``untrusted_content`` block. The notes and the error
messages that the agent reads outside the block are fixed text of this module: the extension
sends codes, and this module maps each code to its text.
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.browser.target import (
    MY_CHROME,
    Act,
    BrowserError,
    BrowserSession,
    Dialog,
    Extracted,
    ExtractRequest,
    NavigationGuard,
    PageState,
    Screenshot,
    Snapshot,
    Tab,
    TabAction,
    TargetName,
)
from sama.db.models import User
from sama.gateway.chrome import ChromeHub, ChromeLink, ChromeOfflineError
from sama.tools.approved import human_approved

log = logging.getLogger("sama.browser.chrome")

CALL_TIMEOUT_S: Final = 150.0
"""The longest wait for one answer of the extension. An answer can wait for the user: the
consent for a site, the click for a sensitive action, or the end of a takeover (at most
``USER_WAIT_S`` in the extension)."""
USER_WAIT_S: Final = 120.0
"""How long the extension waits for the user before it fails the call (extension/src/lib/
actions.ts, USER_WAIT_MS)."""
RELEASE_TIMEOUT_S: Final = 5.0
ANSWER_MARGIN_S: Final = 5.0
"""The extension gets a deadline this much before the API stops waiting for its answer."""
MAX_SCREENSHOT_BYTES: Final = 4_000_000
MAX_TREE_CHARS: Final = 400_000
MAX_EXTRACT_CHARS: Final = 200_000

Op = Literal["navigate", "snapshot", "act", "screenshot", "extract", "tabs", "release"]

# -- the blocked list ---------------------------------------------------------------------------

BLOCKED_HOSTS: Final = (
    # Google account and payment pages, and the Chrome Web Store.
    "accounts.google.com",
    "myaccount.google.com",
    "passwords.google.com",
    "pay.google.com",
    "payments.google.com",
    "chromewebstore.google.com",
    "chrome.google.com",
    # Password managers.
    "1password.com",
    "1password.eu",
    "1password.ca",
    "lastpass.com",
    "bitwarden.com",
    "bitwarden.eu",
    "dashlane.com",
    "keepersecurity.com",
    "keepersecurity.eu",
    "nordpass.com",
    "roboform.com",
    "proton.me",
    # Payments, wallets, and exchanges.
    "paypal.com",
    "venmo.com",
    "wise.com",
    "revolut.com",
    "paytm.com",
    "phonepe.com",
    "coinbase.com",
    "binance.com",
    "kraken.com",
    # Banks with a host name that does not have the word "bank".
    "chase.com",
    "wellsfargo.com",
    "citi.com",
    "citibank.com",
    "hsbc.com",
    "hsbc.co.uk",
    "hsbc.co.in",
    "barclays.co.uk",
    "barclays.com",
    "lloydsbank.com",
    "natwest.com",
    "santander.co.uk",
    "capitalone.com",
    "usbank.com",
    "schwab.com",
    "fidelity.com",
    "vanguard.com",
    "americanexpress.com",
    "discover.com",
    "onlinesbi.sbi",
    "sbi.co.in",
    "kotak.com",
    "icici.com",
)
"""Hosts where the agent never acts in the user's Chrome, with their subdomains. Keep this list
the same as ``extension/src/lib/blocklist.ts`` (a test checks it)."""

BLOCKED_HOST_WORD: Final = "bank"
"""A host with a label that has this word (``hdfcbank.com``, ``netbanking.example.in``) is a
bank site and is blocked too."""


def blocked_site(url: str) -> bool:
    """True when the agent must not act on the page of ``url`` in the user's Chrome."""
    try:
        host = (urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    if any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_HOSTS):
        return True
    return any(BLOCKED_HOST_WORD in label for label in host.split("."))


# -- sensitive actions --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sensitivity:
    """The sensitive elements of the latest snapshot, as the extension found them."""

    refs: Mapping[str, str] = field(default_factory=dict)
    """A reason code for each ref that is sensitive to click or to type into: a submit button,
    a button that buys, sends, or deletes, a password field, or a payment field."""
    submits: frozenset[str] = frozenset()
    """The text fields of a form, where Enter submits the form."""


SUBMIT_REASON: Final = "submit"


def sensitive_reason(sensitivity: Sensitivity, arguments: Mapping[str, Any]) -> str | None:
    """The reason code why a ``browser_act`` call is a sensitive action, from the latest
    snapshot, or ``None``.

    The tool gate uses this to ask the user before the call (Section 16.1). The extension checks
    the live page again before it acts, so a page that changed after the snapshot cannot skip
    the click of the user."""
    action = arguments.get("action")
    ref = arguments.get("ref")
    ref = ref.removeprefix("ref=").strip() if isinstance(ref, str) else None
    if action in {"click", "type", "select"} and ref and ref in sensitivity.refs:
        return sensitivity.refs[ref]
    if action == "type" and arguments.get("submit") is True and ref in sensitivity.submits:
        return SUBMIT_REASON
    key = arguments.get("text")
    name = key.split("+")[-1] if isinstance(key, str) and key != " " else key
    if action == "press" and ref:
        # Enter or Space on a sensitive button activates it; a key with text types into a
        # sensitive field. Other keys (Tab, arrows) are not sensitive.
        activates = name in {"Enter", "Space", " "} or (isinstance(name, str) and len(name) == 1)
        if activates and ref in sensitivity.refs:
            return sensitivity.refs[ref]
        if name == "Enter" and ref in sensitivity.submits:
            return SUBMIT_REASON
    return None


# -- the answers of the extension ---------------------------------------------------------------

NOTES: Final[Mapping[str, str]] = {
    "new_tab": "the page opened a new tab; it is the active tab now",
    "tab_closed": "the tab closed; the active tab changed",
    "dialog_open": "a JavaScript dialog is open; take a snapshot to read it",
    "navigated": "the page changed after the action",
    "approved": "the user approved this sensitive action",
    "consent": "the user allowed sama to act on this site",
}
"""The notes that the extension can add to a page state, by code. Unknown codes are dropped."""

ERRORS: Final[Mapping[str, str]] = {
    "blocked": (
        "this site is on the blocked list of the sama extension (banks, payments, password "
        "managers, Google account pages, and the Chrome Web Store); sama does not act on it in "
        "the user's Chrome"
    ),
    "never": "the user does not allow sama to act on this site in their Chrome",
    "consent_denied": "the user did not allow sama to act on this site",
    "consent_timeout": "the user did not answer the request to act on this site in time",
    "sensitive_denied": "the user did not allow this sensitive action",
    "sensitive_timeout": "the user did not approve this sensitive action in time",
    "taken_over": (
        "the user took over the tab, so sama is paused there. Do not act in the tab now: tell "
        "the user what you were doing and wait for them to give the tab back"
    ),
    "stopped": "the user stopped sama in their Chrome. End the work and report what you did",
    "no_tab": "the tab of this session is closed; open a page with browser_open",
    "stale_ref": "the ref is not in the latest snapshot; take a new snapshot",
    "not_web": "sama acts only on http and https pages in the user's Chrome",
    "debugger": (
        "Chrome did not let sama control the tab; another debugger (for example DevTools) can "
        "be open on it"
    ),
    "busy": "the extension acts for another run in this tab; try again when that run ends",
    "timeout": "the action did not finish in time in the user's Chrome",
    "invalid": "the extension refused the call",
    "failed": "the action failed in the user's Chrome",
}
"""The fixed text of each error code of the extension. The detail of the extension (text of
the page or of Chrome) goes in the untrusted block of ``BrowserError``."""

OFFLINE: Final = (
    "the sama Chrome extension of this user is offline; tell the user in one line to open "
    "Chrome with the sama extension and sign in (the web app has the download and the steps in "
    "Settings, Chrome extension), or to set the browser of this session to Cloud; then use the "
    "cloud target if the task allows it"
)
NO_USER: Final = "my_chrome needs a run of a user; this run has no user"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Page(_Model):
    url: str = Field(default="", max_length=8_192)
    title: str = Field(default="", max_length=2_000)
    tab: int = Field(default=0, ge=0, le=1_000)
    tabs: int = Field(default=1, ge=0, le=1_000)
    notes: list[str] = Field(default_factory=list, max_length=20)

    def state(self) -> PageState:
        notes = tuple(NOTES[code] for code in self.notes if code in NOTES)
        return PageState(url=self.url, title=self.title, tab=self.tab, tabs=self.tabs, notes=notes)


class _Dialog(_Model):
    kind: str = Field(max_length=20)
    message: str = Field(default="", max_length=10_000)


class _Snapshot(_Model):
    page: _Page
    tree: str = Field(default="", max_length=MAX_TREE_CHARS)
    dialog: _Dialog | None = None
    sensitive: dict[str, str] = Field(default_factory=dict, max_length=5_000)
    submits: list[str] = Field(default_factory=list, max_length=5_000)


class _Screenshot(_Model):
    page: _Page
    data: str
    mime_type: Literal["image/jpeg", "image/png"] = "image/jpeg"


class _Extracted(_Model):
    page: _Page
    kind: Literal["text", "links", "tables", "fields"]
    content: str = Field(default="", max_length=MAX_EXTRACT_CHARS)


class _Tab(_Model):
    index: int = Field(ge=0, le=1_000)
    url: str = Field(default="", max_length=8_192)
    title: str = Field(default="", max_length=2_000)
    active: bool = False


class _Tabs(_Model):
    tabs: list[_Tab] = Field(default_factory=list, max_length=100)


class _Error(_Model):
    code: str = Field(default="failed", max_length=40)
    message: str = Field(default="", max_length=2_000)


def _parse[M: BaseModel](model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError:
        raise BrowserError("the extension sent an answer that is not valid") from None


REASONS: Final[Mapping[str, str]] = {
    "submit": "submit a form",
    "pay": "buy or pay",
    "send": "send or publish",
    "delete": "delete or remove",
    "confirm": "confirm",
    "password": "type into a password field",
    "payment": "type into a payment field",
}
"""The fixed text of each reason code of the extension (extension/src/lib/sensitive.ts)."""


def _call_key(arguments: Mapping[str, Any]) -> tuple[Any, ...]:
    """The fields of a ``browser_act`` call that name what it does."""
    ref = arguments.get("ref")
    ref = ref.removeprefix("ref=").strip() if isinstance(ref, str) else None
    return (arguments.get("action"), ref, arguments.get("text"), arguments.get("submit") is True)


def _reason_text(code: str) -> str:
    """The text of a reason code for the approval card. The code comes from the extension, so
    an unknown code gets a fixed text, never the code itself."""
    return REASONS.get(code, "a sensitive action")


# -- the target ---------------------------------------------------------------------------------

AuthUserLookup = Callable[[str, str], Awaitable[uuid.UUID | None]]
"""The Supabase Auth user of a sama user: ``(org_id, user_id)`` gives the auth user id."""


class AuthUsers:
    """``AuthUserLookup`` from the ``users`` table. The control plane reads it with the service
    role, scoped by the org of the run."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def of(self, org_id: str, user_id: str) -> uuid.UUID | None:
        try:
            org, user = uuid.UUID(org_id), uuid.UUID(user_id)
        except ValueError:
            return None
        async with self._engine.connect() as connection:
            found = await connection.scalar(
                select(User.auth_user_id).where(User.org_id == org, User.id == user)
            )
        return found if isinstance(found, uuid.UUID) else None


class ChromeTarget:
    """Starts ``my_chrome`` sessions on the extension connections of the ``ChromeHub``."""

    def __init__(
        self,
        hub: ChromeHub,
        auth_user: AuthUserLookup,
        *,
        call_timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        self._hub = hub
        self._auth_user = auth_user
        self._timeout_s = call_timeout_s

    @property
    def name(self) -> TargetName:
        return MY_CHROME

    async def start(
        self,
        *,
        org_id: str,
        user_id: str | None,
        run_id: str,
        guard: NavigationGuard,
        session_id: str = "",
    ) -> BrowserSession:
        del guard  # The user's browser is on the user's network; Section 14.2.2 has the rules.
        if not user_id:
            raise BrowserError(NO_USER)
        auth_user_id = await self._auth_user(org_id, user_id)
        if auth_user_id is None or self._hub.link(auth_user_id) is None:
            raise BrowserError(OFFLINE)
        return ChromeSession(
            self._hub,
            auth_user_id,
            session_id=session_id,
            run_id=run_id,
            timeout_s=self._timeout_s,
        )

    async def aclose(self) -> None:
        return None


class ChromeSession:
    """The browser calls of one run in the Chrome of its user."""

    def __init__(
        self,
        hub: ChromeHub,
        auth_user_id: uuid.UUID,
        *,
        session_id: str,
        run_id: str,
        timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        self._hub = hub
        self._auth_user_id = auth_user_id
        self._session_id = session_id
        self._run_id = run_id
        self._timeout_s = timeout_s
        self.sensitivity = Sensitivity()
        self.closed = False
        self._generation = 0
        """Grows at each snapshot: a ref names an element of one snapshot only."""
        self._asked: dict[tuple[Any, ...], tuple[int, str]] = {}
        """The snapshot and the reason of each sensitive call when the gate first asked for it.
        A person approved that call for that element only."""
        self._link: ChromeLink | None = None
        self._used: list[ChromeLink] = []
        """The connections that this session used. ``close`` releases the run on each."""

    @property
    def target(self) -> TargetName:
        return MY_CHROME

    def _pick(self) -> ChromeLink:
        """The connection for the next call: the one that the session used, while it is open.
        When it closed, the newest connection of the user. A new browser has no refs of the
        old snapshot, so the session forgets them."""
        if self._link is not None and self._link in self._hub.links(self._auth_user_id):
            return self._link
        link = self._hub.link(self._auth_user_id)
        if link is None:
            raise BrowserError(OFFLINE)
        if self._link is not None:
            self.sensitivity = Sensitivity()
        self._link = link
        if link not in self._used:
            self._used.append(link)
        return link

    def sensitive_reason(self, arguments: Mapping[str, Any]) -> str | None:
        """The text for the gate. The first ask for a call keeps its snapshot and its reason."""
        reason = sensitive_reason(self.sensitivity, arguments)
        if reason is None:
            return None
        self._asked.setdefault(_call_key(arguments), (self._generation, reason))
        return _reason_text(reason)

    async def _call(
        self,
        op: Op,
        args: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        approved_reason: str | None = None,
    ) -> Any:
        link = self._pick()
        wait_s = timeout_s or self._timeout_s
        frame = {
            "op": op,
            "args": dict(args),
            "session_id": self._session_id,
            "run_id": self._run_id,
            "approved": approved_reason is not None,
            "approved_reason": approved_reason,
            # The extension answers before the API stops waiting.
            "timeout_ms": int(max(1.0, wait_s - ANSWER_MARGIN_S) * 1000),
        }
        try:
            answer = await link.call(frame, wait_s)
        except ChromeOfflineError:
            raise BrowserError(OFFLINE) from None
        except TimeoutError:
            raise BrowserError(ERRORS["timeout"]) from None
        if not isinstance(answer, dict):
            raise BrowserError(ERRORS["invalid"])
        if answer.get("ok") is True:
            return answer.get("result")
        error = _parse(_Error, answer.get("error") or {})
        raise BrowserError(ERRORS.get(error.code, ERRORS["failed"]), detail=error.message or None)

    async def navigate(self, url: str) -> PageState:
        if blocked_site(url):
            raise BrowserError(ERRORS["blocked"])
        return _parse(_Page, await self._call("navigate", {"url": url})).state()

    async def snapshot(self) -> Snapshot:
        shot = _parse(_Snapshot, await self._call("snapshot", {}))
        self.sensitivity = Sensitivity(
            refs={ref: reason for ref, reason in shot.sensitive.items()},
            submits=frozenset(shot.submits),
        )
        self._generation += 1
        dialog = None if shot.dialog is None else Dialog(shot.dialog.kind, shot.dialog.message)
        return Snapshot(page=shot.page.state(), tree=shot.tree, dialog=dialog)

    async def act(self, act: Act) -> PageState:
        args: dict[str, Any] = {
            "action": act.action,
            "ref": act.ref.removeprefix("ref=").strip() if act.ref else None,
            "text": act.text,
            "values": list(act.values),
            "submit": act.submit,
            "target_ref": act.target_ref.removeprefix("ref=").strip() if act.target_ref else None,
            "direction": act.direction,
            "amount": act.amount,
            "seconds": act.seconds,
            "accept": act.accept,
            "prompt_text": act.prompt_text,
        }
        # The extension skips its own question only when a person approved this element: the
        # gate asked for it in this snapshot, for this reason. Else the extension asks.
        asked = self._asked.pop(_call_key(args), None)
        code = sensitive_reason(self.sensitivity, args)
        if not human_approved.get() or code is None or asked != (self._generation, code):
            code = None
        return _parse(_Page, await self._call("act", args, approved_reason=code)).state()

    async def screenshot(self, *, full_page: bool = False) -> Screenshot:
        shot = _parse(_Screenshot, await self._call("screenshot", {"full_page": full_page}))
        try:
            data = base64.b64decode(shot.data, validate=True)
        except (binascii.Error, ValueError):
            raise BrowserError("the extension sent a screenshot that is not valid") from None
        if not data or len(data) > MAX_SCREENSHOT_BYTES:
            raise BrowserError("the screenshot of the extension is empty or too large")
        return Screenshot(page=shot.page.state(), data=data, mime_type=shot.mime_type)

    async def extract(self, request: ExtractRequest) -> Extracted:
        args = {"kind": request.kind, "ref": request.ref, "fields": dict(request.fields)}
        result = _parse(_Extracted, await self._call("extract", args))
        return Extracted(page=result.page.state(), kind=result.kind, content=result.content)

    async def tabs(
        self, action: TabAction, *, index: int | None = None, url: str | None = None
    ) -> list[Tab]:
        if url is not None and blocked_site(url):
            raise BrowserError(ERRORS["blocked"])
        args = {"action": action, "index": index, "url": url}
        result = _parse(_Tabs, await self._call("tabs", args))
        return [
            Tab(index=tab.index, url=tab.url, title=tab.title, active=tab.active)
            for tab in result.tabs
        ]

    async def close(self) -> None:
        """Tells the extension that the run ended: it detaches the debugger and removes the
        overlay. It does not raise."""
        if self.closed:
            return
        self.closed = True
        frame: dict[str, Any] = {
            "op": "release",
            "args": {},
            "session_id": self._session_id,
            "run_id": self._run_id,
            "approved": False,
            "approved_reason": None,
            "timeout_ms": 1_000,
        }
        for link in self._used:
            if link not in self._hub.links(self._auth_user_id):
                continue  # A closed connection has no run left.
            try:
                await link.call(dict(frame), RELEASE_TIMEOUT_S)
            except (ChromeOfflineError, TimeoutError):
                continue
            except Exception:  # pragma: no cover - a close never fails the run
                log.warning("the release of a my_chrome session failed", exc_info=True)
