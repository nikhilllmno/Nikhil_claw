"""``image_generate`` and ``text_to_speech`` (docs/architecture.md, Section 11.8; roadmap
M4.9), with a fake OpenAI API and the real database."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.media.generate import (
    DEFAULT_IMAGE_MODEL,
    DEFAULT_SPEECH_MODEL,
    GeneratedMediaStore,
    ImageService,
    MediaStatus,
    SpeechService,
)
from sama.media.meter import MediaError, MediaMeter
from sama.modelproxy.ledger import UsageLedger
from sama.modelproxy.prices import PriceTable, Provider
from sama.modelproxy.store import MemoryUsageStore
from sama.modelproxy.usage import Endpoint
from sama.orchestrator.queue import RunQueue
from sama.tenancy.authority import Authority
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import REDACTED, Redactor
from sama.vault.store import OrgKeys, SecretName
from tests.media.support import PNG
from tests.orchestrator.conftest import Tenant

KEY = "sk-openai-media-key-0001"
OGG = b"OggS" + b"\x00" * 60


@dataclass
class OpenAI:
    """The fake API: records each request and answers for its path."""

    status: int = 200
    image: bytes = PNG
    usage: dict[str, Any] | None = field(
        default_factory=lambda: {"input_tokens": 50, "output_tokens": 1056}
    )
    audio: bytes = OGG
    error: Exception | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": f"bad {KEY}"}})
        if request.url.path == "/v1/audio/speech":
            return httpx.Response(200, content=self.audio)
        body: dict[str, Any] = {"data": [{"b64_json": base64.b64encode(self.image).decode()}]}
        if self.usage is not None:
            body["usage"] = self.usage
        return httpx.Response(200, json=body)

    def sent(self) -> dict[str, Any]:
        body = json.loads(self.requests[-1].content)
        assert isinstance(body, dict)
        return body


@dataclass
class Rig:
    images: ImageService
    speech: SpeechService
    store: GeneratedMediaStore
    api: OpenAI
    usage: MemoryUsageStore
    tenant: Tenant
    run_id: uuid.UUID

    def claims(self, **extra: Any) -> RunTokenClaims:
        ids = self.tenant.ids
        return RunTokenClaims(
            org_id=str(self.tenant.org),
            workspace_id=str(ids["ws"]),
            session_id=str(ids["s3"]),
            run_id=str(self.run_id),
            user_id=str(ids["mia"]),
            token_id="test",
            expires_at=2_000_000_000,
            **extra,
        )


async def build(
    engine: AsyncEngine,
    tenant: Tenant,
    *,
    key: str | None = KEY,
    redactor: Redactor | None = None,
    usage: MemoryUsageStore | None = None,
) -> Rig:
    api = OpenAI()
    usage = usage or MemoryUsageStore()
    keys = OrgKeys(None, {SecretName.OPENAI_API_KEY: None if key is None else SecretStr(key)})
    meter = MediaMeter(usage=usage, prices=PriceTable.load(), ledger=UsageLedger(usage, retry_s=0))
    store = GeneratedMediaStore(engine)
    http = httpx.AsyncClient(transport=httpx.MockTransport(api))
    common: dict[str, Any] = {
        "store": store,
        "keys": keys,
        "meter": meter,
        "base_url": "https://openai.test",
        "http": http,
        "redactor": redactor,
    }
    run_id = await RunQueue(engine).enqueue(
        org_id=tenant.org, session_id=tenant.ids["s3"], text="make a picture"
    )
    return Rig(ImageService(**common), SpeechService(**common), store, api, usage, tenant, run_id)


@pytest.fixture
async def rig(engine: AsyncEngine, tenant: Tenant) -> Rig:
    return await build(engine, tenant)


def test_the_default_models_have_media_prices() -> None:
    table = PriceTable.load()
    assert table.media(Provider.OPENAI, DEFAULT_IMAGE_MODEL) is not None
    assert table.media(Provider.OPENAI, DEFAULT_SPEECH_MODEL) is not None


async def test_a_model_without_a_price_is_refused(engine: AsyncEngine) -> None:
    usage = MemoryUsageStore()
    meter = MediaMeter(usage=usage, prices=PriceTable.load())
    with pytest.raises(ValueError, match="no price"):
        ImageService(
            store=GeneratedMediaStore(engine),
            keys=OrgKeys(None, {}),
            meter=meter,
            model="gpt-6-luna",
        )


async def test_an_image_is_stored_for_the_run_and_recorded(rig: Rig) -> None:
    result = await rig.images.generate(rig.claims(), "a red fox in snow", "1536x1024")
    assert result.status is MediaStatus.OK
    assert result.media_id is not None
    assert str(result.media_id) in result.text
    request = rig.api.requests[-1]
    assert request.url.path == "/v1/images/generations"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert rig.api.sent() == {
        "model": "gpt-image-2.6-flare",
        "prompt": "a red fox in snow",
        "size": "1536x1024",
        "quality": "medium",
        "output_format": "png",
    }
    pending = await rig.store.pending(rig.tenant.org, rig.run_id)
    assert [(m.id, m.kind, m.mime_type, m.data) for m in pending] == [
        (result.media_id, "image", "image/png", PNG)
    ]
    assert pending[0].caption == "a red fox in snow"
    [call] = rig.usage.calls
    assert call.endpoint is Endpoint.IMAGES
    assert call.model == "gpt-image-2.6-flare"
    assert (call.counts.input, call.counts.output) == (50, 1056)
    assert call.usage_estimated is False
    assert call.cost_usd > 0
    assert call.run_id == str(rig.run_id)


async def test_an_image_without_usage_records_an_estimate(rig: Rig) -> None:
    rig.api.usage = None
    await rig.images.generate(rig.claims(), "a fox")
    [call] = rig.usage.calls
    assert call.usage_estimated is True
    assert call.counts.output > 1056


async def test_a_voice_note_is_ogg_and_costs_one_token_for_each_character(rig: Rig) -> None:
    result = await rig.speech.speak(rig.claims(), "  Hello there.  ", "nova")
    assert result.status is MediaStatus.OK
    assert rig.api.requests[-1].url.path == "/v1/audio/speech"
    assert rig.api.sent() == {
        "model": "tts-1",
        "input": "Hello there.",
        "voice": "nova",
        "response_format": "opus",
    }
    [media] = await rig.store.pending(rig.tenant.org, rig.run_id)
    assert (media.kind, media.mime_type, media.data) == ("audio", "audio/ogg", OGG)
    [call] = rig.usage.calls
    assert call.endpoint is Endpoint.SPEECH
    assert call.counts.input == len("Hello there.")
    assert call.usage_estimated is False


async def test_without_a_key_the_tools_say_that_they_are_off(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    rig = await build(engine, tenant, key=None)
    image = await rig.images.generate(rig.claims(), "a fox")
    voice = await rig.speech.speak(rig.claims(), "hello")
    assert image.status is voice.status is MediaStatus.UNAVAILABLE
    assert "no OpenAI key" in image.text
    assert rig.api.requests == []
    assert rig.usage.calls == []


async def test_secrets_in_the_prompt_are_redacted_before_they_leave(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    rig = await build(engine, tenant, redactor=Redactor(["hunter2-secret-value"]))
    await rig.images.generate(rig.claims(), "a sign that says hunter2-secret-value")
    await rig.speech.speak(rig.claims(), "say hunter2-secret-value")
    texts = [
        json.loads(r.content).get("prompt") or json.loads(r.content)["input"]
        for r in rig.api.requests
    ]
    assert all("hunter2" not in text and REDACTED in text for text in texts)
    pending = await rig.store.pending(rig.tenant.org, rig.run_id)
    assert all("hunter2" not in media.caption for media in pending)


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda r, c: r.images.generate(c, "   "), "empty"),
        (lambda r, c: r.images.generate(c, "fox", "512x512"), "size"),
        (lambda r, c: r.speech.speak(c, ""), "empty"),
        (lambda r, c: r.speech.speak(c, "x" * 4097), "4,096"),
        (lambda r, c: r.speech.speak(c, "hi", "robot"), "voice"),
    ],
)
async def test_bad_input_is_refused_before_a_call(rig: Rig, call: Any, match: str) -> None:
    with pytest.raises(MediaError, match=match):
        await call(rig, rig.claims())
    assert rig.api.requests == []


@pytest.mark.parametrize(
    ("status", "match"),
    [(401, "key of the org"), (429, "busy"), (400, "refused the request"), (500, "not available")],
)
async def test_a_refusal_is_recorded_as_a_safe_error(rig: Rig, status: int, match: str) -> None:
    rig.api.status = status
    with pytest.raises(MediaError, match=match) as caught:
        await rig.images.generate(rig.claims(), "a fox")
    assert KEY not in str(caught.value)
    assert "openai.test" not in str(caught.value)
    # The refused call has its row, at no cost, like a refused call of the model proxy.
    [call] = rig.usage.calls
    assert (call.status_code, call.counts.total_tokens) == (status, 0)
    assert call.cost_usd == Decimal(0)
    assert await rig.store.pending(rig.tenant.org, rig.run_id) == []


async def test_a_timeout_records_an_estimate(rig: Rig) -> None:
    rig.api.error = httpx.ReadTimeout("slow")
    with pytest.raises(MediaError, match="in time"):
        await rig.speech.speak(rig.claims(), "hello")
    [call] = rig.usage.calls
    assert call.usage_estimated is True
    assert call.counts.input == len("hello")


async def test_a_connect_error_records_nothing(rig: Rig) -> None:
    rig.api.error = httpx.ConnectError("down")
    with pytest.raises(MediaError, match="cannot be reached"):
        await rig.images.generate(rig.claims(), "a fox")
    assert rig.usage.calls == []


@pytest.mark.parametrize(("image", "match"), [(b"GIF89a", "cannot read"), (b"", "cannot read")])
async def test_an_answer_that_is_not_a_png_is_refused(rig: Rig, image: bytes, match: str) -> None:
    rig.api.image = image
    with pytest.raises(MediaError, match=match):
        await rig.images.generate(rig.claims(), "a fox")
    assert await rig.store.pending(rig.tenant.org, rig.run_id) == []
    assert len(rig.usage.calls) == 1  # the provider charged for it


async def test_an_answer_that_is_not_ogg_is_refused(rig: Rig) -> None:
    rig.api.audio = b"ID3mp3"
    with pytest.raises(MediaError, match="no audio"):
        await rig.speech.speak(rig.claims(), "hello")


async def test_a_run_without_authority_cannot_spend(rig: Rig) -> None:
    rig.usage.authorities[str(rig.run_id)] = Authority(problem="the run is not active")
    with pytest.raises(MediaError, match="not allowed now: the run is not active"):
        await rig.images.generate(rig.claims(), "a fox")
    assert rig.api.requests == []


async def test_a_run_over_its_budget_cannot_spend(rig: Rig) -> None:
    await rig.images.generate(rig.claims(), "a fox")
    with pytest.raises(MediaError, match="not allowed now"):
        await rig.images.generate(rig.claims(max_tokens=10), "a fox")
    assert len(rig.api.requests) == 1


async def test_the_store_is_scoped_by_org_and_marks_delivery(rig: Rig, engine: AsyncEngine) -> None:
    result = await rig.images.generate(rig.claims(), "a fox")
    assert result.media_id is not None
    org, session = rig.tenant.org, rig.tenant.ids["s3"]
    loaded = await rig.store.load(org, session, result.media_id)
    assert loaded is not None
    assert loaded.data == PNG
    assert await rig.store.load(uuid.uuid4(), session, result.media_id) is None
    assert await rig.store.load(org, rig.tenant.ids["s1"], result.media_id) is None
    assert await rig.store.pending(uuid.uuid4(), rig.run_id) == []
    await rig.store.delivered(uuid.uuid4(), result.media_id)  # another org: no change
    assert len(await rig.store.pending(org, rig.run_id)) == 1
    await rig.store.delivered(org, result.media_id)
    assert await rig.store.pending(org, rig.run_id) == []
    assert await rig.store.load(org, session, result.media_id) is not None


async def test_a_cancelled_caller_does_not_lose_the_record(rig: Rig) -> None:
    gate = asyncio.Event()
    accepted = asyncio.Event()
    original = rig.api.__call__

    async def slow(request: httpx.Request) -> httpx.Response:
        accepted.set()
        await gate.wait()
        return await original(request)

    rig.images._http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    task = asyncio.create_task(rig.images.generate(rig.claims(), "a fox"))
    await accepted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await rig.images.aclose()
    [call] = rig.usage.calls
    assert call.usage_estimated is False  # the call ended and its real usage was recorded
