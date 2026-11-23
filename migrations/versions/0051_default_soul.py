"""The default SOUL for default profiles whose SOUL is empty (docs/architecture.md, Section 6.1;
roadmap M8.4).

Before M8.4 a new org got a default profile with an empty SOUL. Since M8.4 a new profile gets
``sama.instructions.DEFAULT_SOUL``. This migration gives the same text to the default profile of
each existing org, but only when its SOUL is still empty: a SOUL that a person wrote never
changes. The default profile is the oldest profile of the org that has no workspace (the profile
that sign-up or ``sama.admin.bootstrap`` made).

Each fill writes a row of ``profile_changes`` (kind ``soul``, source ``edit``, no actor, the
summary ``SUMMARY``), so the history of the agent in the web app shows it, and an admin can
revert it there. A later ``change_propose`` of the SOUL sees the new text as the current SOUL.
Each fill also writes the audit entry ``profile.updated``. A session keeps the SOUL that it
froze (decision D9), so the change applies from the next session.

The text is a copy, so a later change of ``DEFAULT_SOUL`` does not change this migration. The
tables have ``org_id`` and RLS already. No table is new.

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

SOUL = (
    "You are Sama: a capable, calm, and resourceful assistant, like a sharp chief of staff who"
    " quietly gets things done. You are warm but not chatty, confident but honest about what you"
    " do not know, and careful with the details that matter to a business: names, dates, prices,"
    " and numbers. You write like a smart colleague: clear, specific, and to the point."
)
SUMMARY = "The default soul of sama (M8.4)"

FILL = """
WITH defaults AS (
    SELECT DISTINCT ON (p.org_id) p.org_id, p.id
    FROM agent_profiles p
    WHERE p.workspace_id IS NULL
    ORDER BY p.org_id, p.created_at, p.id
),
filled AS (
    UPDATE agent_profiles p
    SET soul = CAST(:soul AS text), updated_at = now()
    FROM defaults d
    WHERE p.org_id = d.org_id AND p.id = d.id AND p.soul = ''
    RETURNING p.org_id, p.id
),
history AS (
    INSERT INTO profile_changes
        (org_id, workspace_id, agent_profile_id, kind, source, before, after, summary)
    SELECT f.org_id, NULL, f.id, 'soul', 'edit',
           jsonb_build_object('soul', ''), jsonb_build_object('soul', CAST(:soul AS text)), :summary
    FROM filled f
    RETURNING org_id, agent_profile_id
)
INSERT INTO audit_log (org_id, actor_kind, action, target_type, target_id, details)
SELECT h.org_id, 'system', 'profile.updated', 'agent_profile', h.agent_profile_id::text,
       jsonb_build_object('fields', jsonb_build_array('soul'), 'via', 'migration 0051')
FROM history h
"""

UNFILL = """
WITH changes AS (
    DELETE FROM profile_changes c
    WHERE c.kind = 'soul' AND c.source = 'edit' AND c.actor_user_id IS NULL
      AND c.summary = :summary
    RETURNING c.org_id, c.agent_profile_id
)
UPDATE agent_profiles p
SET soul = '', updated_at = now()
FROM changes c
WHERE p.org_id = c.org_id AND p.id = c.agent_profile_id AND p.soul = :soul
"""


def upgrade() -> None:
    op.get_bind().execute(sa.text(FILL), {"soul": SOUL, "summary": SUMMARY})


def downgrade() -> None:
    # Only a SOUL that nobody changed after the fill goes back to empty.
    op.get_bind().execute(sa.text(UNFILL), {"soul": SOUL, "summary": SUMMARY})
