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

_SPAN: Final = r"{0,400}"
"""The bound of each gap in a pattern. Each pattern then does at most a fixed amount of work
at each start position, so a check takes linear time."""

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
    (
        re.compile(
            r"(?<!\w)(?P<f>\w{1,64}|:)\s{0,8}\(\)\s{0,8}\{\s{0,8}(?P=f)\s{0,8}\|\s{0,8}"
            r"(?P=f)\s{0,8}&\s{0,8}\}\s{0,8};\s{0,8}(?P=f)"
        ),
        "a fork bomb",
    ),
    (re.compile(r"\bmkfs(?:\.\w+)?\b"), "a command that formats a disk"),
    (re.compile(r"\b(?:wipefs|blkdiscard)\b"), "a command that wipes a disk"),
    (
        re.compile(
            rf"\b(?:dd\b[^;&|\n]{_SPAN}\bof=|shred\b[^;&|\n]{_SPAN}\s)/dev/(?!null\b|zero\b|std)"
        ),
        "a write to a raw disk",
    ),
    (
        re.compile(r">\s{0,8}/dev/(?:sd|hd|vd|xvd|nvme|mmcblk|disk|dm-)"),
        "a write to a raw disk",
    ),
    (
        re.compile(
            rf"\b(?:curl|wget)\b[^;\n]{_SPAN}\|\s{{0,8}}(?:sudo|doas)\b[^;|\n]{_SPAN}\b{_SHELLS}\b"
        ),
        "a remote script piped to a root shell",
    ),
    (
        re.compile(
            rf"\b(?:sudo|doas)\b(?=[^;\n]{_SPAN}\b{_SHELLS}\b)[^;\n]{_SPAN}(?:\$\(|<\(|`)\s{{0,8}}"
            r"(?:curl|wget)\b"
        ),
        "a remote script run by a root shell",
    ),
    (
        re.compile(
            rf"\bch(?:mod|own|grp)\b(?=[^;&|\n]{_SPAN}\s-\w{{0,16}}R)[^;&|\n]{_SPAN}\s/\*?(?=\s|$)"
        ),
        "a recursive permission change of /",
    ),
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
    for pattern, problem in _PATTERNS:
        if pattern.search(command):
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
