"""The hardline list of the tool gate (docs/architecture.md, Section 16.1, step 2).

A fixed deny list for shell commands that nothing overrides: no policy rule, no approval, and
no pre-approval. The list is short on purpose. It stops commands that destroy the sandbox or
its disks, not every risky command: the policy step asks a human for the rest.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from typing import Any, Final

SHELL_TOOLS: Final = {"Bash": "command"}
"""Tools that run a shell command, and the argument that holds the command."""

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
        re.compile(r"(?P<f>\w+|:)\s*\(\)\s*\{\s*(?P=f)\s*\|\s*(?P=f)\s*&\s*\}\s*;\s*(?P=f)"),
        "a fork bomb",
    ),
    (re.compile(r"\bmkfs(?:\.\w+)?\b"), "a command that formats a disk"),
    (re.compile(r"\b(?:wipefs|blkdiscard)\b"), "a command that wipes a disk"),
    (
        re.compile(r"\b(?:dd\b[^;&|\n]*\bof=|shred\b[^;&|\n]*\s)/dev/(?!null\b|zero\b|std)"),
        "a write to a raw disk",
    ),
    (
        re.compile(r">\s*/dev/(?:sd|hd|vd|xvd|nvme|mmcblk|disk|dm-)\w*"),
        "a write to a raw disk",
    ),
    (
        re.compile(rf"\b(?:curl|wget)\b[^;\n]*\|\s*(?:sudo|doas)\b[^;|\n]*\b{_SHELLS}\b"),
        "a remote script piped to a root shell",
    ),
    (
        re.compile(
            rf"\b(?:sudo|doas)\b[^;\n]*\b{_SHELLS}\b[^;\n]*(?:\$\(|<\(|`)\s*(?:curl|wget)\b"
        ),
        "a remote script run by a root shell",
    ),
    (
        re.compile(r"\bch(?:mod|own|grp)\b(?=[^;&|\n]*\s-\w*R)[^;&|\n]*\s/\*?(?=\s|$)"),
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
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if parts and parts[-1] == "*":
        parts = parts[:-1]
    return not parts or (len(parts) == 1 and parts[0] in _ROOT_DIRS)
