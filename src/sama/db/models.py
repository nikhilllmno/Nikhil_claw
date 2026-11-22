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
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
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
INVITE_ROLES: Final = ("admin", "member")
"""An invite never makes an owner. An owner gives the role ``owner`` to a member later."""
RUNTIMES: Final = ("claude_agent_sdk", "openai_agents_sdk", "claude_code", "codex", "fake")
CODING_AGENTS: Final = ("claude_code", "codex")
"""The coding agent runtimes (Section 15.4). A session in the direct mode and a run of
``/claude`` or ``/codex`` name one in ``coding_agent``."""
AUDIENCES: Final = ("private", "shared")
SESSION_STATUSES: Final = ("active", "archived")
LINEAGES: Final = ("compaction", "branch", "retry", "delegation")
MESSAGE_ROLES: Final = ("user", "assistant", "system", "tool")
RUN_STATUSES: Final = ("queued", "running", "paused", "completed", "failed", "cancelled")
ENDED_RUN_STATUSES: Final = ("completed", "failed", "cancelled")
RESUME_DECISIONS: Final = ("approve", "reject")
RUN_KINDS: Final = ("chat", "review")
BUSY_MODES: Final = ("steer", "parallel", "queue")
"""The busy modes of an agent profile (Section 4.3, M7.1). ``steer`` is the default."""
MAX_PARALLEL_RUNS_CAP: Final = 10
"""The highest value of ``agent_profiles.max_parallel_runs``."""
"""``chat`` answers a user message. ``review`` is the background review of a session
(docs/architecture.md, Section 9.4)."""
JOB_STATUSES: Final = ("active", "paused", "pending_approval", "removed")
JOB_MODES: Final = ("agent", "script", "curator")
"""``curator`` is the weekly curator job of a workspace (docs/architecture.md, Section 9.7).
Only sama creates it."""
JOB_CREATORS: Final = ("user", "agent")
JOB_CREATED_BY: Final = (*JOB_CREATORS, "system")
"""Who created a job. ``system`` is sama itself: the curator job of a workspace."""
WORKFLOW_STATUSES: Final = ("active", "paused", "pending_approval", "removed")
WORKFLOW_INSTANCE_STATUSES: Final = ("running", "waiting", "completed", "failed", "cancelled")
ENDED_WORKFLOW_INSTANCE_STATUSES: Final = ("completed", "failed", "cancelled")
WORKFLOW_TRIGGERS: Final = ("manual", "schedule", "trigger", "workflow")
WORKFLOW_STEP_KINDS: Final = ("tool", "agent", "approval", "branch", "parallel", "wait", "workflow")
WORKFLOW_STEP_STATUSES: Final = (
    "pending",
    "running",
    "waiting",
    "needs_reconcile",
    "waiting_human",
    "completed",
    "failed",
    "skipped",
    "cancelled",
)
RECEIPT_STATUSES: Final = ("intent", "done", "failed", "unknown")
FIRE_STATUSES: Final = ("running", "completed", "failed", "skipped")
DELIVERY_KINDS: Final = ("result", "failure", "alert")
DELIVERY_STATUSES: Final = ("pending", "sending", "sent", "failed", "unknown")
PROPOSAL_KINDS: Final = ("memory", "skill", "soul", "profile", "toolsets", "policy")
PROPOSAL_ORIGINS: Final = ("review", "chat")
"""``review``: a background review proposed the change. ``chat``: the agent proposed it in a chat
run, because the user of the run does not have the role for the change (Section 9.4)."""
WORKER_BOT_STATUSES: Final = ("active", "disabled")
PROPOSAL_SCOPES: Final = ("agent", "workspace", "org")
PROPOSAL_STATUSES: Final = ("pending", "applied", "failed", "discarded")
PROFILE_CHANGE_KINDS: Final = ("soul", "profile", "toolsets", "policy")
PROFILE_CHANGE_SOURCES: Final = ("proposal", "edit", "revert")
"""``proposal``: a person approved a ``change_propose`` proposal. ``edit``: an admin changed the
profile in the web app. ``revert``: an admin reverted an earlier change (Section 11.4)."""
TOOL_DECISIONS: Final = ("allow", "ask", "deny")
TOOL_STATUSES: Final = ("started", "finished", "failed", "denied")
APPROVAL_STATUSES: Final = ("pending", "approved", "rejected", "expired")
CHANNELS: Final = ("web", "slack", "telegram", "extension", "whatsapp", "email")
ACTOR_KINDS: Final = ("user", "agent", "system")
PROVIDERS: Final = ("anthropic", "openai", "gemini")
KEY_SOURCES: Final = ("org", "sama")
"""The key of a model call: the key of the org or the key of sama (Section 16.6, M6.7)."""
REGION_PATTERN: Final = "^[a-z]{2}-[a-z]+-[0-9]$"
"""A region id in the form of a cloud region, for example ``ap-south-1`` (Section 17.3)."""
MODEL_ENDPOINTS: Final = (
    "messages",
    "count_tokens",
    "responses",
    "chat_completions",
    "images",
    "speech",
    "generate_content",
)
VAULT_NAMES: Final = (
    "anthropic_api_key",
    "openai_api_key",
    "brave_api_key",
    "telegram_bot_token",
    "slack_bot_token",
    "slack_signing_secret",
    "composio_api_key",
    "composio_webhook_secret",
    "whatsapp_api_token",
    "whatsapp_app_secret",
    "whatsapp_link_secret",
    "sendgrid_api_key",
    "email_link_secret",
    "gemini_api_key",
    "x_bearer_token",
    "mem0_api_key",
)
CONNECTION_SECRET_PATTERN: Final = "^connection:[0-9a-f-]{36}$"  # noqa: S105 (a name pattern)
"""The vault name of the secret fields of one connection (Section 10.3): ``connection:<id>``."""
CONNECTION_SCOPES: Final = ("personal", "workspace")
CONNECTION_KINDS: Final = ("composio", "mcp_remote", "mcp_hosted", "database", "http_api", "plugin")
CONNECTION_STATES: Final = ("pending", "initiated", "connected", "failed", "expired", "disabled")
"""The states of a connection (Section 10.3). Only the backend sets ``connected``."""
TRIGGER_SECRET_PATTERN: Final = "^trigger:[0-9a-f-]{36}$"  # noqa: S105 (a name pattern)
"""The vault name of the signing secret of one webhook trigger (Section 12.2): ``trigger:<id>``."""
BROWSER_LOGIN_SECRET_PATTERN: Final = "^browser_login:[0-9a-f-]{36}$"  # noqa: S105 (a pattern)
"""The vault name of the user name and the password of one saved browser login (Section 14.1):
``browser_login:<id>``."""
CUSTOM_TOOL_SECRET_PATTERN: Final = "^custom_tool:[0-9a-f-]{36}$"  # noqa: S105 (a pattern)
"""The vault name of the credential of one custom HTTP tool (Section 10.8): ``custom_tool:<id>``."""
CUSTOM_TOOL_SCOPES: Final = ("org", "workspace", "user")
CUSTOM_TOOL_KINDS: Final = ("http", "script")
CUSTOM_TOOL_POLICIES: Final = ("allow", "ask", "deny")
CUSTOM_TOOL_CHANGES: Final = ("create", "edit", "revert")
CUSTOM_TOOL_REVIEWS: Final = ("approved", "pending", "rejected")
"""A version of a shared custom tool that a member without the role writes is ``pending`` until
an admin approves or rejects it (Section 10.8)."""
TRIGGER_KINDS: Final = ("webhook", "composio")
TRIGGER_STATUSES: Final = ("active", "disabled")
TRIGGER_OUTCOMES: Final = (
    "accepted",
    "duplicate",
    "bad_signature",
    "disabled",
    "bad_body",
    "rate_limited",
    "rejected",
    "test_ok",
)
"""The outcomes of a delivery to a trigger (M7.10). ``test_ok`` is a test send that stopped after
the checks."""
MAX_DELIVERIES_PER_TRIGGER: Final = 200
"""The delivery rows that stay for one trigger. The control plane deletes the older rows."""
REASONING_EFFORTS: Final = ("low", "medium", "high")
ROLE_PERMISSION_ROLES: Final = ("admin", "member")
TRIGGER_TARGETS: Final = ("run", "workflow")
"""What an event starts (Section 12.2). Workflows (M4.5) add ``workflow``."""
SAFE_TOOLSETS: Final = ("web", "media", "clarify")
"""The ``safe`` preset (Section 10.2): the default toolsets of an event-triggered run."""
INTEGRATION_KINDS: Final = ("telegram", "slack", "whatsapp", "email")
"""The chat apps that an org can connect (docs/architecture.md, Sections 11.6 and 11.9)."""
INTEGRATION_STATUSES: Final = ("pending", "active", "failed")
BROWSER_TARGETS: Final = ("cloud", "my_chrome")
"""The browser targets (Section 14.1): Browserbase now, the Chrome extension in M5.5."""
GITHUB_ACCOUNT_TYPES: Final = ("User", "Organization", "Enterprise")
"""The GitHub account types that can install the sama GitHub App (Section 15.1)."""
SKILL_SCOPES: Final = ("org", "workspace", "user")
"""The stored scopes of a skill. The scope ``builtin`` is in the repo, not in the database."""
SKILL_STATUSES: Final = ("active", "stale", "archived")
"""The curator marks an unused skill ``stale``, then ``archived`` (Section 9.7). A ``stale``
skill still works."""
SKILL_CHANGES: Final = (
    "create",
    "patch",
    "edit",
    "write_file",
    "remove_file",
    "archive",
    "restore",
)
SKILL_AUTHOR_KINDS: Final = ("user", "agent", "system")
TASK_STATUSES: Final = ("todo", "running", "blocked", "done", "cancelled")
"""The columns of the task board (docs/architecture.md, Section 12.4)."""
ENDED_TASK_STATUSES: Final = ("done", "cancelled")
TASK_AUTHOR_KINDS: Final = ("user", "agent", "system")
MAX_TASK_DEPENDENCIES: Final = 20
COMPACTION_TRIGGERS: Final = ("auto", "manual")
PIN_SOURCES: Final = ("user", "message")
STEER_STATUSES: Final = ("pending", "delivered", "requeued", "dropped")
MAX_STEER_CHARS: Final = 4000
EMBEDDING_DIMENSIONS: Final = 1536
"""The size of a message embedding: the output of ``text-embedding-3-small``
(docs/architecture.md, Section 7.1)."""
ATTACHMENT_MIME_TYPES: Final = ("image/png", "image/jpeg", "image/gif", "image/webp")
"""The image types that a channel can attach to a message (docs/architecture.md, Section 11.7)."""
MAX_ATTACHMENT_BYTES: Final = 10 * 1024 * 1024
"""The size limit of one attachment, for the database and for the gateways."""
GENERATED_MEDIA_KINDS: Final = ("image", "audio")
GENERATED_MEDIA_MIME_TYPES: Final = ("image/png", "audio/ogg")
"""PNG images of ``image_generate`` and Ogg Opus voice notes of ``text_to_speech``."""
MAX_GENERATED_MEDIA_BYTES: Final = 10 * 1024 * 1024
API_KEY_SCOPES: Final = ("chat", "mcp")
"""What an org API key can call (Section 10.7): ``chat`` is the OpenAI-compatible endpoint
and ``mcp`` is the sama MCP server for other agents."""
API_KEY_APPROVALS: Final = ("deny", "pending")
"""What an ``ask`` tool call does in a run of an API key (Section 10.7): ``deny`` makes the run
unattended, so the gate denies the call. ``pending`` pauses the run until a person decides the
approval in the web app."""
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


MAX_RETENTION_DAYS: Final = 3650
"""The upper limit of ``orgs.retention_days`` (docs/architecture.md, Section 17.4)."""
PURGE_KINDS: Final = ("sandboxes", "traces", "browser_contexts", "mem0")
MEMORY_PROVIDERS: Final = ("builtin", "mem0")
"""The memory providers of a profile (Section 8.2). ``builtin`` is the default."""


class Org(Base):
    __tablename__ = "orgs"
    __table_args__ = (
        Index(None, "created_by_auth_user_id", "created_at"),
        Index(None, "created_at"),
        CheckConstraint(f"region ~ '{REGION_PATTERN}'", name="region"),
        CheckConstraint(
            f"retention_days IS NULL OR retention_days BETWEEN 1 AND {MAX_RETENTION_DAYS}",
            name="retention_days",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default="team")
    """The plan of the org (``sama.orgs.plans``). It sets the member cap and the default
    limits."""
    region: Mapped[str] = mapped_column(Text, nullable=False, server_default="ap-south-1")
    limits: Mapped[dict[str, Any]] = _json()
    created_by_auth_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The Supabase Auth user who made the org with self-serve sign-up (Section 17.2). Null for
    an org of ``bootstrap``. The rate limits of org creation count these rows."""
    retention_days: Mapped[int | None] = mapped_column(Integer)
    """Sama deletes a session after this many days without activity. Null means the default
    of the plan (Section 17.4)."""
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


class OrgRolePermission(Base):
    """One permission of one role in one org (docs/architecture.md, Section 17.2, M7.10).

    A permission without a row is allowed, so an org that never changes a permission keeps the
    behaviour of the roles. The org owner has every permission and has no row. The check of this
    table adds to the check of the role: it never gives more. An org member reads the rows of the
    org. Only the control plane writes the table."""

    __tablename__ = "org_role_permissions"
    __table_args__ = (
        ForeignKeyConstraint(["org_id", "updated_by_user_id"], ["users.org_id", "users.id"]),
        UniqueConstraint("org_id", "role", "permission"),
        CheckConstraint(_one_of("role", ROLE_PERMISSION_ROLES), name="role"),
        CheckConstraint("char_length(permission) BETWEEN 1 AND 80", name="permission_length"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    role: Mapped[str] = mapped_column(Text, nullable=False)
    permission: Mapped[str] = mapped_column(Text, nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    updated_at: Mapped[datetime] = _created_at()


class OrgInvite(Base):
    """An invite to an org (docs/architecture.md, Section 17.2).

    The link of the invite holds a random token. Only its SHA-256 hash is here. Org admins
    read the rows of their org, but not ``token_hash``. Only the control plane writes the
    table."""

    __tablename__ = "org_invites"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "invited_by_user_id"], ["users.org_id", "users.id"]),
        ForeignKeyConstraint(["org_id", "accepted_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint(_one_of("role", INVITE_ROLES), name="role"),
        CheckConstraint(
            "email = lower(email) AND char_length(email) BETWEEN 3 AND 320", name="email"
        ),
        CheckConstraint("octet_length(token_hash) = 32", name="token_hash_length"),
        CheckConstraint("(accepted_at IS NULL) = (accepted_user_id IS NULL)", name="accepted"),
        CheckConstraint("accepted_at IS NULL OR revoked_at IS NULL", name="one_end"),
        Index(None, "org_id", "created_at"),
        Index(
            None,
            "org_id",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
        Index(None, "email", postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    email: Mapped[str] = mapped_column(Text, nullable=False)
    """The invited email, in lower case. Only a user with this verified email can accept."""
    role: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """A workspace that a ``member`` joins with the org. An admin sees every workspace."""
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class OrgBilling(Base):
    """The Stripe customer and subscription of an org (docs/architecture.md, Section 17.5).

    Org admins read the row of their org. Only the control plane writes the table. The
    ``usage_*`` columns are the outbox of the usage report: ``usage_pending_cents`` is set
    before the meter event goes to Stripe, and becomes ``usage_reported_cents`` after."""

    __tablename__ = "org_billing"
    __table_args__ = (
        CheckConstraint("usage_reported_cents >= 0", name="usage_reported_cents"),
        CheckConstraint(
            "usage_pending_cents IS NULL OR usage_pending_cents > usage_reported_cents",
            name="usage_pending_cents",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    stripe_customer_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(Text, unique=True)
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default="free")
    """The plan of the subscription. ``orgs.plan`` follows it."""
    status: Mapped[str | None] = mapped_column(Text)
    """The Stripe status of the subscription, for example ``active`` or ``past_due``."""
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    usage_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """The billing period of ``usage_reported_cents``."""
    usage_reported_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    """The billable usage of the period that Stripe has, in US cents."""
    usage_pending_cents: Mapped[int | None] = mapped_column(BigInteger)
    """The billable usage of a meter event that is maybe not in Stripe yet."""
    usage_pending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When ``usage_pending_cents`` was set. An old pending event is not sent again."""
    usage_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkout_session_id: Mapped[str | None] = mapped_column(Text)
    """The open Stripe Checkout session of the org. An org has at most one."""
    checkout_url: Mapped[str | None] = mapped_column(Text)
    checkout_plan: Mapped[str | None] = mapped_column(Text)
    checkout_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class BillingEvent(Base):
    """A Stripe event that sama applied (Section 17.5). A retry of the event does nothing."""

    __tablename__ = "billing_events"
    __table_args__ = (Index(None, "org_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    stripe_event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class AgentProfile(Base):
    __tablename__ = "agent_profiles"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        CheckConstraint(_one_of("runtime", RUNTIMES), name="runtime"),
        CheckConstraint("jsonb_typeof(subagents) = 'array'", name="subagents_is_array"),
        CheckConstraint(
            f"reasoning_effort IS NULL OR {_one_of('reasoning_effort', REASONING_EFFORTS)}",
            name="reasoning_effort",
        ),
        CheckConstraint(_one_of("busy_mode", BUSY_MODES), name="busy_mode"),
        CheckConstraint(
            f"max_parallel_runs BETWEEN 1 AND {MAX_PARALLEL_RUNS_CAP}", name="max_parallel_runs"
        ),
        CheckConstraint(
            "jsonb_typeof(library_subagents) = 'array'", name="library_subagents_is_array"
        ),
        CheckConstraint(_one_of("memory_provider", MEMORY_PROVIDERS), name="memory_provider"),
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
    group_toolsets: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """The toolsets of a chat run in a shared session: a Telegram group or a Slack channel
    (Section 11.3). A shared session uses these and not ``toolsets``."""
    skills: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    policy: Mapped[dict[str, Any]] = _json()
    budget: Mapped[dict[str, Any]] = _json()
    background_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    """False turns the background review off for the sessions of the profile (Section 9.4)."""
    review_model: Mapped[str | None] = mapped_column(Text)
    """The model of the background review. ``None``: the default cheap model of the runtime."""
    subagents: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """The native sub-agents of the profile: a JSON array of ``SubagentSpec`` (Section 13.1).
    Write it only with ``sama.subagents.set_profile_subagents``."""
    library_subagents: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """The library agents that the profile uses: a JSON array of ``library_agents`` ids, in
    order (Section 13.3, M7.4). A plain list, not a key: a session freezes the agents."""
    reasoning_effort: Mapped[str | None] = mapped_column(Text)
    """How much the model thinks (M7.10): ``low``, ``medium``, or ``high``. ``None``: the default
    of the model. A session freezes it in its run config."""
    busy_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="steer")
    """What a new message does while a run of its session is open (Section 4.3, M7.1):
    ``steer`` (it joins the open run at its next step), ``parallel`` (it starts a second run at
    the same time), or ``queue`` (it waits)."""
    max_parallel_runs: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    """The most open chat runs (running or paused) of one session of the profile (M7.1)."""
    delegation_model: Mapped[str | None] = mapped_column(Text)
    """The model of the child runs of ``delegate_task`` (Section 13.2). ``None``: the model of
    the profile."""
    memory_provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="builtin")
    """``builtin`` or ``mem0`` (Section 8.2). The built-in memory works in both cases."""
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
        # A delegated child names the run of its parent that made it (Section 13.2). The key
        # proves that the run is a run of the parent session.
        ForeignKeyConstraint(
            ["org_id", "parent_session_id", "parent_run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            # ``runs`` has a key to ``sessions`` too, so the cycle needs ALTER.
            use_alter=True,
        ),
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
        CheckConstraint(
            "parent_run_id IS NULL OR lineage IS NOT DISTINCT FROM 'delegation'",
            name="parent_run_is_delegation",
        ),
        CheckConstraint(
            f"coding_agent IS NULL OR {_one_of('coding_agent', CODING_AGENTS)}",
            name="coding_agent",
        ),
        Index(None, "org_id", "workspace_id"),
        Index(None, "owner_user_id"),
        Index(None, "org_id", "worker_bot_id", postgresql_where=text("worker_bot_id IS NOT NULL")),
        Index(
            "ix_sessions_parent_run",
            "org_id",
            "parent_run_id",
            postgresql_where=text("parent_run_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    worker_bot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The worker bot that answers in the session (Section 11.6). A plain column, not a foreign
    key: a deleted bot leaves its sessions, and they stop counting for any budget."""
    audience: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    parent_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The parent run that delegated this session (lineage ``delegation``, Section 13.2)."""
    lineage: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    sandbox_id: Mapped[str | None] = mapped_column(Text)
    """The provider id of the session sandbox (docs/architecture.md, Section 16.2)."""
    run_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """The frozen ``RunConfig`` of the session, set by the first run (Section 6.1)."""
    config_fingerprint: Mapped[str | None] = mapped_column(Text)
    """``RunConfig.fingerprint()`` of ``run_config``."""
    profile_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """The toolsets and the profile policy rules when the session froze its config (Section
    11.4). The gate lets the session use only what the snapshot and the profile of now both
    allow, so a new permission applies from the next session and a removal applies at once."""
    library_agent: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """The frozen spec of the library agent that the session runs as (Section 13.3, M7.4): a
    child of ``delegate_task(agent=...)`` or a test run from the web app. ``None`` for a
    session that runs as its agent profile."""
    review_user_turns: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """The user turns that ended since the last background review (Section 9.4)."""
    review_tool_iterations: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """The tool calls that ended since the last background review (Section 9.4)."""
    review_cursor_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    """The ``messages.seq`` up to which the last queued review reads the transcript."""
    coding_agent: Mapped[str | None] = mapped_column(Text)
    """The direct mode (Section 15.4): each chat run of the session goes to this coding agent
    (``claude_code`` or ``codex``) until ``/sama``. Null: the runtime of the session."""
    plan_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    """Plan mode of coding mode (Section 15.5, M7.9). While it is on, the gate refuses the
    tools that change files, and the agent proposes a plan with ``plan_propose``."""
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
        CheckConstraint(_one_of("kind", RUN_KINDS), name="kind"),
        # A review reads a range of the transcript, and no person is in it (Section 9.4).
        CheckConstraint(
            "(kind = 'review') = (review_from_seq IS NOT NULL AND review_to_seq IS NOT NULL)",
            name="review_range",
        ),
        CheckConstraint("kind <> 'review' OR unattended", name="review_unattended"),
        # A compact run is a chat run of a person (Section 6.3).
        CheckConstraint("NOT compact OR kind = 'chat'", name="compact_is_chat"),
        CheckConstraint("compact OR compact_focus IS NULL", name="compact_focus"),
        CheckConstraint("NOT side OR (kind = 'chat' AND NOT compact)", name="side_is_chat"),
        CheckConstraint(
            f"coding_agent IS NULL OR {_one_of('coding_agent', CODING_AGENTS)}",
            name="coding_agent",
        ),
        CheckConstraint(
            "toolsets IS NULL OR jsonb_typeof(toolsets) = 'array'", name="toolsets_array"
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
        # One open main run per session (migration 0043, Section 4.3). Side runs (M7.1) run
        # next to it, up to the limit of the profile, which the claim checks.
        Index(
            "ix_runs_one_main_per_session",
            "session_id",
            unique=True,
            postgresql_where=text("status IN ('running', 'paused') AND NOT side"),
        ),
        Index(
            "ix_runs_active_per_session",
            "session_id",
            postgresql_where=text("status IN ('running', 'paused')"),
        ),
        # One waiting review per session (migration 0016, Section 9.4). The index above gives
        # at most one running review.
        Index(
            "ix_runs_one_queued_review_per_session",
            "session_id",
            unique=True,
            postgresql_where=text("kind = 'review' AND status = 'queued'"),
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
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="chat")
    review_from_seq: Mapped[int | None] = mapped_column(BigInteger)
    """For a review: the messages after this ``messages.seq`` are new for it."""
    review_to_seq: Mapped[int | None] = mapped_column(BigInteger)
    """For a review: the last ``messages.seq`` that it reads."""
    compact: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    """True for a ``/compact`` run (docs/architecture.md, Section 6.3). It is a chat run with
    no user message: the runtime compacts the context of the session and does not answer."""
    compact_focus: Mapped[str | None] = mapped_column(Text)
    """What the person asked the summary to keep, for a compact run. Redacted."""
    unattended: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    """True for a run that no human watches, for example a cron job or a webhook. The tool
    gate turns ``ask`` into ``deny`` in these runs (docs/architecture.md, Section 16.1)."""
    toolsets: Mapped[list[str] | None] = mapped_column(JSONB)
    """A limit on the toolsets of this run, for example the toolsets of an event trigger
    (Section 12.2). The run gets only the toolsets in this list and in its profile. Null: no
    extra limit."""
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
    model: Mapped[str | None] = mapped_column(Text)
    """The model pin of a job run (Section 12.1). Null: the model of the profile."""
    coding_agent: Mapped[str | None] = mapped_column(Text)
    """The coding agent of a ``/claude`` or ``/codex`` run with a task (Section 15.4). The run
    uses this runtime and not the runtime of the session."""
    allow_parallel: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    """True when the run can start next to an open chat run of its session (M7.1): the profile
    was in the busy mode ``parallel`` at the enqueue, or the person asked for it (``/bg``)."""
    side: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    """True when the run started while another chat run of its session was open (M7.1). A side
    run starts a new runtime session, and the next run of the session does not resume its
    runtime state."""
    context_seq: Mapped[int | None] = mapped_column(BigInteger)
    """The newest ``messages.seq`` that the runtime state of this run has seen (M7.1): the
    newest message at the claim of a chat run. A ``/compact`` run and a failed run keep
    ``state_seq``, because their state has no catch-up of side answers."""
    state_seq: Mapped[int | None] = mapped_column(BigInteger)
    """The ``context_seq`` of the run whose runtime state this run resumed (M7.1). Null: the
    run started with no state."""


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
        # The tool trail of a session (Section 17.8): migration 0048.
        Index(
            "ix_run_events_activity",
            "org_id",
            "session_id",
            "id",
            postgresql_where=text(
                "type IN ('tool_call_started', 'tool_call_finished', 'subagent_event')"
            ),
        ),
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
        CheckConstraint(_one_of("key_source", KEY_SOURCES), name="key_source"),
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
    key_source: Mapped[str] = mapped_column(Text, nullable=False, server_default="sama")
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
        # Lets a review proposal prove that its approval belongs to its org.
        UniqueConstraint("org_id", "id"),
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
    A skill is never deleted: ``archive`` sets the status. The counters and the pin are for
    the curator (Section 9.7).
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
        CheckConstraint("(status = 'stale') = (stale_since IS NOT NULL)", name="stale_since"),
        CheckConstraint("view_count >= 0 AND use_count >= 0 AND patch_count >= 0", name="counts"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    """A pinned skill is never marked stale or archived by the curator (Section 9.7)."""
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """The ``skill_view`` calls that read the skill."""
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """The slash commands that used the skill."""
    patch_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """The writes that changed the text or the files of the skill."""
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """The last view, use, or change. The curator measures the idle time from it."""
    stale_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the curator marked the skill ``stale``. ``NULL`` for any other status."""
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


class WorkerBot(Base):
    """An agent profile that is bound to one Slack channel, with a daily budget (Section 11.6).

    A mention of the app in the channel starts a shared session with the profile of the bot. The
    model cost of the sessions of the bot in one UTC day must stay below ``daily_budget_usd``.
    Only the control plane writes the table."""

    __tablename__ = "worker_bots"
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
        CheckConstraint(_one_of("status", WORKER_BOT_STATUSES), name="status"),
        CheckConstraint("daily_budget_usd > 0", name="daily_budget_usd"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 80", name="name"),
        Index(None, "org_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    integration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """The Slack integration (the app) that the channel uses."""
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The Slack channel id."""
    name: Mapped[str] = mapped_column(Text, nullable=False)
    daily_budget_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
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
        CheckConstraint(
            f"{_one_of('name', VAULT_NAMES)} OR name ~ '{CONNECTION_SECRET_PATTERN}'"
            f" OR name ~ '{TRIGGER_SECRET_PATTERN}' OR name ~ '{BROWSER_LOGIN_SECRET_PATTERN}'"
            f" OR name ~ '{CUSTOM_TOOL_SECRET_PATTERN}'",
            name="name",
        ),
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


class Attachment(Base):
    """An image that a user sent with a message in a channel (docs/architecture.md, Section
    11.7). The gateway writes it with the message, in one transaction. The bytes stay in the
    database: only the control plane and the tool server read ``data``. A member can read the
    metadata of the sessions that the member can see, never ``data``."""

    __tablename__ = "attachments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id", "message_id"],
            ["messages.org_id", "messages.session_id", "messages.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(_one_of("mime_type", ATTACHMENT_MIME_TYPES), name="mime_type"),
        CheckConstraint(
            f"size_bytes BETWEEN 1 AND {MAX_ATTACHMENT_BYTES} AND size_bytes = octet_length(data)",
            name="size_bytes",
        ),
        Index(None, "org_id", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    """The file name that the sender gave, cleaned and cut short."""
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)
    created_at: Mapped[datetime] = _created_at()


class GeneratedMedia(Base):
    """An image or a voice note that ``image_generate`` or ``text_to_speech`` made in a run
    (docs/architecture.md, Section 11.8; roadmap M4.9). The bytes stay in the database. The
    gateway of the run sends the file to the chat when the run ends and sets ``delivered_at``.
    A member can read the metadata of the sessions that the member can see, never ``data``."""

    __tablename__ = "generated_media"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(_one_of("kind", GENERATED_MEDIA_KINDS), name="kind"),
        CheckConstraint(_one_of("mime_type", GENERATED_MEDIA_MIME_TYPES), name="mime_type"),
        CheckConstraint(
            f"size_bytes BETWEEN 1 AND {MAX_GENERATED_MEDIA_BYTES}"
            " AND size_bytes = octet_length(data)",
            name="size_bytes",
        ),
        Index(None, "org_id", "run_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    caption: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    """A short text for the chat: the prompt of the image, or the start of the spoken text."""
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class ReviewProposal(Base):
    """A change of shared memory or of a shared skill that a background review proposes
    (docs/architecture.md, Section 9.4). The row holds the full change. Its approval is the
    human decision: sama applies the change only after a workspace admin (or the org owner for
    the scope ``org``) approves it. Only the control plane writes the table."""

    __tablename__ = "review_proposals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "approval_id"], ["approvals.org_id", "approvals.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "acting_user_id"], ["users.org_id", "users.id"]),
        UniqueConstraint("approval_id"),
        CheckConstraint(_one_of("kind", PROPOSAL_KINDS), name="kind"),
        CheckConstraint(_one_of("scope", PROPOSAL_SCOPES), name="scope"),
        CheckConstraint(_one_of("origin", PROPOSAL_ORIGINS), name="origin"),
        CheckConstraint(_one_of("status", PROPOSAL_STATUSES), name="status"),
        CheckConstraint("(status = 'applied') = (applied_at IS NOT NULL)", name="applied_at"),
        Index(None, "org_id", "session_id"),
        Index(
            "ix_review_proposals_pending", "created_at", postgresql_where=text("status = 'pending'")
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    approval_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    acting_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The user of the session when the review proposed the change, if there was one."""
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False, server_default="review")
    payload: Mapped[dict[str, Any]] = _json()
    """The full change: the memory target and operations, the ``skill_manage`` request, or the
    new soul of the profile."""
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProfileChange(Base):
    """One change of an agent profile, for the history and the revert in the web app
    (docs/architecture.md, Section 11.4). ``before`` and ``after`` hold the changed fields
    only. Only the control plane writes the table."""

    __tablename__ = "profile_changes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "actor_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint(_one_of("kind", PROFILE_CHANGE_KINDS), name="kind"),
        CheckConstraint(_one_of("source", PROFILE_CHANGE_SOURCES), name="source"),
        CheckConstraint("jsonb_typeof(before) = 'object'", name="before_is_object"),
        CheckConstraint("jsonb_typeof(after) = 'object'", name="after_is_object"),
        Index(None, "org_id", "agent_profile_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The workspace of the profile. ``None`` for a profile of the whole org."""
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The approved proposal. A plain column: the history stays when the session is deleted."""
    reverts_change_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reverted_by_change_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    summary: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any]] = _json()
    """The values before the change, redacted: the table never has a known secret."""
    after: Mapped[dict[str, Any]] = _json()
    redacted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    """True when the redaction changed ``before`` or ``after``. Such a change cannot be
    reverted, because sama does not keep the old values."""
    created_at: Mapped[datetime] = _created_at()


class SessionCompaction(Base):
    """One compaction of a session (docs/architecture.md, Section 6.3). The PreCompact hook of
    the runtime makes the row with the archive session. The ``compacted`` event of the runtime
    sets the token counts. A row without ``completed_at`` is an archive whose compaction did
    not finish. Only the control plane writes the table."""

    __tablename__ = "session_compactions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["org_id", "archive_session_id"], ["sessions.org_id", "sessions.id"]),
        UniqueConstraint("archive_session_id"),
        CheckConstraint(_one_of("trigger", COMPACTION_TRIGGERS), name="trigger"),
        CheckConstraint(
            "(completed_at IS NULL) = (tokens_before IS NULL AND tokens_after IS NULL)",
            name="completed",
        ),
        CheckConstraint(
            "(tokens_before IS NULL OR tokens_before >= 0)"
            " AND (tokens_after IS NULL OR tokens_after >= 0)",
            name="tokens",
        ),
        CheckConstraint("archive_entries >= 0", name="archive_entries"),
        Index(None, "org_id", "session_id", "created_at"),
        Index(None, "run_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_before: Mapped[int | None] = mapped_column(Integer)
    tokens_after: Mapped[int | None] = mapped_column(Integer)
    archive_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The session that holds the transcript before the compaction. It is a child of the
    session with the lineage ``compaction``."""
    archive_entries: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    archive_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    """True when the archive has a cut: sama kept the start and the end of a long transcript."""
    created_at: Mapped[datetime] = _created_at()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContextPin(Base):
    """A text that a person pinned to a session so that compaction keeps it (docs/architecture.md,
    Section 6.3). The next run of the session gets the pin in its input, until a compaction
    makes the runtime forget it: then the next run gets it again. Only the control plane
    writes the table."""

    __tablename__ = "context_pins"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint(_one_of("source", PIN_SOURCES), name="source"),
        CheckConstraint("char_length(text) BETWEEN 1 AND 2000", name="text_length"),
        CheckConstraint("(injected_at IS NULL) = (injected_run_id IS NULL)", name="injected"),
        Index(None, "org_id", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    """The redacted text of the pin."""
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="user")
    injected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When a run got the pin in its input. Null: no run has it yet, or a compaction made the
    runtime forget it."""
    injected_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The run that got the pin. A claim of the same run again gets the pin again."""
    created_at: Mapped[datetime] = _created_at()


class Connection(Base):
    """One configured account or endpoint of a connector in a workspace (docs/architecture.md,
    Section 10.3). A ``personal`` connection belongs to ``owner_user_id``. A ``workspace``
    connection is shared. The row has no secret: ``secret_ref`` names the vault entry of the
    secret fields of a form connection, and a Composio connection keeps its credentials at
    Composio. Only the control plane writes the table."""

    __tablename__ = "connections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "owner_user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        UniqueConstraint("org_id", "id"),
        UniqueConstraint(
            "org_id",
            "workspace_id",
            "connector",
            "owner_user_id",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(_one_of("scope", CONNECTION_SCOPES), name="scope"),
        CheckConstraint(_one_of("kind", CONNECTION_KINDS), name="kind"),
        CheckConstraint(_one_of("state", CONNECTION_STATES), name="state"),
        CheckConstraint("(scope = 'personal') = (owner_user_id IS NOT NULL)", name="owner"),
        CheckConstraint("connector ~ '^[a-z][a-z0-9_-]{0,39}$'", name="connector"),
        CheckConstraint(
            "secret_ref IS NULL OR secret_ref = 'connection:' || id::text", name="secret_ref"
        ),
        CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 500", name="last_error_length"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    connector: Mapped[str] = mapped_column(Text, nullable=False)
    """The name of the catalog manifest."""
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = _json()
    """Non-secret settings, for example the Composio account id and entity id."""
    secret_ref: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    tool_cache: Mapped[dict[str, Any]] = _json()
    policy_overrides: Mapped[dict[str, Any]] = _json()
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class Task(Base):
    """A card on the task board of a workspace (docs/architecture.md, Section 12.4). The
    dispatcher starts a session and a run for a ``todo`` card with an agent assignee. A card
    has the audience of a session, so a private card is visible only to its owner."""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("org_id", "workspace_id", "id"),
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "assignee_profile_id"], ["agent_profiles.org_id", "agent_profiles.id"]
        ),
        ForeignKeyConstraint(["org_id", "assignee_user_id"], ["users.org_id", "users.id"]),
        ForeignKeyConstraint(["org_id", "owner_user_id"], ["users.org_id", "users.id"]),
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        ForeignKeyConstraint(
            ["org_id", "workspace_id", "parent_task_id"],
            ["tasks.org_id", "tasks.workspace_id", "tasks.id"],
        ),
        ForeignKeyConstraint(
            ["org_id", "workspace_id", "session_id"],
            ["sessions.org_id", "sessions.workspace_id", "sessions.id"],
        ),
        CheckConstraint(_one_of("status", TASK_STATUSES), name="status"),
        CheckConstraint(_one_of("audience", AUDIENCES), name="audience"),
        CheckConstraint(
            "audience <> 'private' OR owner_user_id IS NOT NULL", name="private_has_owner"
        ),
        CheckConstraint(
            "assignee_profile_id IS NULL OR assignee_user_id IS NULL", name="one_assignee"
        ),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint("char_length(body) <= 20000", name="body_length"),
        CheckConstraint(
            f"cardinality(depends_on) <= {MAX_TASK_DEPENDENCIES}", name="depends_on_size"
        ),
        CheckConstraint("parent_task_id IS NULL OR parent_task_id <> id", name="not_own_parent"),
        Index(None, "org_id", "workspace_id", "status"),
        Index(
            "ix_tasks_dispatch",
            "created_at",
            postgresql_where=text("status = 'todo' AND assignee_profile_id IS NOT NULL"),
        ),
        Index("ix_tasks_running", "org_id", postgresql_where=text("status = 'running'")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="todo")
    assignee_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """An agent assignee. The dispatcher runs the card with this agent profile."""
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """A human assignee. The dispatcher does not run the card."""
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    depends_on: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    """The cards that must be ``done`` before the dispatcher runs this card. Not a foreign key:
    the task service checks that each card is in the same workspace."""
    audience: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The run of an agent that made the card. Not a foreign key: the card outlives the run."""
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The session that the dispatcher started for the card."""
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The run that the dispatcher started for the card."""
    result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskComment(Base):
    """A comment on a task card, by a person or by an agent run (Section 12.4)."""

    __tablename__ = "task_comments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id", "task_id"],
            ["tasks.org_id", "tasks.workspace_id", "tasks.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["org_id", "author_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint(_one_of("author_kind", TASK_AUTHOR_KINDS), name="author_kind"),
        CheckConstraint("char_length(body) BETWEEN 1 AND 4000", name="body_length"),
        Index(None, "org_id", "task_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    author_kind: Mapped[str] = mapped_column(Text, nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The run of an agent comment. Not a foreign key: the comment outlives the run."""
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class GitHubInstallation(Base):
    """An installation of the sama GitHub App that an org connected (Section 15.1).

    One installation belongs to one org. The row has no token: the control plane makes an
    installation token with the key of the App when it needs one.
    """

    __tablename__ = "github_installations"
    __table_args__ = (
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("installation_id"),
        CheckConstraint(_one_of("account_type", GITHUB_ACCOUNT_TYPES), name="account_type"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """The id of the installation on GitHub."""
    account_login: Mapped[str] = mapped_column(Text, nullable=False)
    account_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class GitHubRepo(Base):
    """A repository on the allowlist of an org (Section 15.1). Only these repositories go
    through the sama git proxy and get pull requests."""

    __tablename__ = "github_repos"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "installation_ref"],
            ["github_installations.org_id", "github_installations.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("org_id", "full_name"),
        CheckConstraint("full_name = lower(owner || '/' || name)", name="full_name"),
        CheckConstraint("jsonb_typeof(protected_paths) = 'array'", name="protected_paths"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    installation_ref: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """The ``github_installations`` row that reaches the repository."""
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    """``owner/name`` in lower case. GitHub names do not depend on case."""
    protected_paths: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """The paths of the repository, in addition to the default ones, that a push can change
    only with an approval. A path that ends with ``/`` is a folder."""
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()


class GitHubPushGrant(Base):
    """An approval to push one commit to one ``sama/*`` branch that changes a protected path
    (Section 15.1). A person approves a ``repo_push_approval`` call, and the git proxy uses
    the grant once."""

    __tablename__ = "github_push_grants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "repo_id"], ["github_repos.org_id", "github_repos.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("commit_sha ~ '^[0-9a-f]{40}$'", name="commit_sha"),
        CheckConstraint("ref LIKE 'refs/heads/sama/%'", name="ref"),
        Index(None, "org_id", "repo_id", "ref", "commit_sha"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    repo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuntimeSessionItem(Base):
    """One conversation item of the OpenAI runtime (docs/architecture.md, Section 5.3). The
    runner keeps the items of its session through ``POST /session-items``, so it needs no
    database credential. ``conversation`` names one list of items in the session, and ``seq``
    is the position of the item in that list, from 0 with no gaps. Only the control plane
    writes the table."""

    __tablename__ = "runtime_session_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        UniqueConstraint("session_id", "conversation", "seq"),
        CheckConstraint("seq >= 0", name="seq"),
        CheckConstraint("char_length(conversation) BETWEEN 1 AND 100", name="conversation"),
        Index(None, "org_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation: Mapped[str] = mapped_column(Text, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    item: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """One Responses API item, as the OpenAI Agents SDK stores it."""
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The run that wrote the item. No foreign key: a compaction rewrites the items."""
    created_at: Mapped[datetime] = _created_at()


class Job(Base):
    """A scheduled job (docs/architecture.md, Section 12.1). Each change of a field that
    changes what the job does makes a new ``version``. A version that needs a human approval
    fires only when ``approved_version`` is that version and ``approved_hash`` matches it.
    Only the control plane writes the table."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "owner_user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"], ["agent_profiles.org_id", "agent_profiles.id"]
        ),
        ForeignKeyConstraint(["org_id", "session_id"], ["sessions.org_id", "sessions.id"]),
        ForeignKeyConstraint(["org_id", "origin_session_id"], ["sessions.org_id", "sessions.id"]),
        ForeignKeyConstraint(["org_id", "approved_by_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint(_one_of("status", JOB_STATUSES), name="status"),
        CheckConstraint(_one_of("mode", JOB_MODES), name="mode"),
        CheckConstraint(_one_of("created_by_kind", JOB_CREATED_BY), name="created_by_kind"),
        CheckConstraint("(mode = 'script') = (script IS NOT NULL)", name="script"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("jsonb_typeof(schedule) = 'object'", name="schedule_is_object"),
        CheckConstraint("jsonb_typeof(skills) = 'array'", name="skills_is_array"),
        CheckConstraint(
            "jsonb_typeof(preapproved_tools) = 'array'", name="preapproved_tools_is_array"
        ),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint(
            "approved_version IS NULL OR approved_version BETWEEN 1 AND version",
            name="approved_version",
        ),
        CheckConstraint(
            "(approved_version IS NULL) = (approved_hash IS NULL)", name="approved_has_hash"
        ),
        CheckConstraint("consecutive_failures >= 0", name="consecutive_failures"),
        Index(None, "org_id", "owner_user_id"),
        Index("ix_jobs_due", "next_fire_at", postgresql_where=text("status = 'active'")),
        Index(
            "uq_jobs_curator",
            "org_id",
            "workspace_id",
            unique=True,
            postgresql_where=text("mode = 'curator'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    schedule: Mapped[dict[str, Any]] = _json()
    """``{"kind": "cron" | "interval" | "once", "expr": ..., "tz": ...}``."""
    prompt: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    skills: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    model: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="agent")
    script: Mapped[str | None] = mapped_column(Text)
    deliver: Mapped[str] = mapped_column(Text, nullable=False, server_default="origin")
    failure_deliver: Mapped[str | None] = mapped_column(Text)
    preapproved_tools: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    attach_to_session: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The long-lived session of a job with ``attach_to_session``."""
    origin_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The session that created the job. The target ``origin`` is its chat."""
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    approved_version: Mapped[int | None] = mapped_column(Integer)
    approved_hash: Mapped[str | None] = mapped_column(Text)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    paused_reason: Mapped[str | None] = mapped_column(Text)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the scheduler sent the alert of the last pause. It sends one alert per pause."""
    created_by_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="user")
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class JobFire(Base):
    """One fire of a job. ``(job_id, scheduled_for)`` is unique, so a fire runs once, also
    when two schedulers tick at the same time (Section 12.1)."""

    __tablename__ = "job_fires"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("job_id", "scheduled_for"),
        ForeignKeyConstraint(["org_id", "job_id"], ["jobs.org_id", "jobs.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "run_id"], ["runs.org_id", "runs.id"]),
        CheckConstraint(_one_of("status", FIRE_STATUSES), name="status"),
        CheckConstraint("job_version >= 1", name="job_version"),
        Index(None, "run_id"),
        Index(None, "org_id", "job_id", "created_at"),
        Index("ix_job_fires_running", "created_at", postgresql_where=text("status = 'running'")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    manual: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    deliver: Mapped[str] = mapped_column(Text, nullable=False, server_default="none")
    """The result target of the job version that started the fire. A later change of the job
    does not change where the result of this fire goes."""
    failure_deliver: Mapped[str | None] = mapped_column(Text)
    script: Mapped[str | None] = mapped_column(Text)
    """The script of the job version that started the fire. A script fire runs this text, not
    the text of a later version that has no approval."""
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    output: Mapped[str | None] = mapped_column(Text)
    """The redacted result of the fire."""
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the work of the fire started. A script fire waits with ``NULL`` until one scheduler
    claims it, so the script runs once."""
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Delivery(Base):
    """One message that a job sends, with its receipt (Section 12.1). The scheduler writes
    ``sending`` before it calls the provider. A ``sending`` row whose lease expired has an
    unknown result: it becomes ``unknown`` and is never sent again."""

    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("org_id", "fire_id", "kind"),
        ForeignKeyConstraint(["org_id", "job_id"], ["jobs.org_id", "jobs.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["org_id", "fire_id"], ["job_fires.org_id", "job_fires.id"], ondelete="CASCADE"
        ),
        CheckConstraint(_one_of("kind", DELIVERY_KINDS), name="kind"),
        CheckConstraint(_one_of("status", DELIVERY_STATUSES), name="status"),
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint("(status = 'sending') = (lease_expires_at IS NOT NULL)", name="lease"),
        Index(None, "org_id", "job_id", "created_at"),
        Index("ix_deliveries_due", "next_attempt_at", postgresql_where=text("status = 'pending'")),
        Index(
            "ix_deliveries_sending",
            "lease_expires_at",
            postgresql_where=text("status = 'sending'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fire_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    """The redacted message."""
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = _created_at()
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Trigger(Base):
    """An event source that starts a run or a workflow (docs/architecture.md, Section 12.2).

    A ``webhook`` trigger has its own URL, and its signing secret is in the vault under the name
    ``trigger:<id>``. A ``composio`` trigger is a Composio trigger instance on the account of
    ``connection_id``. With the target ``run``, each event starts an unattended run in a new
    private session of ``owner_user_id`` with only ``toolsets``. With the target ``workflow``,
    each event starts an instance of ``workflow_id`` (M4.5), limited to ``toolsets``. Only the
    control plane writes the table."""

    __tablename__ = "triggers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "owner_user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "connection_id"],
            ["connections.org_id", "connections.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        ForeignKeyConstraint(
            ["org_id", "workflow_id"],
            ["workflows.org_id", "workflows.id"],
            name="fk_triggers_workflow",
            ondelete="CASCADE",
        ),
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("composio_trigger_id"),
        CheckConstraint(_one_of("kind", TRIGGER_KINDS), name="kind"),
        CheckConstraint(_one_of("status", TRIGGER_STATUSES), name="status"),
        CheckConstraint(_one_of("target", TRIGGER_TARGETS), name="target"),
        CheckConstraint(
            "(target = 'workflow') = (workflow_id IS NOT NULL)", name="workflow_target"
        ),
        CheckConstraint("char_length(name) BETWEEN 1 AND 80", name="name"),
        CheckConstraint("char_length(prompt) BETWEEN 1 AND 8000", name="prompt"),
        CheckConstraint("jsonb_typeof(toolsets) = 'array'", name="toolsets_array"),
        CheckConstraint(
            "(kind = 'composio') = (connection_id IS NOT NULL AND composio_slug IS NOT NULL)",
            name="composio_fields",
        ),
        CheckConstraint(
            "kind = 'composio' OR composio_trigger_id IS NULL", name="composio_trigger_id"
        ),
        CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 500", name="last_error_length"
        ),
        Index(None, "org_id", "workspace_id"),
        Index(None, "workflow_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """The user that the runs act for. Each event starts a private session of this user."""
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    """The instruction of the run. The event payload follows it as untrusted data."""
    toolsets: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("""'["web", "media", "clarify"]'::jsonb""")
    )
    """The toolsets of the runs. The default is the ``safe`` preset (Section 10.2)."""
    target: Mapped[str] = mapped_column(Text, nullable=False, server_default="run")
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """For the target ``workflow``: the workflow that each event starts (M4.5)."""
    connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """For a ``composio`` trigger: the connection whose account has the trigger instance."""
    composio_trigger_id: Mapped[str | None] = mapped_column(Text)
    """The id of the Composio trigger instance. An event names it in ``metadata.trigger_id``."""
    composio_slug: Mapped[str | None] = mapped_column(Text)
    """The Composio trigger type, for example ``GMAIL_NEW_GMAIL_MESSAGE``."""
    composio_config: Mapped[dict[str, Any]] = _json()
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class TriggerEvent(Base):
    """One accepted event of a trigger and the run or the workflow instance that it started
    (Section 12.2).

    The unique ``(trigger_id, delivery_id)`` rejects a replay of the same delivery. Only the
    control plane writes the table."""

    __tablename__ = "trigger_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "trigger_id"], ["triggers.org_id", "triggers.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "workflow_instance_id"],
            ["workflow_instances.org_id", "workflow_instances.id"],
            name="fk_trigger_events_workflow_instance",
            ondelete="CASCADE",
        ),
        UniqueConstraint("trigger_id", "delivery_id"),
        CheckConstraint("char_length(delivery_id) BETWEEN 1 AND 200", name="delivery_id"),
        CheckConstraint(
            "(run_id IS NULL) = (session_id IS NULL)"
            " AND (run_id IS NULL) <> (workflow_instance_id IS NULL)",
            name="one_target",
        ),
        Index(None, "org_id", "session_id"),
        Index(None, "workflow_instance_id"),
        Index(None, "received_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    trigger_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    delivery_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The ``webhook-id`` of the delivery. A sender sends the same id again on a retry."""
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workflow_instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The workflow instance of an event of a ``workflow`` trigger (M4.5). Such an event has no
    session and no run."""
    received_at: Mapped[datetime] = _created_at()


class TriggerDelivery(Base):
    """One delivery to a trigger, accepted or not (docs/architecture.md, Section 12.2, M7.10).

    The web app shows the rows as the delivery log of the trigger. ``trigger_events`` stays the
    table of the accepted events and its unique key rejects a replay. A row has no payload. At
    most ``MAX_DELIVERIES_PER_TRIGGER`` rows stay for each trigger. A user who sees the
    workspace of the trigger reads the rows. Only the control plane writes the table."""

    __tablename__ = "trigger_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "trigger_id"], ["triggers.org_id", "triggers.id"], ondelete="CASCADE"
        ),
        CheckConstraint(_one_of("outcome", TRIGGER_OUTCOMES), name="outcome"),
        CheckConstraint(
            "delivery_id IS NULL OR char_length(delivery_id) <= 200", name="delivery_id_length"
        ),
        CheckConstraint("error IS NULL OR char_length(error) <= 500", name="error_length"),
        Index(
            "ix_trigger_deliveries_trigger_id_received_at", "trigger_id", text("received_at DESC")
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    trigger_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    delivery_id: Mapped[str | None] = mapped_column(Text)
    """The ``webhook-id`` of the delivery, if the sender gave one."""
    received_at: Mapped[datetime] = _created_at()
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    """A short text without a secret. ``None`` for an accepted delivery."""
    test: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    """True for a test send from the web app."""
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workflow_instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class Workflow(Base):
    """A durable workflow (docs/architecture.md, Section 12.3). Each change of the YAML makes a
    new ``version`` in ``workflow_versions``. Only the control plane writes the table."""

    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "owner_user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"], ["agent_profiles.org_id", "agent_profiles.id"]
        ),
        ForeignKeyConstraint(["org_id", "origin_session_id"], ["sessions.org_id", "sessions.id"]),
        CheckConstraint(_one_of("status", WORKFLOW_STATUSES), name="status"),
        CheckConstraint(_one_of("created_by_kind", JOB_CREATORS), name="created_by_kind"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
        CheckConstraint("version >= 1", name="version"),
        Index(None, "org_id", "owner_user_id"),
        Index(
            "ix_workflows_owner_name",
            "org_id",
            "owner_user_id",
            "name",
            unique=True,
            postgresql_where=text("status <> 'removed'"),
        ),
        Index("ix_workflows_due", "next_fire_at", postgresql_where=text("status = 'active'")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    """The latest version. A new instance uses it."""
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    origin_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="user")
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class WorkflowVersion(Base):
    """One version of a workflow: the YAML text, the checked definition, and its hash. A
    version that needs approval runs only when ``approved_hash`` is ``hash``."""

    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("org_id", "workflow_id", "version"),
        ForeignKeyConstraint(
            ["org_id", "workflow_id"], ["workflows.org_id", "workflows.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "approved_by_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("jsonb_typeof(definition) = 'object'", name="definition_is_object"),
        CheckConstraint(_one_of("created_by_kind", JOB_CREATORS), name="created_by_kind"),
        CheckConstraint(
            "(approved_hash IS NULL) = (approved_at IS NULL)", name="approved_has_time"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    hash: Mapped[str] = mapped_column(Text, nullable=False)
    needs_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    approved_hash: Mapped[str | None] = mapped_column(Text)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="user")
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()


class WorkflowInstance(Base):
    """One run of a workflow. ``(workflow_id, scheduled_for)`` is unique, so a schedule fire
    starts one instance. The version stays the same for the whole instance."""

    __tablename__ = "workflow_instances"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("workflow_id", "scheduled_for"),
        ForeignKeyConstraint(
            ["org_id", "workflow_id"], ["workflows.org_id", "workflows.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "workflow_id", "version"],
            [
                "workflow_versions.org_id",
                "workflow_versions.workflow_id",
                "workflow_versions.version",
            ],
            name="fk_workflow_instances_version",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "owner_user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_profile_id"], ["agent_profiles.org_id", "agent_profiles.id"]
        ),
        ForeignKeyConstraint(
            ["org_id", "parent_instance_id"],
            ["workflow_instances.org_id", "workflow_instances.id"],
            name="fk_workflow_instances_parent",
            ondelete="CASCADE",
        ),
        CheckConstraint(_one_of("status", WORKFLOW_INSTANCE_STATUSES), name="status"),
        CheckConstraint(_one_of("trigger", WORKFLOW_TRIGGERS), name="trigger"),
        CheckConstraint(_one_of("started_by_kind", ACTOR_KINDS), name="started_by_kind"),
        CheckConstraint("depth BETWEEN 0 AND 3", name="depth"),
        CheckConstraint("jsonb_typeof(inputs) = 'object'", name="inputs_is_object"),
        CheckConstraint(
            "toolsets IS NULL OR jsonb_typeof(toolsets) = 'array'", name="toolsets_array"
        ),
        CheckConstraint(
            "(parent_instance_id IS NULL) = (parent_step_id IS NULL)", name="parent_has_step"
        ),
        Index(None, "org_id", "workflow_id", "created_at"),
        Index(None, "org_id", "owner_user_id", "created_at"),
        Index(None, "parent_instance_id"),
        Index(
            "ix_workflow_instances_open",
            "checked_at",
            postgresql_where=text("status IN ('running', 'waiting')"),
        ),
        Index(
            "ix_workflow_instances_trigger_starts",
            "org_id",
            "created_at",
            postgresql_where=text("trigger = 'trigger'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    inputs: Mapped[dict[str, Any]] = _json()
    output: Mapped[Any] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    parent_instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    parent_step_id: Mapped[str | None] = mapped_column(Text)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    trigger: Mapped[str] = mapped_column(Text, nullable=False, server_default="manual")
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_by_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="user")
    started_by_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    toolsets: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    """The toolset limit of the agent and tool steps: the toolsets of the trigger that started
    the instance (Section 12.2). Null: no limit beyond the profile. A child keeps the limit."""
    checked_at: Mapped[datetime] = _created_at()
    """When the engine last advanced the instance. The engine advances the oldest first."""
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowStep(Base):
    """The durable state of one step of an instance. ``(instance_id, step_id)`` is unique."""

    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("instance_id", "step_id"),
        ForeignKeyConstraint(
            ["org_id", "instance_id"],
            ["workflow_instances.org_id", "workflow_instances.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["org_id", "session_id"], ["sessions.org_id", "sessions.id"]),
        ForeignKeyConstraint(["org_id", "run_id"], ["runs.org_id", "runs.id"]),
        ForeignKeyConstraint(
            ["org_id", "child_instance_id"],
            ["workflow_instances.org_id", "workflow_instances.id"],
        ),
        ForeignKeyConstraint(["org_id", "resolved_by_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint(_one_of("kind", WORKFLOW_STEP_KINDS), name="kind"),
        CheckConstraint(_one_of("status", WORKFLOW_STEP_STATUSES), name="status"),
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint("char_length(step_id) BETWEEN 1 AND 64", name="step_id_length"),
        Index(None, "run_id"),
        Index(None, "child_instance_id"),
        Index(
            "ix_workflow_steps_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending' AND kind = 'tool'"),
        ),
        Index(
            "ix_workflow_steps_leased",
            "lease_expires_at",
            postgresql_where=text("status = 'running' AND kind = 'tool'"),
        ),
        Index(
            "ix_workflow_steps_reconcile",
            "created_at",
            postgresql_where=text("status = 'needs_reconcile'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    step_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_step_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    input: Mapped[Any] = mapped_column(JSONB, nullable=True)
    """The rendered arguments of a tool step, or the rendered prompt of an agent step."""
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = _created_at()
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_id: Mapped[str | None] = mapped_column(Text)
    output: Mapped[Any] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    child_instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    wake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event: Mapped[str | None] = mapped_column(Text)
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowReceipt(Base):
    """The receipt of one external call of a tool step. Sama writes ``intent`` before the call
    and ``done``, ``failed``, or ``unknown`` after it."""

    __tablename__ = "workflow_receipts"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("step_row_id", "attempt"),
        ForeignKeyConstraint(
            ["org_id", "instance_id"],
            ["workflow_instances.org_id", "workflow_instances.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "step_row_id"],
            ["workflow_steps.org_id", "workflow_steps.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(_one_of("status", RECEIPT_STATUSES), name="status"),
        CheckConstraint("attempt >= 1", name="attempt"),
        Index(None, "org_id", "instance_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    step_row_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    step_id: Mapped[str] = mapped_column(Text, nullable=False)
    operation_id: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="intent")
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    request_digest: Mapped[str] = mapped_column(Text, nullable=False)
    """The SHA-256 of the arguments. The receipt keeps no arguments, which can hold data."""
    response: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunSteer(Base):
    """A note of ``/steer`` for a running run (docs/architecture.md, Section 4.3).

    The tool gate gives the pending notes of a run to the runtime at the next tool call of the
    run (``delivered``). When the run completes before that, the notes go to the session as
    one new message (``requeued``, with the run that answers it). A run that fails or stops
    drops its pending notes (``dropped``). Only the control plane writes the table."""

    __tablename__ = "run_steers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["org_id", "user_id"], ["users.org_id", "users.id"]),
        CheckConstraint(_one_of("status", STEER_STATUSES), name="status"),
        CheckConstraint(f"char_length(text) BETWEEN 1 AND {MAX_STEER_CHARS}", name="text_length"),
        CheckConstraint("(status = 'delivered') = (delivered_at IS NOT NULL)", name="delivered"),
        CheckConstraint("(status = 'requeued') = (requeued_run_id IS NOT NULL)", name="requeued"),
        Index(None, "org_id", "run_id", "created_at"),
        Index(None, "run_id", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    """The redacted text of the note."""
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requeued_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The run of the message that took the note after its run completed."""
    created_at: Mapped[datetime] = _created_at()


class SessionHandoff(Base):
    """One ``/handoff`` of a private session to another channel (docs/architecture.md,
    Section 11.1). Only the follower of the newest handoff of a run sends the output of the
    run. Only the control plane writes the table."""

    __tablename__ = "session_handoffs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id", "session_id"],
            ["sessions.org_id", "sessions.workspace_id", "sessions.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["org_id", "user_id"], ["users.org_id", "users.id"]),
        CheckConstraint(_one_of("from_channel", CHANNELS), name="from_channel"),
        CheckConstraint(_one_of("to_channel", CHANNELS), name="to_channel"),
        Index(None, "org_id", "session_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    from_channel: Mapped[str] = mapped_column(Text, nullable=False)
    from_key: Mapped[str | None] = mapped_column(Text)
    """The binding key of the source chat. Null for the web app."""
    to_channel: Mapped[str] = mapped_column(Text, nullable=False)
    to_key: Mapped[str | None] = mapped_column(Text)
    """The binding key of the target chat. Null for the web app."""
    run_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    """The open runs that the handoff gave to the target chat. Only the follower of the newest
    handoff of a run sends its output (``sama.gateway.handoff.moved_away``)."""
    created_at: Mapped[datetime] = _created_at()


class DataPurge(Base):
    """One cleanup outside Postgres after a delete (docs/architecture.md, Section 17.4): the E2B
    sandboxes of deleted sessions, or the Langfuse traces of deleted runs. The delete writes the
    row in its own transaction, and the purge worker does the cleanup. No foreign key to
    ``orgs``: the row stays after an org delete."""

    __tablename__ = "data_purges"
    __table_args__ = (
        CheckConstraint(_one_of("kind", PURGE_KINDS), name="kind"),
        Index(None, "next_attempt_at", postgresql_where=text("done_at IS NULL")),
        Index(None, "org_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    refs: Mapped[dict[str, Any]] = _json()
    """``sandboxes``: ``{"session_ids": [...]}`` or ``{"all": true}``. ``traces``:
    ``{"trace_ids": [...]}``. ``browser_contexts``: ``{"context_ids": [...]}`` (Browserbase
    context ids, M5.1)."""
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = _created_at()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PersistentBrowserContext(Base):
    """The persistent browser context of one user for one browser target (docs/architecture.md,
    Section 14.1). ``provider_context_id`` is the id of the context at the provider, for
    example a Browserbase context. The provider keeps the cookies: the row has no secret. A
    member reads only their own rows. Only the control plane writes the table."""

    __tablename__ = "browser_contexts"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", "target"),
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        CheckConstraint(_one_of("target", BROWSER_TARGETS), name="target"),
        CheckConstraint(
            "char_length(provider_context_id) BETWEEN 1 AND 200", name="provider_context_id_length"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    provider_context_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BrowserLogin(Base):
    """A saved login of the browser vault (docs/architecture.md, Section 14.1, M5.3). The user
    name and the password are in the vault (``browser_login:<id>``), never in this row. A login
    with ``user_id`` is personal: only that user can use it. A login without ``user_id`` is for
    every member of the org. ``site`` is the origin where sama fills the login (``https://host``
    or ``https://host:port``). Members read the org logins and their own logins. Only the control
    plane writes the table."""

    __tablename__ = "browser_logins"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint("char_length(name) BETWEEN 1 AND 80", name="name_length"),
        CheckConstraint("char_length(site) BETWEEN 1 AND 300", name="site_length"),
        Index(
            "uq_browser_logins_org_name",
            "org_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
        Index(
            "uq_browser_logins_user_name",
            "org_id",
            "user_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    site: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BrowserUrlPolicy(Base):
    """The URL policy of the browser of one org (docs/architecture.md, Section 16.4, M5.3). With
    ``allow_hosts``, the browser opens only these hosts and their subdomains. ``block_hosts`` and
    their subdomains are always refused. The private address rules apply in all cases. An org
    admin reads the row. Only the control plane writes the table."""

    __tablename__ = "browser_url_policies"
    __table_args__ = (
        ForeignKeyConstraint(["org_id", "updated_by_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint("cardinality(allow_hosts) <= 200", name="allow_hosts_count"),
        CheckConstraint("cardinality(block_hosts) <= 200", name="block_hosts_count"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    allow_hosts: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    block_hosts: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApiKey(Base):
    """An org API key for sama as a server (docs/architecture.md, Section 10.7, M7.6).

    The key acts as ``user_id`` (the org admin who made it) in one workspace. Only the SHA-256
    hash of the key is here. Org admins read the rows of their org, but not ``key_hash``. Only
    the control plane writes the table."""

    __tablename__ = "api_keys"
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
        ForeignKeyConstraint(["org_id", "revoked_by_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint("char_length(name) BETWEEN 1 AND 80", name="name_length"),
        CheckConstraint("octet_length(key_hash) = 32", name="key_hash_length"),
        CheckConstraint(
            "cardinality(scopes) BETWEEN 1 AND 2 AND scopes <@ ARRAY['chat', 'mcp']::text[]",
            name="scopes",
        ),
        CheckConstraint(_one_of("approvals", API_KEY_APPROVALS), name="approvals"),
        Index(None, "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """The user that the key acts as: the org admin who made it."""
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """The workspace of the sessions of the key."""
    agent_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The only agent profile of the key, or null for each profile of the workspace."""
    name: Mapped[str] = mapped_column(Text, nullable=False)
    prefix: Mapped[str] = mapped_column(Text, nullable=False)
    """The first characters of the key, to recognise it in a list. Not a secret."""
    key_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    approvals: Mapped[str] = mapped_column(Text, nullable=False, server_default="deny")
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


LIBRARY_AGENT_SCOPES: Final = ("org", "workspace", "user")
LIBRARY_AGENT_SOURCES: Final = ("create", "edit", "revert")


class LibraryAgent(Base):
    """A sub-agent of the library (docs/architecture.md, Section 13.3, M7.4).

    An org agent is for every member of the org, a workspace agent for the members of one
    workspace, and a ``user`` (personal) agent for one user. ``version`` is the current version
    in ``library_agent_versions``. An agent profile names the agent in ``library_subagents``.
    Members read the agents that they can see (RLS). Only the control plane writes the table.
    """

    __tablename__ = "library_agents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        ForeignKeyConstraint(["org_id", "updated_by_user_id"], ["users.org_id", "users.id"]),
        UniqueConstraint("org_id", "id"),
        UniqueConstraint(
            "org_id", "scope", "workspace_id", "user_id", "name", postgresql_nulls_not_distinct=True
        ),
        CheckConstraint(_one_of("scope", LIBRARY_AGENT_SCOPES), name="scope"),
        CheckConstraint(
            "(scope = 'org' AND workspace_id IS NULL AND user_id IS NULL)"
            " OR (scope = 'workspace' AND workspace_id IS NOT NULL AND user_id IS NULL)"
            " OR (scope = 'user' AND workspace_id IS NULL AND user_id IS NOT NULL)",
            name="scope_columns",
        ),
        CheckConstraint("char_length(name) BETWEEN 1 AND 40", name="name_length"),
        CheckConstraint("char_length(description) BETWEEN 1 AND 300", name="description_length"),
        CheckConstraint("char_length(prompt) BETWEEN 1 AND 8000", name="prompt_length"),
        CheckConstraint("jsonb_typeof(toolsets) = 'array'", name="toolsets_is_array"),
        CheckConstraint("max_turns IS NULL OR max_turns BETWEEN 1 AND 50", name="max_turns"),
        CheckConstraint(
            "max_cost_usd IS NULL OR (max_cost_usd > 0 AND max_cost_usd <= 10)", name="max_cost"
        ),
        CheckConstraint("version >= 1", name="version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    toolsets: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    model: Mapped[str | None] = mapped_column(Text)
    """``None``: the model of the run."""
    max_turns: Mapped[int | None] = mapped_column(Integer)
    max_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    """The cost limit of a delegated run of the agent. A native sub-agent shares the budget of
    its run."""
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class LibraryAgentVersion(Base):
    """One version of a library agent. A version never changes (Section 13.3, M7.4).

    ``spec`` has the fields of the agent at that version. ``author_user_id`` is not a key: the
    history outlives a deleted user."""

    __tablename__ = "library_agent_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "agent_id"],
            ["library_agents.org_id", "library_agents.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("org_id", "agent_id", "version"),
        CheckConstraint(_one_of("source", LIBRARY_AGENT_SOURCES), name="source"),
        CheckConstraint("jsonb_typeof(spec) = 'object'", name="spec_is_object"),
        CheckConstraint("version >= 1", name="version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    reverts_version: Mapped[int | None] = mapped_column(Integer)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()


class CustomTool(Base):
    """A custom tool of the org, of a workspace, or of a user (docs/architecture.md, Section
    10.8, M7.3). The row holds the name and the version that runs. The definition is in
    ``custom_tool_versions``. ``current_version`` is null until a version is approved: a new
    shared tool that a member proposes has no live version. The credential of an HTTP tool is
    in the vault (``custom_tool:<id>``), never in a row. Members read the tools of their org,
    of the workspaces that they see, and their own user tools. Only the control plane writes
    the table."""

    __tablename__ = "custom_tools"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        UniqueConstraint("org_id", "id"),
        UniqueConstraint(
            "org_id", "scope", "workspace_id", "user_id", "name", postgresql_nulls_not_distinct=True
        ),
        CheckConstraint(_one_of("scope", CUSTOM_TOOL_SCOPES), name="scope"),
        CheckConstraint(
            "(scope = 'org' AND workspace_id IS NULL AND user_id IS NULL)"
            " OR (scope = 'workspace' AND workspace_id IS NOT NULL AND user_id IS NULL)"
            " OR (scope = 'user' AND workspace_id IS NULL AND user_id IS NOT NULL)",
            name="scope_columns",
        ),
        CheckConstraint("name ~ '^[a-z][a-z0-9_]{1,39}$'", name="name_format"),
        CheckConstraint("current_version IS NULL OR current_version >= 1", name="current_version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    current_version: Mapped[int | None] = mapped_column(Integer)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class CustomToolVersion(Base):
    """One version of a custom tool. A version never changes, but its review does (Section
    10.8). ``config`` holds the HTTP request or the script, never a secret.
    ``author_user_id`` and ``reviewed_by_user_id`` are not foreign keys: the history outlives a
    deleted user."""

    __tablename__ = "custom_tool_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "tool_id"], ["custom_tools.org_id", "custom_tools.id"], ondelete="CASCADE"
        ),
        UniqueConstraint("org_id", "tool_id", "version_no"),
        CheckConstraint("version_no >= 1", name="version_no"),
        CheckConstraint(_one_of("kind", CUSTOM_TOOL_KINDS), name="kind"),
        CheckConstraint(_one_of("policy", CUSTOM_TOOL_POLICIES), name="policy"),
        CheckConstraint(_one_of("change", CUSTOM_TOOL_CHANGES), name="change"),
        CheckConstraint(_one_of("review", CUSTOM_TOOL_REVIEWS), name="review"),
        CheckConstraint("char_length(description) BETWEEN 1 AND 1000", name="description_length"),
        CheckConstraint("char_length(summary) <= 500", name="summary_length"),
        CheckConstraint("jsonb_typeof(input_schema) = 'object'", name="input_schema_object"),
        CheckConstraint("jsonb_typeof(config) = 'object'", name="config_object"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    tool_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict[str, Any]] = _json()
    policy: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = _json()
    change: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    review: Mapped[str] = mapped_column(Text, nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class SkillSettings(Base):
    """The settings of one skill for one workspace (docs/architecture.md, Section 9.8, M7.5).
    The ``config`` block of ``SKILL.md`` declares the keys. ``settings`` maps a key to its
    value. A member who sees the workspace reads the row. Only the control plane writes the
    table."""

    __tablename__ = "skill_settings"
    __table_args__ = (
        UniqueConstraint("org_id", "workspace_id", "skill_name"),
        ForeignKeyConstraint(
            ["org_id", "workspace_id"], ["workspaces.org_id", "workspaces.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "updated_by_user_id"], ["users.org_id", "users.id"]),
        CheckConstraint("jsonb_typeof(settings) = 'object'", name="settings_is_object"),
        CheckConstraint("char_length(skill_name) BETWEEN 1 AND 64", name="skill_name_length"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    skill_name: Mapped[str] = mapped_column(Text, nullable=False)
    settings: Mapped[dict[str, Any]] = _json()
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


SESSION_REPO_STATUSES: Final = ("pending", "ready", "failed")
MAX_SESSION_REPOS: Final = 5
PLAN_STATUSES: Final = ("proposed", "approved", "rejected", "superseded")
MAX_PLAN_CHARS: Final = 20_000
MAX_PLAN_FEEDBACK_CHARS: Final = 4_000


class SessionRepo(Base):
    """A repository that is attached to a session in coding mode (docs/architecture.md,
    Section 15.5, M7.9). A ``pending`` repository clones into the session sandbox when the next
    run of the session starts. Members read the rows of the sessions that they can see. Only
    the control plane writes the table."""

    __tablename__ = "session_repos"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "created_by_user_id"], ["users.org_id", "users.id"]),
        UniqueConstraint("session_id", "folder"),
        CheckConstraint(_one_of("status", SESSION_REPO_STATUSES), name="status"),
        CheckConstraint("(status = 'failed') = (error IS NOT NULL)", name="failed_has_error"),
        CheckConstraint("status <> 'ready' OR commit IS NOT NULL", name="ready_has_commit"),
        Index(None, "org_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str | None] = mapped_column(Text)
    """The branch or tag to clone. ``None`` is the default branch."""
    folder: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    path: Mapped[str | None] = mapped_column(Text)
    proxied: Mapped[bool | None] = mapped_column(Boolean)
    """True when the clone uses the sama git proxy, so the agent can push ``sama/*`` branches."""
    commit: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class SessionPlan(Base):
    """A plan that the agent proposed in plan mode (docs/architecture.md, Section 15.5, M7.9).
    A person approves it (also with an edited text) or rejects it. A new plan of the session
    supersedes the open one. Members read the rows of the sessions that they can see. Only the
    control plane writes the table."""

    __tablename__ = "session_plans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["org_id", "decided_by_user_id"], ["users.org_id", "users.id"]),
        UniqueConstraint("session_id", "version"),
        CheckConstraint(_one_of("status", PLAN_STATUSES), name="status"),
        CheckConstraint(f"char_length(body) BETWEEN 1 AND {MAX_PLAN_CHARS}", name="body_length"),
        CheckConstraint(
            f"char_length(proposed_body) BETWEEN 1 AND {MAX_PLAN_CHARS}",
            name="proposed_body_length",
        ),
        CheckConstraint(
            f"feedback IS NULL OR char_length(feedback) <= {MAX_PLAN_FEEDBACK_CHARS}",
            name="feedback_length",
        ),
        CheckConstraint(
            "(status IN ('approved', 'rejected')) = (decided_at IS NOT NULL)", name="decided"
        ),
        Index(None, "org_id", "session_id", "version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The run that proposed the plan. A plain column, not a foreign key: a plan stays when a
    retention purge removes old runs."""
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    """The plan: the text of the agent, or the edited text of an approve."""
    proposed_body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed")
    edited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    feedback: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class SessionToolAllow(Base):
    """A tool that one user allowed for the rest of one session (docs/architecture.md, Section
    16.1, approval scope ``session``). The tool gate reads it as a rule of the level
    ``session``: it never beats a ``deny`` rule, an org or profile ``ask`` rule, the hardline
    list, or plan mode. It ends with the session. Members read the rows of the sessions that
    they can see. Only the control plane writes the table."""

    __tablename__ = "session_tool_allows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "session_id"], ["sessions.org_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
        UniqueConstraint("session_id", "user_id", "tool"),
        CheckConstraint("length(tool) BETWEEN 1 AND 200", name="tool_length"),
        Index(None, "org_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """The user who approved. The rule applies to the runs that this user starts."""
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    """An exact tool name."""
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """The approval that made the rule. A plain column: the rule stays when a retention purge
    removes the approval."""
    created_at: Mapped[datetime] = _created_at()
