"""The hardline list of the tool gate (docs/architecture.md, Section 16.1, step 2).

A fixed deny list for shell commands that nothing overrides: no policy rule, no approval, and
no pre-approval. The list is short on purpose. It stops commands that destroy the sandbox or
its disks, not every risky command: the policy step asks a human for the rest.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Mapping
from typing import Any, Final

SHELL_TOOLS: Final = {"Bash": "command"}
"""Tools that run a shell command, and the argument that holds the command."""

MAX_COMMAND_CHARS: Final = 32_000
"""A longer shell command is on the hardline list. The check must stay fast, because the
gate runs it in the event loop of the tool server for all orgs."""

_SEPARATORS: Final = re.compile(r"\|\||&&|[;|&\n()`]|\$\(")
_ROOT_DIRS: Final = frozenset(
    {
        "bin",
        "boot",
        "dev",
        "etc",
        "home",
        "lib",
        "lib64",
        "opt",
        "proc",
        "root",
        "sbin",
        "srv",
        "sys",
        "usr",
        "var",
    }
)
_HOME: Final = frozenset({"~", "$HOME", "${HOME}"})
_WRAPPERS: Final = frozenset(
    {"sudo", "doas", "env", "nohup", "nice", "time", "command", "exec", "xargs"}
)
_SHELLS: Final = r"(?:ba|z|da|k)?sh"

_PATTERNS: Final = (
    # Each pattern has no unbounded gap that can follow many start positions, so a search
    # takes linear time. The patterns run on the squeezed command (one blank for each run of
    # white space). Checks with a gap are the functions below, one scan for each segment.
    (
        re.compile(r"(?<!\w)(?P<f>\w+|:) ?\(\) ?\{ ?(?P=f) ?\| ?(?P=f) ?& ?\} ?; ?(?P=f)"),
        "a fork bomb",
    ),
    (re.compile(r"\bmkfs(?:\.\w+)?\b"), "a command that formats a disk"),
    (re.compile(r"\b(?:wipefs|blkdiscard)\b"), "a command that wipes a disk"),
    (
        re.compile(r"> ?/dev/(?:sd|hd|vd|xvd|nvme|mmcblk|disk|dm-)"),
        "a write to a raw disk",
    ),
)
_BLANKS: Final = re.compile(r"\s+")
_LINES: Final = re.compile(r"[;\n]")
_COMMANDS: Final = re.compile(r"[;&|\n]")
_DD: Final = re.compile(r"\bdd\b")
_DD_TARGET: Final = re.compile(r"\bof=/dev/(?!null\b|zero\b|std)")
_SHRED: Final = re.compile(r"\bshred\b")
_SHRED_TARGET: Final = re.compile(r"\s/dev/(?!null\b|zero\b|std)")
_DOWNLOAD: Final = re.compile(r"\b(?:curl|wget)\b")
_ROOT: Final = re.compile(r"\b(?:sudo|doas)\b")
_ROOT_SHELL_STAGE: Final = re.compile(rf" ?(?:sudo|doas)\b[^;|\n]*\b{_SHELLS}\b")
_SHELL: Final = re.compile(rf"\b{_SHELLS}\b")
_DOWNLOAD_RUN: Final = re.compile(r"(?:\$\(|<\(|`) ?(?:curl|wget)\b")
_CHANGE: Final = re.compile(r"\bch(?:mod|own|grp)\b")
_RECURSIVE: Final = re.compile(r"\s-\w*R")
_SLASH: Final = re.compile(r"\s/\*?(?=\s|$)")


def _squeeze(command: str) -> str:
    """One blank for each run of white space. A run with a new line becomes a new line."""
    return _BLANKS.sub(lambda m: "\n" if "\n" in m.group() else " ", command)


def _after(first: re.Pattern[str], then: re.Pattern[str], text: str) -> bool:
    """``then`` matches after the first match of ``first``. Two scans, so linear time."""
    found = first.search(text)
    return found is not None and then.search(text, found.end()) is not None


def _raw_disk_write(text: str) -> bool:
    return any(
        _after(_DD, _DD_TARGET, part) or _after(_SHRED, _SHRED_TARGET, part)
        for part in _COMMANDS.split(text)
    )


def _piped_to_root_shell(text: str) -> bool:
    for line in _LINES.split(text):
        downloaded = False
        for stage in line.split("|"):
            if downloaded and _ROOT_SHELL_STAGE.match(stage):
                return True
            downloaded = downloaded or _DOWNLOAD.search(stage) is not None
    return False


def _root_shell_runs_download(text: str) -> bool:
    for line in _LINES.split(text):
        root = _ROOT.search(line)
        shell = _SHELL.search(line, root.end()) if root else None
        if shell and _DOWNLOAD_RUN.search(line, shell.end()):
            return True
    return False


def _recursive_change_of_root(text: str) -> bool:
    for part in _COMMANDS.split(text):
        change = _CHANGE.search(part)
        if change and _RECURSIVE.search(part, change.end()) and _SLASH.search(part, change.end()):
            return True
    return False


_CHECKS: Final = (
    (_raw_disk_write, "a write to a raw disk"),
    (_piped_to_root_shell, "a remote script piped to a root shell"),
    (_root_shell_runs_download, "a remote script run by a root shell"),
    (_recursive_change_of_root, "a recursive permission change of /"),
)


def hardline_problem(tool_name: str, arguments: Mapping[str, Any]) -> str | None:
    """Why the call is on the hardline list, or ``None``."""
    key = SHELL_TOOLS.get(tool_name)
    if key is None:
        return None
    command = arguments.get(key)
    if not isinstance(command, str):
        return None
    return command_problem(command)


def command_problem(command: str) -> str | None:
    """Why the shell command is on the hardline list, or ``None``."""
    if len(command) > MAX_COMMAND_CHARS:
        return f"a shell command longer than {MAX_COMMAND_CHARS} characters"
    if "--no-preserve-root" in command:
        return "rm --no-preserve-root"
    text = _squeeze(command)
    for pattern, problem in _PATTERNS:
        if pattern.search(text):
            return problem
    for check, problem in _CHECKS:
        if check(text):
            return problem
    for part in _SEPARATORS.split(command):
        if _is_root_delete(part):
            return "a recursive delete of / or a system or home directory"
    return None


def _is_root_delete(part: str) -> bool:
    try:
        words = shlex.split(part, comments=False, posix=True)
    except ValueError:
        words = part.split()
    at = next((i for i, word in enumerate(words) if word.rsplit("/", 1)[-1] == "rm"), None)
    if at is None or (at > 0 and words[0] not in _WRAPPERS and "=" not in words[0]):
        return False
    flags = [w for w in words[at + 1 :] if w.startswith("-")]
    targets = [w for w in words[at + 1 :] if not w.startswith("-")]
    recursive = any(
        f == "--recursive" or (not f.startswith("--") and ("r" in f or "R" in f)) for f in flags
    )
    return recursive and any(_is_root_like(t) for t in targets)


def _is_root_like(target: str) -> bool:
    path = target.strip()
    if path in _HOME or path.rstrip("/*") in _HOME:
        return True
    if not path.startswith("/"):
        return False
    # normpath resolves "." and ".." without the file system: "/etc/../usr" is "/usr".
    parts = [p for p in posixpath.normpath(path).split("/") if p]
    if parts and parts[-1] == "*":
        parts = parts[:-1]
    return not parts or (len(parts) == 1 and parts[0] in _ROOT_DIRS)
