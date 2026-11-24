"""The learn eval: does sama keep a rule that the user teaches, and use it in a new chat?

The eval sends "learn this" messages to a sama API (docs/architecture.md, Section 18.5). Each
learn probe has two sessions:

1. The teach session gets the rule ("learn this: ..."). The agent must keep it in a sama store
   that later sessions read: the ``memory`` tool or ``skill_manage``. A file in the session
   sandbox, for example the auto memory files of Claude Code under ``~/.claude/projects``, is
   lost when the session ends.
2. A NEW session (same user, same workspace) gets a request that needs the rule. Each probe has
   markers that only an answer that follows the rule has (a fixed sign-off, a fixed heading, a
   question about pets). The answer text and the text of any question of the agent count.

Two more probes send one message each and check that the agent does not call a built-in tool of
the Claude CLI that sama does not run (``CronCreate``, ``WebSearch``, ...). Sama has its own
tools for these (``cronjob_manage``, ``web_search``).

Each probe gets a label: ``pass``, or the first failure in this order: ``error``,
``claude_memory`` (a Claude Code memory file was written), ``blocked_tool``, ``not_saved``,
``not_applied``. The checks are rules on the tool calls of the trace and regular expressions on
the text, so the same results always get the same labels (``--rescore``).

Usage (one run for one user; give a fresh user and workspace to each run, because the probes
save real memories and skills)::

    uv run python -m sama.evals.learn --api http://localhost:8090 \\
        --token-file /var/tmp/token --workspace <id> \\
        --profile claude=<profile id> --profile openai=<profile id> \\
        --arm main --run 1 --out /var/tmp/learn-eval --delete-jobs

The runner approves every approval with scope ``session`` (a person who teaches a rule allows
the save), and answers each ``clarify`` question once with the follow-up of the probe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import httpx

Kind = Literal["learn", "blocked"]
Label = Literal["pass", "error", "claude_memory", "blocked_tool", "not_saved", "not_applied"]
LABELS: Final[tuple[Label, ...]] = (
    "pass",
    "error",
    "claude_memory",
    "blocked_tool",
    "not_saved",
    "not_applied",
)


@dataclass(frozen=True, slots=True)
class Probe:
    """One probe. ``teach`` goes to the first session, ``ask`` to a new one (a learn probe), or
    ``ask`` alone (a blocked probe). Every pattern of ``markers`` must match the text of the new
    session. ``followup`` answers a question of the agent once."""

    id: str
    kind: Kind
    ask: str
    teach: str | None = None
    markers: tuple[str, ...] = ()
    followup: str = "That is all I need for now. Use your best judgement and keep it short."


TEACH_FOLLOWUP: Final = "Yes. Save it so that you use it in every new chat, then confirm."
"""The answer when the agent asks about the rule in the teach session (\"Shall I save it?\")."""

PROBES: Final[tuple[Probe, ...]] = (
    Probe(
        "learn-hotel-prices",
        "learn",
        teach=(
            "can you learn this, \n\nWhenever i ask you to see the hotel's price, always go to"
            " booking.com, expedia.com, hotel's website, google hotel's price and then extract"
            " the price of that hotel for all kinds of rooms for those dates\n\nYou will need the"
            " exact name of hotel, dates, number of people, pets (if any) before you can make a"
            " search"
        ),
        ask="check the price of the Taj Mahal Palace in Mumbai",
        # The rule asks for the pets before a search. A normal answer does not ask about pets.
        markers=(r"\bpets?\b",),
        followup="That is all I need for now. Do not search yet.",
    ),
    Probe(
        "learn-email-signoff",
        "learn",
        teach=(
            "Learn this for the future: whenever you write an email for me, end it with the"
            " sign-off 'Warm regards, Nikhil M.' and write the subject line in ALL CAPS."
        ),
        ask="Write a short email to my landlord asking him to fix the leaking kitchen tap.",
        markers=(r"warm\s+regards,?\s*\**\s*\n?\s*\**nikhil\s+m\b",),
    ),
    Probe(
        "learn-falcon-status",
        "learn",
        teach=(
            "Remember this rule: whenever I ask for a status update on Project Falcon, start the"
            " reply with the exact words 'FALCON STATUS:' and then give exactly 3 bullet points."
        ),
        ask=(
            "Give me a status update on Project Falcon: the API is done, the UI is half done,"
            " and testing has not started."
        ),
        markers=(r"(?-i:FALCON\s+STATUS:)",),
    ),
    Probe(
        "learn-daily-brief",
        "learn",
        teach=(
            "Learn this workflow: when I say 'daily brief', reply with the heading 'DAILY BRIEF',"
            " then (1) today's date, (2) one short motivational quote, and (3) a reminder to"
            " drink water, in that order."
        ),
        ask="daily brief",
        markers=(r"(?-i:DAILY\s+BRIEF)", r"\bwater\b"),
    ),
    Probe("blocked-reminder", "blocked", ask="Remind me in 10 minutes to stretch."),
    Probe(
        "blocked-web-search",
        "blocked",
        ask="Search the web: what is the latest stable version of Node.js? Answer in one line.",
    ),
)
"""4 learn probes (the first is the production message of 2026-11-23 that the Claude runtime
saved only in Claude Code memory files) and 2 blocked-tool probes."""

SAMA_STORES: Final = frozenset({"memory", "skill_manage"})
"""The sama tools that keep a rule for later sessions (Sections 8 and 9)."""

BLOCKED_CLI_TOOLS: Final = frozenset(
    {
        "CronCreate",
        "CronDelete",
        "CronList",
        "DesignSync",
        "EnterWorktree",
        "ExitWorktree",
        "ListAgents",
        "Monitor",
        "PushNotification",
        "RemoteTrigger",
        "ReportFindings",
        "ScheduleWakeup",
        "SendMessage",
        "Skill",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
        "WebSearch",
        "Workflow",
    }
)
"""Built-in tools of the Claude CLI that no sama toolset has. The gate denies them, so a call
costs a turn. Sama has its own tools for these features. ``AskUserQuestion`` is not here:
sama supports it (M8.3)."""

_MCP_PREFIX: Final = "mcp__sama__"
_FILE_TOOLS: Final = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_CLAUDE_MEMORY_PATH: Final = re.compile(r"/\.claude/(?:projects/[^\s'\"]*/)?memory\b|MEMORY\.md")
"""A path of the auto memory of Claude Code: ``~/.claude/projects/<cwd>/memory/*.md``."""
_SHELL_WRITE: Final = re.compile(r">|\btee\b|\bcp\b|\bmv\b|\bmkdir\b|\btouch\b|\bsed\s+-i")


def tool_name(name: str) -> str:
    """The sama name of a tool: the trace of the Claude runtime can keep the MCP prefix."""
    return name.removeprefix(_MCP_PREFIX)


def claude_memory_writes(calls: Iterable[Mapping[str, Any]]) -> list[str]:
    """The calls that wrote a Claude Code memory file, as ``"<tool> <path or command>"``. A
    denied call counts too: the agent chose the wrong store."""
    found: list[str] = []
    for call in calls:
        name = tool_name(str(call.get("tool_name", "")))
        arguments = call.get("arguments") or {}
        if name in _FILE_TOOLS:
            path = str(arguments.get("file_path") or arguments.get("notebook_path") or "")
            if _CLAUDE_MEMORY_PATH.search(path):
                found.append(f"{name} {path}")
        elif name == "Bash":
            command = str(arguments.get("command") or "")
            if _CLAUDE_MEMORY_PATH.search(command) and _SHELL_WRITE.search(command):
                found.append(f"Bash {command[:160]}")
    return found


def blocked_calls(calls: Iterable[Mapping[str, Any]]) -> list[str]:
    """The names of the calls to built-in Claude CLI tools that sama does not run."""
    names = (tool_name(str(call.get("tool_name", ""))) for call in calls)
    return [name for name in names if name in BLOCKED_CLI_TOOLS]


def saved_via(calls: Iterable[Mapping[str, Any]]) -> list[str]:
    """The sama stores that a call wrote to and that did not fail or get denied."""
    return sorted(
        {
            tool_name(str(call.get("tool_name", "")))
            for call in calls
            if tool_name(str(call.get("tool_name", ""))) in SAMA_STORES
            and call.get("status") not in ("failed", "denied")
        }
    )


def applied(probe: Probe, texts: Sequence[str]) -> bool:
    """True if the text of the new session has every marker of the probe."""
    text = "\n".join(texts).replace("\u2019", "'")
    return bool(probe.markers) and all(re.search(m, text, re.I) for m in probe.markers)


def label(result: Result) -> Label:
    """The label of a probe from its stored result."""
    if result.error is not None:
        return "error"
    if result.claude_memory:
        return "claude_memory"
    if result.blocked:
        return "blocked_tool"
    if result.kind == "blocked":
        return "pass"
    if not result.saved_via:
        return "not_saved"
    return "pass" if result.applied else "not_applied"


@dataclass(slots=True)
class Result:
    probe: str
    kind: str
    runtime: str
    arm: str = ""
    run: str = ""
    label: str = "error"
    teach_session: str | None = None
    ask_session: str | None = None
    teach_tools: list[str] = field(default_factory=list)
    ask_tools: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    """Every tool call of both sessions: ``tool_name``, ``arguments``, ``status``, ``session``."""
    teach_answers: list[str] = field(default_factory=list)
    ask_answers: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    """The questions of the agent in the new session (``clarify`` or ``AskUserQuestion``)."""
    approvals: list[str] = field(default_factory=list)
    saved_via: list[str] = field(default_factory=list)
    claude_memory: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    applied: bool = False
    cost_usd: float = 0.0
    seconds: float = 0.0
    error: str | None = None


def score(result: Result) -> Result:
    """Fills the checks and the label of ``result`` from its calls and texts."""
    probe = next((p for p in PROBES if p.id == result.probe), None)
    teach_calls = [c for c in result.calls if c.get("session") == "teach"]
    result.saved_via = saved_via(teach_calls)
    result.claude_memory = claude_memory_writes(result.calls)
    result.blocked = blocked_calls(result.calls)
    result.applied = probe is not None and applied(probe, [*result.ask_answers, *result.questions])
    result.label = label(result)
    return result


# ---------------------------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------------------------

_DONE: Final = frozenset({"completed", "failed", "cancelled"})


class Api:
    """A small client of the sama HTTP API. It reads the token file before each request, so a
    separate process can refresh the token during a long eval."""

    def __init__(self, base: str, token_file: Path, *, timeout_s: float = 30.0) -> None:
        self._token_file = token_file
        self._client = httpx.AsyncClient(
            base_url=base.rstrip("/"), timeout=timeout_s, headers={"User-Agent": "sama-eval/1.0"}
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, path: str, body: Any = None) -> Any:
        token = self._token_file.read_text(encoding="utf-8").strip()
        for attempt in range(3):
            try:
                response = await self._client.request(
                    method, path, json=body, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if response.status_code >= 500 and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            response.raise_for_status()
            return response.json() if response.content else None
        raise RuntimeError("unreachable")


@dataclass(slots=True)
class _Turn:
    """What happened in one session: the questions of the agent and the approvals."""

    questions: list[str] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    answered: set[str] = field(default_factory=set)


async def _settle(api: Api, session_id: str, followup: str, turn: _Turn, deadline: float) -> bool:
    """Waits until no run of the session is open. Approves each approval (scope ``session``)
    and answers each question once. Returns False on a timeout (the open runs are cancelled)."""
    while True:
        runs = await api.call("GET", f"/sessions/{session_id}/runs")
        open_runs = [run for run in runs if run["status"] not in _DONE]
        if not open_runs:
            return True
        if time.monotonic() > deadline:
            for run in open_runs:
                await api.call("POST", f"/runs/{run['id']}/cancel")
            return False
        for approval in await api.call("GET", f"/sessions/{session_id}/approvals"):
            turn.approvals.append(approval["tool_name"])
            await api.call("POST", f"/approvals/{approval['id']}/approve", {"scope": "session"})
        for question in await api.call("GET", f"/sessions/{session_id}/questions"):
            if question["id"] in turn.answered:
                continue
            turn.answered.add(question["id"])
            turn.questions.extend(json.dumps(q, ensure_ascii=False) for q in question["questions"])
            await api.call(
                "POST",
                f"/sessions/{session_id}/questions/{question['id']}/answer",
                {"answers": [followup] * len(question["questions"])},
            )
        await asyncio.sleep(3)


async def _answers(api: Api, session_id: str) -> list[str]:
    messages = await api.call("GET", f"/sessions/{session_id}/messages")
    items = messages["items"] if isinstance(messages, dict) else messages
    return [str(m.get("text") or "") for m in items if m.get("role") == "assistant"]


def _asks(text: str) -> bool:
    return "?" in text


async def _session(
    api: Api,
    text: str,
    followup: str,
    *,
    title: str,
    workspace_id: str,
    profile_id: str,
    timeout_s: float,
    asked: Callable[[list[str]], bool],
) -> tuple[str, bool, list[str], _Turn]:
    """Makes a session, sends ``text``, and waits. When ``asked`` says that the last answer asks
    the user something, it sends ``followup`` once."""
    session = await api.call(
        "POST",
        "/sessions",
        {"workspace_id": workspace_id, "agent_profile_id": profile_id, "title": title},
    )
    session_id = str(session["id"])
    turn = _Turn()
    start = time.monotonic()
    await api.call("POST", f"/sessions/{session_id}/messages", {"text": text})
    await asyncio.sleep(2)
    ok = await _settle(api, session_id, followup, turn, start + timeout_s)
    answers = await _answers(api, session_id)
    if ok and answers and asked(answers):
        await api.call("POST", f"/sessions/{session_id}/messages", {"text": followup})
        await asyncio.sleep(2)
        ok = await _settle(api, session_id, followup, turn, start + 2 * timeout_s)
        answers = await _answers(api, session_id)
    return session_id, ok, answers, turn


async def _collect(api: Api, session_id: str, tag: str, result: Result) -> list[str]:
    """Adds the tool calls and the cost of the runs of a session. Returns the failures."""
    failures = []
    runs = await api.call("GET", f"/sessions/{session_id}/runs")
    for run in sorted(runs, key=lambda run: run["created_at"]):
        result.cost_usd += float(run.get("cost_usd") or 0)
        if run["status"] == "failed":
            failures.append(f"{run.get('failure_code')}: {run.get('failure_message')}")
        trace = await api.call("GET", f"/runs/{run['id']}/trace")
        for call in trace["tool_calls"]:
            result.calls.append(
                {
                    "session": tag,
                    "tool_name": tool_name(call["tool_name"]),
                    "arguments": call.get("arguments") or {},
                    "status": call.get("status"),
                }
            )
    return failures


async def run_probe(
    api: Api,
    probe: Probe,
    runtime: str,
    *,
    workspace_id: str,
    profile_id: str,
    timeout_s: float,
    arm: str = "",
    run: str = "",
) -> Result:
    result = Result(probe=probe.id, kind=probe.kind, runtime=runtime, arm=arm, run=run)
    start = time.monotonic()
    failures: list[str] = []
    try:
        if probe.teach is not None:
            session_id, ok, answers, turn = await _session(
                api,
                probe.teach,
                TEACH_FOLLOWUP,
                title=f"eval learn: {probe.id} (teach)",
                # Ask again only when the agent saved nothing and asked something.
                asked=lambda a: _asks(a[-1]),
                workspace_id=workspace_id,
                profile_id=profile_id,
                timeout_s=timeout_s,
            )
            result.teach_session, result.teach_answers = session_id, answers
            result.approvals += turn.approvals
            failures += await _collect(api, session_id, "teach", result)
            if not ok:
                failures.append(f"teach timed out after {timeout_s:.0f} s")
        session_id, ok, answers, turn = await _session(
            api,
            probe.ask,
            probe.followup,
            title=f"eval learn: {probe.id}",
            # A blocked probe gets no second message. A learn probe sends the follow-up once
            # when the agent asks in its text (the hotel rule asks for the dates and pets).
            asked=lambda a: probe.kind == "learn" and _asks(a[-1]) and not applied(probe, a),
            workspace_id=workspace_id,
            profile_id=profile_id,
            timeout_s=timeout_s,
        )
        result.ask_session, result.ask_answers = session_id, answers
        result.questions, result.approvals = turn.questions, result.approvals + turn.approvals
        failures += await _collect(api, session_id, "ask", result)
        if not ok:
            failures.append(f"ask timed out after {timeout_s:.0f} s")
        if failures:
            result.error = "; ".join(failures)[:500]
    except (httpx.HTTPError, KeyError, ValueError) as error:
        result.error = f"{type(error).__name__}: {error}"
    result.teach_tools = [c["tool_name"] for c in result.calls if c["session"] == "teach"]
    result.ask_tools = [c["tool_name"] for c in result.calls if c["session"] == "ask"]
    result.seconds = round(time.monotonic() - start, 1)
    return score(result)


class SandboxGuard(Protocol):
    """Keeps the eval from filling sandboxes that it shares with other users."""

    async def wait_for_room(self) -> None:
        """Returns when a new probe may start."""

    async def release(self, session_ids: Sequence[str]) -> None:
        """Stops the sandboxes of the sessions of a probe that has ended."""


class E2BGuard:
    """A ``SandboxGuard`` for the E2B account of the API under test (``E2B_API_KEY``). Staging
    and production share one account with a limit of running sandboxes (Section 18.5). Before a
    probe, it waits while more than ``max_running`` sandboxes run. After a probe, it kills only
    the sandboxes with the ``sama.session_id`` of the sessions of that probe."""

    def __init__(self, max_running: int, *, poll_s: float = 20.0) -> None:
        self._max = max_running
        self._poll_s = poll_s

    async def wait_for_room(self) -> None:
        while (running := await asyncio.to_thread(self._running)) > self._max:
            print(f"paused: {running} E2B sandboxes run (limit {self._max})", flush=True)
            await asyncio.sleep(self._poll_s)

    async def release(self, session_ids: Sequence[str]) -> None:
        for session_id in session_ids:
            await asyncio.to_thread(self._kill, session_id)

    def _running(self) -> int:
        from e2b import Sandbox, SandboxQuery, SandboxState

        pages = Sandbox.list(query=SandboxQuery(state=[SandboxState.RUNNING]), limit=100)
        count = len(pages.next_items())
        while pages.has_next:
            count += len(pages.next_items())
        return count

    def _kill(self, session_id: str) -> None:
        from e2b import Sandbox, SandboxQuery

        pages = Sandbox.list(query=SandboxQuery(metadata={"sama.session_id": session_id}))
        for sandbox in pages.next_items():
            if sandbox.metadata.get("sama.session_id") == session_id:
                Sandbox.kill(sandbox.sandbox_id)


async def delete_jobs(api: Api, workspace_id: str, since: datetime) -> int:
    """Deletes the jobs of the org of the workspace that were made after ``since`` (the
    reminder probe makes one). Use it only with an org that belongs to the eval."""
    me = await api.call("GET", "/me")
    org = next(
        o for o in me["orgs"] if any(w["id"] == workspace_id for w in o.get("workspaces", []))
    )
    jobs = await api.call("GET", f"/orgs/{org['id']}/jobs")
    items: list[dict[str, Any]] = (
        jobs.get("jobs") or jobs.get("items") or [] if isinstance(jobs, dict) else jobs or []
    )
    count = 0
    for job in items:
        made = datetime.fromisoformat(str(job["created_at"]).replace("Z", "+00:00"))
        if made >= since:
            await api.call("DELETE", f"/orgs/{org['id']}/jobs/{job['id']}")
            count += 1
    return count


async def run(args: argparse.Namespace, guard: SandboxGuard | None = None) -> list[Result]:
    profiles = dict(item.split("=", 1) for item in args.profile or [])
    probes = [probe for probe in PROBES if not args.probe or probe.id in args.probe]
    api = Api(args.api, Path(args.token_file))
    out = Path(args.out)
    limit = asyncio.Semaphore(args.concurrency)
    since = datetime.now(UTC)

    async def one(probe: Probe, runtime: str, profile: str) -> None:
        async with limit:
            if guard is not None:
                await guard.wait_for_room()
            result = await run_probe(
                api,
                probe,
                runtime,
                workspace_id=args.workspace,
                profile_id=profile,
                timeout_s=args.timeout,
                arm=args.arm,
                run=args.run,
            )
            if guard is not None:
                await guard.release(
                    [s for s in (result.teach_session, result.ask_session) if s is not None]
                )
        print(
            f"{args.arm:8} {args.run:4} {runtime:8} {probe.id:22} {result.label:14}"
            f" {result.seconds:6.0f} s ${result.cost_usd:.3f}",
            flush=True,
        )
        with (out / "results.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")

    try:
        await asyncio.gather(
            *(one(probe, rt, profile) for probe in probes for rt, profile in profiles.items())
        )
        if args.delete_jobs:
            print(f"deleted {await delete_jobs(api, args.workspace, since)} jobs", flush=True)
    finally:
        await api.aclose()
    results = rescore(out / "results.jsonl")
    (out / "summary.md").write_text(summary(results) + "\n", encoding="utf-8")
    return results


def rescore(path: Path) -> list[Result]:
    """The results of a ``results.jsonl`` file, scored again with the current rules."""
    return [score(Result(**json.loads(line))) for line in path.read_text("utf-8").splitlines()]


def summary(results: Iterable[Result]) -> str:
    """Markdown tables per arm and runtime: the labels, the counts of the checks, and, for each
    learn probe, how many runs applied the rule in the new chat."""
    groups: dict[tuple[str, str], list[Result]] = defaultdict(list)
    for result in results:
        groups[(result.arm, result.runtime)].append(result)
    lines = [
        "| Arm | Runtime | Runs | Probes | Pass | Saved in sama (learn) | Applied in new chat"
        " (learn) | Claude Code memory writes | Blocked CLI tool calls | Errors | Cost (USD) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    per_probe: dict[tuple[str, str], Counter[str]] = {}
    for (arm, runtime), items in sorted(groups.items()):
        learn = [r for r in items if r.kind == "learn"]
        labels = Counter(r.label for r in items)
        lines.append(
            f"| {arm} | {runtime} | {len({r.run for r in items})} | {len(items)}"
            f" | {labels['pass']} | {sum(bool(r.saved_via) for r in learn)}/{len(learn)}"
            f" | {sum(r.applied for r in learn)}/{len(learn)}"
            f" | {sum(len(r.claude_memory) for r in items)}"
            f" | {sum(len(r.blocked) for r in items)} | {labels['error']}"
            f" | {sum(r.cost_usd for r in items):.2f} |"
        )
        counts = per_probe.setdefault((arm, runtime), Counter())
        for r in items:
            counts[f"{r.probe}:n"] += 1
            counts[f"{r.probe}:ok"] += r.label == "pass"
    probe_ids = [p.id for p in PROBES]
    lines += [
        "",
        "Passes per probe (pass / runs):",
        "",
        "| Arm | Runtime | " + " | ".join(probe_ids) + " |",
        "|---|---|" + "---|" * len(probe_ids),
    ]
    for (arm, runtime), counts in sorted(per_probe.items()):
        cells = [
            f"{counts[f'{p}:ok']}/{counts[f'{p}:n']}" if counts[f"{p}:n"] else "-"
            for p in probe_ids
        ]
        lines.append(f"| {arm} | {runtime} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sama.evals.learn")
    parser.add_argument("--rescore", help="score a results.jsonl file again and print the summary")
    parser.add_argument("--api", help="the sama API, for example a local control plane")
    parser.add_argument("--token-file", help="a file with a user access token")
    parser.add_argument("--workspace")
    parser.add_argument("--profile", action="append", help="runtime label=agent profile id")
    parser.add_argument("--out", help="a folder for results.jsonl and summary.md")
    parser.add_argument("--probe", action="append", help="run only this probe id (repeatable)")
    parser.add_argument("--arm", default="", help="a label for the code under test, e.g. main")
    parser.add_argument("--run", default="", help="a label for this run, e.g. 1")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds per session")
    parser.add_argument(
        "--delete-jobs",
        action="store_true",
        help="at the end, delete the jobs that the run made in the org of the workspace",
    )
    parser.add_argument(
        "--e2b-max-running",
        type=int,
        help="wait before a probe while more E2B sandboxes run, and kill the sandboxes of the"
        " sessions of each probe when it ends (needs E2B_API_KEY of the API under test)",
    )
    args = parser.parse_args(argv)
    if args.rescore:
        print(summary(rescore(Path(args.rescore))))
        return 0
    if not (args.api and args.token_file and args.workspace and args.profile and args.out):
        parser.error("give --api, --token-file, --workspace, --profile, and --out")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    guard = None if args.e2b_max_running is None else E2BGuard(args.e2b_max_running)
    results = asyncio.run(run(args, guard))
    print(summary(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
