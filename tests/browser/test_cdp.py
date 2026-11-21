"""``PlaywrightSession`` with a local headless Chromium and a local web site (Section 14.1).

The ``cloud`` target gives the session a Browserbase browser over CDP. These tests give it a
local browser, so they check the page work without the network. They skip when the Chromium
headless shell is not installed (``uv run playwright install --only-shell chromium``).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from sama.browser.cdp import PlaywrightSession
from sama.browser.target import Act, BrowserError, ExtractRequest
from sama.tools.outbound import BlockedUrlError
from tests.browser.fakes import DenyHosts
from tests.conformance.scripted_model import serve

if TYPE_CHECKING:
    from playwright.async_api import Browser

HOME = """<!doctype html><title>Home</title>
<h1>Welcome</h1>
<form action="/search">
  <label>Query <input name="q"></label>
  <label>Size <select name="size"><option value="s">Small</option>
    <option value="l">Large</option></select></label>
  <label><input type="checkbox" name="agree"> Agree</label>
  <button type="submit">Search</button>
</form>
<a href="/next">Next page</a>
<a href="/next" target="_blank">New tab</a>
<a href="http://blocked.test/">Blocked site</a>
<a href="/redirect">Redirect away</a>
<button onclick="document.title = confirm('Sure?') ? 'yes' : 'no'">Ask</button>
<button onclick="document.getElementById('out').textContent = 'clicked'">Click me</button>
<p id="out"></p>
<table><tr><th>Room</th><th>Price</th></tr><tr><td>Deluxe</td><td>100</td></tr></table>
"""


def _app(port_of: list[int]) -> Starlette:
    async def home(request: Request) -> Response:
        return HTMLResponse(HOME)

    async def page(request: Request) -> Response:
        return HTMLResponse("<title>Next</title><p>The next page</p>")

    async def search(request: Request) -> Response:
        query = request.query_params.get("q", "")
        size = request.query_params.get("size", "")
        return HTMLResponse(f"<title>Results</title><p>You searched {query} ({size})</p>")

    async def redirect(request: Request) -> Response:
        # A redirect from the server to a host that the policy blocks.
        return RedirectResponse(f"http://localhost:{port_of[0]}/secret", status_code=302)

    async def secret(request: Request) -> Response:
        return HTMLResponse("<title>Secret</title><p>internal data</p>")

    return Starlette(
        routes=[
            Route("/", home),
            Route("/next", page),
            Route("/search", search),
            Route("/redirect", redirect),
            Route("/secret", secret),
        ]
    )


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    port_of: list[int] = []
    with serve(_app(port_of)) as url:
        port_of.append(int(url.rsplit(":", 1)[1]))
        yield url


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except Exception as error:  # the headless shell is not installed
            pytest.skip(f"Chromium does not start: {str(error).splitlines()[0]}")
        yield browser
        if browser.is_connected():
            await browser.close()


@pytest.fixture
def guard() -> DenyHosts:
    return DenyHosts(frozenset({"blocked.test", "localhost"}))


@pytest.fixture
async def session(browser: Browser, guard: DenyHosts) -> AsyncIterator[PlaywrightSession]:
    closed: list[bool] = []

    async def on_close() -> None:
        closed.append(True)

    session = await PlaywrightSession.attach(browser, guard, target="cloud", on_close=on_close)
    yield session
    await session.close()
    assert closed == [True]


def ref_of(tree: str, text: str) -> str:
    """The ref of the first line of the tree that has ``text``."""
    for line in tree.splitlines():
        if text in line:
            found = re.search(r"\[ref=([A-Za-z0-9_-]+)\]", line)
            if found:
                return found[1]
    raise AssertionError(f"no ref for {text!r} in:\n{tree}")


async def test_navigate_and_snapshot_with_refs(session: PlaywrightSession, site: str) -> None:
    state = await session.navigate(site + "/")
    assert (state.url, state.title, state.tab, state.tabs) == (site + "/", "Home", 0, 1)
    shot = await session.snapshot()
    assert 'heading "Welcome"' in shot.tree
    assert 'button "Search"' in shot.tree
    assert re.search(r"link \"Next page\" \[ref=[a-z0-9]+\]", shot.tree)


async def test_click_type_select_and_submit(session: PlaywrightSession, site: str) -> None:
    await session.navigate(site + "/")
    tree = (await session.snapshot()).tree
    await session.act(Act(action="click", ref=ref_of(tree, '"Click me"')))
    text = await session.extract(ExtractRequest(kind="text"))
    assert "clicked" in text.content
    await session.act(Act(action="select", ref=ref_of(tree, 'combobox "Size"'), values=("Large",)))
    await session.act(Act(action="click", ref=ref_of(tree, 'checkbox "Agree"')))
    fields = await session.extract(
        ExtractRequest(
            kind="fields",
            fields={"size": ref_of(tree, 'combobox "Size"'), "agree": ref_of(tree, "Agree")},
        )
    )
    assert json.loads(fields.content) == {"size": "Large", "agree": True}
    state = await session.act(
        Act(action="type", ref=ref_of(tree, 'textbox "Query"'), text="rooms", submit=True)
    )
    assert state.title == "Results"
    assert "q=rooms" in state.url
    assert "You searched rooms (l)" in (await session.extract(ExtractRequest(kind="text"))).content
    back = await session.act(Act(action="back"))
    assert back.title == "Home"


async def test_a_stale_ref_gives_a_clear_error(session: PlaywrightSession, site: str) -> None:
    await session.navigate(site + "/")
    await session.snapshot()
    with pytest.raises(BrowserError, match="e999"):
        await session.act(Act(action="click", ref="e999"))


async def test_a_dialog_waits_for_the_agent(session: PlaywrightSession, site: str) -> None:
    await session.navigate(site + "/")
    tree = (await session.snapshot()).tree
    await session.act(Act(action="click", ref=ref_of(tree, '"Ask"')))
    shot = await session.snapshot()
    assert shot.dialog is not None
    assert (shot.dialog.kind, shot.dialog.message) == ("confirm", "Sure?")
    assert shot.tree == ""
    with pytest.raises(BrowserError, match="a confirm dialog is open"):
        await session.act(Act(action="click", ref=ref_of(tree, '"Click me"')))
    state = await session.act(Act(action="dialog", accept=False))
    assert state.title == "no"
    with pytest.raises(BrowserError, match="no dialog is open"):
        await session.act(Act(action="dialog"))


async def test_a_new_tab_from_the_page_becomes_active(
    session: PlaywrightSession, site: str
) -> None:
    await session.navigate(site + "/")
    tree = (await session.snapshot()).tree
    state = await session.act(Act(action="click", ref=ref_of(tree, '"New tab"')))
    assert "a new tab opened and is now the active tab" in state.notes
    assert (state.tab, state.tabs) == (1, 2)
    assert state.url == site + "/next"
    tabs = await session.tabs("list")
    assert [(t.index, t.active) for t in tabs] == [(0, False), (1, True)]
    tabs = await session.tabs("select", index=0)
    assert [t.active for t in tabs] == [True, False]
    tabs = await session.tabs("close", index=1)
    assert [(t.title, t.active) for t in tabs] == [("Home", True)]
    tabs = await session.tabs("new", url=site + "/next")
    assert [(t.title, t.active) for t in tabs] == [("Home", False), ("Next", True)]
    with pytest.raises(BrowserError, match="there is no tab 5"):
        await session.tabs("select", index=5)


async def test_closing_the_last_tab_opens_a_blank_one(
    session: PlaywrightSession, site: str
) -> None:
    await session.navigate(site + "/")
    tabs = await session.tabs("close")
    assert [(t.url, t.active) for t in tabs] == [("about:blank", True)]


async def test_the_policy_blocks_a_link_and_a_redirect(
    session: PlaywrightSession, site: str, guard: DenyHosts
) -> None:
    await session.navigate(site + "/")
    tree = (await session.snapshot()).tree
    state = await session.act(Act(action="click", ref=ref_of(tree, '"Blocked site"')))
    blocked = "the URL policy blocked a navigation to http://blocked.test/"
    assert any(blocked in note for note in state.notes)
    assert state.url == site + "/"  # back on the page before the blocked link
    tree = (await session.snapshot()).tree  # the page loaded again, so the refs are new
    state = await session.act(Act(action="click", ref=ref_of(tree, '"Redirect away"')))
    assert any("the URL policy blocked" in note for note in state.notes)
    assert "internal data" not in (await session.extract(ExtractRequest(kind="text"))).content
    assert not state.url.startswith("http://localhost")


async def test_navigate_to_a_blocked_url_raises(session: PlaywrightSession) -> None:
    with pytest.raises(BlockedUrlError, match="the URL policy blocked a navigation"):
        await session.navigate("http://blocked.test/")


async def test_extract_links_tables_and_a_scoped_text(
    session: PlaywrightSession, site: str
) -> None:
    await session.navigate(site + "/")
    links = json.loads((await session.extract(ExtractRequest(kind="links"))).content)
    assert {"text": "Next page", "href": site + "/next"} in links
    tables = json.loads((await session.extract(ExtractRequest(kind="tables"))).content)
    assert tables == [[["Room", "Price"], ["Deluxe", "100"]]]
    tree = (await session.snapshot()).tree
    scoped = await session.extract(ExtractRequest(kind="text", ref=ref_of(tree, "heading")))
    assert scoped.content == "Welcome"


async def test_screenshots(session: PlaywrightSession, site: str) -> None:
    await session.navigate(site + "/")
    shot = await session.screenshot()
    assert shot.mime_type == "image/jpeg"
    assert shot.data[:3] == b"\xff\xd8\xff"
    full = await session.screenshot(full_page=True)
    assert full.data[:3] == b"\xff\xd8\xff"
    assert full.page.title == "Home"


async def test_scroll_press_wait_and_reload(session: PlaywrightSession, site: str) -> None:
    await session.navigate(site + "/")
    await session.act(Act(action="scroll", direction="down", amount=200))
    await session.act(Act(action="press", text="Tab"))
    await session.act(Act(action="wait", seconds=0.05))
    state = await session.act(Act(action="reload"))
    assert state.title == "Home"
    with pytest.raises(BrowserError, match="press needs text"):
        await session.act(Act(action="press"))
