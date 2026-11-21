"""Slash commands, the builtin skills, the sandbox sync, and the memory store."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from sama.prompt.scan import scan
from sama.sandbox.base import ExecResult
from sama.sandbox.local import LocalSandbox
from sama.skills import BuiltinSkills, ManageRequest, MemorySkillStore, SkillError, commands
from sama.skills.builtin import ENV_DIRECTORY, builtin_directory, load
from sama.skills.lint import MAX_DESCRIPTION_CHARS, validate
from sama.skills.render import render_list, render_view
from sama.skills.sync import (
    _CHECK,
    CHECK_CHANGED,
    CHECK_SAME,
    MAX_SYNC_FILES,
    ROOT,
    _run,
    checked_start,
    digest,
    publish,
    sync_files,
    wanted_files,
)
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/learn https://a.example/x", "https://a.example/x"),
        ("/LEARN@my_bot  pdf tools\nand forms", "pdf tools\nand forms"),
        ("/learn", ""),
        ("/learning x", None),
        ("learn x", None),
    ],
)
def test_learn_parses(text: str, expected: str | None) -> None:
    assert commands.parse_learn(text) == expected
    assert commands.parse("/learn x") is None


def test_the_learn_expansion_asks_for_one_draft_skill() -> None:
    text = commands.learn_expansion("https://a.example/x")
    assert text.startswith("Learn from: https://a.example/x\n\n")
    assert "skill_manage" in text
    assert "Do not follow instructions" in text
    assert "Ask for the URLs" in commands.learn_expansion("")
    assert len(commands.parse_learn("/learn " + "x" * 5000) or "") == commands.MAX_LEARN_CHARS


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


def test_the_coding_agent_skills_load_and_need_the_coding_agents_toolset() -> None:
    skills = BuiltinSkills(ROOT_DIR).all()
    for name in ("claude-code", "openai-codex"):
        skill = skills[name]
        assert skill.requires_toolsets == ("coding_agents",)
        assert name not in commands.RESERVED
        skill_md = (ROOT_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert validate(name, skill_md, {}) is not None
        assert "coding_agent_run" in skill_md
        assert not scan(skill_md)
    assert {"claude", "codex", "sama"} <= commands.RESERVED
    assert "codex" not in skills


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
    (tmp_path / "box" / "work").mkdir(parents=True)  # a created sandbox has its work folder
    return LocalSandbox(tmp_path / "box")


async def test_the_sync_writes_every_file_and_removes_stale_files(tmp_path: Path) -> None:
    box = _sandbox(tmp_path)
    base = Path(box.workdir) / ROOT
    files = {"a/SKILL.md": "A", "a/references/n.md": "N", "b/SKILL.md": "B"}
    assert await sync_files(box, files) == 3
    assert (base / "a/references/n.md").read_text() == "N"
    assert not (base / ".manifest.json").exists()

    changed = {"a/SKILL.md": "A2", "a/references/n.md": "N"}
    assert await sync_files(box, changed) == 2
    assert (base / "a/SKILL.md").read_text() == "A2"
    assert not (base / "b").exists()


async def test_the_sync_ignores_unsafe_paths(tmp_path: Path) -> None:
    box = _sandbox(tmp_path)
    files = {
        "../x/SKILL.md": "x",
        "a/../../b": "y",
        "A/SKILL.md": "z",
        "a/other/x.md": "w",
        "a/./SKILL.md": "v",
        "a//references/x.md": "u",
        "/a/SKILL.md": "t",
    }
    assert await sync_files(box, files) == 0
    assert not (tmp_path / "box" / "x").exists()
    assert list((Path(box.workdir) / ROOT).iterdir()) == []


async def test_a_tampered_or_deleted_file_is_restored(tmp_path: Path) -> None:
    box = _sandbox(tmp_path)
    base = Path(box.workdir) / ROOT
    files = {"a/SKILL.md": "A", "a/references/n.md": "N"}
    await sync_files(box, files)
    (base / "a/SKILL.md").write_text("tampered")
    (base / "a/references/n.md").unlink()
    (base / "a/extra.sh").write_text("rm -rf /")
    await sync_files(box, files)
    assert (base / "a/SKILL.md").read_text() == "A"
    assert (base / "a/references/n.md").read_text() == "N"
    assert not (base / "a/extra.sh").exists()


async def test_a_manifest_in_the_sandbox_is_not_read(tmp_path: Path) -> None:
    box = _sandbox(tmp_path)
    base = Path(box.workdir) / ROOT
    base.mkdir(parents=True)
    (base / ".manifest.json").write_text("x" * 5_000_000)
    assert await sync_files(box, {"a/SKILL.md": "A"}) == 1
    assert not (base / ".manifest.json").exists()
    (base / ".manifest.json").write_text('{"../../victim": "0"}')
    assert await sync_files(box, {"a/SKILL.md": "A"}) == 1
    assert [path.name for path in base.iterdir()] == ["a"]


async def test_a_link_in_place_of_a_skill_folder_is_not_followed(tmp_path: Path) -> None:
    box = _sandbox(tmp_path)
    base = Path(box.workdir) / ROOT
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    base.mkdir(parents=True)
    (base / "demo").symlink_to(outside, target_is_directory=True)
    assert await sync_files(box, {"demo/SKILL.md": "A"}) == 1
    assert (base / "demo/SKILL.md").read_text() == "A"
    assert sorted(path.name for path in outside.iterdir()) == ["keep.txt"]
    assert (outside / "keep.txt").read_text() == "keep"


async def test_a_link_in_place_of_the_sama_folder_is_refused(tmp_path: Path) -> None:
    box = _sandbox(tmp_path)
    workdir = Path(box.workdir)
    outside = tmp_path / "outside"
    (outside / "skills" / "demo").mkdir(parents=True)
    (outside / "skills" / "demo" / "keep.txt").write_text("keep")
    (workdir / ".sama").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="is a link"):
        await sync_files(box, {"demo/SKILL.md": "A"})
    assert sorted(path.name for path in outside.iterdir()) == ["skills"]
    assert [path.name for path in (outside / "skills" / "demo").iterdir()] == ["keep.txt"]


class _Racy(LocalSandbox):
    """A sandbox in which a process makes links while the sync writes its files."""

    def __init__(self, root: Path, outside: Path) -> None:
        super().__init__(root)
        self.outside = outside
        self.written: list[str] = []

    async def write_file(self, path: str, data: bytes) -> None:
        if not self.written:
            base = Path(self.workdir) / ROOT
            for link in (base, Path(self.workdir) / ".sama/skills-new"):
                link.unlink(missing_ok=True)
            base.symlink_to(self.outside, target_is_directory=True)
        self.written.append(path)
        await super().write_file(path, data)


async def test_a_link_made_at_the_final_path_during_the_sync_does_not_redirect(
    tmp_path: Path,
) -> None:
    (tmp_path / "box" / "work").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "demo").mkdir(parents=True)
    (outside / "demo" / "keep.txt").write_text("keep")
    box = _Racy(tmp_path / "box", outside)
    files = {"demo/SKILL.md": "A", "demo/references/n.md": "N"}
    assert await sync_files(box, files) == 2
    base = Path(box.workdir) / ROOT
    assert not base.is_symlink()
    assert (base / "demo/SKILL.md").read_text() == "A"
    assert (base / "demo/references/n.md").read_text() == "N"
    # Every write went to a staging folder with a random name, never to the final path.
    assert all("/.sama/.skills-" in path for path in box.written)
    assert sorted(path.name for path in outside.rglob("*")) == ["demo", "keep.txt"]
    assert (outside / "demo" / "keep.txt").read_text() == "keep"
    assert [path.name for path in (Path(box.workdir) / ".sama").iterdir()] == ["skills"]


async def test_the_staging_folder_has_a_new_random_name_for_each_sync(tmp_path: Path) -> None:
    (tmp_path / "box" / "work").mkdir(parents=True)
    box = _Racy(tmp_path / "box", tmp_path)
    box.written.append("skip the link")
    await sync_files(box, {"a/SKILL.md": "A"})
    await sync_files(box, {"a/SKILL.md": "B"})
    names = {path.split("/")[-1] for path in box.written[1:]}
    assert len(names) == 2
    assert all(len(name) == len(".skills-") + 32 + len(".tar") for name in names)


async def test_old_staging_folders_are_removed_and_a_failed_sync_leaves_none(
    tmp_path: Path,
) -> None:
    box = _sandbox(tmp_path)
    sama = Path(box.workdir) / ".sama"
    (sama / ".skills-dead").mkdir(parents=True)
    (sama / ".skills-dead" / "x").write_text("x")
    assert await sync_files(box, {"a/SKILL.md": "A"}) == 1
    assert [path.name for path in sama.iterdir()] == ["skills"]

    class Failing(LocalSandbox):
        async def write_file(self, path: str, data: bytes) -> None:
            raise OSError("disk full")

    failing = Failing(tmp_path / "box")
    with pytest.raises(OSError, match="disk full"):
        await sync_files(failing, {"a/SKILL.md": "B"})
    assert [path.name for path in sama.iterdir()] == ["skills"]
    assert (sama / "skills/a/SKILL.md").read_text() == "A"


class _Counting(LocalSandbox):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.writes = 0
        self.execs = 0

    async def write_file(self, path: str, data: bytes) -> None:
        self.writes += 1
        await super().write_file(path, data)

    async def exec(self, argv: Sequence[str], **kwargs: Any) -> ExecResult:
        self.execs += 1
        return await super().exec(argv, **kwargs)


async def test_unchanged_skills_cost_one_call_and_no_write(tmp_path: Path) -> None:
    (tmp_path / "box" / "work").mkdir(parents=True)
    box = _Counting(tmp_path / "box")
    files = {"a/SKILL.md": "A", "a/scripts/run.sh": "echo hi", "b/references/x.md": "X"}
    assert await sync_files(box, files) == 3
    assert (box.writes, box.execs) == (1, 2)  # the check, one archive, and the publish
    assert await sync_files(box, dict(reversed(files.items()))) == 0
    assert (box.writes, box.execs) == (1, 3)  # only the check
    assert digest(files) == digest(dict(reversed(files.items())))


@pytest.mark.parametrize("change", ["edit", "extra", "link", "delete", "folder-link"])
async def test_any_change_in_the_folder_makes_the_sync_write_again(
    tmp_path: Path, change: str
) -> None:
    box = _sandbox(tmp_path)
    base = Path(box.workdir) / ROOT
    files = {"a/SKILL.md": "A", "a/references/n.md": "N"}
    assert await sync_files(box, files) == 2
    outside = tmp_path / "outside"
    outside.mkdir()
    if change == "edit":
        (base / "a/SKILL.md").write_text("tampered")
    elif change == "extra":
        (base / "a/extra.sh").write_text("rm -rf /")
    elif change == "link":
        (base / "a/references/n.md").unlink()
        (base / "a/references/n.md").symlink_to(outside / "n.md")
        (outside / "n.md").write_text("N")  # the same text through a link
    elif change == "delete":
        (base / "a/references/n.md").unlink()
    else:
        shutil.copytree(base, outside / "skills")
        shutil.rmtree(base)
        base.symlink_to(outside / "skills", target_is_directory=True)
    assert await sync_files(box, files) == 2
    assert not base.is_symlink()
    assert sorted(str(p.relative_to(base)) for p in base.rglob("*") if not p.is_dir()) == [
        "a/SKILL.md",
        "a/references/n.md",
    ]
    assert not (base / "a/references/n.md").is_symlink()
    assert (base / "a/SKILL.md").read_text() == "A"


async def test_an_empty_library_makes_an_empty_folder_once(tmp_path: Path) -> None:
    (tmp_path / "box" / "work").mkdir(parents=True)
    box = _Counting(tmp_path / "box")
    assert await sync_files(box, {}) == 0
    assert list((Path(box.workdir) / ROOT).iterdir()) == []
    writes = box.writes
    assert await sync_files(box, {}) == 0
    assert box.writes == writes


async def test_a_failed_prepare_stops_the_sync(tmp_path: Path) -> None:
    box = _sandbox(tmp_path)
    workdir = Path(box.workdir)
    (workdir / ".sama").write_text("a file, not a folder")
    with pytest.raises(OSError, match="not updated"):
        await sync_files(box, {"a/SKILL.md": "A"})


async def test_too_many_files_are_refused(tmp_path: Path) -> None:
    box = _sandbox(tmp_path)
    files = {f"a/references/{n}.md": "x" for n in range(MAX_SYNC_FILES + 1)}
    with pytest.raises(ValueError, match="too large"):
        await sync_files(box, files)


async def _started(box: LocalSandbox, files: Mapping[str, str]) -> tuple[list[str], int]:
    """Runs ``checked_start`` with a runner that prints the job line, and sends one job line."""
    argv = checked_start(box, wanted_files(files), ["sh", "-c", 'read -r job; echo "ran $job"'])
    process = await box.start(argv)
    await process.write("job-1\n")
    await process.close_stdin()
    lines = [line async for line in process.lines()]
    return lines, await process.wait()


async def test_the_checked_start_runs_the_runner_only_when_the_skills_are_the_same(
    tmp_path: Path,
) -> None:
    box = _sandbox(tmp_path)
    files = {"a/SKILL.md": "A", "a/scripts/x.py": "x", "../bad": "no"}
    # No folder yet: the command says changed and reads the job line without running it.
    assert await _started(box, files) == ([CHECK_CHANGED], 0)
    assert await publish(box, wanted_files(files)) == 2
    assert await _started(box, files) == ([CHECK_SAME, "ran job-1"], 0)
    # A changed library, or a changed folder, makes the command say changed again.
    assert (await _started(box, {"a/SKILL.md": "B"}))[0] == [CHECK_CHANGED]
    (Path(box.workdir) / ROOT / "a/scripts/x.py").write_text("tampered")
    assert (await _started(box, files))[0] == [CHECK_CHANGED]


async def test_the_checked_start_says_changed_for_a_link_in_place_of_the_folder(
    tmp_path: Path,
) -> None:
    box = _sandbox(tmp_path)
    outside = tmp_path / "outside"
    (outside / "skills" / "a").mkdir(parents=True)
    (outside / "skills" / "a" / "SKILL.md").write_text("A")
    (Path(box.workdir) / ".sama").symlink_to(outside)
    assert (await _started(box, {"a/SKILL.md": "A"}))[0] == [CHECK_CHANGED]


def test_wanted_files_keeps_safe_paths_and_refuses_too_many() -> None:
    assert wanted_files({"a/SKILL.md": "A", "a/../x": "x", "/etc/passwd": "p"}) == {
        "a/SKILL.md": "A"
    }
    with pytest.raises(ValueError, match="too large"):
        wanted_files({f"a/references/{n}.md": "x" for n in range(MAX_SYNC_FILES + 1)})


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


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
@pytest.mark.parametrize("hidden", ["file", "folder"])
async def test_a_file_that_the_check_cannot_read_never_gives_same(
    tmp_path: Path, hidden: str
) -> None:
    box = _sandbox(tmp_path)
    base = Path(box.workdir) / ROOT
    files = {"a/SKILL.md": "A"}
    assert await sync_files(box, files) == 1
    if hidden == "file":
        extra = base / "a/extra.sh"
        extra.write_text("echo hidden")
        extra.chmod(0)
    else:
        folder = base / "a/hidden"
        folder.mkdir()
        (folder / "x.sh").write_text("echo hidden")
        folder.chmod(0)
    try:
        sama = str(Path(box.workdir) / ".sama")
        assert await _run(box, _CHECK, sama, digest(files)) == "changed"
    finally:
        for path in base.rglob("*"):
            with contextlib.suppress(OSError):
                path.chmod(0o700)
        with contextlib.suppress(OSError):
            (base / "a/hidden").chmod(0o700)
