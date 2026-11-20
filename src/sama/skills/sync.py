"""Skills in the sandbox (docs/architecture.md, Section 9.3).

Before a run starts, the control plane writes the files of the skills of the session to
``.sama/skills/`` in the work folder, so that a script of a skill can run.

Each sync removes the whole folder first, without following links, and then writes every file
again from the library. The sandbox is not trusted: the agent or a script can change, delete,
or link the files of the folder. So the sync keeps no manifest and reads nothing from the
sandbox. A link in place of the folder is removed, never followed, so no write and no removal
can leave the folder.

The tools of the run do not need these files, because ``skill_view`` reads the database. So a
failed sync is only a warning.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import Final, Protocol

from sama.runtime.contract import RunScope
from sama.sandbox.base import Sandbox

log = logging.getLogger(__name__)

ROOT: Final = ".sama/skills"
SYNC_TIMEOUT_S: Final = 30.0
MAX_SYNC_FILES: Final = 2_000
MAX_SYNC_CHARS: Final = 20_000_000
_PARALLEL: Final = 8
_SAFE: Final = re.compile(
    r"[a-z0-9]+(?:-[a-z0-9]+)*/(?:SKILL\.md|(?:references|templates|scripts|assets)/[A-Za-z0-9._/-]+)"
)


class SkillSource(Protocol):
    async def files(self, scope: RunScope) -> Mapping[str, str]:
        """The skill files of the session of ``scope``: path to text."""
        ...


def _safe(path: str) -> bool:
    return _SAFE.fullmatch(path) is not None and all(
        part not in {"", ".", ".."} for part in path.split("/")
    )


_CLEAR: Final = (
    'set -e; d="$1"; p="$(dirname "$d")"; '
    'if [ -L "$p" ]; then rm -f "$p"; fi; '
    'mkdir -p "$p"; rm -rf "$d"'
)
"""Removes the skills folder. ``rm -rf`` removes a link and does not follow it. A link in
place of the parent folder ``.sama`` is removed first, so ``rm`` cannot reach its target."""


async def sync_files(sandbox: Sandbox, files: Mapping[str, str]) -> int:
    """Makes ``.sama/skills/`` hold exactly ``files``. Returns the number of files written."""
    base = f"{sandbox.workdir.rstrip('/')}/{ROOT}"
    wanted = {path: text for path, text in files.items() if _safe(path)}
    if len(wanted) > MAX_SYNC_FILES or sum(map(len, wanted.values())) > MAX_SYNC_CHARS:
        raise ValueError("the skill files are too large to sync")
    cleared = await sandbox.exec(["sh", "-c", _CLEAR, "sh", base], timeout_s=SYNC_TIMEOUT_S)
    if cleared.exit_code != 0:
        raise OSError(f"the skills folder was not cleared: {cleared.stderr.strip()[:200]}")
    gate = asyncio.Semaphore(_PARALLEL)

    async def write(path: str) -> None:
        async with gate:
            await sandbox.write_file(f"{base}/{path}", wanted[path].encode())

    await asyncio.gather(*(write(path) for path in wanted))
    return len(wanted)


async def sync_skills(sandbox: Sandbox, source: SkillSource, scope: RunScope) -> None:
    """Syncs the skills of the session into the sandbox. It never raises, except for a cancel:
    a skill that cannot sync must not stop the run."""
    try:
        async with asyncio.timeout(SYNC_TIMEOUT_S):
            written = await sync_files(sandbox, await source.files(scope))
    except Exception as error:
        log.warning("skill sync failed run=%s: %s", scope.run_id, error)
        return
    log.info("skill sync run=%s files=%d", scope.run_id, written)
