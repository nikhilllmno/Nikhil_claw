"""Live check of ``browser_task`` (M5.6). Run only with ``SAMA_LIVE_TESTS=1``.

It needs ``BROWSERBASE_API_KEY``, ``BROWSERBASE_PROJECT_ID``, and ``ANTHROPIC_API_KEY``. The
sub-agent calls a real model through the real model proxy in process (with an in-memory usage
store), and it drives a real Browserbase session. The test deletes the Browserbase context that
it made. Set ``SAMA_LIVE_BROWSER_TASK_MODEL`` to try another model.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from sama.browser.browserbase import API_URL, BrowserbaseTarget
from sama.browser.budget import TaskBudget
from sama.browser.contexts import MemoryContextStore
from sama.browser.guard import UrlGuard
from sama.browser.service import BrowserService
from sama.browser.task import BrowserTaskRunner, ProxyModelClient
from sama.modelproxy import MemoryUsageStore, ModelProxy, PriceTable, ProxySettings
from sama.runtime import Budget
from sama.runtime.contract import RunEvent, SubagentEvent
from sama.tenancy.run_token import RunTokenSigner
from tests.support import make_claims, make_scope

LIVE = os.environ.get("SAMA_LIVE_TESTS") == "1"
KEY = os.environ.get("BROWSERBASE_API_KEY", "")
PROJECT = os.environ.get("BROWSERBASE_PROJECT_ID", "")
ANTHROPIC = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("SAMA_LIVE_BROWSER_TASK_MODEL", "claude-sonnet-5-5")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="set SAMA_LIVE_TESTS=1"),
    pytest.mark.skipif(
        not (KEY and PROJECT and ANTHROPIC), reason="set the Browserbase and Anthropic keys"
    ),
]


class Sink:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def emit(self, org_id: str, run_id: str, event: RunEvent) -> bool:
        if isinstance(event, SubagentEvent):
            self.texts.append(event.text)
        return True


@dataclass
class Live:
    runner: BrowserTaskRunner
    service: BrowserService
    store: MemoryUsageStore
    signer: RunTokenSigner
    sink: Sink


@pytest.fixture
async def live() -> AsyncIterator[Live]:
    contexts = MemoryContextStore()
    target = BrowserbaseTarget(
        api_key=SecretStr(KEY), project_id=PROJECT, contexts=contexts, session_timeout_s=300
    )
    service = BrowserService(target, UrlGuard())
    store, signer, prices = MemoryUsageStore(), RunTokenSigner(os.urandom(32)), PriceTable.load()
    proxy = ModelProxy(
        signer=signer,
        store=store,
        prices=prices,
        settings=ProxySettings(anthropic_key=SecretStr(ANTHROPIC)),
    )
    sink = Sink()
    runner = BrowserTaskRunner(
        service, ProxyModelClient(app=proxy.app()), prices, model=MODEL, progress=sink
    )
    try:
        yield Live(runner, service, store, signer, sink)
    finally:
        await runner.aclose()
        await service.aclose()
        await proxy.aclose()
        async with httpx.AsyncClient(
            base_url=API_URL, headers={"X-BB-API-Key": KEY}, timeout=30
        ) as client:
            for context_id in contexts.rows.values():
                await client.delete(f"/v1/contexts/{context_id}")


async def test_a_real_sub_agent_reads_a_page_through_the_proxy(live: Live) -> None:
    scope = make_scope("run-live-task")
    token = live.signer.issue(scope, ttl_s=900, budget=Budget(max_cost_usd=Decimal(1)))
    text = await live.runner.run(
        make_claims("run-live-task"),
        token,
        "Open https://example.com and report the main heading of the page and the URL that "
        "its only link points to.",
        budget=TaskBudget(max_steps=8, timeout_s=240),
    )
    print(text)
    print("\n".join(live.sink.texts))
    assert text.startswith("browser_task done."), text
    assert "Example Domain" in text
    assert "iana.org" in text
    # Each model call went through the proxy and is recorded for the run.
    assert live.store.calls
    assert {call.run_id for call in live.store.calls} == {"run-live-task"}
    assert live.sink.texts[0].startswith("started:")
    assert any(t.startswith("step 1/8: open") for t in live.sink.texts)
    assert live.sink.texts[-1] == "ended: done"
    live.service.end_run("org-1", "run-live-task")
