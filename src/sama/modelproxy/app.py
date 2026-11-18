"""The model proxy service (docs/architecture.md, Section 16.6, decision D6).

The proxy is the only path from a sandbox to a model provider. It accepts only a run token,
checks the model and the budgets, forwards the call with the provider key, passes the
response through unchanged, and records the usage and the cost of the call.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, SecretStr

from sama.modelproxy.prices import PriceTable, Provider, TokenCounts
from sama.modelproxy.store import CallRecord, UsageStore, day_start, month_start
from sama.modelproxy.usage import Endpoint, UsageReader
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

    async def aclose(self) -> None:
        await self._client.aclose()

    def app(self) -> FastAPI:
        app = FastAPI(title="sama model proxy", docs_url=None, redoc_url=None, openapi_url=None)

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
        body = await self._body(request)
        model = self._check(route, body)
        if route.recorded:
            await self._check_budget(claims)
        stream = body.get("stream") is True
        if route.endpoint is Endpoint.CHAT_COMPLETIONS and stream:
            options = body.get("stream_options")
            body["stream_options"] = {**(options if isinstance(options, dict) else {})}
            body["stream_options"]["include_usage"] = True
        started = time.monotonic()
        upstream = await self._send(route, request, body)
        reader = UsageReader(route.provider, stream=stream)

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    reader.feed(chunk)
                    yield chunk
            finally:
                # The client can stop reading. The record is still written.
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
                    if route.recorded:
                        await self._record(claims, route, model, upstream, reader, started)

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

    async def _body(self, request: Request) -> dict[str, Any]:
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
        return body

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
        self,
        claims: RunTokenClaims,
        route: _Route,
        model: str,
        upstream: httpx.Response,
        reader: UsageReader,
        started: float,
    ) -> None:
        counts = reader.counts() or TokenCounts()
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
            cost_usd=cost,
            duration_ms=int((time.monotonic() - started) * 1000),
            created_at=self._clock(),
        )
        try:
            await self._store.record(call)
        except Exception:
            log.exception("the call record was not written run=%s", claims.run_id)
        log.info(
            "model call run=%s model=%s status=%s tokens=%s cost_usd=%s",
            claims.run_id,
            model,
            upstream.status_code,
            counts.total_tokens,
            cost,
        )


def _allowed(body: Mapping[str, Any], key: str, values: frozenset[str]) -> None:
    value = body.get(key)
    if value is not None and value not in values:
        raise ProxyError(400, "invalid_request_error", f"{key}={value!r} is not allowed")


def _budget(message: str) -> ProxyError:
    return ProxyError(BUDGET_STATUS, "budget_exceeded", message)
