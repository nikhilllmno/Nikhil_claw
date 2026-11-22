"""The run queue in Postgres (docs/architecture.md, Section 4.3, decision D8).

Each run is one ``runs`` row. A worker claims the oldest ``queued`` run with
``FOR UPDATE SKIP LOCKED`` and holds a lease. It extends the lease with ``heartbeat`` and
ends the run with ``complete``, ``fail``, or ``pause``. Only the lease owner
can end a running run.

Rules:

- One active run per session. ``running`` and ``paused`` runs are active. A partial unique
  index enforces the rule, and ``claim`` takes a run only when its session has no active run.
- The runs of one session start in the order of ``created_at`` (FIFO). New messages queue.
- A review run (Section 9.4) is a run of the kind ``review``. It is active too, so a session
  has at most one running review, and a review never runs together with a chat run. A
  second partial unique index allows one waiting review per session. A chat run starts before
  a waiting review, and a review starts only when no chat run of its session waits.
- A paused run waits for its approval. When the approval is decided or times out,
  ``release_decided`` queues the run again with the decision, and the next claim resumes it.
- ``reap`` handles runs whose lease expired, for example after a worker crash. A run that
  the worker did not give to its runtime (``dispatch``) goes back to the queue, up to
  ``max_attempts`` claims. Any other run is marked ``failed``: a tool can have run, so a
  second try is not safe.
- Each write of a worker checks the owner, the attempt, and the lease expiry, so an old
  attempt cannot write after a reap.

The queue writes the transcript: the user message, the run events, the reply, the failure
message, and the approval request. It redacts each of them first (Section 16.3).

The worker is a system process, so ``claim``, ``reap``, and ``release_decided`` read all
orgs. Every other method is scoped by ``org_id``.
"""

from __future__ import annotations

import base64
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    ARRAY,
    Integer,
    Text,
    and_,
    bindparam,
    case,
    cast,
    column,
    exists,
    func,
    insert,
    literal,
    null,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import aliased

from sama.context.compaction import record_compacted
from sama.context.pins import all_pin_texts, compact_focus, pin_block, pins_for_input
from sama.db.engine import autocommit
from sama.db.models import (
    CODING_AGENTS,
    ENDED_RUN_STATUSES,
    MAX_STEER_CHARS,
    AgentProfile,
    Approval,
    Attachment,
    AuditEntry,
    Message,
    ModelCall,
    Org,
    Run,
    RunEvent,
    RunSteer,
    Session,
    WorkflowInstance,
)
from sama.gate.decide import arguments_digest, narrow_toolsets
from sama.gate.service import APPROVAL_TTL, stored_arguments
from sama.media.images import NewAttachment
from sama.orchestrator.limits import ModelQuotas, QueueLimits
from sama.orchestrator.parallel import (
    CATCH_UP_TURNS,
    SIDE_MESSAGES,
    Line,
    Turn,
    catch_up_block,
    side_block,
)
from sama.orgs.plans import plan_of
from sama.review import policy as review_policy
from sama.review.transcript import build_transcript
from sama.review.trigger import after_completed_run
from sama.runtime.contract import (
    BUDGET_KEYS,
    MAX_COMPACT_FOCUS_CHARS,
    ApprovalDecision,
    FailureCode,
    ReasoningEffort,
    RuntimeKind,
    Usage,
    clip_subagent_text,
)
from sama.subagents import model_is_known
from sama.tenancy.regions import home_regions
from sama.vault.redact import default_redactor, redact

DEFAULT_LEASE: Final = timedelta(seconds=30)
DEFAULT_MAX_ATTEMPTS: Final = 3
REAP_BATCH: Final = 100
ACTIVE_STATUSES: Final = ("running", "paused")
EXPIRED_REASON: Final = "the approval timed out"
RATE_WINDOW: Final = timedelta(seconds=60)
MAX_PENDING_STEERS: Final = 10
"""The most notes of ``/steer`` that one run can have before the gate gives them to it."""
STEER_SWEEP_BATCH: Final = 50
CLAIM_TRIES: Final = 5
"""How many candidate runs one ``claim`` tries before it gives up for this call."""


class QueueError(Exception):
    """The queue cannot do the operation. The message is safe to show to the user."""


class RateLimitedError(QueueError):
    """The user or the org sent too many messages in the last 60 seconds."""

    def __init__(self, message: str, retry_after_s: int) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class QuotaExceededError(QueueError):
    """The org or the user used the model quota of the plan (Section 17.5). The message names
    the quota and the Billing page, where an org admin can change the plan."""

    def __init__(self, message: str, quota: str) -> None:
        super().__init__(message)
        self.quota = quota


def _lock(name: str, org_id: uuid.UUID) -> Any:
    """A transaction-level advisory lock for one org and one purpose."""
    return select(func.pg_advisory_xact_lock(func.hashtextextended(f"{name}:{org_id}", 0)))


def _rate_columns(org_id: uuid.UUID, user_id: uuid.UUID | None) -> list[Any]:
    """The columns that ``_check_rate`` reads: the limits of the org and, for the org and for
    the user, the count of messages in the last 60 seconds and the seconds until the oldest
    of them leaves the window. A message is a chat run or a workflow instance that a trigger
    started (Section 12.2). Use them in a statement after the rate lock."""

    def window(name: str, column: Any, *scope: Any) -> list[Any]:
        return [
            select(func.count()).where(*scope).scalar_subquery().label(f"{name}_count"),
            select(func.extract("epoch", func.min(column) + RATE_WINDOW - func.now()))
            .where(*scope)
            .scalar_subquery()
            .label(f"{name}_retry"),
        ]

    def recent(name: str, user: bool) -> list[Any]:
        runs: list[Any] = [
            Run.org_id == org_id,
            Run.kind == "chat",
            Run.created_at > func.now() - RATE_WINDOW,
        ]
        starts: list[Any] = [
            WorkflowInstance.org_id == org_id,
            WorkflowInstance.trigger == "trigger",
            WorkflowInstance.created_at > func.now() - RATE_WINDOW,
        ]
        if user:
            runs.append(Run.triggered_by_user_id == user_id)
            starts.append(WorkflowInstance.owner_user_id == user_id)
        return window(name, Run.created_at, *runs) + window(
            f"{name}_flow", WorkflowInstance.created_at, *starts
        )

    def spend(name: str, start: Any, *scope: Any) -> Any:
        return (
            select(func.coalesce(func.sum(ModelCall.cost_usd), 0))
            .where(ModelCall.org_id == org_id, ModelCall.created_at >= start, *scope)
            .scalar_subquery()
            .label(f"{name}_spend")
        )

    columns = [
        select(Org.limits).where(Org.id == org_id).scalar_subquery().label("limits"),
        select(Org.plan).where(Org.id == org_id).scalar_subquery().label("plan"),
        spend("org", _utc_start("month")),
    ]
    columns += recent("org", user=False)
    if user_id is not None:
        columns += recent("user", user=True)
        columns.append(spend("user", _utc_start("day"), ModelCall.user_id == user_id))
    return columns


def _utc_start(unit: str) -> Any:
    """The start of the calendar month or day of now, in UTC, as the model proxy counts."""
    return func.timezone("UTC", func.date_trunc(unit, func.timezone("UTC", func.now())))


STEER_REQUEST_CHARS: Final = 2_000
"""The most characters of the original request in the text of the notes. A longer one is cut."""
_STEER_TAG: Final = "added_messages"


def steer_block(notes: Sequence[str], *, request: str = "", earlier: Sequence[str] = ()) -> str:
    """The text that the runtime gets for new notes of a running run (Section 4.3, M7.1). Empty
    if there are no new notes.

    A note is a ``/steer`` note or a message that joined the run (busy mode ``steer``). Each one
    adds to the request of the run and replaces it only when it says so in clear words. Two
    requests that seem to conflict are both done: a model that reads "change this part" drops
    the original request (the staging repro of M7.1). The text repeats ``request`` (the
    first message of the run) and lists every open note: ``earlier`` (the notes that the run got
    at an earlier step) and ``notes`` (the new ones), oldest first. A note cannot close its
    block."""
    if not notes:
        return ""

    def safe(text: str) -> str:
        return text.strip().replace(f"</{_STEER_TAG}", f"<\\/{_STEER_TAG}")

    original = request.strip()
    if len(original) > STEER_REQUEST_CHARS:
        original = original[: STEER_REQUEST_CHARS - 3].rstrip() + "..."
    items = [*(f"{text} (you got this one before)" for text in earlier), *notes]
    lines = "\n".join(f"{number}. {safe(text)}" for number, text in enumerate(items, start=1))
    parts = [
        f"<{_STEER_TAG}>",
        "The user sent more messages while you work. Each one is a new request in addition to"
        " the original request. None of them cancels or replaces the original request, unless it"
        ' says so in clear words (for example "stop", "instead", or "do not"). Do every request:'
        " the original request and each added message. If two requests seem to conflict (for"
        " example, two different words to reply with), do both of them: include what each one"
        " asks for. Your final reply must answer the original request and every added message.",
    ]
    if original:
        parts.append(f"\nThe original request:\n{safe(original)}")
    parts.append(f"\nThe added messages, oldest first:\n{lines}")
    parts.append(f"</{_STEER_TAG}>")
    return "\n".join(parts)


def _check_rate(row: Any) -> None:
    """Raises ``RateLimitedError`` if a row with ``_rate_columns`` is over a limit. The rate
    lock puts the enqueues of one org in a sequence, so two messages at the same time cannot
    both take the last place."""
    _check_quota(row)
    limits = QueueLimits.from_json(row.limits)
    checks = [("org", limits.org_messages_per_minute, "the org")]
    if "user_count" in row._fields:
        checks.append(("user", limits.user_messages_per_minute, "you"))
    for name, limit, who in checks:
        count = getattr(row, f"{name}_count") + getattr(row, f"{name}_flow_count")
        if count >= limit:
            waits = [getattr(row, f"{name}_retry"), getattr(row, f"{name}_flow_retry")]
            retry_s = min((float(wait) for wait in waits if wait is not None), default=1.0)
            retry = max(1, math.ceil(retry_s))
            raise RateLimitedError(
                f"{who} sent {limit} messages in the last minute. Try again in {retry} s.",
                retry,
            )


def _check_quota(row: Any) -> None:
    """Raises ``QuotaExceededError`` if the model cost of the org this month, or of the user
    today, is at the quota of ``orgs.limits``. A run that started before goes on: the model
    proxy stops its next model call."""
    quotas = ModelQuotas.from_json(row.limits)
    title = plan_of(row.plan).title or row.plan
    cap = quotas.model_usd_per_month
    if cap is not None and Decimal(row.org_spend) >= cap:
        raise QuotaExceededError(
            f"The org used its model quota of USD {cap:.2f} for this month on the {title}"
            " plan. An org admin can change the plan on the Billing page.",
            "model_usd_per_month",
        )
    cap = quotas.model_usd_per_user_day
    if cap is not None and "user_spend" in row._fields and Decimal(row.user_spend) >= cap:
        raise QuotaExceededError(
            f"You used your model quota of USD {cap:.2f} for today on the {title} plan. Try"
            " again tomorrow (UTC), or ask an org admin to change the plan on the Billing page.",
            "model_usd_per_user_day",
        )


def library_budget(agent: Mapping[str, Any]) -> dict[str, Any]:
    """The budget cap of a run of a session that runs as a library agent (Section 13.3)."""
    out: dict[str, Any] = {}
    if isinstance(agent.get("max_turns"), int):
        out["max_turns"] = agent["max_turns"]
    if agent.get("max_cost_usd") is not None:
        out["max_cost_usd"] = str(agent["max_cost_usd"])
    return out


def library_toolsets(agent: Mapping[str, Any]) -> list[str]:
    """The toolset limit of a run of a session that runs as a library agent. The toolsets that
    a sub-agent never has are out (Section 13.1)."""
    # Here, not at the top: ``sama.subagents`` imports the orchestrator through the runtime.
    from sama.subagents import NOT_FOR_SUBAGENTS

    raw = agent.get("toolsets")
    names = [str(name) for name in raw] if isinstance(raw, list) else []
    return [name for name in names if name not in NOT_FOR_SUBAGENTS]


def narrow_budget(
    budget: Mapping[str, Any] | None, cap: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The lower of two run budgets (``Budget``), key by key. A missing key means no limit.
    A cost is a ``Decimal`` string in JSON, so the stored value is exact."""
    out = dict(budget or {})
    for key, value in (cap or {}).items():
        if key not in BUDGET_KEYS:
            raise ValueError(f"not a budget key: {key}")
        if value is None:
            continue
        current = out.get(key)
        if key == "max_cost_usd":
            low = (
                Decimal(str(value))
                if current is None
                else min(Decimal(str(current)), Decimal(str(value)))
            )
            out[key] = str(low)
        else:
            out[key] = value if current is None else min(current, value)
    return out


def _session_state_query(
    runs: Any, org_id: Any, session_id: Any, runtime: Any, column: str = "runtime_state"
) -> Any:
    """The runtime state of the newest chat run of a session that saved one (see
    ``RunQueue._session_state``), or another ``column`` of that run. ``runs`` is ``Run`` or an
    alias of it. A side run (M7.1) does not count: its runtime session is its own."""
    return (
        select(getattr(runs, column))
        .where(
            runs.org_id == org_id,
            runs.session_id == session_id,
            runs.runtime == runtime,
            runs.kind == "chat",
            runs.side.is_(False),
            runs.status.in_(("completed", "failed", "cancelled")),
            runs.runtime_state.is_not(None),
        )
        .order_by(runs.finished_at.desc())
        .limit(1)
    )


def _resumes(runs: Any) -> Any:
    """The run is queued again after its approval was decided (``release_decided``)."""
    return and_(runs.resume_decision.is_not(None), runs.pending_approval_id.is_not(None))


def _ready(runs: Any, limit: Any) -> Any:
    """The rule that lets a queued run start now, for ``runs`` (``Run`` or an alias). ``limit``
    is the ``max_parallel_runs`` of its profile. FIFO per session: no older chat run of the
    session waits.

    - A review run starts only when the session has no open run and no waiting chat run.
    - A chat run never starts next to an open review run or ``/compact`` run.
    - A chat run starts when the session has no open chat run, or when it resumes after an
      approval (it was open before), or when it can run in parallel (M7.1): it is not a
      ``/compact`` run, ``allow_parallel`` is set, and the session has fewer open chat runs
      than ``limit``. A coding agent run (Section 15.4) never runs in parallel: the run is not
      a coding agent run, no open run is one, and the session is not in the direct mode.
    """
    active = aliased(Run)
    older = aliased(Run)
    opened = (active.session_id == runs.session_id, active.status.in_(ACTIVE_STATUSES))
    open_chat = (
        select(func.count()).where(*opened, active.kind == "chat").correlate(runs).scalar_subquery()
    )
    waiting_chat = (
        older.session_id == runs.session_id,
        older.status == "queued",
        older.kind == "chat",
    )
    return and_(
        ~exists()
        .where(*waiting_chat, tuple_(older.created_at, older.id) < tuple_(runs.created_at, runs.id))
        .correlate(runs),
        case(
            (
                runs.kind == "review",
                and_(
                    ~exists().where(*opened).correlate(runs),
                    ~exists().where(*waiting_chat).correlate(runs),
                ),
            ),
            else_=and_(
                ~exists()
                .where(*opened, or_(active.kind != "chat", active.compact))
                .correlate(runs),
                or_(
                    ~exists().where(*opened).correlate(runs),
                    _resumes(runs),
                    and_(
                        runs.allow_parallel,
                        ~runs.compact,
                        open_chat < limit,
                        # A coding agent CLI (Section 15.4) never runs next to another run, and
                        # a session in the direct mode has no side runs.
                        runs.coding_agent.is_(None),
                        ~exists().where(*opened, active.runtime.in_(CODING_AGENTS)).correlate(runs),
                        ~exists()
                        .where(
                            Session.org_id == runs.org_id,
                            Session.id == runs.session_id,
                            Session.coding_agent.is_not(None),
                        )
                        .correlate(runs),
                    ),
                ),
            ),
        ),
    )


class Lease(StrEnum):
    """The answer of ``heartbeat``."""

    HELD = "held"
    CANCEL = "cancel"
    """The lease is held, and a user asked to cancel the run."""
    LOST = "lost"
    """The worker does not own the run any more. It must stop the run and write nothing."""


@dataclass(frozen=True, slots=True)
class RunRef:
    org_id: uuid.UUID
    run_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class OpenRun:
    """An open chat run of a session, for ``/stop NUMBER`` and the busy notices (M7.1)."""

    number: int
    """The place of the run in the open runs of the session, from 1, oldest first."""
    run_id: uuid.UUID
    status: str
    side: bool
    text: str
    """The start of the request of the run, on one line."""


@dataclass(frozen=True, slots=True)
class Resume:
    approval_id: str
    decision: ApprovalDecision
    reason: str | None


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    """A run that a worker owns until it ends the run or loses the lease."""

    org_id: uuid.UUID
    workspace_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID
    agent_profile_id: uuid.UUID
    user_id: uuid.UUID | None
    """The user who started the run, if any."""
    audience: str
    owner_user_id: uuid.UUID | None
    """The owner of a private session. ``None`` for a shared session."""
    runtime: str
    model: str
    soul: str
    budget: Mapping[str, Any]
    input: str
    runtime_state: bytes | None
    """For a new run, the state of the last completed run of the session with the same
    runtime. For a resume, the state from the pause."""
    resume: Resume | None
    attempt: int
    next_seq: int
    """The ``seq`` of the next run event."""
    kind: str = "chat"
    """``review`` for a background review run (Section 9.4). Its ``input`` is the new part of
    the transcript, ``model`` is the cheap review model, and it has no runtime state."""
    compact: bool = False
    """True for a ``/compact`` run (Section 6.3). Its ``input`` is the focus of the summary,
    not a user message, and the run writes no message."""
    lineage: str | None = None
    """The lineage of the session. ``delegation`` for a delegated child (Section 13.2): the
    gate and the prompt take away the toolsets in ``CHILD_BLOCKED_TOOLSETS``."""
    reasoning_effort: ReasoningEffort | None = None
    """The reasoning effort of the profile (M7.10). ``None``: the default of the model."""
    side: bool = False
    """True for a side run (M7.1): it started while another chat run of its session was open.
    It has no runtime state of the session, its ``input`` carries the transcript, and the
    runner runs it next to the other runner of the session."""


def _stored_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The payload of a run event as stored: redacted, and then cut. The runner sends the whole
    text of a sub-agent event, and it never cuts, so a known secret cannot lose a part before
    the redaction. The cut is after it."""
    redacted: dict[str, Any] = default_redactor().json(payload)
    text = redacted.get("text")
    if kind == "subagent_event" and isinstance(text, str):
        redacted["text"] = clip_subagent_text(text)
    return redacted


def _encode_state(state: bytes | None) -> str | None:
    return None if state is None else base64.b64encode(state).decode("ascii")


def _decode_state(text: str | None) -> bytes | None:
    return None if text is None else base64.b64decode(text)


_CANCELLED: Final = {
    "status": "cancelled",
    "failure_code": "cancelled",
    "failure_message": "cancelled",
}
_REVIEW_OFF: Final = {
    "status": "cancelled",
    "failure_code": "cancelled",
    "failure_message": "the background review of this profile is off",
}
CRASH_MESSAGE: Final = (
    "the worker stopped during the run; a tool can have run, so the run does not start again"
)


async def _side_context(
    connection: AsyncConnection, row: Any, run_id: uuid.UUID
) -> tuple[list[Line], list[str]]:
    """The transcript of a side run (M7.1): the newest messages of the session up to its
    ``context_seq``, oldest first, and the requests of the other open chat runs."""
    rows = (
        await connection.execute(
            select(Message.role, Message.content["text"].astext.label("text"))
            .where(
                Message.org_id == row.org_id,
                Message.session_id == row.session_id,
                Message.seq <= row.context_seq,
                Message.role.in_(("user", "assistant")),
                Message.run_id.is_distinct_from(run_id),
                # The request of an open run is in the list of open requests.
                ~exists().where(
                    Run.org_id == Message.org_id,
                    Run.id == Message.run_id,
                    Message.role == "user",
                    Run.status.in_(ACTIVE_STATUSES),
                ),
            )
            .order_by(Message.seq.desc())
            .limit(SIDE_MESSAGES)
        )
    ).all()
    lines = [Line(r.role, r.text or "") for r in reversed(rows) if r.text]
    requests = (
        await connection.execute(
            select(Message.content["text"].astext)
            .join(Run, and_(Run.org_id == Message.org_id, Run.id == Message.run_id))
            .where(
                Message.org_id == row.org_id,
                Message.session_id == row.session_id,
                Message.role == "user",
                Run.id != run_id,
                Run.kind == "chat",
                Run.status.in_(ACTIVE_STATUSES),
            )
            .order_by(Message.seq)
        )
    ).scalars()
    return lines, [text for text in requests if text]


async def _catch_up(connection: AsyncConnection, row: Any, run_id: uuid.UUID) -> list[Turn]:
    """The side runs that a main run must hear of (M7.1): they completed after the session
    state was made (their reply is newer than ``state_seq``), in the order that they completed.
    A session with no state yet counts every completed side run."""
    base = -1 if row.state_seq is None else row.state_seq
    if row.side_seq is None or row.side_seq <= base:
        return []  # most runs: no side run answered after the state, so no extra statement
    answer = aliased(Message)
    request = aliased(Message)
    rows = (
        await connection.execute(
            select(
                answer.content["text"].astext.label("answer"),
                select(request.content["text"].astext)
                .where(
                    request.org_id == answer.org_id,
                    request.run_id == answer.run_id,
                    request.role == "user",
                )
                .order_by(request.seq)
                .limit(1)
                .scalar_subquery()
                .label("request"),
            )
            .join(Run, and_(Run.org_id == answer.org_id, Run.id == answer.run_id))
            .where(
                answer.org_id == row.org_id,
                answer.session_id == row.session_id,
                answer.role == "assistant",
                answer.seq > base,
                Run.side.is_(True),
                Run.status == "completed",
                Run.id != run_id,
            )
            .order_by(answer.seq.desc())
            .limit(CATCH_UP_TURNS)
        )
    ).all()
    return [Turn(r.request or "", r.answer or "") for r in reversed(rows)]


class RunQueue:
    """The run queue. One instance can serve many workers in one process."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        lease: timedelta = DEFAULT_LEASE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        approval_ttl: timedelta = APPROVAL_TTL,
        review_models: Mapping[str, str] | None = None,
    ) -> None:
        if lease <= timedelta(0):
            raise ValueError("lease must be more than 0")
        if max_attempts < 1:
            raise ValueError("max_attempts must be 1 or more")
        self._engine = engine
        self._lease = lease
        self._max_attempts = max_attempts
        self._approval_ttl = approval_ttl
        self._review_models = {**review_policy.DEFAULT_MODELS, **(review_models or {})}

    @property
    def lease(self) -> timedelta:
        return self._lease

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    # -- producers --------------------------------------------------------------------------

    async def enqueue(
        self,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        text: str,
        user_id: uuid.UUID | None = None,
        unattended: bool = False,
        attachments: Sequence[NewAttachment] = (),
        coding_agent: str | None = None,
        parallel: bool = False,
        human: bool = False,
    ) -> uuid.UUID:
        """Stores a user message and queues a run for it. Returns the run id.

        ``human`` is true when a person sent the message through a human channel (the web app,
        Telegram, Slack, WhatsApp, or email), not an org API key or other automation. It sets
        ``runs.human_origin``, which the direct mode of a coding agent needs (Section 15.4).

        ``coding_agent`` (``claude_code`` or ``codex``) sends this one message to that coding
        agent (``/claude <task>``, Section 15.4).

        ``attachments`` are the images of the message (Section 11.7). The database takes them
        in the same transaction as the message: both exist, or neither.

        The run uses the budget of the session profile. It uses the runtime of the frozen
        session config, or the runtime of the profile before the first run builds the config
        (Section 6.1). If the session has an active run, the new run waits in the queue, unless
        it can run in parallel (M7.1): ``parallel`` is true (``/bg``), or the busy mode of the
        profile is ``parallel``. Then it starts next to the open runs, up to the limit of the
        profile (``max_parallel_runs``).

        Raises ``RateLimitedError`` when the user or the org is over its message rate
        (``QueueLimits``), and ``QuotaExceededError`` when it used a model quota (Section
        17.5).
        """
        async with self._engine.begin() as connection:
            return await self.enqueue_in(
                connection,
                org_id=org_id,
                session_id=session_id,
                text=text,
                user_id=user_id,
                unattended=unattended,
                attachments=attachments,
                coding_agent=coding_agent,
                parallel=parallel,
                human=human,
            )

    async def enqueue_in(
        self,
        connection: AsyncConnection,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        text: str,
        user_id: uuid.UUID | None = None,
        unattended: bool = False,
        attachments: Sequence[NewAttachment] = (),
        budget: Mapping[str, Any] | None = None,
        model: str | None = None,
        check_rate: bool = True,
        toolsets: Sequence[str] | None = None,
        coding_agent: str | None = None,
        parallel: bool = False,
        human: bool = False,
    ) -> uuid.UUID:
        """``enqueue`` in the transaction of the caller. A caller that makes the session in
        the same transaction (delegation, the task dispatcher, an event trigger) gets the
        session and the run together, or neither. The scheduler uses it to write the fire and
        its run in one transaction (Section 12.1). ``budget`` can only make the profile budget
        smaller (``narrow_budget``). ``model`` pins the model of the run; without it, the run
        uses the model of the profile. ``check_rate=False`` keeps the lock order but does not
        apply the message rate: a job fire is not a message of a person, and the notes of
        ``/steer`` passed the rate check already. ``toolsets`` limits the toolsets of the run
        (``runs.toolsets``, Section 12.2). ``parallel`` lets the run start next to an open run of
        the session (M7.1); the busy mode ``parallel`` of the profile does the same."""
        await self._rate_lock(connection, org_id)  # the lock order of all enqueues
        # One statement reads the session, the limits, and the rate counts. It comes
        # after the lock statement, so its snapshot has every enqueue before the lock.
        session = (
            await connection.execute(
                select(
                    Session.status,
                    Session.agent_profile_id,
                    func.coalesce(
                        Session.coding_agent,
                        Session.run_config["runtime"].astext,
                        AgentProfile.runtime,
                    ).label("runtime"),
                    AgentProfile.budget,
                    AgentProfile.busy_mode,
                    Session.library_agent,
                    *_rate_columns(org_id, user_id),
                )
                .join(
                    AgentProfile,
                    and_(
                        AgentProfile.org_id == Session.org_id,
                        AgentProfile.id == Session.agent_profile_id,
                    ),
                )
                .where(Session.org_id == org_id, Session.id == session_id)
                # A share lock on the session row, after the rate lock: an archive of the
                # session (``PATCH /sessions/{id}``) takes the row for update, so it waits for
                # this enqueue or this enqueue sees the new status (M7.10). The run insert
                # takes the same share lock.
                .with_for_update(read=True, of=Session)
            )
        ).one_or_none()
        if session is None:
            raise QueueError("the session does not exist")
        if session.status != "active":
            raise QueueError("the session is not active")
        if check_rate:
            _check_rate(session)
        if isinstance(session.library_agent, dict):
            # A session that runs as a library agent (Section 13.3): each run of it has the
            # toolsets and the limits of the agent, and never more than the caller gave.
            budget = narrow_budget(budget, library_budget(session.library_agent))
            toolsets = narrow_toolsets(toolsets, library_toolsets(session.library_agent))
            wanted = session.library_agent.get("model")
            if (
                model is None
                and isinstance(wanted, str)
                and model_is_known(RuntimeKind(session.runtime), wanted)
            ):
                # The model of the agent, when the runtime of the session knows it.
                model = wanted
        run_id = uuid.uuid4()
        # The run and the message go in one statement: the run insert is a CTE.
        message_id = (
            await connection.execute(
                insert(Message)
                .values(
                    org_id=org_id,
                    session_id=session_id,
                    run_id=run_id,
                    role="user",
                    author_user_id=user_id,
                    content={"text": redact(text)},
                )
                .returning(Message.id)
                .add_cte(
                    insert(Run)
                    .values(
                        id=run_id,
                        org_id=org_id,
                        session_id=session_id,
                        agent_profile_id=session.agent_profile_id,
                        triggered_by_user_id=user_id,
                        runtime=coding_agent or session.runtime,
                        budget=narrow_budget(session.budget, budget),
                        unattended=unattended,
                        model=model,
                        coding_agent=coding_agent,
                        toolsets=list(toolsets) if toolsets is not None else null(),
                        allow_parallel=parallel or session.busy_mode == "parallel",
                        human_origin=human,
                    )
                    .cte("new_run")
                )
            )
        ).scalar_one()
        if attachments:
            await connection.execute(
                insert(Attachment),
                [
                    {
                        "id": image.id,
                        "org_id": org_id,
                        "session_id": session_id,
                        "message_id": message_id,
                        "name": image.name,
                        "mime_type": image.mime_type,
                        "size_bytes": len(image.data),
                        "sha256": image.sha256,
                        "data": image.data,
                    }
                    for image in attachments
                ],
            )
        return run_id

    async def enqueue_compact(
        self,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None,
        focus: str = "",
    ) -> uuid.UUID:
        """Queues a ``/compact`` run for a session and returns the run id (Section 6.3).

        The run asks the runtime to compact the context of the session. It does not answer a
        message and it writes no message. The busy rule: the session must have no open run
        (queued, running, or paused). A paused run waits for a person, and a compaction
        must not change the context under it. The session must also have a completed run
        with a runtime state, or there is nothing to compact.

        Raises ``QueueError`` with a message for the person, and ``RateLimitedError`` when
        the user or the org is over its message rate. A compact run uses a model call, so it
        counts as a message.
        """
        async with self._engine.begin() as connection:
            # The lock order of all enqueues: the org rate lock first, then the session row.
            # A run insert needs a share lock on the session row. If this call took the row
            # first and the rate lock second, a message in the other order would deadlock.
            await self._rate_lock(connection, org_id)
            session = (
                await connection.execute(
                    select(
                        Session.status,
                        Session.agent_profile_id,
                        func.coalesce(
                            Session.run_config["runtime"].astext, AgentProfile.runtime
                        ).label("runtime"),
                        AgentProfile.budget,
                    )
                    .join(
                        AgentProfile,
                        and_(
                            AgentProfile.org_id == Session.org_id,
                            AgentProfile.id == Session.agent_profile_id,
                        ),
                    )
                    .where(Session.org_id == org_id, Session.id == session_id)
                    .with_for_update(of=Session)
                )
            ).one_or_none()
            if session is None:
                raise QueueError("the session does not exist")
            if session.status != "active":
                raise QueueError("the session is not active")
            busy = (
                await connection.execute(
                    select(Run.id)
                    .where(
                        Run.org_id == org_id,
                        Run.session_id == session_id,
                        Run.status.not_in(ENDED_RUN_STATUSES),
                    )
                    .limit(1)
                )
            ).first()
            if busy is not None:
                raise QueueError(
                    "The session is busy: a run is open. Wait until it ends or stop it, then"
                    " compact again."
                )
            if await self._session_state(connection, org_id, session_id, session.runtime) is None:
                raise QueueError("There is nothing to compact yet.")
            _check_rate((await connection.execute(select(*_rate_columns(org_id, user_id)))).one())
            run_id = uuid.uuid4()
            await connection.execute(
                insert(Run).values(
                    id=run_id,
                    org_id=org_id,
                    session_id=session_id,
                    agent_profile_id=session.agent_profile_id,
                    triggered_by_user_id=user_id,
                    runtime=session.runtime,
                    budget=session.budget,
                    compact=True,
                    compact_focus=redact(focus).strip()[:MAX_COMPACT_FOCUS_CHARS],
                )
            )
        return run_id

    async def check_rate_in(
        self, connection: AsyncConnection, *, org_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        """Takes the rate lock and raises ``RateLimitedError`` when the user or the org is over
        its message rate. A trigger calls it before it starts a workflow instance, in the same
        transaction, so the instance counts as a message (Section 12.2)."""
        await self._rate_lock(connection, org_id)
        _check_rate((await connection.execute(select(*_rate_columns(org_id, user_id)))).one())

    @staticmethod
    async def _rate_lock(connection: AsyncConnection, org_id: uuid.UUID) -> None:
        """The lock that puts the enqueues of one org in a sequence. It is the first lock of
        every enqueue, before any lock of a session row (the rows of a run insert take a share
        lock on the session). A transaction can take it again."""
        await connection.execute(_lock("sama.queue.rate", org_id))

    async def cancel(self, *, org_id: uuid.UUID, run_id: uuid.UUID) -> str:
        """Cancels a run and returns its status after the call.

        A queued or paused run is ``cancelled`` at once. For a running run the call records
        the request, and the worker stops the run at its next heartbeat. An ended run does
        not change.
        """
        async with self._engine.begin() as connection:
            status = (
                await connection.execute(
                    select(Run.status)
                    .where(Run.org_id == org_id, Run.id == run_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if status is None:
                raise QueueError("the run does not exist")
            if status in ("queued", "paused"):
                await connection.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(**_CANCELLED, cancel_requested_at=func.now(), finished_at=func.now())
                )
                return "cancelled"
            if status == "running":
                await connection.execute(
                    update(Run)
                    .where(Run.id == run_id, Run.cancel_requested_at.is_(None))
                    .values(cancel_requested_at=func.now())
                )
            return status

    async def waits(self, *, org_id: uuid.UUID, run_id: uuid.UUID) -> bool:
        """True if the run is queued behind an open run of the same session and cannot start
        now: the new message waits in the queue (Section 4.3). A run that can start in parallel
        (M7.1) does not wait."""
        mine = aliased(Run)
        async with self._engine.connect() as connection:
            return bool(
                (
                    await connection.execute(
                        select(
                            and_(
                                mine.status == "queued",
                                exists().where(
                                    Run.org_id == mine.org_id,
                                    Run.session_id == mine.session_id,
                                    Run.id != mine.id,
                                    Run.status.in_(("queued", *ACTIVE_STATUSES)),
                                    Run.created_at <= mine.created_at,
                                ),
                                ~_ready(mine, AgentProfile.max_parallel_runs),
                            )
                        )
                        .join(
                            AgentProfile,
                            and_(
                                AgentProfile.org_id == mine.org_id,
                                AgentProfile.id == mine.agent_profile_id,
                            ),
                        )
                        .where(mine.org_id == org_id, mine.id == run_id)
                    )
                ).scalar_one_or_none()
            )

    async def open_runs(self, *, org_id: uuid.UUID, session_id: uuid.UUID) -> list[OpenRun]:
        """The open chat runs of a session (queued, running, or paused), oldest first, with the
        start of their request. ``/stop NUMBER`` uses the place in this list (M7.1)."""
        request = (
            select(Message.content["text"].astext)
            .where(Message.org_id == Run.org_id, Message.run_id == Run.id, Message.role == "user")
            .order_by(Message.seq)
            .limit(1)
            .correlate(Run)
            .scalar_subquery()
        )
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(Run.id, Run.status, Run.side, Run.compact, request.label("text"))
                    .where(
                        Run.org_id == org_id,
                        Run.session_id == session_id,
                        Run.kind == "chat",
                        Run.status.not_in(ENDED_RUN_STATUSES),
                    )
                    .order_by(Run.created_at, Run.id)
                )
            ).all()
        return [
            OpenRun(
                number=number,
                run_id=row.id,
                status=row.status,
                side=row.side,
                text="/compact" if row.compact else " ".join((row.text or "").split())[:120],
            )
            for number, row in enumerate(rows, start=1)
        ]

    async def steer(
        self,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        text: str,
        user_id: uuid.UUID | None = None,
        busy_mode: bool = False,
    ) -> uuid.UUID | None:
        """Adds a note of ``/steer`` to the active chat run of the session (Section 4.3).
        Returns the run id, or None if the session has no active chat run (a run that ``/stop``
        cancels does not count): then the caller sends the text as a normal message. When the
        session has more than one active chat run (M7.1), the newest one gets the note.

        ``busy_mode=True`` is the busy mode ``steer`` of a normal message (M7.1): the note goes
        only to a ``running`` run of a profile in the mode ``steer``, and a run with
        ``MAX_PENDING_STEERS`` notes returns None, so the caller queues the message.

        The gate gives the note to the run at its next tool call. If the run completes before
        that, ``complete`` sends the notes as a new message. A user over the message rate
        cannot add a note. A note does not count for the rate itself: the limit of pending
        notes (``MAX_PENDING_STEERS``) bounds it, and the new message of ``complete`` counts.

        Raises ``RateLimitedError`` when the user or the org is over its message rate, and
        ``QueueError`` when the run has too many pending notes.
        """
        text = text.strip()
        if not text:
            raise QueueError("the note is empty")
        if len(text) > MAX_STEER_CHARS and busy_mode:
            return None
        if len(text) > MAX_STEER_CHARS:
            raise QueueError(f"the note is longer than {MAX_STEER_CHARS} characters")
        async with self._engine.begin() as connection:
            await self._rate_lock(connection, org_id)
            # The share lock keeps the run active until the note is in: ``_end`` must wait,
            # so it sees the note and sends it on.
            row = (
                await connection.execute(
                    select(
                        Run.id,
                        select(func.count())
                        .where(RunSteer.run_id == Run.id, RunSteer.status == "pending")
                        .scalar_subquery()
                        .label("pending"),
                        *_rate_columns(org_id, user_id),
                    )
                    .where(
                        Run.org_id == org_id,
                        Run.session_id == session_id,
                        Run.status.in_(("running",) if busy_mode else ACTIVE_STATUSES),
                        Run.kind == "chat",
                        Run.compact.is_(False),
                        Run.cancel_requested_at.is_(None),
                        *(
                            [
                                exists().where(
                                    AgentProfile.org_id == Run.org_id,
                                    AgentProfile.id == Run.agent_profile_id,
                                    AgentProfile.busy_mode == "steer",
                                ),
                                # A coding agent CLI cannot read a note in the middle of a run,
                                # and a session in the direct mode sends each message to the
                                # CLI (Section 15.4): there the message waits.
                                Run.runtime.not_in(CODING_AGENTS),
                                ~exists().where(
                                    Session.org_id == Run.org_id,
                                    Session.id == Run.session_id,
                                    Session.coding_agent.is_not(None),
                                ),
                            ]
                            if busy_mode
                            else []
                        ),
                    )
                    .order_by(Run.created_at.desc(), Run.id.desc())
                    .limit(1)
                    .with_for_update(of=Run, read=True)
                )
            ).one_or_none()
            if row is None:
                return None
            _check_rate(row)
            if row.pending >= MAX_PENDING_STEERS and busy_mode:
                return None
            if row.pending >= MAX_PENDING_STEERS:
                raise QueueError(
                    f"the run has {MAX_PENDING_STEERS} notes that it did not read yet."
                    " Wait, or use /stop."
                )
            await connection.execute(
                insert(RunSteer).values(
                    org_id=org_id,
                    session_id=session_id,
                    run_id=row.id,
                    user_id=user_id,
                    text=redact(text),
                )
            )
            steered: uuid.UUID = row.id
            return steered

    async def take_steers(self, *, org_id: uuid.UUID, run_id: uuid.UUID) -> str:
        """Marks the pending notes of a running run as delivered and returns their text, oldest
        first. The tool gate calls it for each tool call of the run. Empty if none."""
        async with self._engine.begin() as connection:
            rows = (
                await connection.execute(
                    update(RunSteer)
                    .where(
                        RunSteer.org_id == org_id,
                        RunSteer.run_id == run_id,
                        RunSteer.status == "pending",
                        exists().where(
                            Run.org_id == org_id, Run.id == run_id, Run.status == "running"
                        ),
                    )
                    .values(status="delivered", delivered_at=func.now())
                    .returning(RunSteer.id, RunSteer.created_at, RunSteer.text)
                )
            ).all()
            if not rows:
                return ""
            # The model must keep the original request and the notes of earlier steps (M7.1).
            request = await connection.scalar(
                select(Message.content["text"].astext)
                .where(Message.org_id == org_id, Message.run_id == run_id, Message.role == "user")
                .order_by(Message.seq)
                .limit(1)
            )
            earlier = (
                await connection.execute(
                    select(RunSteer.text)
                    .where(
                        RunSteer.org_id == org_id,
                        RunSteer.run_id == run_id,
                        RunSteer.status == "delivered",
                        RunSteer.id.not_in([row.id for row in rows]),
                    )
                    .order_by(RunSteer.created_at, RunSteer.id)
                )
            ).scalars()
        return steer_block(
            [row.text for row in sorted(rows, key=lambda row: (row.created_at, row.id))],
            request=request or "",
            earlier=list(earlier),
        )

    async def requeued_run(self, *, org_id: uuid.UUID, run_id: uuid.UUID) -> uuid.UUID | None:
        """The run that answers the notes that a completed run did not read, if any."""
        async with self._engine.connect() as connection:
            return (
                await connection.execute(
                    select(RunSteer.requeued_run_id)
                    .where(
                        RunSteer.org_id == org_id,
                        RunSteer.run_id == run_id,
                        RunSteer.status == "requeued",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def sweep_steers(self) -> int:
        """Handles the notes that no run reads: it drops the pending notes of failed and
        cancelled runs, and it sends the pending notes of completed runs as new messages
        (``complete`` leaves them when they came in at the last moment). Returns the count of
        runs that it handled."""
        async with self._engine.begin() as connection:
            dropped = (
                await connection.execute(
                    update(RunSteer)
                    .where(
                        RunSteer.status == "pending",
                        exists().where(
                            Run.org_id == RunSteer.org_id,
                            Run.id == RunSteer.run_id,
                            Run.status.in_(("failed", "cancelled")),
                        ),
                    )
                    .values(status="dropped")
                    .returning(RunSteer.run_id)
                )
            ).all()
            due = (
                await connection.execute(
                    select(RunSteer.org_id, RunSteer.session_id, RunSteer.run_id)
                    .join(Run, and_(Run.org_id == RunSteer.org_id, Run.id == RunSteer.run_id))
                    .where(RunSteer.status == "pending", Run.status == "completed")
                    .distinct()
                    .limit(STEER_SWEEP_BATCH)
                )
            ).all()
        for row in due:
            async with self._engine.begin() as connection:
                await self._rate_lock(connection, row.org_id)
                await self._requeue_steers(
                    connection, org_id=row.org_id, session_id=row.session_id, run_id=row.run_id
                )
        return len({run_id for (run_id,) in dropped}) + len(due)

    async def _requeue_steers(
        self,
        connection: AsyncConnection,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """Sends the pending notes of an ended run as one new message of the session, and
        returns the new run. The caller holds the rate lock. The notes passed the rate check
        when they came in, so the new message does not need the check (it counts for the
        rate from now on). If the session is not active, the notes are dropped."""
        notes = (
            await connection.execute(
                select(RunSteer.id, RunSteer.user_id, RunSteer.text)
                .where(
                    RunSteer.org_id == org_id,
                    RunSteer.run_id == run_id,
                    RunSteer.status == "pending",
                )
                .order_by(RunSteer.created_at, RunSteer.id)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not notes:
            return None
        ids = [note.id for note in notes]
        users = {note.user_id for note in notes}
        try:
            new_run: uuid.UUID | None = await self.enqueue_in(
                connection,
                org_id=org_id,
                session_id=session_id,
                text="\n\n".join(note.text for note in notes),
                user_id=users.pop() if len(users) == 1 else None,
                check_rate=False,
            )
        except QueueError:
            new_run = None
        await connection.execute(
            update(RunSteer)
            .where(RunSteer.id.in_(ids))
            .values(
                status="requeued" if new_run is not None else "dropped",
                requeued_run_id=new_run,
            )
        )
        return new_run

    async def release_decided(
        self, *, org_id: uuid.UUID | None = None, run_id: uuid.UUID | None = None
    ) -> list[uuid.UUID]:
        """Queues the paused runs whose approval is decided or timed out. Returns their ids.

        An approved approval resumes with ``approve``. A rejected or expired one resumes with
        ``reject``. The API calls it for one run after ``GateService.decide_approval``. The
        worker calls it for all runs from time to time.
        """
        decision = case((Approval.status == "approved", "approve"), else_="reject")
        reason = func.coalesce(
            Approval.decision_reason,
            case((Approval.status == "expired", EXPIRED_REASON), else_=None),
        )
        conditions = [
            Run.status == "paused",
            Run.cancel_requested_at.is_(None),
            Approval.org_id == Run.org_id,
            Approval.run_id == Run.id,
            Approval.call_id == Run.pending_approval_id,
            Approval.status != "pending",
        ]
        if org_id is not None:
            conditions.append(Run.org_id == org_id)
        if run_id is not None:
            conditions.append(Run.id == run_id)
        async with self._engine.begin() as connection:
            rows = await connection.execute(
                update(Run)
                .where(*conditions)
                .values(status="queued", resume_decision=decision, resume_reason=reason)
                .returning(Run.id)
            )
            return list(rows.scalars())

    # -- workers ----------------------------------------------------------------------------

    async def claim(self, owner: str) -> ClaimedRun | None:
        """Claims the next run for ``owner``, or returns None if no run is ready.

        A run of an org that has ``max_running_runs`` running runs waits (``QueueLimits``). An
        advisory lock per org puts the claims of one org in a sequence, so two workers cannot
        both take the last place.
        """
        full = await self._full_orgs()
        for _ in range(CLAIM_TRIES):
            try:
                async with self._engine.begin() as connection:
                    row = (
                        await connection.execute(self._candidate(full, home_regions().home))
                    ).one_or_none()
                    if row is None:
                        return None
                    if row.kind == "review" and not await self._review_on(connection, row.id):
                        # A person turned the review off after it was queued (Section 9.4).
                        await connection.execute(
                            update(Run)
                            .where(Run.id == row.id)
                            .values(**_REVIEW_OFF, finished_at=func.now())
                        )
                        continue
                    await connection.execute(_lock("sama.queue.running", row.org_id))
                    if await self._at_limit(connection, row.org_id):
                        full.add(row.org_id)
                        continue
                    return await self._claimed(connection, row.id, owner)
            except IntegrityError:
                # Another worker started a run of the same session at the same time. The
                # partial unique index refused this claim. The run stays queued for a later
                # claim.
                return None
        return None

    @staticmethod
    def _candidate(full: set[uuid.UUID], region: str) -> Any:
        """The oldest ready run (``_ready``): its org is in ``region`` (the home region, Section
        17.3), and its org is not in ``full``. A run of an org in another region stays queued:
        the deployment of that region runs it. Chat runs go before review runs.

        The statement locks the run and its session row (``FOR NO KEY UPDATE ... SKIP LOCKED``).
        The session lock puts the claims of one session in a sequence, so the count of open runs
        stays true until the claim commits. ``complete`` takes the same lock before it stores a
        reply, so the transcript that a run reads at its claim (M7.1) has every reply that
        committed before, and none of a later reply."""
        conditions = [
            Run.status == "queued",
            exists().where(Org.id == Run.org_id, Org.region == region),
            _ready(Run, AgentProfile.max_parallel_runs),
        ]
        if full:
            conditions.append(Run.org_id.not_in(full))
        return (
            select(Run.id, Run.org_id, Run.kind)
            .join(Session, and_(Session.org_id == Run.org_id, Session.id == Run.session_id))
            .join(
                AgentProfile,
                and_(AgentProfile.org_id == Run.org_id, AgentProfile.id == Run.agent_profile_id),
            )
            .where(*conditions)
            .order_by(case((Run.kind == "chat", 0), else_=1), Run.created_at, Run.id)
            .limit(1)
            .with_for_update(of=(Run, Session), skip_locked=True, key_share=True)
        )

    async def _full_orgs(self) -> set[uuid.UUID]:
        """The orgs that have ``max_running_runs`` running runs. Only orgs with a running run
        are read, so the query stays small."""
        async with autocommit(self._engine) as connection:
            rows = await connection.execute(
                select(Run.org_id, func.count().label("running"), Org.limits)
                .join(Org, Org.id == Run.org_id)
                .where(Run.status == "running")
                .group_by(Run.org_id, Org.limits)
            )
            return {
                row.org_id
                for row in rows
                if row.running >= QueueLimits.from_json(row.limits).max_running_runs
            }

    @staticmethod
    async def _review_on(connection: AsyncConnection, run_id: uuid.UUID) -> bool:
        """The setting ``background_review`` of the profile of a review run, read at claim."""
        return bool(
            (
                await connection.execute(
                    select(AgentProfile.background_review)
                    .join(
                        Run,
                        and_(
                            AgentProfile.org_id == Run.org_id,
                            AgentProfile.id == Run.agent_profile_id,
                        ),
                    )
                    .where(Run.id == run_id)
                )
            ).scalar_one()
        )

    @staticmethod
    async def _at_limit(connection: AsyncConnection, org_id: uuid.UUID) -> bool:
        """The org has ``max_running_runs`` running runs. Call it after the running lock, in a
        later statement, so the count has every claim before the lock."""
        row = (
            await connection.execute(
                select(
                    Org.limits,
                    select(func.count())
                    .where(Run.org_id == org_id, Run.status == "running")
                    .scalar_subquery()
                    .label("running"),
                ).where(Org.id == org_id)
            )
        ).one()
        return bool(row.running >= QueueLimits.from_json(row.limits).max_running_runs)

    async def _claimed(
        self, connection: AsyncConnection, run_id: uuid.UUID, owner: str
    ) -> ClaimedRun:
        """Marks the run as running for ``owner`` and returns it.

        One ``UPDATE ... RETURNING`` statement also reads the session, the profile, the user
        message, the next event number, and the runtime state to start from. Each statement
        is a round trip to the database.
        """
        prior = aliased(Run)
        other = aliased(Run)
        resumes = _resumes(Run)
        starts = and_(Run.kind == "chat", ~resumes)
        # A chat run that starts while another chat run of the session is open is a side run
        # (M7.1). The subquery reads the rows before this update, so the run itself is queued.
        side = case(
            (resumes, Run.side),
            (
                and_(Run.kind == "chat", Run.compact.is_(False)),
                exists().where(
                    other.session_id == Run.session_id,
                    other.id != Run.id,
                    other.kind == "chat",
                    other.status.in_(ACTIVE_STATUSES),
                ),
            ),
            else_=False,
        )
        newest_seq = (
            select(func.max(Message.seq))
            .where(Message.org_id == Run.org_id, Message.session_id == Run.session_id)
            .correlate(Run)
            .scalar_subquery()
        )
        # The newest message that the session state has seen (M7.1). The side runs that
        # answered after it are the catch-up of a main run.
        # A chat run that starts takes the runtime of the session now (see ``runtime`` below).
        # The watermark must come from the same runtime as the state in ``RETURNING``, and the
        # ``SET`` clause still sees the old ``Run.runtime``.
        runtime = case(
            (
                starts,
                func.coalesce(
                    case((Run.compact, null()), else_=Run.coding_agent),
                    case((Run.compact, null()), else_=Session.coding_agent),
                    Session.run_config["runtime"].astext,
                    AgentProfile.runtime,
                ),
            ),
            else_=Run.runtime,
        )
        state_seq = (
            _session_state_query(prior, Run.org_id, Run.session_id, runtime, "context_seq")
            .scalar_subquery()
            .correlate(Run)
        )
        row = (
            await connection.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Session.org_id == Run.org_id,
                    Session.id == Run.session_id,
                    AgentProfile.org_id == Run.org_id,
                    AgentProfile.id == Run.agent_profile_id,
                )
                .values(
                    status="running",
                    lease_owner=owner,
                    lease_expires_at=func.now() + self._lease,
                    attempts=Run.attempts + 1,
                    dispatched_at=None,
                    started_at=func.coalesce(Run.started_at, func.now()),
                    side=side,
                    state_seq=case((starts, state_seq), else_=Run.state_seq),
                    # A ``/compact`` run reads no catch-up, so its state keeps the watermark of
                    # the state that it compacts: the next main run still gets the side answers.
                    context_seq=case(
                        (and_(starts, Run.compact), state_seq),
                        (starts, func.coalesce(newest_seq, 0)),
                        else_=Run.context_seq,
                    ),
                    # A chat run that starts takes the runtime of the session now, as the
                    # model: the profile can switch runtime while the run waits (Section 11.4).
                    # A ``/claude`` or ``/codex`` run keeps its coding agent, and the direct mode
                    # of the session sends its other chat runs to the coding agent. A compact
                    # run compacts the context of the session runtime (Section 15.4).
                    runtime=runtime,
                )
                .returning(
                    Run.org_id,
                    Run.session_id,
                    Run.agent_profile_id,
                    Run.triggered_by_user_id,
                    Run.runtime,
                    Run.budget,
                    Run.runtime_state,
                    Run.pending_approval_id,
                    Run.resume_decision,
                    Run.resume_reason,
                    Run.attempts,
                    Run.kind,
                    Run.compact,
                    Run.compact_focus,
                    Run.review_from_seq,
                    Run.review_to_seq,
                    Run.side,
                    Run.context_seq,
                    Session.workspace_id,
                    Session.audience,
                    Session.owner_user_id,
                    Session.lineage,
                    func.coalesce(Run.model, AgentProfile.model).label("model"),
                    AgentProfile.soul,
                    AgentProfile.review_model,
                    AgentProfile.reasoning_effort,
                    select(Message.content)
                    .where(
                        Message.org_id == Run.org_id,
                        Message.run_id == run_id,
                        Message.role == "user",
                    )
                    .order_by(Message.seq)
                    .limit(1)
                    .scalar_subquery()
                    .label("text"),
                    select(func.coalesce(func.max(RunEvent.seq) + 1, 0))
                    .where(RunEvent.org_id == Run.org_id, RunEvent.run_id == run_id)
                    .scalar_subquery()
                    .label("next_seq"),
                    # Only a chat run that starts (no resume) reads the session state.
                    case(
                        (
                            starts,
                            _session_state_query(prior, Run.org_id, Run.session_id, Run.runtime)
                            .scalar_subquery()
                            .correlate(Run),
                        ),
                        else_=None,
                    ).label("session_state"),
                    Run.state_seq,
                    case(
                        (
                            starts,
                            select(func.max(Message.seq))
                            .join(
                                other,
                                and_(other.org_id == Message.org_id, other.id == Message.run_id),
                            )
                            .where(
                                Message.org_id == Run.org_id,
                                Message.session_id == Run.session_id,
                                Message.role == "assistant",
                                other.side.is_(True),
                                other.status == "completed",
                            )
                            .correlate(Run)
                            .scalar_subquery(),
                        ),
                        else_=None,
                    ).label("side_seq"),
                )
            )
        ).one()
        if row.kind == "review":
            return await self._claimed_review(connection, run_id, row)
        text, next_seq = row.text, row.next_seq
        if row.resume_decision is not None and row.pending_approval_id is not None:
            resume: Resume | None = Resume(
                row.pending_approval_id, ApprovalDecision(row.resume_decision), row.resume_reason
            )
            state = _decode_state(row.runtime_state)
            message = str((text or {}).get("text", ""))  # not used: a resume has no input
        else:
            resume = None
            state = None if row.side else _decode_state(row.session_state)
            message = await self._input(connection, row, run_id, text)
        return ClaimedRun(
            org_id=row.org_id,
            workspace_id=row.workspace_id,
            session_id=row.session_id,
            run_id=run_id,
            agent_profile_id=row.agent_profile_id,
            user_id=row.triggered_by_user_id,
            audience=row.audience,
            owner_user_id=row.owner_user_id,
            runtime=row.runtime,
            model=row.model,
            soul=row.soul,
            budget=row.budget,
            input=message,
            runtime_state=state,
            resume=resume,
            attempt=row.attempts,
            next_seq=int(next_seq),
            compact=bool(row.compact),
            lineage=row.lineage,
            reasoning_effort=row.reasoning_effort,
            side=bool(row.side),
        )

    @staticmethod
    async def _input(
        connection: AsyncConnection, row: Any, run_id: uuid.UUID, text: Mapping[str, Any] | None
    ) -> str:
        """The input of a run that starts: the focus of a compact run, or the user message
        with the pins in front of it (Section 6.3)."""
        if row.compact:
            pins = await all_pin_texts(connection, row.org_id, row.session_id)
            return compact_focus(row.compact_focus or "", pins)
        message = str((text or {}).get("text", ""))
        if row.side:
            # A side run starts a new runtime session (M7.1). It gets all pins, and it does not
            # reserve them: the runtime state of the session does not keep its context.
            pins = await all_pin_texts(connection, row.org_id, row.session_id)
            lines, requests = await _side_context(connection, row, run_id)
            return pin_block(pins) + side_block(lines, requests) + message
        # The pins that no run got, or that a compaction made the runtime forget, go in front
        # of the message. Only the transcript of the runtime has them: the stored message is
        # the text of the person. A run that resumes after an approval gets no pins here.
        pins = await pins_for_input(connection, row.org_id, row.session_id, run_id)
        turns = await _catch_up(connection, row, run_id)
        return pin_block(pins) + catch_up_block(turns) + message

    def _review_model(self, row: Any) -> str:
        """The review model of the profile if the runtime of the run knows it (the fake runtime
        knows all models), or else the default review model of the runtime. The profile can
        switch runtime after the review is in the queue."""
        runtime = RuntimeKind(row.runtime)
        if row.review_model and (
            runtime is RuntimeKind.FAKE or model_is_known(runtime, row.review_model)
        ):
            return str(row.review_model)
        return str(self._review_models.get(row.runtime, row.model))

    async def _claimed_review(
        self, connection: AsyncConnection, run_id: uuid.UUID, row: Any
    ) -> ClaimedRun:
        """A review run reads the new part of the transcript. It starts a fresh runtime
        session with the cheap model, so it has no runtime state and no SOUL."""
        transcript = await build_transcript(
            connection,
            org_id=row.org_id,
            session_id=row.session_id,
            from_seq=row.review_from_seq,
            to_seq=row.review_to_seq,
        )
        return ClaimedRun(
            org_id=row.org_id,
            workspace_id=row.workspace_id,
            session_id=row.session_id,
            run_id=run_id,
            agent_profile_id=row.agent_profile_id,
            user_id=None,
            audience=row.audience,
            owner_user_id=row.owner_user_id,
            runtime=row.runtime,
            model=self._review_model(row),
            soul="",
            budget=row.budget,
            input=transcript,
            runtime_state=None,
            resume=None,
            attempt=row.attempts,
            next_seq=int(row.next_seq),
            kind="review",
        )

    async def _session_state(
        self, connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID, runtime: str
    ) -> bytes | None:
        """The runtime state that the next run of the session starts from: the state of the
        newest chat run that saved one. A completed run saves its state. A failed or cancelled
        run saves one only to keep a count (``RunFailed.runtime_state``), and it keeps the
        conversation of the last completed run."""
        state = (
            await connection.execute(_session_state_query(Run, org_id, session_id, runtime))
        ).scalar_one_or_none()
        return _decode_state(state)

    async def dispatch(
        self, run: ClaimedRun, owner: str, *, config_fingerprint: str | None = None
    ) -> bool:
        """Records that the worker gives the run to its runtime. Returns False if the lease is
        lost. After this mark, ``reap`` does not start the run again, because a tool can run.

        ``config_fingerprint`` is the fingerprint of the ``RunConfig`` that the run uses."""
        async with autocommit(self._engine) as connection:
            row = (
                await connection.execute(
                    update(Run)
                    .where(*self._owned(run, owner))
                    .values(dispatched_at=func.now(), config_fingerprint=config_fingerprint)
                    .returning(Run.id)
                )
            ).one_or_none()
        return row is not None

    async def heartbeat(self, run: ClaimedRun, owner: str) -> Lease:
        """Extends the lease. Tells the worker if a user asked to cancel the run."""
        async with autocommit(self._engine) as connection:
            cancel = (
                await connection.execute(
                    update(Run)
                    .where(*self._owned(run, owner))
                    .values(lease_expires_at=func.now() + self._lease)
                    .returning(Run.cancel_requested_at.is_not(None))
                )
            ).scalar_one_or_none()
        if cancel is None:
            return Lease.LOST
        return Lease.CANCEL if cancel else Lease.HELD

    async def add_events(
        self, run: ClaimedRun, owner: str, events: Sequence[tuple[int, str, dict[str, Any]]]
    ) -> bool:
        """Stores run events as ``(seq, type, payload)``. Returns False if the lease is lost.

        Usually one statement without a transaction stores the events: the insert takes the
        run row ``FOR UPDATE`` while it checks the lease, so a reap waits until it ends. A
        ``compacted`` event writes a second record, so it uses a transaction."""
        if not events:
            return True
        leased = select(Run.id).where(*self._owned(run, owner)).with_for_update()
        if all(kind != "compacted" for _seq, kind, _payload in events):
            rows = (
                func.unnest(
                    bindparam("seqs", [seq for seq, _kind, _payload in events], ARRAY(Integer)),
                    bindparam("types", [kind for _seq, kind, _payload in events], ARRAY(Text)),
                    bindparam(
                        "payloads",
                        [
                            json.dumps(_stored_payload(kind, payload))
                            for _seq, kind, payload in events
                        ],
                        ARRAY(Text),
                    ),
                )
                .table_valued(column("seq", Integer), column("type", Text), column("payload", Text))
                .render_derived(name="event")
            )
            async with autocommit(self._engine) as connection:
                stored = await connection.execute(
                    insert(RunEvent)
                    .from_select(
                        ["org_id", "session_id", "run_id", "seq", "type", "payload"],
                        select(
                            literal(run.org_id, UUID(as_uuid=True)),
                            literal(run.session_id, UUID(as_uuid=True)),
                            literal(run.run_id, UUID(as_uuid=True)),
                            rows.c.seq,
                            rows.c.type,
                            cast(rows.c.payload, JSONB),
                        ).where(exists(leased)),
                    )
                    .returning(RunEvent.seq)
                )
                return bool(stored.first())
        async with self._engine.begin() as connection:
            held = (await connection.execute(leased)).scalar_one_or_none()
            if held is None:
                return False
            await connection.execute(
                insert(RunEvent),
                [
                    {
                        "org_id": run.org_id,
                        "session_id": run.session_id,
                        "run_id": run.run_id,
                        "seq": seq,
                        "type": kind,
                        "payload": _stored_payload(kind, payload),
                    }
                    for seq, kind, payload in events
                ],
            )
            # A compaction is a record of the session, and it is stored with its event.
            for _seq, kind, payload in events:
                if kind == "compacted":
                    await record_compacted(
                        connection,
                        org_id=run.org_id,
                        workspace_id=run.workspace_id,
                        session_id=run.session_id,
                        run_id=run.run_id,
                        payload=payload,
                    )
        return True

    async def complete(
        self,
        run: ClaimedRun,
        owner: str,
        *,
        output: str,
        runtime_state: bytes | None,
        usage: Usage,
    ) -> bool:
        """Stores the reply as an assistant message and ends the run as ``completed``. A
        completed chat run counts for the background review (Section 9.4)."""
        steerable = run.kind == "chat" and not run.compact
        async with self._engine.begin() as connection:
            # Notes of ``/steer`` that the run did not read become a new message after the
            # reply. That is an enqueue, so the rate lock comes first, before the message
            # insert locks the session row. Most runs have no notes: they skip the lock.
            locked = steerable and await self._has_pending_steers(connection, run)
            if locked:
                await self._rate_lock(connection, run.org_id)
            ended = await self._end(
                connection,
                run,
                owner,
                status="completed",
                usage=usage,
                runtime_state=_encode_state(runtime_state),
            )
            # A review run writes no message: nobody reads its reply (Section 9.4). A compact
            # run answers nobody, and it is no user turn.
            if ended and steerable:
                # The claim of a run reads the transcript under the same lock (M7.1), so a run
                # never sees a later reply and never misses an earlier one.
                await connection.execute(
                    select(Session.id)
                    .where(Session.org_id == run.org_id, Session.id == run.session_id)
                    .with_for_update(key_share=True)
                )
                await connection.execute(
                    insert(Message).values(
                        org_id=run.org_id,
                        session_id=run.session_id,
                        run_id=run.run_id,
                        role="assistant",
                        content={"text": redact(output)},
                    )
                )
                # In the same transaction, so a run that completes always counts, once.
                await after_completed_run(connection, run)
                # A note that came in after the check waits for ``sweep_steers``: it needs
                # the rate lock, and this transaction cannot take it now.
                if locked:
                    await self._requeue_steers(
                        connection,
                        org_id=run.org_id,
                        session_id=run.session_id,
                        run_id=run.run_id,
                    )
            return ended

    @staticmethod
    async def _has_pending_steers(connection: AsyncConnection, run: ClaimedRun) -> bool:
        return bool(
            (
                await connection.execute(
                    select(
                        exists().where(
                            RunSteer.org_id == run.org_id,
                            RunSteer.run_id == run.run_id,
                            RunSteer.status == "pending",
                        )
                    )
                )
            ).scalar_one()
        )

    async def fail(
        self,
        run: ClaimedRun,
        owner: str,
        *,
        code: FailureCode,
        message: str,
        usage: Usage,
        runtime_state: bytes | None = None,
    ) -> bool:
        """Ends the run as ``failed``, or as ``cancelled`` for the code ``cancelled``.

        ``runtime_state`` is the state that the next run of the session starts from (see
        ``RunFailed.runtime_state``). The runtime sets it only to keep a count that the failure
        would lose, so the next run never resumes the failed conversation."""
        async with self._engine.begin() as connection:
            return await self._end(
                connection,
                run,
                owner,
                status="cancelled" if code is FailureCode.CANCELLED else "failed",
                usage=usage,
                failure_code=code.value,
                failure_message=redact(message),
                runtime_state=_encode_state(runtime_state),
                # The state of a failure is the conversation from before the run, so it keeps the
                # watermark of that conversation (M7.1): the catch-up of the run is not in it.
                context_seq=Run.state_seq,
            )

    async def pause(
        self,
        run: ClaimedRun,
        owner: str,
        *,
        approval_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        reason: str,
        runtime_state: bytes | None,
        usage: Usage,
    ) -> str | None:
        """Pauses the run until its approval is decided. Returns ``paused``, or ``cancelled``
        if a user asked to cancel the run, or None if the lease is lost.

        The tool gate creates the approval row when it answers ``ask``. If no row exists (for
        example, a runtime with a static gate), this call creates it, so every paused run can
        be decided and can time out.
        """
        async with self._engine.begin() as connection:
            cancel = (
                await connection.execute(
                    select(Run.cancel_requested_at.is_not(None))
                    .where(*self._owned(run, owner))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if cancel is None:
                return None
            if cancel:
                # The cancel came after the last heartbeat. The run must not wait to resume.
                await self._end(
                    connection,
                    run,
                    owner,
                    status="cancelled",
                    usage=usage,
                    failure_code="cancelled",
                    failure_message="cancelled",
                )
                return "cancelled"
            await self._end(
                connection,
                run,
                owner,
                status="paused",
                usage=usage,
                runtime_state=_encode_state(runtime_state),
                pending_approval_id=approval_id,
            )
            created = (
                await connection.execute(
                    pg_insert(Approval)
                    .values(
                        org_id=run.org_id,
                        session_id=run.session_id,
                        run_id=run.run_id,
                        call_id=approval_id,
                        tool_name=tool_name,
                        arguments=stored_arguments(arguments),
                        arguments_digest=arguments_digest(arguments),
                        reason=redact(reason),
                        expires_at=func.now() + self._approval_ttl,
                    )
                    .on_conflict_do_nothing(index_elements=["run_id", "call_id"])
                    .returning(Approval.id)
                )
            ).scalar_one_or_none()
            if created is not None:
                await connection.execute(
                    insert(AuditEntry).values(
                        org_id=run.org_id,
                        workspace_id=run.workspace_id,
                        actor_kind="system",
                        action="approval.requested",
                        target_type="tool_call",
                        target_id=approval_id,
                        run_id=run.run_id,
                        details={"session_id": str(run.session_id), "tool": tool_name},
                    )
                )
            return "paused"

    async def _end(
        self,
        connection: AsyncConnection,
        run: ClaimedRun,
        owner: str,
        *,
        status: str,
        usage: Usage,
        **values: Any,
    ) -> bool:
        finished = func.now() if status != "paused" else None
        # A pause sets ``pending_approval_id`` again in ``values``. Any other end clears it.
        values = {"pending_approval_id": None, **values}
        row = (
            await connection.execute(
                update(Run)
                .where(*self._owned(run, owner))
                .values(
                    status=status,
                    usage=usage.model_dump(mode="json"),
                    lease_owner=None,
                    lease_expires_at=None,
                    resume_decision=None,
                    resume_reason=None,
                    finished_at=finished,
                    **values,
                )
                .returning(Run.id)
            )
        ).one_or_none()
        return row is not None

    @staticmethod
    def _owned(run: ClaimedRun, owner: str) -> tuple[Any, ...]:
        """The run is ``running``, in this attempt, for this owner, and the lease is valid.

        The attempt fences an old attempt of the same owner after a reap and a new claim. The
        expiry check refuses writes after the lease ended, even before a reap.
        """
        return (
            Run.org_id == run.org_id,
            Run.id == run.run_id,
            Run.status == "running",
            Run.lease_owner == owner,
            Run.attempts == run.attempt,
            Run.lease_expires_at > func.now(),
        )

    async def reap(self) -> tuple[list[RunRef], list[RunRef]]:
        """Handles running runs whose lease expired. Returns the runs that went back to the
        queue and the runs that ended.

        A run goes back to the queue only if the worker did not give it to the runtime
        (``dispatched_at`` is null), so no tool of the run can have run.
        """
        async with self._engine.begin() as connection:
            due = (
                await connection.execute(
                    select(
                        Run.org_id,
                        Run.id,
                        Run.cancel_requested_at.is_not(None).label("cancel"),
                        and_(Run.attempts < self._max_attempts, Run.dispatched_at.is_(None)).label(
                            "retry"
                        ),
                    )
                    .where(Run.status == "running", Run.lease_expires_at < func.now())
                    .order_by(Run.lease_expires_at)
                    .limit(REAP_BATCH)
                    .with_for_update(of=Run, skip_locked=True)
                )
            ).all()
            requeued: list[RunRef] = []
            ended: list[RunRef] = []
            for row in due:
                if row.cancel:
                    values: dict[str, Any] = {**_CANCELLED, "finished_at": func.now()}
                elif row.retry:
                    values = {"status": "queued"}
                else:
                    values = {
                        "status": "failed",
                        "failure_code": "internal",
                        "failure_message": CRASH_MESSAGE,
                        "finished_at": func.now(),
                    }
                (requeued if values["status"] == "queued" else ended).append(
                    RunRef(row.org_id, row.id)
                )
                await connection.execute(
                    update(Run)
                    .where(Run.id == row.id)
                    .values(lease_owner=None, lease_expires_at=None, **values)
                )
        return requeued, ended
