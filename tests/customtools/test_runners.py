"""The HTTP runner and the script runner of custom tools (docs/architecture.md, Section 10.8).

``OutboundClient`` sends each request to the IP address and names the host in ``Host``. The
script tests use the local sandbox provider with the system ``python3`` and ``node``.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import shutil
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from sama.customtools.http import Credential, HttpToolRunner, ToolCallError
from sama.customtools.script import run_script
from sama.customtools.spec import HttpConfig, ScriptConfig
from sama.sandbox.base import Sandbox, SandboxSpec
from sama.sandbox.local import LocalProvider
from sama.tools.outbound import IPAddress
from tests.support import make_scope, wire_transport

ORIGIN = "https://api.weather.test"
# Fake credentials, built at run time so that the secret scanner does not flag the source.
FAKE_TOKEN = "-".join(("tok", "fake", "runner"))
FAKE_KEY = "-".join(("key", "fake", "runner"))
FAKE_PASSWORD = "-".join(("pw", "fake", "runner"))


async def public(host: str, port: int) -> Sequence[IPAddress]:
    if host == "private.test":
        return [ipaddress.ip_address("10.0.0.7")]
    return [ipaddress.ip_address("93.184.215.14")]


class Fake:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: Any = {"data": {"temp": 21, "city": "Pune", "extra": "x"}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.body, str):
            return httpx.Response(self.status, text=self.body)
        return httpx.Response(self.status, json=self.body)


def runner(fake: Fake) -> HttpToolRunner:
    return HttpToolRunner(transport=wire_transport(fake), resolver=public)


def config(**values: Any) -> HttpConfig:
    return HttpConfig.model_validate({"url": ORIGIN + "/v1/{{city}}?days={{days}}", **values})


async def test_a_get_fills_the_url_and_maps_the_answer() -> None:
    fake = Fake()
    answer = await runner(fake).call(
        config(response={"path": "data", "fields": ["temp", "city"]}),
        {"city": "Pune", "days": 2},
        None,
    )
    assert json.loads(answer.text) == {"temp": 21, "city": "Pune"}
    request = fake.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/v1/Pune"
    assert request.url.params["days"] == "2"
    assert request.headers["host"] == "api.weather.test"
    assert request.headers["accept"] == "application/json"


@pytest.mark.parametrize(
    ("auth", "credential", "where"),
    [
        ({"type": "bearer"}, {"value": FAKE_TOKEN}, ("authorization", "Bearer " + FAKE_TOKEN)),
        (
            {"type": "api_key", "header": "X-Api-Key"},
            {"value": FAKE_KEY},
            ("x-api-key", FAKE_KEY),
        ),
        ({"type": "api_key", "query": "key"}, {"value": FAKE_KEY}, ("query", "key")),
        (
            {"type": "basic"},
            {"username": "me", "password": FAKE_PASSWORD},
            ("authorization", "Basic " + base64.b64encode(f"me:{FAKE_PASSWORD}".encode()).decode()),
        ),
    ],
)
async def test_the_credential_goes_only_where_the_auth_says(
    auth: dict[str, Any], credential: dict[str, str], where: tuple[str, str]
) -> None:
    fake = Fake()
    secret = Credential(origin=ORIGIN, **credential)
    await runner(fake).call(
        config(method="POST", auth=auth, body={"c": "{{city}}"}), {"city": "x"}, secret
    )
    request = fake.requests[0]
    assert json.loads(request.content) == {"c": "x"}
    header, value = where
    if header == "query":
        assert request.url.params[value] == FAKE_KEY
    else:
        assert request.headers[header] == value


async def test_a_credential_of_another_origin_is_not_sent() -> None:
    fake = Fake()
    secret = Credential(origin="https://old.test", value=FAKE_TOKEN)
    with pytest.raises(ToolCallError, match="another address"):
        await runner(fake).call(config(auth={"type": "bearer"}), {"city": "x"}, secret)
    with pytest.raises(ToolCallError, match="no credential"):
        await runner(fake).call(config(auth={"type": "bearer"}), {"city": "x"}, None)
    assert fake.requests == []


async def test_errors_are_short_and_private_addresses_are_refused() -> None:
    fake = Fake()
    fake.status = 404
    fake.body = "not here " * 100
    with pytest.raises(ToolCallError, match="answered 404"):
        await runner(fake).call(config(), {"city": "x"}, None)
    private = HttpConfig.model_validate({"url": "https://private.test/x"})
    with pytest.raises(ToolCallError, match="not allowed"):
        await runner(fake).call(private, {}, None)
    with pytest.raises(ToolCallError, match="cannot use"):
        await runner(fake).check_url("https://private.test/x")
    with pytest.raises(ToolCallError, match="missing argument"):
        await runner(fake).call(config(), {}, None)


async def test_a_redirect_is_not_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.test/"})

    tool = HttpToolRunner(transport=wire_transport(handler), resolver=public)
    with pytest.raises(ToolCallError, match="redirect"):
        await tool.call(config(), {"city": "x"}, None)


@pytest.fixture
async def sandbox(tmp_path: Path) -> AsyncIterator[Sandbox]:
    (tmp_path / "sandboxes").mkdir()
    provider = LocalProvider(tmp_path / "sandboxes")
    scope = make_scope()
    made = await provider.create(
        SandboxSpec(org_id=scope.org_id, session_id=scope.session_id, template="t")
    )
    yield made
    await made.kill()


PYTHON = """
import json, sys
data = json.load(sys.stdin)
print(json.dumps({"hello": data["name"], "n": len(data["name"])}))
"""
NODE = """
const data = JSON.parse(require("fs").readFileSync(0, "utf8"));
console.log(JSON.stringify({hello: data.name}));
"""


async def test_a_python_script_reads_json_and_writes_json(sandbox: Sandbox) -> None:
    text = await run_script(sandbox, ScriptConfig(code=PYTHON), {"name": "sama"})
    assert json.loads(text) == {"hello": "sama", "n": 4}
    # The folder of the call is removed.
    assert not list((Path(sandbox.workdir) / ".sama" / "tools").iterdir())


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_a_node_script_reads_json_and_writes_json(sandbox: Sandbox) -> None:
    text = await run_script(sandbox, ScriptConfig(language="node", code=NODE), {"name": "x"})
    assert json.loads(text) == {"hello": "x"}


@pytest.mark.parametrize(
    ("code", "problem"),
    [
        ("print('not json')", "one JSON value"),
        ("import sys; sys.stderr.write('boom'); sys.exit(3)", "exit code 3: boom"),
        ("import time; time.sleep(5)", "time limit"),
    ],
)
async def test_a_failed_script_gives_a_clear_error(
    sandbox: Sandbox, code: str, problem: str
) -> None:
    with pytest.raises(ToolCallError, match=problem):
        await run_script(sandbox, ScriptConfig(code=code, timeout_s=1), {})


async def test_an_echoed_query_key_is_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": str(request.url)})

    tool = HttpToolRunner(transport=wire_transport(handler), resolver=public)
    value = "/".join(("opaque", "key+value=fake"))
    secret = Credential(origin=ORIGIN, value=value)
    answer = await tool.call(
        config(auth={"type": "api_key", "query": "key"}), {"city": "x"}, secret
    )
    assert "opaque" not in answer.text
    assert "[REDACTED]" in answer.text


async def test_an_echoed_basic_auth_header_is_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"seen": request.headers["authorization"]})

    tool = HttpToolRunner(transport=wire_transport(handler), resolver=public)
    secret = Credential(origin=ORIGIN, username="someone", password=FAKE_PASSWORD)
    answer = await tool.call(config(auth={"type": "basic"}), {"city": "x"}, secret)
    token = base64.b64encode(f"someone:{FAKE_PASSWORD}".encode()).decode()
    assert token not in answer.text
    assert "[REDACTED]" in answer.text
