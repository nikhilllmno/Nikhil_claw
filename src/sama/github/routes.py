"""The public pages of the GitHub App setup (docs/architecture.md, Section 15.1).

GitHub sends a browser to these pages, so they have no sama sign-in:

- ``/github/app/created``: the redirect of the manifest flow. It shows the one-time code that
  ``python -m sama.admin github-app convert`` changes into the App credentials. The page does
  not convert the code itself: the credentials go to the operator, not to the process.
- ``/github/setup``: the callback after an install. The signed ``state`` names the org, and the
  OAuth ``code`` proves that the person can see the installation.
"""

from __future__ import annotations

import html
import logging
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from sama.github.service import GitHubService, GitHubSetupError

log = logging.getLogger("sama.github")

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>sama: GitHub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"></head>
<body><h1>{title}</h1>{body}</body></html>"""
"""A plain page: the content security policy of the API blocks inline styles."""

_NO_STORE = {"cache-control": "no-store"}


def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """An HTML page. ``body`` must be HTML that is already escaped."""
    return HTMLResponse(
        _PAGE.format(title=html.escape(title), body=body),
        status_code=status_code,
        headers=_NO_STORE,
    )


def _p(text: str) -> str:
    return f"<p>{html.escape(text)}</p>"


def setup_router(service: GitHubService | None) -> APIRouter:
    router = APIRouter()

    @router.get("/github/app/created", include_in_schema=False)
    async def app_created(
        code: Annotated[str, Query(min_length=1, max_length=200)],
    ) -> HTMLResponse:
        body = (
            _p("GitHub created the App. Run this command on a trusted computer in the next hour:")
            + "<pre>python -m sama.admin github-app convert "
            + html.escape(code)
            + "</pre>"
            + _p("The command writes the credentials of the App and prints the next steps.")
        )
        return page("The GitHub App is ready to convert", body)

    @router.get("/github/setup", include_in_schema=False)
    async def setup(
        installation_id: Annotated[int | None, Query(gt=0)] = None,
        setup_action: Annotated[str, Query(max_length=40)] = "",
        code: Annotated[str, Query(max_length=200)] = "",
        state: Annotated[str, Query(max_length=1000)] = "",
    ) -> HTMLResponse:
        if service is None:
            return page("GitHub is off", _p("The GitHub App is off in this process."), 409)
        if setup_action == "request":
            return page(
                "An owner must approve the App",
                _p(
                    "GitHub sent your request to an owner of the GitHub account. After the "
                    "owner approves the install, start the install again from sama."
                ),
            )
        if installation_id is None or not state or not code:
            return page(
                "Start the install from sama",
                _p(
                    "This page needs the install link of sama. An org admin gets the link "
                    "from sama and opens it."
                ),
                400,
            )
        try:
            linked = await service.finish_install(state, installation_id, code)
        except GitHubSetupError as error:
            log.info(
                "github install refused",
                extra={"installation_id": installation_id, "status": error.status_code},
            )
            return page("The install did not complete", _p(str(error)), error.status_code)
        body = _p(
            f"Sama can now use the repositories of {linked['account']} that you gave to the "
            "App. An org admin must add each repository to the allowlist of the org."
        ) + _p("You can close this page.")
        return page("GitHub is connected", body)

    return router
