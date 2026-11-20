"""Skills in the sandbox (docs/architecture.md, Section 9.3).

Before a run starts, the control plane writes the files of the skills of the session to
``.sama/skills/`` in the work folder, so that a script of a skill can run.

Each sync writes every file again from the library into a staging folder with a random name,
and then replaces the folder with it in one command, without following links. The sandbox is
not trusted: the agent or a script can change, delete, or link the files of the folder. So the
sync keeps no manifest and reads nothing from the sandbox. A link in place of the folder is
removed, never followed. The agent can write anywhere in its own sandbox, so this protects the
sync from accident and from simple tampering, not from the agent itself.

The tools of the run do not need these files, because ``skill_view`` reads the database. So a
failed sync is only a warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
from collections.abc import Mapping
from typing import Final, Protocol

from sama.runtime.contract import RunScope
from sama.sandbox.base import Sandbox

log = logging.getLogger(__name__)

ROOT_PARENT: Final = ".sama"
ROOT: Final = f"{ROOT_PARENT}/skills"
STAGE_PREFIX: Final = ".skills-"
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


_PREPARE: Final = (
    'set -e; sama="$1"; stage="$2"; '
    'if [ -L "$sama" ]; then echo ".sama is a link" >&2; exit 3; fi; '
    'mkdir -p "$sama"; rm -rf "$sama"/.skills-*; mkdir "$stage"'
)
"""Refuses a link in place of ``.sama``. Removes the staging folders of an earlier sync that
did not finish (``rm -rf`` removes a link and does not follow it). Makes the new staging
folder with ``mkdir``, which fails if the name exists."""

_PUBLISH: Final = (
    'set -e; sama="$1"; stage="$2"; final="$sama/skills"; '
    'if [ -L "$sama" ]; then echo ".sama is a link" >&2; exit 3; fi; '
    'rm -rf "$final"; '
    'if [ -e "$final" ] || [ -L "$final" ]; then echo "skills was made again" >&2; exit 4; fi; '
    'mv "$stage" "$final"'
)
"""Removes the old skills folder without following a link, and moves the staging folder in its
place, in one step."""


async def _run(sandbox: Sandbox, script: str, *args: str) -> None:
    result = await sandbox.exec(["sh", "-c", script, "sh", *args], timeout_s=SYNC_TIMEOUT_S)
    if result.exit_code != 0:
        raise OSError(f"the skills folder was not updated: {result.stderr.strip()[:200]}")


async def sync_files(sandbox: Sandbox, files: Mapping[str, str]) -> int:
    """Makes ``.sama/skills/`` hold exactly ``files``. Returns the number of files written.

    The files go into a staging folder with a name that nobody can guess. One command then
    replaces the old folder with it. So a link that a process makes at ``.sama/skills`` while
    the files are written does not redirect a write. The sandbox belongs to the agent, which
    can write anywhere in it: this protects the sync from accident and from simple tampering,
    not from the agent itself.
    """
    sama = f"{sandbox.workdir.rstrip('/')}/{ROOT_PARENT}"
    stage = f"{sama}/{STAGE_PREFIX}{secrets.token_hex(16)}"
    wanted = {path: text for path, text in files.items() if _safe(path)}
    if len(wanted) > MAX_SYNC_FILES or sum(map(len, wanted.values())) > MAX_SYNC_CHARS:
        raise ValueError("the skill files are too large to sync")
    await _run(sandbox, _PREPARE, sama, stage)
    gate = asyncio.Semaphore(_PARALLEL)

    async def write(path: str) -> None:
        async with gate:
            await sandbox.write_file(f"{stage}/{path}", wanted[path].encode())

    try:
        await asyncio.gather(*(write(path) for path in wanted))
        await _run(sandbox, _PUBLISH, sama, stage)
    except BaseException:
        with contextlib.suppress(Exception):
            await sandbox.exec(["rm", "-rf", stage], timeout_s=SYNC_TIMEOUT_S)
        raise
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
