"""The vision service (docs/architecture.md, Section 11.7), with a fake Anthropic API."""

from __future__ import annotations

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

from sama.media.store import AttachmentStore
from sama.media.vision import DEFAULT_VISION_MODEL, NO_KEY, NOT_FOUND, VisionError, VisionService
from sama.modelproxy.prices import PriceTable, Provider
from sama.modelproxy.store import MemoryUsageStore, OrgLimits
from sama.tenancy.authority import Authority
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import REDACTED, Redactor
from sama.vault.store import OrgKeys, SecretName
from tests.media.support import PNG, add_image
from tests.orchestrator.conftest import Tenant

KEY = "sk-ant-vision-key-0001"


@dataclass
class Anthropic:
    """The fake API: records each request and answers with ``status`` and ``body``."""

    status: int = 200
    body: dict[str, Any] = field(
        default_factory=lambda: {
            "content": [{"type": "text", "text": "A bar chart."}],
            "usage": {"input_tokens": 1500, "output_tokens": 20},
        }
    )
    error: Exception | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, json=self.body)

    def sent(self) -> dict[str, Any]:
        body = json.loads(self.requests[-1].content)
        assert isinstance(body, dict)
        return body


@dataclass
class Rig:
    service: VisionService
    api: Anthropic
    usage: MemoryUsageStore
    tenant: Tenant
    engine: AsyncEngine

    def claims(self, session: str = "s3", *, user: bool = True, **extra: Any) -> RunTokenClaims:
        ids = self.tenant.ids
        return RunTokenClaims(
            org_id=str(self.tenant.org),
            workspace_id=str(ids["ws"]),
            session_id=str(ids[session]),
            run_id=str(ids["org"]),
            user_id=str(ids["mia"]) if user else None,
            token_id="test",
            expires_at=2_000_000_000,
            **extra,
        )

    async def image(self, session: str = "s3", **kwargs: Any) -> uuid.UUID:
        return await add_image(self.engine, self.tenant.org, self.tenant.ids[session], **kwargs)


def build(
    engine: AsyncEngine,
    tenant: Tenant,
    *,
    key: str | None = KEY,
    redactor: Redactor | None = None,
    usage: MemoryUsageStore | None = None,
    api: Anthropic | None = None,
) -> Rig:
    api = api or Anthropic()
    usage = usage or MemoryUsageStore()
    keys = OrgKeys(None, {SecretName.ANTHROPIC_API_KEY: None if key is None else SecretStr(key)})
    service = VisionService(
        store=AttachmentStore(engine),
        keys=keys,
        usage=usage,
        prices=PriceTable.load(),
        base_url="https://anthropic.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(api)),
        redactor=redactor,
    )
    return Rig(service, api, usage, tenant, engine)


@pytest.fixture
async def rig(engine: AsyncEngine, tenant: Tenant) -> Rig:
    return build(engine, tenant)


async def test_the_default_model_has_a_price() -> None:
    assert PriceTable.load().lookup(Provider.ANTHROPIC, DEFAULT_VISION_MODEL) is not None


async def test_an_unknown_model_is_refused(engine: AsyncEngine) -> None:
    with pytest.raises(ValueError, match="no price"):
        VisionService(
            store=AttachmentStore(engine),
            keys=OrgKeys(None, {}),
            usage=MemoryUsageStore(),
            prices=PriceTable.load(),
            model="claude-unknown-9",
        )


async def test_the_image_and_the_question_go_to_the_model(rig: Rig) -> None:
    image_id = await rig.image(data=PNG, name="sales.png")
    answer = await rig.service.analyze(rig.claims(), str(image_id), "What is the top bar?")
    assert answer == "A bar chart."
    request = rig.api.requests[0]
    assert str(request.url) == "https://anthropic.test/v1/messages"
    assert request.headers["x-api-key"] == KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = rig.api.sent()
    assert body["model"] == DEFAULT_VISION_MODEL
    image, question = body["messages"][0]["content"]
    assert image["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(PNG).decode(),
    }
    assert question == {"type": "text", "text": "What is the top bar?"}
    assert KEY not in json.dumps(body)


async def test_latest_is_the_newest_image_of_the_session(rig: Rig) -> None:
    await rig.image(data=PNG, mime_type="image/png")
    newest = b"GIF89a" + b"\x01" * 10
    await rig.image(data=newest, mime_type="image/gif")
    await rig.service.analyze(rig.claims(), "latest", "what?")
    source = rig.api.sent()["messages"][0]["content"][0]["source"]
    assert source["media_type"] == "image/gif"
    assert base64.b64decode(source["data"]) == newest


@pytest.mark.parametrize("attachment", ["latest", "not-an-id", str(uuid.uuid4()), ""])
async def test_a_missing_image_is_not_found(rig: Rig, attachment: str) -> None:
    with pytest.raises(VisionError) as error:
        await rig.service.analyze(rig.claims(), attachment, "what?")
    assert str(error.value) == NOT_FOUND
    assert rig.api.requests == []


async def test_an_image_of_another_session_or_org_is_not_found(
    rig: Rig,
    engine: AsyncEngine,
) -> None:
    other_session = await rig.image("s2")
    with pytest.raises(VisionError) as error:
        await rig.service.analyze(rig.claims("s3"), str(other_session), "what?")
    assert str(error.value) == NOT_FOUND
    mine = await rig.image("s3")
    foreign = RunTokenClaims(
        org_id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        session_id=str(rig.tenant.ids["s3"]),
        run_id=str(uuid.uuid4()),
        user_id=None,
        token_id="t",
        expires_at=2_000_000_000,
    )
    with pytest.raises(VisionError):
        await rig.service.analyze(foreign, str(mine), "what?")
    assert rig.api.requests == []


async def test_without_a_key_the_answer_says_so(engine: AsyncEngine, tenant: Tenant) -> None:
    rig = build(engine, tenant, key=None)
    await rig.image()
    assert await rig.service.analyze(rig.claims(), "latest", "what?") == NO_KEY
    assert rig.api.requests == []
    assert rig.usage.calls == []


async def test_the_question_is_redacted_before_it_leaves(
    engine: AsyncEngine,
    tenant: Tenant,
) -> None:
    redactor = Redactor()
    redactor.add("zq-secret-value-77")
    rig = build(engine, tenant, redactor=redactor)
    await rig.image()
    await rig.service.analyze(rig.claims(), "latest", "is zq-secret-value-77 visible?")
    text = rig.api.sent()["messages"][0]["content"][1]["text"]
    assert "zq-secret-value-77" not in text
    assert REDACTED in text


async def test_the_usage_is_recorded_for_the_run(rig: Rig) -> None:
    await rig.image()
    claims = rig.claims()
    await rig.service.analyze(claims, "latest", "what?")
    (call,) = rig.usage.calls
    assert (call.org_id, call.session_id, call.run_id, call.user_id) == (
        claims.org_id,
        claims.session_id,
        claims.run_id,
        claims.user_id,
    )
    assert (call.model, call.status_code) == (DEFAULT_VISION_MODEL, 200)
    assert (call.counts.input, call.counts.output) == (1500, 20)
    assert call.cost_usd > 0


async def test_the_budget_of_the_run_is_kept(rig: Rig) -> None:
    await rig.image()
    await rig.service.analyze(rig.claims(), "latest", "what?")
    # 1520 tokens are spent. A limit of 1000 refuses the next call.
    with pytest.raises(VisionError, match="not allowed now"):
        await rig.service.analyze(rig.claims(max_tokens=1000), "latest", "what?")
    assert len(rig.api.requests) == 1


async def test_a_run_without_authority_is_refused(rig: Rig) -> None:
    await rig.image()
    claims = rig.claims()
    rig.usage.authorities[claims.run_id] = Authority(problem="the run was cancelled")
    with pytest.raises(VisionError, match="the run was cancelled"):
        await rig.service.analyze(claims, "latest", "what?")
    assert rig.api.requests == []


async def test_the_limits_of_the_org_and_the_user_are_kept(
    engine: AsyncEngine,
    tenant: Tenant,
) -> None:
    org = str(tenant.org)
    limits = {org: OrgLimits(model_usd_per_month=Decimal("0.000001"))}
    rig = build(engine, tenant, usage=MemoryUsageStore(limits))
    await rig.image()
    await rig.service.analyze(rig.claims(), "latest", "what?")
    with pytest.raises(VisionError, match="the org reached its limit"):
        await rig.service.analyze(rig.claims(), "latest", "what?")
    rig.usage.limits = {org: OrgLimits(model_usd_per_user_day=Decimal("0.000001"))}
    with pytest.raises(VisionError, match="you reached your limit"):
        await rig.service.analyze(rig.claims(), "latest", "what?")
    # A shared run has no user: the daily limit of a user does not apply.
    assert await rig.service.analyze(rig.claims(user=False), "latest", "what?") == "A bar chart."


async def test_a_store_that_fails_refuses_the_call(rig: Rig) -> None:
    await rig.image()

    async def broken(org_id: str, run_id: str) -> Authority:
        raise RuntimeError("database down at 10.0.0.9")

    rig.usage.authority = broken  # type: ignore[method-assign]
    with pytest.raises(VisionError) as error:
        await rig.service.analyze(rig.claims(), "latest", "what?")
    assert "10.0.0.9" not in str(error.value)
    assert rig.api.requests == []


@pytest.mark.parametrize(
    ("status", "words"),
    [
        (401, "refused the key"),
        (403, "refused the key"),
        (429, "busy"),
        (400, "could not use this image"),
        (413, "could not use this image"),
        (500, "not available"),
    ],
)
async def test_an_http_error_gives_a_short_safe_text(
    engine: AsyncEngine,
    tenant: Tenant,
    status: int,
    words: str,
) -> None:
    api = Anthropic(status=status, body={"error": {"message": f"bad {KEY} body secret"}})
    rig = build(engine, tenant, api=api)
    await rig.image()
    with pytest.raises(VisionError) as error:
        await rig.service.analyze(rig.claims(), "latest", "what?")
    assert words in str(error.value)
    assert "body secret" not in str(error.value)
    assert KEY not in str(error.value)
    assert rig.usage.calls == []


async def test_a_timeout_and_a_network_error_are_short_texts(
    engine: AsyncEngine,
    tenant: Tenant,
) -> None:
    api = Anthropic(error=httpx.ReadTimeout("slow"))
    rig = build(engine, tenant, api=api)
    await rig.image()
    with pytest.raises(VisionError, match="did not answer in time"):
        await rig.service.analyze(rig.claims(), "latest", "what?")
    api.error = httpx.ConnectError("no route to 10.0.0.1")
    with pytest.raises(VisionError) as error:
        await rig.service.analyze(rig.claims(), "latest", "what?")
    assert "10.0.0.1" not in str(error.value)


async def test_an_empty_answer_is_an_error(engine: AsyncEngine, tenant: Tenant) -> None:
    api = Anthropic(body={"content": [], "usage": {"input_tokens": 1, "output_tokens": 0}})
    rig = build(engine, tenant, api=api)
    await rig.image()
    with pytest.raises(VisionError, match="no answer"):
        await rig.service.analyze(rig.claims(), "latest", "what?")


async def test_a_long_answer_is_cut(engine: AsyncEngine, tenant: Tenant) -> None:
    body = {"content": [{"type": "text", "text": "x" * 50_000}], "usage": {"input_tokens": 1}}
    rig = build(engine, tenant, api=Anthropic(body=body))
    await rig.image()
    assert len(await rig.service.analyze(rig.claims(), "latest", "what?")) == 8_000
