"""Email and WhatsApp channels (docs/architecture.md, Section 11.9; roadmap M7.8).

- ``integrations.kind`` accepts ``whatsapp`` and ``email``.
- The ``channel`` columns of ``session_bindings``, ``channel_identities``, ``channel_groups``,
  ``pairing_codes``, and ``session_handoffs`` accept ``whatsapp`` and ``email``.
- The vault accepts ``whatsapp_api_token``, ``whatsapp_app_secret``, ``whatsapp_link_secret``,
  ``sendgrid_api_key``, and ``email_link_secret``.

The migration adds no table. The new channels use the tables of Section 11.5, which already
have ``org_id``, RLS, and export and delete rules.

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_CHANNELS = "('web', 'slack', 'telegram', 'extension')"
NEW_CHANNELS = "('web', 'slack', 'telegram', 'extension', 'whatsapp', 'email')"

CHANNEL_CHECKS = (
    ("session_bindings", "channel"),
    ("channel_identities", "channel"),
    ("channel_groups", "channel"),
    ("pairing_codes", "channel"),
    ("session_handoffs", "from_channel"),
    ("session_handoffs", "to_channel"),
)

OLD_KINDS = "kind IN ('telegram', 'slack')"
NEW_KINDS = "kind IN ('telegram', 'slack', 'whatsapp', 'email')"

_PATTERNS = (
    " OR name ~ '^connection:[0-9a-f-]{36}$' OR name ~ '^trigger:[0-9a-f-]{36}$'"
    " OR name ~ '^browser_login:[0-9a-f-]{36}$'"
)
OLD_VAULT_NAMES = (
    "name IN ('anthropic_api_key', 'openai_api_key', 'brave_api_key', 'telegram_bot_token',"
    " 'slack_bot_token', 'slack_signing_secret', 'composio_api_key', 'composio_webhook_secret')"
    + _PATTERNS
)
NEW_VAULT_NAMES = (
    "name IN ('anthropic_api_key', 'openai_api_key', 'brave_api_key', 'telegram_bot_token',"
    " 'slack_bot_token', 'slack_signing_secret', 'composio_api_key', 'composio_webhook_secret',"
    " 'whatsapp_api_token', 'whatsapp_app_secret', 'whatsapp_link_secret', 'sendgrid_api_key',"
    " 'email_link_secret')" + _PATTERNS
)
NEW_SECRETS = (
    "('whatsapp_api_token', 'whatsapp_app_secret', 'whatsapp_link_secret', 'sendgrid_api_key',"
    " 'email_link_secret')"
)


def _checks(channels: str, kinds: str, vault_names: str) -> None:
    for table, column in CHANNEL_CHECKS:
        name = op.f(f"ck_{table}_{column}")
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"{column} IN {channels}")
    op.drop_constraint(op.f("ck_integrations_kind"), "integrations", type_="check")
    op.create_check_constraint(op.f("ck_integrations_kind"), "integrations", kinds)
    op.drop_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", type_="check")
    op.create_check_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", vault_names)


def upgrade() -> None:
    _checks(NEW_CHANNELS, NEW_KINDS, NEW_VAULT_NAMES)


def downgrade() -> None:
    # The rows of the new channels cannot stay. A delete of an integration deletes its links
    # and pairing codes (cascade). Session bindings and handoffs of the channels go too.
    op.execute("DELETE FROM public.session_bindings WHERE channel IN ('whatsapp', 'email')")
    op.execute(
        "DELETE FROM public.session_handoffs"
        " WHERE from_channel IN ('whatsapp', 'email') OR to_channel IN ('whatsapp', 'email')"
    )
    op.execute("DELETE FROM public.integrations WHERE kind IN ('whatsapp', 'email')")
    op.execute(f"DELETE FROM public.vault_secrets WHERE name IN {NEW_SECRETS}")  # noqa: S608
    _checks(OLD_CHANNELS, OLD_KINDS, OLD_VAULT_NAMES)
