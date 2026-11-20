"""The text of the skill tool results."""

from __future__ import annotations

from collections.abc import Sequence

from sama.skills.errors import SkillError
from sama.skills.library import SkillDoc
from sama.skills.lint import SKILL_FILE
from sama.skills.store import Written

SANDBOX_ROOT = ".sama/skills"


def render_list(skills: Sequence[SkillDoc], query: str | None) -> str:
    if not skills:
        return f"No skills match {query!r}." if query else "There are no skills."
    lines = [f"Skills ({len(skills)}):"]
    for skill in skills:
        lines.append(f"- {skill.name} ({skill.scope}, v{skill.version}): {skill.description}")
    lines.append("Use skill_view with the name to load a skill.")
    return "\n".join(lines)


def render_view(skill: SkillDoc, file_path: str | None) -> str:
    if file_path and file_path != SKILL_FILE:
        if file_path not in skill.files:
            known = ", ".join(sorted(skill.files)) or "none"
            raise SkillError(f"The skill {skill.name} has no file {file_path}. Files: {known}.")
        return skill.files[file_path]
    lines = [
        f"Skill: {skill.name} ({skill.scope}, v{skill.version})",
        f"Sandbox folder: {SANDBOX_ROOT}/{skill.name}/ (in the work folder)",
        f"Files: {', '.join(sorted(skill.files)) or 'none'}",
        "",
        skill.skill_md,
    ]
    return "\n".join(lines)


def render_written(written: Written) -> str:
    return (
        f'Skill "{written.name}": {written.change} done in the scope {written.scope}. '
        f"It is version {written.version}. The index of this session does not change. A new "
        "session sees the change."
    )
