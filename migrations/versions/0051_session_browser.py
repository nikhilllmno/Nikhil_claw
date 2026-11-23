"""The browser of a session (docs/architecture.md, Section 14.2.4; roadmap M8.3).

``sessions.browser_target`` holds the browser that a person chose for the session in the web
app: ``cloud`` (Browserbase) or ``my_chrome`` (the Chrome extension of the user). A browser call
without a target uses it. Null means the default target of the process.

The column is in the table ``sessions``, which has ``org_id`` and its RLS policy already. A new
policy is not needed: a member reads the column with the rest of the row of a session that the
member can see, and only the control plane writes it.

Revision ID: 0051
Revises: 0050
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("browser_target", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_sessions_browser_target"),
        "sessions",
        "browser_target IS NULL OR browser_target IN ('cloud', 'my_chrome')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_sessions_browser_target"), "sessions", type_="check")
    op.drop_column("sessions", "browser_target")
