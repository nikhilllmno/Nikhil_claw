"""The session history routes of the web API: search, export, and run traces
(docs/architecture.md, Section 11.4).

Every read runs as the user (``as_user``), so RLS applies. The session check of
``session_access`` runs first, so a session that the user cannot see gives ``404``. The
search runs as the table owner and scopes the rows itself, with the rule of a private run
(Section 7.1). Every text that leaves here goes through the redactor again.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

from fastapi import HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.api.auth import Principal
from sama.db.engine import as_user
from sama.db.models import Message, ModelCall, Run, RunEvent, ToolCall, User
from sama.search.core import MAX_LIMIT, MAX_QUERY_CHARS, SessionSearchError
from sama.search.service import SessionSearch
from sama.tenancy.access import load_grants
from sama.vault.redact import default_redactor, redact

ExportFormat = Literal["markdown", "json"]
SessionCheck = Callable[[AsyncConnection, Principal, uuid.UUID], Awaitable[Any]]
"""``session_access`` of ``sama.api.app``: raises ``404`` if the user cannot read the session.
It returns an object with ``session`` (the row) and ``org_id``."""

EXPORT_FORMAT: Final = "sama.session.v1"
MAX_EXPORT_MESSAGES: Final = 5_000
"""An export has at most this many messages. A longer session sets ``truncated``."""
MAX_EXPORT_CHARS: Final = 4_000_000
"""An export stops after the message that brings the text over this many characters, so one
large session cannot fill the memory of the control plane. It then sets ``truncated``."""
EXPORT_BATCH: Final = 200
"""The export reads the messages in batches of this size."""
MAX_RUNS: Final = 200
DEFAULT_RUNS: Final = 50
MAX_TRACE_EVENTS: Final = 2_000
"""A trace has at most this many stored events. A longer run sets ``truncated``."""
MAX_TRACE_TEXT_CHARS: Final = 20_000
"""The text of one merged text step is cut after this many characters. The cut is after the
redaction and at a space, so it never shows the start of a secret."""
MAX_TRACE_TOOL_CALLS: Final = 500
MAX_TRACE_MODEL_CALLS: Final = 500
ROLE_TITLES: Final = {
    "user": "User",
    "assistant": "Assistant",
    "system": "System",
    "tool": "Tool",
}


class SearchSnippet(BaseModel):
    message_id: uuid.UUID
    role: str
    created_at: datetime
    text: str
    """Redacted. ``<<`` and ``>>`` mark the words that match."""


class SearchHit(BaseModel):
    session_id: uuid.UUID
    workspace_id: uuid.UUID
    title: str | None
    audience: str
    created_at: datetime
    updated_at: datetime
    snippets: list[SearchSnippet]


class RunSummary(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    kind: str
    status: str
    runtime: str
    compact: bool
    triggered_by_user_id: uuid.UUID | None
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    model_calls: int


class TraceStep(BaseModel):
    seq: int
    """The ``seq`` of the first stored event of the step."""
    type: str
    """The event type. ``text`` is a run of ``text_delta`` events, merged."""
    created_at: datetime
    payload: dict[str, Any]


class TraceToolCall(BaseModel):
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    decision: str | None
    status: str
    result_preview: str | None
    created_at: datetime
    finished_at: datetime | None


class TraceModelCall(BaseModel):
    provider: str
    model: str
    endpoint: str
    status_code: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal
    duration_ms: int
    created_at: datetime


class RunTrace(BaseModel):
    run: RunSummary
    steps: list[TraceStep]
    tool_calls: list[TraceToolCall]
    model_calls: list[TraceModelCall]
    truncated: bool
    """True if the run has more events than ``MAX_TRACE_EVENTS``."""


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=what)


def _clean(payload: Any) -> dict[str, Any]:
    """A stored payload for the web: redacted again, without a runtime state."""
    if not isinstance(payload, dict):
        return {}
    shown = {k: v for k, v in payload.items() if k != "runtime_state"}
    cleaned: dict[str, Any] = default_redactor().json(shown)
    return cleaned


def _text(content: Any) -> str:
    return redact(str((content or {}).get("text", "")))


def export_markdown(
    session: dict[str, Any], messages: list[dict[str, Any]], truncated: bool
) -> str:
    """The transcript of a session as Markdown. ``session`` and ``messages`` are the JSON
    export. The text of each message stays as it is: it is Markdown already."""
    title = session["title"] or "Untitled session"
    lines = [
        f"# {title}",
        "",
        f"- Session: `{session['id']}`",
        f"- Audience: {session['audience']}",
        f"- Created: {session['created_at']}",
        f"- Exported: {session['exported_at']}",
        f"- Messages: {len(messages)}",
    ]
    if truncated:
        lines.append(f"- Note: the export is cut. It has only the first {len(messages)} messages.")
    for message in messages:
        role = ROLE_TITLES.get(message["role"], message["role"].title())
        lines += ["", f"## {role} ({message['created_at']})", "", message["text"].rstrip()]
    return "\n".join(lines) + "\n"


class SessionHistory:
    """Search, export, and run traces for the web app."""

    def __init__(
        self, engine: AsyncEngine, search: SessionSearch, session_access: SessionCheck
    ) -> None:
        self._engine = engine
        self._search = search
        self._session_access = session_access

    # -- search -----------------------------------------------------------------------------

    async def search(
        self,
        principal: Principal,
        org_id: uuid.UUID,
        query: str,
        workspace_id: uuid.UUID | None,
        limit: int,
    ) -> list[SearchHit]:
        """The sessions of the org that match ``query``, best first. An empty query gives the
        latest sessions. A user who is not a member gets ``404``."""
        if len(query) > MAX_QUERY_CHARS:
            raise HTTPException(
                status_code=422, detail=f"the query has more than {MAX_QUERY_CHARS} characters"
            )
        async with as_user(self._engine, principal.auth_user_id) as connection:
            user_id = (
                await connection.execute(
                    select(User.id).where(
                        User.org_id == org_id, User.auth_user_id == principal.auth_user_id
                    )
                )
            ).scalar_one_or_none()
            grants = None if user_id is None else await load_grants(connection, org_id, user_id)
        if user_id is None or grants is None or not grants.org_member:
            raise _not_found("the org does not exist")
        if workspace_id is not None and not grants.sees_workspace(workspace_id):
            raise _not_found("the workspace does not exist")
        try:
            hits = await self._search.search_as_user(
                org_id, user_id, query, min(limit, MAX_LIMIT), workspace_id=workspace_id
            )
        except SessionSearchError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return [SearchHit.model_validate(hit, from_attributes=True) for hit in hits]

    # -- export -----------------------------------------------------------------------------

    async def export(
        self, principal: Principal, session_id: uuid.UUID, fmt: ExportFormat
    ) -> Response:
        """The transcript of a session as a file to download."""
        messages: list[dict[str, Any]] = []
        chars = 0
        truncated = False
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await self._session_access(connection, principal, session_id)
            row = access.session
            after = -1
            # Batches by ``seq`` keep only one batch of raw rows in memory at a time.
            while not truncated:
                batch = (
                    await connection.execute(
                        select(
                            Message.seq,
                            Message.run_id,
                            Message.role,
                            Message.author_user_id,
                            Message.content,
                            Message.created_at,
                        )
                        .where(
                            Message.org_id == row.org_id,
                            Message.session_id == session_id,
                            Message.seq > after,
                        )
                        .order_by(Message.seq)
                        .limit(EXPORT_BATCH)
                    )
                ).all()
                for m in batch:
                    if len(messages) >= MAX_EXPORT_MESSAGES or chars > MAX_EXPORT_CHARS:
                        truncated = True
                        break
                    text = _text(m.content)
                    chars += len(text)
                    messages.append(
                        {
                            "seq": m.seq,
                            "run_id": None if m.run_id is None else str(m.run_id),
                            "role": m.role,
                            "author_user_id": None
                            if m.author_user_id is None
                            else str(m.author_user_id),
                            "created_at": m.created_at.isoformat(),
                            "text": text,
                        }
                    )
                if len(batch) < EXPORT_BATCH:
                    break
                after = batch[-1].seq
        session = {
            "id": str(row.id),
            "org_id": str(row.org_id),
            "workspace_id": str(row.workspace_id),
            "agent_profile_id": str(row.agent_profile_id),
            "title": redact(row.title) if row.title else None,
            "audience": row.audience,
            "status": row.status,
            "lineage": row.lineage,
            "parent_session_id": None
            if row.parent_session_id is None
            else str(row.parent_session_id),
            "created_at": row.created_at.isoformat(),
            "exported_at": datetime.now(UTC).isoformat(),
        }
        stem = f"session-{str(row.id)[:8]}"
        if fmt == "json":
            body = json.dumps(
                {
                    "format": EXPORT_FORMAT,
                    "session": session,
                    "messages": messages,
                    "truncated": truncated,
                },
                ensure_ascii=False,
                indent=2,
            )
            media, name = "application/json", f"{stem}.json"
        else:
            body = export_markdown(session, messages, truncated)
            media, name = "text/markdown; charset=utf-8", f"{stem}.md"
        return Response(
            content=body,
            media_type=media,
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Cache-Control": "no-store",
            },
        )

    # -- runs and traces --------------------------------------------------------------------

    @staticmethod
    def _run_columns(*scope: Any) -> tuple[Any, ...]:
        """The run columns with the model usage of each run. ``scope`` bounds the usage rows
        to one session or one run, so the sum reads only the rows that it needs."""
        usage = (
            select(
                ModelCall.run_id.label("run_id"),
                func.coalesce(func.sum(ModelCall.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(ModelCall.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(ModelCall.cost_usd), 0).label("cost_usd"),
                func.count().label("model_calls"),
            )
            .where(*scope)
            .group_by(ModelCall.run_id)
            .subquery()
        )
        return (
            usage,
            (
                Run.id,
                Run.session_id,
                Run.kind,
                Run.status,
                Run.runtime,
                Run.compact,
                Run.triggered_by_user_id,
                Run.failure_code,
                Run.failure_message,
                Run.created_at,
                Run.started_at,
                Run.finished_at,
                func.coalesce(usage.c.input_tokens, 0).label("input_tokens"),
                func.coalesce(usage.c.output_tokens, 0).label("output_tokens"),
                func.coalesce(usage.c.cost_usd, 0).label("cost_usd"),
                func.coalesce(usage.c.model_calls, 0).label("model_calls"),
            ),
        )

    @staticmethod
    def _summary(row: Any) -> RunSummary:
        summary = RunSummary.model_validate(row, from_attributes=True)
        if summary.failure_message:
            summary.failure_message = redact(summary.failure_message)
        return summary

    async def runs(
        self, principal: Principal, session_id: uuid.UUID, limit: int
    ) -> list[RunSummary]:
        """The runs of a session, newest first, with the tokens and the cost of each."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await self._session_access(connection, principal, session_id)
            usage, columns = self._run_columns(
                ModelCall.org_id == access.org_id, ModelCall.session_id == session_id
            )
            rows = await connection.execute(
                select(*columns)
                .outerjoin(usage, usage.c.run_id == Run.id)
                .where(Run.org_id == access.org_id, Run.session_id == session_id)
                .order_by(Run.created_at.desc(), Run.id.desc())
                .limit(min(limit, MAX_RUNS))
            )
            return [self._summary(row) for row in rows]

    async def trace(self, principal: Principal, run_id: uuid.UUID) -> RunTrace:
        """The trace of one run: the stored events, the tool calls, and the model calls."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            # RLS hides the run of a session that the user cannot read. The session check
            # then applies the same rule as every other session route.
            owner = (
                await connection.execute(select(Run.org_id).where(Run.id == run_id))
            ).scalar_one_or_none()
            if owner is None:
                raise _not_found("the run does not exist")
            usage, columns = self._run_columns(
                ModelCall.org_id == owner, ModelCall.run_id == run_id
            )
            row = (
                await connection.execute(
                    select(*columns)
                    .outerjoin(usage, usage.c.run_id == Run.id)
                    .where(Run.org_id == owner, Run.id == run_id)
                )
            ).one()
            access = await self._session_access(connection, principal, row.session_id)
            org_id = access.org_id
            events = (
                await connection.execute(
                    select(RunEvent.seq, RunEvent.type, RunEvent.payload, RunEvent.created_at)
                    .where(
                        RunEvent.org_id == org_id,
                        RunEvent.session_id == row.session_id,
                        RunEvent.run_id == run_id,
                    )
                    .order_by(RunEvent.seq)
                    .limit(MAX_TRACE_EVENTS + 1)
                )
            ).all()
            tools = await connection.execute(
                select(
                    ToolCall.call_id,
                    ToolCall.tool_name,
                    ToolCall.arguments,
                    ToolCall.decision,
                    ToolCall.status,
                    ToolCall.result_preview,
                    ToolCall.created_at,
                    ToolCall.finished_at,
                )
                .where(
                    ToolCall.org_id == org_id,
                    ToolCall.session_id == row.session_id,
                    ToolCall.run_id == run_id,
                )
                .order_by(ToolCall.created_at, ToolCall.call_id)
                .limit(MAX_TRACE_TOOL_CALLS)
            )
            tool_calls = [
                TraceToolCall(
                    call_id=t.call_id,
                    tool_name=t.tool_name,
                    arguments=_clean(t.arguments),
                    decision=t.decision,
                    status=t.status,
                    result_preview=None if t.result_preview is None else redact(t.result_preview),
                    created_at=t.created_at,
                    finished_at=t.finished_at,
                )
                for t in tools
            ]
            models = await connection.execute(
                select(
                    ModelCall.provider,
                    ModelCall.model,
                    ModelCall.endpoint,
                    ModelCall.status_code,
                    ModelCall.input_tokens,
                    ModelCall.output_tokens,
                    ModelCall.cache_read_tokens,
                    ModelCall.cache_write_tokens,
                    ModelCall.cost_usd,
                    ModelCall.duration_ms,
                    ModelCall.created_at,
                )
                .where(
                    ModelCall.org_id == org_id,
                    ModelCall.session_id == row.session_id,
                    ModelCall.run_id == run_id,
                )
                .order_by(ModelCall.created_at, ModelCall.id)
                .limit(MAX_TRACE_MODEL_CALLS)
            )
            model_calls = [TraceModelCall.model_validate(m, from_attributes=True) for m in models]
        truncated = len(events) > MAX_TRACE_EVENTS
        return RunTrace(
            run=self._summary(row),
            steps=merge_steps(events[:MAX_TRACE_EVENTS], more=truncated),
            tool_calls=tool_calls,
            model_calls=model_calls,
            truncated=truncated,
        )


def _cut(text: str, limit: int) -> str:
    """``text`` with at most ``limit`` characters. A cut ends at a space, so the last word,
    which can be the start of a secret, does not show."""
    if len(text) <= limit:
        return text
    return _drop_last_word(text[:limit])


def _drop_last_word(text: str) -> str:
    """``text`` without the characters after its last space."""
    end = max(text.rfind(" "), text.rfind("\n"), text.rfind("\t"))
    return text[: end + 1] if end >= 0 else ""


def merge_steps(events: Sequence[Any], *, more: bool = False) -> list[TraceStep]:
    """The stored events as steps. A run of ``text_delta`` events is one ``text`` step, so a
    long answer is one step, not hundreds. ``more`` is true if the run has events after
    ``events``.

    Each delta was redacted alone, but a secret can span two deltas. Thus the whole joined
    text is redacted before the cut. If the last step is text and ``more`` is true, the text
    goes on in an event that is not here. Then the redactor cannot see the whole secret, so
    that step is not in the result.
    """
    steps: list[TraceStep] = []
    parts: dict[int, list[str]] = {}
    for event in events:
        payload = _clean(event.payload)
        if event.type == "text_delta":
            text = str(payload.get("text", ""))
            if steps and steps[-1].type == "text":
                parts[len(steps) - 1].append(text)
                continue
            parts[len(steps)] = [text]
            steps.append(
                TraceStep(seq=event.seq, type="text", created_at=event.created_at, payload={})
            )
            continue
        steps.append(
            TraceStep(seq=event.seq, type=event.type, created_at=event.created_at, payload=payload)
        )
    for index, texts in parts.items():
        steps[index].payload["text"] = _cut(redact("".join(texts)), MAX_TRACE_TEXT_CHARS)
    if more and steps and steps[-1].type == "text":
        steps.pop()
    return steps


__all__ = [
    "DEFAULT_RUNS",
    "MAX_RUNS",
    "RunSummary",
    "RunTrace",
    "SearchHit",
    "SessionHistory",
    "export_markdown",
    "merge_steps",
]
