"""Skills in the sandbox (docs/architecture.md, Section 9.3).

Before a run starts, the control plane writes the files of the skills of the session to
``.sama/skills/`` in the work folder, so that a script of a skill can run.

Each sync first asks the sandbox, in one command, whether the folder holds exactly the files
of the library: the command compares a digest of the folder with the digest of the library
files. If it does, the sync is done. Each sandbox call is a round trip to the sandbox provider,
so the orchestrator puts the check and the runner start in one command (``checked_start``): a
follow-up run of a session with unchanged skills costs no call for the skills.

Otherwise the sync (``publish``) writes all the files again, as one archive, into a staging
folder with a random name, and then replaces the folder with it in one command, without
following links. The sandbox is not trusted: the agent or a script can change, delete, or link
the files of the folder. So the sync keeps no manifest, and the only thing it reads from the
sandbox is the answer of the check. A link in place of the folder is removed, never followed.
The agent can write anywhere in its own sandbox, so this protects the sync from accident and
from simple tampering, not from the agent itself.

The tools of the run do not need these files, because ``skill_view`` reads the database. So a
failed sync is only a warning.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import re
import secrets
import tarfile
from collections.abc import Mapping, Sequence
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


_CHECK: Final = (
    'set -e; sama="$1"; want="$2"; final="$sama/skills"; '
    'if [ -L "$sama" ]; then echo ".sama is a link" >&2; exit 3; fi; '
    'mkdir -p "$sama"; rm -rf "$sama"/.skills-*; '
    'if [ -d "$final" ] && [ ! -L "$final" ] '
    '&& [ -z "$(cd "$final" && find . ! -type d ! -type f -print -quit)" ]; then '
    'if sums=$(cd "$final" && find . -type f -exec sha256sum -- {} +); then '
    'have=$(if [ -n "$sums" ]; then printf "%s\\n" "$sums"; fi '
    '| LC_ALL=C sort -k 2 | sha256sum | cut -d" " -f1); '
    'if [ "$have" = "$want" ]; then echo same; exit 0; fi; fi; fi; '
    "echo changed"
)
"""Refuses a link in place of ``.sama``. Removes the staging folders and archives of an earlier
sync that did not finish (``rm -rf`` removes a link and does not follow it). Then it says
``same`` if ``skills`` is a real folder that holds only regular files and folders, and the
digest of its files is the wanted digest. Any other state says ``changed``.

``find`` exits with an error if it cannot read a folder or if ``sha256sum`` cannot read a
file, so a file that the check cannot hash never gives ``same``. The sums are sorted by path
(the second field), because the safe paths have no spaces."""

_PUBLISH: Final = (
    'set -e; sama="$1"; stage="$2"; archive="$3"; final="$sama/skills"; '
    'if [ -L "$sama" ]; then echo ".sama is a link" >&2; exit 3; fi; '
    'mkdir "$stage"; '
    'tar -x --no-same-owner --no-same-permissions -f "$archive" -C "$stage"; '
    'rm -f "$archive"; '
    'rm -rf "$final"; '
    'if [ -e "$final" ] || [ -L "$final" ]; then echo "skills was made again" >&2; exit 4; fi; '
    'mv "$stage" "$final"'
)
"""Makes the staging folder with ``mkdir``, which fails if the name exists, and unpacks the
archive into it. Then it removes the old skills folder without following a link, and moves the
staging folder in its place, in one step."""


async def _run(sandbox: Sandbox, script: str, *args: str) -> str:
    result = await sandbox.exec(["sh", "-c", script, "sh", *args], timeout_s=SYNC_TIMEOUT_S)
    if result.exit_code != 0:
        raise OSError(f"the skills folder was not updated: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def digest(files: Mapping[str, str]) -> str:
    """The digest of a skills folder that holds exactly ``files``.

    It is the output of ``find . -type f -exec sha256sum {} + | sort -k 2 | sha256sum`` in the
    folder, so the sandbox can compute the same value with standard tools. The safe paths are
    ASCII, so the sort order of Python and of ``LC_ALL=C sort`` is the same.
    """
    lines = "".join(
        f"{hashlib.sha256(files[path].encode()).hexdigest()}  ./{path}\n" for path in sorted(files)
    )
    return hashlib.sha256(lines.encode()).hexdigest()


def _archive(files: Mapping[str, str]) -> bytes:
    """A tar archive of ``files``: regular files only, with fixed owners and modes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(files):
            data = files[path].encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


CHECK_SAME: Final = "sama-skills: same"
CHECK_CHANGED: Final = "sama-skills: changed"

_START: Final = (
    'check="$1"; sama="$2"; want="$3"; shift 3; t=""; '
    f'if command -v timeout >/dev/null 2>&1; then t="timeout {int(SYNC_TIMEOUT_S)}"; fi; '
    'if [ "$($t sh -c "$check" sh "$sama" "$want" 2>/dev/null)" = same ]; then '
    f'echo "{CHECK_SAME}"; exec "$@"; fi; '
    f'echo "{CHECK_CHANGED}"; head -n 1 >/dev/null'
)
"""Runs ``_CHECK``, and then starts the runner in the same command. If the folder holds the
wanted files, it writes ``CHECK_SAME`` and replaces itself with the runner (``exec``), so the
runner gets the job line. Any other answer, or a failed check, writes ``CHECK_CHANGED``, reads
the job line without running it, and exits. Then the control plane syncs and starts the runner
again. Both answers come before the job is read, so the control plane always knows whether a
runner got the job."""


def wanted_files(files: Mapping[str, str]) -> dict[str, str]:
    """The files that a sync writes: the files with a safe path. Raises ``ValueError`` if they
    are too large to sync."""
    wanted = {path: text for path, text in files.items() if _safe(path)}
    if len(wanted) > MAX_SYNC_FILES or sum(map(len, wanted.values())) > MAX_SYNC_CHARS:
        raise ValueError("the skill files are too large to sync")
    return wanted


def _sama(sandbox: Sandbox) -> str:
    return f"{sandbox.workdir.rstrip('/')}/{ROOT_PARENT}"


def checked_start(sandbox: Sandbox, wanted: Mapping[str, str], argv: Sequence[str]) -> list[str]:
    """The command that checks the skills folder against ``wanted`` and then starts ``argv``
    (``_START``). It saves one sandbox call per run when the skills did not change: a
    follow-up run then costs no call for the skills.

    The first stdout line of the command is ``CHECK_SAME`` or ``CHECK_CHANGED``. After
    ``CHECK_CHANGED``, call ``publish`` and start ``argv`` without the check."""
    return ["sh", "-c", _START, "sh", _CHECK, _sama(sandbox), digest(wanted), *argv]


async def sync_files(sandbox: Sandbox, files: Mapping[str, str]) -> int:
    """Makes ``.sama/skills/`` hold exactly ``files``. Returns the number of files written, or
    0 if the folder already held them.

    The first command checks the folder against the digest of ``files``. This is the only
    thing that the sync reads from the sandbox, and it is one word: ``same`` or ``changed``.
    A false ``same`` only keeps the files that the agent already had.

    If the folder changed, the sync writes one archive with a name that nobody can guess, and
    one command unpacks it into a new staging folder and replaces the old folder with it. So a
    link that a process makes at ``.sama/skills`` during the sync does not redirect a write.
    The sandbox belongs to the agent, which can write anywhere in it: this protects the sync
    from accident and from simple tampering, not from the agent itself.
    """
    wanted = wanted_files(files)
    if await _run(sandbox, _CHECK, _sama(sandbox), digest(wanted)) == "same":
        return 0
    return await publish(sandbox, wanted)


async def publish(sandbox: Sandbox, wanted: Mapping[str, str]) -> int:
    """Writes ``wanted`` (from ``wanted_files``) to ``.sama/skills/`` without a check first,
    and returns the number of files. See ``sync_files``."""
    sama = _sama(sandbox)
    name = f"{sama}/{STAGE_PREFIX}{secrets.token_hex(16)}"
    archive = f"{name}.tar"
    try:
        await sandbox.write_file(archive, _archive(wanted))
        await _run(sandbox, _PUBLISH, sama, name, archive)
    except BaseException:
        with contextlib.suppress(Exception):
            await sandbox.exec(["rm", "-rf", name, archive], timeout_s=SYNC_TIMEOUT_S)
        raise
    return len(wanted)
