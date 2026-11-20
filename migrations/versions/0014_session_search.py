"""Session search: a full-text index and embeddings of messages (docs/architecture.md,
Section 7.1).

The migration needs the ``vector`` extension (pgvector). Supabase offers it. A plain Postgres
needs a pgvector image, for example ``pgvector/pgvector:pg17``.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The default list after the skills migration (0013) and the same list with ``sessions``. An
# exact match keeps the profiles that a person changed.
OLD_TOOLSETS = '["core", "todo", "web", "clarify", "github", "memory", "skills"]'
NEW_TOOLSETS = '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions"]'

SEARCH_VECTOR = "to_tsvector('simple', left(coalesce(content->>'text', ''), 100000))"
EMBEDDING_DIMENSIONS = 1536


class Vector(sa.types.UserDefinedType):  # type: ignore[type-arg]
    """The ``vector`` type of pgvector, only to create the column."""

    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return f"vector({EMBEDDING_DIMENSIONS})"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        "ALTER TABLE public.messages ADD COLUMN search_vector tsvector"
        f" GENERATED ALWAYS AS ({SEARCH_VECTOR}) STORED"
    )
    op.add_column("messages", sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint(
        op.f("uq_messages_org_id_session_id_id"), "messages", ["org_id", "session_id", "id"]
    )
    op.create_index(
        "ix_messages_search_vector", "messages", ["search_vector"], postgresql_using="gin"
    )
    op.create_index(
        "ix_messages_embed_pending",
        "messages",
        ["created_at"],
        postgresql_where=sa.text("embedded_at IS NULL"),
    )
    op.create_table(
        "message_embeddings",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "message_id"],
            ["messages.org_id", "messages.session_id", "messages.id"],
            name=op.f("fk_message_embeddings_org_id_session_id_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_message_embeddings_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_embeddings")),
        sa.UniqueConstraint(
            "org_id", "message_id", name=op.f("uq_message_embeddings_org_id_message_id")
        ),
    )
    op.create_index(
        op.f("ix_message_embeddings_org_id_session_id"),
        "message_embeddings",
        ["org_id", "session_id"],
    )
    op.execute(
        "CREATE INDEX ix_message_embeddings_embedding ON public.message_embeddings"
        " USING hnsw (embedding vector_cosine_ops)"
    )
    # A member reads the embeddings of the sessions that the member can see. No policy allows
    # writes: the control plane writes as the table owner.
    op.execute("ALTER TABLE public.message_embeddings ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.message_embeddings FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.message_embeddings TO authenticated")
    op.execute(
        "CREATE POLICY message_embeddings_select ON public.message_embeddings"
        " FOR SELECT TO authenticated"
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
    op.drop_table("message_embeddings")
    op.drop_index("ix_messages_embed_pending", table_name="messages")
    op.drop_index("ix_messages_search_vector", table_name="messages")
    op.drop_constraint(op.f("uq_messages_org_id_session_id_id"), "messages", type_="unique")
    op.drop_column("messages", "embedded_at")
    op.drop_column("messages", "search_vector")
