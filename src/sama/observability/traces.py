"""Langfuse traces for ended runs (docs/architecture.md, Section 18.2).

The runner in the sandbox has no Langfuse key (no secrets in the sandbox). So the control
plane builds the trace of a run after the run ends, from the rows that it already stores:

- the run row and its agent profile: the root span (Langfuse type ``agent``),
- ``messages``: the input and the output of the root span,
- ``model_calls`` from the model proxy: one ``generation`` per model call, with tokens and cost,
- ``tool_calls`` from the tool gate: one ``tool`` span per call, with the decision and status,
- ``run_events``: an ``event`` for each approval pause and each compaction.

The same code works for both runtimes. ``TraceExporter`` sends one trace per ended run and
marks the run with ``trace_exported_at``. Two exporters never send the same run, because
each one locks the run row with ``SKIP LOCKED``.

Tool arguments and tool output stay out of the trace until redaction exists (M1.17).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import (
    ENDED_RUN_STATUSES,
    AgentProfile,
    Message,
    ModelCall,
    Run,
    RunEvent,
    ToolCall,
)
from sama.observability.langfuse import (
    AttributeValue,
    LangfuseClient,
    LangfuseError,
    Span,
    json_attribute,
)
from sama.observability.logs import log_context

log: Final = logging.getLogger("sama.observability.traces")

MAX_TEXT_CHARS: Final = 20_000
"""The input and the output of a trace are cut at this length."""
EVENT_TYPES: Final = ("approval_required", "compacted")


@dataclass(frozen=True, slots=True)
class RunRecord:
    """The stored facts of one ended run."""

    run: Any
    """A row with the ``Run`` columns plus ``profile_name``."""
    input_text: str | None = None
    output_text: str | None = None
    model_calls: Sequence[Any] = field(default_factory=tuple)
    tool_calls: Sequence[Any] = field(default_factory=tuple)
    events: Sequence[Any] = field(default_factory=tuple)


def span_id(run_id: uuid.UUID, *parts: object) -> str:
    """A stable span id, so the same run always gives the same ids."""
    seed = ":".join(str(part) for part in (run_id, *parts))
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def build_trace(
    record: RunRecord, *, environment: str | None = None, include_content: bool = True
) -> list[Span]:
    """The spans of one run. The trace id is the run id."""
    run = record.run
    trace_id = run.id.hex
    root_id = span_id(run.id, "run")
    start = run.started_at or run.created_at
    end = run.finished_at or start
    shared: dict[str, AttributeValue] = {
        "langfuse.trace.name": run.profile_name,
        "langfuse.session.id": str(run.session_id),
        "langfuse.trace.tags": [run.runtime, run.status],
        "langfuse.trace.metadata.org_id": str(run.org_id),
        "langfuse.trace.metadata.run_id": str(run.id),
        "langfuse.trace.metadata.runtime": run.runtime,
    }
    if run.triggered_by_user_id is not None:
        shared["langfuse.user.id"] = str(run.triggered_by_user_id)
    if environment:
        shared["langfuse.environment"] = environment

    root: dict[str, AttributeValue] = {
        **shared,
        "langfuse.observation.type": "agent",
        "langfuse.observation.metadata.status": run.status,
        "langfuse.observation.metadata.attempts": str(run.attempts),
    }
    if include_content and record.input_text is not None:
        root["langfuse.observation.input"] = _cut(record.input_text)
    if include_content and record.output_text is not None:
        root["langfuse.observation.output"] = _cut(record.output_text)
    error = None
    if run.status == "failed":
        error = f"{run.failure_code}: {run.failure_message or ''}".strip()
        root["langfuse.observation.level"] = "ERROR"
        root["langfuse.observation.status_message"] = error
        root["langfuse.observation.metadata.failure_code"] = run.failure_code or ""
    elif run.status == "cancelled":
        root["langfuse.observation.level"] = "WARNING"
        root["langfuse.observation.status_message"] = run.failure_code or "cancelled"
    spans = [Span(trace_id, root_id, "run", start, end, attributes=root, error=error)]

    for call in record.model_calls:
        call_end = call.created_at
        call_start = call_end - timedelta(milliseconds=call.duration_ms)
        attributes: dict[str, AttributeValue] = {
            **shared,
            "langfuse.observation.type": "generation",
            "langfuse.observation.model.name": call.model,
            "langfuse.observation.usage_details": json_attribute(
                {
                    "input": call.input_tokens,
                    "output": call.output_tokens,
                    "cache_read_input_tokens": call.cache_read_tokens,
                    "cache_creation_input_tokens": call.cache_write_tokens
                    + call.cache_write_long_tokens,
                }
            ),
            "langfuse.observation.cost_details": json_attribute({"total": float(call.cost_usd)}),
            "langfuse.observation.metadata.provider": call.provider,
            "langfuse.observation.metadata.endpoint": call.endpoint,
            "langfuse.observation.metadata.status_code": str(call.status_code),
        }
        if call.usage_estimated:
            attributes["langfuse.observation.metadata.usage_estimated"] = "true"
        call_error = None
        if call.status_code >= 400:
            call_error = f"HTTP {call.status_code}"
            attributes["langfuse.observation.level"] = "ERROR"
        spans.append(
            Span(
                trace_id,
                span_id(run.id, "model", call.call_id),
                call.model,
                call_start,
                call_end,
                parent_id=root_id,
                attributes=attributes,
                error=call_error,
            )
        )

    for tool in record.tool_calls:
        attributes = {
            **shared,
            "langfuse.observation.type": "tool",
            "langfuse.observation.metadata.status": tool.status,
            "langfuse.observation.metadata.decision": tool.decision or "",
        }
        tool_error = None
        if tool.status == "failed":
            tool_error = "the tool failed"
            attributes["langfuse.observation.level"] = "ERROR"
        elif tool.status == "denied":
            attributes["langfuse.observation.level"] = "WARNING"
            attributes["langfuse.observation.status_message"] = "denied"
        spans.append(
            Span(
                trace_id,
                span_id(run.id, "tool", tool.call_id),
                tool.tool_name,
                tool.created_at,
                tool.finished_at or end,
                parent_id=root_id,
                attributes=attributes,
                error=tool_error,
            )
        )

    for event in record.events:
        attributes = {**shared, "langfuse.observation.type": "event"}
        payload = event.payload or {}
        if event.type == "approval_required":
            name = "approval"
            attributes["langfuse.observation.metadata.tool_name"] = str(
                payload.get("tool_name", "")
            )
        else:
            name = "compaction"
            for key in ("tokens_before", "tokens_after"):
                attributes[f"langfuse.observation.metadata.{key}"] = str(payload.get(key, ""))
        spans.append(
            Span(
                trace_id,
                span_id(run.id, "event", event.seq),
                name,
                event.created_at,
                event.created_at,
                parent_id=root_id,
                attributes=attributes,
            )
        )
    return spans


def _cut(text: str) -> str:
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS] + " [cut]"


class TraceExporter:
    """Sends the traces of ended runs to Langfuse, one run at a time.

    A run is sent when it ended less than ``max_age`` ago and has no ``trace_exported_at``.
    When Langfuse rejects a trace for good (``LangfuseError.permanent``), the run is marked
    and skipped. When Langfuse is down, the run waits for the next pass.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        client: LangfuseClient,
        *,
        environment: str | None = None,
        include_content: bool = True,
        max_age: timedelta = timedelta(days=1),
        batch: int = 50,
    ) -> None:
        self._engine = engine
        self._client = client
        self._environment = environment
        self._include_content = include_content
        self._max_age = max_age
        self._batch = batch

    async def export_pending(self) -> int:
        """Sends up to ``batch`` traces. Returns how many runs it marked as sent."""
        sent = 0
        for _ in range(self._batch):
            outcome = await self._export_one()
            if outcome is None:
                break
            sent += outcome
        return sent

    async def serve(self, stop: asyncio.Event, *, interval_s: float = 10.0) -> None:
        while not stop.is_set():
            try:
                await self.export_pending()
            except Exception:
                log.exception("the trace export failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)

    async def _export_one(self) -> int | None:
        """Sends one trace. Returns 1 when sent, 0 when skipped for good, or None when no run
        is ready or Langfuse is down."""
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(*Run.__table__.c, AgentProfile.name.label("profile_name"))
                    .join(
                        AgentProfile,
                        (AgentProfile.org_id == Run.org_id)
                        & (AgentProfile.id == Run.agent_profile_id),
                    )
                    .where(
                        Run.status.in_(ENDED_RUN_STATUSES),
                        Run.trace_exported_at.is_(None),
                        Run.finished_at > func.now() - self._max_age,
                    )
                    .order_by(Run.finished_at)
                    .limit(1)
                    .with_for_update(of=Run, skip_locked=True)
                )
            ).one_or_none()
            if row is None:
                return None
            with log_context(org_id=row.org_id, session_id=row.session_id, run_id=row.id):
                record = await _load(connection, row)
                spans = build_trace(
                    record,
                    environment=self._environment,
                    include_content=self._include_content,
                )
                try:
                    await self._client.send(spans)
                except LangfuseError as error:
                    if not error.permanent:
                        log.warning("the trace waits for the next pass: %s", error)
                        return None
                    log.error("the trace is skipped: %s", error)
                    outcome = 0
                else:
                    outcome = 1
                await connection.execute(
                    update(Run).where(Run.id == row.id).values(trace_exported_at=func.now())
                )
                return outcome


async def _load(connection: AsyncConnection, run: Any) -> RunRecord:
    keys = (Message.org_id == run.org_id, Message.run_id == run.id)
    messages = (
        await connection.execute(
            select(Message.role, Message.content).where(*keys).order_by(Message.seq)
        )
    ).all()
    model_calls = (
        await connection.execute(
            select(*ModelCall.__table__.c)
            .where(ModelCall.org_id == run.org_id, ModelCall.run_id == run.id)
            .order_by(ModelCall.created_at, ModelCall.id)
        )
    ).all()
    tool_calls = (
        await connection.execute(
            select(*ToolCall.__table__.c)
            .where(ToolCall.org_id == run.org_id, ToolCall.run_id == run.id)
            .order_by(ToolCall.created_at)
        )
    ).all()
    events = (
        await connection.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.payload, RunEvent.created_at)
            .where(
                RunEvent.org_id == run.org_id,
                RunEvent.run_id == run.id,
                RunEvent.type.in_(EVENT_TYPES),
            )
            .order_by(RunEvent.seq)
        )
    ).all()
    return RunRecord(
        run=run,
        input_text=_text(messages, "user"),
        output_text=_text(messages, "assistant"),
        model_calls=model_calls,
        tool_calls=tool_calls,
        events=events,
    )


def _text(messages: Sequence[Any], role: str) -> str | None:
    parts = [
        str(message.content.get("text", ""))
        for message in messages
        if message.role == role and isinstance(message.content, Mapping)
    ]
    return "\n\n".join(parts) if parts else None
