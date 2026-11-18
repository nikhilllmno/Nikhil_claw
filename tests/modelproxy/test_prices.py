"""The price table and the cost of a call (docs/architecture.md, Section 16.6)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sama.modelproxy.prices import PriceTable, Provider, TokenCounts

TABLE = PriceTable.load()


def test_the_table_loads_and_every_price_is_set() -> None:
    assert TABLE.anthropic.models
    assert TABLE.openai.models
    for prices in (TABLE.anthropic, TABLE.openai):
        for price in prices.models.values():
            assert price.input > 0
            assert price.output > price.input


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
def test_the_current_claude_models_have_prices(model: str) -> None:
    assert TABLE.lookup(Provider.ANTHROPIC, model) is not None


def test_a_dated_id_uses_the_price_of_its_base_id() -> None:
    base = TABLE.lookup(Provider.ANTHROPIC, "claude-haiku-4-5")
    assert base is not None
    assert TABLE.lookup(Provider.ANTHROPIC, "claude-haiku-4-5-20251001") == base
    assert TABLE.lookup(Provider.OPENAI, "gpt-6-luna-2026-05-01") == TABLE.lookup(
        Provider.OPENAI, "gpt-6-luna"
    )


def test_an_unknown_model_has_no_price() -> None:
    assert TABLE.lookup(Provider.ANTHROPIC, "claude-unknown-9") is None
    assert TABLE.lookup(Provider.OPENAI, "claude-opus-5-5") is None
    with pytest.raises(KeyError):
        TABLE.cost(Provider.ANTHROPIC, "claude-unknown-9", TokenCounts())


def test_the_cost_adds_each_token_kind_at_its_rate() -> None:
    counts = TokenCounts(
        input=1_000_000,
        output=1_000_000,
        cache_read=1_000_000,
        cache_write=1_000_000,
        cache_write_long=1_000_000,
    )
    # Claude Sonnet 5.5: 2 + 10 + 0.10 + 2.50 + 4 USD.
    assert TABLE.cost(Provider.ANTHROPIC, "claude-sonnet-5-5", counts) == Decimal("18.6")


def test_web_searches_cost_extra() -> None:
    counts = TokenCounts(web_search_requests=3)
    assert TABLE.cost(Provider.ANTHROPIC, "claude-opus-5-5", counts) == Decimal("0.03")


def test_a_long_prompt_uses_the_long_context_prices() -> None:
    short = TokenCounts(input=100_000, output=1000)
    long = TokenCounts(input=90_000, cache_read=10_001, output=1000)
    assert long.prompt_tokens == 100_001
    # Claude Haiku 5.5: 0.10 and 0.50 USD per million up to 100,000 prompt tokens.
    assert TABLE.cost(Provider.ANTHROPIC, "claude-haiku-5-5", short) == Decimal("0.0105")
    # Above it, 0.50 input, 0.05 cache read, and 2.50 output.
    expected = Decimal("0.045") + Decimal("0.00050005") + Decimal("0.0025")
    assert TABLE.cost(Provider.ANTHROPIC, "claude-haiku-5-5", long) == expected


def test_usage_has_both_cache_writes() -> None:
    usage = TokenCounts(
        input=1, output=2, cache_read=3, cache_write=4, cache_write_long=5
    ).to_usage(Decimal("0.5"))
    assert usage.cache_write_tokens == 9
    assert usage.total_tokens == 15
    assert usage.cost_usd == Decimal("0.5")
