"""``python -m sama.admin github-app manifest|convert`` (docs/architecture.md, Section 15.1).

A person creates the sama GitHub App with the manifest flow of GitHub:

1. ``manifest`` writes an HTML form. A person opens it in a browser that is signed in to
   GitHub and clicks the button. GitHub shows the App and creates it.
2. GitHub sends the browser to ``<public_url>/github/app/created?code=...``.
3. ``convert <code>`` changes the code into the App credentials. It writes the secret values to
   files with mode ``0600`` and prints the ``flyctl secrets set`` command. The command reads the
   files, so the values are not in the output or the shell history.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import secrets
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

import httpx

from sama.github.client import API_URL, WEB_URL, app_manifest, convert_manifest

PRIVATE_KEY_FILE: Final = "github-app-private-key.pem"
CLIENT_SECRET_FILE: Final = "github-app-client-secret.txt"  # noqa: S105 -- a file name


def manifest_form(
    public_url: str, *, org: str | None = None, name: str = "sama", web_url: str = WEB_URL
) -> str:
    """The HTML form that posts the manifest to GitHub. With ``org``, the GitHub organization
    owns the App. Without it, the personal account of the person owns the App."""
    target = (
        f"{web_url}/organizations/{quote(org, safe='')}/settings/apps/new"
        if org
        else f"{web_url}/settings/apps/new"
    )
    state = secrets.token_urlsafe(16)
    manifest = json.dumps(app_manifest(public_url, name=name), indent=2)
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        "<title>Create the sama GitHub App</title></head><body>\n"
        "<h1>Create the sama GitHub App</h1>\n"
        "<p>Sign in to GitHub in this browser. Then click the button.</p>\n"
        f'<form action="{html.escape(target)}?state={html.escape(state)}" method="post">\n'
        f'<input type="hidden" name="manifest" value="{html.escape(manifest)}">\n'
        '<button type="submit">Create the App on GitHub</button>\n</form>\n'
        f"<pre>{html.escape(manifest)}</pre>\n</body></html>\n"
    )


def _write_secret(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
    os.chmod(path, 0o600)


def convert(
    code: str,
    out_dir: Path,
    *,
    fly_app: str = "sama-staging",
    http: httpx.AsyncClient | None = None,
    api_url: str = API_URL,
) -> dict[str, Any]:
    """Converts the code and writes the secrets. The result has no secret value."""
    body = asyncio.run(convert_manifest(code, http=http, api_url=api_url))
    pem = str(body.get("pem") or "")
    client_secret = str(body.get("client_secret") or "")
    app_id = str(body.get("id") or "")
    slug = str(body.get("slug") or "")
    client_id = str(body.get("client_id") or "")
    if not (pem and client_secret and app_id and slug and client_id):
        raise ValueError("GitHub gave an incomplete App: run the manifest flow again")
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = out_dir / PRIVATE_KEY_FILE
    secret_path = out_dir / CLIENT_SECRET_FILE
    _write_secret(key_path, pem)
    _write_secret(secret_path, client_secret)
    command = (
        f"flyctl secrets set -a {fly_app} SAMA_GITHUB_APP_ID={app_id} "
        f"SAMA_GITHUB_APP_SLUG={slug} SAMA_GITHUB_CLIENT_ID={client_id} "
        f'SAMA_GITHUB_APP_PRIVATE_KEY="$(cat {key_path})" '
        f'SAMA_GITHUB_CLIENT_SECRET="$(cat {secret_path})"'
    )
    return {
        "app_id": app_id,
        "slug": slug,
        "client_id": client_id,
        "html_url": str(body.get("html_url") or ""),
        "private_key_file": str(key_path),
        "client_secret_file": str(secret_path),
        "next": [
            command,
            f"Delete the two files after the secrets are set: rm {key_path} {secret_path}",
            "In the App settings on GitHub, keep the webhook off.",
        ],
    }
