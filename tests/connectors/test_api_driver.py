"""The driver of a custom HTTP API (docs/architecture.md, Section 10.3.3, M4.12).

``OutboundClient`` sends each request to the IP address and names the host in ``Host``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from sama.connectors.api import ApiDriver
from sama.connectors.http_api import DriverError, DriverUnavailableError
from sama.connectors.manifest import ApiSpec
from sama.connectors.openapi import parse_endpoints
from sama.vault.redact import default_redactor, redact
from tests.connectors.fake_mcp import public
from tests.support import wire_transport

HOST = "api.remote.test"
BASE = f"https://{HOST}/v1"
TOKEN_URL = f"https://{HOST}/oauth/token"
CONN = uuid.uuid4()


class FakeApi:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.tokens_issued = 0
        self.valid: set[str] = set()
        self.expires_in: int | None = 3600
        self.prefix = "tok-"
        self.echo_auth = False
        self.error_code = "invalid_client"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth/token":
            form = parse_qs(request.content.decode())
            auth = request.headers.get("authorization", "")
            pair = base64.b64decode(auth.removeprefix("Basic ")).decode() if auth else ""
            body_pair = f"{form.get('client_id', [''])[0]}:{form.get('client_secret', [''])[0]}"
            if form.get("grant_type") != ["client_credentials"] or "cid:csecret" not in (
                pair,
                body_pair,
            ):
                return httpx.Response(401, json={"error": self.error_code, "echo": "csecret"})
            self.tokens_issued += 1
            token = f"{self.prefix}{self.tokens_issued}"
            self.valid = {token}
            answer: dict[str, Any] = {"access_token": token, "token_type": "Bearer"}
            if self.expires_in is not None:
                answer["expires_in"] = self.expires_in
            return httpx.Response(200, json=answer)
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer tok-") and auth.removeprefix("Bearer ") not in self.valid:
            return httpx.Response(401, json={"error": "expired"})
        if self.status != 200:
            echo = f" {auth}" if self.echo_auth else ""
            return httpx.Response(self.status, json={"message": f"nope{echo}"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"path": request.url.path, "ok": True})


def api(auth: dict[str, Any], test: str | None = None) -> ApiSpec:
    return ApiSpec.model_validate({"base_url": BASE, "auth": auth, "test": test})


OPS = {
    op.tool.name: op
    for op in parse_endpoints(
        [
            {"name": "get_item", "method": "GET", "path": "/items/{id}", "query": ["full"]},
            {"name": "list_items", "method": "GET", "path": "/items"},
            {"name": "add_item", "method": "POST", "path": "/items", "body": True},
            {"name": "drop_item", "method": "DELETE", "path": "/items/{id}"},
        ],
        max_name=40,
    ).operations
}


@pytest.fixture
def fake() -> FakeApi:
    return FakeApi()


@pytest.fixture
async def driver(fake: FakeApi) -> Any:
    now = [1000.0]
    made = ApiDriver(transport=wire_transport(fake), resolver=public, clock=lambda: now[0])
    made.now = now  # type: ignore[attr-defined]
    yield made
    await made.aclose()


async def test_each_auth_type_goes_into_the_request(driver: ApiDriver, fake: FakeApi) -> None:
    op = OPS["list_items"]
    await driver.call(CONN, api({"type": "api_key", "name": "X-Key"}), op, {"api_key": "k1"}, {})
    assert fake.requests[-1].headers["x-key"] == "k1"
    assert fake.requests[-1].headers["host"] == HOST
    assert fake.requests[-1].headers["accept"] == "application/json"
    spec = api({"type": "api_key", "name": "key", "location": "query"})
    await driver.call(CONN, spec, op, {"api_key": "k2"}, {})
    assert fake.requests[-1].url.params["key"] == "k2"
    await driver.call(CONN, api({"type": "bearer"}), op, {"token": "b1"}, {})
    assert fake.requests[-1].headers["authorization"] == "Bearer b1"
    await driver.call(CONN, api({"type": "basic"}), op, {"username": "u", "password": "p:w"}, {})
    expected = "Basic " + base64.b64encode(b"u:p:w").decode()
    assert fake.requests[-1].headers["authorization"] == expected
    await driver.call(CONN, api({"type": "none"}), op, {}, {})
    assert "authorization" not in fake.requests[-1].headers
    with pytest.raises(DriverError, match="no credential"):
        await driver.call(CONN, api({"type": "bearer"}), op, {}, {})


@pytest.mark.parametrize("client_auth", ["basic", "body"])
async def test_oauth2_client_credentials_keeps_the_token_and_retries_once_on_401(
    driver: Any, fake: FakeApi, client_auth: str
) -> None:
    spec = api(
        {"type": "oauth2", "token_url": TOKEN_URL, "scope": "read", "client_auth": client_auth}
    )
    values = {"client_id": "cid", "client_secret": "csecret"}
    op = OPS["list_items"]
    await driver.call(CONN, spec, op, values, {})
    await driver.call(CONN, spec, op, values, {})
    assert fake.tokens_issued == 1
    token_request = fake.requests[0]
    assert parse_qs(token_request.content.decode())["scope"] == ["read"]
    assert fake.requests[-1].headers["authorization"] == "Bearer tok-1"
    # The server revokes the token: one 401, a new token, and the call again.
    fake.valid = set()
    answer = await driver.call(CONN, spec, op, values, {})
    assert '"ok":true' in answer
    assert fake.tokens_issued == 2
    # The token expires (with the margin): a new one.
    driver.now[0] += 3600
    await driver.call(CONN, spec, op, values, {})
    assert fake.tokens_issued == 3
    # New values: a new token.
    with pytest.raises(DriverError) as refused:
        await driver.call(CONN, spec, op, {"client_id": "cid", "client_secret": "bad"}, {})
    assert "invalid_client" in str(refused.value)
    assert "bad" not in str(refused.value)
    assert "csecret" not in str(refused.value)


async def test_the_path_argument_stays_in_one_segment(driver: ApiDriver, fake: FakeApi) -> None:
    spec = api({"type": "none"})
    await driver.call(CONN, spec, OPS["get_item"], {}, {"id": "a/../b?c", "full": True})
    sent = fake.requests[-1]
    assert sent.url.raw_path == b"/v1/items/a%2F..%2Fb%3Fc?full=true"
    for bad in ("..", "."):
        with pytest.raises(DriverError, match="not a valid value"):
            await driver.call(CONN, spec, OPS["get_item"], {}, {"id": bad})
    with pytest.raises(DriverError, match="missing argument: id"):
        await driver.call(CONN, spec, OPS["get_item"], {}, {})
    with pytest.raises(DriverError, match="unknown arguments: extra"):
        await driver.call(CONN, spec, OPS["list_items"], {}, {"extra": 1})


async def test_the_body_is_json(driver: ApiDriver, fake: FakeApi) -> None:
    await driver.call(CONN, api({"type": "none"}), OPS["add_item"], {}, {"body": {"n": 1}})
    sent = fake.requests[-1]
    assert sent.method == "POST"
    assert json.loads(sent.content) == {"n": 1}
    answer = await driver.call(CONN, api({"type": "none"}), OPS["drop_item"], {}, {"id": 7})
    assert answer == "(the API answered 204 with no body)"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (404, DriverError),
        (403, DriverError),
        (429, DriverUnavailableError),
        (503, DriverUnavailableError),
    ],
)
async def test_the_status_maps_to_an_error(
    driver: ApiDriver, fake: FakeApi, status: int, error: type[Exception]
) -> None:
    fake.status = status
    with pytest.raises(error, match=f"answered {status}: .*nope"):
        await driver.call(CONN, api({"type": "none"}), OPS["list_items"], {}, {})


async def test_a_private_address_is_refused(fake: FakeApi) -> None:
    driver = ApiDriver(transport=wire_transport(fake), resolver=public)
    spec = ApiSpec.model_validate(
        {"base_url": "https://private.remote.test", "auth": {"type": "none"}}
    )
    with pytest.raises(DriverError, match="not allowed"):
        await driver.call(CONN, spec, OPS["list_items"], {}, {})
    assert fake.requests == []
    await driver.aclose()


async def test_the_health_check_uses_the_test_operation_or_the_first_simple_get(
    driver: ApiDriver, fake: FakeApi
) -> None:
    ops = list(OPS.values())
    await driver.test(CONN, api({"type": "none"}), ops, {})
    assert fake.requests[-1].url.path == "/v1/items"
    with pytest.raises(DriverError, match="no operation nothing"):
        await driver.test(CONN, api({"type": "none"}, test="nothing"), ops, {})
    # Without a usable operation, only the URL is checked.
    count = len(fake.requests)
    await driver.test(CONN, api({"type": "none"}), [OPS["add_item"]], {})
    assert len(fake.requests) == count


async def test_a_document_is_fetched(driver: ApiDriver, fake: FakeApi) -> None:
    text = await driver.fetch_document(f"https://{HOST}/openapi.json")
    assert json.loads(text)["path"] == "/openapi.json"
    fake.status = 404
    with pytest.raises(DriverError, match="answered 404"):
        await driver.fetch_document(f"https://{HOST}/openapi.json")


class HeldTransport(httpx.AsyncBaseTransport):
    """Holds the requests with a revoked token until ``count`` of them are in flight, so that
    all their 401 answers arrive at the same time."""

    def __init__(self, fake: FakeApi, count: int) -> None:
        self._inner = wire_transport(fake)
        self._fake = fake
        self._count = count
        self._held = 0
        self._all_in = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token.startswith("tok-") and token not in self._fake.valid:
            self._held += 1
            if self._held >= self._count:
                self._all_in.set()
            await self._all_in.wait()
        else:
            await asyncio.sleep(0)  # Let the other calls run, as on a real connection.
        return await self._inner.handle_async_request(request)


async def test_overlapping_401s_get_one_new_token(fake: FakeApi) -> None:
    calls = 4
    driver = ApiDriver(transport=HeldTransport(fake, calls), resolver=public)
    spec = api({"type": "oauth2", "token_url": TOKEN_URL})
    values = {"client_id": "cid", "client_secret": "csecret"}
    op = OPS["list_items"]
    await driver.call(CONN, spec, op, values, {})
    assert fake.tokens_issued == 1
    # The server revokes tok-1. The calls all get a 401 with tok-1 at the same time, but only
    # one of them gets a new token: the others use it and do not throw it away.
    fake.valid = set()
    answers = await asyncio.gather(*(driver.call(CONN, spec, op, values, {}) for _ in range(calls)))
    assert all('"ok":true' in answer for answer in answers)
    assert fake.tokens_issued == 2
    await driver.aclose()


async def test_the_issued_token_and_the_oauth_error_are_redacted(
    driver: Any, fake: FakeApi
) -> None:
    fake.prefix = "tok-issued-only-by-the-server-"
    spec = api({"type": "oauth2", "token_url": TOKEN_URL})
    values = {"client_id": "cid", "client_secret": "csecret"}
    await driver.call(CONN, spec, OPS["list_items"], values, {})
    token = fake.requests[-1].headers["authorization"].removeprefix("Bearer ")
    try:
        # The vault does not know the token, but the redactor does now.
        assert token not in redact(f"the API echoed {token}")
        # The API echoes the token in an error: the error text does not show it.
        fake.status, fake.echo_auth = 400, True
        with pytest.raises(DriverError) as refused:
            await driver.call(CONN, spec, OPS["list_items"], values, {})
        assert token not in str(refused.value)
    finally:
        default_redactor().forget(token)
    # A token URL that puts a known secret in its OAuth error code.
    secret = "known-client-secret-value-7"
    default_redactor().add(secret)
    try:
        fake.error_code = f"invalid_client {secret}"
        driver.forget(CONN)
        with pytest.raises(DriverError) as refused:
            await driver.call(
                CONN, spec, OPS["list_items"], {"client_id": "cid", "client_secret": "x"}, {}
            )
        assert secret not in str(refused.value)
        assert "invalid_client" in str(refused.value)
    finally:
        default_redactor().forget(secret)


async def test_the_test_operation_must_only_read(driver: ApiDriver, fake: FakeApi) -> None:
    ops = list(OPS.values())
    for name in ("add_item", "drop_item"):
        with pytest.raises(DriverError, match="GET or HEAD"):
            await driver.test(CONN, api({"type": "none"}, test=name), ops, {})
    assert fake.requests == []
    await driver.test(CONN, api({"type": "none"}, test="list_items"), ops, {})
    assert fake.requests[-1].method == "GET"


async def test_a_list_goes_into_the_query_as_one_pair_for_each_item(
    driver: ApiDriver, fake: FakeApi
) -> None:
    spec = api({"type": "api_key", "name": "full", "location": "query"})
    await driver.call(CONN, spec, OPS["get_item"], {"api_key": "k"}, {"id": 1, "full": ["x"]})
    # The key wins over an argument with its name.
    assert fake.requests[-1].url.params.get_list("full") == ["k"]
    spec = api({"type": "none"})
    await driver.call(CONN, spec, OPS["get_item"], {}, {"id": 1, "full": ["a", 2, True]})
    assert fake.requests[-1].url.params.get_list("full") == ["a", "2", "true"]
    with pytest.raises(DriverError, match="not a valid value for full"):
        await driver.call(CONN, spec, OPS["get_item"], {}, {"id": 1, "full": {"a": 1}})
