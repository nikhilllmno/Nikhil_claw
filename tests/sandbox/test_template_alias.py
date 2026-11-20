"""The content alias of the sandbox template (docs/architecture.md, Section 19.3)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sama.sandbox.template import (
    BUILD_FILES,
    ENTRY_POINTS,
    FingerprintError,
    content_alias,
    main,
    sandbox_sources,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of the files that go into the alias, plus one control plane file."""
    for name in BUILD_FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(
        ROOT / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    return tmp_path


def test_the_sources_hold_every_module_that_the_sandbox_imports() -> None:
    code = (
        "import contextlib, importlib, sys\n"
        f"for name in {list(ENTRY_POINTS)!r}:\n"
        "    with contextlib.suppress(ImportError):\n"
        "        importlib.import_module(name)\n"
        "for name, module in sys.modules.items():\n"
        "    if name == 'sama' or name.startswith('sama.'):\n"
        "        print(module.__file__)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True, cwd=ROOT
    ).stdout
    imported = {Path(line).resolve() for line in out.split()}
    sources = {path.resolve() for path in sandbox_sources(ROOT)}
    assert imported
    assert imported <= sources, sorted(str(path) for path in imported - sources)


def test_the_sources_leave_out_the_control_plane() -> None:
    names = {path.relative_to(ROOT / "src").as_posix() for path in sandbox_sources(ROOT)}
    assert "sama/runtime/runner.py" in names
    assert "sama/warm.py" in names
    assert not any(name.startswith(("sama/api/", "sama/orchestrator/")) for name in names)


def test_a_control_plane_change_keeps_the_alias(tree: Path) -> None:
    before = content_alias(tree)
    assert before.startswith("sama-runner-")
    assert len(before) == len("sama-runner-") + 12
    (tree / "src/sama/api/__init__.py").write_text("# a new comment\n", encoding="utf-8")
    assert content_alias(tree) == before


@pytest.mark.parametrize(
    "name", ["src/sama/runtime/contract.py", "src/sama/warm.py", "sandbox/Dockerfile", "uv.lock"]
)
def test_a_sandbox_change_changes_the_alias(tree: Path, name: str) -> None:
    before = content_alias(tree)
    with (tree / name).open("a", encoding="utf-8") as file:
        file.write("\n# a change\n")
    assert content_alias(tree) != before


def test_a_new_relative_import_joins_the_sources(tree: Path) -> None:
    extra = tree / "src/sama/runtime/extra_helper.py"
    extra.write_text("X = 1\n", encoding="utf-8")
    assert extra not in sandbox_sources(tree)
    runner = tree / "src/sama/runtime/runner.py"
    runner.write_text(
        runner.read_text(encoding="utf-8") + "\nfrom . import extra_helper  # noqa\n",
        encoding="utf-8",
    )
    assert extra in sandbox_sources(tree)


def test_an_import_by_name_in_the_sandbox_code_fails(tree: Path) -> None:
    runner = tree / "src/sama/runtime/runner.py"
    runner.write_text(
        runner.read_text(encoding="utf-8") + "\nimport importlib\nimportlib.import_module('x')\n",
        encoding="utf-8",
    )
    with pytest.raises(FingerprintError, match="import_module"):
        sandbox_sources(tree)


def test_print_alias_prints_the_alias_and_needs_no_key(
    tree: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    assert main(["--print-alias", "--root", str(tree)]) == 0
    assert capsys.readouterr().out.strip() == content_alias(tree)
