"""``video_analyze`` (docs/architecture.md, Section 10.2.2; roadmap M7.5), with a fake Gemini
API, a fake download host, and a fake sandbox. No database."""

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

from sama.media.calls import gemini_counts
from sama.media.generate import MediaStatus
from sama.media.meter import MediaError, MediaMeter
from sama.media.video import (
    DEFAULT_VIDEO_MODEL,
    FILE_ESTIMATE,
    MAX_VIDEO_BYTES,
    VideoService,
    sandbox_path,
    video_type,
    youtube_url,
)
from sama.modelproxy.ledger import UsageLedger
from sama.modelproxy.prices import PriceTable, Provider, TokenCounts
from sama.modelproxy.store import MemoryUsageStore
from sama.modelproxy.usage import Endpoint
from sama.sandbox.base import SandboxFileTooLargeError
from sama.tenancy.authority import Authority
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.outbound import OutboundClient, UrlPolicy
from sama.vault.redact import REDACTED, Redactor
from sama.vault.store import KeySource, OrgKeys, SecretName
from tests.media.support import OwnKeys
from tests.support import wire_transport
from tests.waiting import async_wait_until

KEY = "gemini-test-key-0001"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
WORKDIR = "/home/user/work"


@dataclass
class Gemini:
    status: int = 200
    text: str = "A cat jumps on a table. Someone says hello."
    usage: dict[str, Any] | None = field(
        default_factory=lambda: {
            "promptTokenCount": 3_000,
            "candidatesTokenCount": 40,
            "promptTokensDetails": [
                {"modality": "VIDEO", "tokenCount": 2_000},
                {"modality": "AUDIO", "tokenCount": 900},
                {"modality": "TEXT", "tokenCount": 100},
            ],
        }
    )
    error: Exception | None = None
    stall: asyncio.Event | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.stall is not None:
            await self.stall.wait()
        if self.error is not None:
            raise self.error
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": f"bad {KEY}"}})
        body: dict[str, Any] = {
            "candidates": [{"content": {"parts": [{"text": self.text}]}}],
        }
        if self.usage is not None:
            body["usageMetadata"] = self.usage
        return httpx.Response(200, json=body)

    def sent(self) -> dict[str, Any]:
        data = json.loads(self.requests[-1].content)
        assert isinstance(data, dict)
        return data


class FakeSandbox:
    workdir = WORKDIR

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.reads: list[str] = []

    async def read_file(self, path: str, *, max_bytes: int | None = None) -> bytes:
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        data = self.files[path]
        if max_bytes is not None and len(data) > max_bytes:
            raise SandboxFileTooLargeError(path)
        return data


@dataclass
class Rig:
    service: VideoService
    api: Gemini
    usage: MemoryUsageStore
    sandbox: FakeSandbox
    downloads: list[httpx.Request]

    def claims(self, **extra: Any) -> RunTokenClaims:
        return RunTokenClaims(
            org_id=str(uuid.UUID(int=1)),
            workspace_id=str(uuid.UUID(int=2)),
            session_id=str(uuid.UUID(int=3)),
            run_id=str(uuid.UUID(int=4)),
            user_id=str(uuid.UUID(int=5)),
            token_id="test",
            expires_at=2_000_000_000,
            **extra,
        )


async def _resolver(host: str, port: int) -> list[Any]:
    import ipaddress

    return [ipaddress.ip_address("93.184.216.34")]


def build(
    *,
    key: str | None = KEY,
    keys: OrgKeys | None = None,
    redactor: Redactor | None = None,
    files: dict[str, bytes] | None = None,
    download: bytes = MP4,
    download_status: int = 200,
    sandbox: bool = True,
) -> Rig:
    api = Gemini()
    usage = MemoryUsageStore()
    meter = MediaMeter(usage=usage, prices=PriceTable.load(), ledger=UsageLedger(usage, retry_s=0))
    downloads: list[httpx.Request] = []

    def host(request: httpx.Request) -> httpx.Response:
        downloads.append(request)
        return httpx.Response(download_status, content=download)

    fake = FakeSandbox(files if files is not None else {f"{WORKDIR}/clip.mp4": MP4})

    async def session_sandbox(org_id: str, session_id: str) -> Any:
        return fake if sandbox else None

    service = VideoService(
        keys=keys
        or OrgKeys(None, {SecretName.GEMINI_API_KEY: None if key is None else SecretStr(key)}),
        meter=meter,
        session_sandbox=session_sandbox,
        base_url="https://gemini.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(api)),
        outbound=OutboundClient(
            UrlPolicy(max_bytes=MAX_VIDEO_BYTES + 1),
            resolver=_resolver,
            transport=wire_transport(host),
        ),
        redactor=redactor,
    )
    return Rig(service, api, usage, fake, downloads)


def test_the_default_model_has_a_price() -> None:
    assert PriceTable.load().lookup(Provider.GEMINI, DEFAULT_VIDEO_MODEL) is not None


def test_a_model_without_a_price_is_refused() -> None:
    usage = MemoryUsageStore()
    with pytest.raises(ValueError, match="no price"):
        VideoService(
            keys=OrgKeys(None, {}),
            meter=MediaMeter(usage=usage, prices=PriceTable.load()),
            model="gemini-unknown",
        )


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (MP4, "video/mp4"),
        (b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8, "video/quicktime"),
        (b"\x00\x00\x00\x14ftyp3gp4" + b"\x00" * 8, "video/3gpp"),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 8, "video/webm"),
        (b"RIFF\x00\x00\x00\x00AVI LIST", "video/avi"),
        (b"FLV\x01" + b"\x00" * 8, "video/x-flv"),
        (b"\x00\x00\x01\xba" + b"\x00" * 8, "video/mpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, None),
        (b"<html>", None),
    ],
)
def test_the_type_comes_from_the_bytes(data: bytes, mime: str | None) -> None:
    assert video_type(data) == mime


def test_youtube_links() -> None:
    assert youtube_url("https://www.youtube.com/watch?v=abc") is not None
    assert youtube_url("https://youtu.be/abc") is not None
    assert youtube_url("http://www.youtube.com/watch?v=abc") is None
    assert youtube_url("https://evil.example/youtube.com") is None
    assert youtube_url("https://user@youtube.com/watch?v=abc") is None


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "a/../../x", "", ".", "a\\b"])
def test_a_sandbox_path_must_stay_in_the_work_folder(bad: str) -> None:
    with pytest.raises(MediaError):
        sandbox_path(bad, WORKDIR)


def test_a_sandbox_path_in_the_work_folder() -> None:
    assert sandbox_path("clips/a.mp4", WORKDIR) == f"{WORKDIR}/clips/a.mp4"
    assert sandbox_path("./a.mp4", WORKDIR) == f"{WORKDIR}/a.mp4"


def test_audio_tokens_have_their_weight() -> None:
    counts = gemini_counts(
        {
            "promptTokenCount": 1_000,
            "candidatesTokenCount": 10,
            "thoughtsTokenCount": 5,
            "cachedContentTokenCount": 100,
            "promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 300}],
        }
    )
    # 1000 - 100 cached - 300 audio + 300 * 2 = 1200.
    assert counts == TokenCounts(input=1_200, output=15, cache_read=100)
    price = PriceTable.load()
    cost = price.cost(Provider.GEMINI, DEFAULT_VIDEO_MODEL, TokenCounts(input=1_000_000 * 2))
    # A million audio tokens cost USD 3.00.
    assert cost == Decimal(3)


async def test_a_youtube_link_goes_to_gemini_and_is_recorded() -> None:
    rig = build()
    link = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    result = await rig.service.analyze(rig.claims(), link, "What happens?")
    assert result.status is MediaStatus.OK
    assert result.text == rig.api.text
    request = rig.api.requests[-1]
    assert request.url.path == f"/v1beta/models/{DEFAULT_VIDEO_MODEL}:generateContent"
    assert request.headers["x-goog-api-key"] == KEY
    assert KEY not in str(request.url)
    body = rig.api.sent()
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"file_data": {"file_uri": link}}
    assert parts[1] == {"text": "What happens?"}
    assert body["generationConfig"]["mediaResolution"] == "MEDIA_RESOLUTION_LOW"
    assert rig.downloads == []
    [call] = rig.usage.calls
    assert call.provider is Provider.GEMINI
    assert call.endpoint is Endpoint.GENERATE_CONTENT
    assert call.model == DEFAULT_VIDEO_MODEL
    assert call.counts.input == 3_000 - 900 + 1_800
    assert call.counts.output == 40
    assert call.usage_estimated is False
    assert call.key_source is KeySource.SAMA
    assert call.cost_usd > 0


async def test_a_downloaded_file_is_sent_inline() -> None:
    rig = build()
    result = await rig.service.analyze(rig.claims(), "https://cdn.example/clip.mp4", "")
    assert result.status is MediaStatus.OK
    [download] = rig.downloads
    assert download.headers["host"] == "cdn.example"
    part = rig.api.sent()["contents"][0]["parts"][0]
    assert part["inline_data"]["mime_type"] == "video/mp4"
    assert base64.b64decode(part["inline_data"]["data"]) == MP4


async def test_a_sandbox_file_is_read_from_the_work_folder() -> None:
    rig = build()
    result = await rig.service.analyze(rig.claims(), "clip.mp4", "Who speaks?")
    assert result.status is MediaStatus.OK
    assert rig.sandbox.reads == [f"{WORKDIR}/clip.mp4"]


async def test_a_private_address_is_not_downloaded() -> None:
    rig = build()
    with pytest.raises(MediaError, match="cannot be downloaded"):
        await rig.service.analyze(rig.claims(), "https://127.0.0.1/clip.mp4", "q")
    assert rig.api.requests == []
    assert rig.usage.calls == []


async def test_a_file_that_is_not_a_video_is_refused_before_the_call() -> None:
    rig = build(download=b"<html>sign in</html>")
    with pytest.raises(MediaError, match="not a video"):
        await rig.service.analyze(rig.claims(), "https://cdn.example/clip.mp4", "q")
    assert rig.api.requests == []
    assert rig.usage.calls == []


async def test_a_large_file_is_refused() -> None:
    rig = build(download=MP4 + b"\x00" * MAX_VIDEO_BYTES)
    with pytest.raises(MediaError, match="larger than 14 MB"):
        await rig.service.analyze(rig.claims(), "https://cdn.example/clip.mp4", "q")
    big = build(files={f"{WORKDIR}/big.mp4": MP4 + b"\x00" * MAX_VIDEO_BYTES})
    with pytest.raises(MediaError, match="larger than 14 MB"):
        await big.service.analyze(big.claims(), "big.mp4", "q")


async def test_a_missing_sandbox_file_and_no_sandbox() -> None:
    rig = build()
    with pytest.raises(MediaError, match="does not exist"):
        await rig.service.analyze(rig.claims(), "nope.mp4", "q")
    none = build(sandbox=False)
    with pytest.raises(MediaError, match="no sandbox"):
        await none.service.analyze(none.claims(), "clip.mp4", "q")


async def test_without_a_key_the_tool_says_it_is_off() -> None:
    rig = build(key=None)
    result = await rig.service.analyze(rig.claims(), "https://youtu.be/abc", "q")
    assert result.status is MediaStatus.UNAVAILABLE
    assert "no Gemini key" in result.text
    assert rig.api.requests == []


async def test_the_key_of_the_org_comes_first() -> None:
    rig = build(keys=OwnKeys(SecretName.GEMINI_API_KEY, "own-gemini-key"))
    await rig.service.analyze(rig.claims(), "https://youtu.be/abc", "q")
    assert rig.api.requests[-1].headers["x-goog-api-key"] == "own-gemini-key"
    assert rig.usage.calls[0].key_source is KeySource.ORG


async def test_a_refused_key_gives_a_safe_error_and_a_zero_row() -> None:
    rig = build()
    rig.api.status = 403
    with pytest.raises(MediaError) as caught:
        await rig.service.analyze(rig.claims(), "https://youtu.be/abc", "q")
    assert KEY not in str(caught.value)
    assert "refused the key" in str(caught.value)
    [call] = rig.usage.calls
    assert call.status_code == 403
    assert call.cost_usd == 0


async def test_a_time_out_is_charged_with_the_estimate() -> None:
    rig = build()
    rig.api.error = httpx.ReadTimeout("slow")
    with pytest.raises(MediaError, match="did not finish in time"):
        await rig.service.analyze(rig.claims(), "clip.mp4", "q")
    [call] = rig.usage.calls
    assert call.usage_estimated is True
    assert call.counts.input == FILE_ESTIMATE.input


async def test_a_connect_error_is_not_charged() -> None:
    rig = build()
    rig.api.error = httpx.ConnectError("down")
    with pytest.raises(MediaError, match="cannot be reached"):
        await rig.service.analyze(rig.claims(), "clip.mp4", "q")
    assert rig.usage.calls == []


async def test_a_cancelled_caller_still_pays() -> None:
    rig = build()
    rig.api.stall = asyncio.Event()
    task = asyncio.create_task(rig.service.analyze(rig.claims(), "clip.mp4", "q"))
    await async_wait_until(lambda: rig.api.requests, what="the request")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rig.api.stall.set()
    await rig.service.aclose()
    [call] = rig.usage.calls
    assert call.status_code == 200


async def test_a_run_without_budget_is_refused_before_the_call() -> None:
    rig = build()
    rig.usage.authorities[rig.claims().run_id] = Authority(problem="the run was cancelled")
    with pytest.raises(MediaError, match="not allowed now"):
        await rig.service.analyze(rig.claims(), "clip.mp4", "q")
    assert rig.api.requests == []


async def test_the_question_and_the_answer_are_redacted() -> None:
    secret = "sk-live-secret-value-123456"
    redactor = Redactor()
    redactor.add(secret)
    rig = build(redactor=redactor)
    rig.api.text = f"The screen shows {secret}."
    result = await rig.service.analyze(rig.claims(), "clip.mp4", f"is {secret} shown?")
    assert secret not in json.dumps(rig.api.sent()["contents"][0]["parts"][1])
    assert secret not in result.text
    assert REDACTED in result.text


async def test_an_empty_answer_is_an_error() -> None:
    rig = build()
    rig.api.text = ""
    with pytest.raises(MediaError, match="no answer"):
        await rig.service.analyze(rig.claims(), "clip.mp4", "q")
