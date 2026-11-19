"""The control plane process (docs/architecture.md, Section 19.1).

One process holds the HTTP API, the sama MCP tool server, the model proxy, the run worker, the
monitor, the vault (when ``SAMA_VAULT_KEY`` is set), and the Telegram gateway when a bot token
is set. They share one database engine.
The clarify broker is in memory, so the API that answers a question and the tool server that
asks it must be in the same process.

Paths:

- ``/``: the API and the web page (Section 11.4), and ``/telegram/webhook``.
- ``/tools``: the tool server. The sandbox runner uses ``/tools/mcp`` and ``/tools/gate``.
- ``/model``: the model proxy. The runner uses ``/model/anthropic`` and ``/model/openai``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.routing import Mount

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.api.auth import issuer, jwks_url
from sama.controlplane.settings import ControlPlaneSettings
from sama.db.engine import create_engine
from sama.gate.service import GateService
from sama.gate.wire import GATE_PATH
from sama.gateway.telegram import TelegramGateway, TelegramSettings
from sama.gateway.telegram_client import TelegramClient, bot_id
from sama.gateway.transcribe import OpenAITranscriber
from sama.modelproxy.app import ModelProxy, ProxySettings
from sama.modelproxy.prices import PriceTable
from sama.modelproxy.store import PostgresUsageStore
from sama.observability.alerts import ErrorRateAlerter, SlackWebhook
from sama.observability.langfuse import LangfuseClient
from sama.observability.traces import TraceExporter
from sama.orchestrator import PostgresSandboxDirectory, RunQueue, Worker
from sama.orchestrator.sandboxed import SandboxedRuntime, SandboxSettings
from sama.prompt import PromptAssembler
from sama.runtime.contract import AgentRuntime, McpServerConfig, RuntimeKind, RuntimeRegistry
from sama.sandbox.base import SandboxProvider
from sama.tenancy.run_token import RunTokenSigner
from sama.tools.clarify import ClarifyBroker
from sama.tools.output import SandboxSpill
from sama.tools.repos import RepoCloner
from sama.tools.server import MCP_PATH, ToolServer
from sama.tools.todo import PostgresTodoStore
from sama.tools.web import BraveSearch
from sama.vault.redact import default_redactor
from sama.vault.store import OrgKeys, SecretName, Vault

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
    """Builds the process. ``runtimes`` replaces the sandboxed Claude runtime, for tests.
    ``exit_on_failure`` stops the process when a background loop ends with an error, so
    that the platform starts it again."""
    default_redactor().add(*settings.secret_values())
    engine = engine or create_engine(settings.database_url.get_secret_value())
    vault = Vault(engine, settings.vault_key) if settings.vault_key is not None else None
    keys = OrgKeys(
        vault,
        {
            SecretName.ANTHROPIC_API_KEY: settings.anthropic_key,
            SecretName.OPENAI_API_KEY: settings.openai_key,
            SecretName.BRAVE_API_KEY: settings.brave_key,
        },
    )
    signer = RunTokenSigner(settings.run_token_key.get_secret_value().encode())
    gate = GateService(engine)
    broker = ClarifyBroker()

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
                allow_out=settings.allow_out,
            ),
            directory=PostgresSandboxDirectory(engine),
        )
        runtimes = (sandboxed,)
    for runtime in runtimes:
        registry.register(runtime)

    worker = Worker(
        RunQueue(engine),
        registry,
        config=PromptAssembler(
            engine,
            mcp_servers=(
                McpServerConfig(name=MCP_SERVER_NAME, url=f"{settings.tools_url}{MCP_PATH}"),
            ),
        ),
        gate=gate,
        poll_s=worker_poll_s,
    )
    tools = ToolServer(
        signer=signer,
        todos=PostgresTodoStore(engine),
        search=(
            BraveSearch(lambda org_id: keys.get(org_id, SecretName.BRAVE_API_KEY))
            if keys.available(SecretName.BRAVE_API_KEY)
            else None
        ),
        clarify=broker,
        spill=SandboxSpill(sandboxed.session_sandbox) if sandboxed else None,
        repos=RepoCloner(sandboxed.session_sandbox) if sandboxed else None,
        gate=gate,
    )
    proxy = ModelProxy(
        signer=signer,
        store=PostgresUsageStore(engine),
        prices=PriceTable.load(),
        settings=ProxySettings(
            anthropic_key=settings.anthropic_key, openai_key=settings.openai_key
        ),
        keys=keys,
    )
    api = SamaApi(
        engine,
        verifier=JwtVerifier(
            secret=settings.supabase_jwt_secret,
            jwks_url=jwks_url(settings.supabase_url),
            issuer=issuer(settings.supabase_url),
        ),
        clarify=broker,
        settings=ApiSettings(
            supabase_url=settings.supabase_url,
            supabase_anon_key=settings.supabase_anon_key,
            telegram_bot_username=settings.telegram.bot_username if settings.telegram else None,
        ),
    )
    api_app = api.app()
    tools_app = tools.app()
    proxy_app = proxy.app()

    telegram = _telegram(settings, engine)
    if telegram is not None:
        api_app.include_router(telegram.gateway.router())

    stop = asyncio.Event()

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        # The exit stack closes in the reverse order: the loops, Telegram, the tool server,
        # the model proxy (it writes its last usage records), the sandboxes, the engine.
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_callback(engine.dispose)
            if sandboxed is not None:
                stack.push_async_callback(_close_sandboxes, sandboxed)
            await stack.enter_async_context(proxy_app.router.lifespan_context(proxy_app))
            await stack.enter_async_context(tools_app.router.lifespan_context(tools_app))
            if telegram is not None:
                stack.push_async_callback(telegram.aclose)
                await telegram.client.set_webhook(
                    f"{settings.public_url}/telegram/webhook", telegram.webhook_secret
                )
            loops = _monitor(settings, engine, stack)
            if vault is not None:
                # The redactor must know every vault value before the first run, request,
                # or trace. A failed refresh stops the start.
                await vault.refresh_redactor()
                loops.append(vault)
            plane.tasks = [
                asyncio.create_task(worker.serve(stop, concurrency=settings.worker_concurrency)),
                *(asyncio.create_task(loop.serve(stop)) for loop in loops),
            ]
            for task in plane.tasks:
                task.add_done_callback(lambda t: _ended(t, stop, exit_on_failure))
            stack.push_async_callback(plane.drain)
            log.info(
                "the control plane started",
                extra={
                    "worker_concurrency": settings.worker_concurrency,
                    "telegram": telegram is not None,
                    "traces": settings.langfuse is not None,
                    "alerts": settings.slack_webhook_url is not None,
                    "vault": vault is not None,
                },
            )
            yield

    app = Starlette(
        routes=[
            Mount("/tools", app=tools_app),
            Mount("/model", app=proxy_app),
            Mount("", app=api_app),
        ],
        lifespan=lifespan,
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


async def _close_sandboxes(sandboxed: SandboxedRuntime) -> None:
    try:
        async with asyncio.timeout(SANDBOX_CLOSE_S):
            await sandboxed.close_sandboxes()
    except TimeoutError:
        log.warning("the session sandboxes were not all paused before the stop")


def _ended(task: asyncio.Task[None], stop: asyncio.Event, exit_on_failure: bool) -> None:
    if task.cancelled() or stop.is_set():
        return
    error = task.exception()
    log.critical("a background loop of the control plane ended", exc_info=error)
    stop.set()
    if exit_on_failure:
        # uvicorn stops on SIGTERM. The platform starts the process again.
        os.kill(os.getpid(), signal.SIGTERM)


class _Loop(Protocol):
    async def serve(self, stop: asyncio.Event) -> None: ...


def _monitor(
    settings: ControlPlaneSettings, engine: AsyncEngine, stack: contextlib.AsyncExitStack
) -> list[_Loop]:
    """The trace export and the error rate alerts of Section 18, when they are set."""
    loops: list[_Loop] = []
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
    return loops


@dataclass
class _Telegram:
    gateway: TelegramGateway
    client: TelegramClient
    webhook_secret: SecretStr
    transcriber: OpenAITranscriber | None

    async def aclose(self) -> None:
        await self.gateway.aclose()
        await self.client.aclose()
        if self.transcriber is not None:
            await self.transcriber.aclose()


def _telegram(settings: ControlPlaneSettings, engine: AsyncEngine) -> _Telegram | None:
    config = settings.telegram
    if config is None:
        return None
    if not config.bot_username:
        raise ValueError("the Telegram bot username is not known; set SAMA_TELEGRAM_BOT_USERNAME")
    client = TelegramClient(config.bot_token)
    transcriber = OpenAITranscriber(settings.openai_key) if settings.openai_key else None
    gateway = TelegramGateway(
        engine,
        client,
        settings=TelegramSettings(bot_username=config.bot_username),
        webhook_secret=config.webhook_secret,
        bot_user_id=bot_id(config.bot_token),
        transcriber=transcriber,
    )
    return _Telegram(gateway, client, config.webhook_secret, transcriber)
