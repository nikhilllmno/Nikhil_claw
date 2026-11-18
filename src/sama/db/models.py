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
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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
TOOL_DECISIONS: Final = ("allow", "ask", "deny")
TOOL_STATUSES: Final = ("started", "finished", "failed", "denied")
CHANNELS: Final = ("web", "slack", "telegram", "extension")
ACTOR_KINDS: Final = ("user", "agent", "system")
PROVIDERS: Final = ("anthropic", "openai")
MODEL_ENDPOINTS: Final = ("messages", "count_tokens", "responses", "chat_completions")


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
        Index(None, "org_id", "session_id"),
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
    failure_code: Mapped[str | None] = mapped_column(Text)
    failure_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
        Index(None, "session_id", "seq"),
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
