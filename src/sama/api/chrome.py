"""The WebSocket of the Chrome extension channel: ``/channels/chrome/ws`` (D11, Section 14.2,
M5.4).

The background service worker of the extension (``extension/``) opens one WebSocket for each
signed-in browser. The protocol is JSON text frames with a ``type`` field.

The client sends:

- ``auth {token}``: the first frame, within ``AUTH_TIMEOUT_S``. The token is a Supabase Auth
  access token. It is not in the URL, so it does not go into the access logs. A later ``auth``
  frame with a fresh token of the same user moves the end of the connection.
- ``subscribe {session_id, after?}`` and ``unsubscribe {session_id}``: follow the events of a
  session. Each followed session takes one event stream slot of the user (``open_stream``).
- ``send {id, session_id, text, page?}``: queue a user message. ``page`` is "Ask about this page":
  the URL, title, text, and an optional screenshot of the tab (``sama.gateway.chrome``).
- ``ping``.

The server sends ``ready``, ``auth_ok``, ``subscribed``, ``unsubscribed``, the events of
``SamaApi.session_events`` (``runs``, ``run_event``, ``run``, ``end``, each with
``session_id`` and ``data``), ``sent {id, run_id, queued}``, ``error {id?, code, message}``,
and ``pong``.

Since M5.5 the server also sends ``tool_call {call_id, op, args, session_id, run_id, approved,
timeout_ms}`` for the ``my_chrome`` browser target (``sama.browser.chrome``), and the client
answers with ``tool_result {call_id, ok, result | error}``. An answer that comes after the
deadline, or for a call that is not open, is dropped.

The connection closes with code ``4401`` when the token expires or is not valid, ``4403`` for
an origin that is not an allowed extension or a token of another user, ``4408`` when no
``auth`` frame comes in time, and ``4429`` when the user has too many connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from fastapi import HTTPException, WebSocket
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from sama.api.auth import AuthError, Principal
from sama.api.events import MAX_MESSAGE_CHARS, SendError
from sama.gateway.chrome import ChromeOfflineError, PageContext, TooManyConnectionsError

if TYPE_CHECKING:
    from sama.api.app import SamaApi

log = logging.getLogger("sama.api.chrome")

PROTOCOL: Final = 1
AUTH_TIMEOUT_S: Final = 10.0
MAX_FRAME_CHARS: Final = 6_000_000
"""The largest text frame. A frame with a page screenshot is the largest frame."""
FRAME_WINDOW_S: Final = 10.0
MAX_FRAMES_PER_WINDOW: Final = 40
MAX_SUBSCRIPTIONS: Final = 8
"""The sessions that one connection follows at a time."""
MAX_OPEN_CALLS: Final = 16
"""The ``tool_call`` frames that wait for an answer on one connection."""
SEND_TIMEOUT_S: Final = 15.0
"""A frame that the client does not take in this time closes the connection."""
MAX_TOKEN_CHARS: Final = 8_192
EXTENSION_SCHEME: Final = "chrome-extension://"

CLOSE_INVALID: Final = 4400
CLOSE_UNAUTHORIZED: Final = 4401
CLOSE_FORBIDDEN: Final = 4403
CLOSE_AUTH_TIMEOUT: Final = 4408
CLOSE_TOO_MANY: Final = 4429

_ERROR_CODES: Final = {
    402: "quota_exceeded",
    403: "forbidden",
    404: "not_found",
    409: "busy",
    422: "invalid",
    429: "rate_limited",
}


class _Frame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthFrame(_Frame):
    type: Literal["auth"]
    token: str = Field(min_length=1, max_length=MAX_TOKEN_CHARS)


class SubscribeFrame(_Frame):
    type: Literal["subscribe"]
    session_id: uuid.UUID
    after: int | None = Field(default=None, ge=0)


class UnsubscribeFrame(_Frame):
    type: Literal["unsubscribe"]
    session_id: uuid.UUID


class SendFrame(_Frame):
    type: Literal["send"]
    id: str = Field(min_length=1, max_length=64)
    session_id: uuid.UUID
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    page: PageContext | None = None


class PingFrame(_Frame):
    type: Literal["ping"]


class ToolErrorBody(_Frame):
    code: str = Field(min_length=1, max_length=40)
    message: str = Field(default="", max_length=2_000)


class ToolResultFrame(_Frame):
    type: Literal["tool_result"]
    call_id: str = Field(min_length=1, max_length=64)
    ok: bool
    result: dict[str, Any] | None = None
    error: ToolErrorBody | None = None


ClientFrame = Annotated[
    AuthFrame | SubscribeFrame | UnsubscribeFrame | SendFrame | PingFrame | ToolResultFrame,
    Field(discriminator="type"),
]
_FRAMES: Final[TypeAdapter[ClientFrame]] = TypeAdapter(ClientFrame)


class _Closed(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _problems(error: ValidationError) -> str:
    """Where and why a frame is not valid, without the input (it can hold a token)."""
    parts = [
        f"{'.'.join(str(part) for part in item['loc']) or 'frame'}: {item['msg']}"
        for item in error.errors()[:3]
    ]
    return "; ".join(parts)


def _parse(text: str) -> tuple[ClientFrame | None, str | None, str | None]:
    """The frame, or ``None`` and the problem. The second item is the ``id`` of the frame."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None, None, "the frame is not JSON"
    frame_id = raw.get("id") if isinstance(raw, dict) else None
    frame_id = frame_id if isinstance(frame_id, str) and len(frame_id) <= 64 else None
    try:
        return _FRAMES.validate_python(raw), frame_id, None
    except ValidationError as error:
        return None, frame_id, _problems(error)


class ChromeChannel:
    """The WebSocket route of the extension channel. It uses the session access, the event
    stream, and the message queue of the ``SamaApi``."""

    def __init__(self, api: SamaApi) -> None:
        self.api = api

    def origin_allowed(self, origin: str | None) -> bool:
        """A browser sends the origin of the page or extension that opens the socket. Only an
        extension origin may open it, so a web page cannot use the token of a user. A client
        that is not a browser sends no origin; it still needs a valid token."""
        if origin is None:
            return True
        if not origin.startswith(EXTENSION_SCHEME):
            return False
        allowed = self.api.settings.extension_ids
        return not allowed or origin.removeprefix(EXTENSION_SCHEME) in allowed

    async def serve(self, websocket: WebSocket) -> None:
        if not self.origin_allowed(websocket.headers.get("origin")):
            await websocket.close(code=CLOSE_FORBIDDEN)  # Before accept: an HTTP 403.
            return
        await websocket.accept()
        try:
            principal = await self._authenticate(websocket)
        except _Closed as closed:
            await _close(websocket, closed.code, closed.reason)
            return
        except _Gone:
            return
        connection = _Connection(self.api, websocket, principal)
        try:
            unregister = self.api.chrome.register(principal.auth_user_id, connection)
        except TooManyConnectionsError:
            await _close(websocket, CLOSE_TOO_MANY, "too many extension connections")
            return
        log.info("an extension connected", extra={"auth_user_id": str(principal.auth_user_id)})
        try:
            await connection.run()
        finally:
            unregister()
            log.info(
                "an extension disconnected", extra={"auth_user_id": str(principal.auth_user_id)}
            )

    async def _authenticate(self, websocket: WebSocket) -> Principal:
        try:
            async with asyncio.timeout(AUTH_TIMEOUT_S):
                message = await websocket.receive()
        except TimeoutError:
            raise _Closed(CLOSE_AUTH_TIMEOUT, "send the auth frame first") from None
        if message["type"] == "websocket.disconnect":
            raise _Gone
        text = message.get("text")
        if not isinstance(text, str) or len(text) > MAX_FRAME_CHARS:
            raise _Closed(CLOSE_INVALID, "send the auth frame first")
        frame, _, _ = _parse(text)
        if not isinstance(frame, AuthFrame):
            raise _Closed(CLOSE_INVALID, "send the auth frame first")
        try:
            return await self.api.verifier.verify(frame.token)
        except AuthError as error:
            raise _Closed(CLOSE_UNAUTHORIZED, str(error)[:120]) from None


class _Gone(Exception):
    """The client closed the socket."""


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await websocket.close(code=code, reason=reason[:120])


class _Connection:
    """One open extension connection (a ``ChromeLink`` of the hub)."""

    def __init__(self, api: SamaApi, websocket: WebSocket, principal: Principal) -> None:
        self._api = api
        self._ws = websocket
        self._loop = asyncio.get_running_loop()
        self.principal = principal
        self._deadline = self._end_of(principal)
        self._lock = asyncio.Lock()
        self._subs: dict[uuid.UUID, tuple[asyncio.Task[None], Callable[[], None]]] = {}
        self._frames: deque[float] = deque()
        self._stop = asyncio.Event()
        self._close: tuple[int, str] | None = None
        self._timeout: asyncio.Timeout | None = None
        self._calls: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._gone = False

    def _end_of(self, principal: Principal) -> float:
        """The event loop time when the token of ``principal`` expires."""
        if principal.expires_at is None:
            return self._loop.time() + self._api.settings.max_stream_s
        return self._loop.time() + (principal.expires_at - time.time())

    async def send(self, frame: dict[str, Any]) -> None:
        async with self._lock:
            async with asyncio.timeout(SEND_TIMEOUT_S):
                await self._ws.send_text(json.dumps(frame, separators=(",", ":")))

    async def call(self, frame: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        """Sends a ``tool_call`` and waits for its ``tool_result`` (``ChromeLink.call``)."""
        if self._gone or self._stop.is_set():
            raise ChromeOfflineError
        if len(self._calls) >= MAX_OPEN_CALLS:
            raise ChromeOfflineError
        call_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        self._calls[call_id] = future
        try:
            try:
                await self.send({**frame, "type": "tool_call", "call_id": call_id})
            except (TimeoutError, OSError, RuntimeError):
                raise ChromeOfflineError from None
            async with asyncio.timeout(timeout_s):
                return await future
        finally:
            self._calls.pop(call_id, None)

    def _answer(self, frame: ToolResultFrame) -> None:
        future = self._calls.get(frame.call_id)
        if future is None or future.done():
            return
        answer: dict[str, Any] = {"ok": frame.ok}
        if frame.ok:
            answer["result"] = frame.result or {}
        else:
            error = frame.error or ToolErrorBody(code="failed")
            answer["error"] = {"code": error.code, "message": error.message}
        future.set_result(answer)

    def _fail_calls(self) -> None:
        self._gone = True
        for future in self._calls.values():
            if not future.done():
                future.set_exception(ChromeOfflineError())

    def stop(self, code: int, reason: str) -> None:
        if self._close is None:
            self._close = (code, reason)
        self._stop.set()

    async def run(self) -> None:
        await self.send(
            {
                "type": "ready",
                "protocol": PROTOCOL,
                "expires_at": self.principal.expires_at,
                "max_frame_chars": MAX_FRAME_CHARS,
            }
        )
        reader = asyncio.create_task(self._read())
        stop = asyncio.create_task(self._stop.wait())
        tasks = [reader, stop]
        try:
            try:
                # A fresh token moves the end (``_reauth``).
                async with asyncio.timeout_at(self._deadline) as self._timeout:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            except TimeoutError:
                self.stop(CLOSE_UNAUTHORIZED, "the access token expired")
            if reader.done() and not reader.cancelled() and reader.exception() is not None:
                error = reader.exception()
                if not isinstance(error, _Gone):
                    log.error("the extension connection failed", exc_info=error)
                    self.stop(1011, "the server failed")
        finally:
            self._fail_calls()
            subs = [task for task, _ in self._subs.values()]
            for task in [*tasks, *subs]:
                task.cancel()
            await asyncio.gather(*tasks, *subs, return_exceptions=True)
            for _, release in list(self._subs.values()):
                release()
            self._subs.clear()
        if self._close is not None:
            await _close(self._ws, *self._close)

    def _allow_frame(self) -> bool:
        now = self._loop.time()
        while self._frames and self._frames[0] <= now - FRAME_WINDOW_S:
            self._frames.popleft()
        if len(self._frames) >= MAX_FRAMES_PER_WINDOW:
            return False
        self._frames.append(now)
        return True

    async def _read(self) -> None:
        while True:
            message = await self._ws.receive()
            if message["type"] == "websocket.disconnect":
                raise _Gone
            text = message.get("text")
            if not isinstance(text, str):
                self.stop(1003, "send JSON text frames")
                return
            if len(text) > MAX_FRAME_CHARS:
                self.stop(1009, "the frame is too large")
                return
            if not self._allow_frame():
                self.stop(CLOSE_TOO_MANY, "too many frames")
                return
            frame, frame_id, problem = _parse(text)
            if frame is None:
                await self._error(frame_id, "bad_frame", problem or "the frame is not valid")
                continue
            await self._handle(frame)

    async def _error(self, frame_id: str | None, code: str, message: str, **extra: Any) -> None:
        frame: dict[str, Any] = {"type": "error", "code": code, "message": message, **extra}
        if frame_id is not None:
            frame["id"] = frame_id
        await self.send(frame)

    async def _handle(self, frame: ClientFrame) -> None:
        match frame:
            case PingFrame():
                await self.send({"type": "pong"})
            case AuthFrame():
                await self._reauth(frame)
            case SubscribeFrame():
                await self._subscribe(frame)
            case UnsubscribeFrame():
                self._unsubscribe(frame.session_id)
                await self.send({"type": "unsubscribed", "session_id": str(frame.session_id)})
            case SendFrame():
                await self._send(frame)
            case ToolResultFrame():
                self._answer(frame)

    async def _reauth(self, frame: AuthFrame) -> None:
        try:
            principal = await self._api.verifier.verify(frame.token)
        except AuthError as error:
            self.stop(CLOSE_UNAUTHORIZED, str(error)[:120])
            return
        if principal.auth_user_id != self.principal.auth_user_id:
            self.stop(CLOSE_FORBIDDEN, "the token is for another user")
            return
        self.principal = principal
        self._deadline = self._end_of(principal)
        if self._timeout is not None:
            self._timeout.reschedule(self._deadline)
        await self.send({"type": "auth_ok", "expires_at": principal.expires_at})

    async def _subscribe(self, frame: SubscribeFrame) -> None:
        session_id = frame.session_id
        self._unsubscribe(session_id)
        if len(self._subs) >= MAX_SUBSCRIPTIONS:
            await self._error(
                None, "too_many_subscriptions", f"follow at most {MAX_SUBSCRIPTIONS} sessions"
            )
            return
        try:
            cursor = await self._api.first_cursor(self.principal, session_id, frame.after)
            release = self._api.open_stream(self.principal.auth_user_id)
        except HTTPException as error:
            code = _ERROR_CODES.get(error.status_code, "error")
            await self._error(None, code, str(error.detail), session_id=str(session_id))
            return
        try:
            await self.send({"type": "subscribed", "session_id": str(session_id), "after": cursor})
        except BaseException:
            release()
            raise
        task = asyncio.create_task(self._follow(session_id, cursor))
        self._subs[session_id] = (task, release)
        task.add_done_callback(lambda done: self._ended(session_id, done))

    def _ended(self, session_id: uuid.UUID, task: asyncio.Task[None]) -> None:
        entry = self._subs.get(session_id)
        if entry is not None and entry[0] is task:
            del self._subs[session_id]
            entry[1]()
        if not task.cancelled() and task.exception() is not None:
            # A send that timed out or failed: the client is gone or too slow.
            self.stop(1011, "the client did not take the events")

    def _unsubscribe(self, session_id: uuid.UUID) -> None:
        entry = self._subs.pop(session_id, None)
        if entry is not None:
            entry[0].cancel()
            entry[1]()

    async def _follow(self, session_id: uuid.UUID, cursor: int) -> None:
        events = self._api.session_events(
            self.principal, session_id, cursor, lambda: self._deadline
        )
        async for item in events:
            if item.event == "ping":
                continue  # The client pings; the socket has its own keepalive.
            frame: dict[str, Any] = {
                "type": item.event,
                "session_id": str(session_id),
                "data": item.data,
            }
            if item.event_id is not None:
                frame["event_id"] = item.event_id
            await self.send(frame)
            if item.event == "end":
                return

    async def _send(self, frame: SendFrame) -> None:
        try:
            # The extension shows only the messages of the session, not the notes that join a
            # run (M7.1), so its messages always start or queue a run.
            sent = await self._api.submit(
                self.principal, frame.session_id, frame.text, page=frame.page, join=False
            )
        except SendError as error:
            extra: dict[str, Any] = {"session_id": str(frame.session_id)}
            if error.retry_after_s is not None:
                extra["retry_after_s"] = error.retry_after_s
            code = _ERROR_CODES.get(error.status, "error")
            await self._error(frame.id, code, error.detail, **extra)
            return
        await self.send(
            {
                "type": "sent",
                "id": frame.id,
                "session_id": str(frame.session_id),
                "run_id": str(sent.run_id),
                "queued": sent.queued,
            }
        )
