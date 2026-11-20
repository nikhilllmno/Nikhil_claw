"""Slash commands, the builtin skills, the sandbox sync, and the memory store."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

import pytest

from sama.runtime.contract import RunScope
from sama.sandbox.local import LocalSandbox
from sama.skills import BuiltinSkills, ManageRequest, MemorySkillStore, SkillError, commands
from sama.skills.builtin import ENV_DIRECTORY, builtin_directory, load
from sama.skills.lint import MAX_DESCRIPTION_CHARS
from sama.skills.render import render_list, render_view
from sama.skills.sync import MANIFEST, ROOT, sync_files, sync_skills
from sama.tenancy import RunTokenClaims
from tests.skills.helpers import actor, doc, md
from tests.support import make_scope


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/code-review the diff", ("code-review", "the diff")),
        ("/code_review", ("code-review", "")),
        ("/Code_Review@my_bot  x\ny", ("code-review", "x\ny")),
        ("/a", ("a", "")),
    ],
)
def test_a_skill_command_parses(text: str, expected: tuple[str, str]) -> None:
    assert commands.parse(text) == expected


@pytest.mark.parametrize(
    "text", ["hello", "/new", "/stop now", "/skills", "/model x", "/-a", "/a-", "/a--b", "/", "//a"]
)
def test_other_text_is_not_a_skill_command(text: str) -> None:
    assert commands.parse(text) is None


def test_the_expansion_names_the_skill_and_keeps_the_request() -> None:
    text = commands.expansion("code-review", "check this")
    assert text.startswith("check this")
    assert 'load the skill "code-review" with skill_view' in text
    assert "ask what the user wants" in commands.expansion("code-review", "")


ROOT_DIR = Path(__file__).resolve().parents[2] / "skills"


def test_the_builtin_skills_all_load() -> None:
    folders = sorted(p.name for p in ROOT_DIR.iterdir() if p.is_dir())
    skills = BuiltinSkills(ROOT_DIR).all()
    assert len(folders) >= 10
    assert sorted(skills) == folders
    for skill in skills.values():
        assert skill.scope == "builtin"
        assert 0 < len(skill.description) <= MAX_DESCRIPTION_CHARS
        assert skill.name not in commands.RESERVED


def test_the_builtin_folder_can_be_set_by_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENV_DIRECTORY, str(tmp_path))
    assert builtin_directory() == tmp_path
    monkeypatch.delenv(ENV_DIRECTORY)
    assert builtin_directory() == ROOT_DIR


def test_an_invalid_builtin_skill_is_skipped_with_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    good = tmp_path / "good"
    good.mkdir()
    (good / "SKILL.md").write_text(md("good"))
    (good / "references").mkdir()
    (good / "references" / "a.md").write_text("Notes")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text(md("other"))
    with caplog.at_level(logging.ERROR):
        skills = load(tmp_path)
    assert list(skills) == ["good"]
    assert skills["good"].files == {"references/a.md": "Notes"}
    assert "bad" in caplog.text
    assert load(tmp_path / "missing") == {}


def _sandbox(tmp_path: Path) -> LocalSandbox:
    return LocalSandbox(tmp_path / "box")


async def test_the_sync_writes_changes_and_removes_stale_files(tmp_path: Path) -> None:
    box = _sandbox(tmp_path)
    base = Path(box.workdir) / ROOT
    files = {"a/SKILL.md": "A", "a/references/n.md": "N", "b/SKILL.md": "B"}
    assert await sync_files(box, files) == 3
    assert (base / "a/references/n.md").read_text() == "N"
    assert set(json.loads((base / MANIFEST).read_text())) == set(files)

    assert await sync_files(box, files) == 0
    changed = {"a/SKILL.md": "A2", "a/references/n.md": "N"}
    assert await sync_files(box, changed) == 2
    assert (base / "a/SKILL.md").read_text() == "A2"
    assert not (base / "b/SKILL.md").exists()


async def test_the_sync_ignores_unsafe_paths(tmp_path: Path) -> None:
    box = _sandbox(tmp_path)
    files = {"../x/SKILL.md": "x", "a/../../b": "y", "A/SKILL.md": "z", "a/other/x.md": "w"}
    assert await sync_files(box, files) == 0
    assert (
        not (Path(box.workdir) / ROOT / MANIFEST).exists()
        or json.loads((Path(box.workdir) / ROOT / MANIFEST).read_text()) == {}
    )
    assert not (tmp_path / "box" / "x").exists()


class _Source:
    def __init__(self, files: Mapping[str, str] | Exception) -> None:
        self._files = files

    async def files(self, scope: RunScope) -> Mapping[str, str]:
        if isinstance(self._files, Exception):
            raise self._files
        return self._files


async def test_a_failed_sync_is_only_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    box = _sandbox(tmp_path)
    with caplog.at_level(logging.WARNING):
        await sync_skills(box, _Source(RuntimeError("db is down")), make_scope())
    assert "skill sync failed" in caplog.text
    await sync_skills(box, _Source({"a/SKILL.md": "A"}), make_scope())
    assert (Path(box.workdir) / ROOT / "a/SKILL.md").read_text() == "A"


def _claims() -> RunTokenClaims:
    scope = make_scope()
    return RunTokenClaims(
        org_id=scope.org_id,
        workspace_id=scope.workspace_id,
        session_id=scope.session_id,
        run_id=scope.run_id,
        user_id=scope.user_id,
        token_id="t-1",
        expires_at=0,
    )


async def test_the_memory_store_follows_the_rules() -> None:
    store = MemorySkillStore(actor(), {"base": doc("base", description="From builtin")})
    claims = _claims()
    created = await store.manage(
        claims, ManageRequest(action="create", name="mine", content=md("mine", "Mine"))
    )
    assert (created.scope, created.version) == ("user", 1)
    names = [s.name for s in await store.list(claims, None)]
    assert names == ["base", "mine"]
    assert [s.name for s in await store.list(claims, "builtin")] == ["base"]
    patched = await store.manage(
        claims,
        ManageRequest(action="patch", name="mine", old_string="Do it.", new_string="Do more."),
    )
    assert patched.version == 2
    assert "Do more." in (await store.view(claims, "mine")).skill_md
    with pytest.raises(SkillError, match="read-only"):
        await store.manage(claims, ManageRequest(action="edit", name="base", content=md("base")))
    await store.manage(claims, ManageRequest(action="archive", name="mine"))
    with pytest.raises(SkillError, match="no skill"):
        await store.view(claims, "mine")
    revived = await store.manage(
        claims, ManageRequest(action="create", name="mine", content=md("mine"))
    )
    assert revived.version == 4
    assert store.history("user", "mine") == ["create", "patch", "archive", "create"]


def test_the_render_functions() -> None:
    skill = doc("a", "user", description="Does a", files={"references/n.md": "Notes"})
    assert "- a (user, v1): Does a" in render_list([skill], None)
    assert "no skills" in render_list([], None).lower()
    view = render_view(skill, None)
    assert ".sama/skills/a/" in view
    assert "references/n.md" in view
    assert render_view(skill, "references/n.md") == "Notes"
    with pytest.raises(SkillError, match="no file"):
        render_view(skill, "references/zz.md")
