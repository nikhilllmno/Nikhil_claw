"""Tests of ``python -m sama.admin subagents`` (docs/architecture.md, Section 13.1)."""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.pool import NullPool

from sama.admin.__main__ import main
from sama.db import create_engine
from sama.db.models import AgentProfile
from sama.vault.crypto import VaultKey
from sama.vault.redact import default_redactor
from sama.vault.store import SecretName, Vault
from tests.orchestrator.conftest import add_tenant

SPEC = {
    "name": "researcher",
    "description": "Looks things up.",
    "prompt": "You look things up.",
    "toolsets": ["web"],
}


async def _new_profile(url: str) -> tuple[uuid.UUID, uuid.UUID]:
    engine = create_engine(url, poolclass=NullPool)
    try:
        tenant = await add_tenant(engine)
        async with engine.begin() as connection:
            await connection.execute(
                update(AgentProfile)
                .where(AgentProfile.id == tenant.ids["profile"])
                .values(runtime="claude_agent_sdk", toolsets=["web", "subagents"])
            )
        return tenant.org, tenant.ids["profile"]
    finally:
        await engine.dispose()


def test_set_from_a_file_and_show(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    org, profile = asyncio.run(_new_profile(queue_database_url))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    target = ("--org", str(org), "--profile", str(profile))
    assert main(["subagents", "show", *target]) == 0
    assert json.loads(capsys.readouterr().out)["subagents"] == []
    path = tmp_path / "subagents.json"
    path.write_text(json.dumps([SPEC]), "utf-8")
    assert main(["subagents", "set", *target, "--file", str(path)]) == 0
    out: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert out["org_id"] == str(org)
    assert [item["name"] for item in out["subagents"]] == ["researcher"]
    assert main(["subagents", "show", *target]) == 0
    assert json.loads(capsys.readouterr().out)["subagents"] == out["subagents"]


def test_set_from_stdin(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    org, profile = asyncio.run(_new_profile(queue_database_url))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([SPEC])))
    assert main(["subagents", "set", "--org", str(org), "--profile", str(profile)]) == 0
    assert json.loads(capsys.readouterr().out)["subagents"][0]["name"] == "researcher"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{not json", "error:"),
        ("{}", "must be a list"),
        (json.dumps([{**SPEC, "toolsets": ["memory"]}]), "does not have the toolset memory"),
        (json.dumps([{**SPEC, "max_turns": 500, "prompt": "secret-text"}]), "max_turns"),
    ],
)
def test_an_error_gives_exit_code_1_and_no_spec_text(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    text: str,
    message: str,
) -> None:
    org, profile = asyncio.run(_new_profile(queue_database_url))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    assert main(["subagents", "set", "--org", str(org), "--profile", str(profile)]) == 1
    captured = capsys.readouterr()
    assert message in captured.err
    assert "secret-text" not in captured.err
    assert captured.out == ""


def test_the_name_of_an_extra_field_is_not_printed(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key = "sk-ant-api03-keyname0123456789abcdef"
    org, profile = asyncio.run(_new_profile(queue_database_url))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([{**SPEC, key: 1}])))
    assert main(["subagents", "set", "--org", str(org), "--profile", str(profile)]) == 1
    captured = capsys.readouterr()
    assert "unknown field" in captured.err
    assert key not in captured.err
    assert "sk-ant" not in captured.err
    assert captured.out == ""


def test_a_missing_file_or_profile_gives_exit_code_1(
    queue_database_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    ids = ("--org", str(uuid.uuid4()), "--profile", str(uuid.uuid4()))
    assert main(["subagents", "set", *ids, "--file", str(tmp_path / "none.json")]) == 1
    assert main(["subagents", "show", *ids]) == 1


# -- opaque secrets (the redactor knows the vault and the environment) ---------------------------

OPAQUE = "signing-opaque-0123456789abcdef"  # no pattern of the redactor matches it


def _spec_with(text: str) -> str:
    return json.dumps([{**SPEC, "prompt": f"Use {text} when you call the service."}])


async def _put_secret(url: str, key: str, org: uuid.UUID, value: str) -> None:
    engine = create_engine(url, poolclass=NullPool)
    try:
        await Vault(engine, VaultKey.from_text(key)).put(org, SecretName.BRAVE_API_KEY, value)
    finally:
        await engine.dispose()
    default_redactor().forget(value)  # the CLI process must learn it from the vault


async def _stored(url: str, profile: uuid.UUID) -> Any:
    engine = create_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return (
                await connection.execute(
                    select(AgentProfile.subagents).where(AgentProfile.id == profile)
                )
            ).scalar_one()
    finally:
        await engine.dispose()


def test_an_opaque_vault_secret_in_a_spec_is_found_and_not_stored(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    org, profile = asyncio.run(_new_profile(queue_database_url))
    key = VaultKey.generate()
    asyncio.run(_put_secret(queue_database_url, key, org, OPAQUE))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    monkeypatch.setenv("SAMA_VAULT_KEY", key)
    monkeypatch.setattr("sys.stdin", io.StringIO(_spec_with(OPAQUE)))
    try:
        assert main(["subagents", "set", "--org", str(org), "--profile", str(profile)]) == 1
        captured = capsys.readouterr()
        assert "has secret" in captured.err
        assert OPAQUE not in captured.err + captured.out
        assert asyncio.run(_stored(queue_database_url, profile)) == []
    finally:
        default_redactor().forget(OPAQUE)


def test_an_opaque_secret_of_the_environment_is_found(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    org, profile = asyncio.run(_new_profile(queue_database_url))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    monkeypatch.setenv("SAMA_ALERT_SLACK_WEBHOOK_URL", OPAQUE)
    monkeypatch.delenv("SAMA_VAULT_KEY", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(_spec_with(OPAQUE)))
    try:
        assert main(["subagents", "set", "--org", str(org), "--profile", str(profile)]) == 1
        assert "has secret" in capsys.readouterr().err
    finally:
        default_redactor().forget(OPAQUE)


def test_the_command_stops_when_the_vault_cannot_be_read(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    org, profile = asyncio.run(_new_profile(queue_database_url))
    key = VaultKey.generate()
    asyncio.run(_put_secret(queue_database_url, key, org, OPAQUE))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([SPEC])))
    argv = ["subagents", "set", "--org", str(org), "--profile", str(profile)]
    try:
        # No key, but the org has a vault entry.
        monkeypatch.delenv("SAMA_VAULT_KEY", raising=False)
        with pytest.raises(SystemExit, match="SAMA_VAULT_KEY"):
            main(argv)
        # Another key: the entry does not decrypt.
        monkeypatch.setenv("SAMA_VAULT_KEY", VaultKey.generate())
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([SPEC])))
        with pytest.raises(SystemExit, match="vault cannot be read"):
            main(argv)
        # The database cannot be reached.
        monkeypatch.setenv("SAMA_VAULT_KEY", key)
        monkeypatch.setenv("SAMA_DATABASE_URL", "postgresql://u:pw-not-shown@127.0.0.1:1/db")
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([SPEC])))
        with pytest.raises(SystemExit, match="vault cannot be read") as caught:
            main(argv)
        assert "pw-not-shown" not in str(caught.value)
    finally:
        default_redactor().forget(OPAQUE)
        monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    assert asyncio.run(_stored(queue_database_url, profile)) == []
