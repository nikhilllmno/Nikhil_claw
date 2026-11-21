"""The OpenAI-compatible chat endpoint of sama (docs/architecture.md, Section 10.7, M7.6).

``POST /v1/chat/completions`` sends the conversation to a sama agent profile and answers in the
form of the OpenAI Chat Completions API, also as a stream. ``GET /v1/models`` lists the agent
profiles of the key. An org API key with the scope ``chat`` is the bearer.

The answer comes from a normal sama run: the agent uses its own tools, the gate decides each
tool call, and the run counts against the budget and the quotas of the org. The caller cannot
give tools; sama ignores ``tools`` and the sampling settings.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any, Final, Literal, Self

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sama.apikeys.store import ApiKeyStore, InvalidApiKeyError, KeyContext
from sama.gateway.sessions import final_text, read_run
from sama.server.core import (
    DEFAULT_MODEL,
    MAX_TEXT_CHARS,
    TITLE_CHARS,
    Profile,
    RunResult,
    ServerCore,
    ServerError,
)
from sama.tenancy.regions import OrgInOtherRegion

SESSION_HEADER: Final = "X-Sama-Session-Id"
RUN_HEADER: Final = "X-Sama-Run-Id"
MAX_MESSAGES: Final = 200
MAX_BODY_BYTES: Final = 512 * 1024
CHANNEL: Final = "api"


class _Part(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    text: str | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "developer", "user", "assistant", "tool", "function"]
    content: str | list[_Part] | None = None


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="allow")

    include_usage: bool = False


class ChatRequest(BaseModel):
    """The fields of the OpenAI request that sama reads. Other fields are allowed, and sama
    does not use them."""

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    stream: bool = False
    stream_options: StreamOptions | None = None
    session_id: uuid.UUID | None = None
    """A sama extension (``extra_body``). The header ``X-Sama-Session-Id`` does the same."""


def error_body(
    message: str, *, code: str, kind: str = "invalid_request_error", param: str | None = None
) -> dict[str, Any]:
    return {"error": {"message": message, "type": kind, "param": param, "code": code}}


_KINDS: Final = {
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    409: "invalid_request_error",
    429: "rate_limit_error",
}


def error_response(error: ServerError) -> JSONResponse:
    headers = {}
    if error.retry_after_s is not None:
        headers["Retry-After"] = str(error.retry_after_s)
    kind = "insufficient_quota" if error.code == "insufficient_quota" else None
    return JSONResponse(
        status_code=error.status,
        content=error_body(
            str(error),
            code=error.code,
            kind=kind or _KINDS.get(error.status, "invalid_request_error"),
            param=error.param,
        ),
        headers=headers,
    )


def _text(message: ChatMessage) -> str:
    if message.content is None:
        return ""
    if isinstance(message.content, str):
        return message.content
    texts = []
    for part in message.content:
        if part.type != "text" or part.text is None:
            raise ServerError(
                400,
                "unsupported_content",
                "sama takes only text content; send images in a chat channel",
                param="messages",
            )
        texts.append(part.text)
    return "\n".join(texts)


def compose(messages: list[ChatMessage], *, new_session: bool) -> tuple[str, str]:
    """The text of the user message for the run, and the text for the session title.

    The last message must be a user message. In a session that exists, sama has the earlier
    messages already, so only the last message goes. A new session gets the instructions and
    the earlier messages of the request in the same user message, so the caller's system text
    has user trust, not the trust of the sama system prompt."""
    last = messages[-1]
    if last.role != "user":
        raise ServerError(
            400, "invalid_request", "the last message must have the role user", param="messages"
        )
    question = _text(last)
    if not question.strip():
        raise ServerError(400, "invalid_request", "the last message is empty", param="messages")
    title = question
    if not new_session:
        return question, title
    instructions = [
        _text(message) for message in messages[:-1] if message.role in ("system", "developer")
    ]
    earlier = [
        (message.role, _text(message))
        for message in messages[:-1]
        if message.role in ("user", "assistant")
    ]
    if not instructions and not earlier:
        return question, title
    parts = ["The caller of the sama API sent this conversation. Answer the last message."]
    if instructions:
        parts.append(
            "Instructions from the caller:\n"
            + "\n\n".join(text for text in instructions if text.strip())
        )
    if earlier:
        parts.append(
            "Earlier messages:\n"
            + "\n\n".join(f"[{role}]\n{text}" for role, text in earlier if text.strip())
        )
    parts.append(f"The last message:\n{question}")
    return "\n\n".join(parts), title


def _completion_id(run_id: uuid.UUID) -> str:
    return f"chatcmpl-{run_id.hex}"


def _usage(tokens: tuple[int, int]) -> dict[str, int]:
    prompt, completion = tokens
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _failure(result: RunResult | None, *, timed_out: bool = False) -> ServerError:
    if timed_out or result is None:
        return ServerError(
            504,
            "run_timeout",
            "the run did not end in time; it goes on in the sama session",
        )
    if result.status == "cancelled":
        return ServerError(409, "run_cancelled", "the run was stopped")
    detail = result.failure_message or "unknown"
    return ServerError(502, result.failure_code or "run_failed", f"the run failed: {detail}")


class OpenAICompat:
    """The ``/v1`` routes."""

    def __init__(self, core: ServerCore, keys: ApiKeyStore) -> None:
        self._core = core
        self._keys = keys

    async def key(self, request: Request) -> KeyContext:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise _Refused(
                ServerError(401, "invalid_api_key", "give an org API key as the bearer token")
            )
        try:
            return await self._keys.verify(token.strip(), "chat")
        except InvalidApiKeyError as error:
            raise _Refused(ServerError(401, "invalid_api_key", str(error))) from None
        except OrgInOtherRegion as error:
            raise _Refused(ServerError(421, "wrong_region", str(error))) from None

    def install(self, app: FastAPI) -> None:
        """Adds the ``/v1`` routes and the error answer of a refused key to ``app``."""
        app.include_router(self.router())
        app.add_exception_handler(_Refused, refused_handler)

    def router(self) -> APIRouter:
        routes = APIRouter()
        # This module has no ``from __future__ import annotations``: FastAPI reads the local
        # alias ``key`` when it builds the routes.
        key = Annotated[KeyContext, Depends(self.key)]

        @routes.get("/v1/models")
        async def models(me: key) -> JSONResponse:
            profiles = await self._core.profiles(me)
            return JSONResponse(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": profile.model_id,
                            "object": "model",
                            "created": int(profile.created_at),
                            "owned_by": "sama",
                            "sama_profile_id": str(profile.id),
                            "sama_profile_name": profile.name,
                        }
                        for profile in profiles
                    ],
                }
            )

        @routes.get("/v1/models/{model_id}")
        async def model(me: key, model_id: str) -> Response:
            try:
                profile = await self._core.resolve(me, model_id)
            except ServerError as error:
                return error_response(error)
            return JSONResponse(
                {
                    "id": profile.model_id,
                    "object": "model",
                    "created": int(profile.created_at),
                    "owned_by": "sama",
                }
            )

        @routes.post("/v1/chat/completions")
        async def chat(me: key, request: Request) -> Response:
            try:
                return await self._chat(me, request)
            except ServerError as error:
                return error_response(error)

        return routes

    async def _chat(self, key: KeyContext, request: Request) -> Response:
        declared = request.headers.get("content-length", "")
        if declared.isdecimal() and int(declared) > MAX_BODY_BYTES:
            raise ServerError(413, "request_too_large", "the request is too large")
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_BODY_BYTES:
                raise ServerError(413, "request_too_large", "the request is too large")
        try:
            body = ChatRequest.model_validate_json(bytes(raw))
        except ValidationError as error:
            errors = error.errors(include_input=False, include_url=False)
            where = ".".join(str(part) for part in errors[0]["loc"]) if errors else ""
            message = errors[0]["msg"] if errors else "error"
            raise ServerError(
                400, "invalid_request", f"the request is not valid: {where or 'body'}: {message}"
            ) from None
        session_id = body.session_id
        header = request.headers.get(SESSION_HEADER.lower())
        if header:
            try:
                session_id = uuid.UUID(header.strip())
            except ValueError:
                raise ServerError(
                    400, "invalid_request", f"{SESSION_HEADER} is not a session id"
                ) from None
        if session_id is not None:
            row = await self._core.session(key, session_id)
            profiles = {item.id: item for item in await self._core.profiles(key)}
            found = profiles.get(row.agent_profile_id)
            if found is None:
                raise ServerError(404, "session_not_found", "the session does not exist")
            if (body.model or "").strip() not in ("", DEFAULT_MODEL):
                asked = await self._core.resolve(key, body.model)
                if asked.id != found.id:
                    raise ServerError(
                        400,
                        "model_mismatch",
                        "the session uses another agent profile; leave out model or use "
                        f"{found.model_id}",
                        param="model",
                    )
            profile = found
            text, title = compose(body.messages, new_session=False)
        else:
            profile = await self._core.resolve(key, body.model)
            text, title = compose(body.messages, new_session=True)
            if len(text) > MAX_TEXT_CHARS:
                # The same check as ``send``, before a session exists.
                raise ServerError(
                    400,
                    "context_length_exceeded",
                    f"the conversation has more than {MAX_TEXT_CHARS} characters; send fewer "
                    f"messages or continue a sama session with {SESSION_HEADER}",
                )
        release = self._core.hold_waiter(key)
        try:
            if session_id is None:
                session_id = await self._core.open_session(
                    key, profile, title=title[:TITLE_CHARS], channel=CHANNEL
                )
            run_id = await self._core.send(key, session_id, text)
        except BaseException:
            release()
            raise
        headers = {SESSION_HEADER: str(session_id), RUN_HEADER: str(run_id)}
        include_usage = bool(body.stream_options and body.stream_options.include_usage)
        if body.stream:
            return StreamingResponse(
                _Released(self._stream(key, run_id, profile, include_usage), release),
                media_type="text/event-stream",
                headers={**headers, "Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        try:
            result = await self._core.wait(key, run_id, self._core.settings.first_wait_s)
        except BaseException:
            release()
            raise
        if result.ended:
            try:
                return await self._answer(key, result, profile, headers)
            finally:
                release()
        return StreamingResponse(
            _Released(self._late_answer(key, run_id, profile), release),
            media_type="application/json",
            headers={**headers, "Cache-Control": "no-store"},
        )

    async def _completion(
        self, key: KeyContext, result: RunResult, profile: Profile
    ) -> dict[str, Any]:
        return {
            "id": _completion_id(result.run_id),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": profile.model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result.text, "refusal": None},
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ],
            "usage": _usage(await self._core.usage(key.org_id, result.run_id)),
            "sama": {"session_id": str(result.session_id), "run_id": str(result.run_id)},
        }

    async def _answer(
        self, key: KeyContext, result: RunResult, profile: Profile, headers: dict[str, str]
    ) -> Response:
        if result.status != "completed":
            response = error_response(_failure(result))
            response.headers.update(headers)
            return response
        return JSONResponse(await self._completion(key, result, profile), headers=headers)

    async def _late_answer(
        self, key: KeyContext, run_id: uuid.UUID, profile: Profile
    ) -> AsyncIterator[bytes]:
        """The answer of a run that took longer than ``first_wait_s``. The status is already
        ``200``: spaces keep the connection open (JSON allows them before the value), then the
        body comes. A failure is an OpenAI error body."""
        settings = self._core.settings
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.max_wait_s
        result = await self._core.result(key, run_id)
        while not result.ended and loop.time() < deadline:
            yield b" "
            result = await self._core.wait(
                key, run_id, min(settings.keepalive_s, max(0.0, deadline - loop.time()))
            )
        if result.status == "completed":
            body = await self._completion(key, result, profile)
        else:
            error = _failure(result, timed_out=not result.ended)
            body = error_body(str(error), code=error.code, kind="server_error")
        yield json.dumps(body).encode()

    async def _stream(
        self, key: KeyContext, run_id: uuid.UUID, profile: Profile, include_usage: bool
    ) -> AsyncIterator[bytes]:
        """Server-sent events in the form of the OpenAI stream: one chunk for each text delta
        of the run, then the end chunk and ``[DONE]``. A failure is an ``error`` event."""
        settings = self._core.settings
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.max_wait_s
        created = int(time.time())
        completion = _completion_id(run_id)

        def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
            body = {
                "id": completion,
                "object": "chat.completion.chunk",
                "created": created,
                "model": profile.model_id,
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}
                ],
            }
            return f"data: {json.dumps(body)}\n\n".encode()

        yield chunk({"role": "assistant", "content": ""})
        seq = -1
        sent_any = False
        status = "queued"
        last_byte = loop.time()
        ended = False
        while loop.time() < deadline:
            read = await read_run(self._core.engine, key.org_id, run_id, seq)
            status = read.status
            for event in read.events:
                seq = event.seq
                if event.type == "text_delta":
                    text = str(event.payload.get("text", ""))
                    if text:
                        sent_any = True
                        last_byte = loop.time()
                        yield chunk({"content": text})
            if read.ended:
                ended = True
                break
            if loop.time() - last_byte >= settings.keepalive_s:
                last_byte = loop.time()
                yield b": ping\n\n"
            await asyncio.sleep(settings.poll_s)
        if ended and status == "completed":
            if not sent_any:
                text = await final_text(self._core.engine, key.org_id, run_id)
                if text:
                    yield chunk({"content": text})
            yield chunk({}, "stop")
            if include_usage:
                body = {
                    "id": completion,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": profile.model_id,
                    "choices": [],
                    "usage": _usage(await self._core.usage(key.org_id, run_id)),
                }
                yield f"data: {json.dumps(body)}\n\n".encode()
        else:
            result = await self._core.result(key, run_id) if ended else None
            error = _failure(result, timed_out=not ended)
            body = error_body(str(error), code=error.code, kind="server_error")
            yield f"data: {json.dumps(body)}\n\n".encode()
        yield b"data: [DONE]\n\n"


class _Refused(Exception):
    """A request without a valid key. The app answers it with ``refused_handler``."""

    def __init__(self, error: ServerError) -> None:
        super().__init__(str(error))
        self.error = error


async def refused_handler(_request: Request, exc: Exception) -> Response:
    assert isinstance(exc, _Refused)  # noqa: S101
    response = error_response(exc.error)
    if exc.error.status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


class _Released:
    """An async iterator that gives back the waiter place when it ends, also when the client
    goes away."""

    def __init__(self, inner: AsyncIterator[bytes], release: Callable[[], None]) -> None:
        self._inner = inner
        self._release = release

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._inner.__anext__()
        except BaseException:
            self._release()
            raise

    async def aclose(self) -> None:
        self._release()
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()
