"""SQLAlchemy models of the sama tables (docs/architecture.md, Section 17.1).

The Alembic migrations own the schema. These models must match it: a test compares them with
the migrated database. Every table has ``org_id``. Each child points to its parent with a
composite foreign key on ``(org_id, parent_id)``, so a row cannot link to a row of another org.
A row that names a run and a session points to the run with ``(org_id, session_id, run_id)``,
so the run always belongs to that session. A binding does the same with its workspace.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

NAMING_CONVENTION: Final = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

ROLES: Final = ("owner", "admin", "member")
RUNTIMES: Final = ("claude_agent_sdk", "openai_agents_sdk", "fake")
AUDIENCES: Final = ("private", "shared")
SESSION_STATUSES: Final = ("active", "archived")
LINEAGES: Final = ("compaction", "branch", "retry", "delegation")
MESSAGE_ROLES: Final = ("user", "assistant", "system", "tool")
RUN_STATUSES: Final = ("queued", "running", "paused", "completed", "failed", "cancelled")
ENDED_RUN_STATUSES: Final = ("completed", "failed", "cancelled")
RESUME_DECISIONS: Final = ("approve", "reject")
TOOL_DECISIONS: Final = ("allow", "ask", "deny")
TOOL_STATUSES: Final = ("started", "finished", "failed", "denied")
APPROVAL_STATUSES: Final = ("pending", "approved", "rejected", "expired")
CHANNELS: Final = ("web", "slack", "telegram", "extension")
ACTOR_KINDS: Final = ("user", "agent", "system")
PROVIDERS: Final = ("anthropic", "openai")
MODEL_ENDPOINTS: Final = ("messages", "count_tokens", "responses", "chat_completions")
VAULT_NAMES: Final = (
    "anthropic_api_key",
    "openai_api_key",
    "brave_api_key",
    "telegram_bot_token",
    "slack_bot_token",
    "slack_signing_secret",
)
INTEGRATION_KINDS: Final = ("telegram", "slack")
"""The chat apps that an org can connect (docs/architecture.md, Section 11.6)."""
INTEGRATION_STATUSES: Final = ("pending", "active", "failed")
SKILL_SCOPES: Final = ("org", "workspace", "user")
"""The stored scopes of a skill. The scope ``builtin`` is in the repo, not in the database."""
SKILL_STATUSES: Final = ("active", "archived")
SKILL_CHANGES: Final = ("create", "patch", "edit", "write_file", "remove_file", "archive")
SKILL_AUTHOR_KINDS: Final = ("user", "agent", "system")
EMBEDDING_DIMENSIONS: Final = 1536
"""The size of a message embedding: the output of ``text-embedding-3-small``
(docs/architecture.md, Section 7.1)."""
SEARCH_VECTOR_CHARS: Final = 100_000
"""The full-text index covers the first characters of a message. A ``tsvector`` has a limit of
1 MB, so the index must not take an unlimited text."""


def _one_of(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


def _org_id() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _json(default: str = "'{}'::jsonb") -> Mapped[dict[str, Any]]:
    return mapped_column(JSONB, nullable=False, server_default=text(default))


class Vector(UserDefinedType[list[float]]):
    """The ``vector`` type of the pgvector extension. The value in Python is a list of floats.
    The driver gets the text form, ``[1,2,3]``, so sama needs no extra package."""

    cache_ok = True

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **kw: Any) -> str:
        return f"vector({self.dimensions})"

    def bind_processor(self, dialect: Any) -> Any:
        def process(value: list[float] | None) -> str | None:
            return None if value is None else "[" + ",".join(map(repr, value)) + "]"

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: str | None) -> list[float] | None:
            return None if value is None else [float(x) for x in value.strip("[]").split(",")]

        return process


ischema_names["vector"] = Vector  # Reflection: the migration test compares the schema.


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default="team")
    region: Mapped[str] = mapped_column(Text, nullable=False, server_default="ap-south-1")
    limits: Mapped[dict[str, Any]] = _json()
    created_at: Mapped[datetime] = _created_at()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("org_id", "auth_user_id"),
        Index(None, "auth_user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    auth_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    email: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("org_id", "id"), UniqueConstraint("org_id", "slug"))

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        UniqueConstraint("user_id", "workspace_id", postgresql_nulls_not_distinct=True),
        CheckConstraint(_one_of("role", ROLES), name="role"),
        Index(None, "org_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class AgentProfile(Base):
    __tablename__ = "agent_profiles"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        CheckConstraint(_one_of("runtime", RUNTIMES), name="runtime"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    soul: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    runtime: Mapped[str] = mapped_column(Text, nullable=False, server_default="claude_agent_sdk")
    model: Mapped[str] = mapped_column(Text, nullable=False)
    toolsets: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    skills: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    policy: Mapped[dict[str, Any]] = _json()
    budget: Mapped[dict[str, Any]] = _json()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        # Lets a binding prove that its workspace is the workspace of its session.
        UniqueConstraint("org_id", "workspace_id", "id"),
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"], ["agent_profiles.org_id", "agent_profiles.id"]
        ),
        ForeignKeyConstraint(["org_id", "owner_user_id"], ["users.org_id", "users.id"]),
        ForeignKeyConstraint(["org_id", "parent_session_id"], ["sessions.org_id", "sessions.id"]),
        CheckConstraint(_one_of("audience", AUDIENCES), name="audience"),
        CheckConstraint(
            "audience <> 'private' OR owner_user_id IS NOT NULL", name="private_has_owner"
        ),
        CheckConstraint(_one_of("status", SESSION_STATUSES), name="status"),
        CheckConstraint(f"lineage IS NULL OR {_one_of('lineage', LINEAGES)}", name="lineage"),
        CheckConstraint(
            "(parent_session_id IS NULL) = (lineage IS NULL)", name="lineage_has_parent"
        ),
        CheckConstraint(
            "(run_config IS NULL) = (config_fingerprint IS NULL)", name="config_has_fingerprint"
        ),
        Index(None, "org_id", "workspace_id"),
        Index(None, "owner_user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    audience: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    parent_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lineage: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    sandbox_id: Mapped[str | None] = mapped_column(Text)
    """The provider id of the session sandbox (docs/architecture.md, Section 16.2)."""
    run_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """The frozen ``RunConfig`` of the session, set by the first run (Section 6.1)."""
    config_fingerprint: Mapped[str | None] = mapped_column(Text)
    """``RunConfig.fingerprint()`` of ``run_config``."""
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        # Lets a child row prove that its run belongs to its session.
        UniqueConstraint("org_id", "session_id", "id"),
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"], ["agent_profiles.org_id", "agent_profiles.id"]
        ),
        ForeignKeyConstraint(["org_id", "triggered_by_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint(_one_of("status", RUN_STATUSES), name="status"),
        CheckConstraint(_one_of("runtime", RUNTIMES), name="runtime"),
        CheckConstraint(
            f"resume_decision IS NULL OR {_one_of('resume_decision', RESUME_DECISIONS)}",
            name="resume_decision",
        ),
        CheckConstraint(
            "(status = 'running') = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease",
        ),
        CheckConstraint(
            "status <> 'paused' OR pending_approval_id IS NOT NULL", name="paused_has_approval"
        ),
        Index(None, "org_id", "session_id"),
        Index(None, "status", "created_at"),
        # The per-org message rate and concurrency limit (migration 0007, Section 4.3).
        Index(None, "org_id", "created_at"),
        Index(
            "ix_runs_running_per_org",
            "org_id",
            postgresql_where=text("status = 'running'"),
        ),
        # One active run per session (docs/architecture.md, Section 4.3).
        Index(
            "ix_runs_one_active_per_session",
            "session_id",
            unique=True,
            postgresql_where=text("status IN ('running', 'paused')"),
        ),
        # The runs that wait for the trace export (migration 0009, Section 18.2).
        Index(
            "ix_runs_trace_pending",
            "finished_at",
            postgresql_where=text(
                "trace_exported_at IS NULL AND status IN ('completed', 'failed', 'cancelled')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    runtime: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str | None] = mapped_column(Text)
    budget: Mapped[dict[str, Any]] = _json()
    usage: Mapped[dict[str, Any]] = _json()
    runtime_state: Mapped[str | None] = mapped_column(Text)
    unattended: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    """True for a run that no human watches, for example a cron job or a webhook. The tool
    gate turns ``ask`` into ``deny`` in these runs (docs/architecture.md, Section 16.1)."""
    failure_code: Mapped[str | None] = mapped_column(Text)
    failure_message: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(Text)
    """The worker that runs the run. Set only while the status is ``running``."""
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """How many times a worker claimed the run."""
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the worker gave this attempt to the runtime. A reap starts the run again only if
    this is null."""
    pending_approval_id: Mapped[str | None] = mapped_column(Text)
    """The approval that a paused run waits for. It is the ``call_id`` of the approval row."""
    resume_decision: Mapped[str | None] = mapped_column(Text)
    """Set when the approval is decided. The next claim resumes the run with it."""
    resume_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the control plane sent the trace of the ended run to Langfuse (Section 18.2)."""


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"], ["runs.org_id", "runs.session_id", "runs.id"]
        ),
        ForeignKeyConstraint(["org_id", "author_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint(_one_of("role", MESSAGE_ROLES), name="role"),
        # Lets a message embedding prove that its message belongs to its session.
        UniqueConstraint("org_id", "session_id", "id"),
        Index(None, "session_id", "seq"),
        # Session search (migration 0014, Section 7.1).
        Index("ix_messages_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_messages_embed_pending",
            "created_at",
            postgresql_where=text("embedded_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    content: Mapped[dict[str, Any]] = _json()
    created_at: Mapped[datetime] = _created_at()
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            f"to_tsvector('simple', left(coalesce(content->>'text', ''), {SEARCH_VECTOR_CHARS}))",
            persisted=True,
        ),
        deferred=True,
    )
    """The full-text index of the message text. The ``simple`` configuration keeps words as
    they are, so it works for every language."""
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the embed worker handled the message. Null: not yet. The worker also sets it for a
    message that it skips, for example an empty one (Section 7.1)."""


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("run_id", "seq"),
        Index(None, "org_id", "session_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = _json()
    created_at: Mapped[datetime] = _created_at()


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("run_id", "call_id"),
        CheckConstraint(
            f"decision IS NULL OR {_one_of('decision', TOOL_DECISIONS)}", name="decision"
        ),
        CheckConstraint(_one_of("status", TOOL_STATUSES), name="status"),
        Index(None, "org_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    call_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    arguments: Mapped[dict[str, Any]] = _json()
    # The digest of the full arguments. The gate binds a call id to one tool and one digest.
    arguments_digest: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="started")
    result_preview: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionBinding(Base):
    __tablename__ = "session_bindings"
    __table_args__ = (
        # One key: the workspace of a binding is always the workspace of its session.
        ForeignKeyConstraint(
            ["org_id", "workspace_id", "session_id"],
            ["sessions.org_id", "sessions.workspace_id", "sessions.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        UniqueConstraint("channel", "external_key"),
        CheckConstraint(_one_of("channel", CHANNELS), name="channel"),
        Index(None, "org_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    external_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class AuditEntry(Base):
    __tablename__ = "audit_log"
    # Only org_id is a foreign key: an audit entry stays when its run, user, or workspace goes.
    __table_args__ = (
        CheckConstraint(_one_of("actor_kind", ACTOR_KINDS), name="actor_kind"),
        Index(None, "org_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    details: Mapped[dict[str, Any]] = _json()
    created_at: Mapped[datetime] = _created_at()


class ModelCall(Base):
    """One call through the model proxy (docs/architecture.md, Section 16.6)."""

    __tablename__ = "model_calls"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id", "session_id"],
            ["sessions.org_id", "sessions.workspace_id", "sessions.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["org_id", "user_id"], ["users.org_id", "users.id"]),
        CheckConstraint(_one_of("provider", PROVIDERS), name="provider"),
        CheckConstraint(_one_of("endpoint", MODEL_ENDPOINTS), name="endpoint"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0"
            " AND cache_write_tokens >= 0 AND cache_write_long_tokens >= 0"
            " AND web_search_requests >= 0 AND cost_usd >= 0 AND duration_ms >= 0",
            name="counts",
        ),
        Index(None, "org_id", "run_id"),
        Index(None, "org_id", "created_at"),
        Index(None, "org_id", "user_id", "created_at"),
        Index(None, "org_id", "session_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_write_long_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    web_search_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    usage_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class SessionTodo(Base):
    """The task list of a session, written by the ``todo`` tool (docs/architecture.md,
    Section 16.7)."""

    __tablename__ = "session_todos"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id", "session_id"],
            ["sessions.org_id", "sessions.workspace_id", "sessions.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("jsonb_typeof(items) = 'array'", name="items_array"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    items: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The run of the last change. Not a foreign key: the list outlives a deleted run."""
    updated_at: Mapped[datetime] = _created_at()


class Approval(Base):
    """A human decision for one tool call that the tool gate sent to ``ask``
    (docs/architecture.md, Section 16.1). ``call_id`` is the approval id of the run."""

    __tablename__ = "approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["org_id", "decided_by_user_id"], ["users.org_id", "users.id"]),
        UniqueConstraint("run_id", "call_id"),
        CheckConstraint(_one_of("status", APPROVAL_STATUSES), name="status"),
        CheckConstraint("(status = 'pending') = (decided_at IS NULL)", name="decided_at"),
        Index(None, "org_id", "session_id"),
        Index(None, "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    call_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    arguments: Mapped[dict[str, Any]] = _json()
    arguments_digest: Mapped[str] = mapped_column(Text, nullable=False)
    """The digest of the full arguments. ``arguments`` can be cut, so the gate compares this."""
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolPolicy(Base):
    """One policy rule of the tool gate (docs/architecture.md, Section 16.1, step 4).

    No profile and no user: an org rule. A profile: a rule for that agent profile. A user: a
    rule of that user ("always allow this tool for me"), for one profile or for all.
    """

    __tablename__ = "tool_policies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        UniqueConstraint(
            "org_id", "agent_profile_id", "user_id", "tool", postgresql_nulls_not_distinct=True
        ),
        CheckConstraint(_one_of("action", TOOL_DECISIONS), name="action"),
        CheckConstraint("length(tool) BETWEEN 1 AND 200", name="tool_length"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    agent_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    """An exact tool name, or a prefix that ends with ``*``."""
    action: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


CONTEXT_FILE_STORED_MAX_CHARS: Final = 100_000
"""Cap for a stored context file. The prompt gets at most 20,000 characters of it."""


class StoredContextFile(Base):
    """A context file for the prompt of new sessions (docs/architecture.md, Section 6.1).

    No workspace: the file is for the whole org. No profile: the file is for all profiles.
    """

    __tablename__ = "context_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "org_id", "workspace_id", "agent_profile_id", "path", postgresql_nulls_not_distinct=True
        ),
        CheckConstraint(
            "length(path) BETWEEN 1 AND 200 AND path !~ '[\"<>\\r\\n]'", name="path_text"
        ),
        CheckConstraint(
            f"length(content) <= {CONTEXT_FILE_STORED_MAX_CHARS}", name="content_length"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    path: Mapped[str] = mapped_column(Text, nullable=False)
    """The name in the prompt, for example ``AGENTS.md``."""
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class Skill(Base):
    """A skill of the org, of a workspace, or of a user (docs/architecture.md, Section 9.2).

    The row holds the name and the status. The text and the files are in ``skill_versions``.
    A skill is never deleted: ``archive`` sets the status.
    """

    __tablename__ = "skills"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        UniqueConstraint("org_id", "id"),
        UniqueConstraint(
            "org_id", "scope", "workspace_id", "user_id", "name", postgresql_nulls_not_distinct=True
        ),
        CheckConstraint(_one_of("scope", SKILL_SCOPES), name="scope"),
        CheckConstraint(_one_of("status", SKILL_STATUSES), name="status"),
        CheckConstraint(
            "(scope = 'org' AND workspace_id IS NULL AND user_id IS NULL)"
            " OR (scope = 'workspace' AND workspace_id IS NOT NULL AND user_id IS NULL)"
            " OR (scope = 'user' AND workspace_id IS NULL AND user_id IS NOT NULL)",
            name="scope_columns",
        ),
        CheckConstraint("current_version >= 1", name="current_version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class SkillVersion(Base):
    """One version of a skill. A version never changes (docs/architecture.md, Section 9.2).

    ``files`` maps a relative path to its text. ``author_user_id``, ``session_id``, and
    ``run_id`` are not foreign keys: the provenance outlives a deleted user, session, or run.
    """

    __tablename__ = "skill_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "skill_id"], ["skills.org_id", "skills.id"], ondelete="CASCADE"
        ),
        UniqueConstraint("org_id", "skill_id", "version_no"),
        CheckConstraint(_one_of("change", SKILL_CHANGES), name="change"),
        CheckConstraint(_one_of("author_kind", SKILL_AUTHOR_KINDS), name="author_kind"),
        CheckConstraint("jsonb_typeof(files) = 'object'", name="files_object"),
        CheckConstraint("jsonb_typeof(tags) = 'array'", name="tags_array"),
        CheckConstraint(
            "jsonb_typeof(requires_toolsets) = 'array'", name="requires_toolsets_array"
        ),
        CheckConstraint("version_no >= 1", name="version_no"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    skill_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    skill_md: Mapped[str] = mapped_column(Text, nullable=False)
    files: Mapped[dict[str, Any]] = _json()
    tags: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    requires_toolsets: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    change: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    author_kind: Mapped[str] = mapped_column(Text, nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()


PAIRING_KINDS: Final = ("user", "group")
"""A ``user`` code links a chat account to a user. A ``group`` code links a group chat."""


class Integration(Base):
    """A chat app that an org connected, for example its own Telegram bot (Section 11.6).

    The secrets of the integration are in the vault. The row has no clear text: only the
    SHA-256 digest of the webhook secret.
    """

    __tablename__ = "integrations"
    __table_args__ = (
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("org_id", "kind"),
        UniqueConstraint("kind", "external_id"),
        CheckConstraint(_one_of("kind", INTEGRATION_KINDS), name="kind"),
        CheckConstraint(_one_of("status", INTEGRATION_STATUSES), name="status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    external_id: Mapped[str | None] = mapped_column(Text)
    """The id of the bot (Telegram) or of the workspace (Slack). Empty while pending."""
    name: Mapped[str | None] = mapped_column(Text)
    """The bot username (Telegram) or the workspace name (Slack)."""
    settings: Mapped[dict[str, Any]] = _json()
    secret_hash: Mapped[str | None] = mapped_column(Text)
    """The SHA-256 digest of the webhook secret, in hex."""
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


def _integration_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["org_id", "integration_id"],
        ["integrations.org_id", "integrations.id"],
        ondelete="CASCADE",
    )


class ChannelIdentity(Base):
    """A chat account that is linked to a sama user (docs/architecture.md, Section 11.3).

    A direct chat with this account uses the workspace and the agent profile of the row.
    """

    __tablename__ = "channel_identities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            ondelete="CASCADE",
        ),
        _integration_fk(),
        UniqueConstraint("integration_id", "external_id"),
        CheckConstraint(_one_of("channel", CHANNELS), name="channel"),
        Index(None, "org_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    integration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """The integration (the bot) that the chat uses."""
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The account id in the channel, for example the Telegram user id."""
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class ChannelGroup(Base):
    """A group chat that is linked to a workspace and an agent profile (Section 11.3)."""

    __tablename__ = "channel_groups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        _integration_fk(),
        UniqueConstraint("integration_id", "external_id"),
        CheckConstraint(_one_of("channel", CHANNELS), name="channel"),
        Index(None, "org_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    integration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """The integration (the bot) that the chat uses."""
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The chat id in the channel."""
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class PairingCode(Base):
    """A single-use code that links a chat account or a group chat (Section 11.3).

    Only the SHA-256 digest of the code is stored.
    """

    __tablename__ = "pairing_codes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            ondelete="CASCADE",
        ),
        _integration_fk(),
        UniqueConstraint("code_hash"),
        CheckConstraint(_one_of("channel", CHANNELS), name="channel"),
        CheckConstraint(_one_of("kind", PAIRING_KINDS), name="kind"),
        Index(None, "org_id", "user_id"),
        Index(None, "expires_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """The user who made the code. Only this user can use it."""
    integration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """The integration that the code is for. Only that bot accepts the code."""
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VaultSecret(Base):
    """One encrypted secret of an org (Section 16.3). The value is sealed with a data key, and
    the vault key wraps the data key. Only org admins can read the row through RLS, and the row
    has no clear text."""

    __tablename__ = "vault_secrets"
    __table_args__ = (
        UniqueConstraint("org_id", "name"),
        CheckConstraint(_one_of("name", VAULT_NAMES), name="name"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    key_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The id of the vault key that wraps the data key."""
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


MEMORY_SCOPES: Final = ("agent", "user", "workspace")
MEMORY_ACTIONS: Final = ("add", "patch", "remove", "batch", "edit", "revert")
MEMORY_AUTHORS: Final = ("user", "agent")


class Memory(Base):
    """The current notes of one scope (docs/architecture.md, Section 8). ``agent`` notes
    belong to an agent profile in a workspace, ``user`` notes to one user, and ``workspace``
    notes to a workspace. ``entries`` is a list of strings. ``revision`` is the ``seq`` of the
    last row in ``memory_revisions``."""

    __tablename__ = "memories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        UniqueConstraint("org_id", "id"),
        UniqueConstraint(
            "org_id",
            "scope",
            "workspace_id",
            "agent_profile_id",
            "user_id",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(_one_of("scope", MEMORY_SCOPES), name="scope"),
        CheckConstraint(
            "(scope = 'agent' AND workspace_id IS NOT NULL AND agent_profile_id IS NOT NULL"
            " AND user_id IS NULL)"
            " OR (scope = 'user' AND user_id IS NOT NULL AND workspace_id IS NULL"
            " AND agent_profile_id IS NULL)"
            " OR (scope = 'workspace' AND workspace_id IS NOT NULL AND agent_profile_id IS NULL"
            " AND user_id IS NULL)",
            name="scope_keys",
        ),
        CheckConstraint("jsonb_typeof(entries) = 'array'", name="entries_array"),
        CheckConstraint("revision >= 0", name="revision"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    entries: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class MemoryRevision(Base):
    """One write to memory, with the full new content (Section 8). A revert is a new row with
    the old content. The session, the run, and the author are not foreign keys: the history
    outlives them."""

    __tablename__ = "memory_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "memory_id"], ["memories.org_id", "memories.id"], ondelete="CASCADE"
        ),
        UniqueConstraint("org_id", "memory_id", "seq"),
        CheckConstraint(_one_of("action", MEMORY_ACTIONS), name="action"),
        CheckConstraint(_one_of("author_kind", MEMORY_AUTHORS), name="author_kind"),
        CheckConstraint("author_kind <> 'user' OR author_user_id IS NOT NULL", name="user_has_id"),
        CheckConstraint("jsonb_typeof(entries) = 'array'", name="entries_array"),
        CheckConstraint("seq >= 1", name="seq"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entries: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    author_kind: Mapped[str] = mapped_column(Text, nullable=False)
    """``user`` for a person, ``agent`` for the memory tool."""
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The person. For an ``agent`` write: the user of the run, if there is one."""
    reason: Mapped[str | None] = mapped_column(Text)
    reverted_to: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class MessageEmbedding(Base):
    """The embedding of the text of one message, for the meaning part of session search
    (docs/architecture.md, Section 7.1). The embed worker of the control plane writes it. The
    tool never writes it."""

    __tablename__ = "message_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id", "message_id"],
            ["messages.org_id", "messages.session_id", "messages.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("org_id", "message_id"),
        Index(None, "org_id", "session_id"),
        Index(
            "ix_message_embeddings_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
