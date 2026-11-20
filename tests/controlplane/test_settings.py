from __future__ import annotations

from typing import Any

import pytest

from sama.controlplane import ControlPlaneSettings, SettingsError
from sama.controlplane.settings import DEFAULT_SANDBOX_HOSTS
from sama.media.vision import DEFAULT_VISION_MODEL

BASE = {
    "SAMA_DATABASE_URL": "postgresql://u:p@db.example.test/postgres",
    "SAMA_PUBLIC_URL": "https://sama-staging.fly.dev/",
    "SAMA_RUN_TOKEN_KEY": "k" * 32,
    "SAMA_SUPABASE_URL": "https://abc.supabase.co/",
    "SAMA_SUPABASE_ANON_KEY": "anon",
    "E2B_API_KEY": "e2b-key",
}


def settings(**changes: Any) -> ControlPlaneSettings:
    env = {**BASE, **{k: v for k, v in changes.items() if v is not None}}
    for name in [k for k, v in changes.items() if v is None]:
        env.pop(name, None)
    return ControlPlaneSettings.from_env(env)


def test_the_minimum_gives_the_urls_and_turns_the_options_off() -> None:
    s = settings()
    assert s.public_url == "https://sama-staging.fly.dev"
    assert s.model_url == "https://sama-staging.fly.dev/model"
    assert s.tools_url == "https://sama-staging.fly.dev/tools"
    assert s.supabase_url == "https://abc.supabase.co"
    assert s.allow_out == ("sama-staging.fly.dev", *DEFAULT_SANDBOX_HOSTS)
    assert "github.com" in s.allow_out
    assert "pypi.org" in s.allow_out
    assert s.sandbox_template == "sama-runner"
    assert s.worker_concurrency == 4
    assert s.trace_content
    assert (s.langfuse, s.slack_webhook_url, s.brave_key) == (None, None, None)


@pytest.mark.parametrize(
    "name",
    [
        "SAMA_DATABASE_URL",
        "SAMA_PUBLIC_URL",
        "SAMA_RUN_TOKEN_KEY",
        "SAMA_SUPABASE_URL",
        "SAMA_SUPABASE_ANON_KEY",
        "E2B_API_KEY",
    ],
)
def test_a_missing_required_variable_is_named(name: str) -> None:
    with pytest.raises(SettingsError, match=f"set {name}"):
        settings(**{name: None})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"SAMA_RUN_TOKEN_KEY": "short"}, "at least 32 bytes"),
        ({"SAMA_PUBLIC_URL": "sama.fly.dev"}, "http or https URL"),
        ({"SAMA_PUBLIC_URL": "https://sama.fly.dev/api"}, "no path"),
        ({"SAMA_WORKER_CONCURRENCY": "0"}, "1 or more"),
        ({"SAMA_WORKER_CONCURRENCY": "two"}, "1 or more"),
        ({"LANGFUSE_PUBLIC_KEY": "pk"}, "set LANGFUSE_BASE_URL"),
    ],
)
def test_a_bad_value_is_refused_without_its_value(changes: dict[str, str], message: str) -> None:
    with pytest.raises(SettingsError, match=message) as error:
        settings(**changes)
    for value in changes.values():
        if len(value) > 3:
            assert value not in str(error.value)


def test_the_web_origins_are_read() -> None:
    assert settings().web_origins == ()
    s = settings(SAMA_WEB_ORIGINS=" https://sama-web.example.test,http://localhost:3000, ")
    assert s.web_origins == ("https://sama-web.example.test", "http://localhost:3000")


@pytest.mark.parametrize("value", ["*", "https://example.test/", "example.test"])
def test_a_web_origin_that_is_not_exact_is_refused(value: str) -> None:
    with pytest.raises(SettingsError, match="SAMA_WEB_ORIGINS"):
        settings(SAMA_WEB_ORIGINS=value)


def test_the_options_are_read() -> None:
    s = settings(
        SAMA_SANDBOX_ALLOW_OUT=" GitHub.com, pypi.org,,example.org,sama-staging.fly.dev ",
        SAMA_SANDBOX_TEMPLATE="sama-runner-next",
        LANGFUSE_PUBLIC_KEY="pk",
        LANGFUSE_SECRET_KEY="sk",
        LANGFUSE_BASE_URL="https://us.cloud.langfuse.com/",
        SAMA_ALERT_SLACK_WEBHOOK_URL="https://hooks.slack.test/x",
        SAMA_TRACE_CONTENT="OFF",
        SAMA_ENVIRONMENT="staging",
        SAMA_WORKER_CONCURRENCY="2",
        BRAVE_API_KEY="brave",
    )
    assert s.allow_out == ("sama-staging.fly.dev", *DEFAULT_SANDBOX_HOSTS, "example.org")
    assert s.sandbox_template == "sama-runner-next"
    assert s.langfuse is not None
    assert s.langfuse.base_url == "https://us.cloud.langfuse.com"
    assert s.slack_webhook_url is not None
    assert not s.trace_content
    assert s.environment == "staging"
    assert s.worker_concurrency == 2
    assert s.brave_key is not None


def test_secrets_stay_out_of_the_repr() -> None:
    s = settings(LANGFUSE_PUBLIC_KEY="pk", LANGFUSE_SECRET_KEY="sk-secret", LANGFUSE_BASE_URL="x")
    text = repr(s)
    for value in ("u:p@db", "k" * 32, "e2b-key", "sk-secret"):
        assert value not in text


def test_the_vault_key_is_read_and_checked() -> None:
    from sama.vault.crypto import VaultKey

    assert settings().vault_key is None
    text = VaultKey.generate()
    key = settings(SAMA_VAULT_KEY=text).vault_key
    assert key is not None
    assert key.secret == VaultKey.from_text(text).secret
    for bad in ("not base64!", "c2hvcnQ="):
        with pytest.raises(SettingsError, match="SAMA_VAULT_KEY must be 32 bytes in base64"):
            settings(SAMA_VAULT_KEY=bad)


def test_the_secret_values_cover_every_secret() -> None:
    from sama.vault.crypto import VaultKey

    vault_text = VaultKey.generate()
    s = settings(
        SAMA_VAULT_KEY=vault_text,
        ANTHROPIC_API_KEY="anthropic-value",
    )
    values = s.secret_values()
    for expected in (
        BASE["SAMA_DATABASE_URL"],
        "p",
        BASE["SAMA_RUN_TOKEN_KEY"],
        BASE["E2B_API_KEY"],
        "anthropic-value",
        vault_text,
    ):
        assert expected in values


def test_the_vision_model_has_a_default_and_can_be_set() -> None:
    assert settings().vision_model == DEFAULT_VISION_MODEL
    assert settings(SAMA_VISION_MODEL="  ").vision_model == DEFAULT_VISION_MODEL
    assert settings(SAMA_VISION_MODEL=" claude-opus-5 ").vision_model == "claude-opus-5"
