"""Read a GitHub code owners file (docs/architecture.md, Section 15.2).

``sama_pr`` reads the code owners file of the base branch. It finds the owners of each changed
path and asks them for a review. The ruleset of the repository then requires their approval
before a merge (Section 19.5).

The match follows the GitHub rules for ``CODEOWNERS``:

- Each line is a pattern and zero or more owners. ``#`` starts a comment. ``\\ `` is a space
  in a pattern. GitHub does not support ``\\#`` at the start of a pattern, so sama skips such
  a line.
- The last line that matches a path decides its owners. A line with no owners means that the
  path has no owner.
- ``*`` matches in one folder level. ``**`` matches in any number of levels. ``?`` matches one
  character.
- A pattern that starts with ``/`` or has a ``/`` in the middle starts at the root of the
  repository. Other patterns match at any level.
- A pattern that ends with ``/`` matches the files in that folder, at all levels below it. A
  pattern that ends with ``/*`` matches only the files directly in that folder. Any other
  pattern also matches the files below a folder of that name.
- GitHub does not support ``!`` and ``[ ]`` in this file. Sama skips such lines, as GitHub does.
- The match is case-sensitive.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

CODEOWNERS_PATHS: Final = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
"""GitHub uses the first of these files that exists on the base branch."""
MAX_CODEOWNERS_BYTES: Final = 3 * 1024 * 1024
"""GitHub ignores a code owners file that is larger than 3 MB."""

_USER = re.compile(r"@([A-Za-z0-9][A-Za-z0-9-]{0,38})")
_TEAM = re.compile(r"@([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9][A-Za-z0-9._-]{0,99})")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


@dataclass(frozen=True, slots=True)
class OwnerRule:
    """One line of a code owners file."""

    pattern: str
    owners: tuple[str, ...]
    line: int
    regex: re.Pattern[str]

    def matches(self, path: str) -> bool:
        return self.regex.fullmatch(path.lstrip("/")) is not None


@dataclass(frozen=True, slots=True)
class CodeOwners:
    """The rules of one code owners file, and the lines that sama skipped."""

    path: str
    rules: tuple[OwnerRule, ...]
    skipped: tuple[int, ...] = ()

    def owners_of(self, path: str) -> tuple[str, ...]:
        """The owners of ``path``: the owners of the last rule that matches it."""
        for rule in reversed(self.rules):
            if rule.matches(path):
                return rule.owners
        return ()


def parse_codeowners(text: str, *, path: str = ".github/CODEOWNERS") -> CodeOwners:
    """The rules of a code owners file. A line that GitHub does not support is skipped."""
    rules: list[OwnerRule] = []
    skipped: list[int] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        tokens = _tokens(raw)
        if tokens is None:
            skipped.append(number)
            continue
        if not tokens:
            continue
        pattern, *owners = tokens
        if pattern.startswith("!") or "[" in pattern or "]" in pattern:
            skipped.append(number)
            continue
        if not all(_valid_owner(owner) for owner in owners):
            skipped.append(number)
            continue
        rules.append(OwnerRule(pattern, tuple(owners), number, _compile(pattern)))
    return CodeOwners(path, tuple(rules), tuple(skipped))


def split_owners(owners: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(users, teams)`` to request a review from. A user is a login without ``@``. A team is
    the slug of a team of the organization that owns the repository. GitHub cannot request a
    review from an email address, so emails are left out."""
    users: dict[str, None] = {}
    teams: dict[str, None] = {}
    for owner in owners:
        team = _TEAM.fullmatch(owner)
        if team is not None:
            teams[team.group(2)] = None
            continue
        user = _USER.fullmatch(owner)
        if user is not None:
            users[user.group(1)] = None
    return tuple(users), tuple(teams)


def _tokens(line: str) -> list[str] | None:
    """The pattern and the owners of a line, without the comment. ``\\ `` in a pattern is a
    literal space. ``None`` for a line that GitHub does not support: a pattern that starts
    with ``\\#`` (GitHub does not support this escape)."""
    tokens: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line) and line[index + 1] in " \t":
            current.append(line[index + 1])
            index += 2
            continue
        if char in " \t\r\n\f\v":
            if current:
                tokens.append("".join(current))
                current = []
        elif char == "#" and not current:
            break
        else:
            current.append(char)
        index += 1
    if current:
        tokens.append("".join(current))
    if tokens and tokens[0].startswith("\\#"):
        return None
    return tokens


def _valid_owner(owner: str) -> bool:
    return bool(
        _USER.fullmatch(owner) or _TEAM.fullmatch(owner) or _EMAIL.fullmatch(owner)
    ) and not owner.startswith("#")


def _compile(pattern: str) -> re.Pattern[str]:
    folder_only = pattern.endswith("/") and pattern != "/"
    core = pattern.strip("/")
    if core in {"", "*", "**"}:
        return re.compile(r".*", re.DOTALL)
    anchored = pattern.startswith("/") or "/" in core
    prefix = "" if anchored else "(?:.*/)?"
    if pattern.endswith("/*"):
        # Only the files directly in the folder (a GitHub rule that differs from gitignore).
        suffix = ""
    elif folder_only:
        suffix = "/.*"
    else:
        suffix = "(?:/.*)?"
    return re.compile(prefix + _body(core.split("/")) + suffix, re.DOTALL)


def _body(segments: Sequence[str]) -> str:
    parts: list[str] = []
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        if segment == "**":
            if index == last:
                # ``a/**``: everything below ``a``.
                parts.append(".*")
            else:
                # ``**/b`` or ``a/**/b``: zero or more folder levels.
                parts.append("(?:.*/)?")
            continue
        parts.append(_segment(segment))
        if index != last:
            parts.append("/")
    return "".join(parts)


def _segment(segment: str) -> str:
    out: list[str] = []
    for char in segment:
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
    return "".join(out)
