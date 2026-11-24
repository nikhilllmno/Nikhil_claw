"""The control plane process (docs/architecture.md, Section 19.1).

One process holds the HTTP API, the sama MCP tool server, the model proxy, the run worker, the
monitor, and, when ``SAMA_VAULT_KEY`` is set, the vault and the integrations with the Telegram
and Slack hubs (Section 11.6). They share one database engine.
The clarify broker is in memory, so the API that answers a question and the tool server that
asks it must be in the same process.

Paths:

- ``/``: the API and the web page (Section 11.4), ``/telegram/<integration id>/webhook``, and
  ``/slack/<integration id>/{events,interactions,commands}``.
- ``/tools``: the tool server. The sandbox runner uses ``/tools/mcp`` and ``/tools/gate``.
- ``/model``: the model proxy. The runner uses ``/model/anthropic`` and ``/model/openai``.
- ``/v1`` and ``/mcp``: sama as a server for other agents (Section 10.7): the
  OpenAI-compatible endpoint and the MCP server. An org API key is the bearer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import uuid
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount, Route

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.api.auth import issuer, jwks_url
from sama.api.browser import BrowserSettings
from sama.apikeys import ApiKeyStore
from sama.billing import BillingService, StripeClient
from sama.browser.browserbase import BrowserbaseTarget
from sama.browser.chrome import AuthUsers, ChromeTarget
from sama.browser.contexts import PostgresContextStore
from sama.browser.guard import UrlGuard
from sama.browser.logins import BrowserLogins
from sama.browser.policy import PolicyCache, PolicyStore
from sama.browser.preference import SessionBrowserChoice
from sama.browser.service import BrowserService
from sama.browser.target import CLOUD, BrowserTarget
from sama.browser.task import BrowserTaskRunner, ProxyModelClient
from sama.coding.runtime import CodingAgentRuntime
from sama.coding.service import CodingService
from sama.coding.tool import CodingTools
from sama.coding_mode.harness import HarnessHooks
from sama.coding_mode.prepare import CodingPreparer
from sama.coding_mode.service import CodingService as CodingModeService
from sama.coding_mode.tool import PlanTool
from sama.connectors.chat import connection_escalation
from sama.connectors.database import DatabaseGateway
from sama.connectors.host import ConnectorHosts, E2BHostBackend
from sama.connectors.manifest import Catalog
from sama.connectors.resume import ConnectionResumer, SigninPoller
from sama.connectors.service import ConnectionService
from sama.context import ContextArchive
from sama.context.session_items import PostgresSessionItems
from sama.controlplane.settings import ControlPlaneSettings
from sama.customtools import CustomToolService
from sama.datacontrols.purge import MemoryDeleter, PurgeWorker, SandboxBackend
from sama.datacontrols.service import DataControls, ServiceCleanup
from sama.datacontrols.sweep import RetentionSweep
from sama.db.engine import create_engine
from sama.db.monitor import PoolMonitor, RequestMonitor
from sama.db.pool import named, prewarm
from sama.delegation import DelegationService
from sama.gate.service import Escalation, GateService
from sama.gate.wire import GATE_PATH
from sama.gateway.chrome import ChromeHub
from sama.gateway.handoff import Handoffs
from sama.github.client import GitHubClient
from sama.github.proxy import GitHubUpstream, GitProxy
from sama.github.service import GitHubService
from sama.integrations.email_hub import EmailHub
from sama.integrations.hub import TelegramHub
from sama.integrations.service import Integrations
from sama.integrations.slack_hub import SlackHub
from sama.integrations.whatsapp_hub import WhatsAppHub
from sama.media.files import FileSender
from sama.media.generate import GeneratedMediaStore, ImageService, SpeechService
from sama.media.meter import MediaMeter
from sama.media.store import AttachmentStore
from sama.media.video import VideoService
from sama.media.vision import VisionService
from sama.memory import MemoryService, PostgresMemoryStore
from sama.memory.provider import Mem0Provider, MemoryRecall, MemorySync
from sama.moa import MIN_REFERENCES, MOA_KEYS, MixtureService
from sama.modelkeys import ModelKeys
from sama.modelproxy.app import ModelProxy, ProxySettings
from sama.modelproxy.ledger import UsageLedger
from sama.modelproxy.prices import PriceTable
from sama.modelproxy.store import PostgresUsageStore
from sama.observability.alerts import ErrorRateAlerter, SlackWebhook
from sama.observability.langfuse import LangfuseClient
from sama.observability.traces import TraceExporter
from sama.orchestrator import PostgresSandboxDirectory, RunQueue, Worker
from sama.orchestrator.checkpoints import CheckpointService
from sama.orchestrator.prewarm import SessionWarmer
from sama.orchestrator.references import ReferenceExpander
from sama.orchestrator.sandboxed import WARM_RUNNER_ARGV, SandboxedRuntime, SandboxSettings
from sama.orchestrator.signals import SessionSignals
from sama.prompt import PromptAssembler
from sama.review.apply import ReviewApplier
from sama.review.change import ChangeProposer
from sama.review.proposals import PostgresProposalStore
from sama.runtime.archive import ARCHIVE_PATH
from sama.runtime.contract import AgentRuntime, McpServerConfig, RuntimeKind, RuntimeRegistry
from sama.runtime.session_items import SESSION_ITEMS_PATH
from sama.sandbox.base import SandboxProvider
from sama.scheduler.delivery import Deliverer, EmailConfig, Sender
from sama.scheduler.jobs import JobService
from sama.scheduler.service import Scheduler
from sama.search import EmbedWorker, OpenAIEmbedder, SessionSearch
from sama.server import OpenAICompat, SamaMcpServer, ServerCore
from sama.server.mcp import MCP_PATH as SERVER_MCP_PATH
from sama.skills import BuiltinSkills, PostgresSkillStore, SessionSkillSource, SkillLibrary
from sama.skills.importer import SkillImporter
from sama.skills.settings import SkillSettingsStore
from sama.tasks.dispatcher import TaskDispatcher
from sama.tasks.runs import TaskTools
from sama.tenancy.regions import set_home_regions
from sama.tenancy.run_token import RunTokenSigner
from sama.tools.clarify import ClarifyBroker
from sama.tools.code import CodeRunner
from sama.tools.deferred import DeferredCatalog
from sama.tools.github import GitHubTools
from sama.tools.outbound import OutboundClient
from sama.tools.output import EXPORT_DIR, SandboxSpill
from sama.tools.repos import RepoCloner
from sama.tools.sama_pr import SamaPr
from sama.tools.server import MCP_PATH, ToolServer
from sama.tools.todo import PostgresTodoStore
from sama.tools.web import BraveSearch
from sama.tools.x import XSearch
from sama.triggers.service import TriggerService
from sama.vault.redact import default_redactor
from sama.vault.store import OrgKeys, SecretName, Vault
from sama.workflows.engine import ConnectorStepTools, WorkflowEngine
from sama.workflows.service import WorkflowService

log = logging.getLogger("sama.controlplane")

MCP_SERVER_NAME = "sama"
"""The Claude CLI names the tools of this server ``mcp__sama__<tool>`` (Section 16.1)."""
DRAIN_S = 60.0
"""How long a stop waits for the current runs while HTTP still serves. A run that is still
open after that is cancelled. Its lease runs out, and the next worker ends it (Section 4.3)."""
SANDBOX_CLOSE_S = 15.0
"""How long a stop tries to pause the session sandboxes. E2B pauses an idle sandbox by itself,
so this step is optional."""


@dataclass
class ControlPlane:
    """The parts of the process. Tests use them to look inside."""

    app: Starlette
    engine: AsyncEngine
    worker: Worker
    api: SamaApi
    tools: ToolServer
    proxy: ModelProxy
    sandboxed: SandboxedRuntime | None
    stop: asyncio.Event
    """Set at the start of a stop: the worker claims no more runs."""
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    """The background loops while the app runs."""

    async def drain(self) -> None:
        """Stops the loops and waits for the current runs. Call it while HTTP still serves,
        because the runs need the model proxy and the tool server. After ``DRAIN_S`` the loops
        are cancelled."""
        self.stop.set()
        tasks = list(self.tasks)
        if not tasks:
            return
        _done, open_tasks = await asyncio.wait(tasks, timeout=DRAIN_S)
        for task in open_tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def abort(self) -> None:
        """Cancels the loops at once, for a second stop signal."""
        self.stop.set()
        for task in self.tasks:
            task.cancel()


def build(
    settings: ControlPlaneSettings,
    *,
    engine: AsyncEngine | None = None,
    provider: SandboxProvider | None = None,
    runtimes: Sequence[AgentRuntime] | None = None,
    worker_poll_s: float = 1.0,
    exit_on_failure: bool = True,
) -> ControlPlane:
    """Builds the process. ``runtimes`` replaces the sandboxed runtimes, for tests.
    ``exit_on_failure`` stops the process when a background loop ends with an error, so
    that the platform starts it again."""
    default_redactor().add(*settings.secret_values())
    # The run queue and the API serve only the orgs of the home region (Section 17.3).
    set_home_regions(settings.regions)
    engine = engine or create_engine(
        settings.database_url.get_secret_value(), pool=settings.db_pool
    )
    vault = Vault(engine, settings.vault_key) if settings.vault_key is not None else None
    keys = OrgKeys(
        vault,
        {
            SecretName.ANTHROPIC_API_KEY: settings.anthropic_key,
            SecretName.OPENAI_API_KEY: settings.openai_key,
            SecretName.BRAVE_API_KEY: settings.brave_key,
            SecretName.COMPOSIO_API_KEY: settings.composio_key,
            SecretName.GEMINI_API_KEY: settings.gemini_key,
            SecretName.X_BEARER_TOKEN: settings.x_bearer_token,
            SecretName.MEM0_API_KEY: settings.mem0_key,
        },
    )
    # The external memory of Section 8.2 (M7.5). Without a Mem0 key for any org, it is off.
    mem0 = Mem0Provider(keys) if keys.available(SecretName.MEM0_API_KEY) else None
    memory_sync = MemorySync(engine, mem0) if mem0 is not None else None
    signer = RunTokenSigner(settings.run_token_key.get_secret_value().encode())
    # The deferred tools are in one catalog. The gate reads their toolsets from it, and the tool
    # server runs them (Section 10.3). The connector tools of the catalog in
    # ``connectors/catalog/`` are deferred tools of the toolset ``integrations``.
    deferred = DeferredCatalog()
    # The database gateway runs the tools of the database connectors (Section 10.4). It
    # writes large results and exports to the session sandbox, set below.
    database = DatabaseGateway()
    # A stdio MCP server runs in its own E2B sandbox, the connector host (Section 10.3.2).
    hosts: ConnectorHosts | None = None
    # The purge worker kills the sandboxes of deleted sessions with the same backend.
    e2b: E2BHostBackend | None = None
    if runtimes is None:
        e2b = E2BHostBackend(settings.e2b_api_key.get_secret_value())
        hosts = ConnectorHosts(e2b, template=settings.connector_host_template)
    connections = ConnectionService(
        engine,
        Catalog.load(),
        keys=keys,
        vault=vault,
        hosts=hosts,
        web_base=settings.web_base,
        database=database,
        # The sign-in return pages are on the control plane: they need no web session (M8.1).
        return_base=settings.public_url or None,
    )
    connections.register_tools(deferred)
    # The custom tools of users, workspaces, and orgs (Section 10.8). The sandboxes are set
    # below, when the process has a sandbox provider.
    custom_tools = CustomToolService(engine, vault)
    # The gate also reads the tools of the remote MCP servers and the custom tools that each
    # run may use, and the policy of each custom tool.
    gate = GateService(
        engine,
        extra_toolsets=deferred,
        run_tools=(connections, custom_tools),
        run_rules=custom_tools,
    )
    broker = ClarifyBroker()
    library = SkillLibrary(engine, BuiltinSkills().all())
    # A background review proposes the changes of shared memory and shared skills. A person
    # approves them, and the applier writes them (Section 9.4).
    proposals = PostgresProposalStore(engine)
    memory_store = PostgresMemoryStore(engine)
    skill_store = PostgresSkillStore(engine, library.builtin, proposals)
    reviews = ReviewApplier(engine, memory_store, skill_store)

    registry = RuntimeRegistry()
    sandboxed: SandboxedRuntime | None = None
    if runtimes is None:
        if provider is None:
            from sama.sandbox.e2b import E2BProvider

            provider = E2BProvider(api_key=settings.e2b_api_key.get_secret_value())
        sandboxed = SandboxedRuntime(
            kind=RuntimeKind.CLAUDE_AGENT_SDK,
            provider=provider,
            signer=signer,
            settings=SandboxSettings(
                template=settings.sandbox_template,
                model_url=settings.model_url,
                gate_url=f"{settings.tools_url}{GATE_PATH}",
                archive_url=f"{settings.tools_url}{ARCHIVE_PATH}",
                session_url=f"{settings.tools_url}{SESSION_ITEMS_PATH}",
                allow_out=settings.allow_out,
                # The warm runner saves the Python start of each run (Section 16.2).
                runner_argv=WARM_RUNNER_ARGV,
                # The warm runner keeps the CLI between the runs of a session (Section 4.4).
                keep_runner_s=settings.runner_keep_s,
            ),
            directory=PostgresSandboxDirectory(engine),
            skills=SessionSkillSource(engine, library),
        )
        # The OpenAI runtime runs in the same session sandbox (M3.6). A profile selects it.
        runtimes = (sandboxed, sandboxed.for_kind(RuntimeKind.OPENAI_AGENTS_SDK))
        database.spill = SandboxSpill(sandboxed.session_sandbox)
        database.exports = SandboxSpill(sandboxed.session_sandbox, folder=EXPORT_DIR)
        # A script tool runs in the session sandbox. Its test runs in a scratch sandbox.
        custom_tools.session_sandbox = sandboxed.session_sandbox
        custom_tools.test_sandbox = sandboxed.scratch_sandbox
    for runtime in runtimes:
        registry.register(runtime)

    queue = RunQueue(engine)
    # One outbound client for ``web_extract`` and ``@url`` references: the same address rules
    # apply to both (Section 6.3). The tool server closes it.
    outbound = OutboundClient()
    # The browser (Section 14.1): the cloud browser and the Chrome of the user (the extension
    # connections of the API, M5.5). The worker closes the browser sessions of a run when the
    # run ends. The gate asks the user before a sensitive action in the user's Chrome.
    chrome = ChromeHub()
    browser = _browser_service(settings, engine, chrome, vault)
    gate.escalation = _escalation(browser.sensitive_reason)
    # The worker wakes the event streams of this process when it stores events (Section 4.4).
    signals = SessionSignals()
    warmer = SessionWarmer(sandboxed.warm) if sandboxed is not None else None
    worker = Worker(
        queue,
        registry,
        config=PromptAssembler(
            engine,
            mcp_servers=(
                McpServerConfig(name=MCP_SERVER_NAME, url=f"{settings.tools_url}{MCP_PATH}"),
            ),
            skills=library,
        ),
        gate=gate,
        reviews=reviews,
        references=ReferenceExpander(
            outbound=outbound,
            sessions=SessionSearch(engine),
            session_sandbox=sandboxed.session_sandbox if sandboxed else None,
        ),
        poll_s=worker_poll_s,
        on_run_end=[browser.end_run, *([memory_sync] if memory_sync is not None else [])],
        signals=signals,
    )
    # Without an OpenAI key, there is no embed worker, and session search uses the full-text
    # index alone (Section 7.1).
    embedder = (
        OpenAIEmbedder(keys, redactor=default_redactor())
        if keys.available(SecretName.OPENAI_API_KEY)
        else None
    )
    # The sama GitHub App (Section 15.1). The git proxy holds the installation tokens: a
    # sandbox clones and pushes through ``<public_url>/git`` with its run token.
    github: GitHubService | None = None
    github_tools: GitHubTools | None = None
    upstream: GitHubUpstream | None = None
    if settings.github is not None:
        github_client = GitHubClient(settings.github)
        github = GitHubService(
            engine, github_client, state_key=settings.run_token_key.get_secret_value().encode()
        )
        github_tools = GitHubTools(github)
        upstream = GitHubUpstream(github_client, web_url=settings.github.web_url)

    # The session_search tool and the search page of the web app share one search.
    sessions = SessionSearch(engine, embedder)
    usage = PostgresUsageStore(engine)
    # One ledger for every spender of model money: a record that fails to write fences them all.
    ledger = UsageLedger(usage)
    prices = PriceTable.load()
    proxy = ModelProxy(
        signer=signer,
        store=usage,
        prices=prices,
        settings=ProxySettings(
            anthropic_key=settings.anthropic_key, openai_key=settings.openai_key
        ),
        keys=keys,
        ledger=ledger,
    )
    # The browser sub-agent calls its model through the proxy app in this process (M5.6).
    proxy_app = proxy.app()
    browser_tasks = (
        BrowserTaskRunner(
            browser,
            ProxyModelClient(app=proxy_app),
            prices,
            model=settings.browser_task_model,
            gate=gate,
            progress=worker,
        )
        # The sub-agent works on the cloud target only (Section 14.1, M5.6).
        if CLOUD in browser.targets
        else None
    )
    jobs = JobService(engine, RunQueue(engine))
    # The workflow engine of Section 12.3 runs tool steps with the connector tools.
    workflows = WorkflowService(engine, RunQueue(engine), connections.tool_access)
    workflow_engine = WorkflowEngine(engine, RunQueue(engine), ConnectorStepTools(connections))
    # The media tools use the OpenAI key of the org (Section 11.7). Without a key, they are
    # hidden.
    media = GeneratedMediaStore(engine)
    openai_media = keys.available(SecretName.OPENAI_API_KEY)
    meter = MediaMeter(usage=usage, prices=prices, ledger=ledger)
    cloner = (
        RepoCloner(
            sandboxed.session_sandbox,
            proxy=settings.git_url if github_tools is not None else None,
            access=github_tools,
        )
        if sandboxed
        else None
    )
    # Claude Code and Codex in the session sandbox (Section 15.4): the runtimes claude_code and
    # codex, and the tool coding_agent_run. The CLIs reach models only through the model proxy
    # and git only through the git proxy.
    coding: CodingTools | None = None
    coding_runtimes: list[CodingAgentRuntime] = []
    if sandboxed is not None:
        coding_service = CodingService(
            model_url=settings.model_url, cloner=cloner, github=github_tools
        )
        claude_code = CodingAgentRuntime(
            kind=RuntimeKind.CLAUDE_CODE,
            sandboxes=sandboxed,
            signer=signer,
            service=coding_service,
            gate=gate,
        )
        codex = claude_code.for_kind(RuntimeKind.CODEX)
        registry.register(claude_code)
        registry.register(codex)
        coding_runtimes = [claude_code, codex]
        # The progress lines go to the run events of the run through the worker (Section 15.4).
        coding = CodingTools(coding_service, sandboxed.session_sandbox, progress=worker)
    # ``sama_pr`` changes the code of sama through the GitHub App (Section 15.2).
    sama_pr = (
        SamaPr(
            github,
            cloner=cloner,
            session_sandbox=sandboxed.session_sandbox if sandboxed else None,
            repo=settings.self_repo,
        )
        if github is not None
        else None
    )
    # Coding mode (Section 15.5): attached repos clone before a run, and plan mode.
    coding_mode = CodingModeService(engine, queue)
    if cloner is not None:
        preparer = CodingPreparer(coding_mode, cloner, signer)
        for runtime in runtimes:
            if isinstance(runtime, SandboxedRuntime):
                runtime.set_preparer(preparer)
    tools = ToolServer(
        signer=signer,
        plans=PlanTool(coding_mode),
        todos=PostgresTodoStore(engine),
        search=(
            BraveSearch(lambda org_id: keys.get(org_id, SecretName.BRAVE_API_KEY))
            if keys.available(SecretName.BRAVE_API_KEY)
            else None
        ),
        clarify=broker,
        spill=SandboxSpill(sandboxed.session_sandbox) if sandboxed else None,
        repos=cloner,
        github=github_tools,
        sama_pr=sama_pr,
        # The program reaches the tool server at the same URL as the agent runtime (M4.8).
        code=(
            CodeRunner(sandboxed.session_sandbox, f"{settings.tools_url}{MCP_PATH}")
            if sandboxed
            else None
        ),
        memory=MemoryService(memory_store, proposals),
        skills=skill_store,
        changes=ChangeProposer(engine, proposals),
        sessions=sessions,
        vision=(
            VisionService(
                store=AttachmentStore(engine),
                keys=keys,
                usage=usage,
                prices=prices,
                model=settings.vision_model,
                redactor=default_redactor(),
                ledger=ledger,
            )
            if keys.available(SecretName.ANTHROPIC_API_KEY)
            else None
        ),
        images=(
            ImageService(store=media, keys=keys, meter=meter, redactor=default_redactor())
            if openai_media
            else None
        ),
        speech=(
            SpeechService(store=media, keys=keys, meter=meter, redactor=default_redactor())
            if openai_media
            else None
        ),
        deferred=deferred,
        connections=connections,
        importer=SkillImporter(skill_store),
        outbound=outbound,
        steers=queue,
        archive=ContextArchive(engine),
        session_items=PostgresSessionItems(engine),
        gate=gate,
        delegation=DelegationService(engine, queue),
        tasks=TaskTools(engine),
        jobs=jobs,
        workflows=workflows,
        browser=browser,
        browser_tasks=browser_tasks,
        coding=coding,
        custom_tools=custom_tools,
        # Files of the sandbox for the user (Section 11.10, M8.6). It needs a sandbox, no key.
        files=FileSender(engine, sandboxed.session_sandbox, store=media) if sandboxed else None,
        # The Hermes tool parity tools of Section 10.2.2 (M7.5). Each is hidden without its key.
        video=(
            VideoService(
                keys=keys,
                meter=meter,
                session_sandbox=sandboxed.session_sandbox if sandboxed else None,
                redactor=default_redactor(),
            )
            if keys.available(SecretName.GEMINI_API_KEY)
            else None
        ),
        x=XSearch(keys) if keys.available(SecretName.X_BEARER_TOKEN) else None,
        moa=(
            MixtureService(
                keys=keys,
                meter=meter,
                max_cost_usd=settings.moa_max_cost_usd,
                redactor=default_redactor(),
            )
            if sum(keys.available(name) for name in MOA_KEYS) >= MIN_REFERENCES
            else None
        ),
        recall=MemoryRecall(engine, mem0) if mem0 is not None else None,
        skill_settings=SkillSettingsStore(engine),
    )
    handoffs = Handoffs(engine, web_url=settings.web_base)
    # /undo and /rollback restore the files of the session sandbox (Section 15.3).
    checkpoints = CheckpointService(engine, sandboxed.session_sandbox) if sandboxed else None
    # Coding mode for Claude Code and Codex (Section 15.5): clone, note, checkpoint, plan mode.
    if cloner is not None:
        hooks = HarnessHooks(CodingPreparer(coding_mode, cloner, signer), coding_mode, checkpoints)
        for coding_runtime in coding_runtimes:
            coding_runtime.set_hooks(hooks)
    # The same vault as the proxy: a key that an admin sets applies at once here (M6.7).
    model_keys = ModelKeys(vault, keys) if vault is not None else None
    hub = (
        TelegramHub(
            engine,
            vault,
            keys=keys,
            gate=gate,
            skills=library,
            reviews=reviews,
            connections=connections,
            jobs=jobs,
            handoffs=handoffs,
            checkpoints=checkpoints,
            coding_mode=coding_mode,
        )
        if vault is not None
        else None
    )
    slack = (
        SlackHub(
            engine,
            vault,
            gate=gate,
            skills=library,
            reviews=reviews,
            connections=connections,
            jobs=jobs,
            handoffs=handoffs,
            checkpoints=checkpoints,
            coding_mode=coding_mode,
        )
        if vault is not None
        else None
    )
    # WhatsApp and email (Section 11.9) need the public address for their links.
    whatsapp = (
        WhatsAppHub(
            engine,
            vault,
            public_url=settings.public_url,
            keys=keys,
            gate=gate,
            skills=library,
            reviews=reviews,
        )
        if vault is not None and settings.public_url
        else None
    )
    email = (
        EmailHub(
            engine,
            vault,
            public_url=settings.public_url,
            gate=gate,
            skills=library,
            reviews=reviews,
        )
        if vault is not None and settings.public_url
        else None
    )
    scheduler = _scheduler(settings, engine, vault, sandboxed)
    if hub is not None:
        handoffs.register("telegram", hub.door)
    if slack is not None:
        handoffs.register("slack", slack.door)
    # A finished sign-in from the chat continues its session, also in Telegram and Slack (M8.1).
    connections.on_connected = ConnectionResumer(queue, connections, handoffs)

    def forget(integration_id: uuid.UUID) -> None:
        for cache in (hub, slack, whatsapp, email):
            if cache is not None:
                cache.forget(integration_id)

    integrations = Integrations(
        engine,
        vault,
        public_url=settings.public_url,
        on_change=forget if vault is not None else None,
    )
    stripe_key = settings.stripe_key()
    if stripe_key is None and settings.stripe_secret_key is not None:
        log.error(
            "billing is off: SAMA_STRIPE_WEBHOOK_SECRET is missing, or the key is a live key"
            " without SAMA_STRIPE_ALLOW_LIVE"
        )
    billing = BillingService(
        engine,
        StripeClient(stripe_key) if stripe_key is not None else None,
        webhook_secret=settings.stripe_webhook_secret.get_secret_value()
        if settings.stripe_webhook_secret is not None
        else None,
        web_origins=settings.web_origins,
    )
    triggers = TriggerService(
        engine,
        queue=RunQueue(engine),
        vault=vault,
        connections=connections,
        public_url=settings.public_url,
        composio_secret=settings.composio_webhook_secret,
        workflows=workflows,
    )
    data = DataControls(
        engine,
        cleanup=ServiceCleanup(
            engine,
            connections=connections,
            triggers=triggers,
            integrations=integrations if integrations.enabled else None,
            billing=billing,
            memories=mem0,
        ),
    )
    api = SamaApi(
        engine,
        queue=queue,
        verifier=JwtVerifier(
            secret=settings.supabase_jwt_secret,
            jwks_url=jwks_url(settings.supabase_url),
            issuer=issuer(settings.supabase_url),
        ),
        clarify=broker,
        settings=ApiSettings(
            supabase_url=settings.supabase_url,
            supabase_anon_key=settings.supabase_anon_key,
            web_origins=settings.web_origins,
            signup=settings.signup,
            extension_ids=settings.extension_ids,
            public_url=settings.public_url,
            extension_zip=settings.extension_zip,
            # The polls of the event streams use at most half of the pool (Section 4.4).
            stream_db_slots=max(1, settings.db_pool.size // 2),
        ),
        chrome=chrome,
        integrations=integrations,
        skills=library,
        reviews=reviews,
        github=github,
        connections=connections,
        jobs=jobs,
        triggers=triggers,
        skill_store=skill_store,
        search=sessions,
        workflows=workflows,
        handoffs=handoffs,
        checkpoints=checkpoints,
        coding_mode=coding_mode,
        model_keys=model_keys,
        # The browser settings of the org (M5.3). A change of the URL policy applies at once to
        # the browser sessions of this process.
        live_browser=browser,
        browser=BrowserSettings(
            PolicyStore(engine),
            BrowserLogins(engine, vault) if vault is not None else None,
            on_policy_change=browser.forget_policy,
        ),
        data=data,
        billing=billing,
        custom_tools=custom_tools,
        # The session sandbox starts before the first run (Section 4.4).
        warmer=warmer,
        signals=signals,
    )
    api_app = api.app()
    tools_app = tools.app()
    # Sama as a server (Section 10.7): the OpenAI-compatible endpoint and the MCP server for
    # other agents. Both take an org API key and use the same queue, gate, and budgets.
    api_keys = ApiKeyStore(engine)
    core = ServerCore(engine, queue=queue, search=sessions, skills=library, workflows=workflows)
    OpenAICompat(core, api_keys).install(api_app)
    agents = SamaMcpServer(core, api_keys)
    agents_app = agents.app()

    if hub is not None:
        api_app.include_router(hub.router())
    if slack is not None:
        api_app.include_router(slack.router())
    if whatsapp is not None:
        api_app.include_router(whatsapp.router())
    if email is not None:
        api_app.include_router(email.router())

    stop = asyncio.Event()

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        # The exit stack closes in the reverse order: the loops, the bots, the tool server,
        # the model proxy (it writes its last usage records), the sandboxes, the engine.
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_callback(engine.dispose)
            if sandboxed is not None:
                stack.push_async_callback(_close_sandboxes, sandboxed)
            if warmer is not None:
                # A warm that still runs stops before the sandboxes pause.
                stack.push_async_callback(warmer.aclose)
            await stack.enter_async_context(proxy_app.router.lifespan_context(proxy_app))
            await stack.enter_async_context(tools_app.router.lifespan_context(tools_app))
            await stack.enter_async_context(agents_app.router.lifespan_context(agents_app))
            stack.push_async_callback(connections.aclose)
            stack.push_async_callback(custom_tools.aclose)
            if model_keys is not None:
                stack.push_async_callback(model_keys.aclose)
            if hub is not None:
                stack.push_async_callback(hub.aclose)
            if upstream is not None and github is not None:
                stack.push_async_callback(github.client.aclose)
                stack.push_async_callback(upstream.aclose)
            if slack is not None:
                stack.push_async_callback(slack.aclose)
            if whatsapp is not None:
                stack.push_async_callback(whatsapp.aclose)
            if email is not None:
                stack.push_async_callback(email.aclose)
            if mem0 is not None:
                stack.push_async_callback(mem0.aclose)
            if memory_sync is not None:
                stack.push_async_callback(memory_sync.wait)
            # The first requests do not wait for a connect (Section 4.4).
            await prewarm(engine, settings.db_pool.prewarm)
            loops = _monitor(settings, engine, stack, sandboxes=e2b, memories=mem0)
            loops.append(PoolMonitor(engine))
            loops.append(RetentionSweep(engine))
            loops.append(SigninPoller(connections))
            loops.append(TaskDispatcher(engine, queue))
            if embedder is not None:
                stack.push_async_callback(embedder.aclose)
                loops.append(EmbedWorker(engine, embedder))
            if vault is not None:
                # The redactor must know every vault value before the first run, request,
                # or trace. A failed refresh stops the start.
                await vault.refresh_redactor()
                loops.append(vault)
            stack.push_async_callback(scheduler.aclose)
            loops.append(scheduler)
            loops.append(workflow_engine)
            stack.push_async_callback(billing.aclose)
            await billing.start()
            loops.append(billing)
            plane.tasks = [
                asyncio.create_task(
                    _named("worker", worker.serve(stop, concurrency=settings.worker_concurrency))
                ),
                *(
                    asyncio.create_task(_named(type(loop).__name__.lower(), loop.serve(stop)))
                    for loop in loops
                ),
            ]
            for task in plane.tasks:
                task.add_done_callback(lambda t: _ended(t, stop, exit_on_failure))
            stack.push_async_callback(plane.drain)
            log.info(
                "the control plane started",
                extra={
                    "worker_concurrency": settings.worker_concurrency,
                    "integrations": integrations.enabled,
                    "traces": settings.langfuse is not None,
                    "alerts": settings.slack_webhook_url is not None,
                    "vault": vault is not None,
                    "github": github is not None,
                },
            )
            yield

    routes = [
        Mount("/tools", app=tools_app),
        Mount("/model", app=proxy_app),
        # The MCP app routes the path itself, so a ``Route`` (not a ``Mount``) keeps ``/mcp``.
        Route(SERVER_MCP_PATH, endpoint=agents.guarded(agents_app)),
    ]
    if github is not None and upstream is not None:
        routes.append(Mount("/git", app=GitProxy(signer, github, upstream).app()))
    app = Starlette(
        routes=[*routes, Mount("", app=api_app)],
        lifespan=lifespan,
        # It names each request for the pool log and counts its database round trips.
        middleware=[Middleware(RequestMonitor)],
    )
    plane = ControlPlane(
        app=app,
        engine=engine,
        worker=worker,
        api=api,
        tools=tools,
        proxy=proxy,
        sandboxed=sandboxed,
        stop=stop,
    )
    return plane


def _browser_service(
    settings: ControlPlaneSettings, engine: AsyncEngine, chrome: ChromeHub, vault: Vault | None
) -> BrowserService:
    """The browser of Section 14.1. The cloud target needs a Browserbase key and project, and
    it is the default target when it is there. The ``my_chrome`` target is always there. The URL
    policy of each org applies to the cloud target, and with the vault the service has the
    saved logins (M5.3)."""
    targets: list[BrowserTarget] = []
    if settings.browserbase_key is not None and settings.browserbase_project_id:
        targets.append(
            BrowserbaseTarget(
                api_key=settings.browserbase_key,
                project_id=settings.browserbase_project_id,
                contexts=PostgresContextStore(engine),
                record=settings.browserbase_record,
                proxies=settings.browserbase_proxies,
                solve_captchas=settings.browserbase_solve_captchas,
            )
        )
    targets.append(ChromeTarget(chrome, AuthUsers(engine).of))
    return BrowserService(
        targets,
        UrlGuard(),
        preference=SessionBrowserChoice(engine),
        policies=PolicyCache(PolicyStore(engine)),
        logins=BrowserLogins(engine, vault) if vault is not None else None,
    )


async def _close_sandboxes(sandboxed: SandboxedRuntime) -> None:
    try:
        async with asyncio.timeout(SANDBOX_CLOSE_S):
            await sandboxed.close_sandboxes()
    except TimeoutError:
        log.warning("the session sandboxes were not all paused before the stop")


async def _named(name: str, work: Awaitable[None]) -> None:
    """Runs a background loop with a name for the pool log lines (Section 4.4)."""
    with named(name):
        await work


def _ended(task: asyncio.Task[None], stop: asyncio.Event, exit_on_failure: bool) -> None:
    if task.cancelled() or stop.is_set():
        return
    error = task.exception()
    log.critical("a background loop of the control plane ended", exc_info=error)
    stop.set()
    if exit_on_failure:
        # uvicorn stops on SIGTERM. The platform starts the process again.
        os.kill(os.getpid(), signal.SIGTERM)


def _scheduler(
    settings: ControlPlaneSettings,
    engine: AsyncEngine,
    vault: Vault | None,
    sandboxed: SandboxedRuntime | None,
) -> Scheduler:
    """The job scheduler of Section 12.1. It is a loop of this process, not a Fly process of
    its own. Without the vault, Telegram and Slack deliveries fail; without the SendGrid key,
    email deliveries fail."""

    async def secret(org_id: uuid.UUID, name: SecretName) -> SecretStr | None:
        return None if vault is None else await vault.get(org_id, name, fresh=True)

    email = (
        EmailConfig(settings.sendgrid_key, settings.email_from)
        if settings.sendgrid_key is not None and settings.email_from
        else None
    )
    queue = RunQueue(engine)
    return Scheduler(
        engine,
        queue,
        Deliverer(engine, Sender(secret, email=email)),
        scripts=sandboxed,
        curator=True,
    )


class _Loop(Protocol):
    async def serve(self, stop: asyncio.Event) -> None: ...


def _escalation(browser: Escalation) -> Escalation:
    """The sensitive browser actions (Section 14.2.2), then a workspace connection that the
    agent asks for (M8.1)."""

    def escalate(org_id: str, run_id: str, name: str, arguments: Mapping[str, Any]) -> str | None:
        return browser(org_id, run_id, name, arguments) or connection_escalation(name, arguments)

    return escalate


def _monitor(
    settings: ControlPlaneSettings,
    engine: AsyncEngine,
    stack: contextlib.AsyncExitStack,
    *,
    sandboxes: SandboxBackend | None = None,
    memories: MemoryDeleter | None = None,
) -> list[_Loop]:
    """The trace export and the error rate alerts of Section 18, when they are set, and the
    purge worker of Section 17.4. The purge worker deletes the Langfuse traces, kills the
    sandboxes, and deletes the Browserbase contexts of deleted data."""
    loops: list[_Loop] = []
    langfuse: LangfuseClient | None = None
    if settings.langfuse is not None:
        langfuse = LangfuseClient(
            settings.langfuse.base_url,
            settings.langfuse.public_key,
            settings.langfuse.secret_key,
            environment=settings.environment,
        )
        stack.push_async_callback(langfuse.aclose)
        loops.append(
            TraceExporter(
                engine,
                langfuse,
                environment=settings.environment,
                include_content=settings.trace_content,
            )
        )
    if settings.slack_webhook_url is not None:
        slack = SlackWebhook(settings.slack_webhook_url)
        stack.push_async_callback(slack.aclose)
        loops.append(ErrorRateAlerter(engine, slack, environment=settings.environment))
    browser: BrowserbaseTarget | None = None
    if settings.browserbase_key is not None and settings.browserbase_project_id:
        # Only for the purge: it deletes the Browserbase contexts of deleted users.
        browser = BrowserbaseTarget(
            api_key=settings.browserbase_key,
            project_id=settings.browserbase_project_id,
            contexts=PostgresContextStore(engine),
        )
        stack.push_async_callback(browser.aclose)
    if langfuse is not None or sandboxes is not None or browser is not None or memories:
        loops.append(
            PurgeWorker(
                engine,
                sandboxes=sandboxes,
                traces=langfuse,
                browser_contexts=browser,
                memories=memories,
            )
        )
    return loops
