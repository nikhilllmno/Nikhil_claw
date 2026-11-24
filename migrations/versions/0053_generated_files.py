"""Files that ``file_send`` gives to the user (docs/architecture.md, Section 11.10; roadmap M8.6).

Before M8.6 the agent could write a file in the session sandbox, but sama had no way to give it
to the user: the answer had only a sandbox path or a relative link that did not open. Since
M8.6 the tool ``file_send`` copies a file of the sandbox into ``generated_media``, and the web
app and the chat channels give it to the user as a download.

- ``kind`` can also be ``file``.
- ``mime_type`` can be one of the file types of ``sama.db.models.GENERATED_FILE_MIME_TYPES``.
  The list has ``image/png`` and ``audio/ogg``, so the old rows stay valid.
- The new column ``name`` holds the download name of a file. It is empty for the old rows.
- Members can read ``name`` with the other metadata. The column ``data`` stays out of the
  grant. The table has ``org_id`` and RLS already. No table is new.

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FILE_TYPES = (
    "text/plain",
    "text/csv",
    "text/markdown",
    "text/tab-separated-values",
    "text/calendar",
    "text/html",
    "application/json",
    "application/xml",
    "application/pdf",
    "application/zip",
    "application/gzip",
    "application/x-tar",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/octet-stream",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "audio/mpeg",
    "audio/wav",
    "audio/ogg",
    "video/mp4",
)
"""A copy of ``GENERATED_FILE_MIME_TYPES`` of M8.6, so a later change of the list does not
change this migration."""

OLD_KINDS = "kind IN ('image', 'audio')"
NEW_KINDS = "kind IN ('image', 'audio', 'file')"
OLD_MIME_TYPES = "mime_type IN ('image/png', 'audio/ogg')"
NEW_MIME_TYPES = "mime_type IN (" + ", ".join(f"'{mime}'" for mime in FILE_TYPES) + ")"


def upgrade() -> None:
    op.add_column(
        "generated_media", sa.Column("name", sa.Text(), server_default="", nullable=False)
    )
    op.drop_constraint(op.f("ck_generated_media_kind"), "generated_media", "check")
    op.create_check_constraint(op.f("ck_generated_media_kind"), "generated_media", NEW_KINDS)
    op.drop_constraint(op.f("ck_generated_media_mime_type"), "generated_media", "check")
    op.create_check_constraint(
        op.f("ck_generated_media_mime_type"), "generated_media", NEW_MIME_TYPES
    )
    op.execute("GRANT SELECT (name) ON TABLE public.generated_media TO authenticated")


def downgrade() -> None:
    # The files of ``file_send`` do not fit the old checks.
    op.execute("DELETE FROM public.generated_media WHERE kind = 'file'")
    op.execute("REVOKE SELECT (name) ON TABLE public.generated_media FROM authenticated")
    op.drop_constraint(op.f("ck_generated_media_mime_type"), "generated_media", "check")
    op.create_check_constraint(
        op.f("ck_generated_media_mime_type"), "generated_media", OLD_MIME_TYPES
    )
    op.drop_constraint(op.f("ck_generated_media_kind"), "generated_media", "check")
    op.create_check_constraint(op.f("ck_generated_media_kind"), "generated_media", OLD_KINDS)
    op.drop_column("generated_media", "name")
