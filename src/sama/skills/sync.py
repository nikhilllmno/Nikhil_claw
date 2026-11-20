"""Skills in the sandbox (docs/architecture.md, Section 9.3).

Before a run starts, the control plane writes the files of the skills of the session to
``.sama/skills/`` in the work folder, so that a script of a skill can run. A manifest of the
files (path and SHA-256) keeps the next sync small: it writes the changed files and removes
the files that are not in the library any more.

The tools of the run do not need these files, because ``skill_view`` reads the database. So a
failed sync is only a warning.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Mapping
from typing import Final, Protocol

from sama.runtime.contract import RunScope
from sama.sandbox.base import Sandbox

log = logging.getLogger(__name__)

ROOT: Final = ".sama/skills"
MANIFEST: Final = ".manifest.json"
SYNC_TIMEOUT_S: Final = 30.0
_PARALLEL: Final = 8
_SAFE: Final = re.compile(
    r"[a-z0-9]+(?:-[a-z0-9]+)*/(?:SKILL\.md|(?:references|templates|scripts|assets)/[A-Za-z0-9._/-]+)"
)


class SkillSource(Protocol):
    async def files(self, scope: RunScope) -> Mapping[str, str]:
        """The skill files of the session of ``scope``: path to text."""
        ...


def _safe(path: str) -> bool:
    return _SAFE.fullmatch(path) is not None and ".." not in path.split("/")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _manifest(sandbox: Sandbox, base: str) -> dict[str, str]:
    try:
        data = json.loads((await sandbox.read_file(f"{base}/{MANIFEST}")).decode())
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


async def sync_files(sandbox: Sandbox, files: Mapping[str, str]) -> int:
    """Makes ``.sama/skills/`` match ``files``. Returns the number of changes."""
    base = f"{sandbox.workdir.rstrip('/')}/{ROOT}"
    wanted = {path: _digest(text) for path, text in files.items() if _safe(path)}
    old = await _manifest(sandbox, base)
    changed = [path for path, digest in wanted.items() if old.get(path) != digest]
    stale = [path for path in old if path not in wanted and _safe(path)]
    if not changed and not stale and old == wanted:
        return 0
    gate = asyncio.Semaphore(_PARALLEL)

    async def write(path: str) -> None:
        async with gate:
            await sandbox.write_file(f"{base}/{path}", files[path].encode())

    async def remove(path: str) -> None:
        async with gate:
            await sandbox.remove_file(f"{base}/{path}")

    await asyncio.gather(*(write(path) for path in changed), *(remove(path) for path in stale))
    await sandbox.write_file(f"{base}/{MANIFEST}", json.dumps(wanted, sort_keys=True).encode())
    return len(changed) + len(stale)


async def sync_skills(sandbox: Sandbox, source: SkillSource, scope: RunScope) -> None:
    """Syncs the skills of the session into the sandbox. It never raises, except for a cancel:
    a skill that cannot sync must not stop the run."""
    try:
        async with asyncio.timeout(SYNC_TIMEOUT_S):
            changes = await sync_files(sandbox, await source.files(scope))
    except Exception as error:
        log.warning("skill sync failed run=%s: %s", scope.run_id, error)
        return
    if changes:
        log.info("skill sync run=%s changes=%d", scope.run_id, changes)
