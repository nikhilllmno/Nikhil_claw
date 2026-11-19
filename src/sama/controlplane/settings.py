"""The settings of the control plane process, read from the environment (docs/architecture.md,
Section 19.2)."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from pydantic import SecretStr

from sama.sandbox.e2b import DEFAULT_TEMPLATE
from sama.vault.crypto import VaultKey

DEFAULT_SANDBOX_HOSTS: Final = (
    "github.com",
    "codeload.github.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
)
"""Hosts that every session sandbox can reach, for code work (Section 16.2): GitHub for
public clones, and the Python and npm package registries for test dependencies."""


class SettingsError(ValueError):
    """A variable is missing or not valid. The message names the variable, never its value."""


def _secret(env: Mapping[str, str], name: str) -> SecretStr | None:
    value = env.get(name, "").strip()
    return SecretStr(value) if value else None


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise SettingsError(f"set {name}")
    return value


def _public_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise SettingsError("SAMA_PUBLIC_URL must be an http or https URL")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise SettingsError("SAMA_PUBLIC_URL must have no path, query, or fragment")
    return f"{parts.scheme}://{parts.netloc}"


def _hosts(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(h.strip().lower() for h in value.split(",") if h.strip()))


def _count(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name, "").strip()
    if not value:
        return default
    if not value.isdigit() or int(value) < 1:
        raise SettingsError(f"{name} must be a whole number of 1 or more")
    return int(value)


@dataclass(frozen=True, slots=True)
class LangfuseConfig:
    base_url: str
    public_key: str
    secret_key: SecretStr = field(repr=False)


@dataclass(frozen=True, slots=True)
class ControlPlaneSettings:
    """``public_url`` is the address that the sandboxes and Telegram use to reach this
    process. The model proxy is at ``<public_url>/model`` and the tool server is at
    ``<public_url>/tools``."""

    database_url: SecretStr
    public_url: str
    run_token_key: SecretStr
    supabase_url: str
    supabase_anon_key: str
    e2b_api_key: SecretStr
    supabase_jwt_secret: SecretStr | None = None
    sandbox_template: str = DEFAULT_TEMPLATE
    sandbox_allow_out: tuple[str, ...] = ()
    anthropic_key: SecretStr | None = None
    openai_key: SecretStr | None = None
    brave_key: SecretStr | None = None
    langfuse: LangfuseConfig | None = None
    slack_webhook_url: SecretStr | None = None
    environment: str | None = None
    trace_content: bool = True
    worker_concurrency: int = 4
    vault_key: VaultKey | None = None

    def secret_values(self) -> list[str]:
        """Every secret of the settings, for the redactor (Section 16.3)."""
        secrets = [
            self.database_url,
            self.run_token_key,
            self.e2b_api_key,
            self.supabase_jwt_secret,
            self.anthropic_key,
            self.openai_key,
            self.brave_key,
            self.slack_webhook_url,
        ]
        if self.langfuse is not None:
            secrets.append(self.langfuse.secret_key)
        values = [secret.get_secret_value() for secret in secrets if secret is not None]
        password = urlsplit(self.database_url.get_secret_value()).password
        if password:
            values.append(password)
        if self.vault_key is not None:
            values += [
                base64.b64encode(self.vault_key.secret).decode(),
                base64.urlsafe_b64encode(self.vault_key.secret).decode(),
            ]
        return values

    @property
    def model_url(self) -> str:
        return f"{self.public_url}/model"

    @property
    def tools_url(self) -> str:
        return f"{self.public_url}/tools"

    @property
    def allow_out(self) -> tuple[str, ...]:
        """The egress allowlist of the session sandboxes: this process first, then
        ``DEFAULT_SANDBOX_HOSTS``, then the extra hosts of ``SAMA_SANDBOX_ALLOW_OUT``."""
        host = urlsplit(self.public_url).hostname or ""
        return tuple(dict.fromkeys((host, *DEFAULT_SANDBOX_HOSTS, *self.sandbox_allow_out)))

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ControlPlaneSettings:
        key = _required(env, "SAMA_RUN_TOKEN_KEY")
        if len(key.encode()) < 32:
            raise SettingsError("SAMA_RUN_TOKEN_KEY must have at least 32 bytes")
        langfuse = None
        langfuse_key = env.get("LANGFUSE_PUBLIC_KEY", "").strip()
        if langfuse_key:
            langfuse = LangfuseConfig(
                base_url=_required(env, "LANGFUSE_BASE_URL").rstrip("/"),
                public_key=langfuse_key,
                secret_key=SecretStr(_required(env, "LANGFUSE_SECRET_KEY")),
            )
        vault_key = None
        if env.get("SAMA_VAULT_KEY", "").strip():
            try:
                vault_key = VaultKey.from_text(env["SAMA_VAULT_KEY"])
            except ValueError:
                raise SettingsError("SAMA_VAULT_KEY must be 32 bytes in base64") from None
        return cls(
            database_url=SecretStr(_required(env, "SAMA_DATABASE_URL")),
            public_url=_public_url(_required(env, "SAMA_PUBLIC_URL")),
            run_token_key=SecretStr(key),
            supabase_url=_required(env, "SAMA_SUPABASE_URL").rstrip("/"),
            supabase_anon_key=_required(env, "SAMA_SUPABASE_ANON_KEY"),
            e2b_api_key=SecretStr(_required(env, "E2B_API_KEY")),
            supabase_jwt_secret=_secret(env, "SAMA_SUPABASE_JWT_SECRET"),
            sandbox_template=env.get("SAMA_SANDBOX_TEMPLATE", "").strip() or DEFAULT_TEMPLATE,
            sandbox_allow_out=_hosts(env.get("SAMA_SANDBOX_ALLOW_OUT", "")),
            anthropic_key=_secret(env, "ANTHROPIC_API_KEY"),
            openai_key=_secret(env, "OPENAI_API_KEY"),
            brave_key=_secret(env, "BRAVE_API_KEY"),
            langfuse=langfuse,
            slack_webhook_url=_secret(env, "SAMA_ALERT_SLACK_WEBHOOK_URL"),
            environment=env.get("SAMA_ENVIRONMENT", "").strip() or None,
            trace_content=env.get("SAMA_TRACE_CONTENT", "on").strip().lower() != "off",
            worker_concurrency=_count(env, "SAMA_WORKER_CONCURRENCY", 4),
            vault_key=vault_key,
        )
