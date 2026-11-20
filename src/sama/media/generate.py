"""``image_generate`` and ``text_to_speech`` (docs/architecture.md, Section 11.8; roadmap
M4.9).

Both tools call the OpenAI API with the key of the org. The key stays in the control plane: the
sandbox never gets it, and no log or error has it. The file goes to ``generated_media``. The
gateway of the run sends it to the chat when the run ends: a photo or a voice note in Telegram,
a file in Slack. The API gives it at ``GET /v1/sessions/{id}/media/{media_id}``.

Prices: ``image_generate`` records the token usage that the API returns. ``text_to_speech``
records one input token for each character, because the API charges by character.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Final, Literal, get_args

import httpx
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import MAX_GENERATED_MEDIA_BYTES, GeneratedMedia
from sama.media.meter import MediaError, MediaMeter
from sama.modelproxy.prices import Provider, TokenCounts
from sama.modelproxy.usage import Endpoint
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.crypto import VaultError
from sama.vault.redact import Redactor, default_redactor
from sama.vault.store import OrgKeys, SecretName

log = logging.getLogger("sama.media")

OPENAI_URL: Final = "https://api.openai.com"
DEFAULT_IMAGE_MODEL: Final = "gpt-image-2.6-flare"
DEFAULT_SPEECH_MODEL: Final = "tts-1"
ImageSize = Literal["1024x1024", "1536x1024", "1024x1536"]
IMAGE_SIZES: Final = get_args(ImageSize)
DEFAULT_IMAGE_SIZE: Final = "1024x1024"
IMAGE_QUALITY: Final = "medium"
Voice = Literal["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
VOICES: Final = get_args(Voice)
DEFAULT_VOICE: Final = "alloy"
MAX_PROMPT_CHARS: Final = 4_000
MAX_SPEECH_CHARS: Final = 4_096
"""The input limit of the speech API."""
MAX_CAPTION_CHARS: Final = 200
MAX_SENT_PER_RUN: Final = 10
"""A gateway sends at most this many files of one run. The API still gives the others."""
IMAGE_TIMEOUT_S: Final = 120.0
SPEECH_TIMEOUT_S: Final = 60.0
IMAGE_OUTPUT_ESTIMATE: Final = 6_240
"""The output tokens of the largest image at high quality: above every real call."""

NO_KEY: Final = "{} is not available: no OpenAI key is configured for the org."
IMAGE: Final = "Image generation"
SPEECH: Final = "Text to speech"

PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"
OGG_MAGIC: Final = b"OggS"

_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
"""Errors before the request reached the provider: no charge, so no record."""


class MediaStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    """No key: the tool tells the model that the feature is off. This is not an error."""


@dataclass(frozen=True, slots=True)
class MediaResult:
    status: MediaStatus
    text: str
    media_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """A file without its bytes."""

    id: uuid.UUID
    run_id: uuid.UUID
    kind: str
    mime_type: str
    size_bytes: int
    caption: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredMedia:
    id: uuid.UUID
    kind: str
    mime_type: str
    caption: str
    data: bytes = field(repr=False)


_STORED_COLUMNS: Final = (
    GeneratedMedia.id,
    GeneratedMedia.kind,
    GeneratedMedia.mime_type,
    GeneratedMedia.caption,
    GeneratedMedia.data,
)


def _stored(row: Any) -> StoredMedia:
    return StoredMedia(row.id, row.kind, row.mime_type, row.caption, bytes(row.data))


class GeneratedMediaStore:
    """Reads and writes ``generated_media``. Each read is scoped by org: a file of another org
    is the same as a file that does not exist."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def save(
        self,
        claims: RunTokenClaims,
        *,
        kind: str,
        mime_type: str,
        data: bytes,
        caption: str,
    ) -> uuid.UUID:
        media_id = uuid.uuid4()
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(GeneratedMedia).values(
                    id=media_id,
                    org_id=uuid.UUID(claims.org_id),
                    session_id=uuid.UUID(claims.session_id),
                    run_id=uuid.UUID(claims.run_id),
                    kind=kind,
                    mime_type=mime_type,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    caption=caption[:MAX_CAPTION_CHARS],
                    data=data,
                )
            )
        return media_id

    async def pending(
        self, org_id: uuid.UUID, run_id: uuid.UUID, limit: int = MAX_SENT_PER_RUN
    ) -> Sequence[StoredMedia]:
        """The files of a run that no gateway sent yet, oldest first."""
        query = (
            select(*_STORED_COLUMNS)
            .where(
                GeneratedMedia.org_id == org_id,
                GeneratedMedia.run_id == run_id,
                GeneratedMedia.delivered_at.is_(None),
            )
            .order_by(GeneratedMedia.created_at, GeneratedMedia.id)
            .limit(limit)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).all()
        return [_stored(row) for row in rows]

    async def list(
        self, org_id: uuid.UUID, session_id: uuid.UUID, limit: int
    ) -> Sequence[MediaInfo]:
        """The files of a session without their bytes, newest first."""
        query = (
            select(
                GeneratedMedia.id,
                GeneratedMedia.run_id,
                GeneratedMedia.kind,
                GeneratedMedia.mime_type,
                GeneratedMedia.size_bytes,
                GeneratedMedia.caption,
                GeneratedMedia.created_at,
            )
            .where(GeneratedMedia.org_id == org_id, GeneratedMedia.session_id == session_id)
            .order_by(GeneratedMedia.created_at.desc(), GeneratedMedia.id)
            .limit(limit)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).all()
        return [MediaInfo(*row) for row in rows]

    async def load(
        self, org_id: uuid.UUID, session_id: uuid.UUID, media_id: uuid.UUID
    ) -> StoredMedia | None:
        query = select(*_STORED_COLUMNS).where(
            GeneratedMedia.org_id == org_id,
            GeneratedMedia.session_id == session_id,
            GeneratedMedia.id == media_id,
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(query)).first()
        if row is None:
            return None
        return _stored(row)

    async def delivered(self, org_id: uuid.UUID, media_id: uuid.UUID) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                update(GeneratedMedia)
                .where(GeneratedMedia.org_id == org_id, GeneratedMedia.id == media_id)
                .values(delivered_at=datetime.now(UTC))
            )


class _OpenAIMedia:
    """The shared part of the two services: the key, the budget, the call, and the record."""

    label: str
    endpoint: Endpoint
    default_model: ClassVar[str]
    default_timeout_s: ClassVar[float]

    def __init__(
        self,
        *,
        store: GeneratedMediaStore,
        keys: OrgKeys,
        meter: MediaMeter,
        model: str | None = None,
        base_url: str = OPENAI_URL,
        http: httpx.AsyncClient | None = None,
        timeout_s: float | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        model = model or self.default_model
        timeout_s = timeout_s if timeout_s is not None else self.default_timeout_s
        if not meter.has_price(Provider.OPENAI, model):
            raise ValueError(f"the media model {model!r} has no price")
        self._store = store
        self._keys = keys
        self._meter = meter
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout_s, trust_env=False)
        self._redactor = redactor or default_redactor()

    async def aclose(self) -> None:
        await self._meter.aclose(self._http.aclose)

    async def _key(self, claims: RunTokenClaims) -> str | None:
        try:
            key = await self._keys.get(claims.org_id, SecretName.OPENAI_API_KEY)
        except VaultError:
            log.warning("the OpenAI key of org=%s does not decrypt", claims.org_id)
            return None
        return key.get_secret_value() if key is not None else None

    async def _post(
        self,
        claims: RunTokenClaims,
        key: str,
        path: str,
        body: dict[str, Any],
        estimate: TokenCounts,
    ) -> httpx.Response:
        """Sends the request and settles the record of the call. A call that may have reached
        the provider always gets a record: the real usage, or ``estimate``."""
        call_id = uuid.uuid4()
        started = time.monotonic()

        async def settle(counts: TokenCounts, *, estimated: bool, status: int) -> None:
            await self._meter.settle(
                claims,
                call_id=call_id,
                started=started,
                provider=Provider.OPENAI,
                model=self._model,
                endpoint=self.endpoint,
                counts=counts,
                estimated=estimated,
                status=status,
            )

        try:
            response = await self._http.post(
                f"{self._base_url}{path}",
                headers={"authorization": f"Bearer {key}"},
                json=body,
            )
        except asyncio.CancelledError:
            await settle(estimate, estimated=True, status=0)
            raise
        except httpx.HTTPError as error:
            if not isinstance(error, _NOT_SENT):
                await settle(estimate, estimated=True, status=0)
            if isinstance(error, httpx.TimeoutException):
                raise MediaError(f"{self.label} did not finish in time. Try again.") from None
            log.warning(
                "the media call failed run=%s error=%s", claims.run_id, type(error).__name__
            )
            raise MediaError(f"{self.label} cannot be reached now. Try again later.") from None
        if response.status_code == 200:
            counts = self._counts(response, body)
            await settle(counts or estimate, estimated=counts is None, status=200)
            return response
        # A refused call costs nothing, but it gets its row like in the model proxy.
        await settle(TokenCounts(), estimated=False, status=response.status_code)
        log.warning(
            "the media model refused the call run=%s status=%d",
            claims.run_id,
            response.status_code,
        )
        raise MediaError(_failure(self.label, response.status_code))

    def _counts(self, response: httpx.Response, body: dict[str, Any]) -> TokenCounts | None:
        raise NotImplementedError


class ImageService(_OpenAIMedia):
    label = IMAGE
    endpoint = Endpoint.IMAGES
    default_model = DEFAULT_IMAGE_MODEL
    default_timeout_s = IMAGE_TIMEOUT_S

    async def generate(
        self, claims: RunTokenClaims, prompt: str, size: str = DEFAULT_IMAGE_SIZE
    ) -> MediaResult:
        prompt = prompt.strip()
        if not prompt:
            raise MediaError("The prompt is empty.")
        if size not in IMAGE_SIZES:
            raise MediaError(f"The size must be one of {', '.join(IMAGE_SIZES)}.")
        key = await self._key(claims)
        if key is None:
            return MediaResult(MediaStatus.UNAVAILABLE, NO_KEY.format(IMAGE))
        await self._meter.check(claims, IMAGE)
        # The one place where text leaves for the provider: redact it first.
        safe = self._redactor.text(prompt)[:MAX_PROMPT_CHARS]
        body = {
            "model": self._model,
            "prompt": safe,
            "size": size,
            "quality": IMAGE_QUALITY,
            "output_format": "png",
        }
        estimate = TokenCounts(input=len(safe.encode()) // 2 + 100, output=IMAGE_OUTPUT_ESTIMATE)
        response = await self._meter.shielded(
            self._post(claims, key, "/v1/images/generations", body, estimate)
        )
        data = _image_bytes(response)
        media_id = await self._store.save(
            claims, kind="image", mime_type="image/png", data=data, caption=safe
        )
        return MediaResult(
            MediaStatus.OK,
            f"The image is ready (id {media_id}, {len(data):,} bytes). sama sends it to the "
            "user with your reply. Do not paste it or describe its bytes.",
            media_id,
        )

    def _counts(self, response: httpx.Response, body: dict[str, Any]) -> TokenCounts | None:
        usage = _json(response).get("usage")
        if not isinstance(usage, dict):
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            return None
        return TokenCounts(input=max(0, input_tokens), output=max(0, output_tokens))


class SpeechService(_OpenAIMedia):
    label = SPEECH
    endpoint = Endpoint.SPEECH
    default_model = DEFAULT_SPEECH_MODEL
    default_timeout_s = SPEECH_TIMEOUT_S

    async def speak(
        self, claims: RunTokenClaims, text: str, voice: str = DEFAULT_VOICE
    ) -> MediaResult:
        text = text.strip()
        if not text:
            raise MediaError("The text is empty.")
        if len(text) > MAX_SPEECH_CHARS:
            raise MediaError(f"The text has more than {MAX_SPEECH_CHARS:,} characters.")
        if voice not in VOICES:
            raise MediaError(f"The voice must be one of {', '.join(VOICES)}.")
        key = await self._key(claims)
        if key is None:
            return MediaResult(MediaStatus.UNAVAILABLE, NO_KEY.format(SPEECH))
        await self._meter.check(claims, SPEECH)
        safe = self._redactor.text(text)
        # Ogg Opus is the format of a Telegram voice note.
        body = {"model": self._model, "input": safe, "voice": voice, "response_format": "opus"}
        estimate = TokenCounts(input=len(safe))
        response = await self._meter.shielded(
            self._post(claims, key, "/v1/audio/speech", body, estimate)
        )
        data = response.content
        if not data.startswith(OGG_MAGIC):
            raise MediaError("Text to speech gave no audio.")
        if len(data) > MAX_GENERATED_MEDIA_BYTES:
            raise MediaError("The voice note is too large. Use a shorter text.")
        media_id = await self._store.save(
            claims, kind="audio", mime_type="audio/ogg", data=data, caption=safe
        )
        return MediaResult(
            MediaStatus.OK,
            f"The voice note is ready (id {media_id}, {len(data):,} bytes). sama sends it to "
            "the user with your reply.",
            media_id,
        )

    def _counts(self, response: httpx.Response, body: dict[str, Any]) -> TokenCounts | None:
        # The API charges for each character of the input.
        return TokenCounts(input=len(str(body["input"])))


def _image_bytes(response: httpx.Response) -> bytes:
    items = _json(response).get("data")
    first = items[0] if isinstance(items, list) and items else None
    encoded = first.get("b64_json") if isinstance(first, dict) else None
    if not isinstance(encoded, str):
        raise MediaError("Image generation gave no image.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise MediaError("Image generation gave an image that sama cannot read.") from None
    if not data.startswith(PNG_MAGIC):
        raise MediaError("Image generation gave an image that sama cannot read.")
    if len(data) > MAX_GENERATED_MEDIA_BYTES:
        raise MediaError("The image is too large. Use a smaller size.")
    return data


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _failure(label: str, status: int) -> str:
    if status in (401, 403):
        return f"{label} refused the key of the org. Ask an admin to check it."
    if status == 429:
        return f"{label} is busy now. Try again later."
    if status == 400:
        return f"{label} refused the request. Change the input and try again."
    return f"{label} is not available now. Try again later."
