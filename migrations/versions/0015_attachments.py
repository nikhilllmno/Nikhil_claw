"""Attachments: the images that users send in channels (docs/architecture.md, Section 11.7).

The table keeps the bytes of each image, so that the ``vision_analyze`` tool can read them
later. A member can read the metadata of the sessions that the member can see. The column
``data`` is not in the grant, so the Data API never gives the bytes.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The default list after the session search migration (0014) and the same list with ``media``.
# An exact match keeps the profiles that a person changed.
OLD_TOOLSETS = '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions"]'
NEW_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media"]'
)

MIME_TYPES = "mime_type IN ('image/png', 'image/jpeg', 'image/gif', 'image/webp')"
SIZE = "size_bytes BETWEEN 1 AND 10485760 AND size_bytes = octet_length(data)"
READABLE = "id, org_id, session_id, message_id, name, mime_type, size_bytes, sha256, created_at"


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(MIME_TYPES, name=op.f("ck_attachments_mime_type")),
        sa.CheckConstraint(SIZE, name=op.f("ck_attachments_size_bytes")),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "message_id"],
            ["messages.org_id", "messages.session_id", "messages.id"],
            name=op.f("fk_attachments_org_id_session_id_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_attachments_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attachments")),
    )
    op.create_index(
        op.f("ix_attachments_org_id_session_id_created_at"),
        "attachments",
        ["org_id", "session_id", "created_at"],
    )
    # A member reads the metadata of the sessions that the member can see, never the bytes.
    # No policy allows writes: the control plane writes as the table owner.
    op.execute("ALTER TABLE public.attachments ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.attachments FROM PUBLIC, anon, authenticated")
    op.execute(f"GRANT SELECT ({READABLE}) ON TABLE public.attachments TO authenticated")
    op.execute(
        "CREATE POLICY attachments_select ON public.attachments FOR SELECT TO authenticated"
        " USING (sama_private.can_read_session(org_id, session_id))"
    )
    # The default profile of the first org (M1.15) gets the new toolset.
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{NEW_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{OLD_TOOLSETS}'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{OLD_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{NEW_TOOLSETS}'::jsonb"
    )
    op.drop_table("attachments")
