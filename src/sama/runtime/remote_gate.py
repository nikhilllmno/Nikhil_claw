"""The tool gate of the runner: it asks the control plane for each call
(docs/architecture.md, Section 16.1).

The runner holds only the run token, so the gate context (toolsets, policy, approvals) stays on
the control plane. If the control plane does not answer, the adapter denies the call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import httpx

from sama.gate.wire import GateAnswer, GateRequest
from sama.runtime.claude import GateDecision
from sama.runtime.contract import RunScope

GATE_TIMEOUT_S: Final = 30.0


class RemoteGate:
    """A ``ToolGate`` that sends each call to ``POST <url>`` with the run token."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_s: float = GATE_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._timeout_s = timeout_s
        self._transport = transport

    def __repr__(self) -> str:
        return f"RemoteGate(url={self._url!r}, token=<hidden>)"

    async def check(
        self,
        scope: RunScope,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        child: bool = False,
    ) -> GateDecision:
        body = GateRequest(
            call_id=call_id, tool_name=tool_name, arguments=dict(arguments), child=child
        )
        async with httpx.AsyncClient(
            timeout=self._timeout_s, transport=self._transport, follow_redirects=False
        ) as client:
            response = await client.post(
                self._url,
                content=body.model_dump_json(),
                headers={
                    "authorization": f"Bearer {self._token}",
                    "content-type": "application/json",
                },
            )
        response.raise_for_status()
        answer = GateAnswer.model_validate_json(response.content)
        return GateDecision(answer.action, answer.reason)
