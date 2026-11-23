"""Tests of the process fence (src/sama/runtime/fence.py) with real processes."""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from sama.runtime import fence

SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def subreaper() -> Iterator[None]:
    """Makes the test process a subreaper, as a runner is, for the length of the test."""
    assert fence.set_subreaper()
    yield
    # Take back what the test left (zombies of orphans that came to this process).
    for pid in list(fence.descendants(os.getpid())):
        os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


def wait_until_gone(pid: int, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if fence.exited(pid):
            return True
        time.sleep(0.01)
    return False


def test_the_fence_finds_and_kills_a_child_and_its_child(subreaper: None) -> None:
    child = subprocess.Popen(
        ["/bin/sh", "-c", f"{sys.executable} -c 'import time; time.sleep(60)' & wait"]
    )
    time.sleep(0.3)
    found = fence.descendants(os.getpid())
    assert child.pid in found
    assert len(found) >= 2  # the shell and the sleeping interpreter
    killed = fence.kill_descendants(os.getpid())
    assert child.pid in killed
    assert len(killed) >= 2
    assert child.poll() is not None  # reaped by the fence or by poll
    assert fence.descendants(os.getpid()) == {}


def test_a_process_that_left_its_shell_is_still_found(subreaper: None, tmp_path: Path) -> None:
    """``nohup work &`` in a shell: the shell exits, and the work belongs to ``init``. A
    subreaper gets it, so the fence sees it."""
    pidfile = tmp_path / "pid"
    work = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)"
    script = f"nohup {sys.executable} -c {shlex.quote(work)} >/dev/null 2>&1 &"
    subprocess.run(["/bin/sh", "-c", script], check=True)
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.02)
    orphan = int(pidfile.read_text())
    assert orphan in fence.descendants(os.getpid())
    killed = fence.kill_descendants(os.getpid())
    assert orphan in killed
    assert wait_until_gone(orphan)


def test_a_trusted_process_stays_and_its_children_do_not(subreaper: None) -> None:
    trusted = subprocess.Popen(
        ["/bin/sh", "-c", f"{sys.executable} -c 'import time; time.sleep(60)' & exec sleep 60"]
    )
    time.sleep(0.3)
    killed = fence.kill_descendants(os.getpid(), trusted={trusted.pid})
    assert trusted.pid not in killed
    assert killed  # the child of the trusted process
    assert trusted.poll() is None
    running = {p for p, state in fence.descendants(os.getpid()).items() if state != "Z"}
    assert running == {trusted.pid}  # a zombie of the trusted parent is no process
    assert trusted.pid in fence.kill_all(os.getpid())
    assert fence.exited(trusted.pid)


def test_a_clean_tree_gives_an_empty_answer(subreaper: None) -> None:
    assert fence.kill_descendants(os.getpid()) == []


def test_a_process_that_will_not_die_raises(
    subreaper: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = subprocess.Popen(SLEEP)
    try:
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        with pytest.raises(fence.FenceError):
            fence.kill_descendants(os.getpid(), timeout_s=0.2)
    finally:
        monkeypatch.undo()
        child.kill()
        child.wait()


def test_exited_is_true_for_a_zombie_and_for_a_gone_process() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    time.sleep(0.3)  # it ended, and nobody waited: a zombie
    assert fence.exited(child.pid)
    child.wait()
    assert fence.exited(child.pid)
    assert not fence.exited(os.getpid())


def test_the_process_table_reads_names_with_spaces_and_brackets(tmp_path: Path) -> None:
    script = tmp_path / "x (y) z"
    script.write_text("import time; time.sleep(60)\n")
    # A process name of ``python3 (x)`` must not confuse the parser of the stat line.
    child = subprocess.Popen(
        ["/bin/sh", "-c", textwrap.dedent(f"exec -a 'a (b) c' {sys.executable} '{script}'")]
    )
    try:
        time.sleep(0.3)
        assert child.pid in fence.descendants(os.getpid())
    finally:
        child.kill()
        child.wait()
