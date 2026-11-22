"""Plan mode of coding mode in the tool gate (docs/architecture.md, Sections 15.5 and 16.1).

While a session is in plan mode, the agent reads and explores, then proposes a plan with
``plan_propose``. The gate refuses each tool that can change files or data. The rule is the
same for every runtime, because it is in the gate and not in an adapter.

- A tool in ``PLAN_MODE_TOOLS`` keeps its normal verdict: it reads, asks the user, or stores
  only sama state that a person decides later.
- ``Bash`` keeps its normal verdict only for a command that ``read_only_command`` accepts.
- A deferred tool keeps its normal verdict only when its access is ``read`` (Section 10.3).
- Every other tool gets ``deny`` with a reason that tells the agent what to do.

The command check is strict, and it refuses a command that it does not know. It is a guard for
the plan step, not a security boundary: the sandbox is the boundary (Section 16.2).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from typing import Any, Final

PLAN_PROPOSE: Final = "plan_propose"

PLAN_MODE_TOOLS: Final = frozenset(
    {
        "ToolSearch",
        "Read",
        "Glob",
        "Grep",
        "WebFetch",
        "todo",
        "TodoWrite",
        "web_search",
        "web_extract",
        "clarify",
        PLAN_PROPOSE,
        "coding_agent_plan",
        "repo_clone",
        "skills_list",
        "skill_view",
        "session_search",
        "session_read",
        "vision_analyze",
        "tool_search",
        "tool_describe",
        "Task",
        "Agent",
        "connections_list",
        "delegate_status",
        "task_list",
        "browser_open",
        "browser_snapshot",
        "browser_screenshot",
        "browser_extract",
        "browser_tabs",
    }
)
"""The tools that plan mode does not stop. ``repo_clone`` only copies code into the sandbox, so
the agent can read it. ``Task`` and ``Agent`` start a sub-agent, and each tool call of the
sub-agent passes the gate again, with plan mode. ``coding_agent_plan`` is a run of Claude Code
or Codex that starts in its plan mode (Section 15.5); ``coding_agent_run`` stays blocked."""

PLAN_MODE_REASON: Final = (
    "plan mode is on: tools that change files or data are blocked. Read and explore, then "
    "call plan_propose with your plan and stop. The user approves or edits the plan, and then "
    "plan mode goes off"
)

MAX_COMMAND_CHARS: Final = 4_000

_SEPARATORS: Final = frozenset({"|", "||", "&&", ";"})
_READ_COMMANDS: Final = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "tree",
        "pwd",
        "echo",
        "stat",
        "du",
        "df",
        "which",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "nl",
        "cut",
        "diff",
        "cmp",
        "true",
        "uname",
        "whoami",
        "id",
        "jq",
        "find",
        "git",
    }
)
"""Commands that only read. ``find``, ``rg``, ``tree``, and ``git`` have options that write
files or run programs; ``_segment_ok`` refuses those options. Commands with a write mode that
an argument can turn on are not here: for example ``sort -o``, ``uniq IN OUT``, ``file -C``,
``date -s``."""

_FIND_REFUSED: Final = frozenset(
    {
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-delete",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
    }
)
_GIT_READ: Final = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "ls-files",
        "ls-tree",
        "rev-parse",
        "blame",
        "describe",
        "shortlog",
        "branch",
        "remote",
    }
)
"""Git subcommands that only read. ``git grep`` is not here: ``-O`` opens a pager program."""
_GIT_FLAGS: Final = frozenset(
    {
        "--",
        "-a",
        "-b",
        "-p",
        "-r",
        "-s",
        "-u",
        "-v",
        "-vv",
        "-w",
        "--all",
        "--abbrev-ref",
        "--cached",
        "--decorate",
        "--graph",
        "--list",
        "--name-only",
        "--name-status",
        "--no-color",
        "--numstat",
        "--oneline",
        "--patch",
        "--porcelain",
        "--remotes",
        "--short",
        "--show-current",
        "--show-toplevel",
        "--staged",
        "--stat",
        "--verbose",
    }
)
_GIT_VALUE_FLAGS: Final = (
    "--format=",
    "--pretty=",
    "--max-count=",
    "--since=",
    "--until=",
    "--author=",
    "--grep=",
    "--unified=",
    "--abbrev=",
)
_GIT_NUMBER_FLAG: Final = re.compile(r"-(?:n|U)?\d+")
_GIT_BRANCH_FLAGS: Final = frozenset(
    {"-a", "-r", "-v", "-vv", "--all", "--remotes", "--list", "--show-current", "--verbose"}
)


def plan_mode_allows(name: str, arguments: Mapping[str, Any] | None, access: str | None) -> bool:
    """True when plan mode keeps the normal verdict of the call. ``name`` is the sama name of
    the tool. ``arguments`` is ``None`` when the gate knows only the digest of the call.
    ``access`` is the access of a deferred tool, if it has one."""
    if name in PLAN_MODE_TOOLS:
        return True
    if name == "Bash":
        if arguments is None:
            return False
        command = arguments.get("command")
        return isinstance(command, str) and read_only_command(command)
    return access == "read"


def read_only_command(command: str) -> bool:
    """True for a shell command that only reads: one or more known read commands, joined with
    ``|``, ``||``, ``&&``, or ``;``, with no redirect, no substitution, no variable, and no
    background job. A command that this check does not understand is refused."""
    if not command.strip() or len(command) > MAX_COMMAND_CHARS:
        return False
    # Bash runs each line. shlex reads a new line as a space, so refuse it first.
    if any(char in command for char in "\n\r\\`$"):
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # an open quote
        return False
    segment: list[str] = []
    segments: list[list[str]] = []
    for token in tokens:
        if token in _SEPARATORS:
            if not segment:
                return False
            segments.append(segment)
            segment = []
            continue
        if token and set(token) <= set("|&;<>()"):
            return False  # a redirect, a background job, or a subshell
        segment.append(token)
    if not segment:
        return False
    segments.append(segment)
    return all(_segment_ok(words) for words in segments)


def _segment_ok(words: list[str]) -> bool:
    program, args = words[0], words[1:]
    if program == "cd":
        # ``cd DIR`` only changes the folder of the next commands, for example
        # ``cd repo && git log``. One plain folder name; no option and no ``-``.
        return len(args) <= 1 and not any(arg.startswith("-") for arg in args)
    if program not in _READ_COMMANDS:
        return False
    if program == "find":
        return not any(arg in _FIND_REFUSED for arg in args)
    if program == "rg":
        return not any(arg.startswith("--pre") for arg in args)
    if program == "tree":
        # ``-o FILE`` writes a file, also in a group of short options such as ``-ao``.
        return not any(
            arg.startswith("--output")
            or (arg.startswith("-") and not arg.startswith("--") and "o" in arg)
            for arg in args
        )
    if program == "git":
        return _git_ok(args)
    return True


def _git_ok(args: list[str]) -> bool:
    """A git subcommand that only reads, and only the options in an allow list. Before the
    subcommand only ``-C DIR`` is allowed; any other option (for example ``-c``) is refused."""
    if len(args) >= 2 and args[0] == "-C" and not args[1].startswith("-"):
        args = args[2:]  # ``git -C DIR`` reads in another folder
    if not args or args[0] not in _GIT_READ:
        return False
    sub, rest = args[0], args[1:]
    if sub == "branch":
        return all(arg in _GIT_BRANCH_FLAGS for arg in rest)
    if sub == "remote":
        return all(arg in {"-v", "--verbose"} for arg in rest)
    return all(
        not arg.startswith("-")
        or arg in _GIT_FLAGS
        or arg.startswith(_GIT_VALUE_FLAGS)
        or _GIT_NUMBER_FLAG.fullmatch(arg) is not None
        for arg in rest
    )
