"""The Composio client and the HTTP API driver, with fake transports (Section 10.3)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sama.connectors.composio import ComposioClient, ComposioError
from sama.connectors.http_api import DriverError, HttpApiDriver
from sama.connectors.manifest import parse
from tests.connectors.test_manifest import FORM

KEY = SecretStr("ck-test-key")


class Recorder:
    def __init__(self, answers: dict[tuple[str, str], Any]) -> None:
        self.answers = answers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.get((request.method, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"error": {"message": "not found"}})
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)


def composio(answers: dict[tuple[str, str], Any]) -> tuple[ComposioClient, Recorder]:
    recorder = Recorder(answers)
    return ComposioClient(transport=httpx.MockTransport(recorder)), recorder


async def test_the_auth_config_is_found_once_and_cached() -> None:
    client, recorder = composio(
        {
            ("GET", "/api/v3/auth_configs"): {
                "items": [
                    {"id": "ac_custom", "toolkit": {"slug": "gmail"}, "status": "ENABLED"},
                    {
                        "id": "ac_managed",
                        "toolkit": {"slug": "gmail"},
                        "status": "ENABLED",
                        "is_composio_managed": True,
                    },
                ]
            }
        }
    )
    assert await client.auth_config(KEY, "gmail") == "ac_managed"
    assert await client.auth_config(KEY, "gmail") == "ac_managed"
    assert len(recorder.requests) == 1
    assert recorder.requests[0].headers["x-api-key"] == "ck-test-key"
    assert recorder.requests[0].url.params["toolkit_slug"] == "gmail"
    await client.aclose()


async def test_an_auth_config_is_made_when_there_is_none() -> None:
    client, recorder = composio(
        {
            ("GET", "/api/v3/auth_configs"): {"items": []},
            ("POST", "/api/v3/auth_configs"): {"auth_config": {"id": "ac_new"}},
        }
    )
    assert await client.auth_config(KEY, "github") == "ac_new"
    body = json.loads(recorder.requests[1].content)
    assert body == {
        "toolkit": {"slug": "github"},
        "auth_config": {"type": "use_composio_managed_auth"},
    }
    await client.aclose()


async def test_the_link_needs_https() -> None:
    client, recorder = composio(
        {
            ("POST", "/api/v3.1/connected_accounts/link"): {
                "connected_account_id": "ca_1",
                "redirect_url": "https://connect.composio.dev/link/x",
            }
        }
    )
    link = await client.link(
        KEY, auth_config_id="ac_1", user_id="sama-o-u-u", callback_url="https://w/connections"
    )
    assert (link.account_id, link.redirect_url) == ("ca_1", "https://connect.composio.dev/link/x")
    assert json.loads(recorder.requests[0].content)["user_id"] == "sama-o-u-u"
    recorder.answers[("POST", "/api/v3.1/connected_accounts/link")] = {
        "connected_account_id": "ca_1",
        "redirect_url": "http://evil.test/",
    }
    with pytest.raises(ComposioError, match="https"):
        await client.link(KEY, auth_config_id="ac_1", user_id="u", callback_url="https://w/")
    await client.aclose()


async def test_status_delete_and_execute() -> None:
    client, recorder = composio(
        {
            ("GET", "/api/v3.1/connected_accounts/ca_1"): {
                "status": "active",
                "data": {"access_token": "secret-token"},
            },
            ("POST", "/api/v3.1/tools/execute/GMAIL_GET_PROFILE"): {
                "successful": True,
                "data": {"email": "a@b.c"},
            },
            ("POST", "/api/v3.1/tools/execute/GMAIL_SEND_EMAIL"): {
                "successful": False,
                "error": "quota",
            },
        }
    )
    assert await client.status(KEY, "ca_1") == "ACTIVE"
    await client.delete(KEY, "ca_gone")  # 404 is not an error
    data = await client.execute(
        KEY, "GMAIL_GET_PROFILE", account_id="ca_1", user_id="u", arguments={}
    )
    assert data == {"email": "a@b.c"}
    with pytest.raises(ComposioError, match="quota"):
        await client.execute(KEY, "GMAIL_SEND_EMAIL", account_id="ca_1", user_id="u", arguments={})
    with pytest.raises(ComposioError, match="not a valid"):
        await client.status(KEY, "../x")
    assert all("../" not in str(r.url) for r in recorder.requests)
    await client.aclose()


async def test_an_error_answer_is_short_and_has_a_status() -> None:
    client, _ = composio(
        {
            ("GET", "/api/v3.1/connected_accounts/ca_1"): httpx.Response(
                401, json={"message": "x" * 900}
            )
        }
    )
    with pytest.raises(ComposioError) as caught:
        await client.status(KEY, "ca_1")
    assert caught.value.status_code == 401
    assert len(str(caught.value)) < 400
    await client.aclose()


def driver(handler: Any) -> HttpApiDriver:
    return HttpApiDriver(transport=httpx.MockTransport(handler))


async def test_the_driver_sends_the_credential_and_the_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    manifest = parse(FORM)
    tool = manifest.tools[0]
    http = driver(handler)
    await http.test(manifest, {"token": "tok-1"})
    text = await http.call(manifest, tool, {"token": "tok-1"}, {"item_id": "a/b", "limit": 2})
    assert json.loads(text) == {"ok": True}
    assert seen[0].url.path == "/me"
    assert seen[0].headers["authorization"] == "Bearer tok-1"
    assert seen[1].url.raw_path == b"/items/a%2Fb?limit=2"
    with pytest.raises(DriverError, match="not a valid"):
        await http.call(manifest, tool, {"token": "tok-1"}, {"item_id": ".."})
    with pytest.raises(DriverError, match="missing argument"):
        await http.call(manifest, tool, {"token": "tok-1"}, {})
    await http.aclose()


async def test_the_driver_refuses_an_error_answer() -> None:
    manifest = parse(FORM)
    http = driver(lambda request: httpx.Response(401, text="bad token"))
    with pytest.raises(DriverError, match="401"):
        await http.test(manifest, {"token": "tok-1"})
    with pytest.raises(DriverError, match="no credential"):
        await http.test(manifest, {})
    await http.aclose()
