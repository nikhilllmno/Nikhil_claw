"""The lint and the scan of a skill, on every write (docs/architecture.md, Section 9.1)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Final

from sama.prompt.scan import scan
from sama.skills.commands import RESERVED
from sama.skills.errors import SkillError
from sama.skills.format import ParsedSkill, parse
from sama.vault.redact import redact

MAX_NAME_CHARS: Final = 64
MAX_DESCRIPTION_CHARS: Final = 60
MAX_TAGS: Final = 10
MAX_TAG_CHARS: Final = 30
MAX_SKILL_MD_CHARS: Final = 20_000
MAX_FILE_CHARS: Final = 50_000
MAX_FILES: Final = 20
MAX_TOTAL_CHARS: Final = 200_000
MAX_PATH_CHARS: Final = 200
FILE_FOLDERS: Final = ("references", "templates", "scripts", "assets")
SKILL_FILE: Final = "SKILL.md"

KNOWN_TOOLSETS: Final = frozenset(
    {
        "core",
        "todo",
        "web",
        "memory",
        "skills",
        "sessions",
        "clarify",
        "code_exec",
        "delegation",
        "cron",
        "workflows",
        "browser",
        "media",
        "connections",
        "integrations",
        "github",
        "self",
        "tool_search",
    }
)
"""The toolsets of docs/architecture.md, Section 10.2."""

NAME: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
TAG: Final = re.compile(r"[a-z0-9-]+")
_SEGMENT: Final = re.compile(r"[A-Za-z0-9._-]+")
_MAX_PROBLEMS: Final = 6


def name_problem(name: str) -> str | None:
    """Why ``name`` cannot be a skill name, or ``None``."""
    if len(name) > MAX_NAME_CHARS or NAME.fullmatch(name) is None:
        return (
            f"The name must have lowercase letters, digits, and single hyphens, at most "
            f"{MAX_NAME_CHARS} characters."
        )
    if name in RESERVED:
        return f'The name "{name}" is the name of a channel command. Choose another name.'
    return None


def path_problem(path: str) -> str | None:
    """Why ``path`` cannot be the path of a skill file, or ``None``."""
    if path == SKILL_FILE:
        return "Use the action edit or patch for SKILL.md."
    pure = PurePosixPath(path)
    parts = pure.parts
    if (
        not path
        or len(path) > MAX_PATH_CHARS
        or "\\" in path
        or "\x00" in path
        or pure.is_absolute()
        or len(parts) < 2
        or any(_SEGMENT.fullmatch(part) is None or part in {".", ".."} for part in parts)
    ):
        return (
            f"A file path is relative, has at most {MAX_PATH_CHARS} characters, and has a "
            f"folder and a file name, for example references/notes.md."
        )
    if parts[0] not in FILE_FOLDERS:
        return f"A file must be in one of the folders: {', '.join(FILE_FOLDERS)}."
    return None


def _findings(label: str, text: str) -> list[str]:
    found = list(scan(text))
    if redact(text) != text:
        found.append("secret")
    return [f"{label}: {', '.join(found)}"] if found else []


def problems(
    name: str, skill_md: str, files: Mapping[str, str]
) -> tuple[list[str], ParsedSkill | None]:
    """All problems of a skill, and the parsed skill when the ``SKILL.md`` has a valid form."""
    found: list[str] = []
    if (problem := name_problem(name)) is not None:
        found.append(problem)
    parsed: ParsedSkill | None = None
    try:
        parsed = parse(skill_md)
    except SkillError as error:
        found.append(str(error))
    if len(skill_md) > MAX_SKILL_MD_CHARS:
        found.append(f"SKILL.md is longer than {MAX_SKILL_MD_CHARS} characters.")
    if "\x00" in skill_md:
        found.append("SKILL.md has a NUL character.")
    if parsed is not None:
        found += _meta_problems(name, parsed)
    found += _file_problems(files)
    found += _findings(SKILL_FILE, skill_md)
    if parsed is not None:
        found += _findings("description", parsed.meta.description)
    for path, text in files.items():
        found += _findings(path, text)
    return found, parsed


def _meta_problems(name: str, parsed: ParsedSkill) -> list[str]:
    meta, found = parsed.meta, []
    if meta.name != name:
        found.append(f'The frontmatter name "{meta.name}" must be the skill name "{name}".')
    description = meta.description.strip()
    if not description or "\n" in description or len(description) > MAX_DESCRIPTION_CHARS:
        found.append(
            f"The description must be one line of 1 to {MAX_DESCRIPTION_CHARS} characters "
            f"(now {len(description)})."
        )
    if len(meta.tags) > MAX_TAGS or any(
        len(tag) > MAX_TAG_CHARS or TAG.fullmatch(tag) is None for tag in meta.tags
    ):
        found.append(
            f"Use at most {MAX_TAGS} tags. A tag has at most {MAX_TAG_CHARS} characters: "
            "lowercase letters, digits, and hyphens."
        )
    unknown = sorted(set(meta.requires_toolsets) - KNOWN_TOOLSETS)
    if unknown:
        found.append(f"requires_toolsets has unknown toolsets: {', '.join(unknown)}.")
    if not parsed.body.strip():
        found.append("The body of SKILL.md is empty.")
    return found


def _file_problems(files: Mapping[str, str]) -> list[str]:
    found: list[str] = []
    if len(files) > MAX_FILES:
        found.append(f"A skill has at most {MAX_FILES} files.")
    for path, text in files.items():
        if (problem := path_problem(path)) is not None:
            found.append(f"{path}: {problem}")
        if len(text) > MAX_FILE_CHARS:
            found.append(f"{path} is longer than {MAX_FILE_CHARS} characters.")
        if "\x00" in text:
            found.append(f"{path} has a NUL character.")
    if sum(map(len, files.values())) > MAX_TOTAL_CHARS:
        found.append(f"The files of a skill have at most {MAX_TOTAL_CHARS} characters together.")
    return found


def validate(name: str, skill_md: str, files: Mapping[str, str]) -> ParsedSkill:
    """The parsed skill. Raises ``SkillError`` with the problems, if there are any."""
    found, parsed = problems(name, skill_md, files)
    if found or parsed is None:
        listed = "".join(f"\n- {problem}" for problem in found[:_MAX_PROBLEMS])
        more = len(found) - _MAX_PROBLEMS
        extra = f"\n- and {more} more" if more > 0 else ""
        raise SkillError(f"The skill is not valid:{listed}{extra}")
    return parsed
