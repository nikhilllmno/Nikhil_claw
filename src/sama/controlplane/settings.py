"""The settings of the control plane process, read from the environment (docs/architecture.md,
Section 19.2)."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from pydantic import SecretStr

from sama.api.origins import parse_origins
from sama.connectors.host import DEFAULT_HOST_TEMPLATE
from sama.github.client import GitHubAppConfig
from sama.media.vision import DEFAULT_VISION_MODEL
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


def _github(env: Mapping[str, str]) -> GitHubAppConfig | None:
    """The sama GitHub App, when ``SAMA_GITHUB_APP_ID`` is set. Then all five variables are
    required. The private key can have literal ``\\n`` for its line breaks."""
    if not env.get("SAMA_GITHUB_APP_ID", "").strip():
        return None
    app_id = _required(env, "SAMA_GITHUB_APP_ID")
    if not app_id.isdigit():
        raise SettingsError("SAMA_GITHUB_APP_ID must be a number")
    key = _required(env, "SAMA_GITHUB_APP_PRIVATE_KEY").replace("\\n", "\n")
    if "PRIVATE KEY-----" not in key:
        raise SettingsError("SAMA_GITHUB_APP_PRIVATE_KEY must be a PEM private key")
    return GitHubAppConfig(
        app_id=app_id,
        slug=_required(env, "SAMA_GITHUB_APP_SLUG"),
        client_id=_required(env, "SAMA_GITHUB_CLIENT_ID"),
        private_key=SecretStr(key.strip() + "\n"),
        client_secret=SecretStr(_required(env, "SAMA_GITHUB_CLIENT_SECRET")),
    )


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
    connector_host_template: str = DEFAULT_HOST_TEMPLATE
    """The E2B template of the connector hosts of stdio MCP servers (Section 10.3.2)."""
    sandbox_allow_out: tuple[str, ...] = ()
    vision_model: str = DEFAULT_VISION_MODEL
    anthropic_key: SecretStr | None = None
    openai_key: SecretStr | None = None
    brave_key: SecretStr | None = None
    composio_key: SecretStr | None = None
    """The Composio API key of the process (``COMPOSIO_API_KEY``). An org can set its own key
    in the vault (``composio_api_key``)."""
    composio_webhook_secret: SecretStr | None = None
    """The signing secret of the webhook of the Composio project of the process
    (``SAMA_COMPOSIO_WEBHOOK_SECRET``). An org with its own Composio key keeps its own secret in
    the vault (``composio_webhook_secret``)."""
    langfuse: LangfuseConfig | None = None
    slack_webhook_url: SecretStr | None = None
    environment: str | None = None
    trace_content: bool = True
    worker_concurrency: int = 4
    vault_key: VaultKey | None = None
    web_origins: tuple[str, ...] = ()
    """The origins of the web app that may call the API from a browser (``SAMA_WEB_ORIGINS``)."""
    github: GitHubAppConfig | None = None
    """The sama GitHub App (``SAMA_GITHUB_*``). Without it, the git proxy and the pull request
    tools are off."""
    sendgrid_key: SecretStr | None = None
    """The SendGrid key for the ``email:`` delivery of jobs (``SAMA_SENDGRID_API_KEY``)."""
    email_from: str | None = None
    """The sender address of job emails (``SAMA_EMAIL_FROM``)."""

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
            self.composio_key,
            self.composio_webhook_secret,
            self.slack_webhook_url,
            self.sendgrid_key,
        ]
        if self.langfuse is not None:
            secrets.append(self.langfuse.secret_key)
        values = [secret.get_secret_value() for secret in secrets if secret is not None]
        if self.github is not None:
            values += self.github.secret_values()
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
    def web_base(self) -> str:
        """The address of the web app, for the links of the Connections page: the first
        origin of ``SAMA_WEB_ORIGINS``, else ``public_url``."""
        return self.web_origins[0] if self.web_origins else self.public_url

    @property
    def model_url(self) -> str:
        return f"{self.public_url}/model"

    @property
    def tools_url(self) -> str:
        return f"{self.public_url}/tools"

    @property
    def git_url(self) -> str:
        """The git proxy (Section 15.1): ``<git_url>/<owner>/<name>.git``."""
        return f"{self.public_url}/git"

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
        try:
            web_origins = parse_origins(env.get("SAMA_WEB_ORIGINS", ""))
        except ValueError as error:
            raise SettingsError(f"SAMA_WEB_ORIGINS: {error}") from None
        return cls(
            database_url=SecretStr(_required(env, "SAMA_DATABASE_URL")),
            github=_github(env),
            public_url=_public_url(_required(env, "SAMA_PUBLIC_URL")),
            run_token_key=SecretStr(key),
            supabase_url=_required(env, "SAMA_SUPABASE_URL").rstrip("/"),
            supabase_anon_key=_required(env, "SAMA_SUPABASE_ANON_KEY"),
            e2b_api_key=SecretStr(_required(env, "E2B_API_KEY")),
            supabase_jwt_secret=_secret(env, "SAMA_SUPABASE_JWT_SECRET"),
            sandbox_template=env.get("SAMA_SANDBOX_TEMPLATE", "").strip() or DEFAULT_TEMPLATE,
            connector_host_template=env.get("SAMA_CONNECTOR_HOST_TEMPLATE", "").strip()
            or DEFAULT_HOST_TEMPLATE,
            sandbox_allow_out=_hosts(env.get("SAMA_SANDBOX_ALLOW_OUT", "")),
            vision_model=env.get("SAMA_VISION_MODEL", "").strip() or DEFAULT_VISION_MODEL,
            anthropic_key=_secret(env, "ANTHROPIC_API_KEY"),
            openai_key=_secret(env, "OPENAI_API_KEY"),
            brave_key=_secret(env, "BRAVE_API_KEY"),
            composio_key=_secret(env, "COMPOSIO_API_KEY"),
            composio_webhook_secret=_secret(env, "SAMA_COMPOSIO_WEBHOOK_SECRET"),
            langfuse=langfuse,
            slack_webhook_url=_secret(env, "SAMA_ALERT_SLACK_WEBHOOK_URL"),
            environment=env.get("SAMA_ENVIRONMENT", "").strip() or None,
            trace_content=env.get("SAMA_TRACE_CONTENT", "on").strip().lower() != "off",
            worker_concurrency=_count(env, "SAMA_WORKER_CONCURRENCY", 4),
            vault_key=vault_key,
            web_origins=web_origins,
            sendgrid_key=_secret(env, "SAMA_SENDGRID_API_KEY"),
            email_from=env.get("SAMA_EMAIL_FROM", "").strip() or None,
        )
