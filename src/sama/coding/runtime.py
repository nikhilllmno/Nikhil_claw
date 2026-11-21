"""The runtimes ``claude_code`` and ``codex`` (docs/architecture.md, Sections 5.3 and 15.4).

A ``CodingAgentRuntime`` implements ``AgentRuntime`` on the control plane. Each run gives the
user message to the coding agent CLI in the session sandbox and streams the CLI events as run
events: the text of the agent as ``TextDelta``, and each tool of the CLI as
``ToolCallStarted`` and ``ToolCallFinished``. At the end, the diff and the pull request come
as the last ``TextDelta`` and in ``RunCompleted.output``.

The runtime state keeps the CLI session of each folder, so the next message of the session
continues the same CLI session (``claude --resume`` or ``codex exec resume``).

Before the CLI starts, the sama tool gate judges one call of ``coding_agent_run`` with the
agent, the repository, the task, and the pull request flag. ``ask`` (the default) ends the run
with ``ApprovalRequired``. The resume after an approval starts the CLI. The gate does not see
the tools that the CLI calls inside the sandbox (Section 15.4, assumptions).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final, Protocol

from sama.coding.agents import (
    CODING_AGENT_PLAN,
    CODING_AGENT_RUN,
    MAX_TIMEOUT_S,
    CodingAgent,
    CodingError,
    agent_for,
    coding_model,
    label,
)
from sama.coding.events import CliEvent, CliPlan, CliText, CliToolEnd, CliToolStart, CliUsage
from sama.coding.service import (
    CodingOutcome,
    CodingRequest,
    CodingService,
    render,
)
from sama.orchestrator.sandboxed import SandboxedRuntime
from sama.runtime.contract import (
    ApprovalDecision,
    ApprovalRequired,
    Capabilities,
    FailureCode,
    ResumeRequest,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunRequest,
    RunScope,
    RuntimeKind,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    Usage,
)
from sama.sandbox.base import Sandbox, SandboxError
from sama.tenancy.run_token import RunTokenClaims, RunTokenSigner
from sama.tools.repos import RepoError, parse_repo

log: Final = logging.getLogger("sama.coding")

PR_FLAG: Final = "--pr"
STATE_VERSION: Final = 1
MAX_STATE_FOLDERS: Final = 20
WALL_MARGIN_S: Final = 60.0
"""The CLI stops this long before the wall time of the run, so the diff and the pull request
fit in the run."""
GATE_PROMPT_CHARS: Final = 4_000
"""The task in the arguments that the gate records and the approval card shows."""

CAPABILITIES: Final = Capabilities(
    streaming=True,
    native_subagents=False,
    native_compaction=True,
    native_skills=False,
    native_file_checkpoints=False,
    resumable_sessions=True,
    mcp_client=False,
    manual_compaction=False,
    compaction_archive=False,
    mid_run_steer=False,
)
"""The CLIs compact their own context. Sama cannot ask them to compact now, and they do not
connect to the sama MCP server (Section 15.4)."""


class Verdict(Protocol):
    @property
    def action(self) -> str: ...

    @property
    def reason(self) -> str: ...


class CodingGate(Protocol):
    """``sama.gate.service.GateService.check``."""

    async def check(
        self,
        claims: RunTokenClaims,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        child: bool = False,
    ) -> Verdict: ...


@dataclass(frozen=True, slots=True)
class ParsedRequest:
    prompt: str
    repo: str | None = None
    open_pr: bool = False


_REPO_TOKEN = re.compile(
    r"^(?:(?:https://)?github\.com/[A-Za-z0-9][\w-]*/[\w.-]+?(?:\.git)?/?"
    r"|[A-Za-z0-9][\w-]*/[\w-]+)$"
)
"""A repository word: ``github.com/<owner>/<name>`` (with or without ``https://``), or a short
``<owner>/<name>`` without a dot, so that a path such as ``src/app.py`` is not a repository."""


def parse_request(text: str) -> ParsedRequest:
    """A message to a coding agent: ``[<owner>/<name>] <task> [--pr]``. The first word is the
    repository when it has the form of a GitHub repository (``_REPO_TOKEN``). ``--pr`` as the
    first or the last word asks for a pull request."""
    words = text.strip().split()
    open_pr = False
    if words and words[-1].lower() == PR_FLAG:
        open_pr, words = True, words[:-1]
    if words and words[0].lower() == PR_FLAG:
        open_pr, words = True, words[1:]
    repo: str | None = None
    if words and _REPO_TOKEN.match(words[0]):
        try:
            parse_repo(words[0])
        except RepoError:
            pass
        else:
            repo, words = words[0], words[1:]
    if not repo and not open_pr:
        return ParsedRequest(text.strip())
    rest = text.strip()
    # Keep the line breaks of the task: remove only the words that the parser took.
    if open_pr:
        rest = re.sub(r"(^\s*--pr\b|\s--pr\s*$)", "", rest, flags=re.IGNORECASE).strip()
    if repo:
        rest = rest[rest.find(repo) + len(repo) :].strip()
    return ParsedRequest(rest, repo, open_pr)


@dataclass(slots=True)
class _State:
    """The runtime state: the CLI session of each folder, the last folder, and the request that
    waits for an approval."""

    sessions: dict[str, str] = field(default_factory=dict)
    folder: str | None = None
    pending: dict[str, Any] | None = None
    totals: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Codex: the usage of each thread so far (``Usage`` as JSON)."""

    @classmethod
    def load(cls, raw: bytes | None) -> _State:
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            # The state of another runtime: start fresh.
            return cls()
        if not isinstance(data, dict) or data.get("v") != STATE_VERSION:
            return cls()
        sessions = data.get("sessions")
        folder = data.get("folder")
        pending = data.get("pending")
        totals = data.get("totals")
        return cls(
            {str(k): str(v) for k, v in sessions.items()} if isinstance(sessions, dict) else {},
            folder if isinstance(folder, str) else None,
            pending if isinstance(pending, dict) else None,
            {str(k): v for k, v in totals.items() if isinstance(v, dict)}
            if isinstance(totals, dict)
            else {},
        )

    def baseline(self, session: str | None) -> Usage | None:
        raw = self.totals.get(session or "")
        if raw is None:
            return None
        try:
            return Usage.model_validate(raw)
        except ValueError:
            return None

    def dump(self) -> bytes:
        sessions = dict(list(self.sessions.items())[-MAX_STATE_FOLDERS:])
        kept = set(sessions.values())
        totals = {key: value for key, value in self.totals.items() if key in kept}
        data: dict[str, Any] = {"v": STATE_VERSION, "sessions": sessions, "folder": self.folder}
        if totals:
            data["totals"] = totals
        if self.pending is not None:
            data["pending"] = self.pending
        return json.dumps(data, sort_keys=True).encode()


@dataclass(frozen=True, slots=True)
class CodingModePrep:
    """What coding mode (Section 15.5) gives a run before the CLI starts."""

    note: str = ""
    """Text after the task: the attached repositories, and the plan mode rules."""
    plan_mode: bool = False
    plan_args: tuple[str, ...] = ()


class CodingModeHooks(Protocol):
    """Coding mode for the runtimes ``claude_code`` and ``codex`` (Section 15.5). The control
    plane gives ``sama.coding_mode.harness.HarnessHooks``. A hook must not raise."""

    async def planning(self, scope: RunScope) -> bool:
        """True when the session of the run is in plan mode."""
        ...

    async def before(
        self, sandbox: Sandbox, scope: RunScope, token: str, agent: str, plan: bool
    ) -> CodingModePrep: ...

    async def plan(self, scope: RunScope, text: str) -> str:
        """Stores the plan of the run and returns a line for the user, or ""."""
        ...


class CodingAgentRuntime:
    """``AgentRuntime`` for ``claude_code`` and ``codex``. ``sandboxes`` gives the session
    sandbox and the run token, as for the other runtimes."""

    def __init__(
        self,
        *,
        kind: RuntimeKind,
        sandboxes: SandboxedRuntime,
        signer: RunTokenSigner,
        service: CodingService,
        gate: CodingGate | None = None,
        hooks: CodingModeHooks | None = None,
    ) -> None:
        agent = agent_for(kind)
        if agent is None:
            raise ValueError(f"{kind} is not a coding agent runtime")
        self._kind = kind
        self._agent = agent
        self._sandboxes = sandboxes
        self._signer = signer
        self._service = service
        self._gate = gate
        self._hooks = hooks
        self._active: dict[str, asyncio.Task[CodingOutcome]] = {}
        self._cancelled: set[str] = set()
        self._used: set[str] = set()

    @property
    def kind(self) -> RuntimeKind:
        return self._kind

    @property
    def agent(self) -> CodingAgent:
        return self._agent

    def capabilities(self) -> Capabilities:
        return CAPABILITIES

    def for_kind(self, kind: RuntimeKind) -> CodingAgentRuntime:
        return CodingAgentRuntime(
            kind=kind,
            sandboxes=self._sandboxes,
            signer=self._signer,
            service=self._service,
            gate=self._gate,
            hooks=self._hooks,
        )

    def set_hooks(self, hooks: CodingModeHooks | None) -> None:
        """Sets coding mode (Section 15.5). The control plane builds it after the runtime."""
        self._hooks = hooks

    async def cancel(self, run_id: str) -> None:
        self._cancelled.add(run_id)
        task = self._active.get(run_id)
        if task is not None:
            task.cancel()

    async def run(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        scope = request.scope
        state = _State.load(request.runtime_state)
        if request.compact is not None:
            yield RunFailed(
                code=FailureCode.INVALID_REQUEST,
                message=f"{label(self._agent)} compacts its own context. /compact does nothing.",
            )
            return
        if request.config.runtime is not self._kind:
            yield _invalid(f"config is for runtime {request.config.runtime}, not {self._kind}")
            return
        if scope.run_id in self._active:
            yield _invalid(f"run {scope.run_id} is already active")
            return
        parsed = parse_request(request.input)
        if not parsed.prompt:
            yield _invalid(f"give a task for {label(self._agent)}")
            return
        token = self._sandboxes.issue_token(scope, request.budget)
        claims = self._signer.verify(token)
        call_id = f"coding-{scope.run_id}"
        # Plan mode (Section 15.5): the CLI starts in its plan mode, and the gate judges
        # ``coding_agent_plan``, which plan mode does not block. No pull request.
        planning = self._hooks is not None and await self._hooks.planning(scope)
        tool = CODING_AGENT_PLAN if planning else CODING_AGENT_RUN
        arguments = {
            "agent": self._agent.value,
            "repo": parsed.repo or "",
            "prompt": parsed.prompt[:GATE_PROMPT_CHARS],
            "open_pr": parsed.open_pr and not planning,
        }
        if self._gate is not None:
            verdict = await self._gate.check(claims, call_id, tool, arguments)
            if scope.run_id in self._cancelled:
                # A cancel during the gate check: the CLI must not start.
                yield RunFailed(code=FailureCode.CANCELLED, message="cancelled")
                return
            if verdict.action == "deny":
                yield RunFailed(
                    code=FailureCode.TOOL_ERROR,
                    message=f"The tool gate refused {tool}: {verdict.reason}",
                    runtime_state=state.dump(),
                )
                return
            if verdict.action == "ask":
                state.pending = {
                    "call_id": call_id,
                    "prompt": parsed.prompt,
                    "repo": parsed.repo,
                    "open_pr": parsed.open_pr,
                    "plan": planning,
                }
                yield ApprovalRequired(
                    approval_id=call_id,
                    call_id=call_id,
                    tool_name=tool,
                    arguments=arguments,
                    reason=verdict.reason,
                    runtime_state=state.dump(),
                )
                return
        async for event in self._execute(request, state, parsed, token, claims, planning):
            yield event

    async def resume(self, request: ResumeRequest) -> AsyncIterator[RunEvent]:
        scope = request.scope
        state = _State.load(request.runtime_state)
        pending = state.pending
        problem = None
        if pending is None or pending.get("call_id") != request.approval_id:
            problem = f"unknown approval: {request.approval_id}"
        elif request.approval_id in self._used:
            problem = f"approval {request.approval_id} was already used"
        elif scope.run_id in self._cancelled:
            problem = f"run {scope.run_id} was cancelled"
        elif scope.run_id in self._active:
            problem = f"run {scope.run_id} is already active"
        if problem is not None or pending is None:
            yield _invalid(problem or "no request waits")
            return
        self._used.add(request.approval_id)
        state.pending = None
        if request.decision is ApprovalDecision.REJECT:
            why = f" ({request.reason})" if request.reason else ""
            yield RunCompleted(
                output=f"{label(self._agent)} did not run: the request was not approved{why}.",
                runtime_state=state.dump(),
            )
            return
        parsed = ParsedRequest(
            str(pending.get("prompt") or ""),
            pending.get("repo") or None,
            bool(pending.get("open_pr")),
        )
        token = self._sandboxes.issue_token(scope, request.budget)
        claims = self._signer.verify(token)
        run_request = RunRequest(
            scope=scope, input=parsed.prompt, config=request.config, budget=request.budget
        )
        planning = bool(pending.get("plan"))
        async for event in self._execute(run_request, state, parsed, token, claims, planning):
            yield event

    async def _execute(
        self,
        request: RunRequest,
        state: _State,
        parsed: ParsedRequest,
        token: str,
        claims: RunTokenClaims,
        planning: bool = False,
    ) -> AsyncIterator[RunEvent]:
        scope = request.scope
        run_id = scope.run_id
        model = coding_model(self._agent, request.config.model, self._service.prices)
        wall = request.budget.max_wall_time_s or MAX_TIMEOUT_S + WALL_MARGIN_S
        timeout_s = max(30.0, min(MAX_TIMEOUT_S, wall - WALL_MARGIN_S))
        events: asyncio.Queue[CliEvent] = asyncio.Queue()
        if run_id in self._cancelled:
            yield RunFailed(code=FailureCode.CANCELLED, message="cancelled")
            return

        async def job() -> CodingOutcome:
            sandbox = await self._sandboxes.acquire(scope, request.budget)
            try:
                prep = CodingModePrep(plan_mode=planning)
                if self._hooks is not None:
                    prep = await self._hooks.before(
                        sandbox, scope, token, self._agent.value, planning
                    )
                prompt = f"{parsed.prompt}\n\n{prep.note}" if prep.note else parsed.prompt
                folder = state.folder
                space_key = None
                if parsed.repo:
                    space_key = f"{sandbox.workdir.rstrip('/')}/{parse_repo(parsed.repo).name}"
                resume = state.sessions.get(space_key or folder or sandbox.workdir)
                coding = CodingRequest(
                    agent=self._agent,
                    prompt=prompt,
                    repo=parsed.repo,
                    # No pull request before a person approved the plan.
                    open_pr=parsed.open_pr and not prep.plan_mode,
                    resume=resume,
                    folder=folder,
                    baseline=state.baseline(resume),
                    plan_args=prep.plan_args if prep.plan_mode else (),
                )
                outcome = await self._service.run(
                    sandbox,
                    claims,
                    token,
                    coding,
                    model=model,
                    timeout_s=timeout_s,
                    on_event=events.put,
                )
                if resume and outcome.is_error and not outcome.worked:
                    # The saved CLI session is gone (a new sandbox): start a new one.
                    log.warning("coding resume failed; starting a new CLI session run=%s", run_id)
                    outcome = await self._service.run(
                        sandbox,
                        claims,
                        token,
                        replace(coding, resume=None, baseline=None),
                        model=model,
                        timeout_s=timeout_s,
                        on_event=events.put,
                    )
                return outcome
            finally:
                with contextlib.suppress(SandboxError):
                    await self._sandboxes.release(scope, sandbox)

        plan_text: list[str] = []
        task = asyncio.ensure_future(job())
        self._active[run_id] = task
        usage = Usage()
        try:
            while True:
                getter = asyncio.ensure_future(events.get())
                done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    break
                event = getter.result()
                if isinstance(event, CliUsage):
                    usage = event.usage
                if isinstance(event, CliPlan):
                    plan_text.append(event.text)
                mapped = _map(event, usage)
                if mapped is not None:
                    yield mapped
            while not events.empty():
                event = events.get_nowait()
                if isinstance(event, CliUsage):
                    usage = event.usage
                if isinstance(event, CliPlan):
                    plan_text.append(event.text)
                mapped = _map(event, usage)
                if mapped is not None:
                    yield mapped
            if task.cancelled() or run_id in self._cancelled:
                yield RunFailed(code=FailureCode.CANCELLED, message="cancelled", usage=usage)
                return
            error = task.exception()
            if isinstance(error, CodingError):
                yield RunFailed(
                    code=FailureCode.INVALID_REQUEST,
                    message=str(error),
                    usage=usage,
                    runtime_state=state.dump(),
                )
                return
            if isinstance(error, SandboxError):
                yield RunFailed(
                    code=FailureCode.SANDBOX_ERROR,
                    message=f"the sandbox failed: {error}",
                    retryable=True,
                    usage=usage,
                    runtime_state=state.dump(),
                )
                return
            if error is not None:
                log.error("coding run failed run=%s", run_id, exc_info=error)
                yield RunFailed(
                    code=FailureCode.INTERNAL,
                    message=f"the coding agent failed: {type(error).__name__}",
                    usage=usage,
                    runtime_state=state.dump(),
                )
                return
            outcome = task.result()
            usage = _max_usage(usage, outcome.usage)
            if outcome.workspace is not None:
                state.folder = outcome.workspace.path
                if outcome.session_id:
                    state.sessions.pop(outcome.workspace.path, None)
                    state.sessions[outcome.workspace.path] = outcome.session_id
                    if outcome.thread_usage is not None:
                        state.totals[outcome.session_id] = outcome.thread_usage.model_dump(
                            mode="json"
                        )
            text = render(outcome, for_agent=False)
            tail = _tail(outcome)
            if tail:
                yield TextDelta(text="\n\n" + tail, usage=usage)
            if self._hooks is not None and planning and not outcome.timed_out:
                # Plan mode (Section 15.5): the plan of Claude Code (``ExitPlanMode``), else the
                # answer of the CLI, is the plan that a person decides.
                plan = plan_text[-1] if plan_text else ("" if outcome.is_error else outcome.text)
                if plan.strip():
                    line = await self._hooks.plan(scope, plan)
                    if line:
                        yield TextDelta(text="\n\n" + line, usage=usage)
                        text = f"{text}\n\n{line}"
                    if plan_text:
                        # Claude Code ends its turn at ``ExitPlanMode``; that is not an error.
                        outcome.is_error = False
            if outcome.timed_out:
                yield RunFailed(
                    code=FailureCode.TIMEOUT,
                    message=text[:2_000],
                    usage=usage,
                    runtime_state=state.dump(),
                )
            elif outcome.is_error:
                budget = "budget_exceeded" in outcome.message or "402" in outcome.message
                yield RunFailed(
                    code=FailureCode.BUDGET_EXCEEDED if budget else FailureCode.MODEL_ERROR,
                    message=text[:2_000],
                    usage=usage,
                    runtime_state=state.dump(),
                )
            else:
                yield RunCompleted(output=text, usage=usage, runtime_state=state.dump())
        finally:
            if self._active.get(run_id) is task:
                del self._active[run_id]
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(task, timeout=10)


def _tail(outcome: CodingOutcome) -> str:
    """The part of the result that the stream did not show yet: the diff and the PR."""
    head = replace(outcome, text="", is_error=False, timed_out=False, message="")
    return render(head, for_agent=False)


def _max_usage(a: Usage, b: Usage) -> Usage:
    """Usage never goes down in a run (Section 5.1): the larger of the two counts."""
    return b if b.total_tokens >= a.total_tokens else a


def _map(event: CliEvent, usage: Usage) -> RunEvent | None:
    if isinstance(event, CliText):
        return TextDelta(text=event.text.rstrip() + "\n\n", usage=usage)
    if isinstance(event, CliToolStart):
        return ToolCallStarted(
            call_id=event.call_id,
            name=event.name,
            arguments={"summary": event.summary},
            usage=usage,
        )
    if isinstance(event, CliToolEnd):
        return ToolCallFinished(
            call_id=event.call_id,
            is_error=event.is_error,
            output_preview=event.preview,
            usage=usage,
        )
    return None


def _invalid(message: str) -> RunFailed:
    return RunFailed(code=FailureCode.INVALID_REQUEST, message=message, usage=Usage())


__all__ = [
    "CAPABILITIES",
    "CODING_AGENT_RUN",
    "CodingAgentRuntime",
    "ParsedRequest",
    "parse_request",
]
