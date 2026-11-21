"""``execute_code`` (docs/architecture.md, Section 10.2.1; roadmap M4.8).

The tests use the local sandbox provider and a real sama MCP server on a local port. The
program runs with the system ``python3`` and calls the server through the stub, as it does in
an E2B sandbox.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sama.gate.decide import GateStep, Verdict
from sama.sandbox import Sandbox, SandboxSpec
from sama.sandbox.local import LocalProvider
from sama.tenancy import RunTokenClaims, RunTokenSigner
from sama.tools import CodeError, CodeRunner, MemoryTodoStore, ToolServer
from sama.tools.code import (
    BLOCKED_TOOLS,
    CODE_DIR,
    MAX_STDOUT_BYTES,
    STUB,
    CodeResult,
    parse_output,
    render,
)
from tests.conformance.scripted_model import serve
from tests.support import make_scope
from tests.tools.test_server import connect, text_of


@dataclass
class DenyGate:
    """Allows every tool except the tools in ``denied``."""

    denied: set[str] = field(default_factory=set)
    screened: list[str] = field(default_factory=list)

    async def check(self, *args: Any, **kwargs: Any) -> Verdict:
        raise AssertionError("not used")

    async def screen(
        self, claims: RunTokenClaims, tool_name: str, arguments: Mapping[str, Any]
    ) -> Verdict:
        self.screened.append(tool_name)
        if tool_name in self.denied:
            return Verdict("deny", GateStep.CONTEXT, f"{tool_name} needs an approval")
        return Verdict("allow", GateStep.POLICY, "ok")


@dataclass
class World:
    url: str
    signer: RunTokenSigner
    sandbox: Sandbox
    todos: MemoryTodoStore
    gate: DenyGate

    def token(self) -> str:
        return self.signer.issue(make_scope(), ttl_s=600)


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


@pytest.fixture
def world(sandbox: Sandbox) -> Iterator[World]:
    signer = RunTokenSigner(secrets.token_bytes(32))
    scope = make_scope()
    todos = MemoryTodoStore()
    gate = DenyGate()
    url_box: list[str] = []

    async def session_sandbox(org_id: str, session_id: str) -> Sandbox | None:
        if (org_id, session_id) == (scope.org_id, scope.session_id):
            return sandbox
        return None

    class LateRunner(CodeRunner):
        """The URL of the server is known only after the server starts."""

        def __init__(self) -> None:
            super().__init__(session_sandbox, "")

        async def run(self, *args: Any, **kwargs: Any) -> CodeResult:
            self._mcp_url = url_box[0]
            return await super().run(*args, **kwargs)

    server = ToolServer(signer=signer, todos=todos, gate=gate, code=LateRunner())
    with serve(server.app()) as base:
        url_box.append(base + "/mcp")
        yield World(base + "/mcp", signer, sandbox, todos, gate)


async def test_the_tool_is_listed_only_with_a_runner(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "execute_code" in names
    bare = ToolServer(signer=world.signer, todos=MemoryTodoStore())
    with serve(bare.app()) as base:
        async with connect(base + "/mcp", world.token()) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
    assert "execute_code" not in names


async def test_only_standard_output_comes_back(world: World) -> None:
    code = "import sys\nprint('hello', 6 * 7)\nprint('noise', file=sys.stderr)\n"
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("execute_code", {"code": code})
    assert not result.is_error
    assert text_of(result) == "hello 42\n"


async def test_the_program_calls_sama_tools_through_the_stub(world: World) -> None:
    code = (
        "import sama_tools\n"
        "sama_tools.call('todo', todos=[{'id': 'a', 'content': 'Write', 'status': 'pending'}])\n"
        "text = sama_tools.todo()\n"
        "print('items:', text.count('Write'))\n"
    )
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("execute_code", {"code": code})
    assert not result.is_error, text_of(result)
    assert text_of(result).strip() == "items: 1"
    # The calls of the program passed the gate as normal tool calls.
    assert world.gate.screened.count("todo") == 2
    # The program folder is removed after the run.
    root = Path(world.sandbox.workdir) / CODE_DIR
    assert not root.exists() or list(root.iterdir()) == []


async def test_the_gate_judges_each_call_of_the_program(world: World) -> None:
    world.gate.denied.add("todo")
    code = (
        "import sama_tools\n"
        "try:\n"
        "    sama_tools.todo()\n"
        "except sama_tools.ToolError as error:\n"
        "    print('refused:', error)\n"
    )
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("execute_code", {"code": code})
    assert "refused:" in text_of(result)
    assert "todo needs an approval" in text_of(result)


async def test_the_stub_refuses_blocked_tools(world: World) -> None:
    code = (
        "import sama_tools\n"
        "for name in ['execute_code', 'clarify', 'memory']:\n"
        "    try:\n"
        "        sama_tools.call(name)\n"
        "    except sama_tools.ToolError as error:\n"
        "        print(error)\n"
    )
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("execute_code", {"code": code})
    assert text_of(result).count("is not available in execute_code") == 3
    assert "execute_code" in world.gate.screened
    assert "clarify" not in world.gate.screened


async def test_a_failing_program_gives_the_exit_code_and_the_error(world: World) -> None:
    code = "print('before')\nraise SystemExit('broken: ' + 'x')\n"
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("execute_code", {"code": code})
    text = text_of(result)
    assert "exit code 1" in text
    assert "before" in text
    assert "broken: x" in text


async def test_a_slow_program_stops_at_the_time_limit(world: World) -> None:
    code = "import time\nprint('start', flush=True)\ntime.sleep(30)\n"
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("execute_code", {"code": code, "timeout_s": 1})
    text = text_of(result)
    assert "time limit of 1 s" in text
    assert "start" in text


async def test_the_program_runs_in_the_work_folder(world: World) -> None:
    (Path(world.sandbox.workdir) / "data.txt").write_text("abc")
    code = "print(open('data.txt').read().upper())\n"
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("execute_code", {"code": code})
    assert text_of(result).strip() == "ABC"


async def test_no_sandbox_is_an_error(sandbox: Sandbox) -> None:
    async def none(org_id: str, session_id: str) -> Sandbox | None:
        return None

    signer = RunTokenSigner(secrets.token_bytes(32))
    claims = signer.verify(signer.issue(make_scope(), ttl_s=600))
    with pytest.raises(CodeError, match="no sandbox"):
        await CodeRunner(none, "http://x/mcp").run(claims, "t", "print(1)")
    with pytest.raises(CodeError, match="empty"):
        await CodeRunner(none, "http://x/mcp").run(claims, "t", "  ")


def test_render_cuts_a_long_output_and_says_so() -> None:
    result = CodeResult(0, "x" * 10, "", MAX_STDOUT_BYTES + 5, False, 60)
    assert "Only the first" in render(result)
    assert render(CodeResult(0, "", "", 0, False, 60)).startswith("[The program wrote nothing")


def test_a_missing_mark_is_an_error() -> None:
    with pytest.raises(CodeError, match="did not run"):
        parse_output("", "bash: python3: not found\n", 60)
    assert parse_output("ok", "noise\nSAMA-EXIT 0 2\n", 60).exit_code == 0


def test_the_stub_is_plain_python_and_has_the_block_list() -> None:
    compile(STUB, "sama_tools.py", "exec")
    for name in BLOCKED_TOOLS:
        assert f"'{name}'" in STUB
