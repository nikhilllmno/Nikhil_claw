"""The M1.17 acceptance test (docs/roadmap.md): a planted secret does not appear in a
transcript, a stored tool call, an approval, a trace, or a log.

A secret is stored in the vault of an org. The user sends it, and a fake runtime puts it in
every event: a text delta, the arguments of a tool call, the output of the tool call, an
approval request, and the final reply. The gate records the tool call with the secret in its
arguments. The trace exporter sends the run to a fake Langfuse.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.gate.service import GateService
from sama.observability.logs import configure_logging
from sama.runtime.contract import (
    ApprovalRequired,
    RunCompleted,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    Usage,
)
from sama.runtime.fake import FakeScript
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.redact import REDACTED, default_redactor
from sama.vault.store import SecretName, Vault
from tests.observability.test_exporter import Langfuse
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness

TABLES = ("messages", "run_events", "tool_calls", "approvals", "runs", "audit_log")


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    out = io.StringIO()
    monkeypatch.delenv("SAMA_LOG_FORMAT", raising=False)
    monkeypatch.setenv("SAMA_LOG_LEVEL", "debug")
    configure_logging("test", stream=out)
    yield out
    root.handlers[:] = handlers
    root.setLevel(level)


def script(secret: str) -> FakeScript:
    usage = Usage(input_tokens=1)
    return FakeScript(
        events=(
            TextDelta(text=f"I will use {secret} now", usage=usage),
            ToolCallStarted(
                call_id="call-1",
                name="Bash",
                arguments={"command": f"curl -H 'X-Subscription-Token: {secret}' x"},
                usage=usage,
            ),
            ToolCallFinished(call_id="call-1", output_preview=f"token={secret}", usage=usage),
            ApprovalRequired(
                approval_id="call-2",
                call_id="call-2",
                tool_name="Bash",
                arguments={"command": f"export KEY={secret}"},
                reason=f"it sets {secret}",
                runtime_state=b"state",
                usage=usage,
            ),
        ),
        on_approve=(RunCompleted(output=f"done with {secret}", usage=Usage(input_tokens=2)),),
        on_reject=(RunCompleted(output="stopped", usage=Usage(input_tokens=2)),),
    )


async def test_a_planted_secret_stays_out_of_the_transcript_the_trace_and_the_logs(
    engine: AsyncEngine, tenant: Tenant, logs: io.StringIO
) -> None:
    secret = f"planted-{uuid.uuid4().hex}"
    vault = Vault(engine, VaultKey(b"a" * KEY_BYTES))
    await vault.put(tenant.org, SecretName.BRAVE_API_KEY, secret)
    try:
        harness = Harness(engine)
        harness.runtime.add_script(script(secret))
        run_id = await harness.send(tenant, "s1", f"please use the key {secret}")
        assert await harness.worker.run_once()
        assert await harness.status_is(run_id, "paused")

        claims = RunTokenClaims(
            org_id=str(tenant.org),
            workspace_id=str(tenant.ids["ws"]),
            session_id=str(tenant.ids["s1"]),
            run_id=str(run_id),
            user_id=str(tenant.ids["mia"]),
            token_id="t-1",
            expires_at=2_000_000_000,
        )
        gate = GateService(engine)
        await gate.check(claims, "call-1", "Bash", {"command": f"echo {secret}", secret: True})
        await gate.decide_approval(
            org_id=tenant.org,
            run_id=run_id,
            call_id="call-2",
            user_id=tenant.ids["mia"],
            approve=True,
        )
        await harness.worker.maintain()
        assert await harness.worker.run_once()
        assert await harness.status_is(run_id, "completed")
        logging.getLogger("sama.test").warning("a careless log of %s", secret)

        langfuse = Langfuse()
        assert await langfuse.exporter(engine).export_pending() >= 1
    finally:
        default_redactor().set_vault_values([])

    stored = await _stored(engine, tenant.org)
    assert REDACTED in stored
    assert secret not in stored
    trace = json.dumps(langfuse.traces)
    assert run_id.hex in trace
    assert REDACTED in trace
    assert secret not in trace
    assert REDACTED in logs.getvalue()
    assert secret not in logs.getvalue()


async def _stored(engine: AsyncEngine, org_id: uuid.UUID) -> str:
    rows: list[str] = []
    async with engine.connect() as connection:
        for table in TABLES:
            result = await connection.execute(
                text(f"SELECT row_to_json(t)::text FROM {table} t WHERE org_id = :org"),  # noqa: S608
                {"org": org_id},
            )
            found = result.scalars().all()
            assert found, f"{table} has rows"
            rows += found
    return "\n".join(rows)
