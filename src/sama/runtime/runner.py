"""The runner: the process in the sandbox that runs one invocation of a runtime adapter.

Start it with ``python -m sama.runtime.runner`` (docs/architecture.md, Section 16.2).

Protocol, one JSON object per line:

- stdin, line 1: a ``RunnerJob``. It holds a ``RunRequest`` or a ``ResumeRequest``.
- stdin, later lines: ``{"op": "cancel"}`` stops the run. End of stdin also stops the run, so
  a runner never outlives the orchestrator that started it.
- stdout: one ``RunEvent`` per line. The last line is the terminal event.
- stderr: logs only.

The runner gets exactly one credential, the run token, in ``SAMA_RUN_TOKEN``. It uses the
token as the bearer for every MCP server and as the model key for the model proxy at
``SAMA_MODEL_URL``. The runner refuses to start if a provider key or another long-lived
secret is in its environment (decision D6).

Approval state is in the control plane, not here: each invocation is a new process. The
orchestrator refuses a second resume of the same approval before it starts a runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Mapping
from typing import IO, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from sama.runtime.contract import (
    AgentRuntime,
    ContractViolation,
    FailureCode,
    ResumeRequest,
    RunConfig,
    RunEvent,
    RunFailed,
    RunRequest,
    RunScope,
    RuntimeKind,
    Usage,
    is_terminal,
    run_event_adapter,
)
from sama.runtime.fake import FakeRuntime, FakeScript
from sama.sandbox.base import WORKDIR

log: Final = logging.getLogger("sama.runner")

RUN_TOKEN_ENV: Final = "SAMA_RUN_TOKEN"  # noqa: S105 -- a variable name
MODEL_URL_ENV: Final = "SAMA_MODEL_URL"
WORKDIR_ENV: Final = "SAMA_WORKDIR"
CANCEL_GRACE_S: Final = 4.0
"""The Claude adapter waits this long for the CLI after a cancel. The orchestrator waits
longer before it kills the runner, and both fit in the 10-second cancel limit."""

FORBIDDEN_ENV: Final = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "E2B_API_KEY",
        "DATABASE_URL",
        "SAMA_DATABASE_URL",
        "SAMA_RUN_TOKEN_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
    }
)
"""Variables that must not be in the sandbox. The runner sets the model key for the CLI
itself, from the run token."""


class GatePolicy(BaseModel):
    """A static tool gate for tests and local development. ``ask`` and ``deny`` hold exact
    tool names. Other tools are allowed. A real run sets ``RunnerJob.gate_url`` instead: then
    the runner asks the control plane for each call (``RemoteGate``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


class RunnerJob(BaseModel):
    """The first line on stdin. Exactly one of ``run`` and ``resume`` is set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["run", "resume"]
    run: RunRequest | None = None
    resume: ResumeRequest | None = None
    gate: GatePolicy = GatePolicy()
    gate_url: str | None = Field(default=None, min_length=1)
    """The gate endpoint of the control plane (``POST /gate``). When set, ``gate`` must be
    empty."""
    archive_url: str | None = Field(default=None, min_length=1)
    """The archive endpoint of the control plane (``POST /archive``). With it, the runtime
    gives its transcript to the control plane before it compacts (Section 6.3)."""
    fake_scripts: tuple[FakeScript, ...] = ()
    """Scripts for the fake runtime. Only for tests."""

    @model_validator(mode="after")
    def _one_request(self) -> Self:
        if (self.op == "run") != (self.run is not None) or (self.op == "resume") != (
            self.resume is not None
        ):
            raise ValueError("set run for op=run, or resume for op=resume, not both")
        if self.gate_url is not None and (self.gate.ask or self.gate.deny):
            raise ValueError("set gate_url or a static gate, not both")
        return self

    @property
    def request(self) -> RunRequest | ResumeRequest:
        request = self.run or self.resume
        assert request is not None  # noqa: S101 -- the validator checks it
        return request


def forbidden_env(env: Mapping[str, str]) -> list[str]:
    """The names of forbidden variables that are set and not empty."""
    return sorted(name for name in FORBIDDEN_ENV if env.get(name))


def with_run_token(config: RunConfig, token: str) -> RunConfig:
    """The config with the run token as the bearer of every MCP server.

    The bearer is not part of the JSON or the fingerprint, so the runner sets it here.
    """
    secret = SecretStr(token)
    servers = tuple(s.model_copy(update={"bearer_token": secret}) for s in config.mcp_servers)
    return config.model_copy(update={"mcp_servers": servers})


def claude_cli_env(token: str, model_url: str | None) -> dict[str, str]:
    env = {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "ANTHROPIC_API_KEY": token,
    }
    if model_url:
        # The Anthropic route of the model proxy (docs/architecture.md, Section 16.6).
        env["ANTHROPIC_BASE_URL"] = model_url.rstrip("/") + "/anthropic"
    return env


def build_runtime(job: RunnerJob, env: Mapping[str, str]) -> AgentRuntime:
    kind = job.request.config.runtime
    if kind is RuntimeKind.FAKE:
        return FakeRuntime(scripts=job.fake_scripts)
    if kind is RuntimeKind.CLAUDE_AGENT_SDK:
        from sama.runtime.claude import ClaudeRuntime, ClaudeSettings, GateDecision
        from sama.runtime.claude_transcript import cli_session_exists

        workdir = env.get(WORKDIR_ENV) or WORKDIR
        os.makedirs(workdir, exist_ok=True)
        settings = ClaudeSettings(
            cwd=workdir,
            env=claude_cli_env(env[RUN_TOKEN_ENV], env.get(MODEL_URL_ENV)),
            cancel_grace_s=CANCEL_GRACE_S,
            session_exists=cli_session_exists,
        )
        archiver = None
        if job.archive_url is not None:
            from sama.runtime.archive import RemoteArchiver

            archiver = RemoteArchiver(job.archive_url, env[RUN_TOKEN_ENV])
        if job.gate_url is not None:
            from sama.runtime.remote_gate import RemoteGate

            return ClaudeRuntime(
                gate=RemoteGate(job.gate_url, env[RUN_TOKEN_ENV]),
                settings=settings,
                archiver=archiver,
            )

        policy = job.gate

        class StaticGate:
            async def check(
                self,
                scope: RunScope,
                tool_name: str,
                arguments: Mapping[str, Any],
                *,
                call_id: str,
                child: bool = False,
            ) -> GateDecision:
                if tool_name in policy.deny:
                    return GateDecision("deny", "the tool policy denies this tool")
                if tool_name in policy.ask:
                    return GateDecision("ask", "the tool policy asks for approval")
                return GateDecision("allow")

        return ClaudeRuntime(gate=StaticGate(), settings=settings, archiver=archiver)
    raise ValueError(f"the runner does not support runtime {kind}")


def _failed(code: FailureCode, message: str, usage: Usage | None = None) -> RunFailed:
    return RunFailed(code=code, message=message, usage=usage or Usage())


class Output:
    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def event(self, event: RunEvent) -> None:
        self._stream.write(run_event_adapter.dump_json(event).decode() + "\n")
        self._stream.flush()


async def _stdin_lines() -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while raw := await reader.readline():
        yield raw.decode()


async def _watch_control(
    lines: AsyncIterator[str],
    runtime: AgentRuntime,
    run_id: str,
    done: asyncio.Event,
    requested: asyncio.Event,
) -> None:
    """Reads control lines. A cancel line or the end of stdin cancels the run.

    It sets ``requested`` too: a cancel that comes before the adapter registers the run has
    no effect in the adapter, so ``serve`` sends it again when the first event comes.
    """
    async for line in lines:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log.warning("the runner ignored a control line that is not JSON")
            continue
        if isinstance(message, dict) and message.get("op") == "cancel":
            break
        log.warning("the runner ignored an unknown control line")
    if not done.is_set():
        requested.set()
        await runtime.cancel(run_id)


async def serve(lines: AsyncIterator[str], out: Output, env: Mapping[str, str]) -> int:
    """Runs one job. Returns the exit code: 0 if the stream ended with a terminal event."""
    first = await anext(aiter(lines), None)
    if first is None:
        out.event(_failed(FailureCode.INVALID_REQUEST, "no job on stdin"))
        return 2
    try:
        job = RunnerJob.model_validate_json(first)
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
        out.event(_failed(FailureCode.INVALID_REQUEST, f"the job is not valid: {problems}"))
        return 2
    except ContractViolation as error:  # a fake script that breaks the stream rules
        out.event(_failed(FailureCode.INVALID_REQUEST, f"the job is not valid: {error}"))
        return 2
    leaked = forbidden_env(env)
    if leaked:
        message = f"the sandbox has forbidden variables: {', '.join(leaked)}"
        out.event(_failed(FailureCode.SANDBOX_ERROR, message))
        return 3
    token = env.get(RUN_TOKEN_ENV)
    if not token:
        out.event(_failed(FailureCode.SANDBOX_ERROR, f"{RUN_TOKEN_ENV} is not set"))
        return 3
    try:
        runtime = build_runtime(job, env)
    except (ValueError, ImportError, ContractViolation) as error:
        out.event(_failed(FailureCode.INVALID_REQUEST, str(error)))
        return 2

    config = with_run_token(job.request.config, token)
    stream: AsyncIterator[RunEvent]
    if job.run is not None:
        stream = runtime.run(job.run.model_copy(update={"config": config}))
    else:
        assert job.resume is not None  # noqa: S101 -- the validator checks it
        stream = runtime.resume(job.resume.model_copy(update={"config": config}))

    run_id = job.request.scope.run_id
    done = asyncio.Event()
    requested = asyncio.Event()
    control = asyncio.create_task(_watch_control(lines, runtime, run_id, done, requested))
    usage = Usage()
    ended = False
    try:
        async for event in stream:
            if requested.is_set() and not is_terminal(event):
                requested.clear()  # The control task sets it once, so this repeats it once.
                await runtime.cancel(run_id)
            usage = event.usage
            ended = is_terminal(event)
            out.event(event)
            if ended:
                break
    except Exception as error:  # the adapter broke the contract; report it, do not crash
        log.exception("the runtime raised")
        if not ended:
            out.event(
                _failed(FailureCode.INTERNAL, f"the runtime raised {type(error).__name__}", usage)
            )
            ended = True
    finally:
        done.set()
        control.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await control
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
        close_runtime = getattr(runtime, "aclose", None)
        if close_runtime is not None:
            await close_runtime()
    if not ended:
        out.event(
            _failed(FailureCode.INTERNAL, "the runtime ended without a terminal event", usage)
        )
        return 1
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    # The Claude CLI and the SDK must not write to stdout: it carries the event stream.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        code = asyncio.run(serve(_stdin_lines(), Output(real_stdout), os.environ))
    finally:
        sys.stdout = real_stdout
    raise SystemExit(code)


if __name__ == "__main__":
    main()
