"""The browser tools (docs/architecture.md, Section 14.1; roadmaps M5.1 and M5.3).

The six tools work on one browser session for each run and target: ``browser_open``,
``browser_snapshot``, ``browser_act``, ``browser_screenshot``, ``browser_extract``, and
``browser_tabs``. Each tool has an optional ``target``: ``cloud`` (Browserbase, M5.1) or
``my_chrome`` (the Chrome of the user through the sama extension, M5.5). The descriptions name
only the targets of the service. The agent reads the page as an accessibility tree with refs
(``[ref=e12]``) and acts on an element by its ref. Each output goes through the gate, the
redactor, and the output cap of the tool server, as for the other tools.

Vision mode (M5.2): ``browser_act`` can act at a point (``x``, ``y``) of the viewport instead of
a ref, for a page without a usable accessibility tree (a ``<canvas>`` app). The agent reads the
page with ``browser_screenshot``, whose caption gives the size of the image.

``browser_vault`` (M5.3) lists the saved logins of the user and fills one into the page. The
agent names the login. sama fills the user name and the password, and the result never shows
them. The server has the tool only when the process has the vault.

The gate gives ``browser_act`` and ``browser_vault`` ``ask`` by default: an action can use the
logins of the user. The other tools only read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp_types import TextContent, ToolAnnotations
from pydantic import Field

from sama.browser.target import (
    CLOUD,
    MAX_COORDINATE,
    MY_CHROME,
    Act,
    ActionKind,
    BrowserError,
    ExtractKind,
    Screenshot,
    ScrollDirection,
    TabAction,
    TargetName,
)
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.board import Call

if TYPE_CHECKING:
    # Only the type: ``sama.browser.service`` imports ``sama.tools``, so a runtime import here
    # makes an import cycle.
    from sama.browser.service import BrowserService

BROWSER_OPEN: Final = "browser_open"
BROWSER_SNAPSHOT: Final = "browser_snapshot"
BROWSER_ACT: Final = "browser_act"
BROWSER_SCREENSHOT: Final = "browser_screenshot"
BROWSER_EXTRACT: Final = "browser_extract"
BROWSER_TABS: Final = "browser_tabs"
BROWSER_VAULT: Final = "browser_vault"
BROWSER_TOOLS: Final = frozenset(
    {
        BROWSER_OPEN,
        BROWSER_SNAPSHOT,
        BROWSER_ACT,
        BROWSER_SCREENSHOT,
        BROWSER_EXTRACT,
        BROWSER_TABS,
    }
)
"""The tools that the server has with the browser. ``browser_vault`` needs the vault too."""

MAX_URL_CHARS: Final = 2_000
MAX_TEXT_CHARS: Final = 10_000
MAX_FIELDS: Final = 50

_Ref = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^(ref=)?[A-Za-z0-9_-]+$")]
_Url = Annotated[str, Field(min_length=1, max_length=MAX_URL_CHARS)]
_Coordinate = Annotated[float, Field(ge=0, le=MAX_COORDINATE)]

OPEN_DESCRIPTION: Final = (
    "Open a URL in the browser of this run. The result is the accessibility tree of the page "
    "(snapshot=true) with a [ref=...] for each element that browser_act can use."
)
TARGET_TEXT: Final = {
    CLOUD: (
        'target="cloud": a cloud browser. It keeps the cookies and logins of the user from run '
        "to run (not in a shared session). The URL policy blocks private and internal "
        "addresses."
    ),
    MY_CHROME: (
        'target="my_chrome": the Chrome of the user, through the sama extension, in the tab of '
        "this chat or in tabs that you open. The user sees a cursor and can stop you or take "
        "over at any time. The user allows each site once. Banks, payments, password managers, "
        "and Google account pages are blocked. A sensitive action (submit a form, buy, send, "
        "delete, or type into a password or payment field) waits for a click of the user. Use "
        "it when the user asks you to use their browser or a site where they are logged in."
    ),
}
SAME_TARGET: Final = " Without target, the tool uses the target of the last browser call."
SNAPSHOT_DESCRIPTION: Final = (
    "Read the active tab as an accessibility tree in YAML. Each element that browser_act can "
    "use has [ref=eN]. A new snapshot makes the old refs invalid. While a JavaScript dialog is "
    "open, the result shows the dialog."
)
ACT_DESCRIPTION: Final = (
    "Do one action in the active tab. Actions: click (ref, clicks: 2 for a double click), type "
    "(ref, text, submit), select (ref, values), hover (ref), drag (ref, target_ref), scroll "
    "(direction and amount, or ref to scroll it into view), press (text: a key such as Enter "
    "or Control+A; ref optional), wait (seconds, at most 10), back, forward, reload, and "
    "dialog (accept, prompt_text: answer an open dialog). The refs come from the latest "
    "snapshot. Vision mode, for a page without usable refs (for example a canvas): click, "
    "type, hover, and scroll take x and y instead of ref, and drag takes x, y, to_x, and to_y. "
    "A point is in the pixels of the latest browser_screenshot with full_page=false, from the "
    "top left corner. type at a point clicks there and then types with the keyboard. The "
    "result is a new snapshot (snapshot=true); in vision mode, set snapshot=false and take a "
    "new screenshot."
)
SCREENSHOT_DESCRIPTION: Final = (
    "Take a JPEG screenshot of the active tab (full_page: the whole page, up to 6000 pixels "
    "high). Use it to look at the layout or at content that the snapshot does not show. A "
    "screenshot with full_page=false is the viewport: browser_act takes x and y in its pixels "
    "(vision mode)."
)
EXTRACT_DESCRIPTION: Final = (
    "Read data from the active tab with no model call. Kinds: text (the visible text), links "
    "(text and href), tables (rows of cells), fields (fields: a name for each ref; the value "
    "or the text of each element). ref limits text, links, and tables to one element."
)
VAULT_DESCRIPTION: Final = (
    "Use the saved logins of the user. action=list gives the name and the site of each saved "
    "login. action=fill (name) fills the user name and the password of that login into the "
    "sign-in form of the active tab, only on the site of the login; you never see the values. "
    "username_ref and password_ref pick the fields when sama cannot find them; submit=true "
    "presses Enter after the fill. For a sign-in in two steps, fill, continue, and fill again. "
    "Do not ask the user for a password: ask them to save the login in Settings."
)
TABS_DESCRIPTION: Final = (
    "Manage the tabs of the browser. Actions: list, new (url optional; the new tab becomes "
    "active), select (index), close (index, or the active tab). A tab that a page opens becomes "
    "the active tab."
)


def _targets_text(service: BrowserService) -> str:
    names = list(service.targets)
    lines = " ".join(TARGET_TEXT[name] for name in names if name in TARGET_TEXT)
    if len(names) == 1:
        return f" {lines}"
    return (
        f" target: {' or '.join(names)}. Without it, the target of the last browser call of "
        f"this run, else the browser that the user chose for this session (Cloud or My Chrome), "
        f"else {names[0]}. Use my_chrome when the user says 'in my browser' or 'open a tab', or "
        f"for a site where the user is logged in; use cloud for background research. {lines}"
    )


def _with_target(arguments: dict[str, Any], target: TargetName | None) -> dict[str, Any]:
    if target is not None:
        arguments["target"] = target
    return arguments


def register_browser(server: MCPServer, call: Call, service: BrowserService) -> None:
    """Adds the six browser tools to ``server``, and ``browser_vault`` when the service has the
    saved logins."""
    reads = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    targets = _targets_text(service)
    acts = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

    @server.tool(
        name=BROWSER_OPEN,
        description=OPEN_DESCRIPTION + targets,
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True),
        structured_output=False,
    )
    async def browser_open(
        ctx: Context, url: _Url, snapshot: bool = True, target: TargetName | None = None
    ) -> str:
        async def body(claims: RunTokenClaims) -> str:
            return await service.open(claims, url, snapshot=snapshot, target=target)

        arguments = _with_target({"url": url, "snapshot": snapshot}, target)
        return await call(ctx, BROWSER_OPEN, arguments, body)

    @server.tool(
        name=BROWSER_SNAPSHOT,
        description=SNAPSHOT_DESCRIPTION + SAME_TARGET,
        annotations=reads,
        structured_output=False,
    )
    async def browser_snapshot(ctx: Context, target: TargetName | None = None) -> str:
        async def body(claims: RunTokenClaims) -> str:
            return await service.snapshot(claims, target=target)

        return await call(ctx, BROWSER_SNAPSHOT, _with_target({}, target), body)

    @server.tool(
        name=BROWSER_ACT,
        description=ACT_DESCRIPTION + SAME_TARGET,
        annotations=acts,
        structured_output=False,
    )
    async def browser_act(
        action: ActionKind,
        ctx: Context,
        ref: _Ref | None = None,
        text: Annotated[str | None, Field(max_length=MAX_TEXT_CHARS)] = None,
        values: Annotated[list[Annotated[str, Field(max_length=500)]] | None, Field(max_length=50)]
        | None = None,
        submit: bool = False,
        target_ref: _Ref | None = None,
        direction: ScrollDirection = "down",
        amount: Annotated[int, Field(ge=1, le=20_000)] = 600,
        seconds: Annotated[float, Field(ge=0, le=10)] = 1.0,
        accept: bool = True,
        prompt_text: Annotated[str | None, Field(max_length=MAX_TEXT_CHARS)] = None,
        snapshot: bool = True,
        target: TargetName | None = None,
        x: _Coordinate | None = None,
        y: _Coordinate | None = None,
        to_x: _Coordinate | None = None,
        to_y: _Coordinate | None = None,
        clicks: Annotated[int, Field(ge=1, le=3)] = 1,
    ) -> str:
        act = Act(
            action=action,
            ref=ref,
            x=x,
            y=y,
            to_x=to_x,
            to_y=to_y,
            clicks=clicks,
            text=text,
            values=tuple(values or ()),
            submit=submit,
            target_ref=target_ref,
            direction=direction,
            amount=amount,
            seconds=seconds,
            accept=accept,
            prompt_text=prompt_text,
        )

        async def body(claims: RunTokenClaims) -> str:
            return await service.act(claims, act, snapshot=snapshot, target=target)

        arguments: dict[str, Any] = {
            key: value
            for key, value in {
                "action": action,
                "ref": ref,
                "text": text,
                "values": values,
                "submit": submit,
                "target_ref": target_ref,
                "direction": direction,
                "amount": amount,
                "seconds": seconds,
                "accept": accept,
                "prompt_text": prompt_text,
                "snapshot": snapshot,
                "target": target,
                "x": x,
                "y": y,
                "to_x": to_x,
                "to_y": to_y,
                "clicks": clicks if clicks != 1 else None,
            }.items()
            if value is not None
        }
        return await call(ctx, BROWSER_ACT, arguments, body)

    @server.tool(
        name=BROWSER_SCREENSHOT,
        description=SCREENSHOT_DESCRIPTION + SAME_TARGET,
        annotations=reads,
        structured_output=False,
    )
    async def browser_screenshot(
        ctx: Context, full_page: bool = False, target: TargetName | None = None
    ) -> list[Any]:
        taken: list[Screenshot] = []

        async def body(claims: RunTokenClaims) -> str:
            caption, shot = await service.screenshot(claims, full_page=full_page, target=target)
            taken.append(shot)
            return caption

        arguments = _with_target({"full_page": full_page}, target)
        caption = await call(ctx, BROWSER_SCREENSHOT, arguments, body)
        content: list[Any] = [TextContent(type="text", text=caption)]
        if taken:
            image_format = taken[0].mime_type.removeprefix("image/")
            content.append(Image(data=taken[0].data, format=image_format))
        return content

    @server.tool(
        name=BROWSER_EXTRACT,
        description=EXTRACT_DESCRIPTION + SAME_TARGET,
        annotations=reads,
        structured_output=False,
    )
    async def browser_extract(
        kind: ExtractKind,
        ctx: Context,
        ref: _Ref | None = None,
        fields: Annotated[dict[str, _Ref] | None, Field(max_length=MAX_FIELDS)] = None,
        target: TargetName | None = None,
    ) -> str:
        async def body(claims: RunTokenClaims) -> str:
            return await service.extract(claims, kind, ref=ref, fields=fields, target=target)

        arguments: dict[str, Any] = _with_target({"kind": kind}, target)
        if ref is not None:
            arguments["ref"] = ref
        if fields is not None:
            arguments["fields"] = fields
        return await call(ctx, BROWSER_EXTRACT, arguments, body)

    @server.tool(
        name=BROWSER_TABS,
        description=TABS_DESCRIPTION + SAME_TARGET,
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True),
        structured_output=False,
    )
    async def browser_tabs(
        ctx: Context,
        action: TabAction = "list",
        index: Annotated[int | None, Field(ge=0, le=50)] = None,
        url: _Url | None = None,
        target: TargetName | None = None,
    ) -> str:
        async def body(claims: RunTokenClaims) -> str:
            return await service.tabs(claims, action, index=index, url=url, target=target)

        arguments: dict[str, Any] = _with_target({"action": action}, target)
        if index is not None:
            arguments["index"] = index
        if url is not None:
            arguments["url"] = url
        return await call(ctx, BROWSER_TABS, arguments, body)

    if service.logins is None:
        return

    @server.tool(
        name=BROWSER_VAULT,
        description=VAULT_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True),
        structured_output=False,
    )
    async def browser_vault(
        action: Literal["list", "fill"],
        ctx: Context,
        name: Annotated[str | None, Field(min_length=1, max_length=80)] = None,
        username_ref: _Ref | None = None,
        password_ref: _Ref | None = None,
        submit: bool = False,
        snapshot: bool = True,
        target: TargetName | None = None,
    ) -> str:
        async def body(claims: RunTokenClaims) -> str:
            if action == "list":
                return await service.vault_list(claims)
            if not name:
                raise BrowserError("fill needs name: the name of a saved login from the list")
            return await service.vault_fill(
                claims,
                name,
                username_ref=username_ref,
                password_ref=password_ref,
                submit=submit,
                snapshot=snapshot,
                target=target,
            )

        # The arguments have no secret: only the name of the login and the refs.
        arguments: dict[str, Any] = {
            key: value
            for key, value in {
                "action": action,
                "name": name,
                "username_ref": username_ref,
                "password_ref": password_ref,
                "submit": submit if action == "fill" else None,
                "snapshot": snapshot if action == "fill" else None,
                "target": target,
            }.items()
            if value is not None
        }
        return await call(ctx, BROWSER_VAULT, arguments, body)
