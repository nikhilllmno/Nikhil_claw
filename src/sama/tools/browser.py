"""The browser tools (docs/architecture.md, Section 14.1; roadmap M5.1).

The six tools work on one browser session for each run: ``browser_open``,
``browser_snapshot``, ``browser_act``, ``browser_screenshot``, ``browser_extract``, and
``browser_tabs``. The agent reads the page as an accessibility tree with refs (``[ref=e12]``) and
acts on an element by its ref. Each output goes through the gate, the redactor, and the output
cap of the tool server, as for the other tools.

The gate gives ``browser_act`` ``ask`` by default: an action can use the logins of the user. The
other tools only read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Final

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp_types import TextContent, ToolAnnotations
from pydantic import Field

from sama.browser.target import Act, ActionKind, ExtractKind, Screenshot, ScrollDirection, TabAction
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

MAX_URL_CHARS: Final = 2_000
MAX_TEXT_CHARS: Final = 10_000
MAX_FIELDS: Final = 50

_Ref = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^(ref=)?[A-Za-z0-9_-]+$")]
_Url = Annotated[str, Field(min_length=1, max_length=MAX_URL_CHARS)]

OPEN_DESCRIPTION: Final = (
    "Open a URL in the cloud browser of this run. The browser keeps the cookies and logins of "
    "the user from run to run (not in a shared session). The result is the accessibility tree "
    "of the page (snapshot=true) with a [ref=...] for each element that browser_act can use. "
    "The URL policy blocks private and internal addresses."
)
SNAPSHOT_DESCRIPTION: Final = (
    "Read the active tab as an accessibility tree in YAML. Each element that browser_act can "
    "use has [ref=eN]. A new snapshot makes the old refs invalid. While a JavaScript dialog is "
    "open, the result shows the dialog."
)
ACT_DESCRIPTION: Final = (
    "Do one action in the active tab. Actions: click (ref), type (ref, text, submit), select "
    "(ref, values), hover (ref), drag (ref, target_ref), scroll (direction and amount, or ref "
    "to scroll it into view), press (text: a key such as Enter or Control+A; ref optional), "
    "wait (seconds, at most 10), back, forward, reload, and dialog (accept, prompt_text: answer "
    "an open dialog). The refs come from the latest snapshot. The result is a new snapshot "
    "(snapshot=true)."
)
SCREENSHOT_DESCRIPTION: Final = (
    "Take a JPEG screenshot of the active tab (full_page: the whole page, up to 6000 pixels "
    "high). Use it to look at the layout or at content that the snapshot does not show."
)
EXTRACT_DESCRIPTION: Final = (
    "Read data from the active tab with no model call. Kinds: text (the visible text), links "
    "(text and href), tables (rows of cells), fields (fields: a name for each ref; the value "
    "or the text of each element). ref limits text, links, and tables to one element."
)
TABS_DESCRIPTION: Final = (
    "Manage the tabs of the browser. Actions: list, new (url optional; the new tab becomes "
    "active), select (index), close (index, or the active tab). A tab that a page opens becomes "
    "the active tab."
)


def register_browser(server: MCPServer, call: Call, service: BrowserService) -> None:
    """Adds the six browser tools to ``server``."""
    reads = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    acts = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

    @server.tool(
        name=BROWSER_OPEN,
        description=OPEN_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True),
        structured_output=False,
    )
    async def browser_open(ctx: Context, url: _Url, snapshot: bool = True) -> str:
        async def body(claims: RunTokenClaims) -> str:
            return await service.open(claims, url, snapshot=snapshot)

        return await call(ctx, BROWSER_OPEN, {"url": url, "snapshot": snapshot}, body)

    @server.tool(
        name=BROWSER_SNAPSHOT,
        description=SNAPSHOT_DESCRIPTION,
        annotations=reads,
        structured_output=False,
    )
    async def browser_snapshot(ctx: Context) -> str:
        async def body(claims: RunTokenClaims) -> str:
            return await service.snapshot(claims)

        return await call(ctx, BROWSER_SNAPSHOT, {}, body)

    @server.tool(
        name=BROWSER_ACT,
        description=ACT_DESCRIPTION,
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
    ) -> str:
        act = Act(
            action=action,
            ref=ref,
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
            return await service.act(claims, act, snapshot=snapshot)

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
            }.items()
            if value is not None
        }
        return await call(ctx, BROWSER_ACT, arguments, body)

    @server.tool(
        name=BROWSER_SCREENSHOT,
        description=SCREENSHOT_DESCRIPTION,
        annotations=reads,
        structured_output=False,
    )
    async def browser_screenshot(ctx: Context, full_page: bool = False) -> list[Any]:
        taken: list[Screenshot] = []

        async def body(claims: RunTokenClaims) -> str:
            caption, shot = await service.screenshot(claims, full_page=full_page)
            taken.append(shot)
            return caption

        caption = await call(ctx, BROWSER_SCREENSHOT, {"full_page": full_page}, body)
        content: list[Any] = [TextContent(type="text", text=caption)]
        if taken:
            image_format = taken[0].mime_type.removeprefix("image/")
            content.append(Image(data=taken[0].data, format=image_format))
        return content

    @server.tool(
        name=BROWSER_EXTRACT,
        description=EXTRACT_DESCRIPTION,
        annotations=reads,
        structured_output=False,
    )
    async def browser_extract(
        kind: ExtractKind,
        ctx: Context,
        ref: _Ref | None = None,
        fields: Annotated[dict[str, _Ref] | None, Field(max_length=MAX_FIELDS)] = None,
    ) -> str:
        async def body(claims: RunTokenClaims) -> str:
            return await service.extract(claims, kind, ref=ref, fields=fields)

        arguments: dict[str, Any] = {"kind": kind}
        if ref is not None:
            arguments["ref"] = ref
        if fields is not None:
            arguments["fields"] = fields
        return await call(ctx, BROWSER_EXTRACT, arguments, body)

    @server.tool(
        name=BROWSER_TABS,
        description=TABS_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True),
        structured_output=False,
    )
    async def browser_tabs(
        ctx: Context,
        action: TabAction = "list",
        index: Annotated[int | None, Field(ge=0, le=50)] = None,
        url: _Url | None = None,
    ) -> str:
        async def body(claims: RunTokenClaims) -> str:
            return await service.tabs(claims, action, index=index, url=url)

        arguments: dict[str, Any] = {"action": action}
        if index is not None:
            arguments["index"] = index
        if url is not None:
            arguments["url"] = url
        return await call(ctx, BROWSER_TABS, arguments, body)
