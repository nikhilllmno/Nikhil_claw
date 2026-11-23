"""The kept runtime (docs/architecture.md, Section 4.4), end to end.

These tests run only on the warm targets: the runner of the first run stays alive as a host,
and the next run of the session uses the CLI (Claude) or the MCP connections (OpenAI) that it
holds. The scripted model and the real model proxy are in the loop, so the tests also show that
each run uses its own run token.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from sama.modelproxy.store import MemoryUsageStore
from sama.orchestrator.sandboxed import SandboxedRuntime
from sama.runtime import FailureCode, RunFailed, RunScope, TextDelta
from sama.tenancy import InvalidRunTokenError
from sama.tenancy.authority import Authority
from tests.conformance.targets import Scenario, Target, new_word
from tests.conformance.test_conformance import completed, request, run, scope
from tests.support import collect

pytestmark = pytest.mark.usefixtures("warm_only")

TIMING = re.compile(r"runner timing mode=(\w+) (?P<rest>.*) run=(?P<run>\S+)")


@pytest.fixture
async def warm_only(target: Target, warm_server_socket: str) -> None:
    """Starts the warm server first, as the warm call of the web app does (Section 4.4), so the
    first run of a test is a warm run and can become a host."""
    if target.name not in {"claude-warm", "openai-warm"}:
        pytest.skip("only the warm targets keep a runtime")
    probe = scope()
    runtime = target.runtime
    assert isinstance(runtime, SandboxedRuntime)
    await runtime.warm(probe.org_id, probe.session_id)
    for _ in range(100):
        if await asyncio.to_thread(os.path.exists, warm_server_socket):
            return
        await asyncio.sleep(0.1)
    raise AssertionError("the warm server did not start")


def kept_step(target: Target) -> str:
    return "client_kept_s" if target.name == "claude-warm" else "mcp_kept_s"


async def timing_of(caplog: pytest.LogCaptureFixture, run_id: str) -> tuple[str, str]:
    """The mode and the steps of the timing line of a run. The orchestrator logs it after the
    run ended, so this waits for it."""
    for _ in range(100):
        for record in caplog.records:
            found = TIMING.search(record.getMessage())
            if found and found["run"] == run_id:
                return found[1], found["rest"]
        await asyncio.sleep(0.1)
    raise AssertionError(f"no timing line for run {run_id}")


def end_run(target: Target, run_scope: RunScope) -> None:
    """The orchestrator ends a run. From then on, the model proxy refuses its token."""
    store = target.model_calls
    assert isinstance(store, MemoryUsageStore)
    store.authorities[run_scope.run_id] = Authority(problem="the run ended")


async def test_a_follow_up_uses_the_kept_runtime_and_the_token_of_its_own_run(
    target: Target, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sama.orchestrator")
    word = new_word()
    first_scope, second_scope = scope(), scope()
    first = completed(
        await run(target, target.prompt(Scenario.REMEMBER, word), run_scope=first_scope)
    )
    assert first.runtime_state is not None
    end_run(target, first_scope)
    second = completed(
        await run(
            target,
            target.prompt(Scenario.RECALL),
            run_scope=second_scope,
            state=first.runtime_state,
        )
    )
    # The model has the history of the first run, and the proxy took the calls of the second
    # run with the token of the second run: the token of the first run is refused.
    assert word in second.output
    store = target.model_calls
    assert isinstance(store, MemoryUsageStore)
    assert {call.run_id for call in store.calls} >= {first_scope.run_id, second_scope.run_id}
    assert all(
        call.status_code == 200 for call in store.calls if call.run_id == second_scope.run_id
    )
    mode, rest = await timing_of(caplog, second_scope.run_id)
    assert mode == "host"
    assert kept_step(target) in rest


async def test_a_new_config_starts_a_new_process(
    target: Target, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sama.orchestrator")
    first = completed(await run(target, target.prompt(Scenario.REMEMBER, new_word())))
    assert first.runtime_state is not None
    changed = target.config.model_copy(update={"soul": "You are another agent."})
    second_scope = scope()
    second_request = request(
        target, target.prompt(Scenario.RECALL), run_scope=second_scope, state=first.runtime_state
    ).model_copy(update={"config": changed})
    completed(await collect(target.runtime.run(second_request)))
    _, rest = await timing_of(caplog, second_scope.run_id)
    assert kept_step(target) not in rest


async def test_a_cancel_stops_a_run_in_the_host_and_the_next_run_works(
    target: Target, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sama.orchestrator")
    # Make sure that a host holds a runtime now.
    seed = completed(await run(target, target.prompt(Scenario.REMEMBER, new_word())))
    slow_scope = scope()
    stream = target.runtime.run(
        request(
            target, target.prompt(Scenario.SLOW), run_scope=slow_scope, state=seed.runtime_state
        )
    )
    events = []
    async for event in stream:
        events.append(event)
        if isinstance(event, TextDelta):
            break
    loop = asyncio.get_running_loop()
    started = loop.time()
    await target.runtime.cancel(slow_scope.run_id)
    async for event in stream:
        events.append(event)
    assert loop.time() - started < 10.0
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED
    word = new_word()
    again = completed(await run(target, target.prompt(Scenario.REMEMBER, word)))
    assert again.output


def cli_processes() -> Iterator[Path]:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            if b"stream-json" in (entry / "cmdline").read_bytes():
                yield entry


async def test_the_kept_cli_holds_no_run_token(target: Target) -> None:
    if target.name != "claude-warm":
        pytest.skip("only the Claude runtime has a CLI process")
    completed(await run(target, target.prompt(Scenario.REMEMBER, new_word())))
    found = 0
    for entry in cli_processes():
        with contextlib.suppress(OSError):
            environ = dict(
                item.split("=", 1)
                for item in (entry / "environ").read_bytes().decode().split("\0")
                if "=" in item
            )
            found += 1
            assert environ.get("SAMA_RUN_TOKEN", "") == ""
            with pytest.raises(InvalidRunTokenError):
                target.tools.run_tokens.verify(environ.get("ANTHROPIC_API_KEY", ""))
            assert os.environ.get("SAMA_RUN_TOKEN") is None
    assert found >= 1
