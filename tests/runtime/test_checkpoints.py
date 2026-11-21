"""Checkpoints: shadow git snapshots of the work folder (docs/architecture.md, Section 15.3)."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from sama.runtime import checkpoints as module
from sama.runtime.checkpoints import (
    CHANGE_TOOLS,
    BusyError,
    CheckpointError,
    CheckpointGate,
    Checkpoints,
    main,
)
from sama.runtime.contract import RunScope
from sama.runtime.gating import GateDecision
from sama.runtime.runner import LOCK_FILE
from tests.support import make_scope

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _files(root: Path) -> dict[str, str]:
    """The files of the work folder that a checkpoint holds, with their text."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".sama") or "/.git" in f"/{rel}" or path.is_dir():
            continue
        found[rel] = os.readlink(path) if path.is_symlink() else path.read_text(encoding="utf-8")
    return found


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    return root


def test_the_change_tools_are_the_tools_that_write_files() -> None:
    assert {"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"} <= CHANGE_TOOLS
    assert {"mcp__sama__repo_clone", "mcp__sama__execute_code"} <= CHANGE_TOOLS
    assert not {"Read", "Glob", "Grep", "WebFetch", "TodoWrite"} & CHANGE_TOOLS


def test_a_work_folder_without_files_has_no_checkpoints(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    assert checkpoints.listing() == []
    with pytest.raises(CheckpointError, match="no checkpoints"):
        checkpoints.undo()
    assert Checkpoints(str(work / "missing")).snapshot("r1", "Bash") is None


def test_one_run_keeps_a_checkpoint_only_when_the_files_change(work: Path) -> None:
    _write(work, "a.txt", "one")
    checkpoints = Checkpoints(str(work))
    first = checkpoints.snapshot("r1", "Bash")
    assert first is not None
    assert checkpoints.snapshot("r1", "Edit") is None  # the same files
    _write(work, "a.txt", "two")
    assert checkpoints.snapshot("r1", "Write") is not None
    # A new run always gets its first checkpoint, also with the same files.
    assert checkpoints.snapshot("r2", "Bash") is not None
    listed = checkpoints.listing()
    assert [(c.number, c.run_id, c.tool, c.kind) for c in listed] == [
        (1, "r2", "Bash", "tool"),
        (2, "r1", "Write", "tool"),
        (3, "r1", "Bash", "tool"),
    ]
    assert listed[-1].commit == first
    assert listed[0].tree == listed[1].tree
    assert all(c.created_at > 0 for c in listed)
    assert [c.number for c in checkpoints.listing(2)] == [1, 2]


def test_a_snapshot_holds_the_files_of_cloned_repos_but_not_their_git_folders(
    work: Path,
) -> None:
    subprocess.run([module.GIT, "init", "-q", str(work / "repo")], check=True)
    _write(work, "repo/src/main.py", "print(1)")
    _write(work, "node_modules/lib/index.js", "x")
    _write(work, "pkg/__pycache__/m.pyc", "x")
    _write(work, ".sama/spill/out.txt", "x")
    _write(work, "sub/.git", "gitdir: ../elsewhere")
    os.symlink("repo/src/main.py", work / "link.py")
    checkpoints = Checkpoints(str(work))
    checkpoints.snapshot("r1", "Bash")
    names = subprocess.run(
        [module.GIT, "--git-dir", checkpoints.git_dir, "ls-tree", "-r", "--name-only", module.REF],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert sorted(names) == ["link.py", "repo/src/main.py"]


def test_undo_restores_the_files_before_the_latest_run(work: Path) -> None:
    subprocess.run([module.GIT, "init", "-q", str(work / "repo")], check=True)
    _write(work, "repo/a.txt", "one")
    _write(work, "keep.txt", "keep")
    checkpoints = Checkpoints(str(work))
    before_r1 = _files(work)
    checkpoints.snapshot("r1", "Bash")
    _write(work, "repo/a.txt", "two")
    _write(work, "r1.txt", "new")
    before_r2 = _files(work)
    checkpoints.snapshot("r2", "Write")
    (work / "keep.txt").unlink()
    _write(work, "deep/er/r2.txt", "x")
    _write(work, "repo/a.txt", "three")

    restored = checkpoints.undo()
    assert _files(work) == before_r2
    assert not (work / "deep").exists()  # the empty folders of deleted files go too
    assert (work / "repo/.git").is_dir()  # the git folder of the clone stays
    assert restored.deleted == 1
    newest = checkpoints.listing()[0]
    assert newest.kind == "rollback"
    assert newest.commit == restored.saved
    assert newest.restored == restored.commit

    # Each undo goes one run further back.
    checkpoints.undo()
    assert _files(work) == before_r1
    with pytest.raises(CheckpointError, match="nothing to undo"):
        checkpoints.undo()


def test_a_new_run_after_an_undo_is_undone_first(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    _write(work, "a.txt", "one")
    checkpoints.snapshot("r1", "Bash")
    _write(work, "a.txt", "two")
    checkpoints.undo()
    assert _files(work) == {"a.txt": "one"}
    checkpoints.snapshot("r3", "Bash")
    _write(work, "a.txt", "three")
    checkpoints.undo()
    assert _files(work) == {"a.txt": "one"}


def test_undo_skips_a_run_that_changed_no_files(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    _write(work, "a.txt", "one")
    checkpoints.snapshot("r1", "Bash")
    _write(work, "a.txt", "two")
    checkpoints.snapshot("r2", "Bash")  # r2 ran a command that changed nothing
    checkpoints.undo()
    assert _files(work) == {"a.txt": "one"}


def test_undo_refuses_when_parallel_runs_changed_files_at_the_same_time(work: Path) -> None:
    """M7.1: an undo of one run must not silently drop the files of a parallel run."""
    checkpoints = Checkpoints(str(work))
    _write(work, "a.txt", "one")
    checkpoints.snapshot("main", "Write")
    _write(work, "a.txt", "two")
    checkpoints.snapshot("side", "Write")
    _write(work, "b.txt", "side file")
    checkpoints.snapshot("main", "Write")
    _write(work, "a.txt", "three")
    checkpoints.snapshot("side", "Write")
    _write(work, "b.txt", "side file 2")
    before = _files(work)
    with pytest.raises(CheckpointError, match="at the same time as another run"):
        checkpoints.undo()
    assert _files(work) == before
    # /rollback still restores a chosen checkpoint.
    checkpoints.restore(4)
    assert _files(work) == {"a.txt": "one"}


def test_undo_of_a_run_after_a_parallel_run_ended_works(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    _write(work, "a.txt", "one")
    checkpoints.snapshot("side", "Write")
    _write(work, "side.txt", "s")
    checkpoints.snapshot("main", "Write")
    _write(work, "a.txt", "two")
    checkpoints.undo()
    assert _files(work) == {"a.txt": "one", "side.txt": "s"}


def test_rollback_restores_a_checkpoint_and_a_rollback_can_be_undone(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    _write(work, "a.txt", "one")
    checkpoints.snapshot("r1", "Bash")
    _write(work, "a.txt", "two")
    checkpoints.snapshot("r2", "Bash")
    _write(work, "a.txt", "three")
    _write(work, "b.txt", "b")

    restored = checkpoints.restore(2)  # the checkpoint before r1
    assert _files(work) == {"a.txt": "one"}
    assert restored.written == 1
    # The files before the rollback are checkpoint 1 now.
    newest = checkpoints.listing()[0]
    assert (newest.kind, newest.tool) == ("rollback", "rollback")
    checkpoints.restore(1)
    assert _files(work) == {"a.txt": "three", "b.txt": "b"}
    with pytest.raises(CheckpointError, match="no checkpoint 99"):
        checkpoints.restore(99)
    with pytest.raises(CheckpointError, match="1 or more"):
        checkpoints.restore(0)


def test_a_restore_keeps_large_files_and_excluded_folders(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 10)
    checkpoints = Checkpoints(str(work))
    _write(work, "a.txt", "one")
    checkpoints.snapshot("r1", "Bash")
    _write(work, "big.bin", "x" * 100)
    _write(work, "node_modules/m.js", "m")
    _write(work, "a.txt", "two")
    checkpoints.undo()
    assert (work / "a.txt").read_text() == "one"
    assert (work / "big.bin").exists()
    assert (work / "node_modules/m.js").exists()


def test_a_restore_does_not_overwrite_a_file_that_grew_too_large(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 10)
    checkpoints = Checkpoints(str(work))
    _write(work, "log.txt", "small")
    _write(work, "a.txt", "one")
    checkpoints.snapshot("r1", "Bash")
    _write(work, "log.txt", "large " * 10)
    _write(work, "a.txt", "two")
    restored = checkpoints.undo()
    assert (work / "a.txt").read_text() == "one"
    # The rollback checkpoint does not hold the large file, so the restore keeps it.
    assert (work / "log.txt").read_text() == "large " * 10
    assert (restored.written, restored.kept) == (1, 1)


def test_a_restore_does_not_replace_a_folder_with_unsaved_files(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 10)
    checkpoints = Checkpoints(str(work))
    _write(work, "a.txt", "one")
    os.symlink("a.txt", work / "repo")
    _write(work, "data", "file")
    checkpoints.snapshot("r1", "Bash")
    # The run puts a clone with local commits at the place of the link, and a folder with a
    # large file at the place of a file.
    (work / "repo").unlink()
    subprocess.run([module.GIT, "init", "-q", str(work / "repo")], check=True)
    _write(work, "repo/f.txt", "f")
    (work / "data").unlink()
    _write(work, "data/big.bin", "x" * 100)
    restored = checkpoints.undo()
    assert (work / "repo/.git/HEAD").is_file()
    assert (work / "data/big.bin").read_text() == "x" * 100
    assert (restored.written, restored.kept) == (1, 2)
    assert (work / "a.txt").read_text() == "one"


def test_a_restore_replaces_a_folder_that_holds_only_saved_files(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    _write(work, "data", "file")
    checkpoints.snapshot("r1", "Bash")
    (work / "data").unlink()
    _write(work, "data/a/b.txt", "b")
    restored = checkpoints.undo()
    assert (work / "data").read_text() == "file"
    assert (restored.written, restored.deleted, restored.kept) == (1, 1, 0)
    checkpoints.restore(1)
    assert (work / "data/a/b.txt").read_text() == "b"


def test_a_restore_replaces_a_link_at_the_place_of_a_folder(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    _write(work, "src/main.py", "old")
    checkpoints.snapshot("r1", "Bash")
    # The run moves the folder and puts a link to the new place.
    (work / "src").rename(work / "lib")
    _write(work, "lib/main.py", "new")
    os.symlink("lib", work / "src")
    restored = checkpoints.undo()
    assert not (work / "src").is_symlink()
    assert (work / "src/main.py").read_text() == "old"
    assert not (work / "lib").exists()
    assert restored.kept == 0


def test_git_attributes_do_not_change_the_bytes(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    attributes = "*.txt text eol=lf ident\n*.bin filter=missing\n"
    _write(work, ".gitattributes", attributes)
    original = {"a.txt": b"x\r\ny\r\n$Id$\r\n", "b.bin": b"\x00\r\n\xff"}
    for name, data in original.items():
        (work / name).write_bytes(data)
    checkpoints.snapshot("r1", "Bash")
    (work / "a.txt").write_bytes(b"changed")
    (work / "b.bin").unlink()
    checkpoints.undo()
    for name, data in original.items():
        assert (work / name).read_bytes() == data
    assert (work / ".gitattributes").read_text() == attributes


def test_a_restore_keeps_links_as_links(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    _write(work, "a.txt", "one")
    os.symlink("a.txt", work / "link")
    checkpoints.snapshot("r1", "Bash")
    (work / "link").unlink()
    _write(work, "link", "not a link")
    checkpoints.undo()
    assert (work / "link").is_symlink()
    assert os.readlink(work / "link") == "a.txt"


def test_a_restore_keeps_the_executable_bit(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    _write(work, "run.sh", "echo hi")
    (work / "run.sh").chmod(0o755)
    checkpoints.snapshot("r1", "Bash")
    (work / "run.sh").unlink()
    checkpoints.undo()
    assert os.access(work / "run.sh", os.X_OK)


def test_too_many_files_give_no_checkpoint(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "MAX_FILES", 2)
    for name in ("a", "b", "c"):
        _write(work, name, name)
    checkpoints = Checkpoints(str(work))
    assert checkpoints.snapshot("r1", "Bash") is None
    assert checkpoints.listing() == []


def test_a_restore_waits_for_the_runner_lock(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "RUNNER_LOCK_WAIT_S", 0.1)
    checkpoints = Checkpoints(str(work))
    _write(work, "a.txt", "one")
    checkpoints.snapshot("r1", "Bash")
    _write(work, "a.txt", "two")
    fd = os.open(work / LOCK_FILE, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(BusyError, match="A run works"):
            checkpoints.undo()
    finally:
        os.close(fd)
    assert (work / "a.txt").read_text() == "two"
    checkpoints.undo()
    assert (work / "a.txt").read_text() == "one"


def test_a_file_name_with_odd_characters_round_trips(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    _write(work, "with space/new\nline.txt", "x")
    _write(work, "ü.txt", "y")
    checkpoints.snapshot("r1", "Bash")
    shutil.rmtree(work / "with space")
    (work / "ü.txt").unlink()
    checkpoints.undo()
    assert _files(work) == {"with space/new\nline.txt": "x", "ü.txt": "y"}


# -- the gate ---------------------------------------------------------------------------------


class _Gate:
    def __init__(self, action: str) -> None:
        self.action = action
        self.calls: list[tuple[str, bool]] = []

    async def check(
        self,
        scope: RunScope,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        child: bool = False,
    ) -> GateDecision:
        self.calls.append((tool_name, child))
        return GateDecision(self.action, "why", "note")  # type: ignore[arg-type]


@pytest.mark.parametrize("action", ["allow", "ask"])
async def test_the_gate_takes_a_snapshot_before_a_change_tool(work: Path, action: str) -> None:
    inner = _Gate(action)
    checkpoints = Checkpoints(str(work))
    gate = CheckpointGate(inner, checkpoints)
    _write(work, "a.txt", "one")
    decision = await gate.check(make_scope("run-9"), "Bash", {}, call_id="c1", child=True)
    assert decision == GateDecision(action, "why", "note")  # type: ignore[arg-type]
    assert inner.calls == [("Bash", True)]
    [checkpoint] = checkpoints.listing()
    assert (checkpoint.run_id, checkpoint.tool) == ("run-9", "Bash")


async def test_the_gate_takes_no_snapshot_for_a_denied_call_or_a_read(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    _write(work, "a.txt", "one")
    await CheckpointGate(_Gate("deny"), checkpoints).check(make_scope(), "Write", {}, call_id="c")
    await CheckpointGate(_Gate("allow"), checkpoints).check(make_scope(), "Read", {}, call_id="c")
    assert checkpoints.listing() == []


async def test_a_failed_snapshot_does_not_stop_the_call(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    checkpoints = Checkpoints(str(work))

    def broken(run_id: str, tool: str) -> str | None:
        raise CheckpointError("git failed")

    monkeypatch.setattr(checkpoints, "snapshot", broken)
    with caplog.at_level(logging.WARNING, logger="sama.runtime.checkpoints"):
        decision = await CheckpointGate(_Gate("allow"), checkpoints).check(
            make_scope(), "Edit", {}, call_id="c"
        )
    assert decision.action == "allow"
    assert "the checkpoint failed tool=Edit" in caplog.text


# -- the command ------------------------------------------------------------------------------


def _main(capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    assert main(list(argv)) == 0
    result: dict[str, Any] = json.loads(capsys.readouterr().out)
    return result


def test_the_command_lists_undoes_and_restores(
    work: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMA_WORKDIR", str(work))
    assert _main(capsys, "list") == {"ok": True, "checkpoints": []}
    assert _main(capsys, "undo") == {"ok": False, "error": "This session has no checkpoints yet."}
    checkpoints = Checkpoints(str(work))
    _write(work, "a.txt", "one")
    checkpoints.snapshot("r1", "Bash")
    _write(work, "a.txt", "two")
    listed = _main(capsys, "list", "--limit", "5")["checkpoints"]
    assert [(c["number"], c["run_id"], c["kind"]) for c in listed] == [(1, "r1", "tool")]
    undone = _main(capsys, "--workdir", str(work), "undo")
    assert undone["ok"] is True
    assert undone["restored"]["commit"] == listed[0]["commit"]
    assert (work / "a.txt").read_text() == "one"
    redone = _main(capsys, "restore", "--number", "1")
    assert redone["ok"] is True
    assert (work / "a.txt").read_text() == "two"
    assert _main(capsys, "restore", "--number", "9")["error"] == "There is no checkpoint 9."


def test_the_command_reports_a_busy_runner(
    work: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "RUNNER_LOCK_WAIT_S", 0.05)
    _write(work, "a.txt", "one")
    Checkpoints(str(work)).snapshot("r1", "Bash")
    fd = os.open(work / LOCK_FILE, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        result = _main(capsys, "--workdir", str(work), "undo")
    finally:
        os.close(fd)
    assert result["ok"] is False
    assert result["busy"] is True


# -- diff (coding mode, Section 15.5) ------------------------------------------------------------


def test_the_diff_of_the_session_and_of_one_step(tmp_path: Path) -> None:
    checkpoints = Checkpoints(str(tmp_path))
    _write(tmp_path, "app.py", "a\nb\n")
    _write(tmp_path, "gone.txt", "x\n")
    checkpoints.snapshot("r1", "Write")
    _write(tmp_path, "app.py", "a\nc\nd\n")
    (tmp_path / "gone.txt").unlink()
    (tmp_path / "logo.bin").write_bytes(b"\0\1\2")
    checkpoints.snapshot("r2", "Edit")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "app.py").rename(tmp_path / "pkg" / "app.py")

    everything = checkpoints.diff(None)
    assert (everything.base.number, everything.head) == (2, None)
    found = {f.path: f for f in everything.files}
    # The first app.py and the new one have too little in common to be a rename.
    assert set(found) == {"app.py", "pkg/app.py", "gone.txt", "logo.bin"}
    assert found["app.py"].status == "deleted"
    assert (found["pkg/app.py"].status, found["pkg/app.py"].additions) == ("added", 3)
    assert found["pkg/app.py"].patch.startswith("diff --git a/pkg/app.py b/pkg/app.py")
    assert "+d" in found["pkg/app.py"].patch
    assert (found["gone.txt"].status, found["gone.txt"].deletions) == ("deleted", 1)
    assert found["logo.bin"].binary
    assert found["logo.bin"].additions is None

    step = checkpoints.diff(2, 1)
    assert {f.path: f.status for f in step.files} == {
        "app.py": "modified",
        "gone.txt": "deleted",
        "logo.bin": "added",
    }
    latest = checkpoints.diff(1)
    ((renamed),) = latest.files
    assert (renamed.path, renamed.old_path, renamed.status) == ("pkg/app.py", "app.py", "renamed")
    # A diff leaves the history as it was.
    assert len(checkpoints.listing(10)) == 2


def test_the_diff_cuts_a_long_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "MAX_FILE_PATCH_CHARS", 200)
    checkpoints = Checkpoints(str(tmp_path))
    _write(tmp_path, "a.txt", "")
    checkpoints.snapshot("r1", "Write")
    _write(tmp_path, "a.txt", "line\n" * 500)
    (changed,) = checkpoints.diff(None).files
    assert changed.truncated
    assert len(changed.patch) == 200
    assert changed.additions == 500


def test_the_diff_needs_checkpoints(tmp_path: Path) -> None:
    with pytest.raises(CheckpointError, match="no checkpoints"):
        Checkpoints(str(tmp_path)).diff(None)
    checkpoints = Checkpoints(str(tmp_path))
    _write(tmp_path, "a.txt", "1")
    checkpoints.snapshot("r1", "Write")
    with pytest.raises(CheckpointError, match="no checkpoint 4"):
        checkpoints.diff(4)


def test_the_diff_command_prints_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    checkpoints = Checkpoints(str(tmp_path))
    _write(tmp_path, "a.txt", "1\n")
    checkpoints.snapshot("r1", "Write")
    _write(tmp_path, "a.txt", "2\n")
    assert main(["--workdir", str(tmp_path), "diff", "--base", "oldest"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert [f["path"] for f in result["diff"]["files"]] == ["a.txt"]
    assert main(["--workdir", str(tmp_path), "diff", "--base", "x"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_the_snapshot_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "a.txt", "1\n")
    assert main(["--workdir", str(tmp_path), "snapshot", "--run", "r1", "--tool", "codex"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["ok"] is True
    assert first["commit"]
    assert main(["--workdir", str(tmp_path), "snapshot", "--run", "r1", "--tool", "codex"]) == 0
    assert json.loads(capsys.readouterr().out)["commit"] is None
    (item,) = Checkpoints(str(tmp_path)).listing(5)
    assert (item.run_id, item.tool) == ("r1", "codex")


def test_the_diff_stops_at_the_total_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "MAX_PATCH_CHARS", 300)
    checkpoints = Checkpoints(str(tmp_path))
    _write(tmp_path, "keep.txt", "")
    checkpoints.snapshot("r1", "Write")
    for name in ("a.txt", "b.txt", "c.txt"):
        _write(tmp_path, name, "line\n" * 100)
    found = checkpoints.diff(None)
    assert found.truncated
    assert sum(len(f.patch) for f in found.files) <= 300
    assert [f.path for f in found.files] == ["a.txt", "b.txt", "c.txt"]
    assert found.files[-1].patch == ""
    assert found.files[-1].truncated
    assert all(f.additions == 100 for f in found.files)


def test_a_type_change_keeps_each_patch_with_its_file(tmp_path: Path) -> None:
    checkpoints = Checkpoints(str(tmp_path))
    _write(tmp_path, "a.txt", "one\n")
    _write(tmp_path, "b.txt", "x\n")
    checkpoints.snapshot("r1", "Write")
    (tmp_path / "a.txt").unlink()
    (tmp_path / "a.txt").symlink_to("b.txt")
    _write(tmp_path, "b.txt", "y\n")
    files = {f.path: f for f in checkpoints.diff(None).files}
    assert files["a.txt"].status == "type_changed"
    assert "-one" in files["a.txt"].patch
    assert "+b.txt" in files["a.txt"].patch
    assert files["b.txt"].patch.startswith("diff --git a/b.txt b/b.txt")
    assert "+y" in files["b.txt"].patch


def test_a_slow_git_diff_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkpoints = Checkpoints(str(tmp_path))
    _write(tmp_path, "a.txt", "1\n")
    checkpoints.snapshot("r1", "Write")
    _write(tmp_path, "a.txt", "2\n")
    slow = tmp_path.parent / "slow-git"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    real = module.GIT
    monkeypatch.setattr(module, "GIT_TIMEOUT_S", 0.2)
    original = module.Checkpoints._patches

    def patched(self: Checkpoints, old: str, new: str, files: int) -> list[tuple[str, bool]]:
        monkeypatch.setattr(module, "GIT", str(slow))
        try:
            return original(self, old, new, files)
        finally:
            monkeypatch.setattr(module, "GIT", real)

    monkeypatch.setattr(module.Checkpoints, "_patches", patched)
    with pytest.raises(CheckpointError, match="too long"):
        checkpoints.diff(None)
