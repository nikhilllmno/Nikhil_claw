"""A time limit for each test.

A test that waits for ever (for example on a lock that a leaked transaction holds, or on an
event that no task sets) stops the whole CI job without any output until the job timeout. This
guard stops such a test after ``SAMA_TEST_TIMEOUT_S`` seconds (default 120, ``0`` turns it
off). The failure shows the stack of each thread and of each asyncio task, so the report tells
where the test waits. If the clean-up after the failure also waits, the guard prints the stacks
again and ends the process after ``GRACE_S`` seconds.

pytest's ``faulthandler_timeout`` (pyproject.toml) is the second guard: it prints the stacks of
the threads also when the main thread is in a C call that does not give control to Python.

The file also puts back the home regions of the process after each test (``home_regions``,
Section 17.3): ``controlplane.build`` and the region tests change them.
"""

from __future__ import annotations

import asyncio
import io
import os
import signal
import sys
import threading
import traceback
from collections.abc import Generator, Iterator
from types import FrameType

import pytest

from sama.tenancy.regions import home_regions, set_home_regions

DEFAULT_TIMEOUT_S = 120.0
LIVE_TIMEOUT_S = 900.0
GRACE_S = float(os.environ.get("SAMA_TEST_GRACE_S", "30"))


class TimeLimitError(BaseException):
    """A test ran longer than its time limit.

    A ``BaseException``, so that ``except Exception`` in the code under test does not catch it.
    """


def _timeout_s(item: pytest.Item) -> float:
    text = os.environ.get("SAMA_TEST_TIMEOUT_S", "").strip()
    if text:
        return float(text)
    return LIVE_TIMEOUT_S if item.get_closest_marker("live") else DEFAULT_TIMEOUT_S


def stacks() -> str:
    """The stacks of all threads and of all tasks of the running event loop."""
    out = io.StringIO()
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    for ident, frame in sys._current_frames().items():
        out.write(f"\n--- thread {names.get(ident, ident)} ---\n")
        out.write("".join(traceback.format_stack(frame, limit=25)))
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        out.write("\n(no running event loop)\n")
    else:
        for task in asyncio.all_tasks(loop):
            out.write(f"\n--- task {task.get_name()} ---\n")
            task.print_stack(file=out)
    return out.getvalue()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None]:
    timeout_s = _timeout_s(item)
    if (
        timeout_s <= 0
        or not hasattr(signal, "SIGALRM")
        or threading.current_thread() is not threading.main_thread()
    ):
        return (yield)
    fired = False

    def on_alarm(signum: int, frame: FrameType | None) -> None:
        nonlocal fired
        if not fired:
            fired = True
            # The clean-up after the failure gets a short time too.
            signal.setitimer(signal.ITIMER_REAL, GRACE_S)
            raise TimeLimitError(
                f"{item.nodeid} ran longer than {timeout_s:g} s "
                f"(SAMA_TEST_TIMEOUT_S). Stacks:\n{stacks()}"
            )
        capture = item.config.pluginmanager.getplugin("capturemanager")
        if capture is not None:
            capture.suspend_global_capture(in_=True)
        sys.stderr.write(
            f"\n{item.nodeid}: the clean-up after the timeout did not end in {GRACE_S:g} s. "
            f"Stacks:\n{stacks()}\n"
        )
        sys.stderr.flush()
        os._exit(1)

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        return (yield)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture(autouse=True)
def _home_regions() -> Iterator[None]:
    before = home_regions()
    yield
    set_home_regions(before)
