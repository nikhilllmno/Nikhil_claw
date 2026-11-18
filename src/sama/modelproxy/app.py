"""The model proxy service (docs/architecture.md, Section 16.6, decision D6).

The proxy is the only path from a sandbox to a model provider. It accepts only a run token,
checks the model and the budgets, forwards the call with the provider key, passes the
response through unchanged, and records the usage and the cost of the call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, SecretStr

from sama.modelproxy.prices import PriceTable, Provider, TokenCounts
from sama.modelproxy.store import CallRecord, UsageStore, day_start, month_start
from sama.modelproxy.usage import Endpoint, UsageReader, estimate_counts
from sama.tenancy.run_token import InvalidRunTokenError, RunTokenClaims, RunTokenSigner

log = logging.getLogger("sama.modelproxy")

MAX_REQUEST_BYTES: Final = 32 * 1024 * 1024
BUDGET_STATUS: Final = 402
_REQUEST_HEADERS: Final = frozenset(
    {"anthropic-version", "anthropic-beta", "openai-beta", "content-type"}
)
_RESPONSE_HEADERS: Final = frozenset({"content-type", "request-id", "x-request-id", "retry-after"})
_RESPONSE_PREFIXES: Final = ("anthropic-ratelimit-", "x-ratelimit-", "openai-")
_ANTHROPIC_SPEEDS: Final = frozenset({"standard"})
_ANTHROPIC_GEOS: Final = frozenset({"global"})
_OPENAI_TIERS: Final = frozenset({"auto", "default"})


class ProxySettings(BaseModel):
    """Upstream URLs and provider keys. The keys stay on the control plane."""

    model_config = ConfigDict(frozen=True)

    anthropic_url: str = "https://api.anthropic.com"
    openai_url: str = "https://api.openai.com"
    anthropic_key: SecretStr | None = None
    openai_key: SecretStr | None = None
    max_request_bytes: int = MAX_REQUEST_BYTES
    upstream_timeout_s: float = 600.0
    connect_timeout_s: float = 10.0
    drain_s: float = 600.0
    """After a client stops reading, the proxy reads the rest of the response for at most
    this time to get the final usage."""
    record_attempts: int = 3
    record_retry_s: float = 0.2


@dataclass(frozen=True)
class _Route:
    provider: Provider
    endpoint: Endpoint
    upstream_path: str

    @property
    def recorded(self) -> bool:
        return self.endpoint is not Endpoint.COUNT_TOKENS


ROUTES: Final = {
    "/anthropic/v1/messages": _Route(Provider.ANTHROPIC, Endpoint.MESSAGES, "/v1/messages"),
    "/anthropic/v1/messages/count_tokens": _Route(
        Provider.ANTHROPIC, Endpoint.COUNT_TOKENS, "/v1/messages/count_tokens"
    ),
    "/openai/v1/responses": _Route(Provider.OPENAI, Endpoint.RESPONSES, "/v1/responses"),
    "/openai/v1/chat/completions": _Route(
        Provider.OPENAI, Endpoint.CHAT_COMPLETIONS, "/v1/chat/completions"
    ),
}


@dataclass(frozen=True)
class _Call:
    """One accepted call: what the record needs."""

    claims: RunTokenClaims
    route: _Route
    model: str
    body: dict[str, Any]
    request_bytes: int
    started: float


@dataclass
class _Pump:
    """The state of one provider response that a pump task reads."""

    call: _Call
    upstream: httpx.Response
    reader: UsageReader
    drain_s: float
    queue: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    gone: bool = False
    done: bool = False
    deadline: asyncio.Timeout | None = None

    def client_gone(self) -> None:
        """The client stopped reading. The pump reads on for at most ``drain_s``."""
        if self.gone:
            return
        self.gone = True
        if not self.done and self.deadline is not None and self.deadline.when() is None:
            self.deadline.reschedule(asyncio.get_running_loop().time() + self.drain_s)


class ProxyError(Exception):
    """A refused call. The message is safe to show to the sandbox."""

    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message


def error_response(provider: Provider, error: ProxyError) -> JSONResponse:
    """The error in the format of the provider, so its SDK shows it well."""
    body: dict[str, Any]
    if provider is Provider.ANTHROPIC:
        body = {"type": "error", "error": {"type": error.kind, "message": error.message}}
    else:
        body = {"error": {"type": error.kind, "code": error.kind, "message": error.message}}
    return JSONResponse(body, status_code=error.status)


def _token(request: Request) -> str:
    token = request.headers.get("x-api-key", "")
    if not token:
        auth = request.headers.get("authorization", "")
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
    return token


class ModelProxy:
    """The proxy logic. ``app`` returns the ASGI app."""

    def __init__(
        self,
        *,
        signer: RunTokenSigner,
        store: UsageStore,
        prices: PriceTable,
        settings: ProxySettings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._signer = signer
        self._store = store
        self._prices = prices
        self._settings = settings
        self._clock = clock
        timeout = httpx.Timeout(settings.upstream_timeout_s, connect=settings.connect_timeout_s)
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._tasks: set[asyncio.Task[None]] = set()
        self.unrecorded: list[CallRecord] = []
        """Records that the store did not take. They block new calls of their org."""

    async def aclose(self) -> None:
        """Waits up to 5 seconds for the streams in progress to write their records."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=5)
        await self._client.aclose()

    def app(self) -> FastAPI:
        """The ASGI app. It closes the upstream client when the server stops."""

        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            yield
            await self.aclose()

        app = FastAPI(
            title="sama model proxy",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
            lifespan=lifespan,
        )

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        for path, route in ROUTES.items():
            app.add_api_route(path, self._handler(route), methods=["POST"])
        return app

    def _handler(self, route: _Route) -> Callable[[Request], Any]:
        async def handle(request: Request) -> Response:
            try:
                return await self._call(route, request)
            except ProxyError as error:
                return error_response(route.provider, error)

        return handle

    async def _call(self, route: _Route, request: Request) -> Response:
        claims = self._verify(request)
        body, request_bytes = await self._body(request)
        model = self._check(route, body)
        if route.recorded:
            await self._check_budget(claims)
        stream = body.get("stream") is True
        if route.provider is Provider.OPENAI:
            # `auto` can use a tier of the project with other prices, so the proxy asks for the
            # standard tier, which the price table has.
            body["service_tier"] = "default"
        if route.endpoint is Endpoint.CHAT_COMPLETIONS and stream:
            options = body.get("stream_options")
            body["stream_options"] = {
                **(options if isinstance(options, dict) else {}),
                "include_usage": True,
            }
        started = time.monotonic()
        upstream = await self._send(route, request, body)
        reader = UsageReader(route.provider, stream=stream)

        pump = _Pump(
            call=_Call(claims, route, model, body, request_bytes, started),
            upstream=upstream,
            reader=reader,
            drain_s=self._settings.drain_s,
        )
        self._spawn(self._pump(pump))

        async def relay() -> AsyncIterator[bytes]:
            try:
                while (chunk := await pump.queue.get()) is not None:
                    yield chunk
            finally:
                pump.client_gone()

        headers = {
            name: value
            for name, value in upstream.headers.items()
            if name in _RESPONSE_HEADERS or name.startswith(_RESPONSE_PREFIXES)
        }
        return StreamingResponse(relay(), status_code=upstream.status_code, headers=headers)

    def _verify(self, request: Request) -> RunTokenClaims:
        token = _token(request)
        if not token:
            raise ProxyError(401, "authentication_error", "a run token is required")
        try:
            return self._signer.verify(token)
        except InvalidRunTokenError as exc:
            raise ProxyError(401, "authentication_error", str(exc)) from None

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _pump(self, pump: _Pump) -> None:
        """Reads the provider response in its own task, so a client that stops reading cannot
        cancel it. After the client stops, it reads on for at most ``drain_s`` to get the final
        usage. Then it writes the record."""
        call, reader = pump.call, pump.reader
        wanted = call.route.recorded and pump.upstream.is_success
        try:
            async with asyncio.timeout(None) as pump.deadline:
                if pump.gone:  # the client stopped before the pump started
                    pump.gone = False
                    pump.client_gone()
                async for chunk in pump.upstream.aiter_raw():
                    reader.feed(chunk)
                    if not pump.gone:
                        pump.queue.put_nowait(chunk)
                    elif not wanted or reader.final:
                        break
        except (httpx.HTTPError, TimeoutError) as exc:
            log.warning("the provider stream ended early run=%s error=%r", call.claims.run_id, exc)
        finally:
            pump.done = True
            try:
                # The record is written before the stream ends, so a client that reads to the
                # end sees its call in the store.
                await asyncio.shield(self._finish(pump))
            finally:
                pump.queue.put_nowait(None)

    async def _finish(self, pump: _Pump) -> None:
        call, reader = pump.call, pump.reader
        await pump.upstream.aclose()
        if not call.route.recorded:
            return
        counts, estimated = reader.counts(), False
        if pump.upstream.is_success and not reader.final:
            counts, estimated = estimate_counts(call.request_bytes, call.body, counts), True
        await self._record(call, pump.upstream, counts, estimated)

    async def _body(self, request: Request) -> tuple[dict[str, Any], int]:
        limit = self._settings.max_request_bytes
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise ProxyError(413, "request_too_large", "the request body is too large")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > limit:
                raise ProxyError(413, "request_too_large", "the request body is too large")
        try:
            body = json.loads(data)
        except ValueError:
            raise ProxyError(400, "invalid_request_error", "the body is not JSON") from None
        if not isinstance(body, dict):
            raise ProxyError(400, "invalid_request_error", "the body is not a JSON object")
        return body, len(data)

    def _check(self, route: _Route, body: Mapping[str, Any]) -> str:
        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise ProxyError(400, "invalid_request_error", "the request has no model")
        if self._prices.lookup(route.provider, model) is None:
            raise ProxyError(400, "invalid_request_error", f"the model {model!r} is not allowed")
        if route.provider is Provider.ANTHROPIC:
            _allowed(body, "speed", _ANTHROPIC_SPEEDS)
            _allowed(body, "inference_geo", _ANTHROPIC_GEOS)
        else:
            if body.get("background"):
                raise ProxyError(400, "invalid_request_error", "background mode is not allowed")
            _allowed(body, "service_tier", _OPENAI_TIERS)
        return model

    async def _check_budget(self, claims: RunTokenClaims) -> None:
        """Fails closed: a store that cannot read, or a record that the store did not take,
        refuses the call."""
        await self._flush()
        if any(call.org_id == claims.org_id for call in self.unrecorded):
            raise ProxyError(503, "api_error", "the proxy cannot record usage now")
        try:
            await self._check_limits(claims)
        except ProxyError:
            raise
        except Exception:
            log.exception("the budget check failed run=%s", claims.run_id)
            raise ProxyError(503, "api_error", "the proxy cannot check the budget now") from None

    async def _check_limits(self, claims: RunTokenClaims) -> None:
        if claims.max_tokens is not None or claims.max_cost_usd is not None:
            spend = await self._store.run_spend(claims.org_id, claims.run_id)
            if claims.max_tokens is not None and spend.tokens >= claims.max_tokens:
                raise _budget("the run reached max_tokens")
            if claims.max_cost_usd is not None and spend.cost_usd >= claims.max_cost_usd:
                raise _budget("the run reached max_cost_usd")
        limits = await self._store.org_limits(claims.org_id)
        now = self._clock()
        monthly, daily, user = (
            limits.model_usd_per_month,
            limits.model_usd_per_user_day,
            claims.user_id,
        )
        if monthly is not None:
            spent = await self._store.org_cost(claims.org_id, month_start(now))
            if spent >= monthly:
                raise _budget("the org reached its monthly model limit")
        if daily is not None and user is not None:
            spent = await self._store.user_cost(claims.org_id, user, day_start(now))
            if spent >= daily:
                raise _budget("the user reached the daily model limit")

    async def _send(self, route: _Route, request: Request, body: dict[str, Any]) -> httpx.Response:
        headers = {
            name: value for name, value in request.headers.items() if name in _REQUEST_HEADERS
        }
        headers["content-type"] = "application/json"
        # Identity encoding: the usage reader parses the bytes that the client gets.
        headers["accept-encoding"] = "identity"
        if route.provider is Provider.ANTHROPIC:
            key, base = self._settings.anthropic_key, self._settings.anthropic_url
            if key is not None:
                headers["x-api-key"] = key.get_secret_value()
        else:
            key, base = self._settings.openai_key, self._settings.openai_url
            if key is not None:
                headers["authorization"] = f"Bearer {key.get_secret_value()}"
        if key is None:
            raise ProxyError(503, "api_error", f"the proxy has no {route.provider} key")
        upstream = self._client.build_request(
            "POST",
            base.rstrip("/") + route.upstream_path,
            content=json.dumps(body).encode(),
            headers=headers,
        )
        try:
            return await self._client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            log.warning("the provider did not answer provider=%s error=%s", route.provider, exc)
            raise ProxyError(502, "api_error", "the provider did not answer") from None

    async def _record(
        self, request: _Call, upstream: httpx.Response, counts: TokenCounts, estimated: bool
    ) -> None:
        claims, route, model = request.claims, request.route, request.model
        cost = self._prices.cost(route.provider, model, counts)
        call = CallRecord(
            org_id=claims.org_id,
            workspace_id=claims.workspace_id,
            session_id=claims.session_id,
            run_id=claims.run_id,
            user_id=claims.user_id,
            provider=route.provider,
            model=model,
            endpoint=route.endpoint,
            status_code=upstream.status_code,
            counts=counts,
            usage_estimated=estimated,
            cost_usd=cost,
            duration_ms=int((time.monotonic() - request.started) * 1000),
            created_at=self._clock(),
        )
        if not await self._save(call):
            self.unrecorded.append(call)
            log.error("the call record was not written; the org is blocked run=%s", call.run_id)
        log.info(
            "model call run=%s model=%s status=%s tokens=%s cost_usd=%s",
            claims.run_id,
            model,
            upstream.status_code,
            counts.total_tokens,
            cost,
        )

    async def _save(self, call: CallRecord) -> bool:
        for attempt in range(self._settings.record_attempts):
            if attempt:
                await anyio.sleep(self._settings.record_retry_s * attempt)
            try:
                await self._store.record(call)
            except Exception:
                log.exception("the call record write failed run=%s", call.run_id)
            else:
                return True
        return False

    async def _flush(self) -> None:
        """Tries again to write the records that the store did not take."""
        pending, self.unrecorded = self.unrecorded, []
        for call in pending:
            try:
                await self._store.record(call)
            except Exception:
                self.unrecorded.append(call)


def _allowed(body: Mapping[str, Any], key: str, values: frozenset[str]) -> None:
    value = body.get(key)
    if value is not None and value not in values:
        raise ProxyError(400, "invalid_request_error", f"{key}={value!r} is not allowed")


def _budget(message: str) -> ProxyError:
    return ProxyError(BUDGET_STATUS, "budget_exceeded", message)
