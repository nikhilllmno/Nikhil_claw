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
        ({"SAMA_SIGNUP": "maybe"}, "SAMA_SIGNUP must be open or invite_only"),
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


EXTENSION = "abcdefghijklmnopabcdefghijklmnop"


def test_the_extension_ids_are_read() -> None:
    assert settings().extension_ids == ()
    s = settings(SAMA_EXTENSION_IDS=f" {EXTENSION},,{'p' * 32},{EXTENSION} ")
    assert s.extension_ids == (EXTENSION, "p" * 32)


@pytest.mark.parametrize("value", ["*", "chrome-extension://" + EXTENSION, "q" * 32, "a" * 31])
def test_an_extension_id_that_is_not_valid_is_refused(value: str) -> None:
    with pytest.raises(SettingsError, match="SAMA_EXTENSION_IDS"):
        settings(SAMA_EXTENSION_IDS=value)


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


def test_the_browser_task_model_has_a_default_and_needs_a_price() -> None:
    assert settings().browser_task_model == DEFAULT_VISION_MODEL
    model = settings(SAMA_BROWSER_TASK_MODEL=" claude-haiku-5-5 ").browser_task_model
    assert model == "claude-haiku-5-5"
    for bad in ("gpt-6-luna", "claude-unknown"):
        with pytest.raises(SettingsError, match="SAMA_BROWSER_TASK_MODEL"):
            settings(SAMA_BROWSER_TASK_MODEL=bad)


GITHUB = {
    "SAMA_GITHUB_APP_ID": "123456",
    "SAMA_GITHUB_APP_SLUG": "sama-test",
    "SAMA_GITHUB_CLIENT_ID": "Iv1.client",
    "SAMA_GITHUB_APP_PRIVATE_KEY": (
        # Two parts, so the secret scan does not see a key. The value is not a key.
        "-----BEGIN RSA PRIVATE " + "KEY-----\\nAAAA\\n-----END RSA PRIVATE KEY-----"
    ),
    "SAMA_GITHUB_CLIENT_SECRET": "client-secret-value",
}


def test_the_github_app_is_off_without_an_app_id() -> None:
    s = settings()
    assert s.github is None
    assert s.git_url == "https://sama-staging.fly.dev/git"


def test_the_github_app_is_read_with_the_escaped_line_breaks() -> None:
    s = settings(**GITHUB)
    assert s.github is not None
    assert (s.github.app_id, s.github.slug, s.github.client_id) == (
        "123456",
        "sama-test",
        "Iv1.client",
    )
    key = s.github.private_key.get_secret_value()
    assert key.startswith("-----BEGIN RSA PRIVATE KEY-----\nAAAA\n")
    assert key.endswith("-----END RSA PRIVATE KEY-----\n")
    assert key in s.secret_values()
    assert "client-secret-value" in s.secret_values()
    assert "client-secret-value" not in repr(s)
    assert "AAAA" not in repr(s)


@pytest.mark.parametrize(
    "name",
    [
        "SAMA_GITHUB_APP_SLUG",
        "SAMA_GITHUB_CLIENT_ID",
        "SAMA_GITHUB_APP_PRIVATE_KEY",
        "SAMA_GITHUB_CLIENT_SECRET",
    ],
)
def test_a_partial_github_app_is_refused(name: str) -> None:
    with pytest.raises(SettingsError, match=name):
        settings(**{**GITHUB, name: None})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"SAMA_GITHUB_APP_ID": "abc"}, "SAMA_GITHUB_APP_ID must be a number"),
        ({"SAMA_GITHUB_APP_PRIVATE_KEY": "not-a-key"}, "must be a PEM private key"),
    ],
)
def test_a_bad_github_value_is_refused_without_its_value(
    changes: dict[str, str], message: str
) -> None:
    with pytest.raises(SettingsError, match=message) as error:
        settings(**{**GITHUB, **changes})
    assert "not-a-key" not in str(error.value)


def test_the_self_repo_has_a_default_and_can_be_set() -> None:
    assert settings().self_repo == "nikhilllmno/Nikhil_claw"
    assert settings(SAMA_SELF_REPO=" acme/sama ").self_repo == "acme/sama"
    assert settings(SAMA_SELF_REPO="https://github.com/acme/sama.git").self_repo == "acme/sama"


@pytest.mark.parametrize("value", ["sama", "a/b/c", "-x/y", "acme/.."])
def test_a_bad_self_repo_is_refused(value: str) -> None:
    with pytest.raises(SettingsError, match="SAMA_SELF_REPO must be <owner>/<name>"):
        settings(SAMA_SELF_REPO=value)


def test_sign_up_is_closed_unless_it_is_opened() -> None:
    assert settings().signup.mode == "invite_only"
    opened = settings(SAMA_SIGNUP=" Open ", SAMA_SIGNUP_BLOCKED_DOMAINS="@Spam.test, ,x.test")
    assert opened.signup.mode == "open"
    assert opened.signup.blocked_domains == ("spam.test", "x.test")


def test_the_region_has_a_default_and_can_be_set() -> None:
    s = settings()
    assert (s.regions.home, s.regions.known()) == ("ap-south-1", ["ap-south-1"])
    assert s.regions.url("ap-south-1") == "https://sama-staging.fly.dev"
    s = settings(
        SAMA_REGION=" eu-central-1 ",
        SAMA_WEB_ORIGINS="https://eu.sama-web.test",
        SAMA_REGION_URLS="ap-south-1=https://sama-web.test/",
    )
    assert s.regions.home == "eu-central-1"
    assert s.regions.known() == ["ap-south-1", "eu-central-1"]
    assert s.regions.url("eu-central-1") == "https://eu.sama-web.test"
    assert s.regions.url("ap-south-1") == "https://sama-web.test"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"SAMA_REGION": "Mumbai"}, "SAMA_REGION: 'Mumbai' is not a region id"),
        ({"SAMA_REGION_URLS": "eu-central-1"}, "region=url pairs"),
        ({"SAMA_REGION_URLS": "ap-south-1=https://other.test"}, "not SAMA_PUBLIC_URL"),
    ],
)
def test_a_bad_region_is_refused(changes: dict[str, str], message: str) -> None:
    with pytest.raises(SettingsError, match=message):
        settings(**changes)


def test_billing_needs_the_key_and_the_webhook_secret() -> None:
    assert settings().stripe_key() is None
    assert settings(SAMA_STRIPE_SECRET_KEY="sk_test_a").stripe_key() is None
    assert settings(SAMA_STRIPE_WEBHOOK_SECRET="whsec_a").stripe_key() is None
    both = settings(SAMA_STRIPE_SECRET_KEY="sk_test_a", SAMA_STRIPE_WEBHOOK_SECRET="whsec_a")
    assert both.stripe_key() == "sk_test_a"
    assert "sk_test_a" in both.secret_values()
    assert "whsec_a" in both.secret_values()
    assert "sk_test_a" not in repr(both)


def test_a_live_stripe_key_turns_billing_off_unless_it_is_allowed() -> None:
    live = {"SAMA_STRIPE_SECRET_KEY": "sk_live_a", "SAMA_STRIPE_WEBHOOK_SECRET": "whsec_a"}
    assert settings(**live).stripe_key() is None
    assert settings(**live, SAMA_STRIPE_ALLOW_LIVE="true").stripe_key() == "sk_live_a"
    restricted = {**live, "SAMA_STRIPE_SECRET_KEY": "rk_live_a"}
    assert settings(**restricted).stripe_key() is None
