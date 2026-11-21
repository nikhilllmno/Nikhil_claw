"""The vision service (docs/architecture.md, Section 11.7), with a fake Anthropic API."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

import sama.media.vision as vision_module
from sama.media.store import AttachmentStore
from sama.media.vision import (
    DEFAULT_VISION_MODEL,
    NO_KEY,
    NOT_FOUND,
    VisionError,
    VisionService,
    VisionStatus,
)
from sama.modelproxy.ledger import UsageLedger
from sama.modelproxy.prices import PriceTable, Provider, TokenCounts
from sama.modelproxy.store import CallRecord, MemoryUsageStore, OrgLimits
from sama.modelproxy.usage import Endpoint
from sama.tenancy.authority import Authority
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import REDACTED, Redactor
from sama.vault.store import KeySource, OrgKeys, SecretName
from tests.media.support import PNG, OwnKeys, add_image
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
    hold: asyncio.Event | None = None
    """When set, the fake accepts the request and waits for this event before it answers."""
    accepted: asyncio.Event | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.accepted is not None:
            self.accepted.set()
        if self.hold is not None:
            await self.hold.wait()
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
    keys: OrgKeys | None = None,
    api: Anthropic | None = None,
    ledger: UsageLedger | None = None,
) -> Rig:
    api = api or Anthropic()
    usage = usage or MemoryUsageStore()
    keys = keys or OrgKeys(
        None, {SecretName.ANTHROPIC_API_KEY: None if key is None else SecretStr(key)}
    )
    service = VisionService(
        store=AttachmentStore(engine),
        keys=keys,
        usage=usage,
        prices=PriceTable.load(),
        base_url="https://anthropic.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(api)),
        redactor=redactor,
        ledger=ledger or UsageLedger(usage, retry_s=0),
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
    assert (answer.status, answer.text) == (VisionStatus.OK, "A bar chart.")
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


@pytest.mark.parametrize("own", [False, True])
async def test_the_call_uses_the_key_of_the_org_and_records_its_source(
    engine: AsyncEngine, tenant: Tenant, own: bool
) -> None:
    """M6.7: the key of the org first. The cost is the same; only the source differs."""
    own_key = "sk-ant-own-key-of-the-org"
    keys = OwnKeys(SecretName.ANTHROPIC_API_KEY, own_key) if own else None
    rig = build(engine, tenant, keys=keys)
    image_id = await rig.image()
    answer = await rig.service.analyze(rig.claims(), str(image_id), "what?")
    assert answer.status is VisionStatus.OK
    assert rig.api.requests[0].headers["x-api-key"] == (own_key if own else KEY)
    (call,) = rig.usage.calls
    assert call.key_source is (KeySource.ORG if own else KeySource.SAMA)
    assert call.cost_usd > 0


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
    result = await rig.service.analyze(rig.claims(), "latest", "what?")
    assert (result.status, result.text) == (VisionStatus.UNAVAILABLE, NO_KEY)
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
    shared = await rig.service.analyze(rig.claims(user=False), "latest", "what?")
    assert shared.text == "A bar chart."


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
    assert len((await rig.service.analyze(rig.claims(), "latest", "what?")).text) == 8_000


class FlakyUsage(MemoryUsageStore):
    """A usage store whose ``record`` fails ``failures`` times."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts: list[uuid.UUID] = []

    async def record(self, call: CallRecord) -> None:
        self.attempts.append(call.call_id)
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("database down at 10.0.0.9")
        await super().record(call)


async def test_a_record_that_cannot_be_written_fences_the_org_until_it_is(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    usage = FlakyUsage(failures=100)
    ledger = UsageLedger(usage, retry_s=0)
    rig = build(engine, tenant, usage=usage, ledger=ledger)
    await rig.image()
    # The run has a budget of 1000 tokens. The first call spends 1520 and is not recorded.
    first = await rig.service.analyze(rig.claims(max_tokens=1000), "latest", "what?")
    assert first.text == "A bar chart."
    assert len(ledger.unrecorded) == 1
    with pytest.raises(VisionError, match="cannot record usage"):
        await rig.service.analyze(rig.claims(max_tokens=1000), "latest", "what?")
    assert len(rig.api.requests) == 1, "the second call never reached the provider"
    # Another org is not blocked by this record.
    assert not ledger.blocked(str(uuid.uuid4()))
    # The store comes back: the same record is written again, and the fence ends. The run
    # budget now sees the spend, so the budget refuses the next call.
    usage.failures = 0
    with pytest.raises(VisionError, match="not allowed now"):
        await rig.service.analyze(rig.claims(max_tokens=1000), "latest", "what?")
    assert ledger.unrecorded == []
    (call,) = usage.calls
    assert set(usage.attempts) == {call.call_id}, "every retry used the same call id"
    assert len(rig.api.requests) == 1


async def test_a_short_failure_of_the_store_is_retried_in_the_call(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    usage = FlakyUsage(failures=2)
    rig = build(engine, tenant, usage=usage)
    await rig.image()
    await rig.service.analyze(rig.claims(), "latest", "what?")
    assert len(usage.calls) == 1
    assert len(set(usage.attempts)) == 1
    assert len(usage.attempts) == 3


async def test_a_fence_of_the_shared_ledger_blocks_vision(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    usage = FlakyUsage(failures=0)
    ledger = UsageLedger(usage, retry_s=0)
    rig = build(engine, tenant, usage=usage, ledger=ledger)
    await rig.image()
    ledger.fence(record("1.00", org_id=str(tenant.org)))  # a failed write of the model proxy
    usage.failures = 1000
    with pytest.raises(VisionError, match="cannot record usage"):
        await rig.service.analyze(rig.claims(), "latest", "what?")
    assert rig.api.requests == []


def record(cost: str, *, org_id: str) -> CallRecord:
    return CallRecord(
        org_id=org_id,
        workspace_id="ws",
        session_id="s",
        run_id="r",
        user_id=None,
        provider=Provider.ANTHROPIC,
        model=DEFAULT_VISION_MODEL,
        endpoint=Endpoint.MESSAGES,
        status_code=200,
        counts=TokenCounts(input=1, output=1),
        key_source=KeySource.SAMA,
        cost_usd=Decimal(cost),
        duration_ms=1,
        created_at=datetime.now(UTC),
    )


async def test_a_cancel_after_the_provider_accepted_keeps_the_usage(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    api = Anthropic(hold=asyncio.Event(), accepted=asyncio.Event())
    rig = build(engine, tenant, api=api)
    await rig.image()
    caller = asyncio.create_task(rig.service.analyze(rig.claims(), "latest", "what?"))
    assert api.accepted is not None
    assert api.hold is not None
    await asyncio.wait_for(api.accepted.wait(), 5)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert rig.usage.calls == [], "the provider has not answered yet"
    api.hold.set()
    await asyncio.wait_for(_settled(rig), 5)
    (call,) = rig.usage.calls
    assert (call.counts.input, call.counts.output, call.usage_estimated) == (1500, 20, False)
    assert call.run_id == rig.claims().run_id


async def _settled(rig: Rig) -> None:
    await asyncio.gather(*rig.service._tasks, return_exceptions=True)


async def test_a_stop_while_the_provider_holds_the_call_records_an_estimate(
    engine: AsyncEngine, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = Anthropic(hold=asyncio.Event(), accepted=asyncio.Event())
    rig = build(engine, tenant, api=api)
    await rig.image()
    caller = asyncio.create_task(rig.service.analyze(rig.claims(), "latest", "what?"))
    assert api.accepted is not None
    await asyncio.wait_for(api.accepted.wait(), 5)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    monkeypatch.setattr(vision_module, "CLOSE_WAIT_S", 0.05)
    await rig.service.aclose()
    (call,) = rig.usage.calls
    assert call.usage_estimated
    assert call.counts.output == 1024, "the estimate uses the output limit"
    assert 8_000 <= call.counts.input < 20_000, "the image is a fixed allowance, not its bytes"
    assert call.cost_usd > 0


async def test_a_cancelled_call_with_a_failed_write_fences_the_org(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    usage = FlakyUsage(failures=1000)
    ledger = UsageLedger(usage, retry_s=0)
    api = Anthropic(hold=asyncio.Event(), accepted=asyncio.Event())
    rig = build(engine, tenant, usage=usage, api=api, ledger=ledger)
    await rig.image()
    caller = asyncio.create_task(rig.service.analyze(rig.claims(), "latest", "what?"))
    assert api.accepted is not None
    assert api.hold is not None
    await asyncio.wait_for(api.accepted.wait(), 5)
    caller.cancel()
    api.hold.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.wait_for(_settled(rig), 5)
    assert ledger.blocked(str(tenant.org))


async def test_a_broken_connection_after_the_send_records_an_estimate(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    api = Anthropic(error=httpx.ReadTimeout("slow"))
    rig = build(engine, tenant, api=api)
    await rig.image()
    with pytest.raises(VisionError, match="did not answer in time"):
        await rig.service.analyze(rig.claims(), "latest", "what?")
    api.error = httpx.RemoteProtocolError("peer closed")
    with pytest.raises(VisionError, match="cannot be reached"):
        await rig.service.analyze(rig.claims(), "latest", "what?")
    assert [c.usage_estimated for c in rig.usage.calls] == [True, True]
    assert len({c.call_id for c in rig.usage.calls}) == 2


async def test_an_error_before_the_send_costs_nothing(engine: AsyncEngine, tenant: Tenant) -> None:
    rig = build(engine, tenant, api=Anthropic(error=httpx.ConnectError("refused")))
    await rig.image()
    with pytest.raises(VisionError):
        await rig.service.analyze(rig.claims(), "latest", "what?")
    assert rig.usage.calls == []


async def test_an_answer_without_usage_records_an_estimate(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    body = {"content": [{"type": "text", "text": "ok"}]}
    rig = build(engine, tenant, api=Anthropic(body=body))
    await rig.image()
    await rig.service.analyze(rig.claims(), "latest", "what?")
    (call,) = rig.usage.calls
    assert (call.usage_estimated, call.status_code) == (True, 200)


async def test_a_secret_across_the_cut_does_not_leak_a_part(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    secret = "zq-cut-secret-value-9981"
    redactor = Redactor()
    redactor.add(secret)
    # The secret starts 10 characters before the cut at 8,000.
    text = "x" * 7_990 + secret + " tail"
    body = {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    rig = build(engine, tenant, redactor=redactor, api=Anthropic(body=body))
    await rig.image()
    answer = (await rig.service.analyze(rig.claims(), "latest", "what?")).text
    assert "zq-cut" not in answer
    assert REDACTED in answer
    assert len(answer) <= 8_000
