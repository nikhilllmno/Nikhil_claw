"""``execute_code`` (docs/architecture.md, Section 10.2.1; roadmap M4.8).

The tool runs a Python program in the session sandbox. The program can call sama tools through
the module ``sama_tools``: a small stub that sends one MCP ``tools/call`` request for each call
to the sama tool server. The tool gives only the standard output of the program to the model,
so the program can call many tools and give back a short result.

Security:

- The stub sends the run token of the run. The sandbox runner already has that token, so the
  sandbox gets no new secret.
- Each call of the stub passes the tool gate on the control plane (``GateService.screen``). A
  tool that gets ``ask`` needs an approved call with the same arguments, so the program cannot
  use a tool that a person did not approve.
- The block list and the call limit of the stub are helper behavior, not a security boundary.
  They are values in the program process, and the program has the run token, so it can change
  them or send requests itself. The gate (toolsets, deny rules, approval tickets) is the
  boundary.
- ``execute_code`` itself gets ``ask`` by default, like ``Bash``.

The module runs on the control plane. The sandbox gets the stub as a file, so the sandbox
template does not change.
"""

from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass
from typing import Final

from sama.sandbox.base import SandboxError
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.output import SessionSandbox

CODE_DIR: Final = ".sama/code"
"""The folder in the work folder that has one folder for each ``execute_code`` call."""

DEFAULT_TIMEOUT_S: Final = 60
MAX_TIMEOUT_S: Final = 300
MAX_CODE_CHARS: Final = 50_000
MAX_STDOUT_BYTES: Final = 200_000
"""The tool reads this much standard output at most. ``cap`` then spills more than 50 KB."""
MAX_STDERR_BYTES: Final = 2_000
MAX_FILE_KB: Final = 51_200
"""``ulimit -f``: a file that the program writes can have 50 MB at most."""
MAX_TOOL_CALLS: Final = 50
EXEC_MARGIN_S: Final = 30
"""The sandbox call waits this much longer than the program can run."""

TOOLS_URL_ENV: Final = "SAMA_TOOLS_URL"
TOKEN_ENV: Final = "SAMA_RUN_TOKEN"  # noqa: S105 -- a variable name

BLOCKED_TOOLS: Final = (
    "execute_code",
    "clarify",
    "memory",
    "skill_manage",
    "skill_import",
    "change_propose",
    "tool_call",
)
"""Tools that the stub refuses. They wait for a person, change shared state, or start another
program. The gate is the real boundary (see the module text)."""

STUB: Final = f'''"""Call sama tools from a program of execute_code.

    import sama_tools
    text = sama_tools.call("web_search", query="sama agent", limit=3)
    text = sama_tools.web_search(query="sama agent")

Each call gives the text result of the tool or raises ToolError.
"""

import json
import os
import urllib.error
import urllib.request

BLOCKED = frozenset({sorted(BLOCKED_TOOLS)!r})
_URL = os.environ.get("{TOOLS_URL_ENV}", "")
_TOKEN = os.environ.get("{TOKEN_ENV}", "")
_MAX_CALLS = {MAX_TOOL_CALLS}
_calls = 0


class ToolError(Exception):
    """A tool call failed or the tool gave an error."""


def call(name, **arguments):
    """Calls the sama tool ``name`` and returns its text result."""
    global _calls
    if name in BLOCKED:
        raise ToolError(name + " is not available in execute_code")
    _calls += 1
    if _calls > _MAX_CALLS:
        raise ToolError("a program can call tools " + str(_MAX_CALLS) + " times at most")
    body = json.dumps(
        {{
            "jsonrpc": "2.0",
            "id": _calls,
            "method": "tools/call",
            "params": {{"name": name, "arguments": arguments}},
        }}
    ).encode()
    request = urllib.request.Request(
        _URL,
        data=body,
        method="POST",
        headers={{
            "authorization": "Bearer " + _TOKEN,
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            kind = response.headers.get("content-type", "")
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        raise ToolError(name + " failed: HTTP " + str(error.code)) from None
    except (urllib.error.URLError, OSError) as error:
        raise ToolError(name + " failed: " + type(error).__name__) from None
    message = _message(raw, kind)
    if "error" in message:
        raise ToolError(name + " failed: " + str(message["error"].get("message", "")))
    result = message.get("result") or {{}}
    text = "\\n".join(
        part.get("text", "") for part in result.get("content", []) if part.get("type") == "text"
    )
    if result.get("isError"):
        raise ToolError(text or name + " failed")
    return text


def _message(raw, kind):
    if "text/event-stream" not in kind:
        return json.loads(raw)
    for line in raw.splitlines():
        if line.startswith("data:"):
            data = json.loads(line[5:].strip())
            if "result" in data or "error" in data:
                return data
    raise ToolError("the tool server gave no result")


def __getattr__(name):
    if name.startswith("_"):
        raise AttributeError(name)

    def tool(**arguments):
        return call(name, **arguments)

    return tool
'''


_MARK: Final = "SAMA-EXIT"


class CodeError(Exception):
    """``execute_code`` could not run the program. The message has no secret."""


@dataclass(frozen=True, slots=True)
class CodeResult:
    exit_code: int
    stdout: str
    stderr: str
    stdout_bytes: int
    timed_out: bool
    timeout_s: int


class CodeRunner:
    """Runs ``execute_code`` programs in the session sandbox.

    ``mcp_url`` is the URL of the sama MCP endpoint as the sandbox sees it (the same URL that
    the runner gives to the agent runtime).
    """

    def __init__(self, session_sandbox: SessionSandbox, mcp_url: str) -> None:
        self._session_sandbox = session_sandbox
        self._mcp_url = mcp_url

    async def run(
        self, claims: RunTokenClaims, token: str, code: str, timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> CodeResult:
        if not code.strip():
            raise CodeError("the code is empty")
        if len(code) > MAX_CODE_CHARS:
            raise CodeError(f"the code has more than {MAX_CODE_CHARS:,} characters")
        timeout_s = max(1, min(int(timeout_s), MAX_TIMEOUT_S))
        sandbox = await self._session_sandbox(claims.org_id, claims.session_id)
        if sandbox is None:
            raise CodeError("the session has no sandbox yet. Use a shell or file tool first.")
        workdir = sandbox.workdir.rstrip("/")
        folder = f"{workdir}/{CODE_DIR}/{uuid.uuid4().hex[:12]}"
        try:
            await sandbox.write_file(f"{folder}/sama_tools.py", STUB.encode())
            await sandbox.write_file(f"{folder}/main.py", code.encode())
            result = await sandbox.exec(
                ["bash", "-c", _script(folder, timeout_s)],
                env={
                    TOOLS_URL_ENV: self._mcp_url,
                    TOKEN_ENV: token,
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                cwd=workdir,
                timeout_s=timeout_s + EXEC_MARGIN_S,
            )
        except SandboxError as error:
            raise CodeError(f"the sandbox failed: {error}") from None
        return _result(result.stdout, result.stderr, timeout_s)


def _script(folder: str, timeout_s: int) -> str:
    """The shell script that runs the program. It prints the head of the standard output. The
    standard error has a line ``SAMA-EXIT <exit code> <stdout bytes>``, then the tail of the
    standard error of the program. The folder is removed at the end."""
    q = shlex.quote(folder)
    return "\n".join(
        [
            f"ulimit -f {MAX_FILE_KB}",
            f"timeout -k 5 {timeout_s} python3 {q}/main.py"
            f" > {q}/out.txt 2> {q}/err.txt < /dev/null",
            "code=$?",
            f'size=$(wc -c < {q}/out.txt | tr -d " ")',
            f"head -c {MAX_STDOUT_BYTES} {q}/out.txt",
            f'printf "{_MARK} %s %s\\n" "$code" "$size" >&2',
            f"tail -c {MAX_STDERR_BYTES} {q}/err.txt >&2",
            f"rm -rf {q}",
            "exit 0",
        ]
    )


def _result(stdout: str, stderr: str, timeout_s: int) -> CodeResult:
    _, mark, after = stderr.partition(_MARK + " ")
    first, _, rest = after.partition("\n")
    try:
        if not mark:
            raise ValueError
        code_text, size_text = first.split()
        exit_code, size = int(code_text), int(size_text)
    except ValueError:
        raise CodeError("the program did not run") from None
    # ``timeout`` exits with 124, or 137 when it must kill the program.
    timed_out = exit_code in (124, 137)
    return CodeResult(exit_code, stdout, rest, size, timed_out, timeout_s)


def render(result: CodeResult) -> str:
    """The text for the model. On success it is the standard output only."""
    out = result.stdout
    if result.stdout_bytes > MAX_STDOUT_BYTES:
        out += (
            f"\n[The program wrote {result.stdout_bytes:,} bytes. Only the first "
            f"{MAX_STDOUT_BYTES:,} bytes are here.]"
        )
    if result.exit_code == 0:
        return out if out else "[The program wrote nothing to standard output.]"
    if result.timed_out:
        problem = f"The program stopped after the time limit of {result.timeout_s} s."
    else:
        problem = f"The program ended with exit code {result.exit_code}."
    parts = [problem]
    if out:
        parts.append("Standard output:\n" + out)
    if result.stderr.strip():
        parts.append("End of the standard error:\n" + result.stderr.strip())
    return "\n\n".join(parts)
