"""A small OTLP/HTTP JSON client for Langfuse (docs/architecture.md, Section 18.2).

Langfuse receives traces on ``/api/public/otel/v1/traces``. The old ``/api/public/ingestion``
endpoint is closed on Langfuse Cloud, so sama sends OpenTelemetry spans. Sama builds each
span when the run has ended, so it sends each span one time and needs no OpenTelemetry SDK.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
from pydantic import SecretStr

TRACES_PATH: Final = "/api/public/otel/v1/traces"
DELETE_PATH: Final = "/api/public/traces"
"""The bulk delete of traces (docs/architecture.md, Section 17.4). Langfuse deletes them later,
in the background."""
MAX_DELETE_IDS: Final = 1000
"""The most trace ids in one delete request."""
SCOPE: Final = "sama.observability"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

AttributeValue = str | int | float | bool | Sequence[str]
"""An OTLP attribute value. Structured values go in as JSON strings."""


@dataclass(frozen=True, slots=True)
class Span:
    """One finished span. ``trace_id`` has 32 hex characters, the span ids have 16."""

    trace_id: str
    span_id: str
    name: str
    start: datetime
    end: datetime
    parent_id: str | None = None
    attributes: Mapping[str, AttributeValue] = field(default_factory=dict)
    error: str | None = None
    """When set, the span has the OTLP status ``ERROR`` with this message."""


RETRY_STATUSES: Final = frozenset({401, 403, 408, 429, 503})
"""Answers that tell that Langfuse did not store the spans and can store them later: a wrong
or revoked key, a request timeout, a rate limit, or a short outage."""

_NOT_SENT: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
"""Transport errors before the request left sama."""


class LangfuseError(Exception):
    """Langfuse did not confirm the spans. ``retry`` is True only when Langfuse surely did not
    store them, so a second send makes no duplicate. Langfuse does not remove a span that is
    sent two times. The message holds no key."""

    def __init__(self, message: str, *, retry: bool) -> None:
        super().__init__(message)
        self.retry = retry


class LangfuseClient:
    def __init__(
        self,
        base_url: str,
        public_key: str,
        secret_key: SecretStr,
        *,
        service: str = "sama",
        environment: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._url = base_url.rstrip("/") + TRACES_PATH
        self._delete_url = base_url.rstrip("/") + DELETE_PATH
        self._resource = _attributes(
            {"service.name": service}
            | ({"deployment.environment.name": environment} if environment else {})
        )
        self._http = httpx.AsyncClient(
            auth=httpx.BasicAuth(public_key, secret_key.get_secret_value()),
            headers={"x-langfuse-ingestion-version": "4"},
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def payload(self, spans: Sequence[Span]) -> dict[str, object]:
        """The OTLP JSON body for ``spans``."""
        return {
            "resourceSpans": [
                {
                    "resource": {"attributes": self._resource},
                    "scopeSpans": [
                        {"scope": {"name": SCOPE}, "spans": [_span(span) for span in spans]}
                    ],
                }
            ]
        }

    async def send(self, spans: Sequence[Span]) -> None:
        """Sends ``spans`` in one request. Raises ``LangfuseError``."""
        if not spans:
            return
        try:
            response = await self._http.post(self._url, json=self.payload(spans))
        except httpx.HTTPError as error:
            # After a read timeout or a broken answer, Langfuse can have the spans already.
            raise LangfuseError(
                f"Langfuse did not answer: {type(error).__name__}",
                retry=isinstance(error, _NOT_SENT),
            ) from None
        if response.is_success:
            rejected = _rejected(response)
            if rejected:
                raise LangfuseError(f"Langfuse rejected {rejected} spans", retry=False)
            return
        status = response.status_code
        raise LangfuseError(f"Langfuse answered HTTP {status}", retry=status in RETRY_STATUSES)

    async def delete_traces(self, trace_ids: Sequence[str]) -> None:
        """Asks Langfuse to delete the traces, ``MAX_DELETE_IDS`` in one request. A second
        request for the same ids does no harm. Raises ``LangfuseError``."""
        ids = sorted(set(trace_ids))
        for start in range(0, len(ids), MAX_DELETE_IDS):
            batch = ids[start : start + MAX_DELETE_IDS]
            try:
                response = await self._http.request(
                    "DELETE", self._delete_url, json={"traceIds": batch}
                )
            except httpx.HTTPError as error:
                raise LangfuseError(
                    f"Langfuse did not answer: {type(error).__name__}", retry=True
                ) from None
            if not response.is_success:
                status = response.status_code
                raise LangfuseError(f"Langfuse answered HTTP {status}", retry=True)


def _rejected(response: httpx.Response) -> int:
    """The ``partialSuccess.rejectedSpans`` count of an OTLP answer, or 0."""
    try:
        return int(response.json()["partialSuccess"]["rejectedSpans"] or 0)
    except (KeyError, TypeError, ValueError):
        return 0


def _span(span: Span) -> dict[str, object]:
    body: dict[str, object] = {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "name": span.name,
        "kind": 1,  # SPAN_KIND_INTERNAL
        "startTimeUnixNano": str(_nanos(span.start)),
        "endTimeUnixNano": str(_nanos(max(span.start, span.end))),
        "attributes": _attributes(span.attributes),
        "status": {"code": 2, "message": span.error} if span.error else {"code": 1},
    }
    if span.parent_id:
        body["parentSpanId"] = span.parent_id
    return body


def _nanos(moment: datetime) -> int:
    return (moment - _EPOCH) // timedelta(microseconds=1) * 1_000


def _attributes(values: Mapping[str, AttributeValue]) -> list[dict[str, object]]:
    return [{"key": key, "value": _value(value)} for key, value in values.items()]


def _value(value: AttributeValue) -> dict[str, object]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    return {"arrayValue": {"values": [{"stringValue": str(item)} for item in value]}}


def json_attribute(value: object) -> str:
    """A structured value as the JSON string that Langfuse expects."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
