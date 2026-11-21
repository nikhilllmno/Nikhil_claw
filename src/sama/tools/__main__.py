"""Starts the sama MCP tool server: ``python -m sama.tools`` (docs/architecture.md,
Section 16.7).

Environment:

- ``SAMA_RUN_TOKEN_KEY``: the run token key of the control plane, at least 32 bytes.
- ``BRAVE_API_KEY``: optional. Without it, ``web_search`` is hidden. The vault replaces this
  variable in M1.17.
- ``OPENAI_API_KEY``: optional. Without it and without a vault key of the org,
  ``session_search`` uses the full-text index alone.
- ``ANTHROPIC_API_KEY``: optional. It is the default key of ``vision_analyze``; the key of the
  org in the vault comes first. Without either, the tool tells the model that image analysis is
  off. ``SAMA_VISION_MODEL`` sets the vision model (default ``claude-sonnet-5-5``).
  ``vision_analyze`` needs ``--store postgres``: the images are in the database.
- ``SAMA_DATABASE_URL``: used only with ``--store postgres``. Then the server also has the
  tool gate (``POST /gate``, M1.9). Without it, the runner denies every tool call.
- ``SAMA_VAULT_KEY``: optional, used only with ``--store postgres``. It opens the vault, so that
  the redactor knows the vault values. Memory and skill text with a secret is refused
  (Section 8.1). The server does not start when the first load of the values fails.

This process has no sandbox provider and no API, so large outputs keep only their head and
tail, and ``clarify`` is hidden. The control plane process (M1.12) adds both.
"""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette

from sama.media.store import AttachmentStore
from sama.media.vision import DEFAULT_VISION_MODEL, VisionService
from sama.memory import InProcessMemoryStore, MemoryService, MemoryStore, PostgresMemoryStore
from sama.modelproxy.prices import PriceTable
from sama.modelproxy.store import PostgresUsageStore
from sama.observability.logs import configure_logging
from sama.orchestrator.queue import RunQueue
from sama.review.change import ChangeProposer
from sama.review.proposals import PostgresProposalStore
from sama.search import OpenAIEmbedder, SessionSearch
from sama.skills import BuiltinSkills, PostgresSkillStore, SkillStore
from sama.tenancy.run_token import RunTokenSigner
from sama.tools.server import ToolServer, quiet_dependency_logs
from sama.tools.todo import MemoryTodoStore, PostgresTodoStore, TodoStore
from sama.tools.web import BraveSearch
from sama.vault.redact import default_redactor, env_secrets
from sama.vault.standalone import keep_redactor_current, vault_from_env
from sama.vault.store import OrgKeys, SecretName, Vault

log = logging.getLogger("sama.tools")


def _embedder(vault: Vault | None) -> OpenAIEmbedder | None:
    """The embedder for the meaning part of session search. The key of the org in the vault
    comes first, then ``OPENAI_API_KEY``. Without either, the search uses the full-text index
    alone. The embedder redacts all text with the redactor of the process, which has the
    vault values (``keep_redactor_current``) and the variables of ``main``."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    keys = OrgKeys(vault, {SecretName.OPENAI_API_KEY: SecretStr(key) if key else None})
    if not keys.available(SecretName.OPENAI_API_KEY):
        log.warning("there is no OpenAI key, so session search has no meaning search")
        return None
    return OpenAIEmbedder(keys, redactor=default_redactor())


def _vision(engine: AsyncEngine, vault: Vault | None) -> VisionService | None:
    """The backend of ``vision_analyze``. The key of the org in the vault comes first, then
    ``ANTHROPIC_API_KEY``. With neither source, an org has no key, and the tool answers that
    image analysis is off."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    keys = OrgKeys(vault, {SecretName.ANTHROPIC_API_KEY: SecretStr(key) if key else None})
    if not keys.available(SecretName.ANTHROPIC_API_KEY):
        log.warning("there is no Anthropic key, so vision_analyze is hidden")
        return None
    return VisionService(
        store=AttachmentStore(engine),
        keys=keys,
        usage=PostgresUsageStore(engine),
        prices=PriceTable.load(),
        model=os.environ.get("SAMA_VISION_MODEL", "").strip() or DEFAULT_VISION_MODEL,
        redactor=default_redactor(),
    )


def build_app(store_kind: str) -> Starlette:
    key = os.environ.get("SAMA_RUN_TOKEN_KEY", "")
    if not key:
        raise SystemExit("set SAMA_RUN_TOKEN_KEY")
    todos: TodoStore
    memories: MemoryStore
    skills: SkillStore | None = None
    gate = None
    vault = None
    sessions: SessionSearch | None = None
    vision: VisionService | None = None
    proposals: PostgresProposalStore | None = None
    changes: ChangeProposer | None = None
    steers: RunQueue | None = None
    if store_kind == "postgres":
        from sama.db.engine import create_engine
        from sama.gate.service import GateService

        url = os.environ.get("SAMA_DATABASE_URL", "")
        if not url:
            raise SystemExit("set SAMA_DATABASE_URL for --store postgres")
        engine = create_engine(url)
        todos = PostgresTodoStore(engine)
        memories = PostgresMemoryStore(engine)
        proposals = PostgresProposalStore(engine)
        changes = ChangeProposer(engine, proposals)
        skills = PostgresSkillStore(engine, BuiltinSkills().all(), proposals)
        gate = GateService(engine)
        steers = RunQueue(engine)
        vault = vault_from_env(engine)
        sessions = SessionSearch(engine, _embedder(vault))
        vision = _vision(engine, vault)
    else:
        log.warning(
            "the task lists are in memory only, there is no tool gate, and the skills tools are off"
        )
        log.warning("session search needs a database, so the session tools are hidden")
        log.warning("images are in the database, so vision_analyze is hidden")
        todos = MemoryTodoStore()
        memories = InProcessMemoryStore()
    brave_key = os.environ.get("BRAVE_API_KEY", "")
    search = None
    if brave_key:
        search = BraveSearch(SecretStr(brave_key))
    else:
        log.warning("BRAVE_API_KEY is not set, so web_search is hidden")
    server = ToolServer(
        signer=RunTokenSigner(key.encode()),
        todos=todos,
        search=search,
        memory=MemoryService(memories, proposals),
        skills=skills,
        sessions=sessions,
        vision=vision,
        gate=gate,
        changes=changes,
        steers=steers,
    )
    return server.app(around=lambda: keep_redactor_current(vault))


def main() -> None:
    parser = argparse.ArgumentParser(description="The sama MCP tool server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--store", choices=("memory", "postgres"), default="memory")
    args = parser.parse_args()
    configure_logging("tools")
    default_redactor().add(
        *env_secrets(
            "SAMA_RUN_TOKEN_KEY",
            "SAMA_DATABASE_URL",
            "BRAVE_API_KEY",
            "SAMA_VAULT_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        )
    )
    quiet_dependency_logs()
    uvicorn.run(build_app(args.store), host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
