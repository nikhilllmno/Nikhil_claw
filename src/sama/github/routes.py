"""The public pages of the GitHub App setup (docs/architecture.md, Section 15.1).

GitHub sends a browser to these pages, so they have no sama sign-in:

- ``/github/app/created``: the redirect of the manifest flow. It shows the one-time code that
  ``python -m sama.admin github-app convert`` changes into the App credentials. The page does
  not convert the code itself: the credentials go to the operator, not to the process.
- ``/github/setup``: the callback after an install. The signed ``state`` names the org. The
  page shows the sama org and the GitHub account, and a button to confirm. The confirmation
  (a POST) uses the OAuth ``code`` to check that the person owns the GitHub account (the user,
  or an owner of the organization), then connects the installation.
"""

from __future__ import annotations

import html
import logging
import re
import shlex
from typing import Annotated, Final
from urllib.parse import parse_qs

from fastapi import APIRouter, Query, Request
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
_CODE: Final = re.compile(r"[A-Za-z0-9_-]{1,200}")
"""The characters of a manifest code of GitHub."""


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
        if not _CODE.fullmatch(code):
            # The page shows a shell command: a code with other characters could add a
            # second command for the operator to run.
            return page("The code is not valid", _p("Create the App again from sama."), 400)
        command = f"python -m sama.admin github-app convert {shlex.quote(code)}"
        body = (
            _p("GitHub created the App. Run this command on a trusted computer in the next hour:")
            + f"<pre>{html.escape(command)}</pre>"
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
        """Shows what the install connects, with a button to confirm. Only the confirmation
        (a POST) connects the installation."""
        if service is None:
            return _off()
        if setup_action == "request":
            return page(
                "An owner must approve the App",
                _p(
                    "GitHub sent your request to an owner of the GitHub account. After the "
                    "owner approves the install, start the install again from sama."
                ),
            )
        if installation_id is None or not state or not code:
            return _start_from_sama()
        try:
            preview = await service.preview_install(state, installation_id)
        except GitHubSetupError as error:
            return _refused(error, installation_id)
        fields = "".join(
            f'<input type="hidden" name="{name}" value="{html.escape(value)}">'
            for name, value in (
                ("installation_id", str(installation_id)),
                ("code", code),
                ("state", state),
            )
        )
        body = (
            _p(f"Connect the GitHub account {preview.account} to the sama org {preview.org_name}?")
            + _p(
                f"The agents of {preview.org_name} can then work in the repositories of "
                f"{preview.account} that you gave to the App and that an org admin adds to "
                "the allowlist. Do not continue if you do not know this sama org."
            )
            + f'<form method="post" action="/github/setup">{fields}'
            + '<button type="submit">Connect</button></form>'
        )
        return page("Confirm the GitHub connection", body)

    @router.post("/github/setup", include_in_schema=False)
    async def confirm(request: Request) -> HTMLResponse:
        if service is None:
            return _off()
        raw = await request.body()
        if len(raw) > 4096:
            return _start_from_sama()
        form = parse_qs(raw.decode("utf-8", "replace"), max_num_fields=10)
        installation = _one(form, "installation_id")
        code = _one(form, "code")
        state = _one(form, "state")
        if not installation.isdigit() or not code or not state or len(code) > 200:
            return _start_from_sama()
        installation_id = int(installation)
        try:
            linked = await service.finish_install(state, installation_id, code)
        except GitHubSetupError as error:
            return _refused(error, installation_id)
        body = _p(
            f"Sama can now use the repositories of {linked['account']} that you gave to the "
            "App. An org admin must add each repository to the allowlist of the org."
        ) + _p("You can close this page.")
        return page("GitHub is connected", body)

    return router


def _one(form: dict[str, list[str]], name: str) -> str:
    values = form.get(name, [])
    return values[0] if len(values) == 1 else ""


def _off() -> HTMLResponse:
    return page("GitHub is off", _p("The GitHub App is off in this process."), 409)


def _start_from_sama() -> HTMLResponse:
    return page(
        "Start the install from sama",
        _p("This page needs the install link of sama. An org admin gets the link from sama."),
        400,
    )


def _refused(error: GitHubSetupError, installation_id: int) -> HTMLResponse:
    log.info(
        "github install refused",
        extra={"installation_id": installation_id, "status": error.status_code},
    )
    return page("The install did not complete", _p(str(error)), error.status_code)
