"""The model proxy service (docs/architecture.md, Section 16.6, decision D6).

The proxy is the only path from a sandbox to a model provider. It accepts only a run token,
checks the model and the budgets, forwards the call with the provider key of the org (from the
vault, or the default key of the process), passes the response through unchanged, and records
the usage and the cost of the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Final

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, SecretStr
from starlette.types import Receive, Scope, Send

from sama.modelproxy.prices import PriceTable, Provider, TokenCounts
from sama.modelproxy.store import CallRecord, UsageStore, day_start, month_start
from sama.modelproxy.usage import Endpoint, UsageReader, estimate_counts
from sama.tenancy.run_token import InvalidRunTokenError, RunTokenClaims, RunTokenSigner
from sama.vault.store import OrgKeys, SecretName

log = logging.getLogger("sama.modelproxy")

MAX_REQUEST_BYTES: Final = 32 * 1024 * 1024
BUDGET_STATUS: Final = 402
QUEUE_CHUNKS: Final = 16
"""The pump holds at most this many response chunks for a slow client. Then it waits."""
CLOSE_WAIT_S: Final = 5.0
STOP_WAIT_S: Final = 10.0
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


class _End(Enum):
    """The last item in the queue of a pump."""

    OK = "ok"
    FAILED = "failed"


class UpstreamStreamError(Exception):
    """The provider stream broke. The proxy ends the client stream with an error, not with a
    clean end, so the client does not take a cut response as complete."""


@dataclass(eq=False)
class _Pump:
    """The state of one provider response that a pump task reads."""

    call: _Call
    upstream: httpx.Response
    reader: UsageReader
    drain_s: float
    queue: asyncio.Queue[bytes | _End] = field(default_factory=asyncio.Queue)
    """Chunks for the client, then one ``_End``. ``space`` bounds the chunks, so the end
    marker never waits."""
    space: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(QUEUE_CHUNKS))
    gone: bool = False
    stopped: bool = False
    reading: bool = False
    deadline: asyncio.Timeout | None = None

    def client_gone(self) -> None:
        """The client stopped reading. The queued chunks go, and the pump reads on for at most
        ``drain_s`` to get the final usage."""
        if self.gone:
            return
        self.gone = True
        while not self.queue.empty():
            self.queue.get_nowait()
        self.space.release()  # wakes a pump that waits for space
        self.limit(self.drain_s)

    def stop(self) -> None:
        """The proxy stops: the pump stops reading now and writes its record."""
        self.stopped = True
        self.limit(0)

    def limit(self, seconds: float) -> None:
        """Ends the read after ``seconds`` at the latest."""
        if not self.reading or self.deadline is None:
            return
        when = asyncio.get_running_loop().time() + seconds
        current = self.deadline.when()
        if current is None or when < current:
            self.deadline.reschedule(when)


class _PumpResponse(StreamingResponse):
    """A stream response that tells the pump when the client is gone, also when the client
    goes before the body starts."""

    def __init__(self, pump: _Pump, content: AsyncIterator[bytes], **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._pump = pump

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._pump.client_gone()


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
        keys: OrgKeys | None = None,
    ) -> None:
        """``keys`` gives the provider key of each org (the vault, Section 16.3). Without
        it, or when an org has no key, the proxy uses the keys of ``settings``."""
        self._signer = signer
        self._keys = keys
        self._store = store
        self._prices = prices
        self._settings = settings
        self._clock = clock
        timeout = httpx.Timeout(settings.upstream_timeout_s, connect=settings.connect_timeout_s)
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._tasks: set[asyncio.Task[None]] = set()
        self._pumps: set[_Pump] = set()
        self._flush_lock = asyncio.Lock()
        self.unrecorded: list[CallRecord] = []
        """Records that the store did not take. They block new calls of their org until a
        write succeeds."""

    async def aclose(self) -> None:
        """Stops the proxy. Streams in progress get ``CLOSE_WAIT_S`` to end. Then each pump
        stops reading and writes its record (an estimate if the usage is not final). A record
        that still is not written is logged as JSON for a manual fix."""
        pending = set(self._tasks)
        if pending:
            _, pending = await asyncio.wait(pending, timeout=CLOSE_WAIT_S)
        if pending:
            for pump in list(self._pumps):
                pump.stop()
            _, pending = await asyncio.wait(pending, timeout=STOP_WAIT_S)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            async with asyncio.timeout(STOP_WAIT_S):
                await self._flush()
        except TimeoutError:
            log.error("the last record writes did not end in time")
        for call in self.unrecorded:
            log.error("the proxy stops with an unwritten call record: %s", call.model_dump_json())
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
        upstream = await self._send(route, request, body, claims.org_id)
        reader = UsageReader(route.provider, stream=stream)

        pump = _Pump(
            call=_Call(claims, route, model, body, request_bytes, started),
            upstream=upstream,
            reader=reader,
            drain_s=self._settings.drain_s,
        )
        self._pumps.add(pump)
        self._spawn(self._pump(pump))

        async def relay() -> AsyncIterator[bytes]:
            try:
                while True:
                    item = await pump.queue.get()
                    if item is _End.OK:
                        return
                    if item is _End.FAILED:
                        raise UpstreamStreamError("the provider stream broke")
                    pump.space.release()
                    yield item
            finally:
                pump.client_gone()

        headers = {
            name: value
            for name, value in upstream.headers.items()
            if name in _RESPONSE_HEADERS or name.startswith(_RESPONSE_PREFIXES)
        }
        return _PumpResponse(pump, relay(), status_code=upstream.status_code, headers=headers)

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
        usage. It writes the record before the client stream ends."""
        call, reader = pump.call, pump.reader
        wanted = call.route.recorded and pump.upstream.is_success
        end = _End.OK
        try:
            async with asyncio.timeout(None) as pump.deadline:
                pump.reading = True
                if pump.stopped:
                    pump.limit(0)
                elif pump.gone:
                    pump.limit(pump.drain_s)
                async for chunk in pump.upstream.aiter_raw():
                    reader.feed(chunk)
                    if not pump.gone:
                        # Waits while the client holds QUEUE_CHUNKS chunks. client_gone wakes it.
                        await pump.space.acquire()
                    if not pump.gone:
                        pump.queue.put_nowait(chunk)
                    elif not wanted or reader.final:
                        break
        except TimeoutError:
            end = _End.FAILED  # only the proxy stop ends a read while the client reads
        except httpx.HTTPError as exc:
            log.warning("the provider stream broke run=%s error=%r", call.claims.run_id, exc)
            end = _End.FAILED
        finally:
            pump.reading = False
            try:
                await self._finish(pump)
            finally:
                self._pumps.discard(pump)
                pump.queue.put_nowait(end)  # never waits, also after a cancel

    async def _finish(self, pump: _Pump) -> None:
        """Builds the record first. A cancel during the cleanup or the write fences it."""
        call, reader = pump.call, pump.reader
        record: CallRecord | None = None
        if call.route.recorded:
            counts, estimated = reader.counts(), False
            if pump.upstream.is_success and not reader.final:
                counts, estimated = estimate_counts(call.request_bytes, call.body, counts), True
            record = self._record_for(call, pump.upstream.status_code, counts, estimated)
        try:
            with contextlib.suppress(httpx.HTTPError):
                await pump.upstream.aclose()
            if record is not None and not await self._save(record):
                self._fence(record)
        except asyncio.CancelledError:
            if record is not None:
                self._fence(record)
            raise

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

    async def _key(self, provider: Provider, org_id: str) -> SecretStr | None:
        if provider is Provider.ANTHROPIC:
            name, default = SecretName.ANTHROPIC_API_KEY, self._settings.anthropic_key
        else:
            name, default = SecretName.OPENAI_API_KEY, self._settings.openai_key
        if self._keys is None:
            return default
        try:
            return await self._keys.get(org_id, name) or default
        except Exception:
            log.exception("the provider key of an org cannot be read org=%s", org_id)
            raise ProxyError(
                503, "api_error", "the proxy cannot read the provider key now"
            ) from None

    async def _send(
        self, route: _Route, request: Request, body: dict[str, Any], org_id: str
    ) -> httpx.Response:
        headers = {
            name: value for name, value in request.headers.items() if name in _REQUEST_HEADERS
        }
        headers["content-type"] = "application/json"
        # Identity encoding: the usage reader parses the bytes that the client gets.
        headers["accept-encoding"] = "identity"
        key = await self._key(route.provider, org_id)
        if route.provider is Provider.ANTHROPIC:
            base = self._settings.anthropic_url
            if key is not None:
                headers["x-api-key"] = key.get_secret_value()
        else:
            base = self._settings.openai_url
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

    def _record_for(
        self, request: _Call, status_code: int, counts: TokenCounts, estimated: bool
    ) -> CallRecord:
        claims, route, model = request.claims, request.route, request.model
        cost = self._prices.cost(route.provider, model, counts)
        log.info(
            "model call run=%s model=%s status=%s tokens=%s cost_usd=%s estimated=%s",
            claims.run_id,
            model,
            status_code,
            counts.total_tokens,
            cost,
            estimated,
        )
        return CallRecord(
            org_id=claims.org_id,
            workspace_id=claims.workspace_id,
            session_id=claims.session_id,
            run_id=claims.run_id,
            user_id=claims.user_id,
            provider=route.provider,
            model=model,
            endpoint=route.endpoint,
            status_code=status_code,
            counts=counts,
            usage_estimated=estimated,
            cost_usd=cost,
            duration_ms=int((time.monotonic() - request.started) * 1000),
            created_at=self._clock(),
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

    def _fence(self, call: CallRecord) -> None:
        if call in self.unrecorded:
            return
        self.unrecorded.append(call)
        log.error("the call record was not written; the org is blocked run=%s", call.run_id)

    async def _flush(self) -> None:
        """Tries again to write the records that the store did not take. A record stays in
        ``unrecorded``, so it blocks its org, until its write succeeds. The record id makes a
        second write of the same record harmless."""
        if not self.unrecorded:
            return
        async with self._flush_lock:
            for call in list(self.unrecorded):
                try:
                    await self._store.record(call)
                except Exception:
                    log.exception("the call record write failed again run=%s", call.run_id)
                    continue
                self.unrecorded.remove(call)


def _allowed(body: Mapping[str, Any], key: str, values: frozenset[str]) -> None:
    value = body.get(key)
    if value is not None and value not in values:
        raise ProxyError(400, "invalid_request_error", f"{key}={value!r} is not allowed")


def _budget(message: str) -> ProxyError:
    return ProxyError(BUDGET_STATUS, "budget_exceeded", message)
