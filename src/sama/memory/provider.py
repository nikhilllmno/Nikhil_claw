"""External memory providers (docs/architecture.md, Section 8.2; roadmap M7.5).

The built-in memory (``memory`` tool, Section 8.1) is always on. A profile can also use an
external provider (``agent_profiles.memory_provider``). Today there is one: ``mem0``.

With ``mem0``:

- after each completed chat run in a private session, ``MemorySync`` sends the user message and
  the answer of the run to Mem0. Mem0 keeps the facts that it finds, for that user only. The
  text goes through the redactor first;
- the agent has the tool ``memory_recall(query)``, which searches the Mem0 memories of the user
  of the run.

The scope is one user of one org: the Mem0 ``user_id`` is ``sama:<org id>:<user id>``. Shared
sessions, delegated children, unattended runs, and runs without a user send nothing. A user or
org delete deletes the Mem0 memories through the purge worker (Section 17.4).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, Message, Run, Session
from sama.gate.decide import DELEGATION_LINEAGE
from sama.vault.crypto import VaultError
from sama.vault.redact import Redactor, default_redactor
from sama.vault.store import OrgKeys, SecretName

log = logging.getLogger("sama.memory.provider")

BUILTIN: Final = "builtin"
MEM0: Final = "mem0"
MEM0_URL: Final = "https://api.mem0.ai"
TIMEOUT_S: Final = 20.0
MAX_TURN_CHARS: Final = 8_000
"""Each text of a turn is cut to this length before it goes to Mem0."""
MAX_RECALL: Final = 10
MAX_QUERY_CHARS: Final = 1_000
MAX_HIT_CHARS: Final = 500
MAX_OPEN_SYNCS: Final = 32
"""More syncs at one time are dropped with a log line: the built-in memory still works."""
NO_KEY: Final = "External memory is not available: no Mem0 key is configured for the org."
NOT_ON: Final = (
    "External memory is off for this agent profile. An admin can set the memory provider of "
    "the profile to mem0."
)


class MemoryProviderError(Exception):
    """The provider failed. The text is safe for the model: no key, no URL, no body."""


@dataclass(frozen=True, slots=True)
class RecallHit:
    text: str
    created_at: str = ""


def mem0_user(org_id: str | uuid.UUID, user_id: str | uuid.UUID) -> str:
    """The Mem0 ``user_id`` of one sama user of one org."""
    return f"sama:{org_id}:{user_id}"


class MemoryProvider(Protocol):
    """An external store of long-term memory for one user."""

    name: str

    async def available(self, org_id: str) -> bool:
        """True if the org has a key for the provider."""
        ...

    async def recall(
        self, org_id: str, user_id: str, query: str, limit: int
    ) -> list[RecallHit]: ...

    async def remember(self, org_id: str, user_id: str, turns: Sequence[tuple[str, str]]) -> None:
        """Stores a conversation: ``(role, text)`` pairs."""
        ...

    async def forget(self, org_id: str, user_ids: Sequence[str]) -> None:
        """Deletes all memories of these users. A user without memories is not an error."""
        ...

    async def aclose(self) -> None: ...


class Mem0Provider:
    """The Mem0 platform API. The key of the org (vault ``mem0_api_key``) comes first, then the
    key of sama (``MEM0_API_KEY``)."""

    name = MEM0

    def __init__(
        self,
        keys: OrgKeys,
        *,
        base_url: str = MEM0_URL,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._keys = keys
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT_S, trust_env=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _key(self, org_id: str) -> str | None:
        try:
            found = await self._keys.get(org_id, SecretName.MEM0_API_KEY)
        except VaultError:
            log.warning("the Mem0 key of org=%s does not decrypt", org_id)
            return None
        return None if found is None else found.get_secret_value()

    async def available(self, org_id: str) -> bool:
        return await self._key(org_id) is not None

    async def _send(
        self, org_id: str, method: str, path: str, **kwargs: Any
    ) -> httpx.Response | None:
        key = await self._key(org_id)
        if key is None:
            return None
        try:
            response = await self._http.request(
                method,
                f"{self._base_url}{path}",
                headers={"authorization": f"Token {key}"},
                **kwargs,
            )
        except httpx.TimeoutException:
            raise MemoryProviderError("Mem0 did not answer in time. Try again.") from None
        except httpx.HTTPError as error:
            log.warning("a Mem0 call failed org=%s error=%s", org_id, type(error).__name__)
            raise MemoryProviderError("Mem0 cannot be reached now. Try again later.") from None
        if response.status_code >= 300:
            log.warning("Mem0 refused a call org=%s status=%d", org_id, response.status_code)
            if response.status_code in (401, 403):
                raise MemoryProviderError("Mem0 refused the key of the org. Ask an admin.")
            if response.status_code == 429:
                raise MemoryProviderError("Mem0 is busy now. Try again later.")
            raise MemoryProviderError("Mem0 is not available now. Try again later.")
        return response

    async def recall(self, org_id: str, user_id: str, query: str, limit: int) -> list[RecallHit]:
        body = {
            "query": query,
            "filters": {"user_id": mem0_user(org_id, user_id)},
            "top_k": limit,
        }
        response = await self._send(org_id, "POST", "/v2/memories/search/", json=body)
        if response is None:
            raise MemoryProviderError(NO_KEY)
        try:
            data = response.json()
        except ValueError:
            raise MemoryProviderError("Mem0 gave an answer that sama cannot read.") from None
        items = data if isinstance(data, list) else data.get("results", [])
        hits: list[RecallHit] = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("memory"), str):
                hits.append(
                    RecallHit(
                        text=item["memory"][:MAX_HIT_CHARS],
                        created_at=str(item.get("created_at") or "")[:10],
                    )
                )
        return hits[:limit]

    async def remember(self, org_id: str, user_id: str, turns: Sequence[tuple[str, str]]) -> None:
        messages = [{"role": role, "content": text} for role, text in turns if text.strip()]
        if not messages:
            return
        body = {
            "messages": messages,
            "user_id": mem0_user(org_id, user_id),
            "version": "v2",
            "async_mode": True,
            "metadata": {"source": "sama"},
        }
        await self._send(org_id, "POST", "/v1/memories/", json=body)

    async def forget(self, org_id: str, user_ids: Sequence[str]) -> None:
        for user_id in user_ids:
            response = await self._send(
                org_id, "DELETE", "/v1/memories/", params={"user_id": mem0_user(org_id, user_id)}
            )
            if response is None:
                # No key: sama cannot have sent memories of the org with this key, and cannot
                # delete them. The delete is done.
                log.warning("no Mem0 key: the memories of org=%s are not deleted", org_id)
                return


@dataclass(frozen=True, slots=True)
class _Turn:
    user_id: uuid.UUID
    texts: tuple[tuple[str, str], ...]


class MemorySync:
    """The run-end hook of the worker: sends the turn of a finished run to the provider of
    its profile. It never blocks or fails the run."""

    def __init__(
        self,
        engine: AsyncEngine,
        provider: MemoryProvider,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self._engine = engine
        self._provider = provider
        self._redactor = redactor or default_redactor()
        self._tasks: set[asyncio.Task[bool]] = set()

    def __call__(self, org_id: str, run_id: str) -> None:
        if len(self._tasks) >= MAX_OPEN_SYNCS:
            log.warning("too many memory syncs: run=%s is not sent", run_id)
            return
        task = asyncio.get_running_loop().create_task(self.sync(org_id, run_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait(self) -> None:
        """Waits for the open syncs (for the tests and the stop)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def sync(self, org_id: str, run_id: str) -> bool:
        """Sends one turn. Returns True when it sent the turn."""
        try:
            turn = await self._turn(uuid.UUID(org_id), uuid.UUID(run_id))
            if turn is None:
                return False
            await self._provider.remember(org_id, str(turn.user_id), turn.texts)
            return True
        except MemoryProviderError as error:
            log.warning("the memory sync of run=%s failed: %s", run_id, error)
        except Exception:
            log.exception("the memory sync of run=%s failed", run_id)
        return False

    async def _turn(self, org_id: uuid.UUID, run_id: uuid.UUID) -> _Turn | None:
        async with self._engine.connect() as connection:
            run = (
                await connection.execute(
                    select(
                        Run.status,
                        Run.kind,
                        Run.unattended,
                        Run.compact,
                        Run.triggered_by_user_id,
                        Session.audience,
                        Session.lineage,
                        Session.owner_user_id,
                        AgentProfile.memory_provider,
                    )
                    .join(Session, and_(Session.org_id == Run.org_id, Session.id == Run.session_id))
                    .join(
                        AgentProfile,
                        and_(
                            AgentProfile.org_id == Run.org_id,
                            AgentProfile.id == Run.agent_profile_id,
                        ),
                    )
                    .where(Run.org_id == org_id, Run.id == run_id)
                )
            ).one_or_none()
            if (
                run is None
                or run.memory_provider != self._provider.name
                or run.status != "completed"
                or run.kind != "chat"
                or run.unattended
                or run.compact
                or run.audience != "private"
                or run.lineage == DELEGATION_LINEAGE
                or run.owner_user_id is None
                or run.triggered_by_user_id not in (None, run.owner_user_id)
            ):
                return None
            rows = (
                await connection.execute(
                    select(Message.role, Message.content)
                    .where(Message.org_id == org_id, Message.run_id == run_id)
                    .order_by(Message.seq)
                )
            ).all()
        texts = tuple(
            (row.role, self._redactor.text(str(row.content.get("text", "")))[:MAX_TURN_CHARS])
            for row in rows
            if row.role in ("user", "assistant") and isinstance(row.content, dict)
        )
        if not any(role == "user" for role, _ in texts):
            return None
        return _Turn(user_id=run.owner_user_id, texts=texts)


def format_hits(query: str, hits: Sequence[RecallHit]) -> str:
    if not hits:
        return f"No remembered facts match: {query}"
    lines = [f"Remembered facts about the user for: {query}"]
    for hit in hits:
        when = f" ({hit.created_at})" if hit.created_at else ""
        lines.append(f"- {' '.join(hit.text.split())}{when}")
    return "\n".join(lines)


class MemoryRecall:
    """``memory_recall``: searches the external memories of the user of the run. Only in a
    private session of a profile whose provider is the provider of this process."""

    def __init__(self, engine: AsyncEngine, provider: MemoryProvider) -> None:
        self._engine = engine
        self._provider = provider

    async def recall(self, org_id: str, run_id: str, query: str, limit: int) -> tuple[bool, str]:
        """``(found, text)``: ``found`` is True when ``text`` has memories of the provider
        (untrusted content), and False for a note of sama."""
        query = query.strip()
        if not query:
            raise MemoryProviderError("The query is empty.")
        if len(query) > MAX_QUERY_CHARS:
            raise MemoryProviderError(f"The query has more than {MAX_QUERY_CHARS} characters.")
        if not 1 <= limit <= MAX_RECALL:
            raise MemoryProviderError(f"limit must be 1 to {MAX_RECALL}.")
        async with self._engine.connect() as connection:
            run = (
                await connection.execute(
                    select(
                        Session.audience,
                        Session.owner_user_id,
                        AgentProfile.memory_provider,
                    )
                    .select_from(Run)
                    .join(Session, and_(Session.org_id == Run.org_id, Session.id == Run.session_id))
                    .join(
                        AgentProfile,
                        and_(
                            AgentProfile.org_id == Run.org_id,
                            AgentProfile.id == Run.agent_profile_id,
                        ),
                    )
                    .where(Run.org_id == uuid.UUID(org_id), Run.id == uuid.UUID(run_id))
                )
            ).one_or_none()
        if run is None:
            raise MemoryProviderError("The run is not known.")
        if run.memory_provider != self._provider.name:
            return False, NOT_ON
        if run.audience != "private" or run.owner_user_id is None:
            return False, "External memory works only in a private session with one user."
        if not await self._provider.available(org_id):
            return False, NO_KEY
        hits = await self._provider.recall(org_id, str(run.owner_user_id), query, limit)
        return bool(hits), format_hits(query, hits)
