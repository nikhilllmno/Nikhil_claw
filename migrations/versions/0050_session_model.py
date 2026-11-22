"""The model of a session (docs/architecture.md, Section 5.4; roadmap M8.2).

``sessions.model_override`` holds the model that a person chose for the session. The assembler
puts it in the config of each run. The frozen ``run_config`` does not change (decision D9).
Null means that the session uses the model of its frozen config.

The column is in the table ``sessions``, which has ``org_id`` and its RLS policy already. A new
policy is not needed: a member reads the column with the rest of the row of a session that the
member can see, and only the control plane writes it.

Revision ID: 0050
Revises: 0047
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
# 0048 and 0049 are in other branches. Change this when they are on main.
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("model_override", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_sessions_model_override"),
        "sessions",
        "model_override IS NULL OR char_length(model_override) BETWEEN 1 AND 100",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_sessions_model_override"), "sessions", type_="check")
    op.drop_column("sessions", "model_override")
