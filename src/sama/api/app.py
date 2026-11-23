"""The sama HTTP API and the minimal web page (docs/architecture.md, Section 11.4).

Every request carries a Supabase Auth access token. The API has two guards for each session:

1. **Code.** It finds the user row of the token in the org of the session and checks the
   memberships with the rules of the RLS helpers (``sama.tenancy.access``).
2. **RLS.** It reads as the Supabase role ``authenticated`` with the claims of the user
   (``sama.db.as_user``), so the database shows only the rows that the user can read.

A session that the user cannot read gets ``404``, the same answer as a session that does not
exist. Writes go through the run queue and the tool gate, which connect as the table owner.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from importlib import resources
from typing import Annotated, Any, ClassVar, Final, Literal, Self
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import Select, and_, delete, exists, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.background import BackgroundTask
from starlette.types import ASGIApp, Lifespan

from sama import tool_policy, workerbots
from sama.api.admin import (
    DEFAULT_AUDIT_LIMIT,
    DEFAULT_CURATOR_REPORTS,
    DEFAULT_USAGE_DAYS,
    MAX_AUDIT_FILTER_CHARS,
    MAX_AUDIT_ID,
    MAX_AUDIT_LIMIT,
    MAX_CURATOR_REPORTS,
    MAX_USAGE_DAYS,
    AuditPage,
    CuratorReports,
    OrgUsage,
    OrgViews,
)
from sama.api.apikeys import router as api_keys_router
from sama.api.auth import AuthError, JwtVerifier, Principal
from sama.api.billing import router as billing_router
from sama.api.browser import BrowserSettings, LoginChange, LoginIn, UrlPolicyIn
from sama.api.chrome import ChromeChannel
from sama.api.coding_mode import Access as CodingAccess
from sama.api.coding_mode import router as coding_router
from sama.api.custom_tools import router as custom_tools_router
from sama.api.events import MAX_MESSAGE_CHARS, SendError, Sent, StreamEvent
from sama.api.history import (
    DEFAULT_RUNS,
    MAX_RUNS,
    ExportFormat,
    RunSummary,
    RunTrace,
    SearchHit,
    SessionHistory,
)
from sama.api.orgs import router as org_router
from sama.api.origins import check_extension_ids, check_origins
from sama.api.skill_editor import (
    ArchivedSkill,
    ReadScope,
    SkillCatalog,
    SkillDetail,
    SkillEditor,
    SkillSettingsOut,
    SkillSettingsWrite,
    SkillState,
    SkillVersionDetail,
    SkillWrite,
    SkillWritten,
    StateAction,
    WritableScope,
)
from sama.api.subagents import (
    LIBRARY_ERRORS,
    LibraryAgentChange,
    LibraryAgentIn,
    LibraryRoutes,
    LibraryTestIn,
    library_http_error,
)
from sama.api.tasks import (
    CommentOut,
    NewComment,
    NewTaskIn,
    TaskBoard,
    TaskEdit,
    TaskList,
    TaskOut,
    TaskStatus,
)
from sama.api.uploads import read_message
from sama.apikeys.store import ApiKeyStore
from sama.billing import BillingService
from sama.coding.agents import CodingAgent, agent_for
from sama.coding.commands import SwitchNotAllowedError, parse_command, set_direct
from sama.coding_mode.service import CodingService
from sama.connectors.chat import return_page
from sama.connectors.service import ConnectionService, ConnectionView, ConnectorError
from sama.context import (
    MAX_PIN_CHARS,
    Pin,
    PinError,
    PinStore,
    ancestors,
    children,
    list_compactions,
)
from sama.customtools.service import CustomToolService
from sama.datacontrols.delete import DataError
from sama.datacontrols.export import ExportFile
from sama.datacontrols.retention import RetentionError
from sama.datacontrols.service import DataControls
from sama.db.engine import as_user
from sama.db.models import (
    MAX_PARALLEL_RUNS_CAP,
    MAX_RETENTION_DAYS,
    MAX_STEER_CHARS,
    AgentProfile,
    Approval,
    Attachment,
    AuditEntry,
    ChannelIdentity,
    CustomTool,
    Integration,
    Membership,
    Message,
    Org,
    ReviewProposal,
    Run,
    RunEvent,
    RunSteer,
    Session,
    Skill,
    SkillVersion,
    User,
    Workspace,
)
from sama.db.models import Connection as ConnectionRow
from sama.gate.service import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalScope,
    ApprovalScopeError,
    GateService,
    allowed_scopes,
)
from sama.gateway.chrome import ChromeHub, PageContext, PageError, page_message
from sama.gateway.handoff import HandoffError, Handoffs
from sama.gateway.pairing import PairingError, create_code
from sama.gateway.whatsapp_providers import WhatsAppCredentials
from sama.github.routes import setup_router
from sama.github.service import MAX_PATH_CHARS, MAX_PROTECTED_PATHS, GitHubService, GitHubSetupError
from sama.integrations.service import IntegrationError, Integrations
from sama.media.generate import GeneratedMediaStore
from sama.media.images import NewAttachment, clean_name, with_markers
from sama.media.store import AttachmentStore
from sama.memory import (
    CAPS,
    MemoryChangeError,
    MemoryDeniedError,
    MemoryScope,
    MemoryService,
    PostgresMemoryStore,
    Writer,
    scope_of,
)
from sama.memory import (
    MAX_REASON_CHARS as MAX_MEMORY_REASON_CHARS,
)
from sama.memory.core import MAX_ENTRY_CHARS, Target, used
from sama.memory.store import MemoryState, read_revisions, read_state
from sama.modelkeys import MAX_KEY_CHARS, ModelKeyError, ModelKeys
from sama.modelproxy.prices import Provider
from sama.orchestrator.checkpoints import (
    LIST_LIMIT,
    MAX_LIST_LIMIT,
    CheckpointService,
    CheckpointServiceError,
    restored_text,
)
from sama.orchestrator.prewarm import SessionWarmer, WarmRateLimitedError, WarmStatus
from sama.orchestrator.queue import (
    ACTIVE_STATUSES,
    QueueError,
    QuotaExceededError,
    RateLimitedError,
    RunQueue,
)
from sama.orchestrator.signals import SessionSignals
from sama.orgs import AuthUsersEmails, OrgService, SignupPolicy
from sama.orgs.permissions import (
    MAX_CHANGES as MAX_PERMISSION_CHANGES,
)
from sama.orgs.permissions import (
    PermissionChangeError,
    PermissionDeniedError,
    RolePermissions,
    allowed_keys,
    stored_rows,
)
from sama.orgs.service import OrgError
from sama.profile_changes import MAX_RULES as MAX_POLICY_RULES
from sama.profile_changes import TOOL_MAX_CHARS as MAX_TOOL_NAME_CHARS
from sama.profile_changes import RevertError, StaleChangeError, list_changes, revert_change
from sama.profiles import (
    NAME_MAX_CHARS,
    SOUL_MAX_CHARS,
    NewProfile,
    NotAdminError,
    ProfileChanges,
    ProfileError,
    ProfileLimitError,
    budget_json,
    create_profile,
    profile_options,
    update_profile,
)
from sama.review.apply import ReviewApplier
from sama.review.shown import shown_proposal
from sama.runtime.checkpoints import MAX_HISTORY
from sama.runtime.contract import BUDGET_KEYS, MAX_COMPACT_FOCUS_CHARS, RuntimeKind
from sama.scheduler.jobs import Actor as JobActor
from sama.scheduler.jobs import JobService
from sama.scheduler.jobs import preview as preview_schedule
from sama.scheduler.spec import JobError, JobInput, JobNotFoundError
from sama.search.core import MAX_LIMIT as MAX_SEARCH_LIMIT
from sama.search.core import MAX_QUERY_CHARS
from sama.search.service import SessionSearch
from sama.skills.service import SkillLibrary
from sama.skills.store import PostgresSkillStore
from sama.subagent_library import SubagentLibrary
from sama.subagents import SubagentError, known_models, parse_subagents, supports_subagents
from sama.tenancy.access import Grants, load_grants
from sama.tenancy.regions import (
    OrgInOtherRegion,
    check_here,
    home_regions,
    member_user_id,
    served_here,
)
from sama.tools.clarify import ClarifyBroker, ClarifyError
from sama.triggers import router as trigger_routes
from sama.triggers import service as triggers
from sama.vault.redact import default_redactor, redact
from sama.vault.standalone import VaultUnreadableError, know_the_secrets
from sama.workflows.service import WorkflowService
from sama.workflows.spec import WorkflowError, WorkflowNotFoundError

log = logging.getLogger("sama.api")

MAX_TITLE_CHARS: Final = 200
STEERS_LISTED: Final = 50
"""The most notes that `GET /sessions/{id}/steers` lists (M7.1)."""
MAX_REASON_CHARS: Final = 500
STREAM_BATCH: Final = 500
RECENT_RUNS: Final = 20
ACTIVITY_RUNS: Final = 50
"""``GET /sessions/{id}/activity`` reads the tool events of at most this many newest chat runs."""
ACTIVITY_EVENTS: Final = 3_000
"""The most events in one answer of ``GET /sessions/{id}/activity``. The newest events stay."""
ACTIVITY_TYPES: Final = ("tool_call_started", "tool_call_finished", "subagent_event")
"""The run events that the chat shows for a run that ended (the tool trail)."""
ACTIVITY_TEXT_CHARS: Final = 2_000
"""A text value of an activity event is cut after this many characters."""
STREAM_GRACE_S: Final = 1.0
"""The response of a stream stops this long after the end time of the stream at the latest."""
OPEN_RUN_STATUSES: Final = frozenset({"queued", "running", "paused"})
NOT_FOUND: Final = "the session does not exist"
STATIC: Final = resources.files("sama.api") / "static"


class ApiSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    """The public key of the Supabase project. The web page needs it to sign in."""
    poll_s: float = Field(default=0.5, gt=0)
    """How often an event stream reads new rows."""
    wake_s: float = Field(default=0.05, ge=0)
    """After a signal of the worker of this process (``SessionSignals``), the stream waits
    this long before it reads, so that it reads once for a group of stores."""
    heartbeat_s: float = Field(default=15.0, gt=0)
    max_stream_s: float = Field(default=3600.0, gt=0)
    """A stream ends after this time, or when the access token expires if that is earlier.
    The client connects again with a fresh token and ``Last-Event-ID``."""
    max_streams: int = Field(default=1000, gt=0)
    """The open event streams of this process. One more stream gets ``429``."""
    max_streams_per_user: int = Field(default=8, gt=0)
    """The open event streams of one user in this process."""
    stream_db_slots: int = Field(default=4, gt=0)
    """At most this many event stream polls read the database at the same time. A poll holds a
    database connection for about 6 round trips (0.4 s when the database is 60 ms away). Many
    streams would use the whole pool, and a send or an approval would wait for a connection
    (Section 4.4). A poll that finds no free slot waits in the process, not in the pool."""
    web_origins: tuple[str, ...] = ()
    """The exact origins of the web app (``SAMA_WEB_ORIGINS``). With none, the API sends no CORS
    headers."""
    signup: SignupPolicy = SignupPolicy()
    """Self-serve sign-up (Section 17.2). The default is ``invite_only``."""

    extension_ids: tuple[str, ...] = ()
    """The ids of the Chrome extensions that may open the extension channel
    (``SAMA_EXTENSION_IDS``). With none, every ``chrome-extension://`` origin may open it. The
    channel always needs a valid access token."""

    @field_validator("web_origins")
    @classmethod
    def _origins_are_exact(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return check_origins(value)

    @field_validator("extension_ids")
    @classmethod
    def _extension_ids_are_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return check_extension_ids(value)


# -- request and response bodies ------------------------------------------------------------


MAX_FORM_FIELDS: Final = 20


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Edit(_Body):
    """A body of optional fields to change. It has at least one field, and no field is null,
    except the fields in ``nullable``."""

    nullable: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _has_a_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        for name in sorted(self.model_fields_set):
            if getattr(self, name) is None and name not in self.nullable:
                raise ValueError(f"{name} cannot be null")
        return self


class NewSession(_Body):
    workspace_id: uuid.UUID
    agent_profile_id: uuid.UUID
    audience: Literal["private", "shared"] = "private"
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)


class NewMessage(_Body):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    parallel: bool = False
    """Start a run next to the open runs of the session (``/bg``, M7.1). Without it, the busy
    mode of the profile decides."""


class Steer(_Body):
    text: str = Field(min_length=1, max_length=MAX_STEER_CHARS)


class Handoff(_Body):
    to: Literal["telegram", "slack", "web"]
    """Where the session goes (Section 11.1)."""


class HandoffOut(BaseModel):
    to_channel: str
    text: str
    """The answer for the user."""


class Rollback(_Body):
    number: int = Field(ge=1, le=MAX_HISTORY)
    """The number of a checkpoint in ``GET /sessions/{id}/checkpoints``. 1 is the newest."""
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    """The ``commit`` of the checkpoint in the list. With it, the restore fails (``409``) when
    the checkpoint ``number`` is another checkpoint now (Section 15.5)."""


class CodingAgentBody(_Body):
    agent: CodingAgent | None
    """The coding agent that gets each message of the session (the direct mode, Section 15.4).
    ``null`` ends the direct mode."""


class CodingAgentOut(BaseModel):
    text: str
    """The answer for the user."""


class CheckpointOut(BaseModel):
    number: int
    created_at: datetime
    tool: str
    kind: str
    commit: str
    """The id of the checkpoint. It does not change when newer checkpoints come."""


class RestoreOut(BaseModel):
    created_at: datetime
    """The time of the checkpoint that the files are now equal to."""
    files: int
    deleted: int
    kept: int
    """Large or excluded items that the restore did not replace."""
    text: str
    """The answer for the user."""


class Compact(_Body):
    focus: str = Field(default="", max_length=MAX_COMPACT_FOCUS_CHARS)
    """What the summary must keep. Empty for the default summary."""


class NewPin(_Body):
    """Give ``text`` or ``message_id``, not both."""

    text: str | None = Field(default=None, min_length=1, max_length=MAX_PIN_CHARS)
    message_id: uuid.UUID | None = None


class Decision(_Body):
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)


class Approve(Decision):
    scope: ApprovalScope | None = None
    """How far the approval reaches. ``once``: this call. ``session``: also the same tool for
    the rest of this session, for this user. ``always``: also this tool for this user and this
    agent profile from now on. Empty: ``always`` when ``always_allow`` is true, else ``once``."""
    always_allow: bool = False
    """The old form of ``scope="always"``. ``scope`` wins when both are set."""


class DataSettingsIn(_Body):
    retention_days: int | None = Field(ge=1, le=MAX_RETENTION_DAYS)
    """Sama deletes a session after this many days without activity. Null uses the default of
    the plan (Section 17.4)."""


class NewPairingCode(_Body):
    workspace_id: uuid.UUID
    agent_profile_id: uuid.UUID
    kind: Literal["user", "group"] = "user"
    """``user`` links the chat account of the user. ``group`` links a group chat (Telegram)
    or a channel (Slack)."""


class TelegramConnect(_Body):
    bot_token: SecretStr = Field(min_length=1, max_length=200)
    """The token of the bot from @BotFather."""


class WhatsAppConnect(_Body):
    """The WhatsApp number of the org and the credentials of its provider (Section 11.9)."""

    provider: Literal["meta", "twilio", "gupshup"]
    number: str = Field(min_length=8, max_length=16)
    """The business number in E.164 form, for example ``+15551234567``."""
    api_token: SecretStr = Field(min_length=1, max_length=600)
    """The access token (Meta), the auth token (Twilio), or the API key (Gupshup)."""
    app_secret: SecretStr | None = Field(default=None, max_length=200)
    """The app secret of the Meta app. Meta only."""
    phone_number_id: str | None = Field(default=None, max_length=40)
    """Meta only."""
    account_sid: str | None = Field(default=None, max_length=40)
    """Twilio only."""
    app_id: str | None = Field(default=None, max_length=60)
    """Gupshup only."""
    app_name: str | None = Field(default=None, max_length=100)
    """Gupshup only."""


class EmailConnect(_Body):
    """The inbound address of the org and its SendGrid account (Section 11.9)."""

    address: str = Field(min_length=3, max_length=254)
    api_key: SecretStr = Field(min_length=1, max_length=200)
    """A SendGrid API key with the Mail Send permission."""
    public_key: str = Field(min_length=20, max_length=2000)
    """The public key of the security policy of the Inbound Parse webhook."""
    sender_name: str = Field(default="sama", min_length=1, max_length=60)


class ModelKeyIn(_Body):
    api_key: SecretStr = Field(min_length=1, max_length=MAX_KEY_CHARS)
    """The API key of the provider. It goes to the vault; no answer has it."""


class GitHubRepoAdd(_Body):
    repo: str = Field(min_length=3, max_length=300)
    """``owner/name`` or the URL of the repository."""
    protected_paths: list[str] = Field(default_factory=list, max_length=MAX_PROTECTED_PATHS)
    """More protected paths, after the default ones. A path that ends with ``/`` is a folder."""

    @field_validator("protected_paths")
    @classmethod
    def _short(cls, paths: list[str]) -> list[str]:
        if any(len(path) > MAX_PATH_CHARS for path in paths):
            raise ValueError(f"a protected path has more than {MAX_PATH_CHARS} characters")
        return paths


class ConnectionStart(_Body):
    connector: str = Field(min_length=1, max_length=40)
    scope: Literal["personal", "workspace"] = "personal"


class ConnectionForm(_Body):
    values: dict[str, SecretStr] = Field(max_length=MAX_FORM_FIELDS)
    """The fields of the form of the connector. Each value is secret here: the service keeps
    the non-secret fields in the row and the secret fields in the vault."""


class McpServerAdd(_Body):
    """A remote MCP server that is not in the catalog (Section 10.3)."""

    name: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=9, max_length=2000)
    auth: Literal["oauth", "bearer", "header", "none"]
    header: str | None = Field(default=None, max_length=64)
    """The header of the credential, for ``auth: header``."""
    oauth_scope: str | None = Field(default=None, max_length=400)
    title: str | None = Field(default=None, max_length=60)
    scope: Literal["personal", "workspace"] = "workspace"


class StdioPackage(_Body):
    registry: Literal["npm", "pypi"]
    name: str = Field(min_length=1, max_length=214)
    version: str = Field(min_length=1, max_length=64)
    bin: str | None = Field(default=None, max_length=100)
    args: list[str] = Field(default_factory=list, max_length=32)


class StdioServerAdd(_Body):
    """A stdio MCP server that sama runs in a connector host (Section 10.3.2). It has a pinned
    package, or a custom command (the org owner only)."""

    name: str = Field(min_length=1, max_length=40)
    title: str | None = Field(default=None, max_length=60)
    scope: Literal["personal", "workspace"] = "workspace"
    package: StdioPackage | None = None
    command: list[str] | None = Field(default=None, min_length=1, max_length=32)
    env: list[str] = Field(default_factory=list, max_length=MAX_FORM_FIELDS)
    """The environment variables of the server. Each one is a secret field of the form."""
    egress: list[str] = Field(default_factory=list, max_length=20)
    memory_mb: int | None = None
    idle_s: int | None = None


class ApiEndpoint(_Body):
    name: str | None = Field(default=None, max_length=64)
    method: Literal["GET", "PUT", "POST", "DELETE", "PATCH", "HEAD"] = "GET"
    path: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=1000)
    query: list[str] = Field(default_factory=list, max_length=30)
    body: bool = False


class ApiAuthBody(_Body):
    type: Literal["api_key", "bearer", "basic", "oauth2", "none"]
    name: str | None = Field(default=None, max_length=64)
    location: Literal["header", "query"] = "header"
    token_url: str | None = Field(default=None, max_length=500)
    scope: str | None = Field(default=None, max_length=400)
    client_auth: Literal["basic", "body"] = "basic"


class ApiAdd(_Body):
    """An HTTP API from an OpenAPI 3 document or a list of endpoints (Section 10.3.3)."""

    name: str = Field(min_length=1, max_length=40)
    title: str | None = Field(default=None, max_length=60)
    scope: Literal["personal", "workspace"] = "workspace"
    base_url: str | None = Field(default=None, max_length=500)
    auth: ApiAuthBody
    openapi: str | None = Field(default=None, max_length=2_000_000)
    openapi_url: str | None = Field(default=None, max_length=2000)
    endpoints: list[ApiEndpoint] | None = Field(default=None, max_length=200)
    test: str | None = Field(default=None, max_length=64)


class OAuthCallback(_Body):
    """The query of the redirect of an authorization server to the Connections page."""

    state: str = Field(min_length=10, max_length=200)
    code: str | None = Field(default=None, max_length=4000)
    error: str | None = Field(default=None, max_length=200)


class ToolFilters(_Body):
    include: list[str] = Field(default_factory=list, max_length=50)
    exclude: list[str] = Field(default_factory=list, max_length=50)


class SlackConnect(_Body):
    bot_token: SecretStr = Field(min_length=1, max_length=300)
    """The bot token of the Slack app (``xoxb-``), from OAuth & Permissions."""
    signing_secret: SecretStr = Field(min_length=1, max_length=100)
    """The signing secret of the Slack app, from Basic Information."""


Channel = Literal["telegram", "slack", "whatsapp", "email"]
_APP_NAMES: Final = {
    "telegram": "Telegram bot",
    "slack": "Slack app",
    "whatsapp": "WhatsApp number",
    "email": "email address",
}
_DIRECT_CHANNELS: Final = frozenset({"whatsapp", "email"})
"""Channels without groups: only ``user`` pairing codes (Section 11.9)."""

MAX_MEMORY_ENTRIES: Final = 100


class MemoryEdit(_Body):
    """The new notes of one scope. They replace all entries."""

    entries: list[Annotated[str, Field(max_length=MAX_ENTRY_CHARS)]] = Field(
        max_length=MAX_MEMORY_ENTRIES
    )
    agent_profile_id: uuid.UUID | None = None
    """Needed for the ``agent`` target."""
    reason: str | None = Field(default=None, max_length=MAX_MEMORY_REASON_CHARS)


class MemoryRevert(_Body):
    agent_profile_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=MAX_MEMORY_REASON_CHARS)


_BotName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=workerbots.NAME_MAX_CHARS),
]
_BotChannel = Annotated[str, StringConstraints(pattern=r"^[CG][A-Z0-9]{2,31}$")]
_BotBudget = Annotated[Decimal, Field(gt=0, le=workerbots.MAX_DAILY_BUDGET_USD, decimal_places=4)]


class NewWorkerBot(_Body):
    """A worker bot (Section 11.6): an agent profile on one Slack channel of the org's Slack
    app, with a daily budget in USD."""

    name: _BotName
    agent_profile_id: uuid.UUID
    channel_id: _BotChannel
    daily_budget_usd: _BotBudget


class WorkerBotEdit(_Edit):
    """The fields to change. Every field is optional, and a body has at least one."""

    name: _BotName | None = None
    agent_profile_id: uuid.UUID | None = None
    channel_id: _BotChannel | None = None
    daily_budget_usd: _BotBudget | None = None
    status: Literal["active", "disabled"] | None = None


_TriggerName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=triggers.NAME_MAX_CHARS),
]
_TriggerPrompt = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=triggers.PROMPT_MAX_CHARS),
]
_TriggerToolsets = Annotated[list[Annotated[str, Field(max_length=100)]], Field(max_length=100)]


class NewTriggerBody(_Body):
    """An event trigger (Section 12.2). Without ``toolsets``, the runs get the ``safe`` preset.
    A ``composio`` trigger needs ``connection_id`` and ``composio_slug``. With ``workflow_id``,
    each event starts the workflow (M4.5), and ``prompt`` and ``agent_profile_id`` are optional.
    Without it, each event starts a run, and they are required."""

    kind: Literal["webhook", "composio"]
    name: _TriggerName
    prompt: _TriggerPrompt | None = None
    agent_profile_id: uuid.UUID | None = None
    toolsets: _TriggerToolsets | None = None
    connection_id: uuid.UUID | None = None
    composio_slug: Annotated[str, Field(max_length=100)] | None = None
    composio_config: dict[str, Any] | None = None
    workflow_id: uuid.UUID | None = None


class TriggerEdit(_Edit):
    """The fields to change. Every field is optional, and a body has at least one."""

    name: _TriggerName | None = None
    prompt: _TriggerPrompt | None = None
    toolsets: _TriggerToolsets | None = None
    status: Literal["active", "disabled"] | None = None


ModelKeyProvider = Literal["anthropic", "openai"]
"""The providers of the org model keys (Section 17.3). Gemini is not a runtime provider."""


class ProfileEdit(_Edit):
    """The fields to change. Every field is optional, and a body has at least one."""

    name: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX_CHARS)
        ]
        | None
    ) = None
    soul: Annotated[str, Field(max_length=SOUL_MAX_CHARS)] | None = None
    runtime: RuntimeKind | None = None
    model: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    toolsets: (
        Annotated[list[Annotated[str, Field(max_length=100)]], Field(max_length=100)] | None
    ) = None
    subagents: list[Any] | None = None
    budget: dict[str, Any] | None = None
    """The whole run budget (M7.10). A key that is missing or null is no limit. ``sama.profiles``
    checks the values and the caps of the plan."""
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    """``null`` sets the default of the model. It is the only field that may be null."""
    library_subagents: (
        Annotated[list[Annotated[str, Field(max_length=36)]], Field(max_length=8)] | None
    ) = None
    """The ids of library agents (Section 13.3), in order."""

    delegation_model: Annotated[str, Field(max_length=100)] | None = None
    """The model of the children of ``delegate_task``. The empty text: the model of the
    profile (Section 13.2)."""
    memory_provider: Literal["builtin", "mem0"] | None = None

    nullable: ClassVar[frozenset[str]] = frozenset({"reasoning_effort"})
    busy_mode: Literal["steer", "parallel", "queue"] | None = None
    max_parallel_runs: Annotated[int, Field(ge=1, le=MAX_PARALLEL_RUNS_CAP)] | None = None


class ProfileCreate(_Body):
    """A new agent profile (M6.9). It gets the toolsets of a new org and the run budget of the
    plan. ``workspace_id`` null makes a profile of the whole org."""

    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX_CHARS)
    ]
    soul: Annotated[str, Field(max_length=SOUL_MAX_CHARS)] = ""
    runtime: RuntimeKind | None = None
    model: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    workspace_id: uuid.UUID | None = None


class ToolRuleIn(_Body):
    """One rule of a tool policy change (M7.10). ``action`` null removes the rule."""

    tool: Annotated[str, Field(min_length=1, max_length=MAX_TOOL_NAME_CHARS)]
    action: Literal["allow", "ask", "deny"] | None


class ToolPolicyEdit(_Body):
    rules: Annotated[list[ToolRuleIn], Field(min_length=1, max_length=MAX_POLICY_RULES)]


class TriggerTest(_Body):
    """A test send to a trigger (M7.10). Without ``payload``, the test sends a small event.
    Without ``start``, the test checks the delivery and starts nothing."""

    payload: dict[str, Any] | None = None
    start: bool = False


class RolePermissionsEdit(_Body):
    """The permissions to change for one role (M7.10): a partial map from a key to a boolean."""

    permissions: Annotated[dict[str, bool], Field(min_length=1, max_length=MAX_PERMISSION_CHANGES)]


class Answers(_Body):
    answers: list[str] = Field(min_length=1, max_length=10)


MAX_MODEL_CHARS: Final = 100


class SessionPatch(_Edit):
    """A rename, an archive, or a restore of a session (M7.10). A body has at least one field."""

    title: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TITLE_CHARS),
        ]
        | None
    ) = None
    status: Literal["active", "archived"] | None = None
    model: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MODEL_CHARS)
        ]
        | None
    ) = None
    """The model for the next runs of the session (M8.2). ``null`` goes back to the model of the
    profile. The runtime of the session must know the model."""
    nullable: ClassVar[frozenset[str]] = frozenset({"model"})


class SessionOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    agent_profile_id: uuid.UUID
    audience: str
    title: str | None
    status: str
    created_at: datetime
    lineage: str | None = None
    """``compaction`` for the archive of a transcript, else ``None`` (Section 6.3)."""
    parent_session_id: uuid.UUID | None = None
    coding_agent: str | None = None
    """``claude_code`` or ``codex`` when the session is in the direct mode of a coding agent
    (Section 15.4, M7.2), else ``None``."""
    can_manage: bool = False
    """True when ``PATCH /sessions/{id}`` lets the caller rename, archive, or restore the
    session: the owner of a private session, or the owner or an admin of a shared session."""
    run_state: Literal["running", "waiting", "failed", "done"] | None = None
    """The state of the chat runs (only in ``GET /sessions``, Section 17.8): ``waiting`` when a
    run waits for a person, ``running`` when a run is queued or works, else the end of the
    newest run (``failed`` or ``done``). ``None``: no run yet."""
    last_activity_at: datetime | None = None
    """The start of the newest chat run, or ``None`` (only in ``GET /sessions``)."""
    runtime: str | None = None
    """The runtime of the session. ``GET /sessions/{id}`` and ``PATCH`` fill this field and the
    next three. ``GET /sessions`` and ``POST /sessions`` leave them empty."""
    model: str | None = None
    """The model that the next run of the session uses (M8.2)."""
    default_model: str | None = None
    """The model without a choice of the user: the frozen model, or the model of the profile."""
    model_is_override: bool = False
    """True when a person chose a model that is not the default one."""


class SessionModels(BaseModel):
    """The models of a session (M8.2), for the model picker of the web app."""

    runtime: str
    models: list[str]
    """The model ids that the runtime knows, sorted. Empty for a coding agent."""
    model: str
    default_model: str
    is_override: bool
    editable: bool
    """True when the caller can change the model (``can_manage``) and the runtime lets it."""


JobAction = Literal["pause", "resume", "run", "cancel"]
WorkflowAction = Literal["pause", "resume"]


class WorkflowSource(BaseModel):
    """The YAML text of a workflow (Section 12.3)."""

    model_config = ConfigDict(extra="forbid")

    yaml: str = Field(min_length=1, max_length=50_000)
    workspace_id: uuid.UUID | None = None
    agent_profile_id: uuid.UUID | None = None


class WorkflowApproval(BaseModel):
    """The version that the person saw. An approval of another version fails."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class WorkflowStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any] = Field(default_factory=dict)


class WorkflowSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str = Field(min_length=1, max_length=100)
    data: Any = None


class WorkflowDecision(BaseModel):
    """The decision of the owner for an approval step."""

    model_config = ConfigDict(extra="forbid")

    approve: bool


class WorkflowResolution(BaseModel):
    """The decision of the owner for a tool step whose result is not known: ``done`` (the
    write happened; ``output`` is its output), ``retry``, or ``fail``."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["done", "retry", "fail"]
    output: Any = None


class SchedulePreview(BaseModel):
    """A schedule to check: natural language, a cron line, or a schedule object."""

    model_config = ConfigDict(extra="forbid")

    schedule: Annotated[str, StringConstraints(min_length=1, max_length=300)] | dict[str, Any]
    tz: Annotated[str, StringConstraints(max_length=64)] | None = None


class JobApproval(BaseModel):
    """The version that the person saw. An approval of another version fails."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class PinOut(BaseModel):
    id: uuid.UUID
    text: str
    source: str
    created_at: datetime


class MediaOut(BaseModel):
    """An image or a voice note that a run made (Section 11.8). ``GET .../media/{id}`` gives
    the bytes."""

    id: uuid.UUID
    run_id: uuid.UUID
    kind: str
    mime_type: str
    size_bytes: int
    caption: str
    created_at: datetime


class CompactionOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    trigger: str
    tokens_before: int | None
    tokens_after: int | None
    archive_session_id: uuid.UUID | None
    archive_entries: int
    archive_truncated: bool
    created_at: datetime
    completed_at: datetime | None


class LinkOut(BaseModel):
    session_id: uuid.UUID
    parent_session_id: uuid.UUID | None
    lineage: str | None
    title: str | None
    status: str
    created_at: datetime


class LineageOut(BaseModel):
    ancestors: list[LinkOut]
    """The parents of the session, nearest first."""
    children: list[LinkOut]
    """The sessions that start from this session: archives of compactions, and branches."""


class RunOut(BaseModel):
    id: uuid.UUID
    status: str
    failure_code: str | None
    failure_message: str | None
    created_at: datetime


class SessionDetail(SessionOut):
    runs: list[RunOut]
    """The last runs, newest first."""
    last_event_id: int
    """Give it to the event stream as ``after`` to get only new events."""


# -- access ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionAccess:
    session: Any
    """The ``sessions`` row as read with RLS."""
    user_id: uuid.UUID
    """The user row of the token in the org of the session."""
    grants: Grants | None = None
    """The memberships of the user in the org of the session."""
    open_run: bool | None = None
    """Whether the session had a queued, running, or paused run when the access was read.
    ``None`` when the caller did not ask."""

    @property
    def org_id(self) -> uuid.UUID:
        return self.session.org_id  # type: ignore[no-any-return]


_ADMIN_ONLY: Final = "only an org owner or admin can do this"
_OWNER_ONLY: Final = "only an org owner can do this"
_SELF_OR_OWNER: Final = "only the user or an org owner can do this"
_SELF_OR_ADMIN: Final = "only the user or an org owner or admin can do this"
_BOT_ADMIN_ONLY: Final = "only a workspace admin or an org owner or admin can do this"


class _CorsApp(FastAPI):
    """A ``FastAPI`` app with CORS for ``app.state.web_origins`` as its outermost layer.

    ``add_middleware`` puts a middleware inside ``ServerErrorMiddleware``, so an unhandled error
    would have an answer without the CORS headers, and the browser would hide the status from
    the web app. This wrapper is outside of it: every answer has the same origin policy. The
    web app sends a Bearer token and no cookies, so credentials stay off.
    """

    def build_middleware_stack(self) -> ASGIApp:
        stack = super().build_middleware_stack()
        origins = list(getattr(self.state, "web_origins", ()))
        if not origins:
            return stack
        return CORSMiddleware(
            stack,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
            # The web app reads the file name of an export (M6.9).
            expose_headers=["Content-Disposition"],
            max_age=600,
        )


def _return_page(
    service: ConnectionService, view: ConnectionView | None, via: str, problem: str | None
) -> Response:
    """The end of a sign-in (M8.1). A sign-in from the Connections page goes back there. A
    sign-in from a chat gets a small page: the chat continues by itself."""
    web = service.web_base
    if view is not None and via == "web" and web is not None:
        query = urlencode({"workspace": str(view.workspace_id), "connection": str(view.id)})
        return RedirectResponse(f"{web}/connections?{query}", status_code=303)
    manifest = None if view is None else service.catalog.get(view.connector)
    title = (manifest.title if manifest else None) or (view.connector if view else "The app")
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if view is not None and view.state == "connected":
        page = return_page(
            f"{title} is connected",
            f"{title} is connected",
            "You can close this page and go back to the chat. sama continues your request.",
            link=web,
        )
        return HTMLResponse(page, headers=headers)
    reason = (
        problem or (view.last_error if view is not None else None) or "The sign-in did not finish."
    )
    page = return_page(
        "The sign-in did not finish",
        f"{title} is not connected",
        f"{redact(reason)} Go back to the chat and ask sama to connect it again.",
        link=web,
    )
    return HTMLResponse(page, status_code=400 if view is None else 200, headers=headers)


def _marker_at(text: str, image: dict[str, Any]) -> int:
    """Where the marker line of an image is in the text of its message."""
    found = text.find(str(image["id"]))
    return len(text) if found < 0 else found


def _not_found(what: str = NOT_FOUND) -> HTTPException:
    return HTTPException(status_code=404, detail=what)


async def _user_id(
    connection: AsyncConnection, principal: Principal, org_id: uuid.UUID
) -> uuid.UUID | None:
    """The user of the person in the org. Raises ``OrgInOtherRegion`` (``421``) for an org in a
    region that this deployment does not serve (Section 17.3)."""
    return await member_user_id(connection, principal.auth_user_id, org_id)


_SESSION_COLUMNS: Final = (
    Session.id,
    Session.org_id,
    Session.workspace_id,
    Session.agent_profile_id,
    Session.audience,
    Session.owner_user_id,
    Session.title,
    Session.status,
    Session.created_at,
    Session.lineage,
    Session.parent_session_id,
    Session.coding_agent,
)


def _access_select(
    principal: Principal, *extra: Any, source: Any = None, busy: bool = False
) -> Select[Any]:
    """One statement for everything that an access check reads: the session, the user of the
    person in the org (with the region of the org), and the memberships of the user. Three
    statements cost three round trips, and the database can be 60 ms away (Section 4.4).
    ``source`` is a join that starts at ``sessions``. ``busy`` adds ``open_run``: whether the
    session has a queued, running, or paused run."""
    columns: list[Any] = [
        *_SESSION_COLUMNS,
        User.id.label("member_user_id"),
        Org.region.label("member_region"),
        Membership.workspace_id.label("grant_workspace_id"),
        Membership.role.label("grant_role"),
        *extra,
    ]
    if busy:
        columns.append(
            exists()
            .where(
                Run.org_id == Session.org_id,
                Run.session_id == Session.id,
                Run.status.in_(("queued", *ACTIVE_STATUSES)),
            )
            .label("open_run")
        )
    return (
        select(*columns)
        .select_from(Session if source is None else source)
        .outerjoin(
            User,
            and_(User.org_id == Session.org_id, User.auth_user_id == principal.auth_user_id),
        )
        .outerjoin(Org, Org.id == User.org_id)
        .outerjoin(
            Membership, and_(Membership.org_id == Session.org_id, Membership.user_id == User.id)
        )
    )


def _access_from(rows: Sequence[Any], *, busy: bool = False) -> SessionAccess:
    """The access of the rows of ``_access_select``. Raises ``404`` if the user cannot read
    the session, and ``OrgInOtherRegion`` (``421``) for an org in another region."""
    if not rows:
        raise _not_found()
    row = rows[0]
    if row.member_user_id is None or row.member_region is None:
        raise _not_found()
    check_here(row.member_region)
    grants = Grants(
        row.member_user_id,
        tuple(
            (item.grant_workspace_id, item.grant_role)
            for item in rows
            if item.grant_role is not None
        ),
    )
    if not grants.sees_session(row.workspace_id, row.audience, row.owner_user_id):
        raise _not_found()
    return SessionAccess(
        row, row.member_user_id, grants, open_run=bool(row.open_run) if busy else None
    )


async def session_access(
    connection: AsyncConnection,
    principal: Principal,
    session_id: uuid.UUID,
    *,
    busy: bool = False,
) -> SessionAccess:
    """Checks that the user can read the session, with both guards. ``connection`` must run
    as the user (``as_user``). Raises ``404`` if not. It uses one statement. With ``busy``,
    the access has ``open_run`` (see ``_access_select``)."""
    rows = (
        await connection.execute(
            _access_select(principal, busy=busy).where(Session.id == session_id)
        )
    ).all()
    return _access_from(rows, busy=busy)


def _pin_out(pin: Pin) -> PinOut:
    return PinOut(id=pin.id, text=pin.text, source=pin.source, created_at=pin.created_at)


def _can_change_session(row: Any, grants: Grants) -> bool:
    """The rule of ``PATCH /sessions/{id}``: the owner of the session can. For a shared session,
    an admin of the workspace or of the org can too."""
    return bool(
        row.owner_user_id == grants.user_id
        or (
            row.audience == "shared"
            and (grants.org_admin or grants.admins_workspace(row.workspace_id))
        )
    )


async def _run_states(
    connection: AsyncConnection, session_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[Any, datetime | None]]:
    """The run state and the newest run start of each session (``SessionOut.run_state``).

    One read for the open runs and one for the newest run of each session. Both use the index
    on ``(org_id, session_id)`` of ``runs`` through the session ids."""
    if not session_ids:
        return {}
    scope = (Run.session_id.in_(session_ids), Run.kind == "chat")
    newest = (
        await connection.execute(
            select(Run.session_id, Run.status, Run.created_at)
            .where(*scope)
            .distinct(Run.session_id)
            .order_by(Run.session_id, Run.created_at.desc(), Run.id.desc())
        )
    ).all()
    open_rows = (
        await connection.execute(
            select(Run.session_id, Run.status).where(*scope, Run.status.in_(OPEN_RUN_STATUSES))
        )
    ).all()
    waiting = {row.session_id for row in open_rows if row.status == "paused"}
    working = {row.session_id for row in open_rows}
    states: dict[uuid.UUID, tuple[Any, datetime | None]] = {}
    for row in newest:
        if row.session_id in waiting:
            state = "waiting"
        elif row.session_id in working:
            state = "running"
        elif row.status in ("failed", "error"):
            state = "failed"
        else:
            state = "done"
        states[row.session_id] = (state, row.created_at)
    return states


def _session_out(row: Any, grants: Grants) -> SessionOut:
    out = SessionOut.model_validate(row, from_attributes=True)
    out.can_manage = _can_change_session(row, grants)
    return out


@dataclass(frozen=True, slots=True)
class _ModelState:
    """The model of a session as the next run sees it (Section 5.4, M8.2)."""

    runtime: RuntimeKind
    default: str
    chosen: str | None

    @property
    def effective(self) -> str:
        return self.default if self.chosen is None else self.chosen

    @property
    def is_override(self) -> bool:
        return self.chosen is not None and self.chosen != self.default

    @property
    def selectable(self) -> bool:
        """A coding agent chooses its own model, so the web app cannot choose it."""
        return agent_for(self.runtime) is None

    @property
    def allowed(self) -> list[str]:
        return known_models(self.runtime) if self.selectable else []


async def _model_state(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID
) -> _ModelState | None:
    """The runtime and the models of a session. The frozen config wins over the profile, as it
    does for a run. ``None`` when the session or its profile is not readable."""
    row = (
        await connection.execute(
            select(
                Session.model_override,
                Session.run_config["model"].astext.label("frozen_model"),
                Session.run_config["runtime"].astext.label("frozen_runtime"),
                AgentProfile.model,
                AgentProfile.runtime,
            )
            .join(
                AgentProfile,
                and_(
                    AgentProfile.org_id == Session.org_id,
                    AgentProfile.id == Session.agent_profile_id,
                ),
            )
            .where(Session.org_id == org_id, Session.id == session_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return _ModelState(
        runtime=RuntimeKind(row.frozen_runtime or row.runtime),
        default=row.frozen_model or row.model,
        chosen=row.model_override,
    )


def _with_model(out: SessionOut, state: _ModelState | None) -> SessionOut:
    if state is not None:
        out.runtime = state.runtime.value
        out.model = state.effective
        out.default_model = state.default
        out.model_is_override = state.is_override
    return out


def _sse(event: str, data: Any, event_id: int | None = None) -> str:
    lines = [] if event_id is None else [f"id: {event_id}"]
    lines += [f"event: {event}", f"data: {json.dumps(data, separators=(',', ':'))}"]
    return "\n".join(lines) + "\n\n"


class _EventStream(StreamingResponse):
    """A streaming response with a hard end time that gives back its stream slot at the end.

    The slot goes back also when the client goes away before the first chunk, because the
    release is in the response, not in the generator. The time limit covers the whole
    response, also a database read or a send to a slow client that blocks.
    """

    def __init__(
        self,
        content: AsyncIterator[str],
        *,
        release: Callable[[], None],
        lifetime: float,
        **kwargs: Any,
    ) -> None:
        super().__init__(content, **kwargs)
        self._release = release
        self._lifetime = lifetime

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            async with asyncio.timeout(max(0.0, self._lifetime) + STREAM_GRACE_S):
                await super().__call__(scope, receive, send)
        except TimeoutError:
            log.info("an event stream reached its end time")
        finally:
            self._release()


def _run_out(run: Any) -> dict[str, Any]:
    return {
        "run_id": str(run.id),
        "status": run.status,
        "failure_code": run.failure_code,
        "failure_message": run.failure_message,
    }


def _cursor(after: int | None, last_event_id: str | None) -> int | None:
    if last_event_id:
        try:
            return max(0, int(last_event_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be a number") from None
    return after


async def _principal(request: Request) -> Principal:
    api: SamaApi = request.app.state.sama_api
    return await api.principal(request)


Me = Annotated[Principal, Depends(_principal)]
"""The user of the request. A route with this parameter needs a valid access token."""


def _cut_text(value: Any, limit: int) -> Any:
    """``value`` with each string longer than ``limit`` cut to ``limit`` characters and an
    ellipsis, in nested lists and objects too."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "\u2026"
    if isinstance(value, dict):
        return {key: _cut_text(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_cut_text(item, limit) for item in value]
    return value


def _chat_runs(session_id: uuid.UUID) -> Any:
    """The chat runs of a session. A background review run (Section 9.4) is not part of the
    chat: its runs and events stay out of the run list and the event stream."""
    return select(Run.id).where(Run.session_id == session_id, Run.kind == "chat")


async def _data_call[T](call: Awaitable[T]) -> T:
    """Maps the errors of the data controls to HTTP answers."""
    try:
        return await call
    except DataError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from None
    except RetentionError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except OrgError as error:
        # ``BillingError``: the Stripe subscription did not end, so the org delete stopped.
        raise HTTPException(status_code=error.status_code, detail=str(error)) from None


def _zip(result: ExportFile) -> FileResponse:
    """Sends the export, then deletes the file."""
    return FileResponse(
        str(result.path),
        media_type="application/zip",
        filename=result.filename,
        background=BackgroundTask(result.path.unlink, missing_ok=True),
    )


async def _job_call[T](call: Awaitable[T]) -> T:
    """Maps the errors of the job service to HTTP answers."""
    try:
        return await call
    except JobNotFoundError:
        raise _not_found("the job does not exist") from None
    except JobError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


async def _workflow_call[T](call: Awaitable[T]) -> T:
    """Maps the errors of the workflow service to HTTP answers."""
    try:
        return await call
    except WorkflowNotFoundError as error:
        raise _not_found(str(error)) from None
    except WorkflowError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


# -- the service ----------------------------------------------------------------------------


def _quota_answer(error: QuotaExceededError) -> JSONResponse:
    """``402``: the org or the user used a model quota of the plan (Section 17.5). The web app
    shows the message and a link to the Billing page."""
    return JSONResponse(status_code=402, content={"detail": str(error), "quota": error.quota})


class SamaApi:
    """The API of one control plane process."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        verifier: JwtVerifier,
        queue: RunQueue | None = None,
        gate: GateService | None = None,
        clarify: ClarifyBroker | None = None,
        settings: ApiSettings | None = None,
        integrations: Integrations | None = None,
        memory: MemoryService | None = None,
        skills: SkillLibrary | None = None,
        reviews: ReviewApplier | None = None,
        pins: PinStore | None = None,
        github: GitHubService | None = None,
        connections: ConnectionService | None = None,
        jobs: JobService | None = None,
        triggers: triggers.TriggerService | None = None,
        skill_store: PostgresSkillStore | None = None,
        search: SessionSearch | None = None,
        workflows: WorkflowService | None = None,
        handoffs: Handoffs | None = None,
        orgs: OrgService | None = None,
        checkpoints: CheckpointService | None = None,
        model_keys: ModelKeys | None = None,
        data: DataControls | None = None,
        chrome: ChromeHub | None = None,
        billing: BillingService | None = None,
        browser: BrowserSettings | None = None,
        custom_tools: CustomToolService | None = None,
        coding_mode: CodingService | None = None,
        warmer: SessionWarmer | None = None,
        signals: SessionSignals | None = None,
    ) -> None:
        """Without ``custom_tools``, the custom tool routes answer ``409``. Without
        ``integrations`` the integration routes answer ``409``. Without ``github``,
        the GitHub routes answer ``409``. Without ``handoffs`` the handoff route answers
        ``409``, and without ``checkpoints`` the checkpoint routes answer ``409``. Without
        ``connections`` the connection routes answer ``409``. Without
        ``jobs``, the job routes answer ``409``, and without ``workflows``, the workflow routes
        answer ``409``. Without ``skills``,
        the skill routes answer ``409`` and a message that starts with ``/`` is not a skill
        command. Without ``search``, the session search of the web app uses full-text search
        only. Without ``orgs``, sign-up reads the verified emails from ``auth.users`` and uses
        ``settings.signup``. Without ``model_keys`` (no vault), the model key
        routes answer ``503``. Without ``billing``, billing is off: the billing pages show the
        plan and the quotas, and a purchase answers ``409``. Without ``browser``, the browser
        settings routes answer ``409``. Without ``coding_mode``, the coding mode routes that write
        answer ``409`` (Section 15.5). Without ``warmer``, ``POST /sessions/{id}/warm``
        answers ``{"status": "off"}`` and a new session is not warmed (Section 4.4). With
        ``signals``, the event streams of this process read at once when the worker of this
        process stores events (Section 4.4)."""
        self._engine = engine
        self._warmer = warmer
        self._signals = signals
        self._poll_slots = asyncio.Semaphore((settings or ApiSettings()).stream_db_slots)
        self._coding_mode = coding_mode
        self._browser = browser
        self._api_keys = ApiKeyStore(engine)
        self._library = LibraryRoutes(SubagentLibrary(engine), self._can_manage_agents)
        self._custom_tools = custom_tools
        self._memory = memory or MemoryService(PostgresMemoryStore(engine))
        self._integrations = integrations
        self._github = github
        self._connections = connections
        self._triggers = triggers
        self._handoffs = handoffs
        self._checkpoints = checkpoints
        self._model_keys = model_keys
        self._data = data or DataControls(engine)
        self._role_permissions = RolePermissions(engine)
        self._views = OrgViews(engine)
        self._skills = skills
        self._reviews = reviews
        self._pins = pins or PinStore(engine)
        self._jobs = jobs
        self._workflows = workflows
        self._media = GeneratedMediaStore(engine)
        self._attachments = AttachmentStore(engine)
        self._verifier = verifier
        self._queue = queue or RunQueue(engine)
        self._board = TaskBoard(engine, self._queue)
        self._history = SessionHistory(engine, search or SessionSearch(engine), session_access)
        self._skill_editor = (
            None
            if skills is None
            else SkillEditor(
                engine, skills, skill_store or PostgresSkillStore(engine, skills.builtin)
            )
        )
        self._gate = gate or GateService(engine)
        self._clarify = clarify
        self._settings = settings or ApiSettings()
        self._billing = billing or BillingService(engine, web_origins=self._settings.web_origins)
        self._org_service = orgs or OrgService(
            engine,
            AuthUsersEmails(engine),
            self._settings.signup,
            plan_quotas=lambda: self._billing.enabled,
        )
        self._streams: Counter[uuid.UUID] = Counter()
        self.chrome = chrome or ChromeHub()
        """The open connections of the Chrome extension channel (M5.4). The ``my_chrome``
        browser target uses the same hub (M5.5)."""

    @property
    def settings(self) -> ApiSettings:
        return self._settings

    @property
    def verifier(self) -> JwtVerifier:
        return self._verifier

    async def principal(self, request: Request) -> Principal:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(
                status_code=401,
                detail="sign in first",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return await self._verifier.verify(token.strip())
        except AuthError as error:
            raise HTTPException(
                status_code=401,
                detail=str(error),
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            ) from None

    def app(self, *, lifespan: Lifespan[FastAPI] | None = None) -> FastAPI:
        app = _CorsApp(title="sama", docs_url=None, redoc_url=None, lifespan=lifespan)
        app.state.sama_api = self
        settings = self._settings

        @app.exception_handler(RequestValidationError)
        async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
            # The default answer repeats the input of each error. A body can hold a secret (a
            # bot token, a connection form), so the answer says only where and why.
            detail = [{k: v for k, v in e.items() if k != "input"} for e in error.errors()]
            return JSONResponse({"detail": jsonable_encoder(detail)}, status_code=422)

        @app.exception_handler(ConnectorError)
        async def connector_error(request: Request, error: ConnectorError) -> JSONResponse:
            return JSONResponse({"detail": str(error)}, status_code=error.status_code)

        @app.exception_handler(OrgInOtherRegion)
        async def other_region(request: Request, error: OrgInOtherRegion) -> JSONResponse:
            # 421 Misdirected Request: another deployment serves the org (Section 17.3).
            return JSONResponse(
                {"detail": str(error), "region": error.region, "url": error.url},
                status_code=421,
            )

        @app.middleware("http")
        async def security_headers(request: Request, call_next: Any) -> Response:
            response: Response = await call_next(request)
            connect = "'self'"
            if settings.supabase_url:
                connect += f" {settings.supabase_url.rstrip('/')}"
            response.headers["Content-Security-Policy"] = (
                f"default-src 'self'; connect-src {connect}; img-src 'self' data:; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        app.state.web_origins = settings.web_origins

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/", include_in_schema=False)
        async def page() -> FileResponse:
            return FileResponse(str(STATIC / "index.html"), media_type="text/html")

        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

        @app.get("/config")
        async def config() -> dict[str, Any]:
            return {
                "supabase_url": settings.supabase_url,
                "supabase_anon_key": settings.supabase_anon_key,
                "integrations": self._integrations is not None and self._integrations.enabled,
                "connections": self._connections is not None,
                "signup": self._org_service.policy.mode,
                "billing": self._billing.enabled,
                "custom_tools": self._custom_tools is not None,
            }

        app.include_router(org_router(self._org_service, _principal))
        app.include_router(billing_router(self._billing, _principal))
        app.include_router(api_keys_router(self._api_keys, _principal, self._org_admin))
        app.include_router(
            custom_tools_router(
                self._require_custom_tools, _principal, self._workspace_grants, self._tool_grants
            )
        )
        app.include_router(
            coding_router(
                self._engine, self._coding_mode, self._checkpoints, _principal, self._coding_access
            )
        )

        @app.get("/me")
        async def me(principal: Me) -> dict[str, Any]:
            return {"orgs": await self._orgs(principal)}

        @app.get("/sessions")
        async def list_sessions(
            principal: Me,
            limit: Annotated[int, Query(ge=1, le=100)] = 50,
            status: Literal["active", "archived"] = "active",
        ) -> list[SessionOut]:
            return await self._sessions(principal, limit, status)

        @app.patch("/sessions/{session_id}")
        async def patch_session(
            principal: Me, session_id: uuid.UUID, body: SessionPatch
        ) -> SessionOut:
            return await self._patch_session(principal, session_id, body)

        @app.post("/sessions", status_code=201)
        async def create_session(principal: Me, body: NewSession) -> SessionOut:
            return await self._create_session(principal, body)

        @app.post("/sessions/{session_id}/warm", status_code=202)
        async def warm_session(principal: Me, session_id: uuid.UUID) -> JSONResponse:
            return await self._warm(principal, session_id)

        @app.get("/sessions/{session_id}")
        async def get_session(principal: Me, session_id: uuid.UUID) -> SessionDetail:
            return await self._detail(principal, session_id)

        @app.get("/sessions/{session_id}/models")
        async def session_models(principal: Me, session_id: uuid.UUID) -> SessionModels:
            return await self._session_models(principal, session_id)

        @app.get("/sessions/{session_id}/messages")
        async def list_messages(
            principal: Me,
            session_id: uuid.UUID,
            after: Annotated[int, Query(ge=0)] = 0,
            limit: Annotated[int, Query(ge=1, le=500)] = 200,
        ) -> list[dict[str, Any]]:
            return await self._messages(principal, session_id, after, limit)

        @app.post("/sessions/{session_id}/messages", status_code=202)
        async def send_message(
            principal: Me, session_id: uuid.UUID, body: NewMessage
        ) -> JSONResponse:
            return await self._send(principal, session_id, body.text, body.parallel)

        @app.post("/sessions/{session_id}/messages/images", status_code=202)
        async def send_message_with_images(
            principal: Me, session_id: uuid.UUID, request: Request
        ) -> JSONResponse:
            # The access check comes first: a user who cannot see the session sends no byte.
            await self._access(principal, session_id)
            message = await read_message(request)
            return await self._send(
                principal, session_id, message.text, message.parallel, message.images
            )

        @app.get("/sessions/{session_id}/steers")
        async def list_steers(principal: Me, session_id: uuid.UUID) -> list[dict[str, Any]]:
            return await self._steers(principal, session_id)

        @app.get("/sessions/{session_id}/activity")
        async def session_activity(principal: Me, session_id: uuid.UUID) -> dict[str, Any]:
            return await self._activity(principal, session_id)

        @app.get("/sessions/{session_id}/open-runs")
        async def list_open_runs(principal: Me, session_id: uuid.UUID) -> list[dict[str, Any]]:
            access = await self._access(principal, session_id)
            runs = await self._queue.open_runs(org_id=access.org_id, session_id=session_id)
            return [
                {
                    "number": run.number,
                    "run_id": str(run.run_id),
                    "status": run.status,
                    "side": run.side,
                    "text": run.text,
                }
                for run in runs
            ]

        @app.post("/sessions/{session_id}/steer", status_code=202)
        async def steer(principal: Me, session_id: uuid.UUID, body: Steer) -> JSONResponse:
            return await self._steer(principal, session_id, body.text)

        @app.post("/sessions/{session_id}/handoff")
        async def handoff(principal: Me, session_id: uuid.UUID, body: Handoff) -> HandoffOut:
            return await self._handoff(principal, session_id, body.to)

        @app.get("/sessions/{session_id}/checkpoints")
        async def checkpoints(
            principal: Me,
            session_id: uuid.UUID,
            limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = LIST_LIMIT,
        ) -> list[CheckpointOut]:
            return await self._checkpoint_list(principal, session_id, limit)

        @app.post("/sessions/{session_id}/undo")
        async def undo(principal: Me, session_id: uuid.UUID) -> RestoreOut:
            return await self._restore(principal, session_id, None)

        @app.post("/sessions/{session_id}/coding-agent")
        async def coding_agent(
            principal: Me, session_id: uuid.UUID, body: CodingAgentBody
        ) -> CodingAgentOut:
            access = await self._access(principal, session_id)
            try:
                text = await set_direct(
                    self._engine,
                    access.org_id,
                    session_id,
                    body.agent,
                    user_id=access.user_id,
                    audit=True,
                )
            except SwitchNotAllowedError as error:
                raise HTTPException(status_code=403, detail=str(error)) from None
            return CodingAgentOut(text=text)

        @app.post("/sessions/{session_id}/rollback")
        async def rollback(principal: Me, session_id: uuid.UUID, body: Rollback) -> RestoreOut:
            return await self._restore(principal, session_id, body.number, body.commit)

        @app.post("/sessions/{session_id}/compact", status_code=202)
        async def compact(
            principal: Me, session_id: uuid.UUID, body: Compact | None = None
        ) -> JSONResponse:
            return await self._compact(principal, session_id, body.focus if body else "")

        @app.get("/sessions/{session_id}/compactions")
        async def compactions(
            principal: Me,
            session_id: uuid.UUID,
            limit: Annotated[int, Query(ge=1, le=100)] = 50,
        ) -> list[CompactionOut]:
            return await self._compactions(principal, session_id, limit)

        @app.get("/sessions/{session_id}/lineage")
        async def lineage(principal: Me, session_id: uuid.UUID) -> LineageOut:
            return await self._lineage(principal, session_id)

        @app.get("/sessions/{session_id}/pins")
        async def list_pins(principal: Me, session_id: uuid.UUID) -> list[PinOut]:
            access = await self._access(principal, session_id)
            pins = await self._pins.list(access.org_id, session_id)
            return [
                PinOut(id=p.id, text=p.text, source=p.source, created_at=p.created_at) for p in pins
            ]

        @app.post("/sessions/{session_id}/pins", status_code=201)
        async def add_pin(principal: Me, session_id: uuid.UUID, body: NewPin) -> PinOut:
            return await self._add_pin(principal, session_id, body)

        @app.delete("/sessions/{session_id}/pins/{pin_id}", status_code=204)
        async def remove_pin(principal: Me, session_id: uuid.UUID, pin_id: uuid.UUID) -> Response:
            access = await self._access(principal, session_id)
            if not await self._pins.remove(access.org_id, session_id, pin_id):
                raise _not_found("the pin does not exist")
            return Response(status_code=204)

        @app.get("/sessions/{session_id}/media")
        async def list_media(
            principal: Me,
            session_id: uuid.UUID,
            limit: Annotated[int, Query(ge=1, le=100)] = 50,
        ) -> list[MediaOut]:
            access = await self._access(principal, session_id)
            files = await self._media.list(access.org_id, session_id, limit)
            return [MediaOut(**asdict(media)) for media in files]

        @app.get("/sessions/{session_id}/media/{media_id}")
        async def get_media(principal: Me, session_id: uuid.UUID, media_id: uuid.UUID) -> Response:
            access = await self._access(principal, session_id)
            media = await self._media.load(access.org_id, session_id, media_id)
            if media is None:
                raise _not_found("the file does not exist")
            name = "image.png" if media.kind == "image" else "voice.ogg"
            return Response(
                content=media.data,
                media_type=media.mime_type,
                headers={
                    "Content-Disposition": f'inline; filename="{name}"',
                    "Cache-Control": "private, max-age=3600",
                },
            )

        @app.get("/sessions/{session_id}/attachments/{attachment_id}")
        async def get_attachment(
            principal: Me, session_id: uuid.UUID, attachment_id: uuid.UUID
        ) -> Response:
            access = await self._access(principal, session_id)
            image = await self._attachments.load(
                org_id=access.org_id, session_id=session_id, attachment_id=attachment_id
            )
            if image is None:
                raise _not_found("the image does not exist")
            return Response(
                content=image.data,
                media_type=image.mime_type,
                headers={
                    "Content-Disposition": f'inline; filename="{clean_name(image.name)}"',
                    "Cache-Control": "private, max-age=3600",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        @app.get("/sessions/{session_id}/events")
        async def events(
            principal: Me,
            session_id: uuid.UUID,
            after: Annotated[int | None, Query(ge=0)] = None,
            last_event_id: Annotated[str | None, Header()] = None,
        ) -> StreamingResponse:
            cursor = await self.first_cursor(principal, session_id, _cursor(after, last_event_id))
            lifetime = self._stream_lifetime(principal)
            release = self.open_stream(principal.auth_user_id)
            return _EventStream(
                self._stream(principal, session_id, cursor, lifetime),
                release=release,
                lifetime=lifetime,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        app.add_api_websocket_route("/channels/chrome/ws", ChromeChannel(self).serve)

        @app.post("/runs/{run_id}/cancel")
        async def cancel(principal: Me, run_id: uuid.UUID) -> dict[str, str]:
            return await self._cancel(principal, run_id)

        @app.get("/sessions/{session_id}/approvals")
        async def approvals(principal: Me, session_id: uuid.UUID) -> list[dict[str, Any]]:
            return await self._approvals(principal, session_id)

        @app.get("/workspaces/{workspace_id}/review-proposals")
        async def review_proposals(principal: Me, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
            return await self.review_proposals(principal, workspace_id)

        @app.post("/approvals/{approval_id}/approve")
        async def approve(
            principal: Me, approval_id: uuid.UUID, body: Approve | None = None
        ) -> JSONResponse:
            body = body or Approve()
            return await self._decide(
                principal, approval_id, True, body.always_allow, body.reason, body.scope
            )

        @app.post("/approvals/{approval_id}/deny")
        async def deny(
            principal: Me, approval_id: uuid.UUID, body: Decision | None = None
        ) -> JSONResponse:
            reason = (body or Decision()).reason
            return await self._decide(principal, approval_id, False, False, reason, None)

        @app.get("/sessions/{session_id}/questions")
        async def questions(principal: Me, session_id: uuid.UUID) -> list[dict[str, Any]]:
            return await self._questions(principal, session_id)

        @app.post("/sessions/{session_id}/questions/{request_id}/answer", status_code=204)
        async def answer(
            principal: Me, session_id: uuid.UUID, request_id: str, body: Answers
        ) -> Response:
            await self._answer(principal, session_id, request_id, body.answers)
            return Response(status_code=204)

        @app.post("/orgs/{org_id}/agent-profiles", status_code=201)
        async def create_agent_profile(
            principal: Me, org_id: uuid.UUID, body: ProfileCreate
        ) -> dict[str, Any]:
            return await self._create_profile(principal, org_id, body)

        @app.get("/orgs/{org_id}/role-permissions")
        async def role_permissions(principal: Me, org_id: uuid.UUID) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            return await self._role_permissions.view(org_id, grants)

        @app.put("/orgs/{org_id}/role-permissions/{role}")
        async def set_role_permissions(
            principal: Me, org_id: uuid.UUID, role: str, body: RolePermissionsEdit
        ) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            try:
                return await self._role_permissions.change(org_id, grants, role, body.permissions)
            except PermissionChangeError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None

        @app.get("/orgs/{org_id}/agent-profiles/{profile_id}")
        async def get_agent_profile(
            principal: Me, org_id: uuid.UUID, profile_id: uuid.UUID
        ) -> dict[str, Any]:
            return await self._profile(principal, org_id, profile_id)

        @app.patch("/orgs/{org_id}/agent-profiles/{profile_id}")
        async def edit_agent_profile(
            principal: Me, org_id: uuid.UUID, profile_id: uuid.UUID, body: ProfileEdit
        ) -> dict[str, Any]:
            return await self._edit_profile(principal, org_id, profile_id, body)

        @app.get("/orgs/{org_id}/agent-profiles/{profile_id}/tool-policy")
        async def agent_profile_tool_policy(
            principal: Me, org_id: uuid.UUID, profile_id: uuid.UUID
        ) -> dict[str, Any]:
            return await self._profile_tool_policy(principal, org_id, profile_id)

        @app.put("/orgs/{org_id}/agent-profiles/{profile_id}/tool-policy")
        async def set_agent_profile_tool_policy(
            principal: Me, org_id: uuid.UUID, profile_id: uuid.UUID, body: ToolPolicyEdit
        ) -> dict[str, Any]:
            _, grants = await self._profile_access(principal, org_id, profile_id)
            await self._agents_admin(principal, org_id, grants)
            try:
                await tool_policy.set_profile_rules(
                    self._engine,
                    org_id,
                    profile_id,
                    [(rule.tool, rule.action) for rule in body.rules],
                    actor_user_id=grants.user_id,
                )
            except NotAdminError:
                raise HTTPException(status_code=403, detail=_ADMIN_ONLY) from None
            except ProfileError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None
            except SubagentError as error:
                if str(error) == "the profile does not exist":
                    raise _not_found("the agent profile does not exist") from None
                raise HTTPException(status_code=422, detail=str(error)) from None
            return await self._profile_tool_policy(principal, org_id, profile_id)

        @app.get("/orgs/{org_id}/tool-policy")
        async def org_tool_policy(principal: Me, org_id: uuid.UUID) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            editable = await self._can_manage_agents(org_id, grants)
            return await tool_policy.org_view(self._engine, org_id, editable=editable)

        @app.put("/orgs/{org_id}/tool-policy")
        async def set_org_tool_policy(
            principal: Me, org_id: uuid.UUID, body: ToolPolicyEdit
        ) -> dict[str, Any]:
            grants = await self._agents_admin(principal, org_id)
            try:
                await tool_policy.set_org_rules(
                    self._engine,
                    org_id,
                    [(rule.tool, rule.action) for rule in body.rules],
                    actor_user_id=grants.user_id,
                )
            except NotAdminError:
                raise HTTPException(status_code=403, detail=_ADMIN_ONLY) from None
            except ProfileError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None
            return await tool_policy.org_view(self._engine, org_id, editable=True)

        @app.get("/orgs/{org_id}/agent-profiles/{profile_id}/changes")
        async def agent_profile_changes(
            principal: Me, org_id: uuid.UUID, profile_id: uuid.UUID
        ) -> list[dict[str, Any]]:
            return await self._profile_changes(principal, org_id, profile_id)

        @app.post("/orgs/{org_id}/agent-profiles/{profile_id}/changes/{change_id}/revert")
        async def revert_agent_profile_change(
            principal: Me, org_id: uuid.UUID, profile_id: uuid.UUID, change_id: uuid.UUID
        ) -> dict[str, Any]:
            return await self._revert_profile_change(principal, org_id, profile_id, change_id)

        @app.get("/orgs/{org_id}/integrations")
        async def list_integrations(principal: Me, org_id: uuid.UUID) -> list[dict[str, Any]]:
            await self._org_grants(principal, org_id)
            return [view.public() for view in await self._require_integrations().list(org_id)]

        @app.post("/orgs/{org_id}/integrations/telegram", status_code=201)
        async def connect_telegram(
            principal: Me, org_id: uuid.UUID, body: TelegramConnect
        ) -> dict[str, Any]:
            grants = await self._chat_admin(principal, org_id)
            try:
                view = await self._require_integrations().connect_telegram(
                    org_id, grants.user_id, body.bot_token
                )
            except IntegrationError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            return view.public()

        @app.post("/orgs/{org_id}/integrations/whatsapp", status_code=201)
        async def connect_whatsapp(
            principal: Me, org_id: uuid.UUID, body: WhatsAppConnect
        ) -> dict[str, Any]:
            grants = await self._chat_admin(principal, org_id)
            credentials = WhatsAppCredentials(
                provider=body.provider,
                number=body.number.strip(),
                api_token=SecretStr(body.api_token.get_secret_value().strip()),
                app_secret=(
                    SecretStr(body.app_secret.get_secret_value().strip())
                    if body.app_secret is not None
                    else None
                ),
                phone_number_id=body.phone_number_id,
                account_sid=body.account_sid,
                app_id=body.app_id,
                app_name=body.app_name,
            )
            try:
                setup = await self._require_integrations().connect_whatsapp(
                    org_id, grants.user_id, credentials
                )
            except IntegrationError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            return setup.public()

        @app.post("/orgs/{org_id}/integrations/email", status_code=201)
        async def connect_email(
            principal: Me, org_id: uuid.UUID, body: EmailConnect
        ) -> dict[str, Any]:
            grants = await self._chat_admin(principal, org_id)
            try:
                setup = await self._require_integrations().connect_email(
                    org_id,
                    grants.user_id,
                    address=body.address,
                    api_key=body.api_key,
                    parse_public_key=body.public_key,
                    sender_name=body.sender_name,
                )
            except IntegrationError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            return setup.public()

        @app.get("/orgs/{org_id}/model-keys")
        async def model_keys(principal: Me, org_id: uuid.UUID) -> list[dict[str, Any]]:
            await self._org_admin(principal, org_id)
            return [s.public() for s in await self._require_model_keys().status(org_id)]

        @app.put("/orgs/{org_id}/model-keys/{provider}")
        async def set_model_key(
            principal: Me, org_id: uuid.UUID, provider: ModelKeyProvider, body: ModelKeyIn
        ) -> dict[str, Any]:
            grants = await self._chat_admin(principal, org_id)
            try:
                status = await self._require_model_keys().set(
                    org_id, Provider(provider), body.api_key, grants.user_id
                )
            except ModelKeyError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            return status.public()

        @app.delete("/orgs/{org_id}/model-keys/{provider}", status_code=204)
        async def remove_model_key(
            principal: Me, org_id: uuid.UUID, provider: ModelKeyProvider
        ) -> Response:
            grants = await self._chat_admin(principal, org_id)
            try:
                await self._require_model_keys().remove(org_id, Provider(provider), grants.user_id)
            except ModelKeyError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            return Response(status_code=204)

        @app.get("/orgs/{org_id}/subagents")
        async def library_agents(
            principal: Me, org_id: uuid.UUID, workspace_id: uuid.UUID | None = None
        ) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            return await self._library.catalog(org_id, grants, workspace_id)

        @app.post("/orgs/{org_id}/subagents", status_code=201)
        async def create_library_agent(
            principal: Me, org_id: uuid.UUID, body: LibraryAgentIn
        ) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            await self._know_secrets(org_id)
            return await self._library.create(org_id, grants, body)

        @app.get("/orgs/{org_id}/subagents/{agent_id}")
        async def library_agent(
            principal: Me, org_id: uuid.UUID, agent_id: uuid.UUID
        ) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            return await self._library.get(org_id, grants, agent_id)

        @app.patch("/orgs/{org_id}/subagents/{agent_id}")
        async def edit_library_agent(
            principal: Me, org_id: uuid.UUID, agent_id: uuid.UUID, body: LibraryAgentChange
        ) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            if body.description is not None or body.prompt is not None:
                await self._know_secrets(org_id)
            return await self._library.update(org_id, grants, agent_id, body)

        @app.delete("/orgs/{org_id}/subagents/{agent_id}", status_code=204)
        async def delete_library_agent(
            principal: Me, org_id: uuid.UUID, agent_id: uuid.UUID
        ) -> Response:
            grants = await self._org_grants(principal, org_id)
            await self._library.delete(org_id, grants, agent_id)
            return Response(status_code=204)

        @app.get("/orgs/{org_id}/subagents/{agent_id}/versions")
        async def library_agent_versions(
            principal: Me, org_id: uuid.UUID, agent_id: uuid.UUID
        ) -> list[dict[str, Any]]:
            grants = await self._org_grants(principal, org_id)
            return await self._library.versions(org_id, grants, agent_id)

        @app.post("/orgs/{org_id}/subagents/{agent_id}/versions/{version}/revert")
        async def revert_library_agent(
            principal: Me, org_id: uuid.UUID, agent_id: uuid.UUID, version: int
        ) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            await self._know_secrets(org_id)
            return await self._library.revert(org_id, grants, agent_id, version)

        @app.post("/orgs/{org_id}/subagents/{agent_id}/test", status_code=201)
        async def test_library_agent(
            principal: Me, org_id: uuid.UUID, agent_id: uuid.UUID, body: LibraryTestIn
        ) -> JSONResponse:
            return await self._test_library_agent(principal, org_id, agent_id, body)

        @app.get("/orgs/{org_id}/browser/url-policy")
        async def browser_url_policy(principal: Me, org_id: uuid.UUID) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            return await self._require_browser().policy(org_id, grants)

        @app.put("/orgs/{org_id}/browser/url-policy")
        async def set_browser_url_policy(
            principal: Me, org_id: uuid.UUID, body: UrlPolicyIn
        ) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            return await self._require_browser().set_policy(org_id, grants, body)

        @app.get("/orgs/{org_id}/browser/logins")
        async def browser_logins(principal: Me, org_id: uuid.UUID) -> list[dict[str, Any]]:
            grants = await self._org_grants(principal, org_id)
            return await self._require_browser().logins(org_id, grants)

        @app.post("/orgs/{org_id}/browser/logins", status_code=201)
        async def add_browser_login(
            principal: Me, org_id: uuid.UUID, body: LoginIn
        ) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            await self._permit(principal, org_id, "browser.logins", grants)
            return await self._require_browser().add_login(org_id, grants, body)

        @app.patch("/orgs/{org_id}/browser/logins/{login_id}")
        async def change_browser_login(
            principal: Me, org_id: uuid.UUID, login_id: uuid.UUID, body: LoginChange
        ) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            return await self._require_browser().change_login(org_id, login_id, grants, body)

        @app.delete("/orgs/{org_id}/browser/logins/{login_id}", status_code=204)
        async def remove_browser_login(
            principal: Me, org_id: uuid.UUID, login_id: uuid.UUID
        ) -> Response:
            grants = await self._org_grants(principal, org_id)
            await self._require_browser().remove_login(org_id, login_id, grants)
            return Response(status_code=204)

        @app.post("/orgs/{org_id}/integrations/slack/setup")
        async def setup_slack(principal: Me, org_id: uuid.UUID) -> dict[str, Any]:
            grants = await self._chat_admin(principal, org_id)
            try:
                setup = await self._require_integrations().setup_slack(org_id, grants.user_id)
            except IntegrationError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            return setup.public()

        @app.post("/orgs/{org_id}/integrations/slack", status_code=201)
        async def connect_slack(
            principal: Me, org_id: uuid.UUID, body: SlackConnect
        ) -> dict[str, Any]:
            grants = await self._chat_admin(principal, org_id)
            try:
                view = await self._require_integrations().connect_slack(
                    org_id, grants.user_id, body.bot_token, body.signing_secret
                )
            except IntegrationError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            return view.public()

        @app.post("/orgs/{org_id}/integrations/{kind}/check")
        async def check_integration(
            principal: Me, org_id: uuid.UUID, kind: Channel
        ) -> dict[str, Any]:
            grants = await self._chat_admin(principal, org_id)
            try:
                ok, detail, view = await self._require_integrations().check(
                    org_id, kind, grants.user_id
                )
            except IntegrationError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            return {"ok": ok, "detail": detail, "integration": view.public()}

        @app.delete("/orgs/{org_id}/integrations/{kind}", status_code=204)
        async def disconnect(principal: Me, org_id: uuid.UUID, kind: Channel) -> Response:
            grants = await self._chat_admin(principal, org_id)
            try:
                gone = await self._require_integrations().disconnect(org_id, kind, grants.user_id)
            except IntegrationError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            if not gone:
                raise _not_found("the org has no such integration")
            return Response(status_code=204)

        app.include_router(setup_router(self._github))

        @app.post("/orgs/{org_id}/github/install")
        async def github_install(principal: Me, org_id: uuid.UUID) -> dict[str, Any]:
            grants = await self._org_admin(principal, org_id)
            return {"url": self._require_github().install_url(org_id, grants.user_id)}

        @app.get("/orgs/{org_id}/github/installations")
        async def github_installations(principal: Me, org_id: uuid.UUID) -> list[dict[str, Any]]:
            await self._org_grants(principal, org_id)
            return await self._require_github().installations(org_id)

        @app.delete("/orgs/{org_id}/github/installations/{row_id}", status_code=204)
        async def github_uninstall(principal: Me, org_id: uuid.UUID, row_id: uuid.UUID) -> Response:
            grants = await self._org_admin(principal, org_id)
            github = self._require_github()
            if not await github.remove_installation(org_id, grants.user_id, row_id):
                raise _not_found("the org has no such installation")
            return Response(status_code=204)

        @app.get("/orgs/{org_id}/github/repos")
        async def github_repos(principal: Me, org_id: uuid.UUID) -> list[dict[str, Any]]:
            await self._org_grants(principal, org_id)
            return [repo.public() for repo in await self._require_github().repos(org_id)]

        @app.post("/orgs/{org_id}/github/repos", status_code=201)
        async def github_add_repo(
            principal: Me, org_id: uuid.UUID, body: GitHubRepoAdd
        ) -> dict[str, Any]:
            grants = await self._org_admin(principal, org_id)
            try:
                repo = await self._require_github().add_repo(
                    org_id, grants.user_id, body.repo, body.protected_paths
                )
            except GitHubSetupError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            return repo.public()

        @app.delete("/orgs/{org_id}/github/repos/{repo_id}", status_code=204)
        async def github_remove_repo(
            principal: Me, org_id: uuid.UUID, repo_id: uuid.UUID
        ) -> Response:
            grants = await self._org_admin(principal, org_id)
            if not await self._require_github().remove_repo(org_id, grants.user_id, repo_id):
                raise _not_found("the allowlist of the org has no such repository")
            return Response(status_code=204)

        @app.get("/connectors")
        async def connectors(principal: Me) -> list[dict[str, Any]]:
            return [m.public() for m in self._require_connections().catalog]

        @app.get("/workspaces/{workspace_id}/connectors/availability")
        async def connector_availability(principal: Me, workspace_id: uuid.UUID) -> dict[str, str]:
            # Why a connector cannot connect in this org now, in plain words (M8.1). The
            # Connections page shows it instead of a row that stays "pending".
            org_id, _ = await self._workspace_grants(principal, workspace_id)
            service = self._require_connections()
            out: dict[str, str] = {}
            for manifest in service.catalog:
                problem = await service.availability(org_id, manifest)
                if problem is not None:
                    out[manifest.name] = problem
            return out

        @app.get("/workspaces/{workspace_id}/connections")
        async def list_connections(principal: Me, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
            org_id, grants = await self._workspace_grants(principal, workspace_id)
            views = await self._require_connections().list_connections(
                org_id, workspace_id, grants.user_id
            )
            service = self._require_connections()
            return [view.public(service.can_manage(view, grants)) for view in views]

        @app.post("/workspaces/{workspace_id}/connections", status_code=201)
        async def start_connection(
            principal: Me, workspace_id: uuid.UUID, body: ConnectionStart
        ) -> dict[str, Any]:
            org_id, grants = await self._workspace_grants(principal, workspace_id)
            await self._permit(principal, org_id, "connections.add", grants)
            service = self._require_connections()
            started = await service.start(org_id, workspace_id, grants, body.connector, body.scope)
            return started.public(
                self._require_connections().can_manage(started.connection, grants)
            )

        @app.post("/workspaces/{workspace_id}/connections/mcp", status_code=201)
        async def add_mcp_server(
            principal: Me, workspace_id: uuid.UUID, body: McpServerAdd
        ) -> dict[str, Any]:
            org_id, grants = await self._workspace_grants(principal, workspace_id)
            await self._permit(principal, org_id, "connections.add", grants)
            started = await self._require_connections().add_mcp(
                org_id,
                workspace_id,
                grants,
                name=body.name,
                url=body.url,
                auth=body.auth,
                header=body.header,
                oauth_scope=body.oauth_scope,
                title=body.title,
                scope=body.scope,
            )
            return started.public(
                self._require_connections().can_manage(started.connection, grants)
            )

        @app.post("/workspaces/{workspace_id}/connections/stdio", status_code=201)
        async def add_stdio_server(
            principal: Me, workspace_id: uuid.UUID, body: StdioServerAdd
        ) -> dict[str, Any]:
            org_id, grants = await self._workspace_grants(principal, workspace_id)
            await self._permit(principal, org_id, "connections.add", grants)
            started = await self._require_connections().add_stdio(
                org_id,
                workspace_id,
                grants,
                name=body.name,
                title=body.title,
                scope=body.scope,
                package=None if body.package is None else body.package.model_dump(),
                command=body.command,
                env=body.env,
                egress=body.egress,
                memory_mb=body.memory_mb,
                idle_s=body.idle_s,
            )
            return started.public(
                self._require_connections().can_manage(started.connection, grants)
            )

        @app.post("/workspaces/{workspace_id}/connections/api", status_code=201)
        async def add_api(principal: Me, workspace_id: uuid.UUID, body: ApiAdd) -> dict[str, Any]:
            org_id, grants = await self._workspace_grants(principal, workspace_id)
            await self._permit(principal, org_id, "connections.add", grants)
            started = await self._require_connections().add_api(
                org_id,
                workspace_id,
                grants,
                name=body.name,
                title=body.title,
                scope=body.scope,
                auth=body.auth.model_dump(exclude_none=True),
                base_url=body.base_url,
                openapi=body.openapi,
                openapi_url=body.openapi_url,
                endpoints=None
                if body.endpoints is None
                else [e.model_dump() for e in body.endpoints],
                test=body.test,
            )
            return started.public(
                self._require_connections().can_manage(started.connection, grants)
            )

        @app.post("/connections/oauth/callback")
        async def connection_oauth_callback(principal: Me, body: OAuthCallback) -> dict[str, Any]:
            # The state is ``<connection id>.<random>``: the id finds the connection, and the
            # service compares the whole state with the one that it stored.
            try:
                connection_id = uuid.UUID(body.state.split(".", 1)[0])
            except ValueError:
                raise HTTPException(status_code=400, detail="the state is not valid") from None
            org_id, view, grants = await self._connection(principal, connection_id, manage=True)
            done = await self._require_connections().finish_oauth(
                org_id,
                view.id,
                state=body.state,
                code=body.code,
                error=body.error,
                actor=grants.user_id,
            )
            return done.public(self._require_connections().can_manage(done, grants))

        @app.get("/connections/return", include_in_schema=False)
        async def connection_return(
            connection: Annotated[uuid.UUID, Query()],
            t: Annotated[str, Query(min_length=8, max_length=200)],
        ) -> Response:
            # The Composio sign-in comes back here (M8.1). No sama session: the token of the
            # start is the proof, and the check only reads the state from Composio.
            service = self._require_connections()
            try:
                view, via = await service.finish_return(connection, t)
            except ConnectorError as error:
                return _return_page(service, None, "web", str(error))
            return _return_page(service, view, via, None)

        @app.get("/connections/go", include_in_schema=False)
        async def connection_go(
            connection: Annotated[uuid.UUID, Query()],
            t: Annotated[str, Query(min_length=8, max_length=200)],
        ) -> Response:
            # The short link of a long sign-in link (M8.1): the token of the start is the proof.
            service = self._require_connections()
            try:
                url = await service.sign_in_link(connection, t)
            except ConnectorError as error:
                return _return_page(service, None, "chat", str(error))
            return RedirectResponse(
                url,
                status_code=302,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )

        @app.get("/connections/oauth/return", include_in_schema=False)
        async def connection_oauth_return(
            state: Annotated[str, Query(min_length=3, max_length=500)],
            code: Annotated[str | None, Query(max_length=4000)] = None,
            error: Annotated[str | None, Query(max_length=200)] = None,
        ) -> Response:
            # The OAuth redirect URI of a remote MCP server (M8.1). The state and the PKCE
            # verifier are checked and used once, as in ``POST /connections/oauth/callback``.
            service = self._require_connections()
            try:
                view, via = await service.finish_oauth_return(state=state, code=code, error=error)
            except ConnectorError as problem:
                return _return_page(service, None, "web", str(problem))
            return _return_page(service, view, via, None)

        @app.put("/connections/{connection_id}/tools")
        async def connection_tools(
            principal: Me, connection_id: uuid.UUID, body: ToolFilters
        ) -> dict[str, Any]:
            org_id, view, grants = await self._connection(principal, connection_id, manage=True)
            saved = await self._require_connections().set_filters(
                org_id,
                view.id,
                include=tuple(body.include),
                exclude=tuple(body.exclude),
                actor=grants.user_id,
            )
            return saved.public(self._require_connections().can_manage(saved, grants))

        @app.post("/connections/{connection_id}/check")
        async def check_connection(principal: Me, connection_id: uuid.UUID) -> dict[str, Any]:
            org_id, view, grants = await self._connection(principal, connection_id, manage=False)
            checked = await self._require_connections().check(org_id, view.id, actor=grants.user_id)
            return checked.public(self._require_connections().can_manage(checked, grants))

        @app.put("/connections/{connection_id}/form")
        async def connection_form(
            principal: Me, connection_id: uuid.UUID, body: ConnectionForm
        ) -> dict[str, Any]:
            org_id, view, grants = await self._connection(principal, connection_id, manage=True)
            values = {k: v.get_secret_value() for k, v in body.values.items()}
            saved = await self._require_connections().submit_form(
                org_id, view.id, values, actor=grants.user_id
            )
            return saved.public(self._require_connections().can_manage(saved, grants))

        @app.post("/connections/{connection_id}/enable")
        async def enable_connection(principal: Me, connection_id: uuid.UUID) -> dict[str, Any]:
            org_id, view, grants = await self._connection(principal, connection_id, manage=True)
            done = await self._require_connections().enable(org_id, view.id, actor=grants.user_id)
            return done.public(self._require_connections().can_manage(done, grants))

        @app.post("/connections/{connection_id}/disable")
        async def disable_connection(principal: Me, connection_id: uuid.UUID) -> dict[str, Any]:
            org_id, view, grants = await self._connection(principal, connection_id, manage=True)
            done = await self._require_connections().disable(org_id, view.id, actor=grants.user_id)
            return done.public(self._require_connections().can_manage(done, grants))

        @app.delete("/connections/{connection_id}", status_code=204)
        async def delete_connection(principal: Me, connection_id: uuid.UUID) -> Response:
            org_id, view, grants = await self._connection(principal, connection_id, manage=True)
            if not await self._require_connections().delete(org_id, view.id, actor=grants.user_id):
                raise _not_found("the connection does not exist")
            return Response(status_code=204)

        @app.get("/orgs/{org_id}/skills")
        async def list_skills(
            principal: Me, org_id: uuid.UUID, workspace_id: uuid.UUID
        ) -> list[dict[str, Any]]:
            return await self._skills_of(principal, org_id, workspace_id)

        @app.post("/jobs/preview")
        async def job_preview(principal: Me, body: SchedulePreview) -> dict[str, Any]:
            self._require_jobs()
            try:
                return preview_schedule(body.schedule, body.tz)
            except JobError as error:
                raise HTTPException(status_code=400, detail=str(error)) from None

        @app.get("/orgs/{org_id}/jobs")
        async def list_jobs(principal: Me, org_id: uuid.UUID) -> list[dict[str, Any]]:
            jobs, actor = await self._job_actor(principal, org_id)
            return [view.as_json() for view in await jobs.list(actor)]

        @app.post("/orgs/{org_id}/jobs", status_code=201)
        async def create_job(principal: Me, org_id: uuid.UUID, body: JobInput) -> dict[str, Any]:
            await self._permit(principal, org_id, "jobs.create")
            jobs, actor = await self._job_actor(principal, org_id)
            return (await _job_call(jobs.create(actor, body))).as_json()

        @app.get("/orgs/{org_id}/jobs/{job_id}")
        async def get_job(principal: Me, org_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any]:
            jobs, actor = await self._job_actor(principal, org_id)
            return (await _job_call(jobs.get(actor, job_id))).as_json()

        @app.patch("/orgs/{org_id}/jobs/{job_id}")
        async def update_job(
            principal: Me, org_id: uuid.UUID, job_id: uuid.UUID, body: JobInput
        ) -> dict[str, Any]:
            jobs, actor = await self._job_actor(principal, org_id)
            return (await _job_call(jobs.update(actor, job_id, body))).as_json()

        @app.post("/orgs/{org_id}/jobs/{job_id}/approve")
        async def approve_job(
            principal: Me, org_id: uuid.UUID, job_id: uuid.UUID, body: JobApproval
        ) -> dict[str, Any]:
            jobs, actor = await self._job_actor(principal, org_id)
            return (await _job_call(jobs.approve(actor, job_id, body.version))).as_json()

        @app.post("/orgs/{org_id}/jobs/{job_id}/{action}")
        async def job_action(
            principal: Me, org_id: uuid.UUID, job_id: uuid.UUID, action: JobAction
        ) -> dict[str, Any]:
            jobs, actor = await self._job_actor(principal, org_id)
            if action == "pause":
                view = await _job_call(jobs.pause(actor, job_id))
            elif action == "resume":
                view = await _job_call(jobs.resume(actor, job_id))
            elif action == "run":
                fire_id = await _job_call(jobs.run_now(actor, job_id))
                return {"fire_id": str(fire_id)}
            else:
                return {"cancelled": await _job_call(jobs.cancel(actor, job_id))}
            return view.as_json()

        @app.delete("/orgs/{org_id}/jobs/{job_id}", status_code=204)
        async def remove_job(principal: Me, org_id: uuid.UUID, job_id: uuid.UUID) -> Response:
            jobs, actor = await self._job_actor(principal, org_id)
            await _job_call(jobs.remove(actor, job_id))
            return Response(status_code=204)

        self._workflow_routes(app)
        self._data_routes(app)
        self._admin_routes(app)

        @app.get("/skills/{skill_id}/versions")
        async def skill_versions(principal: Me, skill_id: uuid.UUID) -> list[dict[str, Any]]:
            return await self._skill_versions(principal, skill_id)

        @app.post("/channels/{channel}/pairing-codes", status_code=201)
        async def pairing_code(
            principal: Me, channel: Channel, body: NewPairingCode
        ) -> dict[str, Any]:
            return await self._pairing_code(principal, channel, body)

        @app.get("/channels/{channel}/identities")
        async def identities(principal: Me, channel: Channel) -> list[dict[str, Any]]:
            return await self._identities(principal, channel)

        @app.delete("/channels/{channel}/identities/{identity_id}", status_code=204)
        async def unlink(principal: Me, channel: Channel, identity_id: uuid.UUID) -> Response:
            await self._unlink(principal, channel, identity_id)
            return Response(status_code=204)

        @app.exception_handler(workerbots.WorkerBotError)
        async def worker_bot_error(_: Request, error: workerbots.WorkerBotError) -> JSONResponse:
            return JSONResponse({"detail": str(error)}, status_code=error.status_code)

        @app.get("/workspaces/{workspace_id}/worker-bots")
        async def worker_bots(principal: Me, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
            org_id, _ = await self._workspace_grants(principal, workspace_id)
            return await workerbots.list_bots(self._engine, org_id, workspace_id)

        @app.post("/workspaces/{workspace_id}/worker-bots", status_code=201)
        async def new_worker_bot(
            principal: Me, workspace_id: uuid.UUID, body: NewWorkerBot
        ) -> dict[str, Any]:
            org_id, grants = await self._bot_admin(principal, workspace_id)
            fields = workerbots.BotFields(
                name=body.name,
                agent_profile_id=body.agent_profile_id,
                channel_id=body.channel_id,
                daily_budget_usd=body.daily_budget_usd,
            )
            return await workerbots.create_bot(
                self._engine, org_id, workspace_id, grants.user_id, fields
            )

        @app.patch("/workspaces/{workspace_id}/worker-bots/{bot_id}")
        async def edit_worker_bot(
            principal: Me, workspace_id: uuid.UUID, bot_id: uuid.UUID, body: WorkerBotEdit
        ) -> dict[str, Any]:
            org_id, grants = await self._bot_admin(principal, workspace_id)
            fields = workerbots.BotFields(
                name=body.name,
                agent_profile_id=body.agent_profile_id,
                channel_id=body.channel_id,
                daily_budget_usd=body.daily_budget_usd,
                status=body.status,
            )
            return await workerbots.update_bot(
                self._engine, org_id, workspace_id, bot_id, grants.user_id, fields
            )

        @app.delete("/workspaces/{workspace_id}/worker-bots/{bot_id}", status_code=204)
        async def delete_worker_bot(
            principal: Me, workspace_id: uuid.UUID, bot_id: uuid.UUID
        ) -> Response:
            org_id, grants = await self._bot_admin(principal, workspace_id)
            await workerbots.delete_bot(self._engine, org_id, workspace_id, bot_id, grants.user_id)
            return Response(status_code=204)

        @app.exception_handler(triggers.TriggerError)
        async def trigger_error(_: Request, error: triggers.TriggerError) -> JSONResponse:
            return JSONResponse(
                {"detail": str(error)}, status_code=error.status_code, headers=error.headers
            )

        if self._triggers is not None:
            app.include_router(trigger_routes.router(self._triggers))

        @app.get("/workspaces/{workspace_id}/triggers")
        async def list_triggers(principal: Me, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
            org_id, _ = await self._workspace_grants(principal, workspace_id)
            return await self._require_triggers().list_triggers(org_id, workspace_id)

        @app.post("/workspaces/{workspace_id}/triggers", status_code=201)
        async def new_trigger(
            principal: Me, workspace_id: uuid.UUID, body: NewTriggerBody
        ) -> dict[str, Any]:
            org_id, grants = await self._workspace_grants(principal, workspace_id)
            await self._permit(principal, org_id, "triggers.create", grants)
            fields = triggers.NewTrigger(**body.model_dump())
            return await self._require_triggers().create(org_id, workspace_id, grants, fields)

        @app.get("/workspaces/{workspace_id}/triggers/{trigger_id}")
        async def get_trigger(
            principal: Me, workspace_id: uuid.UUID, trigger_id: uuid.UUID
        ) -> dict[str, Any]:
            org_id, _ = await self._workspace_grants(principal, workspace_id)
            return await self._require_triggers().get(org_id, workspace_id, trigger_id)

        @app.patch("/workspaces/{workspace_id}/triggers/{trigger_id}")
        async def edit_trigger(
            principal: Me, workspace_id: uuid.UUID, trigger_id: uuid.UUID, body: TriggerEdit
        ) -> dict[str, Any]:
            org_id, grants = await self._workspace_grants(principal, workspace_id)
            changes = triggers.TriggerChanges(**body.model_dump())
            return await self._require_triggers().update(
                org_id, workspace_id, trigger_id, grants, changes
            )

        @app.get("/workspaces/{workspace_id}/triggers/{trigger_id}/deliveries")
        async def trigger_deliveries(
            principal: Me,
            workspace_id: uuid.UUID,
            trigger_id: uuid.UUID,
            limit: Annotated[int, Query(ge=1, le=100)] = 50,
        ) -> list[dict[str, Any]]:
            org_id, _ = await self._workspace_grants(principal, workspace_id)
            return await self._require_triggers().list_deliveries(
                org_id, workspace_id, trigger_id, limit
            )

        @app.post("/workspaces/{workspace_id}/triggers/{trigger_id}/test")
        async def test_trigger(
            principal: Me, workspace_id: uuid.UUID, trigger_id: uuid.UUID, body: TriggerTest
        ) -> dict[str, Any]:
            org_id, grants = await self._workspace_grants(principal, workspace_id)
            return await self._require_triggers().test_send(
                org_id, workspace_id, trigger_id, grants, body.payload, start=body.start
            )

        @app.post("/workspaces/{workspace_id}/triggers/{trigger_id}/secret")
        async def rotate_trigger_secret(
            principal: Me, workspace_id: uuid.UUID, trigger_id: uuid.UUID
        ) -> dict[str, Any]:
            org_id, grants = await self._workspace_grants(principal, workspace_id)
            return await self._require_triggers().rotate_secret(
                org_id, workspace_id, trigger_id, grants
            )

        @app.delete("/workspaces/{workspace_id}/triggers/{trigger_id}", status_code=204)
        async def delete_trigger(
            principal: Me, workspace_id: uuid.UUID, trigger_id: uuid.UUID
        ) -> Response:
            org_id, grants = await self._workspace_grants(principal, workspace_id)
            await self._require_triggers().delete(org_id, workspace_id, trigger_id, grants)
            return Response(status_code=204)

        @app.put("/orgs/{org_id}/composio/webhook")
        async def composio_webhook(principal: Me, org_id: uuid.UUID) -> dict[str, Any]:
            grants = await self._org_admin(principal, org_id)
            return await self._require_triggers().subscribe_composio(org_id, grants.user_id)

        @app.get("/workspaces/{workspace_id}/memory")
        async def memory_notes(
            principal: Me, workspace_id: uuid.UUID, agent_profile_id: uuid.UUID | None = None
        ) -> dict[str, Any]:
            return await self._memory_notes(principal, workspace_id, agent_profile_id)

        @app.get("/workspaces/{workspace_id}/memory/{target}/revisions")
        async def memory_revisions(
            principal: Me,
            workspace_id: uuid.UUID,
            target: Target,
            agent_profile_id: uuid.UUID | None = None,
            limit: Annotated[int, Query(ge=1, le=100)] = 20,
        ) -> list[dict[str, Any]]:
            return await self._memory_revisions(
                principal, workspace_id, target, agent_profile_id, limit
            )

        @app.put("/workspaces/{workspace_id}/memory/{target}")
        async def memory_edit(
            principal: Me, workspace_id: uuid.UUID, target: Target, body: MemoryEdit
        ) -> dict[str, Any]:
            return await self._memory_write(
                principal, workspace_id, target, body.agent_profile_id, body.reason, None, body
            )

        @app.post("/workspaces/{workspace_id}/memory/{target}/revisions/{seq}/revert")
        async def memory_revert(
            principal: Me, workspace_id: uuid.UUID, target: Target, seq: int, body: MemoryRevert
        ) -> dict[str, Any]:
            return await self._memory_write(
                principal, workspace_id, target, body.agent_profile_id, body.reason, seq, None
            )

        @app.get("/workspaces/{workspace_id}/tasks")
        async def list_tasks(
            principal: Me,
            workspace_id: uuid.UUID,
            status: Annotated[list[TaskStatus] | None, Query()] = None,
        ) -> TaskList:
            return await self._board.list(principal, workspace_id, status or [])

        @app.post("/workspaces/{workspace_id}/tasks", status_code=201)
        async def create_task(principal: Me, workspace_id: uuid.UUID, body: NewTaskIn) -> TaskOut:
            return await self._board.create(principal, workspace_id, body)

        @app.get("/workspaces/{workspace_id}/tasks/{task_id}")
        async def get_task(
            principal: Me, workspace_id: uuid.UUID, task_id: uuid.UUID
        ) -> dict[str, Any]:
            return await self._board.get(principal, workspace_id, task_id)

        @app.patch("/workspaces/{workspace_id}/tasks/{task_id}")
        async def edit_task(
            principal: Me, workspace_id: uuid.UUID, task_id: uuid.UUID, body: TaskEdit
        ) -> TaskOut:
            return await self._board.edit(principal, workspace_id, task_id, body)

        @app.post("/workspaces/{workspace_id}/tasks/{task_id}/comments", status_code=201)
        async def comment_task(
            principal: Me, workspace_id: uuid.UUID, task_id: uuid.UUID, body: NewComment
        ) -> CommentOut:
            return await self._board.comment(principal, workspace_id, task_id, body)

        @app.get("/orgs/{org_id}/session-search")
        async def session_search(
            principal: Me,
            org_id: uuid.UUID,
            q: Annotated[str, Query(max_length=MAX_QUERY_CHARS)] = "",
            workspace_id: uuid.UUID | None = None,
            limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_LIMIT)] = MAX_SEARCH_LIMIT,
        ) -> list[SearchHit]:
            return await self._history.search(principal, org_id, q, workspace_id, limit)

        @app.get("/sessions/{session_id}/export")
        async def export_session(
            principal: Me, session_id: uuid.UUID, format: ExportFormat = "markdown"
        ) -> Response:
            return await self._history.export(principal, session_id, format)

        @app.get("/sessions/{session_id}/runs")
        async def session_runs(
            principal: Me,
            session_id: uuid.UUID,
            limit: Annotated[int, Query(ge=1, le=MAX_RUNS)] = DEFAULT_RUNS,
        ) -> list[RunSummary]:
            return await self._history.runs(principal, session_id, limit)

        @app.get("/runs/{run_id}/trace")
        async def run_trace(principal: Me, run_id: uuid.UUID) -> RunTrace:
            return await self._history.trace(principal, run_id)

        @app.get("/workspaces/{workspace_id}/skills")
        async def workspace_skills(principal: Me, workspace_id: uuid.UUID) -> SkillCatalog:
            return await self._editor().catalog(principal, workspace_id)

        @app.get("/workspaces/{workspace_id}/skills/{name}")
        async def workspace_skill(
            principal: Me, workspace_id: uuid.UUID, name: str, scope: ReadScope | None = None
        ) -> SkillDetail:
            return await self._editor().detail(principal, workspace_id, name, scope)

        @app.post("/workspaces/{workspace_id}/skills")
        async def write_skill(
            principal: Me, workspace_id: uuid.UUID, body: SkillWrite
        ) -> SkillWritten:
            org_id, grants = await self._workspace_grants(principal, workspace_id)
            await self._permit(principal, org_id, "skills.write", grants)
            return await self._editor().write(principal, workspace_id, body)

        @app.get("/workspaces/{workspace_id}/skills/{name}/settings")
        async def skill_settings(
            principal: Me, workspace_id: uuid.UUID, name: str
        ) -> SkillSettingsOut:
            return await self._editor().settings(principal, workspace_id, name)

        @app.put("/workspaces/{workspace_id}/skills/{name}/settings")
        async def put_skill_settings(
            principal: Me, workspace_id: uuid.UUID, name: str, body: SkillSettingsWrite
        ) -> SkillSettingsOut:
            return await self._editor().put_settings(principal, workspace_id, name, body)

        @app.get("/workspaces/{workspace_id}/archived-skills")
        async def archived_skills(principal: Me, workspace_id: uuid.UUID) -> list[ArchivedSkill]:
            return await self._editor().archived(principal, workspace_id)

        @app.post("/workspaces/{workspace_id}/skills/{name}/{action}")
        async def skill_state(
            principal: Me,
            workspace_id: uuid.UUID,
            name: str,
            action: StateAction,
            scope: WritableScope,
        ) -> SkillState:
            org_id, grants = await self._workspace_grants(principal, workspace_id)
            await self._permit(principal, org_id, "skills.write", grants)
            return await self._editor().change_state(principal, workspace_id, name, scope, action)

        @app.get("/skills/{skill_id}/versions/{version}")
        async def skill_version(
            principal: Me, skill_id: uuid.UUID, version: Annotated[int, Path(ge=1)]
        ) -> SkillVersionDetail:
            return await self._editor().version(principal, skill_id, version)

        return app

    def _editor(self) -> SkillEditor:
        if self._skill_editor is None:
            raise HTTPException(status_code=409, detail="skills are not set up")
        return self._skill_editor

    # -- reads ------------------------------------------------------------------------------

    async def _orgs(self, principal: Principal) -> list[dict[str, Any]]:
        """The orgs, workspaces, and agent profiles that the user can see. An org in another
        region shows only its name, its region, and the address of its deployment: the data of
        the org is there (Section 17.3)."""
        result: list[dict[str, Any]] = []
        regions = home_regions()
        async with as_user(self._engine, principal.auth_user_id) as connection:
            users = await connection.execute(
                select(
                    User.id,
                    User.org_id,
                    User.display_name,
                    Org.name,
                    Org.slug,
                    Org.plan,
                    Org.region,
                )
                .join(Org, Org.id == User.org_id)
                .where(User.auth_user_id == principal.auth_user_id)
                .order_by(Org.name, Org.id)
            )
            for user in users.all():
                grants = await load_grants(connection, user.org_id, user.id)
                if not grants.org_member:
                    continue
                entry: dict[str, Any] = {
                    "id": str(user.org_id),
                    "name": user.name,
                    "slug": user.slug,
                    "user_id": str(user.id),
                    "display_name": user.display_name,
                    "admin": grants.org_admin,
                    "role": next(role for ws, role in grants.rows if ws is None),
                    "plan": user.plan,
                    "permissions": [],
                    "workspaces": [],
                    "agent_profiles": [],
                    "integrations": [],
                    "region": user.region,
                    "region_here": regions.here(user.region),
                    "region_url": regions.url(user.region),
                }
                result.append(entry)
                if not entry["region_here"]:
                    continue
                workspaces = [
                    {"id": str(w.id), "name": w.name}
                    for w in await connection.execute(
                        select(Workspace.id, Workspace.name)
                        .where(Workspace.org_id == user.org_id)
                        .order_by(Workspace.name, Workspace.id)
                    )
                    if grants.sees_workspace(w.id)
                ]
                profiles = [
                    {
                        "id": str(p.id),
                        "name": p.name,
                        "workspace_id": None if p.workspace_id is None else str(p.workspace_id),
                        "runtime": p.runtime,
                        "model": p.model,
                    }
                    for p in await connection.execute(
                        select(
                            AgentProfile.id,
                            AgentProfile.name,
                            AgentProfile.workspace_id,
                            AgentProfile.runtime,
                            AgentProfile.model,
                        )
                        .where(AgentProfile.org_id == user.org_id)
                        .order_by(AgentProfile.name, AgentProfile.id)
                    )
                    if p.workspace_id is None or grants.sees_workspace(p.workspace_id)
                ]
                integrations = [
                    {"kind": i.kind, "status": i.status, "name": i.name}
                    for i in await connection.execute(
                        select(Integration.kind, Integration.status, Integration.name)
                        .where(Integration.org_id == user.org_id)
                        .order_by(Integration.kind)
                    )
                ]
                entry.update(
                    workspaces=workspaces,
                    agent_profiles=profiles,
                    integrations=integrations,
                    permissions=allowed_keys(
                        grants.org_role, await stored_rows(connection, user.org_id)
                    ),
                )
        return result

    async def _profile_access(
        self, principal: Principal, org_id: uuid.UUID, profile_id: uuid.UUID
    ) -> tuple[Any, Grants]:
        """The row of the profile and the grants. Raises ``404`` if the user is not a member of
        the org or cannot see the profile (the rule of ``/me``)."""
        missing = "the agent profile does not exist"
        async with as_user(self._engine, principal.auth_user_id) as connection:
            user_id = await _user_id(connection, principal, org_id)
            grants = None if user_id is None else await load_grants(connection, org_id, user_id)
            if grants is None or not grants.org_member:
                raise _not_found(missing)
            row = (
                await connection.execute(
                    select(
                        AgentProfile.id,
                        AgentProfile.org_id,
                        AgentProfile.workspace_id,
                        AgentProfile.name,
                        AgentProfile.soul,
                        AgentProfile.runtime,
                        AgentProfile.model,
                        AgentProfile.toolsets,
                        AgentProfile.group_toolsets,
                        AgentProfile.subagents,
                        AgentProfile.budget,
                        AgentProfile.reasoning_effort,
                        AgentProfile.busy_mode,
                        AgentProfile.max_parallel_runs,
                        AgentProfile.library_subagents,
                        AgentProfile.delegation_model,
                        AgentProfile.memory_provider,
                        AgentProfile.updated_at,
                        select(Org.plan)
                        .where(Org.id == AgentProfile.org_id)
                        .scalar_subquery()
                        .label("plan"),
                    ).where(AgentProfile.org_id == org_id, AgentProfile.id == profile_id)
                )
            ).one_or_none()
        if row is None or not (row.workspace_id is None or grants.sees_workspace(row.workspace_id)):
            raise _not_found(missing)
        return row, grants

    async def _profile(
        self, principal: Principal, org_id: uuid.UUID, profile_id: uuid.UUID
    ) -> dict[str, Any]:
        row, grants = await self._profile_access(principal, org_id, profile_id)
        runtime = RuntimeKind(row.runtime)
        budget = budget_json(row.budget if isinstance(row.budget, dict) else {})
        return {
            "id": str(row.id),
            "org_id": str(row.org_id),
            "workspace_id": None if row.workspace_id is None else str(row.workspace_id),
            "name": row.name,
            "soul": row.soul,
            "runtime": row.runtime,
            "model": row.model,
            "toolsets": list(row.toolsets),
            "group_toolsets": list(row.group_toolsets),
            "subagents": [s.model_dump(mode="json") for s in parse_subagents(row.subagents)],
            "budget": {key: budget.get(key) for key in BUDGET_KEYS},
            "reasoning_effort": row.reasoning_effort,
            "busy_mode": row.busy_mode,
            "max_parallel_runs": row.max_parallel_runs,
            "library_subagents": [str(value) for value in row.library_subagents or []],
            "delegation_model": row.delegation_model,
            "memory_provider": row.memory_provider,
            "updated_at": row.updated_at.isoformat(),
            "editable": await self._can_manage_agents(org_id, grants),
            "options": profile_options(runtime, row.plan),
        }

    async def _can_manage_agents(self, org_id: uuid.UUID, grants: Grants) -> bool:
        """True for an org owner or admin whose role has the permission ``agents.manage``."""
        if not grants.org_admin:
            return False
        try:
            await self._role_permissions.require(org_id, grants, "agents.manage")
        except PermissionDeniedError:
            return False
        return True

    async def _profile_tool_policy(
        self, principal: Principal, org_id: uuid.UUID, profile_id: uuid.UUID
    ) -> dict[str, Any]:
        """The tool policy of the profile for the web app. Each member who sees the profile can
        read it."""
        row, grants = await self._profile_access(principal, org_id, profile_id)
        return await tool_policy.profile_view(
            self._engine,
            org_id,
            profile_id,
            [str(name) for name in row.toolsets],
            editable=await self._can_manage_agents(org_id, grants),
        )

    async def _edit_profile(
        self, principal: Principal, org_id: uuid.UUID, profile_id: uuid.UUID, body: ProfileEdit
    ) -> dict[str, Any]:
        """Changes the profile. A session keeps its frozen soul, runtime, model, and sub-agents:
        those changes apply from the next session. A session also keeps a snapshot of its
        toolsets (M6.1): a new toolset applies from the next session, and a removed toolset
        applies at the next tool call of every session, because the gate reads the toolsets of
        now on every call and uses only the toolsets that the snapshot also has."""
        _, grants = await self._profile_access(principal, org_id, profile_id)
        await self._agents_admin(principal, org_id, grants)
        if body.soul is not None or body.subagents is not None:
            # The redactor must know every vault value before the text is checked. Fail closed.
            try:
                await know_the_secrets(self._engine, org_id)
            except VaultUnreadableError as error:
                raise HTTPException(status_code=503, detail=str(error)) from None
        try:
            await update_profile(
                self._engine,
                org_id,
                profile_id,
                ProfileChanges(**body.model_dump(exclude_unset=True)),
                actor_user_id=grants.user_id,
            )
        except NotAdminError:
            raise HTTPException(status_code=403, detail=_ADMIN_ONLY) from None
        except ProfileError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except SubagentError as error:
            if str(error) == "the profile does not exist":
                raise _not_found("the agent profile does not exist") from None
            raise HTTPException(status_code=422, detail=str(error)) from None
        return await self._profile(principal, org_id, profile_id)

    async def _create_profile(
        self, principal: Principal, org_id: uuid.UUID, body: ProfileCreate
    ) -> dict[str, Any]:
        """Makes an agent profile (M6.9). Only an org owner or admin can do it."""
        grants = await self._agents_admin(principal, org_id)
        if body.soul:
            # The redactor must know every vault value before the soul is checked.
            try:
                await know_the_secrets(self._engine, org_id)
            except VaultUnreadableError as error:
                raise HTTPException(status_code=503, detail=str(error)) from None
        try:
            profile_id = await create_profile(
                self._engine,
                org_id,
                NewProfile(
                    name=body.name,
                    soul=body.soul,
                    runtime=body.runtime,
                    model=body.model,
                    workspace_id=body.workspace_id,
                ),
                actor_user_id=grants.user_id,
            )
        except NotAdminError:
            raise HTTPException(status_code=403, detail=_ADMIN_ONLY) from None
        except ProfileLimitError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except ProfileError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return await self._profile(principal, org_id, profile_id)

    async def _profile_changes(
        self, principal: Principal, org_id: uuid.UUID, profile_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """The history of the profile (M6.1): each member who can see the profile can read it."""
        await self._profile_access(principal, org_id, profile_id)
        async with as_user(self._engine, principal.auth_user_id) as connection:
            changes = await list_changes(connection, org_id, profile_id)
        redactor = default_redactor()
        return [redactor.json(change) for change in changes]

    async def _revert_profile_change(
        self,
        principal: Principal,
        org_id: uuid.UUID,
        profile_id: uuid.UUID,
        change_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Writes the old values of one change of the history again (M6.1). Only an org owner
        or admin can do it, as for an edit. It fails with ``409`` when the profile changed
        after the change, or when the change is already reverted. As for an edit, the old
        values apply from the next session."""
        _, grants = await self._profile_access(principal, org_id, profile_id)
        await self._agents_admin(principal, org_id, grants)
        try:
            await know_the_secrets(self._engine, org_id)
        except VaultUnreadableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        try:
            async with self._engine.begin() as connection:
                new_id = await revert_change(
                    connection,
                    org_id=org_id,
                    profile_id=profile_id,
                    change_id=change_id,
                    actor_user_id=grants.user_id,
                )
        except LookupError:
            raise _not_found("the change does not exist") from None
        except NotAdminError:
            raise HTTPException(status_code=403, detail=_ADMIN_ONLY) from None
        except (RevertError, StaleChangeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except ProfileError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except SubagentError as error:
            if str(error) == "the profile does not exist":
                raise _not_found("the agent profile does not exist") from None
            raise HTTPException(status_code=422, detail=str(error)) from None
        return {"id": str(new_id), "profile": await self._profile(principal, org_id, profile_id)}

    async def _patch_session(
        self, principal: Principal, session_id: uuid.UUID, body: SessionPatch
    ) -> SessionOut:
        """Renames, archives, or restores a session (M7.10). The owner of a private session can.
        For a shared session, the owner, an admin of the workspace, or an org admin can. The
        archive of a compaction and a session with an archive of a compaction keep their status
        (``409``), and a session with an open run cannot be archived (``409``). The row is
        locked, so a second request waits and then sees the new state."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
        row = access.session
        assert access.grants is not None  # noqa: S101 (``session_access`` sets it)
        if not _can_change_session(row, access.grants):
            raise HTTPException(
                status_code=403,
                detail="only the owner of the session or an admin can change it",
            )
        title = None if body.title is None else default_redactor().text(body.title)
        async with self._engine.begin() as connection:
            current = (
                await connection.execute(
                    select(Session.title, Session.status, Session.lineage, Session.model_override)
                    .where(Session.org_id == access.org_id, Session.id == session_id)
                    .with_for_update()
                )
            ).one_or_none()
            if current is None:
                raise _not_found()
            actions: list[tuple[str, dict[str, Any]]] = []
            values: dict[str, Any] = {}
            if title is not None and title != current.title:
                values["title"] = title
                actions.append(("session.renamed", {"title_length": len(title)}))
            if body.status is not None and body.status != current.status:
                await self._check_status_change(connection, access.org_id, session_id, current)
                values["status"] = body.status
                actions.append(
                    ("session.archived" if body.status == "archived" else "session.restored", {})
                )
            if "model" in body.model_fields_set and body.model != current.model_override:
                await self._check_model_change(connection, access.org_id, session_id, body.model)
                values["model_override"] = body.model
                actions.append(
                    ("session.model_changed", {"from": current.model_override, "to": body.model})
                )
            if values:
                await connection.execute(
                    update(Session)
                    .where(Session.org_id == access.org_id, Session.id == session_id)
                    .values(**values, updated_at=func.now())
                )
                for action, details in actions:
                    await connection.execute(
                        insert(AuditEntry).values(
                            org_id=access.org_id,
                            workspace_id=row.workspace_id,
                            actor_kind="user",
                            actor_user_id=access.user_id,
                            action=action,
                            target_type="session",
                            target_id=str(session_id),
                            details=details,
                        )
                    )
            fresh = (
                await connection.execute(
                    select(*_SESSION_COLUMNS).where(
                        Session.org_id == access.org_id, Session.id == session_id
                    )
                )
            ).one()
            state = await _model_state(connection, access.org_id, session_id)
        return _with_model(_session_out(fresh, access.grants), state)

    @staticmethod
    async def _check_model_change(
        connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID, model: str | None
    ) -> None:
        """Raises ``409`` for a session of a coding agent and ``422`` for a model that the
        runtime of the session does not know. ``None`` (the model of the profile) always works.
        The rule is the rule of the model of a profile (``update_profile``)."""
        state = await _model_state(connection, org_id, session_id)
        if state is None:
            raise _not_found()
        if not state.selectable:
            raise HTTPException(
                status_code=409, detail="a coding agent chooses its own model in this session"
            )
        if model is not None and model not in state.allowed:
            raise HTTPException(
                status_code=422, detail="the model is not known for the runtime of the session"
            )

    @staticmethod
    async def _check_status_change(
        connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID, current: Any
    ) -> None:
        """Raises ``409`` when the status of the session must not change: it is the archive of
        a compaction, it has one, or (for an archive) a run is open."""
        if current.lineage == "compaction":
            raise HTTPException(
                status_code=409, detail="the archive of a compaction keeps its status"
            )
        compacted = (
            await connection.execute(
                select(Session.id)
                .where(
                    Session.org_id == org_id,
                    Session.parent_session_id == session_id,
                    Session.lineage == "compaction",
                )
                .limit(1)
            )
        ).first()
        if compacted is not None:
            raise HTTPException(
                status_code=409, detail="a session with a compaction keeps its status"
            )
        if current.status == "active":
            open_run = (
                await connection.execute(
                    select(Run.id)
                    .where(
                        Run.org_id == org_id,
                        Run.session_id == session_id,
                        Run.status.in_(OPEN_RUN_STATUSES),
                    )
                    .limit(1)
                )
            ).first()
            if open_run is not None:
                raise HTTPException(
                    status_code=409, detail="a run is open: stop it before you archive"
                )

    async def _sessions(
        self, principal: Principal, limit: int, status: str = "active"
    ) -> list[SessionOut]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            rows = (
                await connection.execute(
                    select(*_SESSION_COLUMNS)
                    .where(Session.status == status, served_here(Session.org_id))
                    .order_by(Session.created_at.desc(), Session.id)
                    .limit(limit)
                )
            ).all()
            grants: dict[uuid.UUID, Grants | None] = {}
            for org_id in {row.org_id for row in rows}:
                user_id = await _user_id(connection, principal, org_id)
                grants[org_id] = (
                    None if user_id is None else await load_grants(connection, org_id, user_id)
                )
            shown = [
                (row, g)
                for row in rows
                if (g := grants[row.org_id]) is not None
                and g.sees_session(row.workspace_id, row.audience, row.owner_user_id)
            ]
            states = await _run_states(connection, [row.id for row, _ in shown])
        out: list[SessionOut] = []
        for row, g in shown:
            item = _session_out(row, g)
            item.run_state, item.last_activity_at = states.get(row.id, (None, None))
            out.append(item)
        return out

    async def _session_models(self, principal: Principal, session_id: uuid.UUID) -> SessionModels:
        """``GET /sessions/{id}/models`` (M8.2): what the model picker needs."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
            state = await _model_state(connection, access.org_id, session_id)
        if state is None:
            raise _not_found()
        assert access.grants is not None  # noqa: S101 (``session_access`` sets it)
        return SessionModels(
            runtime=state.runtime.value,
            models=state.allowed,
            model=state.effective,
            default_model=state.default,
            is_override=state.is_override,
            editable=state.selectable and _can_change_session(access.session, access.grants),
        )

    async def _detail(self, principal: Principal, session_id: uuid.UUID) -> SessionDetail:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
            runs = await connection.execute(
                select(
                    Run.id,
                    Run.status,
                    Run.failure_code,
                    Run.failure_message,
                    Run.created_at,
                )
                .where(
                    Run.org_id == access.org_id,
                    Run.session_id == session_id,
                    Run.kind == "chat",
                )
                .order_by(Run.created_at.desc(), Run.id.desc())
                .limit(RECENT_RUNS)
            )
            run_list = [RunOut.model_validate(row, from_attributes=True) for row in runs]
            last = await self._last_event_id(connection, session_id)
            state = await _model_state(connection, access.org_id, session_id)
        assert access.grants is not None  # noqa: S101 (``session_access`` sets it)
        return SessionDetail(
            **_with_model(_session_out(access.session, access.grants), state).model_dump(),
            runs=run_list,
            last_event_id=last,
        )

    @staticmethod
    async def _last_event_id(connection: AsyncConnection, session_id: uuid.UUID) -> int:
        value = (
            await connection.execute(
                select(func.coalesce(func.max(RunEvent.id), 0)).where(
                    RunEvent.session_id == session_id, RunEvent.run_id.in_(_chat_runs(session_id))
                )
            )
        ).scalar_one()
        return int(value)

    async def _activity(self, principal: Principal, session_id: uuid.UUID) -> dict[str, Any]:
        """``GET /sessions/{id}/activity``: the tool trail of the newest chat runs of a session,
        so that the chat shows the tool calls of runs that ended before the page opened.

        The answer has the stored ``tool_call_started``, ``tool_call_finished``, and
        ``subagent_event`` events of the newest ``ACTIVITY_RUNS`` chat runs, oldest first, in
        the shape of a ``run_event`` of the event stream. At most ``ACTIVITY_EVENTS`` events
        come back (the newest); ``truncated`` tells that older events were left out. The
        payloads go through the redactor again, and long text values are cut, because the chat
        shows only a preview."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
            runs = (
                select(Run.id)
                .where(
                    Run.org_id == access.org_id,
                    Run.session_id == session_id,
                    Run.kind == "chat",
                )
                .order_by(Run.created_at.desc(), Run.id.desc())
                .limit(ACTIVITY_RUNS)
            )
            rows = (
                await connection.execute(
                    select(
                        RunEvent.id,
                        RunEvent.run_id,
                        RunEvent.seq,
                        RunEvent.type,
                        RunEvent.payload,
                    )
                    .where(
                        RunEvent.org_id == access.org_id,
                        RunEvent.session_id == session_id,
                        RunEvent.run_id.in_(runs),
                        RunEvent.type.in_(ACTIVITY_TYPES),
                    )
                    .order_by(RunEvent.id.desc())
                    .limit(ACTIVITY_EVENTS + 1)
                )
            ).all()
        truncated = len(rows) > ACTIVITY_EVENTS
        # The redactor runs again: it may know more secrets now (as for the run trace).
        redactor = default_redactor()
        events = [
            {
                "id": row.id,
                "run_id": str(row.run_id),
                "seq": row.seq,
                "type": row.type,
                "payload": _cut_text(redactor.json(row.payload or {}), ACTIVITY_TEXT_CHARS),
            }
            for row in reversed(rows[:ACTIVITY_EVENTS])
        ]
        return {"events": events, "truncated": truncated}

    async def _steers(self, principal: Principal, session_id: uuid.UUID) -> list[dict[str, Any]]:
        """``GET /sessions/{id}/steers`` (M7.1): the newest notes that joined a run of the
        session, oldest first. A note is ``pending`` until the run reads it at its next step
        (``delivered``). A note that the run did not read before it completed went to the
        session as a new message (``requeued``), and a stopped run drops its notes
        (``dropped``)."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
            rows = (
                await connection.execute(
                    select(
                        RunSteer.id,
                        RunSteer.run_id,
                        RunSteer.text,
                        RunSteer.status,
                        RunSteer.created_at,
                        RunSteer.delivered_at,
                        RunSteer.requeued_run_id,
                    )
                    .where(RunSteer.org_id == access.org_id, RunSteer.session_id == session_id)
                    .order_by(RunSteer.created_at.desc(), RunSteer.id)
                    .limit(STEERS_LISTED)
                )
            ).all()
        return [
            {
                "id": str(row.id),
                "run_id": str(row.run_id),
                "text": row.text,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                "delivered_at": None if row.delivered_at is None else row.delivered_at.isoformat(),
                "requeued_run_id": None
                if row.requeued_run_id is None
                else str(row.requeued_run_id),
            }
            for row in reversed(rows)
        ]

    async def _messages(
        self, principal: Principal, session_id: uuid.UUID, after: int, limit: int
    ) -> list[dict[str, Any]]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
            rows = await connection.execute(
                select(
                    Message.id,
                    Message.seq,
                    Message.run_id,
                    Message.role,
                    Message.author_user_id,
                    Message.content,
                    Message.created_at,
                )
                .where(
                    Message.org_id == access.org_id,
                    Message.session_id == session_id,
                    Message.seq > after,
                )
                .order_by(Message.seq)
                .limit(limit)
            )
            messages = rows.all()
            images: dict[uuid.UUID, list[dict[str, Any]]] = {}
            if messages:
                # The metadata only, never the bytes (M8.2): ``data`` is not in the column grant.
                found = await connection.execute(
                    select(
                        Attachment.id,
                        Attachment.message_id,
                        Attachment.name,
                        Attachment.mime_type,
                        Attachment.size_bytes,
                    )
                    .where(
                        Attachment.org_id == access.org_id,
                        Attachment.session_id == session_id,
                        Attachment.message_id.in_([row.id for row in messages]),
                    )
                    .order_by(Attachment.created_at, Attachment.id)
                )
                for image in found:
                    images.setdefault(image.message_id, []).append(
                        {
                            "id": str(image.id),
                            "name": image.name,
                            "mime_type": image.mime_type,
                            "size_bytes": image.size_bytes,
                        }
                    )
            out: list[dict[str, Any]] = []
            for row in messages:
                text = str((row.content or {}).get("text", ""))
                # The images of one message share a time, so the order of the marker lines in
                # the text is the order of the images.
                shown = sorted(images.get(row.id, []), key=lambda item: _marker_at(text, item))
                out.append(
                    {
                        "id": str(row.id),
                        "seq": row.seq,
                        "run_id": None if row.run_id is None else str(row.run_id),
                        "role": row.role,
                        "author_user_id": None
                        if row.author_user_id is None
                        else str(row.author_user_id),
                        "text": text,
                        "created_at": row.created_at.isoformat(),
                        "images": shown,
                    }
                )
            return out

    async def _approvals(self, principal: Principal, session_id: uuid.UUID) -> list[dict[str, Any]]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
            rows = await connection.execute(
                select(
                    Approval.id,
                    Approval.run_id,
                    Approval.call_id,
                    Approval.tool_name,
                    Approval.arguments,
                    Approval.reason,
                    Approval.created_at,
                    Approval.expires_at,
                    ReviewProposal.kind.label("proposal_kind"),
                    ReviewProposal.scope.label("proposal_scope"),
                    ReviewProposal.payload.label("proposal_payload"),
                    ReviewProposal.origin.label("proposal_origin"),
                )
                .outerjoin(
                    ReviewProposal,
                    and_(
                        ReviewProposal.org_id == Approval.org_id,
                        ReviewProposal.approval_id == Approval.id,
                    ),
                )
                .where(
                    Approval.org_id == access.org_id,
                    Approval.session_id == session_id,
                    Approval.status == "pending",
                )
                .order_by(Approval.created_at, Approval.id)
            )
            return [self._approval_entry(row) for row in rows]

    @staticmethod
    def _approval_entry(row: Any) -> dict[str, Any]:
        arguments, reason, diff = row.arguments, row.reason, None
        if row.proposal_payload is not None:
            # A proposal of a background review or a chat run. The stored text is not shown:
            # the redactor may know more secrets now (Section 9.4).
            shown = shown_proposal(
                row.proposal_kind, row.proposal_scope, row.proposal_payload, row.proposal_origin
            )
            arguments, reason, diff = shown.arguments, shown.reason, shown.diff
        return {
            "id": str(row.id),
            "run_id": str(row.run_id),
            "call_id": row.call_id,
            "tool_name": row.tool_name,
            "arguments": arguments,
            "reason": reason,
            "proposal": row.proposal_payload is not None,
            "scopes": list(
                allowed_scopes(row.tool_name, proposal=row.proposal_payload is not None)
            ),
            "diff": diff,
            "created_at": row.created_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
        }

    async def _questions(self, principal: Principal, session_id: uuid.UUID) -> list[dict[str, Any]]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
        if self._clarify is None:
            return []
        return [
            {
                "id": request.id,
                "run_id": request.run_id,
                "questions": [q.model_dump(mode="json") for q in request.questions],
            }
            for request in self._clarify.pending(str(access.org_id), str(session_id))
        ]

    # -- memory -----------------------------------------------------------------------------

    async def _memory_access(
        self,
        connection: AsyncConnection,
        principal: Principal,
        workspace_id: uuid.UUID,
        target: Target,
        agent_profile_id: uuid.UUID | None,
    ) -> tuple[MemoryScope, uuid.UUID, bool]:
        """The scope, the user row, and whether the user is a workspace admin. Raises ``404``
        if the user cannot see the workspace or the profile is not in it, and ``422`` if an
        ``agent`` call has no profile."""
        org_id = (
            await connection.execute(select(Workspace.org_id).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        user_id = None if org_id is None else await _user_id(connection, principal, org_id)
        if org_id is None or user_id is None:
            raise _not_found("the workspace does not exist")
        grants = await load_grants(connection, org_id, user_id)
        if not grants.sees_workspace(workspace_id):
            raise _not_found("the workspace does not exist")
        if target is Target.AGENT:
            if agent_profile_id is None:
                raise HTTPException(status_code=422, detail="agent_profile_id is needed")
            profile_workspace = (
                await connection.execute(
                    select(AgentProfile.workspace_id).where(
                        AgentProfile.org_id == org_id, AgentProfile.id == agent_profile_id
                    )
                )
            ).one_or_none()
            if profile_workspace is None or profile_workspace[0] not in (None, workspace_id):
                raise _not_found("the agent profile does not exist in this workspace")
        scope = scope_of(
            target,
            org_id=org_id,
            workspace_id=workspace_id,
            agent_profile_id=agent_profile_id,
            user_id=user_id,
        )
        return scope, user_id, grants.admins_workspace(workspace_id)

    async def _memory_notes(
        self, principal: Principal, workspace_id: uuid.UUID, agent_profile_id: uuid.UUID | None
    ) -> dict[str, Any]:
        targets = [Target.WORKSPACE, Target.USER]
        if agent_profile_id is not None:
            targets.insert(0, Target.AGENT)
        notes: list[dict[str, Any]] = []
        async with as_user(self._engine, principal.auth_user_id) as connection:
            for target in targets:
                scope, _, admin = await self._memory_access(
                    connection, principal, workspace_id, target, agent_profile_id
                )
                state = await read_state(connection, scope)
                notes.append(
                    {
                        "target": target.value,
                        "entries": list(state.entries),
                        "chars": used(state.entries),
                        "cap": CAPS[target],
                        "revision": state.revision,
                        "can_edit": admin or target is not Target.WORKSPACE,
                    }
                )
        return {"workspace_id": str(workspace_id), "targets": notes}

    async def _memory_revisions(
        self,
        principal: Principal,
        workspace_id: uuid.UUID,
        target: Target,
        agent_profile_id: uuid.UUID | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            scope, _, _ = await self._memory_access(
                connection, principal, workspace_id, target, agent_profile_id
            )
            revisions = await read_revisions(connection, scope, limit)
        return [
            {
                "seq": r.seq,
                "action": r.action,
                "entries": list(r.entries),
                "author_kind": r.author_kind,
                "author_user_id": None if r.author_user_id is None else str(r.author_user_id),
                "session_id": None if r.session_id is None else str(r.session_id),
                "run_id": None if r.run_id is None else str(r.run_id),
                "reason": r.reason,
                "reverted_to": r.reverted_to,
                "created_at": r.created_at.isoformat(),
            }
            for r in revisions
        ]

    async def _memory_write(
        self,
        principal: Principal,
        workspace_id: uuid.UUID,
        target: Target,
        agent_profile_id: uuid.UUID | None,
        reason: str | None,
        revert_to: int | None,
        edit: MemoryEdit | None,
    ) -> dict[str, Any]:
        """A person replaces the notes of a scope (``edit``) or sets them back to an old
        revision (``revert_to``)."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            scope, user_id, admin = await self._memory_access(
                connection, principal, workspace_id, target, agent_profile_id
            )
            if revert_to is not None and not await read_revisions(
                connection, scope, 1, seq=revert_to
            ):
                raise _not_found("the revision does not exist")
        if target is Target.WORKSPACE and not admin:
            raise HTTPException(
                status_code=403, detail="only a workspace admin can change workspace memory"
            )
        writer = Writer("user", user_id)

        async def audit(connection: AsyncConnection, state: MemoryState) -> None:
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=scope.org_id,
                    workspace_id=workspace_id,
                    actor_kind="user",
                    actor_user_id=user_id,
                    action="memory.reverted" if revert_to is not None else "memory.edited",
                    target_type="memory",
                    target_id=target.value,
                    details={"revision": state.revision, "reverted_to": revert_to},
                )
            )

        try:
            if edit is not None:
                state = await self._memory.edit(scope, edit.entries, writer, reason, after=audit)
            elif revert_to is not None:
                state = await self._memory.revert(scope, revert_to, writer, reason, after=audit)
        except MemoryDeniedError as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        except MemoryChangeError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return {
            "target": target.value,
            "entries": list(state.entries),
            "chars": used(state.entries),
            "cap": CAPS[target],
            "revision": state.revision,
        }

    # -- writes -----------------------------------------------------------------------------

    async def _workspace_access(
        self, principal: Principal, workspace_id: uuid.UUID, agent_profile_id: uuid.UUID
    ) -> tuple[uuid.UUID, Grants]:
        """The org and the grants of the user for a new session in the workspace with the agent
        profile.
        Raises ``404`` if the user cannot see the workspace or the profile is not in it."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            org_id = (
                await connection.execute(
                    select(Workspace.org_id).where(Workspace.id == workspace_id)
                )
            ).scalar_one_or_none()
            user_id = None if org_id is None else await _user_id(connection, principal, org_id)
            if org_id is None or user_id is None:
                raise _not_found("the workspace does not exist")
            grants = await load_grants(connection, org_id, user_id)
            if not grants.sees_workspace(workspace_id):
                raise _not_found("the workspace does not exist")
            profile_workspace = (
                await connection.execute(
                    select(AgentProfile.workspace_id).where(
                        AgentProfile.org_id == org_id, AgentProfile.id == agent_profile_id
                    )
                )
            ).one_or_none()
            if profile_workspace is None or profile_workspace[0] not in (None, workspace_id):
                raise _not_found("the agent profile does not exist in this workspace")
        return org_id, grants

    async def _know_secrets(self, org_id: uuid.UUID) -> None:
        """The redactor must know every vault value before a text is checked. Fail closed."""
        try:
            await know_the_secrets(self._engine, org_id)
        except VaultUnreadableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None

    async def _test_library_agent(
        self, principal: Principal, org_id: uuid.UUID, agent_id: uuid.UUID, body: LibraryTestIn
    ) -> JSONResponse:
        """A test run of a library agent (Section 13.3): a new private session of the person
        with the agent profile, that runs as the agent, and the first message. The agent must
        apply to the workspace: a workspace agent of another workspace cannot run there."""
        workspace_org, grants = await self._workspace_access(
            principal, body.workspace_id, body.profile_id
        )
        if workspace_org != org_id:
            raise _not_found("the workspace does not exist")
        user_id = grants.user_id
        try:
            agent = await self._library.library.get(org_id, grants, agent_id)
        except LIBRARY_ERRORS as error:
            raise library_http_error(error) from None
        if not agent.applies(
            workspace_id=body.workspace_id, audience="private", owner_user_id=user_id
        ):
            raise HTTPException(status_code=422, detail="the sub-agent is of another workspace")
        async with self._engine.connect() as connection:
            runtime = (
                await connection.execute(
                    select(AgentProfile.runtime).where(
                        AgentProfile.org_id == org_id, AgentProfile.id == body.profile_id
                    )
                )
            ).scalar_one()
        if not supports_subagents(RuntimeKind(runtime)):
            raise HTTPException(
                status_code=422, detail=f"the runtime {runtime} cannot run a library agent"
            )
        session_id = uuid.uuid4()
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(Session).values(
                    id=session_id,
                    org_id=org_id,
                    workspace_id=body.workspace_id,
                    agent_profile_id=body.profile_id,
                    audience="private",
                    owner_user_id=user_id,
                    title=f"Test: {agent.name}",
                    library_agent=agent.frozen(),
                )
            )
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    workspace_id=body.workspace_id,
                    actor_kind="user",
                    actor_user_id=user_id,
                    action="session.created",
                    target_type="session",
                    target_id=str(session_id),
                    details={
                        "audience": "private",
                        "channel": "web",
                        "library_agent": agent.name,
                    },
                )
            )
        try:
            sent = await self.submit(principal, session_id, body.message)
        except SendError as error:
            return JSONResponse(status_code=error.status, content={"detail": error.detail})
        return JSONResponse(
            status_code=201,
            content={"session_id": str(session_id), "run_id": str(sent.run_id)},
        )

    async def _create_session(self, principal: Principal, body: NewSession) -> SessionOut:
        org_id, grants = await self._workspace_access(
            principal, body.workspace_id, body.agent_profile_id
        )
        user_id = grants.user_id
        if body.audience == "shared":
            await self._permit(principal, org_id, "sessions.shared")
        session_id = uuid.uuid4()
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    insert(Session)
                    .values(
                        id=session_id,
                        org_id=org_id,
                        workspace_id=body.workspace_id,
                        agent_profile_id=body.agent_profile_id,
                        audience=body.audience,
                        owner_user_id=user_id if body.audience == "private" else None,
                        title=body.title,
                    )
                    .returning(*_SESSION_COLUMNS)
                )
            ).one()
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    workspace_id=body.workspace_id,
                    actor_kind="user",
                    actor_user_id=user_id,
                    action="session.created",
                    target_type="session",
                    target_id=str(session_id),
                    details={"audience": body.audience, "channel": "web"},
                )
            )
        # The user types the first message next: start the sandbox now (Section 4.4).
        with contextlib.suppress(WarmRateLimitedError):
            self._request_warm(org_id, session_id, principal.auth_user_id)
        return _session_out(row, grants)

    def _request_warm(
        self, org_id: uuid.UUID, session_id: uuid.UUID, user: uuid.UUID
    ) -> WarmStatus:
        if self._warmer is None:
            return WarmStatus.OFF
        return self._warmer.request(str(org_id), str(session_id), str(user))

    async def _warm(self, principal: Principal, session_id: uuid.UUID) -> JSONResponse:
        """``POST /sessions/{id}/warm`` (Section 4.4): starts or resumes the session sandbox and
        the warm runner in the background. It creates no run. It answers ``202`` at once with
        the status ``warming``, ``recent``, ``limited``, or ``off``; ``429`` when the user asked
        too often; ``404`` when the user cannot read the session."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
        try:
            status = self._request_warm(access.org_id, session_id, principal.auth_user_id)
        except WarmRateLimitedError as error:
            return JSONResponse(
                status_code=429,
                content={"detail": str(error)},
                headers={"Retry-After": str(error.retry_after_s)},
            )
        return JSONResponse(status_code=202, content={"status": status.value})

    async def _pairing_code(
        self, principal: Principal, channel: Channel, body: NewPairingCode
    ) -> dict[str, Any]:
        if channel in _DIRECT_CHANNELS and body.kind != "user":
            raise HTTPException(
                status_code=400, detail=f"{channel} has no groups: make a user code."
            )
        org_id, grants = await self._workspace_access(
            principal, body.workspace_id, body.agent_profile_id
        )
        user_id = grants.user_id
        async with self._engine.connect() as connection:
            bot = (
                await connection.execute(
                    select(
                        Integration.id,
                        Integration.name,
                        Integration.external_id,
                        Integration.settings,
                    ).where(
                        Integration.org_id == org_id,
                        Integration.kind == channel,
                        Integration.status == "active",
                    )
                )
            ).one_or_none()
        if bot is None:
            app_name = _APP_NAMES[channel]
            raise HTTPException(
                status_code=409,
                detail=f"the org has no {app_name}. An org admin connects one under Integrations.",
            )
        try:
            async with self._engine.begin() as connection:
                code, expires_at = await create_code(
                    connection,
                    org_id=org_id,
                    user_id=user_id,
                    integration_id=bot.id,
                    channel=channel,
                    kind=body.kind,
                    workspace_id=body.workspace_id,
                    agent_profile_id=body.agent_profile_id,
                )
        except PairingError as error:
            raise HTTPException(status_code=429, detail=str(error)) from None
        link: str | None
        if channel == "whatsapp":
            # The link opens a chat with the number and fills in the code.
            link = f"https://wa.me/{str(bot.name).lstrip('+')}?text={code}"
            command = code
        elif channel == "email":
            link = f"mailto:{bot.name}?subject={code}"
            command = code
        elif channel == "slack":
            # The link opens the direct messages with the app. The code goes in by hand.
            app_id = (bot.settings or {}).get("app_id")
            link = (
                f"https://slack.com/app_redirect?app={app_id}&team={bot.external_id}"
                if app_id
                else None
            )
            command = f"/sama pair {code}"
        else:
            # The link opens the bot and sends the code with /start. A group link adds the bot
            # to a group first.
            start = "start" if body.kind == "user" else "startgroup"
            link = f"https://t.me/{bot.name}?{start}={code}"
            command = f"/pair {code}"
        return {
            "code": code,
            "kind": body.kind,
            "expires_at": expires_at.isoformat(),
            "bot_username": bot.name,
            "link": link,
            "command": command,
        }

    def _require_github(self) -> GitHubService:
        if self._github is None:
            raise HTTPException(status_code=409, detail="the GitHub App is off in this process")
        return self._github

    def _require_triggers(self) -> triggers.TriggerService:
        if self._triggers is None:
            raise HTTPException(status_code=409, detail="triggers are off in this process")
        return self._triggers

    def _require_connections(self) -> ConnectionService:
        if self._connections is None:
            raise HTTPException(status_code=409, detail="connections are off in this process")
        return self._connections

    async def _connection(
        self, principal: Principal, connection_id: uuid.UUID, *, manage: bool
    ) -> tuple[uuid.UUID, ConnectionView, Grants]:
        """The connection and the grants of the user. The row is read as the user, so row
        level security hides the connections of other orgs and the personal connections of
        other users. Raises ``404`` for a connection that the user cannot see, and ``403``
        for a change that the user cannot make."""
        service = self._require_connections()
        async with as_user(self._engine, principal.auth_user_id) as connection:
            org_id = (
                await connection.execute(
                    select(ConnectionRow.org_id).where(ConnectionRow.id == connection_id)
                )
            ).scalar_one_or_none()
            user_id = None if org_id is None else await _user_id(connection, principal, org_id)
            grants = (
                None
                if org_id is None or user_id is None
                else await load_grants(connection, org_id, user_id)
            )
        view = None if org_id is None else await service.get(org_id, connection_id)
        if org_id is None or grants is None or view is None or not service.can_see(view, grants):
            raise _not_found("the connection does not exist")
        if manage and not service.can_manage(view, grants):
            raise HTTPException(
                status_code=403,
                detail="only the owner, a workspace admin, or the org owner can change it",
            )
        return org_id, view, grants

    def _require_custom_tools(self) -> CustomToolService:
        if self._custom_tools is None:
            raise HTTPException(status_code=409, detail="custom tools are off in this process")
        return self._custom_tools

    async def _tool_grants(
        self, principal: Principal, tool_id: uuid.UUID
    ) -> tuple[uuid.UUID, Grants]:
        """The org of a custom tool and the grants of the user. The row is read as the user, so
        row level security hides the tools of other orgs, of workspaces that the user does not
        see, and the user tools of other users. Raises ``404`` for a tool that the user cannot
        see (Section 10.8)."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            org_id = (
                await connection.execute(select(CustomTool.org_id).where(CustomTool.id == tool_id))
            ).scalar_one_or_none()
            user_id = None if org_id is None else await _user_id(connection, principal, org_id)
            grants = (
                None
                if org_id is None or user_id is None
                else await load_grants(connection, org_id, user_id)
            )
        if org_id is None or grants is None or not grants.org_member:
            raise _not_found("the tool does not exist")
        return org_id, grants

    def _require_browser(self) -> BrowserSettings:
        if self._browser is None:
            raise HTTPException(status_code=409, detail="the browser is off in this process")
        return self._browser

    def _require_model_keys(self) -> ModelKeys:
        if self._model_keys is None:
            raise HTTPException(status_code=503, detail="the vault is off in this process")
        return self._model_keys

    def _require_integrations(self) -> Integrations:
        if self._integrations is None:
            raise HTTPException(status_code=409, detail="integrations are off in this process")
        return self._integrations

    async def _org_grants(self, principal: Principal, org_id: uuid.UUID) -> Grants:
        """The grants of the user in the org. Raises ``404`` for a user who is not a member."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            user_id = await _user_id(connection, principal, org_id)
            grants = None if user_id is None else await load_grants(connection, org_id, user_id)
        if grants is None or not grants.org_member:
            raise _not_found("the org does not exist")
        return grants

    async def _workspace_grants(
        self, principal: Principal, workspace_id: uuid.UUID
    ) -> tuple[uuid.UUID, Grants]:
        """The org and the grants of the user. Raises ``404`` if the user cannot see the
        workspace."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            org_id = (
                await connection.execute(
                    select(Workspace.org_id).where(Workspace.id == workspace_id)
                )
            ).scalar_one_or_none()
            user_id = None if org_id is None else await _user_id(connection, principal, org_id)
            if org_id is None or user_id is None:
                raise _not_found("the workspace does not exist")
            grants = await load_grants(connection, org_id, user_id)
        if not grants.sees_workspace(workspace_id):
            raise _not_found("the workspace does not exist")
        return org_id, grants

    async def _bot_admin(
        self, principal: Principal, workspace_id: uuid.UUID
    ) -> tuple[uuid.UUID, Grants]:
        """Like ``_workspace_grants``, and ``403`` for a user who is not a workspace admin, an
        org admin, or the org owner."""
        org_id, grants = await self._workspace_grants(principal, workspace_id)
        if not (grants.org_admin or grants.admins_workspace(workspace_id)):
            raise HTTPException(status_code=403, detail=_BOT_ADMIN_ONLY)
        return org_id, grants

    def _data_routes(self, app: FastAPI) -> None:
        """The data control routes of Section 17.4: export, delete, and retention."""
        data = self._data

        @app.get("/orgs/{org_id}/export")
        async def export_org(principal: Me, org_id: uuid.UUID) -> FileResponse:
            grants = await self._org_grants(principal, org_id)
            if not grants.org_owner:
                raise HTTPException(status_code=403, detail=_OWNER_ONLY)
            result = await _data_call(data.export(org_id, kind="org", actor_user_id=grants.user_id))
            return _zip(result)

        @app.get("/orgs/{org_id}/users/{user_id}/export")
        async def export_user(principal: Me, org_id: uuid.UUID, user_id: uuid.UUID) -> FileResponse:
            grants = await self._org_grants(principal, org_id)
            if grants.user_id != user_id and not grants.org_owner:
                raise HTTPException(status_code=403, detail=_SELF_OR_OWNER)
            result = await _data_call(
                data.export(org_id, kind="user", user_id=user_id, actor_user_id=grants.user_id)
            )
            return _zip(result)

        @app.delete("/orgs/{org_id}/users/{user_id}")
        async def delete_user(
            principal: Me, org_id: uuid.UUID, user_id: uuid.UUID
        ) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            if grants.user_id != user_id:
                if not grants.org_admin:
                    raise HTTPException(status_code=403, detail=_SELF_OR_ADMIN)
                async with self._engine.connect() as connection:
                    target = await load_grants(connection, org_id, user_id)
                if target.org_owner and not grants.org_owner:
                    raise HTTPException(
                        status_code=403, detail="only an org owner can delete an owner"
                    )
            removed = await _data_call(
                data.delete_user(org_id, user_id, actor_user_id=grants.user_id)
            )
            return {"sessions": len(removed.sessions), "runs": removed.runs}

        @app.delete("/orgs/{org_id}")
        async def delete_org(
            principal: Me,
            org_id: uuid.UUID,
            confirm: Annotated[str, Query(max_length=200)],
        ) -> dict[str, Any]:
            grants = await self._org_grants(principal, org_id)
            if not grants.org_owner:
                raise HTTPException(status_code=403, detail=_OWNER_ONLY)
            async with self._engine.connect() as connection:
                slug = (
                    await connection.execute(select(Org.slug).where(Org.id == org_id))
                ).scalar_one_or_none()
            if slug is None or confirm != slug:
                raise HTTPException(
                    status_code=400, detail="set confirm to the slug of the org to delete it"
                )
            removed = await _data_call(data.delete_org(org_id, actor_user_id=grants.user_id))
            return {"sessions": len(removed.sessions), "runs": removed.runs}

        @app.get("/orgs/{org_id}/data-settings")
        async def data_settings(principal: Me, org_id: uuid.UUID) -> dict[str, Any]:
            await self._org_grants(principal, org_id)
            return (await _data_call(data.retention(org_id))).as_json()

        @app.put("/orgs/{org_id}/data-settings")
        async def set_data_settings(
            principal: Me, org_id: uuid.UUID, body: DataSettingsIn
        ) -> dict[str, Any]:
            grants = await self._org_admin(principal, org_id)
            view = await _data_call(
                data.set_retention(org_id, body.retention_days, actor_user_id=grants.user_id)
            )
            return view.as_json()

    def _admin_routes(self, app: FastAPI) -> None:
        """The admin views of the web app (Section 17.6, M6.9): usage, audit log, and curator
        reports. Only an org owner or admin reads them."""
        views = self._views

        @app.get("/orgs/{org_id}/usage")
        async def org_usage(
            principal: Me,
            org_id: uuid.UUID,
            days: Annotated[int, Query(ge=1, le=MAX_USAGE_DAYS)] = DEFAULT_USAGE_DAYS,
        ) -> OrgUsage:
            await self._org_admin(principal, org_id)
            return await views.usage(org_id, days)

        @app.get("/orgs/{org_id}/audit")
        async def org_audit(
            principal: Me,
            org_id: uuid.UUID,
            limit: Annotated[int, Query(ge=1, le=MAX_AUDIT_LIMIT)] = DEFAULT_AUDIT_LIMIT,
            before: Annotated[int | None, Query(ge=1, le=MAX_AUDIT_ID)] = None,
            action: Annotated[str | None, Query(max_length=MAX_AUDIT_FILTER_CHARS)] = None,
            actor_user_id: uuid.UUID | None = None,
        ) -> AuditPage:
            await self._org_admin(principal, org_id)
            return await views.audit(
                org_id,
                limit=limit,
                before=before,
                action=action.strip() if action else None,
                actor_user_id=actor_user_id,
            )

        @app.get("/orgs/{org_id}/curator-reports")
        async def org_curator_reports(
            principal: Me,
            org_id: uuid.UUID,
            limit: Annotated[int, Query(ge=1, le=MAX_CURATOR_REPORTS)] = DEFAULT_CURATOR_REPORTS,
        ) -> CuratorReports:
            await self._org_admin(principal, org_id)
            return await views.curator_reports(org_id, limit=limit)

    def _workflow_routes(self, app: FastAPI) -> None:
        """The workflow routes of Section 12.3. A user sees and changes only the workflows that
        the user owns."""

        @app.post("/workflows/validate")
        async def validate_workflow(principal: Me, body: WorkflowSource) -> dict[str, Any]:
            workflows = self._require_workflows()
            try:
                return workflows.validate(body.yaml)
            except WorkflowError as error:
                raise HTTPException(status_code=400, detail=str(error)) from None

        @app.get("/orgs/{org_id}/workflows")
        async def list_workflows(principal: Me, org_id: uuid.UUID) -> list[dict[str, Any]]:
            workflows, actor = await self._workflow_actor(principal, org_id)
            return await workflows.list(actor)

        @app.post("/orgs/{org_id}/workflows", status_code=201)
        async def create_workflow(
            principal: Me, org_id: uuid.UUID, body: WorkflowSource
        ) -> dict[str, Any]:
            await self._permit(principal, org_id, "workflows.create")
            workflows, actor = await self._workflow_actor(principal, org_id)
            return await _workflow_call(
                workflows.create(
                    actor,
                    body.yaml,
                    workspace_id=body.workspace_id,
                    agent_profile_id=body.agent_profile_id,
                )
            )

        @app.get("/orgs/{org_id}/workflows/{workflow_id}")
        async def get_workflow(
            principal: Me, org_id: uuid.UUID, workflow_id: uuid.UUID
        ) -> dict[str, Any]:
            workflows, actor = await self._workflow_actor(principal, org_id)
            return await _workflow_call(workflows.get(actor, workflow_id))

        @app.put("/orgs/{org_id}/workflows/{workflow_id}")
        async def update_workflow(
            principal: Me, org_id: uuid.UUID, workflow_id: uuid.UUID, body: WorkflowSource
        ) -> dict[str, Any]:
            workflows, actor = await self._workflow_actor(principal, org_id)
            return await _workflow_call(workflows.update(actor, workflow_id, body.yaml))

        @app.post("/orgs/{org_id}/workflows/{workflow_id}/approve")
        async def approve_workflow(
            principal: Me, org_id: uuid.UUID, workflow_id: uuid.UUID, body: WorkflowApproval
        ) -> dict[str, Any]:
            workflows, actor = await self._workflow_actor(principal, org_id)
            return await _workflow_call(workflows.approve(actor, workflow_id, body.version))

        @app.post("/orgs/{org_id}/workflows/{workflow_id}/start", status_code=201)
        async def start_workflow(
            principal: Me, org_id: uuid.UUID, workflow_id: uuid.UUID, body: WorkflowStart
        ) -> dict[str, Any]:
            workflows, actor = await self._workflow_actor(principal, org_id)
            return await _workflow_call(workflows.start(actor, workflow_id, body.inputs))

        @app.post("/orgs/{org_id}/workflows/{workflow_id}/{action}")
        async def workflow_action(
            principal: Me, org_id: uuid.UUID, workflow_id: uuid.UUID, action: WorkflowAction
        ) -> dict[str, Any]:
            workflows, actor = await self._workflow_actor(principal, org_id)
            if action == "pause":
                return await _workflow_call(workflows.pause(actor, workflow_id))
            return await _workflow_call(workflows.resume(actor, workflow_id))

        @app.delete("/orgs/{org_id}/workflows/{workflow_id}", status_code=204)
        async def remove_workflow(
            principal: Me, org_id: uuid.UUID, workflow_id: uuid.UUID
        ) -> Response:
            workflows, actor = await self._workflow_actor(principal, org_id)
            await _workflow_call(workflows.remove(actor, workflow_id))
            return Response(status_code=204)

        @app.get("/orgs/{org_id}/workflow-instances")
        async def list_workflow_instances(
            principal: Me,
            org_id: uuid.UUID,
            workflow_id: uuid.UUID | None = None,
            limit: Annotated[int, Query(ge=1, le=100)] = 20,
        ) -> list[dict[str, Any]]:
            workflows, actor = await self._workflow_actor(principal, org_id)
            return await workflows.instances(actor, workflow_id, limit=limit)

        @app.get("/orgs/{org_id}/workflow-instances/{instance_id}")
        async def get_workflow_instance(
            principal: Me, org_id: uuid.UUID, instance_id: uuid.UUID
        ) -> dict[str, Any]:
            workflows, actor = await self._workflow_actor(principal, org_id)
            return await _workflow_call(workflows.instance(actor, instance_id))

        @app.post("/orgs/{org_id}/workflow-instances/{instance_id}/cancel")
        async def cancel_workflow_instance(
            principal: Me, org_id: uuid.UUID, instance_id: uuid.UUID
        ) -> dict[str, Any]:
            workflows, actor = await self._workflow_actor(principal, org_id)
            return await _workflow_call(workflows.cancel(actor, instance_id))

        @app.post("/orgs/{org_id}/workflow-instances/{instance_id}/signal")
        async def signal_workflow_instance(
            principal: Me, org_id: uuid.UUID, instance_id: uuid.UUID, body: WorkflowSignal
        ) -> dict[str, Any]:
            workflows, actor = await self._workflow_actor(principal, org_id)
            ended = await _workflow_call(
                workflows.signal(actor, instance_id, body.event, body.data)
            )
            return {"ended": ended}

        @app.post("/orgs/{org_id}/workflow-instances/{instance_id}/steps/{step_id}/decide")
        async def decide_workflow_step(
            principal: Me,
            org_id: uuid.UUID,
            instance_id: uuid.UUID,
            step_id: str,
            body: WorkflowDecision,
        ) -> dict[str, Any]:
            workflows, actor = await self._workflow_actor(principal, org_id)
            return await _workflow_call(
                workflows.decide(actor, instance_id, step_id, approve=body.approve)
            )

        @app.post("/orgs/{org_id}/workflow-instances/{instance_id}/steps/{step_id}/resolve")
        async def resolve_workflow_step(
            principal: Me,
            org_id: uuid.UUID,
            instance_id: uuid.UUID,
            step_id: str,
            body: WorkflowResolution,
        ) -> dict[str, Any]:
            workflows, actor = await self._workflow_actor(principal, org_id)
            return await _workflow_call(
                workflows.resolve(actor, instance_id, step_id, body.action, body.output)
            )

    def _require_workflows(self) -> WorkflowService:
        if self._workflows is None:
            raise HTTPException(status_code=409, detail="workflows are off in this process")
        return self._workflows

    async def _workflow_actor(
        self, principal: Principal, org_id: uuid.UUID
    ) -> tuple[WorkflowService, JobActor]:
        workflows = self._require_workflows()
        grants = await self._org_grants(principal, org_id)
        return workflows, JobActor(org_id=org_id, user_id=grants.user_id, kind="user")

    def _require_jobs(self) -> JobService:
        if self._jobs is None:
            raise HTTPException(status_code=409, detail="scheduled jobs are off in this process")
        return self._jobs

    async def _job_actor(
        self, principal: Principal, org_id: uuid.UUID
    ) -> tuple[JobService, JobActor]:
        """The job service and the user as the actor. A user sees and changes only the jobs
        that the user owns (Section 12.1)."""
        jobs = self._require_jobs()
        grants = await self._org_grants(principal, org_id)
        return jobs, JobActor(org_id=org_id, user_id=grants.user_id, kind="user")

    async def _permit(
        self,
        principal: Principal,
        org_id: uuid.UUID,
        key: str,
        grants: Grants | None = None,
    ) -> Grants:
        """Checks the permission ``key`` of the role of the user (M7.10) and returns the grants.
        Call it after the check of the role. Raises ``404`` for a user who is not a member and
        ``403`` when the owner of the org turned the permission off for the role of the user.
        With ``grants``, it does not read them again."""
        grants = grants or await self._org_grants(principal, org_id)
        try:
            await self._role_permissions.require(org_id, grants, key)
        except PermissionDeniedError as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        return grants

    async def _agents_admin(
        self, principal: Principal, org_id: uuid.UUID, grants: Grants | None = None
    ) -> Grants:
        """An org owner or admin whose role has the permission ``agents.manage``: ``404`` for a
        user who is not a member, ``403`` for another role or a turned-off permission. With
        ``grants``, it does not read them again."""
        grants = grants or await self._org_grants(principal, org_id)
        if not grants.org_admin:
            raise HTTPException(status_code=403, detail=_ADMIN_ONLY)
        return await self._permit(principal, org_id, "agents.manage", grants)

    async def _chat_admin(self, principal: Principal, org_id: uuid.UUID) -> Grants:
        """An org owner or admin with the permission to manage chat apps."""
        grants = await self._org_admin(principal, org_id)
        return await self._permit(principal, org_id, "chat_apps.manage", grants)

    async def _org_admin(self, principal: Principal, org_id: uuid.UUID) -> Grants:
        """Raises ``404`` for a user who is not a member and ``403`` for a member who is not an
        owner or an admin of the org."""
        grants = await self._org_grants(principal, org_id)
        if not grants.org_admin:
            raise HTTPException(status_code=403, detail=_ADMIN_ONLY)
        return grants

    async def _identities(self, principal: Principal, channel: Channel) -> list[dict[str, Any]]:
        """The chat accounts of ``channel`` that are linked to the user, in all orgs."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            rows = await connection.execute(
                select(
                    ChannelIdentity.id,
                    ChannelIdentity.org_id,
                    ChannelIdentity.workspace_id,
                    ChannelIdentity.agent_profile_id,
                    ChannelIdentity.integration_id,
                    ChannelIdentity.created_at,
                )
                .where(ChannelIdentity.channel == channel, served_here(ChannelIdentity.org_id))
                .order_by(ChannelIdentity.created_at)
            )
            return [
                {
                    "id": str(row.id),
                    "org_id": str(row.org_id),
                    "workspace_id": str(row.workspace_id),
                    "agent_profile_id": str(row.agent_profile_id),
                    "integration_id": str(row.integration_id),
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    async def _unlink(self, principal: Principal, channel: Channel, identity_id: uuid.UUID) -> None:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            row = (
                await connection.execute(
                    select(
                        ChannelIdentity.org_id,
                        ChannelIdentity.user_id,
                        ChannelIdentity.workspace_id,
                        ChannelIdentity.channel,
                        ChannelIdentity.external_id,
                    ).where(ChannelIdentity.id == identity_id, ChannelIdentity.channel == channel)
                )
            ).one_or_none()
            if row is not None:
                # The account is in the data of its org: only the home region changes it.
                await _user_id(connection, principal, row.org_id)
        if row is None:
            raise _not_found("the linked account does not exist")
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(ChannelIdentity).where(
                    ChannelIdentity.org_id == row.org_id, ChannelIdentity.id == identity_id
                )
            )
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=row.org_id,
                    workspace_id=row.workspace_id,
                    actor_kind="user",
                    actor_user_id=row.user_id,
                    action="channel_identity.unlinked",
                    target_type="channel_identity",
                    target_id=f"{row.channel}:{row.external_id}",
                )
            )

    async def _skills_of(
        self, principal: Principal, org_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """The skills that the user sees in a workspace: builtin, org, workspace, and user."""
        if self._skills is None:
            raise HTTPException(status_code=409, detail="skills are not set up")
        async with as_user(self._engine, principal.auth_user_id) as connection:
            user_id = await _user_id(connection, principal, org_id)
            grants = None if user_id is None else await load_grants(connection, org_id, user_id)
            if user_id is None or grants is None or not grants.sees_workspace(workspace_id):
                raise _not_found()
            docs = await self._skills.catalog(
                connection, org_id=org_id, workspace_id=workspace_id, user_id=user_id
            )
        return [
            {
                "id": None if doc.skill_id is None else str(doc.skill_id),
                "name": doc.name,
                "description": doc.description,
                "scope": doc.scope,
                "version": doc.version,
                "tags": list(doc.tags),
            }
            for doc in docs
        ]

    async def _skill_versions(
        self, principal: Principal, skill_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """The versions of a skill, with who changed it and in which run. RLS hides the skills
        that the user cannot read."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            # The skill comes first, under RLS. Its org then bounds the version rows.
            org_id = (
                await connection.execute(select(Skill.org_id).where(Skill.id == skill_id))
            ).scalar_one_or_none()
            if org_id is None:
                raise _not_found("the skill does not exist")
            await _user_id(connection, principal, org_id)
            rows = (
                await connection.execute(
                    select(
                        SkillVersion.version_no,
                        SkillVersion.description,
                        SkillVersion.change,
                        SkillVersion.summary,
                        SkillVersion.author_kind,
                        SkillVersion.author_user_id,
                        SkillVersion.session_id,
                        SkillVersion.run_id,
                        SkillVersion.created_at,
                    )
                    .where(SkillVersion.org_id == org_id, SkillVersion.skill_id == skill_id)
                    .order_by(SkillVersion.version_no.desc())
                )
            ).all()
        return [
            {
                "version": row.version_no,
                "description": row.description,
                "change": row.change,
                "summary": row.summary,
                "author_kind": row.author_kind,
                "author_user_id": None if row.author_user_id is None else str(row.author_user_id),
                "session_id": None if row.session_id is None else str(row.session_id),
                "run_id": None if row.run_id is None else str(row.run_id),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    async def _expand_skill(self, access: SessionAccess, text: str) -> str:
        """The text of a skill command (Section 9.5), or ``text`` if it is not one."""
        if self._skills is None or not text.lstrip().startswith("/"):
            return text
        session = access.session
        async with self._engine.connect() as connection:
            actor = await self._skills.actor(
                connection,
                org_id=session.org_id,
                workspace_id=session.workspace_id,
                profile_id=session.agent_profile_id,
                user_id=access.user_id,
                private=session.audience == "private",
            )
        return await self._skills.expand(actor, text.strip()) or text

    async def _send(
        self,
        principal: Principal,
        session_id: uuid.UUID,
        text: str,
        parallel: bool,
        images: tuple[NewAttachment, ...] = (),
    ) -> JSONResponse:
        try:
            sent = await self.submit(principal, session_id, text, parallel=parallel, images=images)
        except SendError as error:
            headers = {}
            if error.retry_after_s is not None:
                headers["Retry-After"] = str(error.retry_after_s)
            content: dict[str, Any] = {"detail": error.detail}
            if error.quota is not None:
                content["quota"] = error.quota
            return JSONResponse(status_code=error.status, content=content, headers=headers)
        return JSONResponse(status_code=202, content=sent.model_dump(mode="json"))

    async def submit(
        self,
        principal: Principal,
        session_id: uuid.UUID,
        text: str,
        *,
        page: PageContext | None = None,
        parallel: bool = False,
        join: bool = True,
        images: tuple[NewAttachment, ...] = (),
    ) -> Sent:
        """Queues a user message, as ``POST /sessions/{id}/messages`` does. Raises
        ``SendError``.

        Busy modes (M7.1): in the mode ``steer`` (the default), a plain text message while a run
        of the session is running joins that run as a note (``steered``). ``parallel`` starts
        a run next to the open runs, whatever the mode. ``join=False`` never adds the message to
        a running run: the Chrome extension shows no notes.

        With ``page`` (the extension channel, M5.4), the message has the page of the tab as
        untrusted content, and a skill command is not expanded. Page content can hold private
        data of the user, so only the owner of a private session can share a page in it.
        """
        async with as_user(self._engine, principal.auth_user_id) as connection:
            try:
                # ``busy`` also reads whether the session has an open run, in the same statement.
                access = await session_access(connection, principal, session_id, busy=True)
            except HTTPException as error:
                raise SendError(error.status_code, str(error.detail)) from None
        attachments: tuple[NewAttachment, ...] = ()
        coding_agent: CodingAgent | None = None
        steer = False
        if page is None:
            command = parse_command(text)
            if command is not None and command.agent is not None and command.task:
                # One message for a coding agent (Section 15.4): no skill, and it never joins
                # a running run (M7.1).
                coding_agent, text = command.agent, command.task
            else:
                typed = text
                text = await self._expand_skill(access, text)
                steer = join and not parallel and not images and text == typed
        else:
            session = access.session
            if session.audience != "private" or session.owner_user_id != access.user_id:
                raise SendError(403, "share a page only in a private session of your own")
            try:
                message = page_message(text, page)
            except PageError as error:
                raise SendError(422, str(error)) from None
            text, attachments = message.text, message.attachments
        if images and page is None:
            # A note holds only text, so a message with images never joins a run. The text
            # names each image, as in the channels (Section 11.7).
            text, attachments = with_markers(text, images), images
        try:
            # One transaction: the note for a running run, or the queued run, and the check
            # whether it waits (Section 4.4).
            submitted = await self._queue.submit(
                org_id=access.org_id,
                session_id=session_id,
                text=text,
                user_id=access.user_id,
                attachments=attachments,
                coding_agent=None if coding_agent is None else coding_agent.value,
                parallel=parallel,
                human=True,
                steer=steer,
                open_run=access.open_run,
            )
            return Sent(run_id=submitted.run_id, queued=submitted.queued, steered=submitted.steered)
        except RateLimitedError as error:
            raise SendError(429, str(error), retry_after_s=error.retry_after_s) from None
        except QuotaExceededError as error:
            raise SendError(402, str(error), quota=error.quota) from None
        except QueueError as error:
            raise SendError(409, str(error)) from None

    async def _steer(self, principal: Principal, session_id: uuid.UUID, text: str) -> JSONResponse:
        """``POST /sessions/{id}/steer`` (M3.9): a note for the open run of the session. When no
        run is open, the note is a normal message."""
        access = await self._access(principal, session_id)
        try:
            steer_id = await self._queue.steer(
                org_id=access.org_id, session_id=session_id, text=text, user_id=access.user_id
            )
            if steer_id is not None:
                return JSONResponse(
                    status_code=202,
                    content={"steered": True, "run_id": str(steer_id), "queued": False},
                )
            run_id = await self._queue.enqueue(
                org_id=access.org_id,
                session_id=session_id,
                text=text,
                user_id=access.user_id,
                human=True,
            )
        except RateLimitedError as error:
            return JSONResponse(
                status_code=429,
                content={"detail": str(error)},
                headers={"Retry-After": str(error.retry_after_s)},
            )
        except QuotaExceededError as error:
            return _quota_answer(error)
        except QueueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        queued = await self._queue.waits(org_id=access.org_id, run_id=run_id)
        return JSONResponse(
            status_code=202, content={"steered": False, "run_id": str(run_id), "queued": queued}
        )

    async def _handoff(self, principal: Principal, session_id: uuid.UUID, to: str) -> HandoffOut:
        """``POST /sessions/{id}/handoff`` (M3.9, Section 11.1)."""
        access = await self._access(principal, session_id)
        if self._handoffs is None:
            raise HTTPException(status_code=409, detail="handoffs are not configured")
        try:
            moved = await self._handoffs.move(
                org_id=access.org_id,
                user_id=access.user_id,
                session_id=session_id,
                from_channel="web",
                from_key=None,
                to_channel=to,
            )
        except HandoffError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return HandoffOut(to_channel=moved.to_channel, text=moved.text)

    def _checkpoint_service(self) -> CheckpointService:
        if self._checkpoints is None:
            raise HTTPException(status_code=409, detail="checkpoints are not configured")
        return self._checkpoints

    async def _checkpoint_list(
        self, principal: Principal, session_id: uuid.UUID, limit: int
    ) -> list[CheckpointOut]:
        """``GET /sessions/{id}/checkpoints``: the checkpoints of the session files, newest
        first (M6.4, Section 15.3)."""
        access = await self._access(principal, session_id)
        service = self._checkpoint_service()
        try:
            listed = await service.listing(org_id=access.org_id, session_id=session_id, limit=limit)
        except CheckpointServiceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return [CheckpointOut.model_validate(item, from_attributes=True) for item in listed]

    async def _restore(
        self,
        principal: Principal,
        session_id: uuid.UUID,
        number: int | None,
        commit: str | None = None,
    ) -> RestoreOut:
        """``POST /sessions/{id}/undo`` and ``/rollback`` (M6.4, Section 15.3)."""
        access = await self._access(principal, session_id)
        service = self._checkpoint_service()
        try:
            if number is None:
                restored = await service.undo(
                    org_id=access.org_id, user_id=access.user_id, session_id=session_id
                )
            else:
                restored = await service.rollback(
                    org_id=access.org_id,
                    user_id=access.user_id,
                    session_id=session_id,
                    number=number,
                    commit=commit,
                )
        except CheckpointServiceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return RestoreOut(
            created_at=restored.created_at,
            files=restored.files,
            deleted=restored.deleted,
            kept=restored.kept,
            text=restored_text(restored, "/rollback"),
        )

    async def _coding_access(self, principal: Principal, session_id: uuid.UUID) -> CodingAccess:
        access = await self._access(principal, session_id)
        return CodingAccess(org_id=access.org_id, user_id=access.user_id)

    async def _access(self, principal: Principal, session_id: uuid.UUID) -> SessionAccess:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            return await session_access(connection, principal, session_id)

    async def _compact(
        self, principal: Principal, session_id: uuid.UUID, focus: str
    ) -> JSONResponse:
        """``POST /sessions/{id}/compact``: the busy rules of a message apply (Section 6.3)."""
        access = await self._access(principal, session_id)
        try:
            run_id = await self._queue.enqueue_compact(
                org_id=access.org_id, session_id=session_id, user_id=access.user_id, focus=focus
            )
        except RateLimitedError as error:
            return JSONResponse(
                status_code=429,
                content={"detail": str(error)},
                headers={"Retry-After": str(error.retry_after_s)},
            )
        except QuotaExceededError as error:
            return _quota_answer(error)
        except QueueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return JSONResponse(status_code=202, content={"run_id": str(run_id)})

    async def _lineage(self, principal: Principal, session_id: uuid.UUID) -> LineageOut:
        """The links of a session. The connection runs as the user, so a linked session that
        the user may not read is not in the answer."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
            up = await ancestors(connection, access.org_id, session_id)
            down = await children(connection, access.org_id, session_id)
        return LineageOut(
            ancestors=[LinkOut.model_validate(link, from_attributes=True) for link in up],
            children=[LinkOut.model_validate(link, from_attributes=True) for link in down],
        )

    async def _compactions(
        self, principal: Principal, session_id: uuid.UUID, limit: int
    ) -> list[CompactionOut]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
            rows = await list_compactions(connection, access.org_id, session_id, limit)
        return [CompactionOut.model_validate(row, from_attributes=True) for row in rows]

    async def _add_pin(self, principal: Principal, session_id: uuid.UUID, body: NewPin) -> PinOut:
        if (body.text is None) == (body.message_id is None):
            raise HTTPException(status_code=422, detail="give either text or message_id")
        access = await self._access(principal, session_id)
        try:
            if body.message_id is not None:
                pin = await self._pins.add_message(
                    org_id=access.org_id,
                    session_id=session_id,
                    user_id=access.user_id,
                    message_id=body.message_id,
                )
            else:
                pin = await self._pins.add(
                    org_id=access.org_id,
                    session_id=session_id,
                    user_id=access.user_id,
                    text=body.text or "",
                )
        except PinError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return _pin_out(pin)

    async def _cancel(self, principal: Principal, run_id: uuid.UUID) -> dict[str, str]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            session_id = (
                await connection.execute(select(Run.session_id).where(Run.id == run_id))
            ).scalar_one_or_none()
            if session_id is None:
                raise _not_found("the run does not exist")
            access = await session_access(connection, principal, session_id)
        try:
            status = await self._queue.cancel(org_id=access.org_id, run_id=run_id)
        except QueueError:
            raise _not_found("the run does not exist") from None
        return {"status": status}

    async def _approval_target(
        self, principal: Principal, approval_id: uuid.UUID
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
        """The org, the user row, the run, and the call id of an approval that the user may
        decide. A user who reads the session decides here. The proposal of a background review
        is also open to the workspace admin and the org owner, who may not read the private
        session (Section 9.4); the gate checks their role. Raises ``404``."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            # One statement reads the approval, the session, the user, and the memberships.
            rows = (
                await connection.execute(
                    _access_select(
                        principal,
                        Approval.run_id,
                        Approval.call_id,
                        source=Session.__table__.join(
                            Approval,
                            and_(
                                Approval.org_id == Session.org_id, Approval.session_id == Session.id
                            ),
                        ),
                    ).where(Approval.id == approval_id)
                )
            ).all()
            try:
                access = _access_from(rows)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
            else:
                return access.org_id, access.user_id, rows[0].run_id, rows[0].call_id
        proposal = await self._review_approval(principal, approval_id)
        if proposal is None:
            raise _not_found("the approval does not exist")
        return proposal

    async def _review_approval(
        self, principal: Principal, approval_id: uuid.UUID
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str] | None:
        """The approval of a review proposal in a workspace that the user sees. It reads as
        the table owner, because the user may not read the session. It gives ids only."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(Approval.org_id, Approval.run_id, Approval.call_id, Session.workspace_id)
                    .join(
                        ReviewProposal,
                        and_(
                            ReviewProposal.org_id == Approval.org_id,
                            ReviewProposal.approval_id == Approval.id,
                        ),
                    )
                    .join(
                        Session,
                        and_(Session.org_id == Approval.org_id, Session.id == Approval.session_id),
                    )
                    .where(Approval.id == approval_id)
                )
            ).one_or_none()
            if row is None:
                return None
            user_id = await _user_id(connection, principal, row.org_id)
            if user_id is None:
                return None
            grants = await load_grants(connection, row.org_id, user_id)
            if not grants.sees_workspace(row.workspace_id):
                return None
        return row.org_id, user_id, row.run_id, row.call_id

    async def review_proposals(
        self, principal: Principal, workspace_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """The open proposals of background reviews that the user may decide in a workspace:
        ``agent`` and ``workspace`` scope for a workspace admin, ``org`` scope for the org
        owner. The user need not read the session. The answer has the redacted proposal and
        no session, run, or message (Section 9.4)."""
        async with self._engine.connect() as connection:
            org_id = (
                await connection.execute(
                    select(Workspace.org_id).where(Workspace.id == workspace_id)
                )
            ).scalar_one_or_none()
            user_id = None if org_id is None else await _user_id(connection, principal, org_id)
            if org_id is None or user_id is None:
                raise _not_found("the workspace does not exist")
            grants = await load_grants(connection, org_id, user_id)
            if not grants.sees_workspace(workspace_id):
                raise _not_found("the workspace does not exist")
            rows = await connection.execute(
                select(
                    ReviewProposal.id,
                    ReviewProposal.kind,
                    ReviewProposal.scope,
                    ReviewProposal.payload,
                    ReviewProposal.origin,
                    Approval.id.label("approval_id"),
                    Approval.created_at,
                    Approval.expires_at,
                )
                .join(
                    Approval,
                    and_(
                        Approval.org_id == ReviewProposal.org_id,
                        Approval.id == ReviewProposal.approval_id,
                    ),
                )
                .join(
                    Session,
                    and_(
                        Session.org_id == ReviewProposal.org_id,
                        Session.id == ReviewProposal.session_id,
                    ),
                )
                .where(
                    ReviewProposal.org_id == org_id,
                    Session.workspace_id == workspace_id,
                    ReviewProposal.status == "pending",
                    Approval.status == "pending",
                )
                .order_by(Approval.created_at, Approval.id)
            )
            # The stored text is not shown. The redactor of the process may know more secrets
            # now, so the view is made again from the payload (Section 9.4).
            views = [
                (row, shown_proposal(row.kind, row.scope, row.payload, row.origin))
                for row in rows
                if (
                    grants.org_owner
                    if row.scope == "org"
                    else grants.admins_workspace(workspace_id)
                )
            ]
            return [
                {
                    "id": str(row.id),
                    "approval_id": str(row.approval_id),
                    "kind": row.kind,
                    "scope": row.scope,
                    "summary": shown.reason,
                    "proposal": shown.payload,
                    "diff": shown.diff,
                    "created_at": row.created_at.isoformat(),
                    "expires_at": row.expires_at.isoformat(),
                }
                for row, shown in views
            ]

    async def _decide(
        self,
        principal: Principal,
        approval_id: uuid.UUID,
        approve: bool,
        always_allow: bool,
        reason: str | None,
        scope: ApprovalScope | None,
    ) -> JSONResponse:
        org_id, user_id, run_id, call_id = await self._approval_target(principal, approval_id)

        async def release(connection: AsyncConnection) -> list[uuid.UUID]:
            # An expired approval resumes the run as a reject, so it is released too.
            return await self._queue.release_decided_in(connection, org_id=org_id, run_id=run_id)

        try:
            decision = await self._gate.decide_with_scope(
                org_id=org_id,
                run_id=run_id,
                call_id=call_id,
                user_id=user_id,
                approve=approve,
                always_allow=always_allow,
                scope=scope,
                reason=reason,
                release=release,
            )
        except ApprovalExpiredError as error:
            # The release ran in the transaction of the decision: the run resumes as a reject.
            await self._apply_reviews(org_id, run_id)
            return JSONResponse(status_code=409, content={"detail": str(error)})
        except ApprovalScopeError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except ApprovalError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        if decision.proposal:
            # Only a review proposal has more to do (Section 9.4).
            await self._apply_reviews(org_id, run_id)
        return JSONResponse(
            content={
                "status": decision.status,
                "scope": decision.scope,
                "resumed": bool(decision.released),
            }
        )

    async def _apply_reviews(self, org_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """Applies a decided proposal of a background review at once (Section 9.4). The worker
        does the same in its maintenance round, so a failure here only delays the change."""
        if self._reviews is None:
            return
        try:
            await self._reviews.apply_decided(org_id=org_id, run_id=run_id)
        except Exception:
            log.exception("the review proposals could not be applied run=%s", run_id)

    async def _answer(
        self, principal: Principal, session_id: uuid.UUID, request_id: str, answers: Sequence[str]
    ) -> None:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
        broker = self._clarify
        if broker is None or request_id not in {
            r.id for r in broker.pending(str(access.org_id), str(session_id))
        }:
            raise _not_found("the question is not open")
        try:
            broker.answer(str(access.org_id), request_id, answers)
        except ClarifyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    # -- the event stream -------------------------------------------------------------------

    def open_stream(self, user: uuid.UUID) -> Callable[[], None]:
        """Takes one stream slot of the user, or raises ``429``. Returns the release. The
        extension channel takes one slot for each session that it follows."""
        settings = self._settings
        if (
            self._streams.total() >= settings.max_streams
            or self._streams[user] >= settings.max_streams_per_user
        ):
            raise HTTPException(
                status_code=429,
                detail="too many open event streams. Close a page and try again.",
                headers={"Retry-After": "5"},
            )
        self._streams[user] += 1
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._streams[user] -= 1
                if self._streams[user] <= 0:
                    del self._streams[user]

        return release

    def _stream_lifetime(self, principal: Principal) -> float:
        """Seconds until the stream ends: ``max_stream_s``, or the token expiry if earlier."""
        lifetime = self._settings.max_stream_s
        if principal.expires_at is not None:
            lifetime = min(lifetime, principal.expires_at - time.time())
        return lifetime

    async def first_cursor(
        self, principal: Principal, session_id: uuid.UUID, after: int | None
    ) -> int:
        """Checks the access to the session and returns the cursor of a new stream: ``after``,
        or the last event of the session. Raises ``404``."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            await session_access(connection, principal, session_id)
            if after is None:
                return await self._last_event_id(connection, session_id)
        return after

    async def _stream(
        self, principal: Principal, session_id: uuid.UUID, cursor: int, lifetime: float
    ) -> AsyncIterator[str]:
        """Server-sent events of a session (``session_events``). The SSE id of a
        ``run_event`` is the ``run_events.id``."""
        end = asyncio.get_running_loop().time() + lifetime
        yield "retry: 2000\n\n"
        async for item in self.session_events(principal, session_id, cursor, lambda: end):
            if item.event == "ping":
                yield ": ping\n\n"
            else:
                yield _sse(item.event, item.data, item.event_id)

    async def session_events(
        self,
        principal: Principal,
        session_id: uuid.UUID,
        cursor: int,
        deadline: Callable[[], float],
    ) -> AsyncIterator[StreamEvent]:
        """The events of a session after ``cursor``, until ``deadline()`` (event loop time).

        - ``runs``: the first event. The status of each open run and of the recent runs. A
          client that connects again uses it to correct the runs that it shows as open.
        - ``run_event``: one stored run event. ``event_id`` is the ``run_events.id``. One
          worker writes the events of a session at a time, so the ids of a session grow in the
          order of the commits.
        - ``run``: the status of a run changed.
        - ``end``: the user cannot read the session any more. The events stop.
        - ``ping``: the heartbeat, every ``heartbeat_s``.

        A poll waits ``poll_s``. With ``signals``, a store of the worker of this process ends
        the wait early (Section 4.4). Each poll checks the access again, with both guards. The
        caller sets the deadline to the token expiry, so that a stream does not live longer than
        its token. The extension channel moves the deadline when the client sends a fresh
        token.
        """
        settings = self._settings
        loop = asyncio.get_running_loop()
        next_ping = loop.time() + settings.heartbeat_s
        statuses: dict[uuid.UUID, str] = {}
        first = True
        signals = self._signals
        key = str(session_id)
        while loop.time() < deadline():
            # The ticket comes before the read, so a store during the read wakes the next wait.
            ticket = signals.ticket(key) if signals is not None else None
            out: list[StreamEvent] = []
            async with (
                self._poll_slots,
                as_user(self._engine, principal.auth_user_id) as connection,
            ):
                try:
                    access = await session_access(connection, principal, session_id)
                except HTTPException:
                    yield StreamEvent("end", {"reason": "the session is not visible"})
                    return
                events = await connection.execute(
                    select(
                        RunEvent.id,
                        RunEvent.run_id,
                        RunEvent.seq,
                        RunEvent.type,
                        RunEvent.payload,
                    )
                    .where(
                        RunEvent.org_id == access.org_id,
                        RunEvent.session_id == session_id,
                        RunEvent.run_id.in_(_chat_runs(session_id)),
                        RunEvent.id > cursor,
                    )
                    .order_by(RunEvent.id)
                    .limit(STREAM_BATCH)
                )
                for event in events:
                    cursor = event.id
                    out.append(
                        StreamEvent(
                            "run_event",
                            {
                                "run_id": str(event.run_id),
                                "seq": event.seq,
                                "type": event.type,
                                "payload": event.payload,
                            },
                            event.id,
                        )
                    )
                runs = await self._stream_runs(connection, access.org_id, session_id, statuses)
                if first:
                    out.insert(0, StreamEvent("runs", {"runs": [_run_out(run) for run in runs]}))
                changed = [run for run in runs if statuses.get(run.id) != run.status]
                statuses.update((run.id, run.status) for run in runs)
                if not first:
                    out += [StreamEvent("run", _run_out(run)) for run in changed]
            first = False
            if loop.time() >= deadline():
                return  # The token can have expired during the reads.
            for item in out:
                yield item
            if loop.time() >= next_ping:
                next_ping = loop.time() + settings.heartbeat_s
                yield StreamEvent("ping", {})
            if len(out) < STREAM_BATCH:
                wait = max(0.0, min(settings.poll_s, deadline() - loop.time()))
                if ticket is None:
                    await asyncio.sleep(wait)
                elif await SessionSignals.wait(ticket, wait):
                    # A store in this process: read soon, but not more often than
                    # ``wake_s``, so a fast text stream does not read for each flush.
                    await asyncio.sleep(max(0.0, min(settings.wake_s, deadline() - loop.time())))

    @staticmethod
    async def _stream_runs(
        connection: AsyncConnection,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        statuses: dict[uuid.UUID, str],
    ) -> list[Any]:
        """The recent runs, every open run, and each run that the stream last saw open.

        A run that the stream saw open stays in the poll until the stream sends its end
        status, also when more than ``RECENT_RUNS`` newer runs are in the session.
        """
        columns = (Run.id, Run.status, Run.failure_code, Run.failure_message)
        scope = (Run.org_id == org_id, Run.session_id == session_id, Run.kind == "chat")
        recent = await connection.execute(
            select(*columns)
            .where(*scope)
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(RECENT_RUNS)
        )
        watched = [run_id for run_id, status in statuses.items() if status in OPEN_RUN_STATUSES]
        rows = {run.id: run for run in recent}
        if len(rows) < RECENT_RUNS:
            # The recent runs are all the chat runs of the session: no older run is open.
            return list(rows.values())
        older = await connection.execute(
            select(*columns).where(
                *scope,
                or_(Run.status.in_(OPEN_RUN_STATUSES), Run.id.in_(watched)),
                Run.id.not_in(list(rows)),
            )
        )
        rows.update((run.id, run) for run in older)
        return list(rows.values())
