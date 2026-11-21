"""A canvas app for the vision mode tests (roadmap M5.2, docs/architecture.md Section 14.1).

The page draws everything on one ``<canvas>``, so its accessibility tree has no elements and no
refs. An agent can do the task only with a screenshot and actions at points:

1. click the circles 1, 2, and 3 in order (a wrong circle starts the order again),
2. double-click the blue diamond,
3. hover over the yellow "hover" circle,
4. drag the red square into the dashed box,
5. click the text box, type ``sama``, and press Enter,
6. scroll the mouse wheel down over the gauge until it is full.

The title is ``Canvas quest N/6`` (the number of steps done) and ``Canvas quest - solved`` at the
end, and the canvas shows ``SOLVED``. ``?seed=N`` (N > 0) moves the items, so a model cannot
know the points before it looks.

Run ``uv run python -m tests.browser.canvas_app --port 8765`` to serve the page for a live test.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Final

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

STEPS: Final = 6
SOLVED_TITLE: Final = "Canvas quest - solved"
TASK: Final = (
    "The page is a canvas game with no buttons or links. Use browser_screenshot and browser_act "
    "with x and y. Do the six steps that the page shows, then tell me the title of the page."
)

Point = tuple[int, int]

CIRCLES: Final[dict[int, Point]] = {1: (120, 130), 2: (420, 100), 3: (270, 230)}
DIAMOND: Final[Point] = (640, 130)
HOVER: Final[Point] = (680, 260)
SQUARE: Final[Point] = (110, 380)
BOX: Final[Point] = (600, 380)
TEXT_BOX: Final[Point] = (410, 320)
GAUGE: Final[Point] = (400, 495)
"""The centres of the items without a seed, in CSS pixels from the top left of the page. The
canvas is at the top left corner of the page. The page script has the same values."""

PAGE: Final = (Path(__file__).parent / "canvas_app.html").read_text(encoding="utf-8")
"""The page. Its script has the same points as the constants above."""


async def _page(request: Request) -> Response:
    return HTMLResponse(PAGE)


def app() -> Starlette:
    """The canvas app at ``/`` (and at ``/canvas``)."""
    return Starlette(routes=[Route("/", _page), Route("/canvas", _page)])


def main() -> None:  # pragma: no cover - a helper for live tests
    import uvicorn

    parser = argparse.ArgumentParser(description="Serve the canvas app of the vision tests.")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - a test helper
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(app(), host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
