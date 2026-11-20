"""Code execution and generated media (docs/architecture.md, Sections 10.2.1 and 11.8; roadmap
M4.8 and M4.9).

- ``generated_media`` keeps the images of ``image_generate`` and the voice notes of
  ``text_to_speech`` until the gateway sends them to the chat. A member can read the metadata
  of the sessions that the member can see. The column ``data`` is not in the grant, so the
  Data API never gives the bytes.
- ``model_calls.endpoint`` accepts ``images`` and ``speech``, so the media calls count against
  the run, the user, and the org.
- The default profile gets the toolset ``code_exec``. ``execute_code`` gets ``ask`` by default.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The default list after the jobs migration (0026) and the same list with ``code_exec``. An
# exact match keeps the profiles that a person changed.
OLD_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron"]'
)
NEW_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron",'
    ' "code_exec"]'
)

OLD_ENDPOINTS = "endpoint IN ('messages', 'count_tokens', 'responses', 'chat_completions')"
NEW_ENDPOINTS = (
    "endpoint IN ('messages', 'count_tokens', 'responses', 'chat_completions', 'images', 'speech')"
)
KINDS = "kind IN ('image', 'audio')"
MIME_TYPES = "mime_type IN ('image/png', 'audio/ogg')"
SIZE = "size_bytes BETWEEN 1 AND 10485760 AND size_bytes = octet_length(data)"
READABLE = (
    "id, org_id, session_id, run_id, kind, mime_type, size_bytes, sha256, caption,"
    " delivered_at, created_at"
)


def upgrade() -> None:
    op.create_table(
        "generated_media",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("caption", sa.Text(), server_default="", nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(KINDS, name=op.f("ck_generated_media_kind")),
        sa.CheckConstraint(MIME_TYPES, name=op.f("ck_generated_media_mime_type")),
        sa.CheckConstraint(SIZE, name=op.f("ck_generated_media_size_bytes")),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            name=op.f("fk_generated_media_org_id_session_id_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_generated_media_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generated_media")),
    )
    op.create_index(
        op.f("ix_generated_media_org_id_run_id_created_at"),
        "generated_media",
        ["org_id", "run_id", "created_at"],
    )
    # A member reads the metadata of the sessions that the member can see, never the bytes.
    # No policy allows writes: the control plane writes as the table owner.
    op.execute("ALTER TABLE public.generated_media ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.generated_media FROM PUBLIC, anon, authenticated")
    op.execute(f"GRANT SELECT ({READABLE}) ON TABLE public.generated_media TO authenticated")
    op.execute(
        "CREATE POLICY generated_media_select ON public.generated_media FOR SELECT"
        " TO authenticated USING (sama_private.can_read_session(org_id, session_id))"
    )
    op.drop_constraint(op.f("ck_model_calls_endpoint"), "model_calls", "check")
    op.create_check_constraint(op.f("ck_model_calls_endpoint"), "model_calls", NEW_ENDPOINTS)
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{NEW_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{OLD_TOOLSETS}'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{OLD_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{NEW_TOOLSETS}'::jsonb"
    )
    # The records of the media calls stay: they are money that was spent. ``NOT VALID`` keeps
    # them and refuses new rows with these endpoints.
    op.drop_constraint(op.f("ck_model_calls_endpoint"), "model_calls", "check")
    op.execute(
        "ALTER TABLE public.model_calls ADD CONSTRAINT ck_model_calls_endpoint"
        f" CHECK ({OLD_ENDPOINTS}) NOT VALID"
    )
    op.drop_table("generated_media")
