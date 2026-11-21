"""Vision mode of the browser (roadmap M5.2, docs/architecture.md Section 14.1).

``PlaywrightSession`` acts at points on a canvas app with a local headless Chromium: the page
has no accessibility tree, so the task can be done only with a screenshot and points. The tests
skip when the Chromium headless shell is not installed
(``uv run playwright install --only-shell chromium``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import pytest

from sama.browser.cdp import PlaywrightSession, jpeg_size
from sama.browser.service import (
    VISION_HINT,
    BrowserService,
    check_act,
    render_screenshot,
    render_snapshot,
)
from sama.browser.target import (
    Act,
    BrowserError,
    PageState,
    Screenshot,
    Snapshot,
    has_vision,
)
from tests.browser.canvas_app import (
    BOX,
    CIRCLES,
    DIAMOND,
    GAUGE,
    HOVER,
    SOLVED_TITLE,
    SQUARE,
    TEXT_BOX,
    Point,
    app,
)
from tests.browser.fakes import AllowAll, FakeTarget
from tests.conformance.scripted_model import serve
from tests.support import make_claims

if TYPE_CHECKING:
    from playwright.async_api import Browser


@pytest.fixture(scope="module")
def canvas() -> Iterator[str]:
    with serve(app()) as url:
        yield url + "/"


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


def at(action: str, point: Point, **fields: object) -> Act:
    """An action at a point."""
    return Act(action=action, x=float(point[0]), y=float(point[1]), **fields)  # type: ignore[arg-type]


async def solve(session: PlaywrightSession) -> PageState:
    """Does the six steps of the canvas app at the known points."""
    for number in (1, 2, 3):
        state = await session.act(at("click", CIRCLES[number]))
    assert state.title == "Canvas quest 1/6"
    state = await session.act(at("click", DIAMOND, clicks=2))
    assert state.title == "Canvas quest 2/6"
    state = await session.act(at("hover", HOVER))
    assert state.title == "Canvas quest 3/6"
    state = await session.act(at("drag", SQUARE, to_x=float(BOX[0]), to_y=float(BOX[1])))
    assert state.title == "Canvas quest 4/6"
    state = await session.act(at("type", TEXT_BOX, text="sama", submit=True))
    assert state.title == "Canvas quest 5/6"
    return await session.act(at("scroll", GAUGE, amount=700))


async def test_the_canvas_app_has_no_refs_and_the_screenshot_is_the_viewport(
    session: PlaywrightSession, canvas: str
) -> None:
    state = await session.navigate(canvas)
    assert state.title == "Canvas quest 0/6"
    snapshot = await session.snapshot()
    assert "[ref=" not in snapshot.tree
    assert VISION_HINT in render_snapshot(snapshot)
    shot = await session.screenshot()
    assert shot.data[:3] == b"\xff\xd8\xff"
    # One pixel of the image is one CSS pixel of the viewport.
    assert (shot.width, shot.height) == (1280, 720)
    assert jpeg_size(shot.data) == (1280, 720)
    assert not shot.full_page
    full = await session.screenshot(full_page=True)
    assert full.full_page
    assert full.width == 1280


async def test_the_canvas_task_is_done_only_with_points(
    session: PlaywrightSession, canvas: str
) -> None:
    await session.navigate(canvas)
    before = await session.screenshot()
    state = await solve(session)
    assert state.title == SOLVED_TITLE
    after = await session.screenshot()
    assert after.data != before.data


async def test_a_wrong_order_starts_the_circles_again(
    session: PlaywrightSession, canvas: str
) -> None:
    await session.navigate(canvas)
    await session.act(at("click", CIRCLES[2]))
    for number in (1, 2, 3):
        state = await session.act(at("click", CIRCLES[number]))
    assert state.title == "Canvas quest 1/6"


async def test_a_point_outside_the_viewport_fails_before_the_action(
    session: PlaywrightSession, canvas: str
) -> None:
    await session.navigate(canvas)
    with pytest.raises(BrowserError, match=r"outside the viewport \(1280x720\)"):
        await session.act(Act(action="click", x=1280, y=10))
    with pytest.raises(BrowserError, match="outside the viewport"):
        await session.act(Act(action="drag", x=110, y=380, to_x=10, to_y=5000))
    # The failed drag did not hold the mouse button down.
    state = await session.act(at("click", CIRCLES[1]))
    assert state.title == "Canvas quest 0/6"


async def test_points_work_on_a_page_with_a_dom(session: PlaywrightSession, canvas: str) -> None:
    page = (
        "data:text/html,<title>Form</title><input id=q style='position:absolute;left:0;top:0;"
        "width:200px;height:30px'><button style='position:absolute;left:0;top:100px;width:100px;"
        "height:40px' onclick=\"document.title='clicked '+document.getElementById('q').value\">"
        "Go</button>"
    )
    await session.navigate(page)
    await session.act(Act(action="type", x=100, y=15, text="rooms"))
    state = await session.act(Act(action="click", x=50, y=120))
    assert state.title == "clicked rooms"


def test_the_cdp_session_has_vision_mode() -> None:
    assert has_vision(PlaywrightSession)
    assert not has_vision(object())


@pytest.mark.parametrize(
    ("act", "message"),
    [
        (Act(action="click", x=1), "needs both x and y"),
        (Act(action="drag", x=1, y=1, to_x=2), "needs both to_x and to_y"),
        (Act(action="select", x=1, y=1, values=("a",)), "x and y work only with"),
        (Act(action="press", x=1, y=1, text="Enter"), "x and y work only with"),
        (Act(action="click", ref="e1", x=1, y=1), "not both"),
        (Act(action="click", ref="e1", to_x=1, to_y=1), "to_x and to_y work only with drag"),
        (Act(action="drag", x=1, y=1), "needs to_x and to_y"),
        (Act(action="drag", x=1, y=1, target_ref="e2", to_x=1, to_y=1), "not target_ref"),
        (Act(action="drag", ref="e1"), "needs target_ref"),
        (Act(action="hover", clicks=2, ref="e1"), "clicks works only with click"),
        (Act(action="click"), "or x and y"),
        (Act(action="select", values=("a",)), "select needs ref"),
    ],
)
def test_check_act_refuses_arguments_that_do_not_fit(act: Act, message: str) -> None:
    with pytest.raises(BrowserError, match=message):
        check_act(act)


@pytest.mark.parametrize(
    "act",
    [
        Act(action="click", x=0, y=0, clicks=2),
        Act(action="type", x=5, y=5, text="hi", submit=True),
        Act(action="hover", x=5, y=5),
        Act(action="drag", x=5, y=5, to_x=50, to_y=50),
        Act(action="scroll", x=5, y=5),
        Act(action="scroll"),
        Act(action="drag", ref="e1", target_ref="e2"),
        Act(action="press", text="Enter"),
    ],
)
def test_check_act_allows_points_and_refs(act: Act) -> None:
    check_act(act)


async def test_the_service_refuses_a_point_on_a_session_without_vision() -> None:
    target = FakeTarget()
    service = BrowserService(target, AllowAll())
    try:
        with pytest.raises(BrowserError, match="cannot act at a point"):
            await service.act(make_claims("run-v"), Act(action="click", x=1, y=1))
        assert target.sessions[0].calls == []
        target.sessions[0].vision = True
        await service.act(make_claims("run-v"), Act(action="click", x=1, y=1))
        assert target.sessions[0].calls[0] == "act click None at (1, 1)"
    finally:
        await service.aclose()


def test_the_vision_hint_is_only_for_a_page_with_few_refs() -> None:
    page = PageState(url="https://example.com/", title="Example", tab=0)
    few = render_snapshot(Snapshot(page=page, tree='- img "board" [ref=e1]'))
    many = render_snapshot(
        Snapshot(page=page, tree="\n".join(f"- button [ref=e{n}]" for n in range(5)))
    )
    blank = render_snapshot(Snapshot(page=PageState(url="about:blank", title="", tab=0), tree=""))
    assert VISION_HINT in few
    assert VISION_HINT not in many
    assert VISION_HINT not in blank
    # The hint is fixed text of sama, outside the untrusted block.
    assert few.index(VISION_HINT) < few.index("<untrusted_content")


def test_jpeg_size_reads_the_frame_header() -> None:
    assert jpeg_size(b"not a jpeg") == (0, 0)
    assert jpeg_size(b"\xff\xd8\xff\xd9") == (0, 0)


def test_the_screenshot_caption_gives_the_size_and_the_range_of_points() -> None:
    page = PageState(url="https://example.com/", title="Example", tab=0)
    viewport = render_screenshot(
        Screenshot(page=page, data=b"x", mime_type="image/jpeg", width=1024, height=768)
    )
    full = render_screenshot(
        Screenshot(
            page=page, data=b"x", mime_type="image/jpeg", width=1024, height=3000, full_page=True
        )
    )
    unknown = render_screenshot(Screenshot(page=page, data=b"x", mime_type="image/png"))
    assert "The image is 1024x768 pixels. It is the viewport" in viewport
    assert "x (0 to 1023) and y (0 to 767)" in viewport
    assert "It is the full page" in full
    assert "full_page=false" in full
    assert "The image is" not in unknown
