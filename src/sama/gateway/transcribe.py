"""Speech to text for voice notes (docs/architecture.md, Section 11.2)."""

from __future__ import annotations

from typing import Final, Protocol

import httpx
from pydantic import SecretStr

OPENAI_URL: Final = "https://api.openai.com/v1"


class TranscribeError(Exception):
    pass


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, *, filename: str, mime_type: str) -> str:
        """Returns the text of the audio. Raises ``TranscribeError``."""
        ...


class OpenAITranscriber:
    """The OpenAI transcription endpoint. The key stays in the control plane."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        model: str = "whisper-1",
        base_url: str = OPENAI_URL,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def transcribe(self, audio: bytes, *, filename: str, mime_type: str) -> str:
        try:
            response = await self._http.post(
                f"{self._base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                data={"model": self._model, "response_format": "json"},
                files={"file": (filename, audio, mime_type)},
            )
        except httpx.HTTPError as error:
            raise TranscribeError(f"transcription failed: {type(error).__name__}") from None
        if response.status_code != 200:
            raise TranscribeError(f"transcription failed: HTTP {response.status_code}")
        try:
            text = response.json()["text"]
        except (ValueError, KeyError, TypeError):
            raise TranscribeError("transcription failed: a bad response") from None
        return str(text).strip()
