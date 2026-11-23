"""The browser target interface (docs/architecture.md, Section 14.1, decision D12).

The browser tools have one contract and more than one target. A target starts a browser
session for a run. The session does the page work: navigate, snapshot with refs, act on a ref,
screenshot, extract, and tabs.

- ``cloud`` (M5.1) is a Browserbase session with a persistent context per user.
- ``my_chrome`` (M5.5) relays the same calls to the Chrome extension of the user
  (``sama.browser.chrome``).

The tool service (``sama.browser.service``) owns the sessions, the URL guard, and the text for
the model. A target knows only its browser.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol, runtime_checkable

from pydantic import SecretStr

TargetName = Literal["cloud", "my_chrome"]
CLOUD: Final = "cloud"
MY_CHROME: Final = "my_chrome"
TARGETS: Final = (CLOUD, MY_CHROME)
UNTRUSTED_SOURCE: Final = "browser"
"""The ``source`` of the ``untrusted_content`` blocks of the browser tools."""

ActionKind = Literal[
    "click",
    "type",
    "select",
    "hover",
    "scroll",
    "press",
    "drag",
    "wait",
    "back",
    "forward",
    "reload",
    "dialog",
]
ACTIONS_WITH_REF: Final = frozenset({"click", "type", "select", "hover", "drag"})
"""The actions that need the ``ref`` of an element from the latest snapshot, or a point (``x``
and ``y``) in vision mode. ``select`` needs a ref."""
ACTIONS_WITH_POINT: Final = frozenset({"click", "type", "hover", "drag", "scroll"})
"""The actions that can use a point (``x`` and ``y``) instead of a ref: vision mode (M5.2)."""
MAX_COORDINATE: Final = 10_000
"""The largest ``x`` or ``y`` that a tool call can give. The session checks the point against
the viewport."""

ExtractKind = Literal["text", "links", "tables", "fields"]
TabAction = Literal["list", "new", "select", "close"]
ScrollDirection = Literal["up", "down", "left", "right"]


class BrowserError(Exception):
    """A browser call failed in a way that the agent can fix or report. The message is for the
    agent and is fixed text of sama. ``detail`` is text from the page or the browser (an error
    text, a URL): it goes in an ``untrusted_content`` block, never in the message."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        if not self.detail:
            return self.message
        # Imported here: ``sama.tools`` imports this module (the tool server), so a module-level
        # import makes an import cycle when ``sama.browser`` is imported first.
        from sama.tools.output import untrusted

        return f"{self.message}\n{untrusted(self.detail, source=UNTRUSTED_SOURCE)}"


@dataclass(frozen=True, slots=True)
class Act:
    """One action of ``browser_act``. An action names its element with a ``ref`` from the latest
    snapshot, or with a point (``x``, ``y``) in the pixels of the latest viewport screenshot
    (vision mode, M5.2). A point is in CSS pixels from the top left corner of the viewport."""

    action: ActionKind
    ref: str | None = None
    x: float | None = None
    y: float | None = None
    """The point of the action in vision mode (``click``, ``type``, ``hover``, ``drag``,
    ``scroll``)."""
    to_x: float | None = None
    to_y: float | None = None
    """The point to drop on (``drag`` in vision mode)."""
    clicks: int = 1
    """The number of clicks (``click``): 2 is a double click."""
    text: str | None = None
    """The text to type (``type``) or the key to press (``press``, for example ``Enter``)."""
    values: tuple[str, ...] = ()
    """The option values or labels to select (``select``)."""
    submit: bool = False
    """Press Enter after ``type``."""
    target_ref: str | None = None
    """The element to drop on (``drag``)."""
    direction: ScrollDirection = "down"
    amount: int = 600
    """Pixels to scroll."""
    seconds: float = 1.0
    """How long to wait (``wait``)."""
    accept: bool = True
    """Accept or dismiss the open dialog (``dialog``)."""
    prompt_text: str | None = None
    """The answer to a ``prompt`` dialog."""

    @property
    def point(self) -> tuple[float, float] | None:
        """The point of the action, or ``None`` when the action uses a ref."""
        if self.x is None or self.y is None:
            return None
        return (self.x, self.y)

    @property
    def to_point(self) -> tuple[float, float] | None:
        if self.to_x is None or self.to_y is None:
            return None
        return (self.to_x, self.to_y)


@dataclass(frozen=True, slots=True)
class PageState:
    url: str
    title: str
    tab: int
    """The index of the active tab."""
    tabs: int = 1
    notes: tuple[str, ...] = ()
    """What sama did that the agent must know, for example a blocked navigation or a new tab.
    Fixed text of sama only: no text of the page."""
    blocked: tuple[str, ...] = ()
    """The URLs that the URL policy blocked since the last state, each with the reason. A page
    chooses these URLs, so they go in the ``untrusted_content`` block."""


@dataclass(frozen=True, slots=True)
class Dialog:
    """A JavaScript dialog that waits for ``browser_act`` with ``action="dialog"``."""

    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class Snapshot:
    page: PageState
    tree: str
    """The accessibility tree in YAML, with ``[ref=eN]`` on the elements that an action can
    use. Empty while a dialog is open."""
    dialog: Dialog | None = None


@dataclass(frozen=True, slots=True)
class Screenshot:
    page: PageState
    data: bytes
    mime_type: str
    width: int = 0
    height: int = 0
    """The size of the image in pixels, or 0 when the target does not know it. For a viewport
    screenshot of a target with vision mode, one pixel is one CSS pixel of the viewport, so the
    agent can give a point of the image to ``browser_act``."""
    full_page: bool = False


@dataclass(frozen=True, slots=True)
class Tab:
    index: int
    url: str
    title: str
    active: bool


@dataclass(frozen=True, slots=True)
class Extracted:
    page: PageState
    kind: ExtractKind
    content: str
    """Plain text for ``text``, JSON for the other kinds."""


@dataclass(frozen=True, slots=True)
class ExtractRequest:
    kind: ExtractKind
    ref: str | None = None
    """Limit ``text``, ``links``, and ``tables`` to this element."""
    fields: Mapping[str, str] = field(default_factory=dict)
    """For ``fields``: a name for each ref."""


class BrowserSession(Protocol):
    """One live browser of one run.

    A session that can act at a point (vision mode, M5.2) has the class attribute
    ``vision = True``. The service reads it with ``getattr``, so a session without it (for
    example ``my_chrome`` before it adds vision mode) refuses a point with a clear error."""

    @property
    def target(self) -> TargetName: ...

    async def navigate(self, url: str) -> PageState:
        """Opens ``url`` in the active tab. The service checked the URL before."""
        ...

    async def snapshot(self) -> Snapshot: ...

    async def act(self, act: Act) -> PageState: ...

    async def screenshot(self, *, full_page: bool = False) -> Screenshot: ...

    async def extract(self, request: ExtractRequest) -> Extracted: ...

    async def tabs(
        self, action: TabAction, *, index: int | None = None, url: str | None = None
    ) -> list[Tab]:
        """Lists, opens, selects, or closes tabs. ``new`` opens ``url`` (checked before) or a
        blank tab and selects it."""
        ...

    async def close(self) -> None:
        """Ends the browser. It does not raise."""
        ...


@dataclass(frozen=True, slots=True)
class FillRequest:
    """A saved login to fill into the active tab (``browser_vault``, M5.3). The secret never goes
    back to the agent."""

    username: SecretStr
    password: SecretStr
    site_allows: Callable[[str], bool]
    """True for the URL of a document where sama may fill the login (the site of the login)."""
    username_ref: str | None = None
    password_ref: str | None = None
    submit: bool = False
    """Press Enter in the last filled field."""


@dataclass(frozen=True, slots=True)
class Filled:
    page: PageState
    fields: tuple[str, ...]
    """The fields that sama filled: ``username``, ``password``, or both."""


@runtime_checkable
class LoginFiller(Protocol):
    """A browser session that can fill a saved login. A target without it gives the agent an
    error for ``browser_vault`` with ``action="fill"``."""

    async def fill_login(self, request: FillRequest) -> Filled:
        """Fills the user name and the password into the fields of the active tab. It refuses a
        field in a document that ``site_allows`` refuses. An error message has no part of the
        secret."""
        ...


@dataclass(frozen=True, slots=True)
class LiveView:
    """What the web app shows of a live browser session (M8.3, Section 14.1): a short-lived
    URL of the live view of the active tab, the URL and the title of that tab, and the number
    of tabs. The live view URL lets a person watch and use the browser, so only a member who
    can see the session gets it."""

    view_url: str
    page_url: str
    title: str
    tabs: int = 1
    page_id: str = ""
    """The id of the tab at the provider. The view URL of one tab changes at each request (a new
    time to live), so the web app loads a new URL only when the tab changes."""


class LiveViewSource(Protocol):
    """A session whose browser a person can watch (the ``cloud`` target)."""

    async def live_view(self, ttl_s: int) -> LiveView | None: ...


def has_live_view(session: object) -> bool:
    """True when ``session`` can give a live view (M8.3)."""
    return callable(getattr(session, "live_view", None))


def has_vision(session: object) -> bool:
    """True when ``session`` can act at a point (vision mode, M5.2)."""
    return getattr(session, "vision", False) is True


class BrowserTarget(Protocol):
    """Starts browser sessions of one kind."""

    @property
    def name(self) -> TargetName: ...

    async def start(
        self,
        *,
        org_id: str,
        user_id: str | None,
        run_id: str,
        guard: NavigationGuard,
        session_id: str = "",
    ) -> BrowserSession:
        """Starts a session. With a ``user_id``, the session uses the persistent context of the
        user, so logins stay from run to run. Without one (a shared session), the context is
        new and is not kept. ``session_id`` is the sama session of the run: the ``my_chrome``
        target acts in the tab that is bound to it."""
        ...

    async def aclose(self) -> None: ...


class NavigationGuard(Protocol):
    """The outbound URL policy for browser navigation (Section 16.4)."""

    async def check(self, url: str) -> None:
        """Raises ``BlockedUrlError`` if the browser must not open ``url``."""
        ...

    def quick_problem(self, url: str) -> str | None:
        """A check without DNS for every request of a page: why the URL is refused, or
        ``None``."""
        ...
