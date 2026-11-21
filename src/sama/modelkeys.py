"""The model keys of an org (docs/architecture.md, Section 17.3, M6.7).

An org admin can give the org its own Anthropic key and its own OpenAI key. The key goes in the
vault (``anthropic_api_key``, ``openai_api_key``). The model proxy and the media tools then use
the key of the org in place of the key of sama, and they record the cost the same way, with
``model_calls.key_source = 'org'``.

Before it stores a key, the service asks the provider to list its models with the key. A key
that the provider refuses is not stored. No answer of the service has the key or a part of it.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import httpx
from pydantic import SecretStr

from sama.modelproxy.prices import Provider
from sama.vault.store import OrgKeys, SecretName, Vault

log: Final = logging.getLogger("sama.modelkeys")

ANTHROPIC_VERSION: Final = "2023-06-01"
CHECK_TIMEOUT_S: Final = 10.0
MIN_KEY_CHARS: Final = 20
MAX_KEY_CHARS: Final = 512
_KEY: Final = re.compile(r"\S+")


_SECRET: Final = {
    Provider.ANTHROPIC: SecretName.ANTHROPIC_API_KEY,
    Provider.OPENAI: SecretName.OPENAI_API_KEY,
}
_PREFIX: Final = {Provider.ANTHROPIC: "sk-ant-", Provider.OPENAI: "sk-"}


class ModelKeyError(Exception):
    """A key that cannot be set or removed. ``status_code`` is the HTTP answer."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ModelKeyStatus:
    """What an org admin sees about one provider. It has no part of a key."""

    provider: Provider
    org_key: bool
    """True if the org has its own key in the vault."""
    updated_at: datetime | None
    """When the key of the org changed last."""
    default_key: bool
    """True if sama has a key for the provider, for orgs without their own key."""

    def public(self) -> dict[str, object]:
        return {
            "provider": self.provider.value,
            "org_key": self.org_key,
            "updated_at": None if self.updated_at is None else self.updated_at.isoformat(),
            "default_key": self.default_key,
        }


def check_format(provider: Provider, key: str) -> str:
    """Gives the key without the spaces at its ends. The message of an error has no part of
    the key."""
    key = key.strip()
    if not MIN_KEY_CHARS <= len(key) <= MAX_KEY_CHARS or not _KEY.fullmatch(key):
        raise ModelKeyError(
            f"the key must have {MIN_KEY_CHARS} to {MAX_KEY_CHARS} characters and no spaces"
        )
    if not key.startswith(_PREFIX[provider]):
        raise ModelKeyError(f"an {provider.value} key starts with {_PREFIX[provider]}")
    return key


class ModelKeys:
    """Sets, removes, and shows the model keys of an org. ``vault`` must be the vault of the
    model proxy, so that a change applies at once in this process."""

    def __init__(
        self,
        vault: Vault,
        keys: OrgKeys,
        *,
        anthropic_url: str = "https://api.anthropic.com",
        openai_url: str = "https://api.openai.com",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._vault = vault
        self._keys = keys
        self._urls = {
            Provider.ANTHROPIC: anthropic_url.rstrip("/"),
            Provider.OPENAI: openai_url.rstrip("/"),
        }
        self._http = http or httpx.AsyncClient(timeout=CHECK_TIMEOUT_S, follow_redirects=False)
        self._own_http = http is None

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    async def status(self, org_id: uuid.UUID) -> list[ModelKeyStatus]:
        updated = await self._vault.updated(org_id)
        return [
            ModelKeyStatus(
                provider=provider,
                org_key=name in updated,
                updated_at=updated.get(name),
                default_key=self._keys.has_default(name),
            )
            for provider, name in _SECRET.items()
        ]

    async def set(
        self, org_id: uuid.UUID, provider: Provider, key: SecretStr, actor: uuid.UUID
    ) -> ModelKeyStatus:
        """Checks the key with the provider, then stores it. The vault writes the audit entry
        ``vault.secret_set`` without the value."""
        value = check_format(provider, key.get_secret_value())
        await self._check(provider, value)
        await self._vault.put(org_id, _SECRET[provider], value, actor_user_id=actor)
        log.info("an org key is set org=%s provider=%s", org_id, provider.value)
        [status] = [s for s in await self.status(org_id) if s.provider is provider]
        return status

    async def remove(self, org_id: uuid.UUID, provider: Provider, actor: uuid.UUID) -> None:
        """Removes the key of the org. The calls of the org then use the key of sama."""
        if not await self._vault.delete(org_id, _SECRET[provider], actor_user_id=actor):
            raise ModelKeyError("the org has no key for this provider", status_code=404)
        log.info("an org key is removed org=%s provider=%s", org_id, provider.value)

    async def _check(self, provider: Provider, key: str) -> None:
        """Lists the models with the key. It costs no tokens."""
        if provider is Provider.ANTHROPIC:
            headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        else:
            headers = {"authorization": f"Bearer {key}"}
        try:
            response = await self._http.get(f"{self._urls[provider]}/v1/models", headers=headers)
        except httpx.HTTPError as error:
            log.warning("the key check failed provider=%s error=%s", provider, type(error).__name__)
            raise ModelKeyError(
                f"{provider.value} cannot be reached now. Try again later.", status_code=502
            ) from None
        if response.status_code == 200:
            return
        if response.status_code in (401, 403):
            raise ModelKeyError(f"{provider.value} refused the key")
        log.warning("the key check failed provider=%s status=%d", provider, response.status_code)
        raise ModelKeyError(
            f"{provider.value} did not accept the check now (HTTP {response.status_code}). "
            "Try again later.",
            status_code=502,
        )
