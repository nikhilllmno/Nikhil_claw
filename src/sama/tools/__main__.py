"""Starts the sama MCP tool server: ``python -m sama.tools`` (docs/architecture.md,
Section 16.7).

Environment:

- ``SAMA_RUN_TOKEN_KEY``: the run token key of the control plane, at least 32 bytes.
- ``BRAVE_API_KEY``: optional. Without it, ``web_search`` is hidden. The vault replaces this
  variable in M1.17.
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
from starlette.applications import Starlette

from sama.memory import InProcessMemoryStore, MemoryService, MemoryStore, PostgresMemoryStore
from sama.observability.logs import configure_logging
from sama.skills import BuiltinSkills, PostgresSkillStore, SkillStore
from sama.tenancy.run_token import RunTokenSigner
from sama.tools.server import ToolServer, quiet_dependency_logs
from sama.tools.todo import MemoryTodoStore, PostgresTodoStore, TodoStore
from sama.tools.web import BraveSearch
from sama.vault.redact import default_redactor, env_secrets
from sama.vault.standalone import keep_redactor_current, vault_from_env

log = logging.getLogger("sama.tools")


def build_app(store_kind: str) -> Starlette:
    key = os.environ.get("SAMA_RUN_TOKEN_KEY", "")
    if not key:
        raise SystemExit("set SAMA_RUN_TOKEN_KEY")
    todos: TodoStore
    memories: MemoryStore
    skills: SkillStore | None = None
    gate = None
    vault = None
    if store_kind == "postgres":
        from sama.db.engine import create_engine
        from sama.gate.service import GateService

        url = os.environ.get("SAMA_DATABASE_URL", "")
        if not url:
            raise SystemExit("set SAMA_DATABASE_URL for --store postgres")
        engine = create_engine(url)
        todos = PostgresTodoStore(engine)
        memories = PostgresMemoryStore(engine)
        skills = PostgresSkillStore(engine, BuiltinSkills().all())
        gate = GateService(engine)
        vault = vault_from_env(engine)
    else:
        log.warning(
            "the task lists are in memory only, there is no tool gate, and the skills tools are off"
        )
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
        memory=MemoryService(memories),
        skills=skills,
        gate=gate,
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
        *env_secrets("SAMA_RUN_TOKEN_KEY", "SAMA_DATABASE_URL", "BRAVE_API_KEY", "SAMA_VAULT_KEY")
    )
    quiet_dependency_logs()
    uvicorn.run(build_app(args.store), host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
