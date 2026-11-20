"""The sama GitHub App and its REST client (docs/architecture.md, Section 15.1).

One GitHub App serves one sama deployment. The operator creates it from the manifest of
``app_manifest``. Each org installs the App on its GitHub account. Only the control plane
holds the private key of the App and the installation tokens that it makes. No token goes to
a sandbox, a prompt, or a log.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote

import httpx
import jwt
from pydantic import SecretStr

API_URL: Final = "https://api.github.com"
WEB_URL: Final = "https://github.com"
API_VERSION: Final = "2022-11-28"
TIMEOUT_S: Final = 20.0
TOKEN_MARGIN_S: Final = 300
"""An installation token lives 1 hour. Sama makes a new one 5 minutes before the end."""
MAX_INSTALLATION_PAGES: Final = 10

APP_PERMISSIONS: Final[Mapping[str, str]] = {
    "contents": "write",
    "pull_requests": "write",
    "metadata": "read",
}
"""The App has no ``workflows`` permission, so GitHub also refuses a push that changes a file
in ``.github/workflows``. It has no ``administration`` permission, so it cannot change a
branch rule or a ruleset."""


class GitHubError(Exception):
    """A GitHub call failed. The message has no token."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class GitHubAppConfig:
    """The identity of the sama GitHub App (``SAMA_GITHUB_*`` settings, Section 19.2)."""

    app_id: str
    slug: str
    client_id: str
    private_key: SecretStr = field(repr=False)
    client_secret: SecretStr = field(repr=False)
    api_url: str = API_URL
    web_url: str = WEB_URL

    def secret_values(self) -> list[str]:
        return [self.private_key.get_secret_value(), self.client_secret.get_secret_value()]

    def install_url(self, state: str) -> str:
        return f"{self.web_url}/apps/{quote(self.slug)}/installations/new?state={quote(state)}"


def app_manifest(public_url: str, *, name: str = "sama") -> dict[str, Any]:
    """The manifest of the sama GitHub App, for the manifest flow of GitHub.

    The webhook is off: sama does not need events. ``request_oauth_on_install`` makes GitHub
    send the person who installs the App to ``/github/setup`` with an OAuth code, so sama can
    check that this person can see the installation.
    """
    base = public_url.rstrip("/")
    return {
        "name": name,
        "url": base,
        "description": "Sama clones repositories, pushes sama/* branches, and opens pull "
        "requests for the agents of an org.",
        "public": False,
        "redirect_url": f"{base}/github/app/created",
        "callback_urls": [f"{base}/github/setup"],
        "request_oauth_on_install": True,
        "setup_on_update": True,
        "hook_attributes": {"url": f"{base}/github/webhook", "active": False},
        "default_permissions": dict(APP_PERMISSIONS),
        "default_events": [],
    }


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    expires_at: float


class GitHubClient:
    """Calls the GitHub REST API as the App, as an installation, or with an OAuth code."""

    def __init__(
        self,
        config: GitHubAppConfig,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False)
        self._clock = clock
        self._tokens: dict[int, _Token] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- credentials ------------------------------------------------------------------------

    def app_jwt(self) -> str:
        """A JWT that signs in as the App. It lives 9 minutes."""
        now = int(self._clock())
        claims = {"iat": now - 60, "exp": now + 540, "iss": self.config.client_id}
        return jwt.encode(claims, self.config.private_key.get_secret_value(), algorithm="RS256")

    async def installation_token(self, installation_id: int) -> str:
        """A token of the installation, from the cache when it has more than 5 minutes left."""
        cached = self._tokens.get(installation_id)
        if cached is not None and cached.expires_at - TOKEN_MARGIN_S > self._clock():
            return cached.value
        lock = self._locks.setdefault(installation_id, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(installation_id)
            if cached is not None and cached.expires_at - TOKEN_MARGIN_S > self._clock():
                return cached.value
            body = await self._json(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                auth=f"Bearer {self.app_jwt()}",
            )
            token = str(body.get("token") or "")
            if not token:
                raise GitHubError("GitHub gave no installation token")
            # GitHub gives the end time in ISO 8601. One hour is the documented life.
            self._tokens[installation_id] = _Token(token, self._clock() + 3600)
            return token

    def forget(self, installation_id: int) -> None:
        self._tokens.pop(installation_id, None)

    # -- the App ----------------------------------------------------------------------------

    async def installation(self, installation_id: int) -> dict[str, Any]:
        return await self._json(
            "GET", f"/app/installations/{installation_id}", auth=f"Bearer {self.app_jwt()}"
        )

    async def repo_installation(self, owner: str, name: str) -> int | None:
        """The id of the installation of the App on the repository, or ``None``."""
        try:
            body = await self._json(
                "GET", f"/repos/{_path(owner)}/{_path(name)}/installation", auth=self._app()
            )
        except GitHubError as error:
            if error.status_code == 404:
                return None
            raise
        return int(body["id"])

    def _app(self) -> str:
        return f"Bearer {self.app_jwt()}"

    async def user_installation_ids(self, code: str) -> set[int]:
        """The installations of the App that the person who gave the OAuth code can see."""
        response = await self._http.post(
            f"{self.config.web_url}/login/oauth/access_token",
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret.get_secret_value(),
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        body = _body(response)
        token = str(body.get("access_token") or "") if response.status_code == 200 else ""
        if not token:
            raise GitHubError("GitHub did not accept the sign-in code", 400)
        found: set[int] = set()
        for page in range(1, MAX_INSTALLATION_PAGES + 1):
            listed = await self._json(
                "GET",
                f"/user/installations?per_page=100&page={page}",
                auth=f"Bearer {token}",
            )
            items = listed.get("installations") or []
            found.update(int(item["id"]) for item in items)
            if len(items) < 100:
                break
        return found

    # -- an installation --------------------------------------------------------------------

    async def repo(self, installation_id: int, owner: str, name: str) -> dict[str, Any]:
        return await self._as(installation_id, "GET", f"/repos/{_path(owner)}/{_path(name)}")

    async def create_pull(
        self,
        installation_id: int,
        owner: str,
        name: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool,
    ) -> dict[str, Any]:
        return await self._as(
            installation_id,
            "POST",
            f"/repos/{_path(owner)}/{_path(name)}/pulls",
            json={"head": head, "base": base, "title": title, "body": body, "draft": draft},
        )

    async def create_comment(
        self, installation_id: int, owner: str, name: str, number: int, body: str
    ) -> dict[str, Any]:
        return await self._as(
            installation_id,
            "POST",
            f"/repos/{_path(owner)}/{_path(name)}/issues/{number}/comments",
            json={"body": body},
        )

    async def pull(
        self, installation_id: int, owner: str, name: str, number: int
    ) -> dict[str, Any]:
        return await self._as(
            installation_id, "GET", f"/repos/{_path(owner)}/{_path(name)}/pulls/{number}"
        )

    async def is_ancestor(
        self, installation_id: int, owner: str, name: str, old: str, new: str
    ) -> bool:
        """True if ``old`` is ``new`` or an ancestor of ``new`` on GitHub."""
        try:
            body = await self._as(
                installation_id,
                "GET",
                f"/repos/{_path(owner)}/{_path(name)}/compare/{old}...{new}?per_page=1",
            )
        except GitHubError as error:
            if error.status_code == 404:
                return False
            raise
        return body.get("status") in {"ahead", "identical"}

    async def _as(
        self, installation_id: int, method: str, path: str, json: Any = None
    ) -> dict[str, Any]:
        token = await self.installation_token(installation_id)
        try:
            return await self._json(method, path, auth=f"Bearer {token}", json=json)
        except GitHubError as error:
            if error.status_code == 401:
                self.forget(installation_id)
            raise

    async def _json(
        self, method: str, path: str, *, auth: str | None = None, json: Any = None
    ) -> dict[str, Any]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
        if auth is not None:
            headers["Authorization"] = auth
        try:
            response = await self._http.request(
                method, f"{self.config.api_url}{path}", headers=headers, json=json
            )
        except httpx.HTTPError as error:
            raise GitHubError(f"GitHub cannot be reached: {type(error).__name__}") from None
        body = _body(response)
        if response.status_code >= 400:
            raise GitHubError(_problem(response.status_code, body), response.status_code)
        return body


async def convert_manifest(
    code: str, *, http: httpx.AsyncClient | None = None, api_url: str = API_URL
) -> dict[str, Any]:
    """Finishes the manifest flow: GitHub gives the id, the slug, and the keys of the new App.
    The code works once, for 1 hour."""
    client = http or httpx.AsyncClient(timeout=TIMEOUT_S)
    try:
        response = await client.post(
            f"{api_url}/app-manifests/{quote(code, safe='')}/conversions",
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION},
        )
    finally:
        if http is None:
            await client.aclose()
    body = _body(response)
    if response.status_code >= 400:
        raise GitHubError(_problem(response.status_code, body), response.status_code)
    return body


def _path(segment: str) -> str:
    return quote(segment, safe="")


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {"items": body}


def _problem(status: int, body: Mapping[str, Any]) -> str:
    message = str(body.get("message") or "no message")[:300]
    details = body.get("errors")
    if isinstance(details, list) and details:
        first = details[0]
        extra = first.get("message") if isinstance(first, dict) else str(first)
        if extra:
            message = f"{message}: {str(extra)[:300]}"
    return f"GitHub answered {status}: {message}"
