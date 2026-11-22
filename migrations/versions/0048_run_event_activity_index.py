"""An index for the tool trail of a session (docs/architecture.md, Section 17.8; roadmap M8.2).

``GET /sessions/{id}/activity`` reads the newest ``tool_call_started``, ``tool_call_finished``,
and ``subagent_event`` events of a session. A session has many more ``text_delta`` events than
tool events, so the read uses a partial index on the tool events, ordered by ``id``.

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_run_events_activity"
WHERE = "type IN ('tool_call_started', 'tool_call_finished', 'subagent_event')"


def upgrade() -> None:
    op.create_index(
        INDEX,
        "run_events",
        ["org_id", "session_id", "id"],
        postgresql_where=sa.text(WHERE),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="run_events")
