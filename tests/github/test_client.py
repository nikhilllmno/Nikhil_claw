"""The GitHub REST client of the sama GitHub App, with a fake GitHub (Section 15.1)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from sama.github.client import (
    APP_PERMISSIONS,
    GitHubAppConfig,
    GitHubClient,
    GitHubError,
    app_manifest,
    convert_manifest,
)

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC = _KEY.public_key()


def config() -> GitHubAppConfig:
    return GitHubAppConfig(
        app_id="123",
        slug="sama-test",
        client_id="Iv1.client",
        private_key=SecretStr(PEM),
        client_secret=SecretStr("client-secret-value"),
        api_url="https://api.example.test",
        web_url="https://web.example.test",
    )


Handler = Callable[[httpx.Request], httpx.Response]


class Fake:
    """A fake GitHub. ``routes`` maps ``METHOD path`` to a handler."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.routes: dict[str, Handler] = {}
        self.tokens = 0
        self.now = 1_000_000.0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = f"{request.method} {request.url.path}"
        if key in self.routes:
            return self.routes[key](request)
        if request.method == "POST" and request.url.path.endswith("/access_tokens"):
            self.tokens += 1
            return httpx.Response(201, json={"token": f"ghs_token{self.tokens}"})
        return httpx.Response(404, json={"message": "Not Found"})

    def client(self) -> GitHubClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return GitHubClient(config(), http=http, clock=lambda: self.now)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.calls]


@pytest.fixture
def fake() -> Fake:
    return Fake()


def test_the_app_jwt_is_signed_with_the_key(fake: Fake) -> None:
    token = fake.client().app_jwt()
    claims = jwt.decode(token, PUBLIC, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "Iv1.client"
    assert claims["exp"] - claims["iat"] <= 600


def test_the_config_hides_the_secrets() -> None:
    text = repr(config())
    assert "client-secret-value" not in text
    assert "PRIVATE KEY" not in text
    assert config().secret_values() == [PEM, "client-secret-value"]
    assert config().install_url("a b") == (
        "https://web.example.test/apps/sama-test/installations/new?state=a%20b"
    )


async def test_the_installation_token_is_cached_until_it_is_near_its_end(fake: Fake) -> None:
    client = fake.client()
    first = await client.installation_token(7)
    assert await client.installation_token(7) == first
    assert fake.tokens == 1
    request = fake.calls[0]
    assert request.url.path == "/app/installations/7/access_tokens"
    assert request.headers["authorization"].startswith("Bearer ey")
    fake.now += 3600 - 299
    assert await client.installation_token(7) != first
    assert fake.tokens == 2


async def test_a_401_forgets_the_token(fake: Fake) -> None:
    fake.routes["GET /repos/acme/demo"] = lambda _: httpx.Response(401, json={"message": "Bad"})
    client = fake.client()
    with pytest.raises(GitHubError) as caught:
        await client.repo(7, "acme", "demo")
    assert caught.value.status_code == 401
    assert "ghs_token1" not in str(caught.value)
    fake.routes.pop("GET /repos/acme/demo")
    with pytest.raises(GitHubError):
        await client.repo(7, "acme", "demo")
    assert fake.tokens == 2


async def test_the_installation_of_a_repository(fake: Fake) -> None:
    fake.routes["GET /repos/acme/demo/installation"] = lambda _: httpx.Response(
        200, json={"id": 42}
    )
    client = fake.client()
    assert await client.repo_installation("acme", "demo") == 42
    assert await client.repo_installation("acme", "other") is None
    fake.routes["GET /repos/acme/demo/installation"] = lambda _: httpx.Response(500, json={})
    with pytest.raises(GitHubError):
        await client.repo_installation("acme", "demo")


async def test_the_installations_of_the_user(fake: Fake) -> None:
    def token(request: httpx.Request) -> httpx.Response:
        form = dict(item.split("=") for item in request.content.decode().split("&"))
        assert form == {
            "client_id": "Iv1.client",
            "client_secret": "client-secret-value",
            "code": "c",
        }
        return httpx.Response(200, json={"access_token": "ghu_user"})

    def installations(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ghu_user"
        page = int(request.url.params["page"])
        count = 100 if page == 1 else 3
        items = [{"id": page * 1000 + i} for i in range(count)]
        return httpx.Response(200, json={"installations": items})

    def membership(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ghu_user"
        return httpx.Response(200, json={"state": "active", "role": "admin"})

    fake.routes["POST /login/oauth/access_token"] = token
    fake.routes["GET /user"] = lambda _: httpx.Response(200, json={"login": "octo"})
    fake.routes["GET /user/installations"] = installations
    fake.routes["GET /user/memberships/orgs/acme"] = membership
    access = await fake.client().user_access("c")
    assert access.login == "octo"
    assert len(access.installation_ids) == 103
    assert 2002 in access.installation_ids
    assert access.org_admin is False
    assert (await fake.client().user_access("c", org="acme")).org_admin is True


@pytest.mark.parametrize(
    ("status", "body", "admin"),
    [
        (200, {"state": "active", "role": "member"}, False),
        (200, {"state": "pending", "role": "admin"}, False),
        (404, {"message": "Not Found"}, False),
        (403, {"message": "Forbidden"}, False),
    ],
)
async def test_only_an_active_org_admin_counts(
    fake: Fake, status: int, body: dict[str, str], admin: bool
) -> None:
    fake.routes["POST /login/oauth/access_token"] = lambda _: httpx.Response(
        200, json={"access_token": "ghu_user"}
    )
    fake.routes["GET /user"] = lambda _: httpx.Response(200, json={"login": "octo"})
    fake.routes["GET /user/installations"] = lambda _: httpx.Response(
        200, json={"installations": [{"id": 1}]}
    )
    fake.routes["GET /user/memberships/orgs/acme"] = lambda _: httpx.Response(status, json=body)
    assert (await fake.client().user_access("c", org="acme")).org_admin is admin


async def test_a_bad_oauth_code_is_refused(fake: Fake) -> None:
    fake.routes["POST /login/oauth/access_token"] = lambda _: httpx.Response(
        200, json={"error": "bad_verification_code"}
    )
    with pytest.raises(GitHubError) as caught:
        await fake.client().user_access("bad")
    assert caught.value.status_code == 400


async def test_a_pull_request_and_a_comment(fake: Fake) -> None:
    seen: dict[str, Any] = {}

    def pulls(request: httpx.Request) -> httpx.Response:
        seen["pull"] = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer ghs_token1"
        return httpx.Response(201, json={"number": 5, "html_url": "https://x/5"})

    def comments(request: httpx.Request) -> httpx.Response:
        seen["comment"] = json.loads(request.content)
        return httpx.Response(201, json={"html_url": "https://x/5#c"})

    fake.routes["POST /repos/acme/demo/pulls"] = pulls
    fake.routes["POST /repos/acme/demo/issues/5/comments"] = comments
    client = fake.client()
    pull = await client.create_pull(
        7, "acme", "demo", head="sama/x", base="main", title="T", body="B", draft=True
    )
    assert pull["number"] == 5
    assert seen["pull"] == {
        "head": "sama/x",
        "base": "main",
        "title": "T",
        "body": "B",
        "draft": True,
    }
    await client.create_comment(7, "acme", "demo", 5, "hi")
    assert seen["comment"] == {"body": "hi"}


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"status": "ahead"}, True),
        (200, {"status": "identical"}, True),
        (200, {"status": "diverged"}, False),
        (200, {"status": "behind"}, False),
        (404, {"message": "No common ancestor"}, False),
    ],
)
async def test_is_ancestor(fake: Fake, status: int, body: dict[str, Any], expected: bool) -> None:
    path = "GET /repos/acme/demo/compare/" + "a" * 40 + "..." + "b" * 40
    fake.routes[path] = lambda _: httpx.Response(status, json=body)
    assert await fake.client().is_ancestor(7, "acme", "demo", "a" * 40, "b" * 40) is expected


async def test_an_error_names_the_status_and_the_message(fake: Fake) -> None:
    fake.routes["POST /repos/acme/demo/pulls"] = lambda _: httpx.Response(
        422, json={"message": "Validation Failed", "errors": [{"message": "No commits"}]}
    )
    with pytest.raises(GitHubError) as caught:
        await fake.client().create_pull(
            7, "acme", "demo", head="sama/x", base="main", title="T", body="", draft=False
        )
    assert str(caught.value) == "GitHub answered 422: Validation Failed: No commits"
    assert caught.value.status_code == 422


async def test_a_network_error_has_no_token() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    client = GitHubClient(config(), http=http)
    with pytest.raises(GitHubError) as caught:
        await client.installation(1)
    assert str(caught.value) == "GitHub cannot be reached: ConnectError"
    with pytest.raises(GitHubError):
        await client.user_access("c")


def test_the_manifest_asks_for_the_least_permissions() -> None:
    manifest = app_manifest("https://sama.example.test/")
    assert manifest["default_permissions"] == APP_PERMISSIONS
    assert APP_PERMISSIONS == {
        "contents": "write",
        "pull_requests": "write",
        "metadata": "read",
        "members": "read",
    }
    assert "administration" not in manifest["default_permissions"]
    assert "workflows" not in manifest["default_permissions"]
    assert manifest["public"] is True
    assert manifest["redirect_url"] == "https://sama.example.test/github/app/created"
    assert manifest["callback_urls"] == ["https://sama.example.test/github/setup"]
    assert manifest["request_oauth_on_install"] is True
    assert manifest["hook_attributes"]["active"] is False
    assert manifest["default_events"] == []


async def test_convert_manifest(fake: Fake) -> None:
    fake.routes["POST /app-manifests/abc/conversions"] = lambda _: httpx.Response(
        201, json={"id": 1, "slug": "sama", "pem": "PEM"}
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    body = await convert_manifest("abc", http=http, api_url="https://api.example.test")
    assert body["slug"] == "sama"
    with pytest.raises(GitHubError) as caught:
        await convert_manifest("used", http=http, api_url="https://api.example.test")
    assert caught.value.status_code == 404
