"""The runner of a custom script tool (docs/architecture.md, Section 10.8).

A script tool is a Python or a Node program. It runs in the session sandbox of the run, like
``execute_code`` (Section 10.2.1): the arguments go to its standard input as one JSON object,
and its standard output must be one JSON value. The program gets no run token and no secret, so
it cannot call sama tools, and it has only the egress of the sandbox.

The test button of the web app has no session. It runs the script in a new sandbox from the
same template, and kills that sandbox at the end (``SandboxFactory``).
"""

from __future__ import annotations

import json
import shlex
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from sama.customtools.http import ToolCallError
from sama.customtools.spec import ScriptConfig
from sama.sandbox.base import Sandbox, SandboxError
from sama.tools.code import (
    EXEC_MARGIN_S,
    MAX_STDOUT_BYTES,
    CodeError,
    parse_output,
    program_script,
)

TOOLS_DIR: Final = ".sama/tools"
"""The folder in the work folder that has one folder for each call of a script tool."""
MAX_RESULT_CHARS: Final = 50_000

COMMANDS: Final = {"python": ("main.py", "python3"), "node": ("main.js", "node")}
"""The file name and the interpreter of each language."""

SandboxFactory = Callable[[str], Awaitable[Sandbox]]
"""``org_id -> Sandbox``: a new sandbox for a test call. The caller kills it."""


async def run_script(sandbox: Sandbox, script: ScriptConfig, arguments: Mapping[str, Any]) -> str:
    """Runs the script with the arguments and returns its JSON output as text. Raises
    ``ToolCallError``."""
    workdir = sandbox.workdir.rstrip("/")
    folder = f"{workdir}/{TOOLS_DIR}/{uuid.uuid4().hex[:12]}"
    file, interpreter = COMMANDS[script.language]
    q = shlex.quote(folder)
    try:
        await sandbox.write_file(f"{folder}/{file}", script.code.encode())
        await sandbox.write_file(
            f"{folder}/input.json", json.dumps(dict(arguments), ensure_ascii=False).encode()
        )
        result = await sandbox.exec(
            [
                "bash",
                "-c",
                program_script(
                    folder,
                    script.timeout_s,
                    command=f"{interpreter} {q}/{file}",
                    stdin=f"{folder}/input.json",
                ),
            ],
            env={"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
            cwd=workdir,
            timeout_s=script.timeout_s + EXEC_MARGIN_S,
        )
    except SandboxError as error:
        raise ToolCallError(f"the sandbox failed: {error}") from None
    try:
        output = parse_output(result.stdout, result.stderr, script.timeout_s)
    except CodeError as error:
        raise ToolCallError(f"the script did not run: {error}") from None
    if output.timed_out:
        raise ToolCallError(f"the script stopped after the time limit of {script.timeout_s} s")
    tail = output.stderr.strip()[-500:]
    if output.exit_code != 0:
        detail = f": {tail}" if tail else ""
        raise ToolCallError(f"the script ended with exit code {output.exit_code}{detail}")
    if output.stdout_bytes > MAX_STDOUT_BYTES:
        raise ToolCallError(f"the script wrote more than {MAX_STDOUT_BYTES:,} bytes")
    try:
        value = json.loads(output.stdout)
    except ValueError:
        raise ToolCallError("the script must write one JSON value to standard output") from None
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n[cut]"
    return text
