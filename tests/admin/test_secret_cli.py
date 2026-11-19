"""Tests of ``python -m sama.admin secret`` (docs/architecture.md, Section 16.3)."""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.pool import NullPool

from sama.admin.__main__ import main
from sama.db import create_engine
from sama.vault.crypto import VaultKey
from sama.vault.redact import default_redactor
from sama.vault.store import SecretName, Vault
from tests.orchestrator.conftest import add_tenant

ORG = "00000000-0000-0000-0000-000000000001"


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    assert main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


async def _new_org(url: str) -> uuid.UUID:
    engine = create_engine(url, poolclass=NullPool)
    try:
        return (await add_tenant(engine)).org
    finally:
        await engine.dispose()


async def _read(url: str, key: str, org: uuid.UUID) -> SecretStr | None:
    engine = create_engine(url, poolclass=NullPool)
    try:
        return await Vault(engine, VaultKey.from_text(key)).get(org, SecretName.BRAVE_API_KEY)
    finally:
        await engine.dispose()


def test_set_list_and_delete(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    org_id = asyncio.run(_new_org(queue_database_url))
    key = VaultKey.generate()
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    monkeypatch.setenv("SAMA_VAULT_KEY", key)
    monkeypatch.setattr("sys.stdin", io.StringIO("brave-cli-secret\n"))
    org = str(org_id)
    name = ("--name", "brave_api_key")
    try:
        out = _run(capsys, "secret", "set", "--org", org, *name)
        assert out == {"org_id": org, "name": "brave_api_key", "set": True}
        assert _run(capsys, "secret", "list", "--org", org) == {
            "org_id": org,
            "names": ["brave_api_key"],
        }
        value = asyncio.run(_read(queue_database_url, key, org_id))
        assert value is not None
        assert value.get_secret_value() == "brave-cli-secret"
        assert _run(capsys, "secret", "delete", "--org", org, *name)["deleted"] is True
        assert _run(capsys, "secret", "delete", "--org", org, *name)["deleted"] is False
    finally:
        default_redactor().forget("brave-cli-secret")


def test_the_command_needs_a_name_and_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        main(["secret", "set", "--org", ORG])
    monkeypatch.setenv("SAMA_DATABASE_URL", "postgresql://u:p@localhost:1/db")
    monkeypatch.delenv("SAMA_VAULT_KEY", raising=False)
    with pytest.raises(SystemExit, match="SAMA_VAULT_KEY"):
        main(["secret", "list", "--org", ORG])
    monkeypatch.setenv("SAMA_VAULT_KEY", "c2hvcnQ=")
    with pytest.raises(SystemExit, match="SAMA_VAULT_KEY"):
        main(["secret", "list", "--org", ORG])
