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

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

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
"""The actions that need the ``ref`` of an element from the latest snapshot."""

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
    """One action of ``browser_act``. M5.2 (vision mode) adds a coordinate point as a second way
    to name the element."""

    action: ActionKind
    ref: str | None = None
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
    """One live browser of one run."""

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
