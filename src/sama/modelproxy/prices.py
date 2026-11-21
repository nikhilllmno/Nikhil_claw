"""Model prices and the cost of one call (docs/architecture.md, Section 16.6).

The prices are data in ``prices.toml``. A model that is not in the table is not allowed,
because the proxy cannot compute its cost.
"""

from __future__ import annotations

import re
import tomllib
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field

PRICES_FILE: Final = Path(__file__).with_name("prices.toml")
MILLION: Final = Decimal(1_000_000)
_DATED_ID: Final = re.compile(r"^(?P<base>.+?)-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    """Only the tools of the control plane call Gemini (``video_analyze`` and ``moa``,
    Section 10.2.2). The model proxy has no Gemini route."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TokenCounts(_Frozen):
    """The tokens of one call, split by price. ``input`` has no cache tokens."""

    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)
    cache_write: int = Field(default=0, ge=0)
    cache_write_long: int = Field(default=0, ge=0)
    web_search_requests: int = Field(default=0, ge=0)

    @property
    def prompt_tokens(self) -> int:
        """All input tokens of the call. The long-context tier uses this number."""
        return self.input + self.cache_read + self.cache_write + self.cache_write_long

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output


class Rates(_Frozen):
    """USD per million tokens."""

    input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)
    cache_read: Decimal = Field(ge=0)
    cache_write: Decimal = Field(ge=0)
    cache_write_long: Decimal = Field(ge=0)


class LongContext(Rates):
    above_tokens: int = Field(gt=0)


class ModelPrice(Rates):
    long_context: LongContext | None = None

    def rates_for(self, counts: TokenCounts) -> Rates:
        tier = self.long_context
        if tier is not None and counts.prompt_tokens > tier.above_tokens:
            return tier
        return self


class ProviderPrices(_Frozen):
    web_search_per_1k: Decimal = Field(default=Decimal(0), ge=0)
    models: dict[str, ModelPrice]
    media: dict[str, ModelPrice] = Field(default_factory=dict)
    """The models of the media tools (Section 11.8). The proxy does not allow them: a run cannot
    call them as chat models."""


class PriceTable(_Frozen):
    anthropic: ProviderPrices
    openai: ProviderPrices
    gemini: ProviderPrices = Field(default_factory=lambda: ProviderPrices(models={}))

    @classmethod
    def load(cls, path: Path = PRICES_FILE) -> Self:
        return cls.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))

    def _prices(self, provider: Provider) -> ProviderPrices:
        if provider is Provider.GEMINI:
            return self.gemini
        return self.anthropic if provider is Provider.ANTHROPIC else self.openai

    def lookup(self, provider: Provider, model: str) -> ModelPrice | None:
        """The price of ``model``. A dated id uses the price of its base id."""
        models = self._prices(provider).models
        if model in models:
            return models[model]
        dated = _DATED_ID.match(model)
        return models.get(dated["base"]) if dated else None

    def media(self, provider: Provider, model: str) -> ModelPrice | None:
        """The price of a media model, for example ``gpt-image-2.6-flare``."""
        return self._prices(provider).media.get(model)

    def cost(self, provider: Provider, model: str, counts: TokenCounts) -> Decimal:
        price = self.lookup(provider, model)
        if price is None:
            raise KeyError(f"no price for the model {model!r}")
        return self._cost(provider, price, counts)

    def media_cost(self, provider: Provider, model: str, counts: TokenCounts) -> Decimal:
        price = self.media(provider, model)
        if price is None:
            raise KeyError(f"no price for the media model {model!r}")
        return self._cost(provider, price, counts)

    def _cost(self, provider: Provider, price: ModelPrice, counts: TokenCounts) -> Decimal:
        rates = price.rates_for(counts)
        tokens = (
            rates.input * counts.input
            + rates.output * counts.output
            + rates.cache_read * counts.cache_read
            + rates.cache_write * counts.cache_write
            + rates.cache_write_long * counts.cache_write_long
        )
        searches = self._prices(provider).web_search_per_1k * counts.web_search_requests
        return tokens / MILLION + searches / 1000
