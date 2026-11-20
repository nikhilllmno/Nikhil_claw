"""Group toolsets: the toolsets of a shared session (docs/architecture.md, Sections 10.2 and
11.3).

A chat run in a shared session (a Telegram group or a Slack channel) uses the column
``group_toolsets`` of its agent profile, and not ``toolsets``. The upgrade fills the column for
the profiles that exist. A new column with ``[]`` would remove each tool from the groups of
these profiles. Each profile gets the default group list, cut to the toolsets that the profile
has, so a group never has a toolset that the profile does not have. A new profile gets the
default list from the bootstrap code. The column default is ``[]``, which fails closed.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The same list as ``DEFAULT_GROUP_TOOLSETS`` in ``sama.admin.bootstrap``. A migration keeps
# its own copy, because the code can change later.
DEFAULT_GROUP_TOOLSETS = '["todo", "web", "clarify", "memory", "sessions", "media"]'


def upgrade() -> None:
    op.add_column(
        "agent_profiles",
        sa.Column(
            "group_toolsets",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE agent_profiles SET group_toolsets = COALESCE((SELECT jsonb_agg(d.item"
            " ORDER BY d.pos) FROM jsonb_array_elements_text(CAST(:defaults AS jsonb))"
            " WITH ORDINALITY AS d(item, pos) WHERE agent_profiles.toolsets @> to_jsonb(d.item)),"
            " '[]'::jsonb)"
        ).bindparams(defaults=DEFAULT_GROUP_TOOLSETS)
    )


def downgrade() -> None:
    op.drop_column("agent_profiles", "group_toolsets")
