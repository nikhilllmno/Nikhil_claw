"""Org keys and regions (docs/architecture.md, Sections 16.6 and 17.3, M6.7).

``model_calls.key_source`` tells which key a call used: the key of the org (``org``) or the
key of sama (``sama``). The cost is the same for both. The rows that exist used the key of sama
or an org key from before M6.7; they get ``sama``, because the proxy did not keep the source.

``orgs.region`` must be a region id such as ``ap-south-1``. The column and its default exist
since ``0001``. The table has RLS already, and the new column and the new check change no
policy and no grant.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_calls",
        sa.Column("key_source", sa.Text(), server_default="sama", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_model_calls_key_source"), "model_calls", "key_source IN ('org', 'sama')"
    )
    op.create_check_constraint(op.f("ck_orgs_region"), "orgs", "region ~ '^[a-z]{2}-[a-z]+-[0-9]$'")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_orgs_region"), "orgs", type_="check")
    op.drop_constraint(op.f("ck_model_calls_key_source"), "model_calls", type_="check")
    op.drop_column("model_calls", "key_source")
