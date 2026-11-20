"""The ``SKILL.md`` format: YAML frontmatter and a Markdown body (docs/architecture.md,
Section 9.1)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import yaml

from sama.skills.errors import SkillError

FRONTMATTER_MAX_CHARS: Final = 4_000
KNOWN_KEYS: Final = frozenset(
    {
        "name",
        "description",
        "version",
        "tags",
        "requires_toolsets",
        "requires_integrations",
        "config",
        "license",
        "compatibility",
        "metadata",
        "allowed-tools",
    }
)
"""The frontmatter keys of the Agent Skills format and of sama. Another key is an error, so a
typo does not pass silently."""


@dataclass(frozen=True, slots=True)
class Frontmatter:
    name: str
    description: str
    version: str
    tags: tuple[str, ...]
    requires_toolsets: tuple[str, ...]
    fields: Mapping[str, Any]
    """All keys of the block."""


@dataclass(frozen=True, slots=True)
class ParsedSkill:
    meta: Frontmatter
    body: str


def split(skill_md: str) -> tuple[str, str]:
    """The YAML text and the body of a ``SKILL.md``."""
    lines = skill_md.lstrip("\ufeff").split("\n")
    if lines[0].rstrip() != "---":
        raise SkillError("SKILL.md must start with a line of three hyphens (the frontmatter).")
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise SkillError("The frontmatter of SKILL.md has no closing line of three hyphens.")


def _strings(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SkillError(f"The frontmatter key {key} must be a list of text values.")
    return tuple(value)


def parse(skill_md: str) -> ParsedSkill:
    """Parses a ``SKILL.md``. It does not check the rules of the lint, only the form."""
    block, body = split(skill_md)
    if len(block) > FRONTMATTER_MAX_CHARS:
        raise SkillError(f"The frontmatter is longer than {FRONTMATTER_MAX_CHARS} characters.")
    try:
        # An alias can make a small block grow when the loader builds it, so refuse aliases.
        for event in yaml.parse(block, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise SkillError("The frontmatter must not use YAML anchors or aliases.")
        data = yaml.safe_load(block)
    except yaml.YAMLError as error:
        problem = getattr(error, "problem", None) or "the YAML is not valid"
        raise SkillError(f"The frontmatter is not valid YAML: {problem}.") from None
    if not isinstance(data, dict):
        raise SkillError("The frontmatter must be a YAML mapping.")
    unknown = sorted(str(key) for key in data if key not in KNOWN_KEYS)
    if unknown:
        raise SkillError(f"The frontmatter has unknown keys: {', '.join(unknown)}.")
    name, description = data.get("name"), data.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        raise SkillError("The frontmatter needs the text values name and description.")
    version = data.get("version")
    meta = Frontmatter(
        name=name,
        description=description,
        version="" if version is None else str(version),
        tags=_strings(data.get("tags"), "tags"),
        requires_toolsets=_strings(data.get("requires_toolsets"), "requires_toolsets"),
        fields=data,
    )
    return ParsedSkill(meta=meta, body=body)
