"""One request to a coding agent (docs/architecture.md, Section 15.4).

``CodingService.run`` prepares the folder, starts the CLI in the session sandbox, gives each
event to a callback, and waits for the end of the turn. Then it reads the diff and, when the
request asks for it, opens a pull request. The runtime (``sama.coding.runtime``) and the tool
``coding_agent_run`` (``sama.coding.tool``) both use it.

A cancel of the task that calls ``run`` kills the CLI at once. A time limit does the same.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

from sama.coding.agents import (
    DEFAULT_TIMEOUT_S,
    MAX_PROMPT_CHARS,
    MAX_TIMEOUT_S,
    CodingAgent,
    CodingError,
    cli_command,
    label,
)
from sama.coding.events import (
    CliEvent,
    CliResult,
    CliSession,
    CliText,
    CliToolStart,
    CliUsage,
    CodexStream,
    stream_for,
)
from sama.coding.workspace import (
    Diff,
    Workspace,
    WorkspaceError,
    branch_name,
    diff,
    open_pull,
    prepare,
)
from sama.modelproxy.prices import PriceTable
from sama.runtime.contract import Usage
from sama.sandbox.base import Sandbox, SandboxError, SandboxProcess
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.github import GitHubTools
from sama.tools.repos import RepoCloner
from sama.vault.redact import redact

log: Final = logging.getLogger("sama.coding")

MIN_TIMEOUT_S: float = 30.0
KILL_WAIT_S: Final = 5.0
MAX_STDERR_CHARS: Final = 1_000

EventSink = Callable[[CliEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CodingRequest:
    agent: CodingAgent
    prompt: str
    repo: str | None = None
    open_pr: bool = False
    resume: str | None = None
    """The CLI session (Claude Code) or thread (Codex) to continue."""
    folder: str | None = None
    """The folder of the last request, for a request without ``repo``."""
    baseline: Usage | None = None
    """Codex: the usage of the thread at the end of the last request (its counts are for the
    whole thread)."""


@dataclass(slots=True)
class CodingOutcome:
    agent: CodingAgent
    model: str
    workspace: Workspace | None = None
    session_id: str | None = None
    text: str = ""
    is_error: bool = False
    message: str = ""
    usage: Usage = field(default_factory=Usage)
    diff: Diff | None = None
    pull: str = ""
    pull_error: str = ""
    timed_out: bool = False
    seconds: float = 0.0
    worked: bool = False
    """The CLI gave text or started a tool. A failed resume does neither."""
    thread_usage: Usage | None = None
    """Codex: the usage of the whole thread, the ``baseline`` of the next request."""

    @property
    def ok(self) -> bool:
        return not self.is_error and not self.timed_out


class CodingService:
    """Runs coding agent requests in session sandboxes.

    ``model_url`` is the root URL of the model proxy as the sandbox sees it (the runner gets
    the same URL). ``cloner`` clones a repository of a request. ``github`` opens the pull
    request."""

    def __init__(
        self,
        *,
        model_url: str,
        cloner: RepoCloner | None = None,
        github: GitHubTools | None = None,
        prices: PriceTable | None = None,
    ) -> None:
        self._model_url = model_url
        self._cloner = cloner
        self._github = github
        self._prices = prices or PriceTable.load()

    @property
    def prices(self) -> PriceTable:
        return self._prices

    async def run(
        self,
        sandbox: Sandbox,
        claims: RunTokenClaims,
        token: str,
        request: CodingRequest,
        *,
        model: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        on_event: EventSink | None = None,
    ) -> CodingOutcome:
        prompt = request.prompt.strip()
        if not prompt:
            raise CodingError("give the task for the coding agent")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise CodingError(f"the task has more than {MAX_PROMPT_CHARS:,} characters")
        timeout_s = max(MIN_TIMEOUT_S, min(float(timeout_s), MAX_TIMEOUT_S))
        started = time.monotonic()
        outcome = CodingOutcome(request.agent, model)
        try:
            space = await prepare(
                sandbox,
                claims,
                token,
                repo=request.repo,
                cloner=self._cloner,
                folder=request.folder,
            )
        except WorkspaceError as error:
            raise CodingError(str(error)) from None
        outcome.workspace = space
        command = cli_command(
            request.agent,
            prompt=prompt,
            model=model,
            model_url=self._model_url,
            token=token,
            resume=request.resume,
        )
        stream = stream_for(request.agent, model, self._prices, baseline=request.baseline)
        try:
            process = await sandbox.start(list(command.argv), env=dict(command.env), cwd=space.path)
        except SandboxError as error:
            raise CodingError(
                f"the sandbox could not start {label(request.agent)}: {error}"
            ) from None
        result: CliResult | None = None
        code: int | None = None
        try:
            async with asyncio.timeout(timeout_s):
                await process.write(command.stdin)
                await process.close_stdin()
                async for line in process.lines():
                    for event in stream.feed(line):
                        if isinstance(event, CliSession):
                            outcome.session_id = event.session_id
                        elif isinstance(event, CliText | CliToolStart):
                            outcome.worked = True
                        elif isinstance(event, CliUsage):
                            outcome.usage = event.usage
                        elif isinstance(event, CliResult):
                            result = event
                        if on_event is not None:
                            await on_event(event)
                code = await process.wait()
        except TimeoutError:
            outcome.timed_out = True
            outcome.message = f"{label(request.agent)} did not end in {int(timeout_s)} s"
        except SandboxError as error:
            outcome.is_error = True
            outcome.message = f"the sandbox failed: {error}"
        finally:
            # A cancel, a time limit, or an error: the CLI must not go on.
            await _kill(process)
        if isinstance(stream, CodexStream):
            outcome.thread_usage = stream.thread_usage
        if result is not None:
            outcome.text = result.text
            if result.is_error:
                outcome.is_error = True
                outcome.message = result.message or "the coding agent failed"
        elif not outcome.timed_out and not outcome.is_error:
            outcome.is_error = True
            detail = stream.last_error if isinstance(stream, CodexStream) else ""
            tail = _scrub(process.stderr_tail(), token)[-MAX_STDERR_CHARS:].strip()
            outcome.message = (
                f"{label(request.agent)} ended with no result (exit code {code})"
                + (f": {detail}" if detail else "")
                + (f"\n{tail}" if tail and not detail else "")
            )
        try:
            outcome.diff = await diff(sandbox, space)
        except WorkspaceError as error:
            log.warning("coding diff failed run=%s: %s", claims.run_id, error)
        if request.open_pr and outcome.ok:
            await self._pull(sandbox, claims, token, request, outcome)
        outcome.seconds = time.monotonic() - started
        log.info(
            "coding request run=%s agent=%s ok=%s seconds=%.1f files=%s",
            claims.run_id,
            request.agent.value,
            outcome.ok,
            outcome.seconds,
            None if outcome.diff is None else outcome.diff.files,
        )
        return outcome

    async def _pull(
        self,
        sandbox: Sandbox,
        claims: RunTokenClaims,
        token: str,
        request: CodingRequest,
        outcome: CodingOutcome,
    ) -> None:
        space = outcome.workspace
        if space is None or outcome.diff is None or outcome.diff.empty:
            outcome.pull_error = "the coding agent changed no file, so there is no pull request"
            return
        # The pull request is a write outside sama: redact the text first (the run token too).
        title = _title(_scrub(request.prompt, token))
        body = _scrub(pull_body(request, outcome, token), token)
        try:
            outcome.pull = await open_pull(
                sandbox,
                claims,
                token,
                space,
                github=self._github,
                branch=branch_name(request.agent.value, claims.run_id),
                title=title,
                body=body,
            )
        except WorkspaceError as error:
            outcome.pull_error = str(error)


def _scrub(text: str, token: str) -> str:
    """``text`` with the secrets that the redactor knows and the run token removed."""
    text = redact(text)
    return text.replace(token, "[redacted]") if token else text


def _title(prompt: str) -> str:
    first = " ".join(prompt.strip().splitlines()[0].split()) if prompt.strip() else "change"
    return first if len(first) <= 72 else first[:69] + "..."


def pull_body(request: CodingRequest, outcome: CodingOutcome, token: str = "") -> str:
    """The body of the pull request. Each field is scrubbed whole, before its cut: a cut secret
    no longer matches."""
    stat = _scrub(outcome.diff.stat, token) if outcome.diff is not None else ""
    summary = _scrub(outcome.text.strip(), token)[:8_000] or "(no summary)"
    prompt = _scrub(request.prompt.strip(), token)
    return "\n".join(
        [
            "## Summary",
            "",
            summary,
            "",
            "## Request",
            "",
            prompt[:4_000],
            "",
            "## Changes",
            "",
            "```text",
            stat[:8_000],
            "```",
            "",
            "---",
            f"Made by {label(request.agent)} ({outcome.model}) in a sama sandbox. A person "
            "reviews and merges this pull request. The agent cannot merge it.",
        ]
    )


async def _kill(process: SandboxProcess) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.shield(process.kill()), timeout=KILL_WAIT_S)


def render(outcome: CodingOutcome, *, for_agent: bool) -> str:
    """The text of the result. ``for_agent`` adds how to continue the CLI session."""
    name = label(outcome.agent)
    lines: list[str] = []
    if outcome.timed_out:
        lines.append(f"{name} stopped: {outcome.message}.")
    elif outcome.is_error:
        lines.append(f"{name} failed: {outcome.message}")
    if outcome.text.strip():
        lines += ["", outcome.text.strip()] if lines else [outcome.text.strip()]
    if outcome.workspace is not None:
        where = outcome.workspace.path
        changed = outcome.diff
        if changed is None:
            lines += ["", f"Folder: {where} (not a git repository, so no diff)."]
        elif changed.empty:
            lines += ["", f"No file changed in {where}."]
        else:
            lines += ["", f"Changes in {where} ({changed.files} files):", "```text"]
            lines += [changed.stat, "```", "", "```diff", changed.patch.rstrip(), "```"]
            if changed.cut:
                lines.append(
                    "The diff is cut. Run git diff in the folder to see all of it."
                    if for_agent
                    else "The diff is cut."
                )
    if outcome.pull:
        lines += ["", outcome.pull]
    elif outcome.pull_error:
        lines += ["", f"No pull request: {outcome.pull_error}."]
    if for_agent and outcome.session_id:
        lines += [
            "",
            f"{name} session: {outcome.session_id}. To continue this work with the same "
            f"context, call coding_agent_run again with session={outcome.session_id}.",
        ]
    return "\n".join(lines).strip()
