"""A live Claude run through the model proxy (docs/architecture.md, Section 16.6).

The runner starts in a local sandbox with only the run token and the proxy URL. The proxy adds
the real Anthropic key. Runs only with ``SAMA_LIVE_TESTS=1`` and ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import os
import secrets
import sys
import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr

from sama.modelproxy import MemoryUsageStore, ModelProxy, PriceTable, ProxySettings
from sama.orchestrator.sandboxed import SandboxedRuntime, SandboxSettings
from sama.runtime import RunCompleted, RunConfig, RunRequest, RuntimeKind
from sama.sandbox.local import LocalProvider
from sama.tenancy import RunTokenSigner
from tests.conformance.scripted_model import serve
from tests.conformance.targets import LIVE_MODEL
from tests.live_budget import LIVE_SPEND
from tests.support import collect, make_scope

LIVE = os.environ.get("SAMA_LIVE_TESTS") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="set SAMA_LIVE_TESTS=1 and ANTHROPIC_API_KEY"),
]


async def test_a_live_run_goes_through_the_proxy(tmp_path: Path) -> None:
    signer = RunTokenSigner(secrets.token_bytes(32))
    store = MemoryUsageStore()
    proxy = ModelProxy(
        signer=signer,
        store=store,
        prices=PriceTable.load(),
        settings=ProxySettings(anthropic_key=SecretStr(os.environ["ANTHROPIC_API_KEY"])),
    )
    word = f"teal{uuid.uuid4().hex[:6]}"
    scope = make_scope(f"run-{uuid.uuid4().hex[:8]}")
    config = RunConfig(
        runtime=RuntimeKind.CLAUDE_AGENT_SDK,
        model=LIVE_MODEL,
        soul="You are a test agent. Follow the user's instructions exactly.",
    )
    with serve(proxy.app()) as url:
        runtime = SandboxedRuntime(
            kind=RuntimeKind.CLAUDE_AGENT_SDK,
            provider=LocalProvider(tmp_path / "sandboxes"),
            signer=signer,
            settings=SandboxSettings(
                template="live",
                model_url=url,
                runner_argv=(sys.executable, "-m", "sama.runtime.runner"),
            ),
        )
        request = RunRequest(
            scope=scope,
            input=f"Reply with exactly this word and nothing else: {word}",
            config=config,
            budget=LIVE_SPEND.budget(),
        )
        try:
            events = await collect(runtime.run(request))
        finally:
            await runtime.close_sandboxes(kill=True)
    LIVE_SPEND.record(events)
    done = events[-1]
    assert isinstance(done, RunCompleted), done
    assert word in done.output
    calls = [c for c in store.calls if c.run_id == scope.run_id]
    assert calls
    assert all(c.status_code == 200 for c in calls)
    assert sum(c.counts.output for c in calls) == done.usage.output_tokens
    proxy_cost = sum(c.cost_usd for c in calls)
    assert proxy_cost > 0
    # The CLI computes its own cost. The proxy record is the source of truth, and they agree.
    assert abs(proxy_cost - done.usage.cost_usd) <= proxy_cost / 100
