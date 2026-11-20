"""The builtin skills: the folders of ``skills/`` in the repo (docs/architecture.md, Section 9.2).

They are not in the database. A pull request changes them, and a deploy ships them. Each one
passes the same lint and scan as a stored skill. A skill that fails is not loaded, and the
log has an error.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

from sama.skills.errors import SkillError
from sama.skills.library import SkillDoc
from sama.skills.lint import FILE_FOLDERS, SKILL_FILE, validate

log = logging.getLogger(__name__)

ENV_DIRECTORY: Final = "SAMA_BUILTIN_SKILLS_DIR"


def builtin_directory() -> Path:
    """The folder of the builtin skills: the environment variable, else ``skills/`` of the
    repo, else ``skills/`` of the current folder."""
    configured = os.environ.get(ENV_DIRECTORY)
    if configured:
        return Path(configured)
    repo = Path(__file__).resolve().parents[3] / "skills"
    return repo if repo.is_dir() else Path.cwd() / "skills"


def _files(folder: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for top in FILE_FOLDERS:
        root = folder / top
        if not root.is_dir() or root.is_symlink():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                files[path.relative_to(folder).as_posix()] = path.read_text(encoding="utf-8")
    return files


def load(directory: Path) -> dict[str, SkillDoc]:
    """The valid skills of ``directory``, by name."""
    skills: dict[str, SkillDoc] = {}
    if not directory.is_dir():
        return skills
    for folder in sorted(directory.iterdir()):
        path = folder / SKILL_FILE
        if not path.is_file() or folder.is_symlink():
            continue
        try:
            skill_md, files = path.read_text(encoding="utf-8"), _files(folder)
            parsed = validate(folder.name, skill_md, files)
        except (SkillError, OSError, UnicodeDecodeError) as error:
            log.error("builtin skill %s is not valid and is not loaded: %s", folder.name, error)
            continue
        meta = parsed.meta
        skills[folder.name] = SkillDoc(
            name=folder.name,
            scope="builtin",
            description=meta.description.strip(),
            version=meta.version or "1",
            tags=meta.tags,
            requires_toolsets=meta.requires_toolsets,
            skill_md=skill_md,
            files=files,
        )
    return skills


class BuiltinSkills:
    """The builtin skills of a folder. The first read loads them, and a later read uses the
    loaded copy."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory
        self._skills: dict[str, SkillDoc] | None = None

    def all(self) -> dict[str, SkillDoc]:
        if self._skills is None:
            self._skills = load(self._directory or builtin_directory())
        return self._skills
