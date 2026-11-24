"""The file-delivery eval: when the user asks for a file, can the user open what sama gives?

The eval sends requests for files (a CSV, a Markdown report, a PDF, a chart, a zip, and "send me
that file again" in a second turn) to a sama API, one new session per case and runtime, and
checks what the last turn delivered (docs/architecture.md, Section 11.10):

- ``delivered``: a file of the session that the last turn made (``GET /sessions/{id}/media``),
  or an ``http(s)`` link in the last answer, returns HTTP 200 with the expected type and a
  non-empty body that starts with the expected bytes.
- ``dead_link``: nothing was delivered, and the last answer or the session has a file that does
  not open: a link to a file that fails, a relative link to a file name (``[a.csv](a.csv)``),
  or a stored file with a wrong type or no bytes.
- ``path_only``: nothing was delivered, and the last answer gives a sandbox path
  (``/home/user/work/a.csv``) as the place of the file.
- ``no_file``: nothing was delivered, and the answer has no path and no link to a file (for
  example, it pastes the content only).
- ``error``: nothing was delivered, and a run failed, timed out, or an API call failed.

The pass bar of M8.6 is 0 ``dead_link`` and 0 ``path_only`` cases over 6 cases x 2 runtimes x 3
runs. A path next to a delivered file is not a failure: the user can open the file.

Usage (the token file holds an access token of a test user)::

    uv run python -m sama.evals.file_delivery --api http://localhost:8090 \\
        --token-file /var/tmp/token --workspace <id> \\
        --profile claude=<profile id> --profile openai=<profile id> --out /var/tmp/fd-run1 \\
        --concurrency 2 --e2b-max-running 12

The runner approves the tools that a person who asks for a file approves (the shell, file
writes, code, web, and the file tools) with the scope ``session``, and denies every other
approval. It answers a ``clarify`` question, and a question in the answer text, once, with
the ``followup`` of the case.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit

import httpx

Outcome = Literal["delivered", "dead_link", "path_only", "no_file", "error"]
OUTCOMES: Final[tuple[Outcome, ...]] = ("delivered", "dead_link", "path_only", "no_file", "error")
FAILURES: Final = frozenset({"dead_link", "path_only"})
"""The outcomes that the pass bar counts."""

Kind = Literal["csv", "markdown", "pdf", "image", "zip"]


@dataclass(frozen=True, slots=True)
class Expect:
    """What a delivered file of a kind looks like."""

    mime_types: frozenset[str]
    magic: tuple[bytes, ...] = ()
    """The file starts with one of these byte strings. Empty: any start."""


EXPECT: Final[dict[str, Expect]] = {
    "csv": Expect(
        frozenset({"text/csv", "text/plain", "application/csv", "application/vnd.ms-excel"})
    ),
    "markdown": Expect(frozenset({"text/markdown", "text/plain", "text/x-markdown"})),
    "pdf": Expect(frozenset({"application/pdf"}), (b"%PDF",)),
    "image": Expect(
        frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
        (b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"GIF8"),
    ),
    "zip": Expect(frozenset({"application/zip", "application/x-zip-compressed"}), (b"PK\x03\x04",)),
}


@dataclass(frozen=True, slots=True)
class Case:
    """One request for a file. ``again`` is a second message in the same session: the eval
    then checks only what the second turn delivered."""

    id: str
    kind: Kind
    text: str
    again: str | None = None
    followup: str = "Use your best judgement for anything I did not say, and go ahead."


CASES: Final[tuple[Case, ...]] = (
    Case(
        "csv-countries",
        "csv",
        "Make a CSV file with the 5 most populous countries and their populations, and give"
        " me the file.",
    ),
    Case(
        "markdown-report",
        "markdown",
        "Read https://peps.python.org/pep-0008/ and write the main naming-convention rules as"
        " a short markdown report called naming.md. Give me the file.",
    ),
    Case(
        "pdf-checklist",
        "pdf",
        "Make a one-page PDF with a packing checklist for a weekend trip to Goa and send it to me.",
    ),
    Case(
        "chart-png",
        "image",
        "Make a bar chart as a PNG file of the average monthly rainfall in Mumbai (rough"
        " averages are fine) and send me the image file.",
    ),
    Case(
        "zip-haikus",
        "zip",
        "Create three small text files, a.txt, b.txt and c.txt, each with one haiku about the"
        " sea. Zip them into haikus.zip and send me the zip file.",
    ),
    Case(
        "resend-csv",
        "csv",
        "Make a CSV file with the 7 days of the week and their number (Monday is 1) and give"
        " me the file.",
        again="Send me that file again, please.",
    ),
)
"""6 requests. The first two are the requests of the staging sweep of 2026-11-23 (Q13 and
Q14) that got only a sandbox path or a relative link on both runtimes."""


# ---------------------------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------------------------

_SANDBOX_PATH: Final = re.compile(
    r"(?:sandbox:)?(?<![\w.])(?:/home/user|/tmp|/workspace|/mnt/data|/root|~)/[^\s`'\"<>)\]]+"
    r"|\bsandbox:/[^\s`'\"<>)\]]+"
)
_MD_LINK: Final = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_URL: Final = re.compile(r"https?://[^\s<>()\[\]`'\"]+")
_FILE_NAME: Final = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_NOT_RELATIVE: Final = re.compile(r"^(?:https?:|mailto:|tel:|data:|#|//)", re.I)
_LINE_SUFFIX: Final = re.compile(r"(?::\d+){1,2}$")


def sandbox_paths(text: str) -> list[str]:
    """The sandbox paths in ``text``, for example ``/home/user/work/a.csv``."""
    return [m.group(0).rstrip(".,;:") for m in _SANDBOX_PATH.finditer(text)]


def relative_file_links(text: str) -> list[str]:
    """The Markdown links in ``text`` whose target is a relative file name, not a URL
    (``[naming.md](naming.md)``, ``[naming.md](naming.md:1)``). The web chat opens them as a
    page of the web app: 404. A link to a sandbox path counts in ``sandbox_paths``."""
    found = []
    for match in _MD_LINK.finditer(text):
        target = match.group(1)
        if _NOT_RELATIVE.match(target) or _SANDBOX_PATH.match(target):
            continue
        name = _LINE_SUFFIX.sub("", target.split("?", 1)[0].split("#", 1)[0])
        if _FILE_NAME.search(name):
            found.append(target)
    return found


def urls(text: str) -> list[str]:
    """The ``http(s)`` links in ``text``, in order, without duplicates."""
    seen: dict[str, None] = {}
    for match in _URL.finditer(text):
        seen.setdefault(match.group(0).rstrip(".,;:!?*_"), None)
    return list(seen)


def looks_like_file_link(url: str, api: str | None = None) -> bool:
    """True for a link that names a file: a path with a file extension, a link to the sama API,
    or a sandbox host. A link to a page (a source of facts) is not a file link."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if api is not None and host == (urlsplit(api).hostname or "").lower():
        return True
    if host.endswith((".e2b.app", ".e2b.dev")):
        return True
    return bool(_FILE_NAME.search(parts.path)) and not parts.path.endswith(
        (".html", ".htm", ".php", ".asp", ".aspx")
    )


@dataclass(slots=True)
class Fetched:
    """The check of one delivered file: a stored file of the session or a link."""

    source: str
    """``media:<id>`` or the URL."""
    status: int
    mime_type: str
    size: int
    head: str
    """The first bytes, as hex, for the report."""
    ok: bool
    file_link: bool = True
    """False for a link to a page: the link is a source, not a delivery."""


def check(kind: str, status: int, content_type: str, body: bytes) -> bool:
    """True if a response is a good file of ``kind``: 200, the expected type, bytes, and the
    expected start."""
    expect = EXPECT[kind]
    mime = content_type.split(";", 1)[0].strip().lower()
    if status != 200 or not body or mime not in expect.mime_types:
        return False
    if expect.magic and not body.startswith(expect.magic):
        return False
    if kind in {"csv", "markdown"}:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return bool(text.strip()) and (kind != "csv" or "," in text)
    return True


def classify(
    answer: str, fetched: Sequence[Fetched], *, failed: bool = False, answered: bool = True
) -> Outcome:
    """The outcome of a case from the last answer and the checks of its delivered files."""
    if any(f.ok for f in fetched):
        return "delivered"
    if any(f.file_link for f in fetched) or relative_file_links(answer):
        return "dead_link"
    if sandbox_paths(answer):
        return "path_only"
    if failed or not answered:
        return "error"
    return "no_file"


def asks(text: str) -> bool:
    """True if an answer asks the user something before it makes the file."""
    return text.rstrip().endswith("?") and not sandbox_paths(text) and not urls(text)


# ---------------------------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------------------------

AUTO_APPROVE: Final = frozenset(
    {
        "Bash",
        "Write",
        "Edit",
        "MultiEdit",
        "Read",
        "WebFetch",
        "execute_code",
        "web_search",
        "web_extract",
        "image_generate",
        "file_send",
        "browser_open",
        "browser_snapshot",
        "browser_extract",
    }
)
"""The tools that the runner approves. A person who asks for a file approves these."""

_DONE: Final = frozenset({"completed", "failed", "cancelled"})
MAX_FETCH_BYTES: Final = 20 * 1024 * 1024


@dataclass(slots=True)
class Result:
    case: str
    kind: str
    runtime: str
    session_id: str | None = None
    outcome: Outcome = "error"
    answer: str = ""
    """The last answer of the turn that the eval checks."""
    answers: list[str] = field(default_factory=list)
    fetched: list[dict[str, Any]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    followups: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    error: str | None = None


class Api:
    """A small client of the sama HTTP API. It reads the token file before each request, so a
    separate process can refresh the token during a long eval."""

    def __init__(self, base: str, token_file: Path, *, timeout_s: float = 30.0) -> None:
        self.base = base.rstrip("/")
        self._token_file = token_file
        self._client = httpx.AsyncClient(
            base_url=self.base, timeout=timeout_s, headers={"User-Agent": "sama-eval/1.0"}
        )

    def token(self) -> str:
        return self._token_file.read_text(encoding="utf-8").strip()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, path: str, body: Any = None) -> Any:
        for attempt in range(3):
            try:
                response = await self._client.request(
                    method, path, json=body, headers={"Authorization": f"Bearer {self.token()}"}
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

    async def fetch(self, url: str) -> tuple[int, str, bytes]:
        """GETs a link, with the user token only for the API itself. Follows redirects."""
        same = urlsplit(url).hostname == urlsplit(self.base).hostname
        headers = {"Authorization": f"Bearer {self.token()}"} if same else {}
        try:
            async with self._client.stream(
                "GET", url, headers=headers, follow_redirects=True
            ) as response:
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_FETCH_BYTES:
                        break
                return response.status_code, response.headers.get("content-type", ""), body
        except httpx.HTTPError as error:
            return 0, type(error).__name__, b""


async def _settle(
    api: Api, session_id: str, case: Case, result: Result, deadline: float, answered: set[str]
) -> bool:
    """Waits until no run of the session is open. Approves, denies, and answers on the way.
    Returns False on a timeout (the open runs are cancelled)."""
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
            tool = approval["tool_name"]
            result.approvals.append(tool)
            if tool in AUTO_APPROVE:
                await api.call("POST", f"/approvals/{approval['id']}/approve", {"scope": "session"})
            else:
                await api.call(
                    "POST", f"/approvals/{approval['id']}/deny", {"reason": "the eval denies it"}
                )
        for question in await api.call("GET", f"/sessions/{session_id}/questions"):
            if question["id"] in answered:
                continue
            answered.add(question["id"])
            result.followups += 1
            await api.call(
                "POST",
                f"/sessions/{session_id}/questions/{question['id']}/answer",
                {"answers": [case.followup] * len(question["questions"])},
            )
        await asyncio.sleep(3)


async def _turn(
    api: Api,
    session_id: str,
    case: Case,
    text: str,
    result: Result,
    timeout_s: float,
    answered: set[str],
) -> tuple[bool, set[str]]:
    """Sends one message (and the follow-up once, if the agent asks), waits, and returns
    whether the turn ended in time and the ids of the runs that the turn made."""
    before = {run["id"] for run in await api.call("GET", f"/sessions/{session_id}/runs")}
    await api.call("POST", f"/sessions/{session_id}/messages", {"text": text})
    await asyncio.sleep(2)
    ok = await _settle(api, session_id, case, result, time.monotonic() + timeout_s, answered)
    answers = await _answers(api, session_id)
    if ok and answers and asks(answers[-1]):
        result.followups += 1
        await api.call("POST", f"/sessions/{session_id}/messages", {"text": case.followup})
        await asyncio.sleep(2)
        ok = await _settle(api, session_id, case, result, time.monotonic() + timeout_s, answered)
    runs = await api.call("GET", f"/sessions/{session_id}/runs")
    return ok, {run["id"] for run in runs} - before


async def run_case(
    api: Api,
    case: Case,
    runtime: str,
    *,
    workspace_id: str,
    profile_id: str,
    timeout_s: float,
) -> Result:
    result = Result(case=case.id, kind=case.kind, runtime=runtime)
    start = time.monotonic()
    failed = True
    try:
        session = await api.call(
            "POST",
            "/sessions",
            {
                "workspace_id": workspace_id,
                "agent_profile_id": profile_id,
                "title": f"eval file-delivery: {case.id}",
            },
        )
        result.session_id = session_id = session["id"]
        answered: set[str] = set()
        ok, turn_runs = await _turn(api, session_id, case, case.text, result, timeout_s, answered)
        if case.again is not None and ok:
            ok, turn_runs = await _turn(
                api, session_id, case, case.again, result, timeout_s, answered
            )
        result.answers = await _answers(api, session_id)
        result.answer = result.answers[-1] if result.answers else ""
        runs = await api.call("GET", f"/sessions/{session_id}/runs")
        failed = not ok or any(run["status"] != "completed" for run in runs)
        for run in sorted(runs, key=lambda run: run["created_at"]):
            result.cost_usd += float(run["cost_usd"])
            trace = await api.call("GET", f"/runs/{run['id']}/trace")
            result.tools.extend(call["tool_name"] for call in trace["tool_calls"])
            if run["status"] == "failed":
                result.error = f"{run['failure_code']}: {run['failure_message']}"
        if not ok:
            result.error = f"timed out after {timeout_s:.0f} s per turn"
        fetched = await _deliveries(api, session_id, case, result.answer, turn_runs)
        result.fetched = [asdict(f) for f in fetched]
        result.paths = sandbox_paths(result.answer) + relative_file_links(result.answer)
        result.outcome = classify(
            result.answer, fetched, failed=failed, answered=bool(result.answers)
        )
    except (httpx.HTTPError, KeyError, ValueError) as error:
        result.error = f"{type(error).__name__}: {error}"
        result.outcome = classify(result.answer, [], failed=True, answered=bool(result.answers))
    result.seconds = round(time.monotonic() - start, 1)
    return result


async def _deliveries(
    api: Api, session_id: str, case: Case, answer: str, turn_runs: set[str]
) -> list[Fetched]:
    """Checks the stored files that the checked turn made and the links of its last answer."""
    fetched: list[Fetched] = []
    for media in await api.call("GET", f"/sessions/{session_id}/media?limit=100"):
        if str(media["run_id"]) not in turn_runs:
            continue
        status, content_type, body = await api.fetch(
            f"{api.base}/sessions/{session_id}/media/{media['id']}"
        )
        fetched.append(_fetched(f"media:{media['id']}", case.kind, status, content_type, body))
    for url in urls(answer):
        status, content_type, body = await api.fetch(url)
        item = _fetched(url, case.kind, status, content_type, body)
        item.file_link = item.ok or looks_like_file_link(url, api.base)
        fetched.append(item)
    return fetched


def _fetched(source: str, kind: str, status: int, content_type: str, body: bytes) -> Fetched:
    return Fetched(
        source=source,
        status=status,
        mime_type=content_type.split(";", 1)[0].strip(),
        size=len(body),
        head=body[:8].hex(),
        ok=check(kind, status, content_type, body),
    )


async def _answers(api: Api, session_id: str) -> list[str]:
    messages = await api.call("GET", f"/sessions/{session_id}/messages")
    items = messages["items"] if isinstance(messages, dict) else messages
    return [str(m.get("text") or "") for m in items if m.get("role") == "assistant"]


def summary(results: Iterable[Result]) -> str:
    """A Markdown table: the count of each outcome per runtime, the failures of the pass bar,
    and the cost."""
    rows: dict[str, Counter[str]] = {}
    cost: Counter[str] = Counter()
    for result in results:
        rows.setdefault(result.runtime, Counter())[result.outcome] += 1
        cost[result.runtime] += round(result.cost_usd * 100)
    lines = [
        "| Runtime | Cases | Delivered | Dead link | Sandbox path only | No file | Error"
        " | Bar failures | Cost (USD) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for runtime, counts in sorted(rows.items()):
        cells = [str(counts[outcome]) for outcome in OUTCOMES]
        failures = sum(counts[outcome] for outcome in FAILURES)
        lines.append(
            f"| {runtime} | {sum(counts.values())} | {' | '.join(cells)} | {failures}"
            f" | {cost[runtime] / 100:.2f} |"
        )
    return "\n".join(lines)


class SandboxGuard(Protocol):
    """Keeps the eval from filling the E2B sandboxes that it shares with other sessions."""

    async def wait_for_room(self) -> None:
        """Returns when a new case may start."""

    async def release(self, session_id: str) -> None:
        """Stops the sandboxes of the session of a case that has ended."""


class E2BGuard:
    """A ``SandboxGuard`` for the E2B account of the API under test (``E2B_API_KEY``). Staging
    and production share one account with a limit of running sandboxes. Before a case, it
    waits while more than ``max_running`` sandboxes run. After a case, it kills only the
    sandboxes with the ``sama.session_id`` of that case: the session is new and belongs to the
    eval, so no other sandbox is touched."""

    def __init__(self, max_running: int, *, poll_s: float = 20.0) -> None:
        self._max = max_running
        self._poll_s = poll_s

    async def wait_for_room(self) -> None:
        while (running := await asyncio.to_thread(self._running)) > self._max:
            print(f"paused: {running} E2B sandboxes run (limit {self._max})", flush=True)
            await asyncio.sleep(self._poll_s)

    async def release(self, session_id: str) -> None:
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


def _done(path: Path) -> set[tuple[str, str]]:
    if not path.is_file():
        return set()
    return {(r.case, r.runtime) for r in load(path)}


async def run(args: argparse.Namespace, guard: SandboxGuard | None = None) -> list[Result]:
    profiles = dict(item.split("=", 1) for item in args.profile)
    cases = [case for case in CASES if not args.case or case.id in args.case]
    api = Api(args.api, Path(args.token_file))
    out = Path(args.out)
    done = _done(out / "results.jsonl") if args.resume else set()
    limit = asyncio.Semaphore(args.concurrency)

    async def one(case: Case, runtime: str, profile: str) -> None:
        async with limit:
            if guard is not None:
                await guard.wait_for_room()
            result = await run_case(
                api,
                case,
                runtime,
                workspace_id=args.workspace,
                profile_id=profile,
                timeout_s=args.timeout,
            )
            if guard is not None and result.session_id is not None:
                await guard.release(result.session_id)
        print(f"{runtime:8} {case.id:18} {result.outcome:10} {result.seconds:6.0f} s", flush=True)
        with (out / "results.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")

    try:
        await asyncio.gather(
            *(
                one(case, rt, profile)
                for case in cases
                for rt, profile in profiles.items()
                if (case.id, rt) not in done
            )
        )
    finally:
        await api.aclose()
    results = load(out / "results.jsonl")
    (out / "summary.md").write_text(summary(results) + "\n", encoding="utf-8")
    return results


def load(path: Path) -> list[Result]:
    """The results of a ``results.jsonl`` file, classified again from the stored answer and
    checks. A case that is in the file more than once keeps its last result."""
    results: dict[tuple[str, str], Result] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        result = Result(**json.loads(line))
        fetched = [Fetched(**item) for item in result.fetched]
        result.outcome = classify(
            result.answer,
            fetched,
            failed=result.error is not None,
            answered=bool(result.answers),
        )
        results[(result.case, result.runtime)] = result
    return list(results.values())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sama.evals.file_delivery")
    parser.add_argument("--rescore", nargs="+", help="results.jsonl files to classify again")
    parser.add_argument("--api", help="the sama API, e.g. http://localhost:8090")
    parser.add_argument("--token-file", help="a file with a user access token")
    parser.add_argument("--workspace")
    parser.add_argument("--profile", action="append", help="runtime label=agent profile id")
    parser.add_argument("--out", help="a folder for results.jsonl and summary.md")
    parser.add_argument("--case", action="append", help="run only this case id (repeatable)")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds per turn")
    parser.add_argument(
        "--resume", action="store_true", help="skip the cases that results.jsonl already has"
    )
    parser.add_argument(
        "--e2b-max-running",
        type=int,
        help="wait before a case while more E2B sandboxes run, and kill the sandboxes of each"
        " eval session when its case ends (needs E2B_API_KEY of the API under test)",
    )
    args = parser.parse_args(argv)
    if args.rescore:
        print(summary([r for path in args.rescore for r in load(Path(path))]))
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
