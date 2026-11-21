"""Migration 0039: the WhatsApp and email channels (docs/architecture.md, Section 11.9)."""

from __future__ import annotations

import asyncio

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

CHECKS = (
    "ck_integrations_kind",
    "ck_session_bindings_channel",
    "ck_channel_identities_channel",
    "ck_channel_groups_channel",
    "ck_pairing_codes_channel",
    "ck_session_handoffs_from_channel",
    "ck_session_handoffs_to_channel",
    "ck_vault_secrets_name",
)


async def _definitions(url: str) -> dict[str, str]:
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint"
            " WHERE conname = ANY($1::text[])",
            list(CHECKS),
        )
    finally:
        await connection.close()
    return {row["conname"]: row["definition"] for row in rows}


def test_the_checks_accept_the_new_channels_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        definitions = asyncio.run(_definitions(url))
        assert set(definitions) == set(CHECKS)
        for name, definition in definitions.items():
            word = "sendgrid_api_key" if name == "ck_vault_secrets_name" else "whatsapp"
            assert word in definition, name
        assert "email" in definitions["ck_integrations_kind"]
        assert "whatsapp_app_secret" in definitions["ck_vault_secrets_name"]
        config = alembic_config(url)
        command.downgrade(config, "0038")
        for definition in asyncio.run(_definitions(url)).values():
            assert "whatsapp" not in definition
            assert "sendgrid" not in definition
        command.upgrade(config, "head")
        assert "whatsapp" in asyncio.run(_definitions(url))["ck_integrations_kind"]
