"""One metered model call of a control-plane tool (docs/architecture.md, Section 10.2.2; roadmap
M7.5).

``video_analyze`` and ``moa`` call Anthropic, OpenAI, or Gemini with the key of the org, as
``vision_analyze`` and ``image_generate`` do. The key stays in the control plane. Each call that
can have reached the provider writes one ``model_calls`` row through the shared ledger, so the
cost counts for the run, the user, and the org. The call runs in a task that a cancel of the
caller does not stop (``MediaMeter.shielded``).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from sama.media.meter import MediaError, MediaMeter
from sama.modelproxy.prices import Provider, TokenCounts
from sama.modelproxy.usage import Endpoint, anthropic_counts, openai_counts
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.crypto import VaultError
from sama.vault.store import KeySource, OrgKeys, SecretName

log = logging.getLogger("sama.media")

ANTHROPIC_URL: Final = "https://api.anthropic.com"
ANTHROPIC_VERSION: Final = "2023-06-01"
OPENAI_URL: Final = "https://api.openai.com"
GEMINI_URL: Final = "https://generativelanguage.googleapis.com"

KEY_NAMES: Final[Mapping[Provider, SecretName]] = {
    Provider.ANTHROPIC: SecretName.ANTHROPIC_API_KEY,
    Provider.OPENAI: SecretName.OPENAI_API_KEY,
    Provider.GEMINI: SecretName.GEMINI_API_KEY,
}
AUDIO_WEIGHT: Final = 2
"""Gemini 3.5 Flash charges audio input at USD 3.00 and other input at USD 1.50 per million
tokens. The count gives each audio token 2 input tokens, so one input rate gives both
prices."""

_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
"""Errors before the request reached the provider: no charge, so no record."""


@dataclass(frozen=True, slots=True)
class ProviderKey:
    value: str = field(repr=False)
    source: KeySource


async def provider_key(
    keys: OrgKeys, claims: RunTokenClaims, provider: Provider
) -> ProviderKey | None:
    """The key of the org for ``provider``, else the key of sama; ``None`` without a key."""
    try:
        found = await keys.lookup(claims.org_id, KEY_NAMES[provider])
    except VaultError:
        log.warning("the %s key of org=%s does not decrypt", provider.value, claims.org_id)
        return None
    if found.value is None:
        return None
    return ProviderKey(found.value.get_secret_value(), found.source)


def gemini_counts(usage: Mapping[str, Any]) -> TokenCounts:
    """The tokens of a Gemini answer. Audio input tokens get their weight (``AUDIO_WEIGHT``).
    The output has the thinking tokens, because Gemini charges them as output."""

    def number(data: Mapping[str, Any], key: str) -> int:
        value = data.get(key)
        return value if isinstance(value, int) and value >= 0 else 0

    prompt = number(usage, "promptTokenCount")
    cached = min(number(usage, "cachedContentTokenCount"), prompt)
    audio = 0
    details = usage.get("promptTokensDetails")
    if isinstance(details, list):
        for item in details:
            if isinstance(item, Mapping) and item.get("modality") == "AUDIO":
                audio += number(item, "tokenCount")
    audio = min(audio, prompt - cached)
    return TokenCounts(
        input=prompt - cached - audio + audio * AUDIO_WEIGHT,
        output=number(usage, "candidatesTokenCount") + number(usage, "thoughtsTokenCount"),
        cache_read=cached,
    )


def usage_of(provider: Provider, data: Mapping[str, Any]) -> TokenCounts | None:
    """The token counts of a ``200`` answer, or ``None`` when the answer has no usage."""
    key = "usageMetadata" if provider is Provider.GEMINI else "usage"
    usage = data.get(key)
    if not isinstance(usage, Mapping):
        return None
    if provider is Provider.GEMINI:
        return gemini_counts(usage)
    if provider is Provider.ANTHROPIC:
        return anthropic_counts(usage)
    return openai_counts(usage)


def json_of(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def failure(label: str, status: int) -> str:
    """A short text for a refused call: no key, no URL, and no body of the provider."""
    if status in (401, 403):
        return f"{label} refused the key of the org. Ask an admin to check it."
    if status == 429:
        return f"{label} is busy now. Try again later."
    if status in (400, 404, 413, 422):
        return f"{label} refused the request. Change the input and try again."
    return f"{label} is not available now. Try again later."


@dataclass(frozen=True, slots=True)
class Call:
    """One request to a provider. ``estimate`` is the charge when the real usage is not
    known (a cancel, a time limit, a broken connection, or an answer without usage)."""

    provider: Provider
    model: str
    endpoint: Endpoint
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: Mapping[str, Any] = field(repr=False)
    estimate: TokenCounts
    label: str


def auth_headers(provider: Provider, key: ProviderKey) -> dict[str, str]:
    if provider is Provider.ANTHROPIC:
        return {"x-api-key": key.value, "anthropic-version": ANTHROPIC_VERSION}
    if provider is Provider.GEMINI:
        # In a header, not in the URL: a URL can reach a log.
        return {"x-goog-api-key": key.value}
    return {"authorization": f"Bearer {key.value}"}


async def metered_post(
    meter: MediaMeter,
    http: httpx.AsyncClient,
    claims: RunTokenClaims,
    call: Call,
    key: ProviderKey,
    *,
    timeout_s: float,
    counts: Callable[[Mapping[str, Any]], TokenCounts | None] | None = None,
) -> dict[str, Any]:
    """Sends ``call`` and settles its record. Returns the JSON body of a ``200`` answer.
    Raises ``MediaError`` with a safe text for every failure. Run it with
    ``meter.shielded``."""
    call_id = uuid.uuid4()
    started = time.monotonic()

    async def settle(found: TokenCounts, *, estimated: bool, status: int) -> None:
        await meter.settle(
            claims,
            call_id=call_id,
            started=started,
            provider=call.provider,
            model=call.model,
            endpoint=call.endpoint,
            counts=found,
            estimated=estimated,
            status=status,
            key_source=key.source,
            chat=True,
        )

    try:
        response = await http.post(
            call.url,
            headers={**call.headers, **auth_headers(call.provider, key)},
            json=dict(call.body),
            timeout=timeout_s,
        )
    except asyncio.CancelledError:
        await settle(call.estimate, estimated=True, status=0)
        raise
    except httpx.HTTPError as error:
        if not isinstance(error, _NOT_SENT):
            await settle(call.estimate, estimated=True, status=0)
        if isinstance(error, httpx.TimeoutException):
            raise MediaError(f"{call.label} did not finish in time. Try again.") from None
        log.warning("the model call failed run=%s error=%s", claims.run_id, type(error).__name__)
        raise MediaError(f"{call.label} cannot be reached now. Try again later.") from None
    if response.status_code == 200:
        data = json_of(response)
        found = (counts or (lambda body: usage_of(call.provider, body)))(data)
        await settle(found or call.estimate, estimated=found is None, status=200)
        return data
    # A refused call costs nothing, but it gets its row like in the model proxy.
    await settle(TokenCounts(), estimated=False, status=response.status_code)
    log.warning(
        "the model refused the call run=%s provider=%s status=%d",
        claims.run_id,
        call.provider.value,
        response.status_code,
    )
    raise MediaError(failure(call.label, response.status_code))


def gemini_text(data: Mapping[str, Any]) -> str:
    """The text of the first candidate of a Gemini answer."""
    candidates = data.get("candidates")
    first = candidates[0] if isinstance(candidates, list) and candidates else None
    content = first.get("content") if isinstance(first, Mapping) else None
    parts = content.get("parts") if isinstance(content, Mapping) else None
    if not isinstance(parts, list):
        return ""
    texts = [
        str(part["text"])
        for part in parts
        if isinstance(part, Mapping)
        and isinstance(part.get("text"), str)
        and not part.get("thought")
    ]
    return "".join(texts).strip()


def anthropic_text(data: Mapping[str, Any]) -> str:
    blocks = data.get("content")
    if not isinstance(blocks, list):
        return ""
    return "".join(
        str(block["text"])
        for block in blocks
        if isinstance(block, Mapping) and block.get("type") == "text" and "text" in block
    ).strip()


def openai_text(data: Mapping[str, Any]) -> str:
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    return content.strip() if isinstance(content, str) else ""
