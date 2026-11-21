"""``moa``: mixture of agents (docs/architecture.md, Section 10.2.2; roadmap M7.5), with fake
Anthropic, OpenAI, and Gemini APIs. No database."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sama.media.meter import MediaError, MediaMeter
from sama.moa import (
    AGGREGATE_MAX_TOKENS,
    DEFAULT_AGGREGATORS,
    DEFAULT_REFERENCES,
    MixtureService,
    MoaModel,
    render,
)
from sama.modelproxy.ledger import UsageLedger
from sama.modelproxy.prices import PriceTable, Provider
from sama.modelproxy.store import MemoryUsageStore
from sama.modelproxy.usage import Endpoint
from sama.tenancy.authority import Authority
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import REDACTED, Redactor
from sama.vault.store import OrgKeys, SecretName

KEYS = {
    SecretName.ANTHROPIC_API_KEY: "sk-ant-test-0001",
    SecretName.OPENAI_API_KEY: "sk-openai-test-0001",
    SecretName.GEMINI_API_KEY: "gemini-test-0001",
}


@dataclass
class Providers:
    """One fake for the three APIs. ``fail`` maps a host to a status code."""

    fail: dict[str, int] = field(default_factory=dict)
    texts: dict[str, str] = field(
        default_factory=lambda: {
            "anthropic.test": "Claude says 42.",
            "openai.test": "GPT says 42.",
            "gemini.test": "Gemini says 41.",
        }
    )
    requests: list[httpx.Request] = field(default_factory=list)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host in self.fail:
            return httpx.Response(self.fail[host], json={"error": "no"})
        text = self.texts[host]
        body = json.loads(request.content)
        if host == "anthropic.test":
            if "<question>" in body["messages"][0]["content"]:
                text = "The answer is 42. One model said 41."
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            )
        if host == "openai.test":
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": text}}],
                    "usage": {"prompt_tokens": 90, "completion_tokens": 40},
                },
            )
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": text}]}}],
                "usageMetadata": {"promptTokenCount": 80, "candidatesTokenCount": 30},
            },
        )

    def bodies(self, host: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.host == host]


@dataclass
class Rig:
    service: MixtureService
    api: Providers
    usage: MemoryUsageStore

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


def build(
    *,
    without: tuple[SecretName, ...] = (),
    cap: Decimal = Decimal("0.50"),
    redactor: Redactor | None = None,
) -> Rig:
    api = Providers()
    usage = MemoryUsageStore()
    meter = MediaMeter(usage=usage, prices=PriceTable.load(), ledger=UsageLedger(usage, retry_s=0))
    keys = OrgKeys(
        None,
        {name: None if name in without else SecretStr(value) for name, value in KEYS.items()},
    )
    service = MixtureService(
        keys=keys,
        meter=meter,
        max_cost_usd=cap,
        base_urls={
            Provider.ANTHROPIC: "https://anthropic.test",
            Provider.OPENAI: "https://openai.test",
            Provider.GEMINI: "https://gemini.test",
        },
        http=httpx.AsyncClient(transport=httpx.MockTransport(api)),
        redactor=redactor,
    )
    return Rig(service, api, usage)


def test_the_default_models_have_prices() -> None:
    table = PriceTable.load()
    for model in (*DEFAULT_REFERENCES, *DEFAULT_AGGREGATORS):
        assert table.lookup(model.provider, model.model) is not None, model.name


def test_a_model_without_a_price_is_refused() -> None:
    usage = MemoryUsageStore()
    with pytest.raises(ValueError, match="no price"):
        MixtureService(
            keys=OrgKeys(None, {}),
            meter=MediaMeter(usage=usage, prices=PriceTable.load()),
            references=(MoaModel(Provider.OPENAI, "gpt-unknown"),),
        )


async def test_three_models_answer_and_the_aggregator_writes_the_answer() -> None:
    rig = build()
    answer = await rig.service.ask(rig.claims(), "What is six times seven?")
    assert answer.available
    assert answer.text == "The answer is 42. One model said 41."
    assert answer.references == (
        "anthropic/claude-sonnet-5-5",
        "openai/gpt-6.1-terra",
        "gemini/gemini-3.5-flash",
    )
    assert answer.aggregator == "anthropic/claude-opus-5-5"
    assert Decimal(0) < answer.max_cost_usd <= Decimal("0.50")
    # The aggregator gets each answer as data.
    aggregate = [b for b in rig.api.bodies("anthropic.test") if b["model"] == "claude-opus-5-5"]
    [body] = aggregate
    prompt = body["messages"][0]["content"]
    for text in ("Claude says 42.", "GPT says 42.", "Gemini says 41."):
        assert text in prompt
    assert body["max_tokens"] == AGGREGATE_MAX_TOKENS
    # Four calls, each with its own record and endpoint.
    calls = sorted((c.provider.value, c.model, c.endpoint) for c in rig.usage.calls)
    assert calls == [
        ("anthropic", "claude-opus-5-5", Endpoint.MESSAGES),
        ("anthropic", "claude-sonnet-5-5", Endpoint.MESSAGES),
        ("gemini", "gemini-3.5-flash", Endpoint.GENERATE_CONTENT),
        ("openai", "gpt-6.1-terra", Endpoint.CHAT_COMPLETIONS),
    ]
    assert all(c.cost_usd > 0 and not c.usage_estimated for c in rig.usage.calls)
    note, text = render(answer)
    assert "3 models answered" in note
    assert text == answer.text


async def test_the_keys_go_only_in_headers() -> None:
    rig = build()
    await rig.service.ask(rig.claims(), "q?")
    for request in rig.api.requests:
        for key in KEYS.values():
            assert key not in str(request.url)
    by_host = {r.url.host: r for r in rig.api.requests}
    assert by_host["anthropic.test"].headers["x-api-key"] == KEYS[SecretName.ANTHROPIC_API_KEY]
    assert by_host["openai.test"].headers["authorization"].endswith(KEYS[SecretName.OPENAI_API_KEY])
    assert by_host["gemini.test"].headers["x-goog-api-key"] == KEYS[SecretName.GEMINI_API_KEY]


async def test_two_keys_are_enough_and_the_aggregator_follows_the_keys() -> None:
    rig = build(without=(SecretName.ANTHROPIC_API_KEY,))
    answer = await rig.service.ask(rig.claims(), "q?")
    assert answer.available
    assert answer.aggregator == "openai/gpt-6.2-sol"
    assert len(answer.references) == 2


async def test_one_key_is_not_enough() -> None:
    rig = build(without=(SecretName.ANTHROPIC_API_KEY, SecretName.OPENAI_API_KEY))
    answer = await rig.service.ask(rig.claims(), "q?")
    assert not answer.available
    assert "at least two" in answer.text
    assert rig.api.requests == []


async def test_a_failed_model_is_skipped() -> None:
    rig = build()
    rig.api.fail["openai.test"] = 500
    answer = await rig.service.ask(rig.claims(), "q?")
    assert answer.available
    assert answer.failed == ("openai/gpt-6.1-terra",)
    assert "No answer from: openai/gpt-6.1-terra" in render(answer)[0]


async def test_a_low_cap_drops_the_expensive_models_or_refuses() -> None:
    rig = build()
    # The sonnet and opus plan is the most expensive part. A small cap drops a reference.
    full = rig.service.plan(
        "q", list(DEFAULT_REFERENCES), DEFAULT_AGGREGATORS[0], _keys(), Decimal(100)
    )
    assert len(full.references) == 3
    smaller = rig.service.plan(
        "q",
        list(DEFAULT_REFERENCES),
        DEFAULT_AGGREGATORS[0],
        _keys(),
        full.worst_usd - Decimal("0.0001"),
    )
    assert len(smaller.references) == 2
    assert smaller.worst_usd < full.worst_usd
    with pytest.raises(MediaError, match="above the limit"):
        await rig.service.ask(rig.claims(), "q?", Decimal("0.001"))
    assert rig.api.requests == []


async def test_the_remaining_run_budget_caps_the_plan() -> None:
    rig = build()
    claims = rig.claims(max_cost_usd=Decimal("0.002"))
    with pytest.raises(MediaError, match=r"above the limit of USD 0\.00"):
        await rig.service.ask(claims, "q?")
    assert rig.api.requests == []


async def test_a_run_without_authority_is_refused() -> None:
    rig = build()
    rig.usage.authorities[rig.claims().run_id] = Authority(problem="the run was cancelled")
    with pytest.raises(MediaError, match="not allowed now"):
        await rig.service.ask(rig.claims(), "q?")


async def test_the_question_and_the_answer_are_redacted() -> None:
    secret = "-".join(("sk", "live", "secret", "moa", "123456"))
    redactor = Redactor()
    redactor.add(secret)
    rig = build(redactor=redactor)
    rig.api.texts["anthropic.test"] = f"key {secret}"
    answer = await rig.service.ask(rig.claims(), f"is {secret} valid?")
    for request in rig.api.requests:
        assert secret not in request.content.decode()
    assert secret not in answer.text
    assert REDACTED in json.dumps(rig.api.bodies("openai.test")[0])


async def test_empty_and_long_questions_are_refused() -> None:
    rig = build()
    with pytest.raises(MediaError, match="empty"):
        await rig.service.ask(rig.claims(), "  ")
    with pytest.raises(MediaError, match="more than"):
        await rig.service.ask(rig.claims(), "x" * 8_001)


def _keys() -> dict[Provider, Any]:
    from sama.media.calls import ProviderKey
    from sama.vault.store import KeySource

    return {provider: ProviderKey("k", KeySource.SAMA) for provider in Provider}


async def test_the_aggregator_call_is_checked_again() -> None:
    """A fence or a lost authority while the references run stops the aggregator call."""
    rig = build()
    run_id = rig.claims().run_id
    sent: list[str] = []
    original = rig.api.__call__

    async def revoke_after_references(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.host)
        if len(sent) == 3:
            rig.usage.authorities[run_id] = Authority(problem="the run was cancelled")
        return await original(request)

    rig.service._http = httpx.AsyncClient(transport=httpx.MockTransport(revoke_after_references))
    with pytest.raises(MediaError, match="not allowed now"):
        await rig.service.ask(rig.claims(), "q?")
    assert len(sent) == 3  # no aggregator call
    assert len(rig.usage.calls) == 3


async def test_a_long_answer_is_cut_to_the_planned_size() -> None:
    from sama.moa import MAX_REFERENCE_BYTES

    rig = build()
    rig.api.texts["openai.test"] = "é" * MAX_REFERENCE_BYTES
    await rig.service.ask(rig.claims(), "q?")
    aggregate = [b for b in rig.api.bodies("anthropic.test") if b["model"] == "claude-opus-5-5"]
    prompt = aggregate[0]["messages"][0]["content"]
    answer = prompt.split('<answer index="2">\n')[1].split("\n</answer>")[0]
    assert len(answer.encode()) <= MAX_REFERENCE_BYTES
    assert answer == "é" * (MAX_REFERENCE_BYTES // 2)
