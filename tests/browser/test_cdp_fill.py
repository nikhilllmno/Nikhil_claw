"""``PlaywrightSession.fill_login`` with a local headless Chromium (Section 14.1, M5.3).

The site serves on ``127.0.0.1``. The same server on ``localhost`` is another site, so a test can
put a login form of another site in a frame. The tests skip when the Chromium headless shell is
not installed (``uv run playwright install --only-shell chromium``).
"""

from __future__ import annotations

import dataclasses
import html
from collections.abc import AsyncIterator, Iterator
from functools import partial
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from sama.browser.cdp import HIDDEN, PlaywrightSession, hide_values
from sama.browser.logins import site_allows
from sama.browser.target import BrowserError, ExtractRequest, FillRequest
from tests.browser.fakes import AllowAll
from tests.browser.test_cdp import ref_of
from tests.conformance.scripted_model import serve

if TYPE_CHECKING:
    from playwright.async_api import Browser

USERNAME = "robot@example.com"
PASSWORD = "hunter2-Secret"

LOGIN = """<!doctype html><title>Sign in</title>
<form method="post" action="/login">
  <label>Search <input name="q"></label>
</form>
<form method="post" action="/login">
  <label>Email <input name="user" autocomplete="username"></label>
  <label>Password <input name="pass" type="password"></label>
  <button type="submit">Sign in</button>
</form>"""
STEP_ONE = """<!doctype html><title>Step one</title>
<form method="post" action="/step-two"><label>Login <input name="user" id="login"></label>
<button>Next</button></form>"""
STEP_TWO = """<!doctype html><title>Step two</title>
<form method="post" action="/login"><input type="hidden" name="user" value="{user}">
<label>Password <input name="pass" type="password"></label><button>Sign in</button></form>"""
SPOOF = """<!doctype html><title>Spoof</title>
<form><label>Password <input id="pw" type="password"></label></form>
<script>
Object.defineProperty(document.getElementById("pw"), "ownerDocument", {
  get: () => ({location: {href: "{site}/login"}}),
});
</script>"""
HOP = """<!doctype html><title>Hop</title>
<form><label>Email <input name="user" autocomplete="username"
  oninput="location.href = '{other}/login'"></label>
<label>Password <input name="pass" type="password"></label></form>"""
GET_FORM = """<!doctype html><title>Get form</title>
<form method="get" action="/plain"><label>Email <input name="user" type="email"></label>
<label>Password <input name="pass" type="password"></label><button>Go</button></form>"""
STEAL = """<!doctype html><title>Steal</title><input id="mine" aria-label="Mine">
<iframe src="{src}" width="600" height="300"></iframe>
<script>setInterval(() => document.getElementById("mine").focus(), 1);</script>"""
FRAMED = """<!doctype html><title>Framed</title><p>An embedded sign-in form:</p>
<iframe src="{src}" width="600" height="300"></iframe>"""


async def _form(request: Request) -> dict[str, str]:
    fields = parse_qs((await request.body()).decode())
    return {key: values[0] for key, values in fields.items()}


def _app() -> Starlette:
    async def login(request: Request) -> Response:
        if request.method == "GET":
            return HTMLResponse(LOGIN)
        form = await _form(request)
        if form.get("user") == USERNAME and form.get("pass") == PASSWORD:
            return HTMLResponse(
                f"<title>Account</title><p>Signed in as {html.escape(form['user'])}</p>"
            )
        return HTMLResponse("<title>Sign in</title><p>Wrong user name or password</p>")

    async def step_one(request: Request) -> Response:
        return HTMLResponse(STEP_ONE)

    async def step_two(request: Request) -> Response:
        user = html.escape((await _form(request)).get("user", ""))
        return HTMLResponse(STEP_TWO.replace("{user}", user))

    async def framed(request: Request) -> Response:
        return HTMLResponse(FRAMED.replace("{src}", request.query_params["src"]))

    async def spoof(request: Request) -> Response:
        return HTMLResponse(SPOOF.replace("{site}", request.query_params["site"]))

    async def hop(request: Request) -> Response:
        return HTMLResponse(HOP.replace("{other}", request.query_params["other"]))

    async def get_form(request: Request) -> Response:
        return HTMLResponse(GET_FORM)

    async def steal(request: Request) -> Response:
        return HTMLResponse(STEAL.replace("{src}", request.query_params["src"]))

    async def plain(request: Request) -> Response:
        return HTMLResponse("<title>Plain</title><p>No form here</p>")

    return Starlette(
        routes=[
            Route("/login", login, methods=["GET", "POST"]),
            Route("/step-one", step_one),
            Route("/step-two", step_two, methods=["POST"]),
            Route("/framed", framed),
            Route("/plain", plain),
            Route("/spoof", spoof),
            Route("/hop", hop),
            Route("/get-form", get_form),
            Route("/steal", steal),
        ]
    )


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    with serve(_app()) as url:
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
async def session(browser: Browser) -> AsyncIterator[PlaywrightSession]:
    session = await PlaywrightSession.attach(browser, AllowAll(), target="cloud")
    yield session
    await session.close()


def fill(site: str, **changes: object) -> FillRequest:
    request = FillRequest(
        username=SecretStr(USERNAME),
        password=SecretStr(PASSWORD),
        site_allows=partial(site_allows, site),
    )
    return dataclasses.replace(request, **changes)  # type: ignore[arg-type]


def other_site(site: str) -> str:
    return site.replace("127.0.0.1", "localhost")


async def test_fill_finds_the_fields_signs_in_and_hides_the_password(
    session: PlaywrightSession, site: str
) -> None:
    await session.navigate(site + "/login")
    filled = await session.fill_login(fill(site))
    assert filled.fields == ("username", "password")
    shot = await session.snapshot()
    assert f": {USERNAME}" in shot.tree
    assert PASSWORD not in shot.tree
    assert any(
        'textbox "Password"' in ln and ln.endswith(f": {HIDDEN}") for ln in shot.tree.splitlines()
    )
    # The search box of the other form stays empty.
    search = [line for line in shot.tree.splitlines() if 'textbox "Search"' in line]
    assert search
    assert not search[0].rstrip().endswith(USERNAME)
    # An extract of the password field gives the mark, not the value.
    extracted = await session.extract(
        ExtractRequest(kind="fields", fields={"password": ref_of(shot.tree, "Password")})
    )
    assert PASSWORD not in extracted.content
    assert HIDDEN in extracted.content
    # The marks of sama are gone from the page.
    assert await session._context.pages[0].locator("[data-sama-login]").count() == 0
    done = await session.fill_login(fill(site, submit=True))
    assert done.page.title == "Account"
    text = await session.extract(ExtractRequest(kind="text"))
    assert f"Signed in as {USERNAME}" in text.content


async def test_a_login_in_two_steps_fills_each_step(session: PlaywrightSession, site: str) -> None:
    await session.navigate(site + "/step-one")
    first = await session.fill_login(fill(site, submit=True))
    assert first.fields == ("username",)
    assert first.page.title == "Step two"
    second = await session.fill_login(fill(site, submit=True))
    assert second.fields == ("password",)
    assert second.page.title == "Account"


async def test_fill_refuses_the_page_of_another_site(session: PlaywrightSession, site: str) -> None:
    await session.navigate(other_site(site) + "/login")
    with pytest.raises(BrowserError, match="no login field of the site"):
        await session.fill_login(fill(site))
    await session.navigate(site + "/plain")
    with pytest.raises(BrowserError, match="no login field of the site"):
        await session.fill_login(fill(site))
    page = session._context.pages[0]
    await session.navigate(other_site(site) + "/login")
    assert await page.locator("input[type=password]").input_value() == ""


async def test_fill_uses_only_the_frames_of_the_site(session: PlaywrightSession, site: str) -> None:
    # The login form of another site in a frame of this site: no fill.
    await session.navigate(f"{site}/framed?src={other_site(site)}/login")
    with pytest.raises(BrowserError, match="no login field of the site"):
        await session.fill_login(fill(site))
    # The login form of the site in a frame of another page: the fill goes to the frame.
    await session.navigate(f"{other_site(site)}/framed?src={site}/login")
    filled = await session.fill_login(fill(site))
    assert filled.fields == ("username", "password")
    frame = session._context.pages[0].frames[1]
    assert await frame.locator("input[type=password]").input_value() == PASSWORD


async def test_refs_pick_the_fields_and_are_checked(session: PlaywrightSession, site: str) -> None:
    await session.navigate(site + "/login")
    tree = (await session.snapshot()).tree
    with pytest.raises(BrowserError, match="password_ref must be a password field"):
        await session.fill_login(fill(site, password_ref=ref_of(tree, "Search")))
    with pytest.raises(BrowserError, match="username_ref must be a text field"):
        await session.fill_login(fill(site, username_ref=ref_of(tree, "Sign in")))
    filled = await session.fill_login(
        fill(site, username_ref=ref_of(tree, "Search"), password_ref=ref_of(tree, "Password"))
    )
    assert filled.fields == ("username", "password")
    page = session._context.pages[0]
    assert await page.locator("input[name=q]").input_value() == USERNAME
    # A ref on the page of another site is refused.
    await session.navigate(other_site(site) + "/login")
    tree = (await session.snapshot()).tree
    with pytest.raises(BrowserError, match="not on the site of this login"):
        await session.fill_login(fill(site, password_ref=ref_of(tree, "Password")))


def test_hide_values_replaces_only_the_value_of_a_text_box() -> None:
    tree = (
        '- textbox "Password" [ref=e2]: abc\n'
        "- text: abc is also here\n"
        '- textbox "Other" [ref=e3]: xabc'
    )
    assert hide_values(tree, ["abc"]) == (
        f'- textbox "Password" [ref=e2]: {HIDDEN}\n- text: abc is also here\n'
        '- textbox "Other" [ref=e3]: xabc'
    )
    assert hide_values(tree, []) == tree


async def test_a_page_cannot_fake_the_document_of_a_field(
    session: PlaywrightSession, site: str
) -> None:
    # A page of another site says that its password field is in a document of the site.
    await session.navigate(f"{other_site(site)}/spoof?site={site}")
    tree = (await session.snapshot()).tree
    with pytest.raises(BrowserError, match="not on the site of this login"):
        await session.fill_login(fill(site, password_ref=ref_of(tree, "Password")))
    page = session._context.pages[0]
    assert await page.locator("#pw").input_value() == ""


async def test_a_page_that_navigates_during_the_fill_does_not_get_the_password(
    session: PlaywrightSession, site: str
) -> None:
    # The page of the site goes to another site when the user name is typed.
    await session.navigate(f"{site}/hop?other={other_site(site)}")
    with pytest.raises(BrowserError):
        await session.fill_login(fill(site))
    page = session._context.pages[0]
    await page.wait_for_url(f"{other_site(site)}/login")
    assert await page.locator("input[type=password]").input_value() == ""


async def test_a_parent_page_that_takes_the_focus_does_not_get_the_password(
    session: PlaywrightSession, site: str
) -> None:
    await session.navigate(f"{other_site(site)}/steal?src={site}/login")
    filled = await session.fill_login(fill(site))
    assert filled.fields == ("username", "password")
    page = session._context.pages[0]
    assert await page.locator("#mine").input_value() == ""
    frame = page.frames[1]
    assert await frame.locator("input[type=password]").input_value() == PASSWORD


@pytest.mark.parametrize("password", ["hunter~secret", "a b+c&d=e/f?g%h", "pässwörd~!*'()"])
async def test_the_scrubber_knows_how_a_browser_encodes_a_get_form(
    session: PlaywrightSession, site: str, password: str
) -> None:
    from sama.browser.service import _forms

    await session.navigate(site + "/get-form")
    await session.fill_login(fill(site, password=SecretStr(password), submit=True))
    url = session._context.pages[0].url
    assert "pass=" in url
    assert any(form in url for form in _forms(password) if form != password), url
