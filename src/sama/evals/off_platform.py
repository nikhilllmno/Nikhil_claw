"""The off-platform eval: does sama get a price itself, or does it send the user to another site?

The eval sends real price questions (hotels, flights, products) to a sama API, one new session
per question and runtime, and classifies each answer (docs/architecture.md, Section 18.4):

- ``off_platform``: an answer tells the user to get the data somewhere else ("enter your dates
  on wyndhamhotels.com", "call the hotel", "I cannot check live prices").
- ``answered``: no answer sends the user away, and the last answer has a price.
- ``no_price``: no answer sends the user away, but the last answer has no price.
- ``error``: no answer sends the user away, and a run failed, timed out, or an API call
  failed.

The classifier is a set of regular expressions, so the same answer always gets the same label.
It is strict on purpose: a source link or an offer ("Do you want me to try the Wyndham site
next?") is not off-platform; an instruction to the user to go and look is.

Usage (staging; the token file holds a Supabase access token of a test user)::

    uv run python -m sama.evals.off_platform --api https://sama-staging.fly.dev \\
        --token-file /var/tmp/token --workspace <id> \\
        --profile claude=<profile id> --profile openai=<profile id> --out /var/tmp/eval-before

The runner approves the browser and web tools of the run (scope ``session``) and denies every
other approval. It answers a ``clarify`` question, and a question in the answer text, once,
with the ``followup`` of the case.
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

import httpx

Outcome = Literal["off_platform", "answered", "no_price", "error"]
OUTCOMES: Final[tuple[Outcome, ...]] = ("off_platform", "answered", "no_price", "error")


@dataclass(frozen=True, slots=True)
class Case:
    """One question of the eval. ``followup`` answers a question of the agent once."""

    id: str
    kind: Literal["hotel", "flight", "product", "other"]
    text: str
    followup: str = "Use your best judgement for anything I did not say, and go ahead."


CASES: Final[tuple[Case, ...]] = (
    Case(
        "wyndham-vague",
        "hotel",
        "can you check the price of wyndam",
        "Reside Navy Yard, a Wyndham Residence, in Washington DC. Check in next Monday,"
        " check out next Wednesday, 2 adults, 1 room.",
    ),
    Case(
        "wyndham-dc-5-nights",
        "hotel",
        "check the price of hotel wyndham in washington DC for the next 5 days",
        "Any Wyndham in central Washington DC, 2 adults, 1 room, check in tomorrow.",
    ),
    Case(
        "reside-navy-yard",
        "hotel",
        "What is the price of Reside Navy Yard, a Wyndham Residence, Washington DC, for 2"
        " nights starting next Friday? 2 adults.",
    ),
    Case(
        "sheraton-palo-alto",
        "hotel",
        "How much is the Sheraton Palo Alto for 3 nights from the 10th of next month,"
        " 2 adults, 1 room?",
    ),
    Case(
        "hyatt-sf",
        "hotel",
        "Price of Hyatt Regency San Francisco for next Saturday night, 1 adult",
    ),
    Case(
        "times-square-cheapest",
        "hotel",
        "Find me the cheapest hotel near Times Square for 2 nights starting two weeks from"
        " today, 2 adults, and tell me the price.",
    ),
    Case(
        "taj-mumbai",
        "hotel",
        "How much does a room at the Taj Mahal Palace Mumbai cost for 2 nights from next Thursday?",
        "2 adults, 1 room, the cheapest room type.",
    ),
    Case(
        "flight-sfo-jfk",
        "flight",
        "What's the cheapest one-way economy flight from SFO to JFK two weeks from today?",
    ),
    Case(
        "flight-del-bom",
        "flight",
        "Flight price from Delhi to Mumbai next Monday morning, 1 adult economy",
    ),
    Case(
        "flight-lon-par-return",
        "flight",
        "How much is a return flight London to Paris, leaving the 20th of next month and"
        " coming back a week later? 1 adult, economy.",
    ),
    Case(
        "iphone-apple",
        "product",
        "What is the current price of the iPhone 17 Pro 256GB on apple.com (US)?",
    ),
    Case(
        "sony-amazon",
        "product",
        "Check the price of Sony WH-1000XM6 headphones on amazon.com",
    ),
    Case(
        "switch-bestbuy",
        "product",
        "How much does a Nintendo Switch 2 cost at Best Buy right now?",
    ),
    Case(
        "macbook-india",
        "product",
        "Price of the cheapest MacBook Air 13 inch in India right now?",
    ),
    Case(
        "airpods-compare",
        "product",
        "Compare the price of AirPods Pro 3 on Amazon US and Best Buy",
    ),
    Case(
        "airbnb-lisbon",
        "other",
        "How much is an Airbnb in central Lisbon for 2 people for 3 nights starting the"
        " 18th of next month? Give me a few options with prices.",
    ),
    Case(
        "car-rental-lax",
        "other",
        "Price of a compact rental car at LAX for 3 days from next Thursday",
    ),
    Case(
        "eurostar",
        "other",
        "What's the price of a Eurostar ticket from London to Paris on the 20th of next"
        " month, 1 adult, standard class, morning?",
    ),
)
"""18 questions. Relative dates keep the eval valid on any day."""


# ---------------------------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------------------------

_SITE: Final = r"(?:[a-z0-9-]+\.)+(?:com|net|org|in|co\.uk|co|io|travel|fr|de)\b"
_PLACE: Final = (
    rf"(?:{_SITE}"
    r"|(?:the\s+)?official\s+(?:site|website|page|app|store)"
    r"|the\s+(?:hotel|airline|retailer|store|seller|property|host)(?:'s)?"
    r"(?:\s+(?:own\s+)?(?:site|website|page|app|directly))?"
    r"|(?:the\s+)?(?:site|website|app)\s+(?:directly|yourself)"
    r"|google\s+(?:hotels|flights)|kayak|expedia|booking\.com|skyscanner|amazon|best\s*buy"
    r"|airbnb|trivago|hotels\.com|priceline|momondo|makemytrip|cleartrip|eurostar"
    r"|(?:their|its)\s+(?:site|website|app|page)"
    r"|(?:this|that|the)\s+(?:link|url)(?:\s+below)?)"
)
_ACTION: Final = (
    r"(?:check|visit|open|go\s+to|head\s+to|search|look\s+(?:it\s+)?up|enter|call|contact|use|try"
    r"|ring|phone)"
)
_USER: Final = (
    r"(?:you(?:'ll|'d)?\s+(?:can|could|should|must|may\s+want\s+to|might\s+want\s+to"
    r"|will\s+need\s+to|need\s+to|would\s+need\s+to|have\s+to|would\s+have\s+to)"
    r"|i\s+(?:recommend|suggest|(?:would|'d)\s+(?:recommend|suggest))(?:\s+(?:that\s+)?you)?"
    r"|please|it'?s\s+best\s+to|best\s+to|we\s+recommend)"
)

_ORDER: Final = "an order to the user to go to a place"

_DEFLECTIONS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "tells the user to go to a place",
        re.compile(
            rf"\b{_USER}\s+(?:directly\s+)?{_ACTION}\w*\b(?!-)[^.\n]{{0,60}}?{_PLACE}", re.I
        ),
    ),
    (
        _ORDER,
        re.compile(
            r"(?:^[ \t]*(?:[-*•]|\d+[.)])?|[.!?:;,]|\b(?:or|and|then|also))[ \t]*(?:\*\*)?"
            rf"{_ACTION}\b(?!-)(?!\s+link)[^.\n]{{0,60}}?{_PLACE}",
            re.I | re.M,
        ),
    ),
    (
        "tells the user to enter their details somewhere",
        re.compile(
            r"\b(?:enter|put|type|plug)\s+(?:in\s+)?(?:your|the)\s+(?:exact\s+)?"
            r"(?:dates|details|trip|travel\s+dates|search)\b",
            re.I,
        ),
    ),
    (
        "says it cannot get live data",
        re.compile(
            r"\bi\s+(?:can(?:no|')t|cannot|don't|do\s+not|am\s+unable\s+to|'m\s+unable"
            r"\s+to|am\s+not\s+able\s+to)\s+(?:have\s+)?(?:access\s+(?:to\s+)?|browse\s+|check\s+"
            r"|look\s+up\s+|get\s+|see\s+|retrieve\s+|pull\s+)?(?:the\s+)?(?:real[- ]time|live)\b",
            re.I,
        ),
    ),
)

_OFFER: Final = re.compile(
    r"\b(?:i\s+(?:can(?!')|could|will|'ll|would|'d|may|might)|let\s+me|want\s+me\s+to"
    r"|shall\s+i|should\s+i)\b",
    re.I,
)
"""An offer of the agent. "I can retry Best Buy later, or check Amazon directly" is an offer,
not an order to the user."""

_PRICE: Final = re.compile(
    r"(?:[$€£₹¥]\s?\d|\b(?:USD|EUR|GBP|INR|CAD|AUD|Rs\.?)\s?\d|\d[\d,.]*\s?(?:USD|EUR|GBP|INR)\b)"
)


def deflections(text: str) -> list[str]:
    """The off-platform rules that ``text`` breaks, with the matched text."""
    text = text.replace("\u2019", "'")
    found = []
    for name, pattern in _DEFLECTIONS:
        for match in pattern.finditer(text):
            if name == _ORDER and _offered(text, match.start()):
                continue
            found.append(f"{name}: {match.group(0).strip()[:120]}")
            break
    return found


def _offered(text: str, at: int) -> bool:
    """True if the sentence of ``at``, up to ``at``, is an offer of the agent."""
    start = max(text.rfind(mark, 0, at + 1) for mark in ".!?\n") + 1
    return _OFFER.search(text, start, at + 1) is not None


def has_price(text: str) -> bool:
    """True if ``text`` has an amount of money, for example ``$129`` or ``INR 4,500``."""
    return _PRICE.search(text) is not None


def asks(text: str) -> bool:
    """True if an answer asks the user something and has no price: the runner then gives the
    follow-up of the case once."""
    return "?" in text and not has_price(text)


def classify(answers: Sequence[str], *, failed: bool = False) -> Outcome:
    """The outcome of a case from all assistant answers of its session, in order."""
    if any(deflections(answer) for answer in answers):
        return "off_platform"
    if failed or not answers:
        return "error"
    return "answered" if has_price(answers[-1]) else "no_price"


# ---------------------------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------------------------

AUTO_APPROVE: Final = frozenset(
    {
        "browser_open",
        "browser_snapshot",
        "browser_act",
        "browser_screenshot",
        "browser_extract",
        "browser_tabs",
        "browser_task",
        "web_search",
        "web_extract",
    }
)
"""The tools that the runner approves. A person who asks for a price approves these."""

_DONE: Final = frozenset({"completed", "failed", "cancelled"})


@dataclass(slots=True)
class Result:
    case: str
    kind: str
    runtime: str
    session_id: str | None = None
    outcome: Outcome = "error"
    answers: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    deflections: list[str] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    followups: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    error: str | None = None


class Api:
    """A small client of the sama HTTP API. It reads the token file before each request, so a
    separate process can refresh the token during a long eval."""

    def __init__(self, base: str, token_file: Path, *, timeout_s: float = 30.0) -> None:
        self._token_file = token_file
        self._client = httpx.AsyncClient(
            base_url=base.rstrip("/"),
            timeout=timeout_s,
            headers={"User-Agent": "sama-eval/1.0"},
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
            answers = [case.followup] * len(question["questions"])
            await api.call(
                "POST",
                f"/sessions/{session_id}/questions/{question['id']}/answer",
                {"answers": answers},
            )
        await asyncio.sleep(3)


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
    try:
        session = await api.call(
            "POST",
            "/sessions",
            {
                "workspace_id": workspace_id,
                "agent_profile_id": profile_id,
                "title": f"eval off-platform: {case.id}",
            },
        )
        result.session_id = session_id = session["id"]
        answered: set[str] = set()
        await api.call("POST", f"/sessions/{session_id}/messages", {"text": case.text})
        await asyncio.sleep(2)
        ok = await _settle(api, session_id, case, result, start + timeout_s, answered)
        answers = await _answers(api, session_id)
        if ok and answers and asks(answers[-1]):
            # The agent asked in its answer text: give the follow-up once.
            result.followups += 1
            await api.call("POST", f"/sessions/{session_id}/messages", {"text": case.followup})
            await asyncio.sleep(2)
            ok = await _settle(api, session_id, case, result, start + 2 * timeout_s, answered)
            answers = await _answers(api, session_id)
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
        result.answers = answers
        result.deflections = [d for answer in answers for d in deflections(answer)]
        result.outcome = classify(answers, failed=failed)
    except (httpx.HTTPError, KeyError, ValueError) as error:
        result.error = f"{type(error).__name__}: {error}"
        result.outcome = classify(result.answers, failed=True)
    result.seconds = round(time.monotonic() - start, 1)
    return result


async def _answers(api: Api, session_id: str) -> list[str]:
    messages = await api.call("GET", f"/sessions/{session_id}/messages")
    items = messages["items"] if isinstance(messages, dict) else messages
    return [str(m.get("text") or "") for m in items if m.get("role") == "assistant"]


def summary(results: Iterable[Result]) -> str:
    """A Markdown table: the count of each outcome per runtime, and the totals."""
    rows: dict[str, Counter[str]] = {}
    cost: Counter[str] = Counter()
    browser: Counter[str] = Counter()
    for result in results:
        rows.setdefault(result.runtime, Counter())[result.outcome] += 1
        cost[result.runtime] += round(result.cost_usd * 100)
        if any(tool.startswith("browser_") for tool in result.tools):
            browser[result.runtime] += 1
    lines = [
        "| Runtime | Cases | Off-platform | Answered with a price | No price | Error"
        " | Used the browser | Cost (USD) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for runtime, counts in sorted(rows.items()):
        total = sum(counts.values())
        cells = [str(counts[outcome]) for outcome in OUTCOMES]
        lines.append(
            f"| {runtime} | {total} | {' | '.join(cells)} | {browser[runtime]}"
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
    and production share one account with a limit of running sandboxes (Section 18.4). Before a
    case, it waits while more than ``max_running`` sandboxes run. After a case, it kills only
    the sandboxes with the ``sama.session_id`` of that case: the session is new and belongs to
    the eval, so no other sandbox is touched."""

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
    return {(r.case, r.runtime) for r in rescore(path)}


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
        print(f"{runtime:8} {case.id:24} {result.outcome:13} {result.seconds:6.0f} s", flush=True)
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
    results = rescore(out / "results.jsonl")
    (out / "summary.md").write_text(summary(results) + "\n", encoding="utf-8")
    return results


def rescore(path: Path) -> list[Result]:
    """The results of a ``results.jsonl`` file, classified again with the current rules. A
    case that is in the file more than once keeps its last result."""
    results: dict[tuple[str, str], Result] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        result = Result(**json.loads(line))
        failed = result.error is not None
        result.deflections = [d for answer in result.answers for d in deflections(answer)]
        result.outcome = classify(result.answers, failed=failed)
        results[(result.case, result.runtime)] = result
    return list(results.values())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sama.evals.off_platform")
    parser.add_argument(
        "--rescore", help="classify a results.jsonl file again and print the summary"
    )
    parser.add_argument("--api", help="the sama API, e.g. the staging URL")
    parser.add_argument("--token-file", help="a file with a user access token")
    parser.add_argument("--workspace")
    parser.add_argument("--profile", action="append", help="runtime label=agent profile id")
    parser.add_argument("--out", help="a folder for results.jsonl and summary.md")
    parser.add_argument("--case", action="append", help="run only this case id (repeatable)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=480.0, help="seconds per turn")
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
