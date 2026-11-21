"""Checkpoints: shadow git snapshots of the work folder (docs/architecture.md, Section 15.3).

Before a tool call that can change files runs, the runner takes a snapshot of the work folder.
The snapshot goes to a shadow git repository in ``<workdir>/.sama/checkpoints``. The shadow
repository is not the repository of the agent: it has its own git folder, and the git folders
in the work folder stay as they are. ``/undo`` and ``/rollback`` of the channels restore a
snapshot. They run this module as a command in the session sandbox:

    python -m sama.runtime.checkpoints list [--limit N]
    python -m sama.runtime.checkpoints undo
    python -m sama.runtime.checkpoints restore --number N

Each command writes one JSON object to stdout. The module uses only the standard library and
the ``git`` program of the sandbox image, so the runner can import it at a low cost.

Rules:

- A snapshot holds the files of the work folder. It does not hold the folders in
  ``EXCLUDED_DIRS`` (git folders, ``.sama``, dependency and cache folders), files larger than
  ``MAX_FILE_BYTES``, or anything when the work folder has more than ``MAX_FILES`` files.
- A snapshot does not follow symbolic links. It keeps a link as a link.
- One run can make many snapshots. A snapshot that has the same files as the last one of the
  same run is not kept. The first snapshot of each run is always kept.
- A snapshot that fails only logs. It never stops the tool call.
- A restore waits for the runner lock: it does not change files while a runner works. First it
  takes a snapshot of the current files (kind ``rollback``), so a restore can be undone with
  ``/rollback``. Then it deletes the files that are not in the target snapshot and writes the
  files of the target snapshot. Excluded and large files stay as they are: the restore does not
  replace a large file, or a folder that holds excluded or large files, with a file of the
  target. It counts these items as ``kept``.
- Git stores the bytes of each file as they are. The shadow repository turns off the git
  attributes (``.gitattributes``) that change files, such as line ends and filters.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final, Literal

from sama.runtime.contract import RunScope
from sama.runtime.gating import GateDecision, ToolGate
from sama.sandbox.base import WORKDIR

__all__ = [
    "CHANGE_TOOLS",
    "Checkpoint",
    "CheckpointError",
    "CheckpointGate",
    "Checkpoints",
    "Restored",
]

log: Final = logging.getLogger("sama.runtime.checkpoints")

SHADOW_DIR: Final = ".sama/checkpoints"
"""The git folder of the shadow repository, in the work folder."""
CHECKPOINT_LOCK: Final = ".sama/checkpoints.lock"
RUNNER_LOCK: Final = ".sama/runner.lock"
"""The lock that a runner holds while it works (``sama.runtime.runner.LOCK_FILE``)."""
REF: Final = "refs/sama/checkpoints"

EXCLUDED_DIRS: Final = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".sama",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)
"""Folders that a snapshot does not hold, at any depth."""
MAX_FILE_BYTES: Final = 10 * 1024 * 1024
MAX_FILES: Final = 50_000
"""A work folder with more files gets no snapshot: the snapshot would take too long."""
LIST_LIMIT: Final = 10
MAX_HISTORY: Final = 1_000
"""How many checkpoints ``undo`` and ``restore`` look at."""
RUNNER_LOCK_WAIT_S: Final = 10.0
GIT_TIMEOUT_S: Final = 60.0
GIT: Final = shutil.which("git") or "/usr/bin/git"
ATTRIBUTES: Final = "* -text -filter -ident -working-tree-encoding\n"
"""``info/attributes`` of the shadow repository. It has the highest priority of all attribute
files, so the ``.gitattributes`` files of the work folder cannot change the stored bytes."""

CHANGE_TOOLS: Final = frozenset(
    {
        "Bash",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "mcp__sama__repo_clone",
        "mcp__sama__execute_code",
    }
)
"""The tools that can change files in the work folder. A snapshot comes before each call."""

Kind = Literal["tool", "rollback"]


class CheckpointError(Exception):
    """A checkpoint command failed. The message is for the user."""


class BusyError(CheckpointError):
    """A runner holds the runner lock."""


@dataclass(frozen=True, slots=True)
class Checkpoint:
    number: int
    """1 is the newest checkpoint."""
    commit: str
    tree: str
    created_at: int
    """Unix time in seconds."""
    run_id: str
    tool: str
    kind: str
    restored: str = ""
    """For a ``rollback`` checkpoint: the checkpoint that the restore wrote after it."""


@dataclass(frozen=True, slots=True)
class Restored:
    commit: str
    """The checkpoint that the files are now equal to."""
    created_at: int
    written: int
    deleted: int
    saved: str
    """The ``rollback`` checkpoint: the files before the restore."""
    kept: int = 0
    """Files of the target that the restore did not write: writing them would remove a large
    file or excluded files, which the ``rollback`` checkpoint does not hold."""


@dataclass(frozen=True, slots=True)
class _Files:
    paths: list[str]
    skipped: set[str]
    """Files that are too large. A snapshot does not hold them."""


class Checkpoints:
    """The shadow repository of one work folder. The methods block: call them in a thread."""

    def __init__(self, workdir: str) -> None:
        self.workdir = os.path.abspath(workdir)
        self.git_dir = os.path.join(self.workdir, SHADOW_DIR)

    # -- git --------------------------------------------------------------------------------

    def _env(self, index: str | None = None) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/"),
            "GIT_DIR": self.git_dir,
            "GIT_WORK_TREE": self.workdir,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "sama",
            "GIT_AUTHOR_EMAIL": "sama@localhost",
            "GIT_COMMITTER_NAME": "sama",
            "GIT_COMMITTER_EMAIL": "sama@localhost",
            "LC_ALL": "C",
        }
        if index is not None:
            env["GIT_INDEX_FILE"] = index
        return env

    def _git(
        self, *args: str, data: bytes | None = None, index: str | None = None, check: bool = True
    ) -> bytes:
        done = subprocess.run(  # noqa: S603 -- fixed git arguments, no shell
            [
                GIT,
                *("-c", "core.autocrlf=false", "-c", "core.safecrlf=false"),
                *("-c", f"core.attributesFile={os.devnull}"),
                *args,
            ],
            input=data,
            capture_output=True,
            env=self._env(index),
            cwd=self.workdir,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
        if check and done.returncode != 0:
            message = done.stderr.decode(errors="replace").strip()[-500:]
            raise CheckpointError(f"git {args[0]} failed: {message}")
        return done.stdout

    def _exists(self) -> bool:
        return os.path.isfile(os.path.join(self.git_dir, "HEAD"))

    def _ensure(self) -> None:
        if not self._exists():
            os.makedirs(self.git_dir, exist_ok=True)
            self._git("init", "-q")
        path = os.path.join(self.git_dir, "info", "attributes")
        try:
            with open(path, encoding="utf-8") as file:
                if file.read() == ATTRIBUTES:
                    return
        except OSError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(ATTRIBUTES)

    @contextlib.contextmanager
    def _lock(self, name: str, *, wait_s: float | None = None) -> Iterator[None]:
        """An exclusive ``flock`` on a file in the work folder. ``wait_s`` None waits without
        a limit; else ``BusyError`` comes after ``wait_s``."""
        path = os.path.join(self.workdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            if wait_s is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + wait_s
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise BusyError(
                                "A run works in this session now. Try again when it ends."
                            ) from None
                        time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    # -- files ------------------------------------------------------------------------------

    def _walk(self) -> _Files | None:
        """The files of the work folder, relative to it. None when there are too many."""
        paths: list[str] = []
        skipped: set[str] = set()
        for root, dirs, files in os.walk(self.workdir, followlinks=False):
            rel_root = os.path.relpath(root, self.workdir)
            keep: list[str] = []
            for name in dirs:
                full = os.path.join(root, name)
                if name in EXCLUDED_DIRS:
                    continue
                if os.path.islink(full):
                    # os.walk does not enter a link to a folder. Keep the link as a file.
                    files.append(name)
                    continue
                keep.append(name)
            dirs[:] = keep
            for name in files:
                if name == ".git":  # the git file of a worktree or a submodule
                    continue
                rel = name if rel_root == "." else os.path.join(rel_root, name)
                try:
                    st = os.lstat(os.path.join(root, name))
                except OSError:
                    continue
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_size > MAX_FILE_BYTES:
                    skipped.add(rel)
                    continue
                paths.append(rel)
                if len(paths) > MAX_FILES:
                    return None
        return _Files(paths, skipped)

    def _tree(self, files: _Files) -> str:
        """Writes the files to the index of the shadow repository and returns the tree id.
        The index is kept between snapshots: git hashes only the files that changed."""
        old = set(_split(self._git("ls-files", "-z")))
        gone = old - set(files.paths)
        if gone:
            self._git("update-index", "--force-remove", "-z", "--stdin", data=_join(sorted(gone)))
        if files.paths:
            self._git("update-index", "--add", "--remove", "-z", "--stdin", data=_join(files.paths))
        return self._git("write-tree").decode().strip()

    def _head(self) -> Checkpoint | None:
        found = self._history(1)
        return found[0] if found else None

    def _history(self, limit: int) -> list[Checkpoint]:
        if not self._git("rev-parse", "--verify", "-q", REF, check=False).strip():
            return []
        out = self._git("log", REF, f"-n{limit}", "--format=%H%x1f%T%x1f%ct%x1f%B%x1e")
        found: list[Checkpoint] = []
        for item in out.decode(errors="replace").split("\x1e"):
            record = item.strip("\n")
            if not record:
                continue
            commit, tree, created, body = record.split("\x1f", 3)
            meta = _meta(body)
            found.append(
                Checkpoint(
                    number=len(found) + 1,
                    commit=commit,
                    tree=tree,
                    created_at=int(created),
                    run_id=meta.get("run", ""),
                    tool=meta.get("tool", ""),
                    kind=meta.get("kind", "tool"),
                    restored=meta.get("restored", ""),
                )
            )
        return found

    def _snapshot(self, run_id: str, tool: str, kind: Kind) -> tuple[str | None, bool]:
        """Takes a snapshot under the checkpoint lock. Returns the commit that holds the files
        now, and true if the snapshot made a new checkpoint."""
        self._ensure()
        files = self._walk()
        if files is None:
            log.warning("no checkpoint: the work folder has more than %d files", MAX_FILES)
            return None, False
        return self._commit(self._tree(files), run_id, tool, kind)

    def _commit(
        self, tree: str, run_id: str, tool: str, kind: Kind, restored: str = ""
    ) -> tuple[str, bool]:
        """Records ``tree`` as a checkpoint. A ``tool`` checkpoint with the same files as the
        last checkpoint of the same run is not recorded. A ``rollback`` checkpoint is always
        recorded: ``undo`` reads its ``restored`` line."""
        head = self._head()
        if kind == "tool" and head is not None and head.tree == tree and head.run_id == run_id:
            return head.commit, False
        lines = [f"run: {_line(run_id)}", f"tool: {_line(tool)}", f"kind: {kind}"]
        if restored:
            lines.append(f"restored: {restored}")
        message = "sama checkpoint\n\n" + "\n".join(lines) + "\n"
        parents = ["-p", head.commit] if head is not None else []
        commit = self._git("commit-tree", tree, *parents, data=message.encode()).decode().strip()
        self._git("update-ref", REF, commit, *([head.commit] if head is not None else []))
        return commit, True

    # -- public -----------------------------------------------------------------------------

    def snapshot(self, run_id: str, tool: str) -> str | None:
        """Takes a snapshot before a tool call. Returns the new checkpoint, or None if no new
        checkpoint was needed. Raises on a failure; ``CheckpointGate`` only logs it."""
        if not os.path.isdir(self.workdir):
            return None
        with self._lock(CHECKPOINT_LOCK):
            commit, new = self._snapshot(run_id, tool, "tool")
        return commit if new else None

    def listing(self, limit: int = LIST_LIMIT) -> list[Checkpoint]:
        if not self._exists():
            return []
        with self._lock(CHECKPOINT_LOCK):
            return self._history(limit)

    def restore(self, number: int) -> Restored:
        """Restores checkpoint ``number`` of ``list``."""
        if number < 1:
            raise CheckpointError("The checkpoint number must be 1 or more.")
        return self._restore(lambda history, _tree: _pick(history, number))

    def undo(self) -> Restored:
        """Undoes the file changes of the latest run that changed files: restores the first
        checkpoint of that run. A rollback checkpoint is not a run, so ``undo`` again goes one
        run further back."""
        return self._restore(_undo_target)

    def _restore(self, choose: Callable[[Sequence[Checkpoint], str], Checkpoint]) -> Restored:
        if not self._exists():
            raise CheckpointError("This session has no checkpoints yet.")
        with (
            self._lock(RUNNER_LOCK, wait_s=RUNNER_LOCK_WAIT_S),
            self._lock(CHECKPOINT_LOCK),
        ):
            self._ensure()
            history = self._history(MAX_HISTORY)
            if not history:
                raise CheckpointError("This session has no checkpoints yet.")
            files = self._walk()
            if files is None:
                raise CheckpointError(
                    f"The work folder has more than {MAX_FILES} files. sama cannot restore it."
                )
            current = self._tree(files)
            target: Checkpoint = choose(history, current)
            wanted = _split(self._git("ls-tree", "-r", "-z", "--name-only", target.commit))
            # Decide before any change: a path is not written when the write would remove
            # files that the rollback checkpoint does not hold.
            guard = _Guard(self.workdir, set(files.paths))
            write = [rel for rel in wanted if guard.safe(rel)]
            saved, _ = self._commit(current, "rollback", "rollback", "rollback", target.commit)
            deleted = 0
            for rel in sorted(guard.saved - set(wanted), reverse=True):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.workdir, rel))
                    deleted += 1
                _prune(self.workdir, os.path.dirname(rel))
            if write:
                with tempfile.TemporaryDirectory(dir=self.git_dir) as scratch:
                    index = os.path.join(scratch, "index")
                    self._git("read-tree", target.commit, index=index)
                    self._git(
                        "checkout-index", "-f", "-z", "--stdin", data=_join(write), index=index
                    )
            return Restored(
                commit=target.commit,
                created_at=target.created_at,
                written=len(write),
                deleted=deleted,
                saved=saved,
                kept=len(wanted) - len(write),
            )


class CheckpointGate:
    """Wraps the tool gate of the runner. After the gate allows a call of a tool in
    ``CHANGE_TOOLS``, it takes a snapshot before the call runs.

    It also takes a snapshot when the gate answers ``ask``: the run pauses, and the call runs
    after the approval without a new gate check. No run can change the files of the session
    while the run is paused, so the snapshot holds the files before the call."""

    def __init__(self, inner: ToolGate, checkpoints: Checkpoints) -> None:
        self._inner = inner
        self._checkpoints = checkpoints

    async def check(
        self,
        scope: RunScope,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        child: bool = False,
    ) -> GateDecision:
        decision = await self._inner.check(
            scope, tool_name, arguments, call_id=call_id, child=child
        )
        if decision.action != "deny" and tool_name in CHANGE_TOOLS:
            await self._take(scope.run_id, tool_name)
        return decision

    async def _take(self, run_id: str, tool_name: str) -> None:
        started = time.monotonic()
        try:
            commit = await asyncio.to_thread(self._checkpoints.snapshot, run_id, tool_name)
        except Exception as exc:
            log.warning("the checkpoint failed tool=%s: %r", tool_name, exc)
            return
        log.info(
            "checkpoint tool=%s new=%s duration_s=%.3f",
            tool_name,
            commit is not None,
            time.monotonic() - started,
        )


def _pick(history: Sequence[Checkpoint], number: int) -> Checkpoint:
    if number > len(history):
        raise CheckpointError(f"There is no checkpoint {number}.")
    return history[number - 1]


def _undo_target(history: Sequence[Checkpoint], current: str) -> Checkpoint:
    """The first checkpoint of the newest run whose first checkpoint differs from the current
    files. ``history`` is newest first, and the checkpoints of one run are next to each other.

    When the newest checkpoint is a restore, the search starts after the checkpoint that the
    restore wrote. So each ``undo`` goes one run further back, and a new run starts again from
    the newest run."""
    start = 0
    if history[0].kind == "rollback":
        restored = history[0].restored
        start = next((i + 1 for i, c in enumerate(history) if c.commit == restored), len(history))
    groups: list[list[Checkpoint]] = []
    for checkpoint in history[start:]:
        if checkpoint.kind == "rollback":
            continue
        if groups and groups[-1][-1].run_id == checkpoint.run_id:
            groups[-1].append(checkpoint)
        else:
            groups.append([checkpoint])
    for group in groups:
        first = group[-1]
        if first.tree != current:
            return first
    raise CheckpointError("There is nothing to undo: no older checkpoint has other files.")


def _prune(workdir: str, rel_dir: str) -> None:
    """Removes the empty folders of a deleted file, up to the work folder."""
    while rel_dir:
        try:
            os.rmdir(os.path.join(workdir, rel_dir))
        except OSError:
            return
        rel_dir = os.path.dirname(rel_dir)


class _Guard:
    """Finds the paths of a target that a restore cannot write without data loss.

    ``saved`` holds the files of the ``rollback`` checkpoint. To write a target path, the
    restore can remove a file or a link that is in ``saved``, or a folder that holds only files
    in ``saved``. It must not remove anything else: a large file, an excluded folder (such as a
    ``.git`` folder), or a special file. ``git checkout-index -f`` would remove them."""

    def __init__(self, workdir: str, saved: set[str]) -> None:
        self.workdir = workdir
        self.saved = saved
        self._lossy: dict[str, bool] = {}

    def safe(self, rel: str) -> bool:
        parts = rel.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            lossy = self._lossy.get(prefix)
            if lossy is None:
                lossy = self._lossy[prefix] = self._replace_loses(prefix, depth == len(parts))
            if lossy:
                return False
        return True

    def _replace_loses(self, rel: str, last: bool) -> bool:
        """True if a write at (or under) ``rel`` removes something that is not saved."""
        try:
            st = os.lstat(os.path.join(self.workdir, rel))
        except (FileNotFoundError, NotADirectoryError):
            # Nothing is there, or a file on the path goes first (it is saved, or the check
            # of that file stops the write).
            return False
        except OSError:
            return True
        if stat.S_ISDIR(st.st_mode):
            # A folder on the path of a file stays. A folder at the place of a file goes.
            return last and self._holds_unsaved(rel)
        if stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            return rel not in self.saved
        return True

    def _holds_unsaved(self, rel: str) -> bool:
        for root, dirs, files in os.walk(os.path.join(self.workdir, rel), followlinks=False):
            names = files + [name for name in dirs if os.path.islink(os.path.join(root, name))]
            for name in names:
                path = os.path.relpath(os.path.join(root, name), self.workdir)
                if path not in self.saved:
                    return True
        return False


def _split(data: bytes) -> list[str]:
    return [os.fsdecode(item) for item in data.split(b"\0") if item]


def _join(paths: Sequence[str]) -> bytes:
    return b"".join(os.fsencode(path) + b"\0" for path in paths)


def _line(text: str) -> str:
    return " ".join(text.split())[:200]


def _meta(body: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in body.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            meta[key] = value.strip()
    return meta


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sama.runtime.checkpoints")
    parser.add_argument("--workdir", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--limit", type=int, default=LIST_LIMIT)
    commands.add_parser("undo")
    restore = commands.add_parser("restore")
    restore.add_argument("--number", type=int, required=True)
    args = parser.parse_args(argv)
    workdir = args.workdir or os.environ.get("SAMA_WORKDIR") or WORKDIR
    checkpoints = Checkpoints(workdir)
    result: dict[str, Any]
    try:
        if args.command == "list":
            limit = max(1, min(args.limit, 100))
            result = {"ok": True, "checkpoints": [asdict(c) for c in checkpoints.listing(limit)]}
        elif args.command == "undo":
            result = {"ok": True, "restored": asdict(checkpoints.undo())}
        else:
            result = {"ok": True, "restored": asdict(checkpoints.restore(args.number))}
    except BusyError as error:
        result = {"ok": False, "busy": True, "error": str(error)}
    except CheckpointError as error:
        result = {"ok": False, "error": str(error)}
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    raise SystemExit(main())
