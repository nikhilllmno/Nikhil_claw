"""The tool gate of the runner: it asks the control plane for each call
(docs/architecture.md, Section 16.1).

The runner holds only the run token, so the gate context (toolsets, policy, approvals) stays on
the control plane. If the control plane does not answer, the adapter denies the call.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

import httpx

from sama.gate.wire import ASK_PATH, GATE_PATH, STEER_HEADER, AskAnswer, GateAnswer, GateRequest
from sama.runtime.contract import RunScope
from sama.runtime.gating import GateDecision

GATE_TIMEOUT_S: Final = 30.0
ASK_TIMEOUT_S: Final = 900.0
"""A question waits for the user up to 600 seconds on the control plane (``ClarifyBroker``)."""


class AskError(RuntimeError):
    """The control plane did not ask the questions. The message is for the model."""


class RemoteGate:
    """A ``ToolGate`` that sends each call to ``POST <url>`` with the run token."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_s: float = GATE_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
        steer: bool = True,
    ) -> None:
        """``steer`` asks the control plane for the notes of ``/steer``. Only a runtime with
        ``Capabilities.mid_run_steer`` sets it: the control plane marks the notes it sends as
        delivered, so a runtime that cannot give them to the model must not take them."""
        self._url = url
        self._token = token
        self._timeout_s = timeout_s
        self._transport = transport
        self._steer = steer

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
        # ``child`` goes on the wire only when it is true. A control plane from before M2.7
        # refuses an unknown key (422), so a parent call must keep the old body.
        content = body.model_dump_json(exclude=None if child else {"child"})
        async with httpx.AsyncClient(
            timeout=self._timeout_s, transport=self._transport, follow_redirects=False
        ) as client:
            headers = {
                "authorization": f"Bearer {self._token}",
                "content-type": "application/json",
            }
            if self._steer:
                headers[STEER_HEADER] = "1"
            response = await client.post(self._url, content=content, headers=headers)
        response.raise_for_status()
        answer = GateAnswer.model_validate_json(response.content)
        return GateDecision(answer.action, answer.reason, steer=answer.steer)

    async def ask(
        self, scope: RunScope, arguments: Mapping[str, Any], *, call_id: str
    ) -> dict[str, str] | None:
        """Asks the user the questions of one ``AskUserQuestion`` call (``POST /gate/ask``) and
        waits for the answers (M8.3). ``None`` when nobody answered in time."""
        url = self._url
        if url.endswith(GATE_PATH):
            url = url[: -len(GATE_PATH)] + ASK_PATH
        else:
            url = url.rstrip("/") + "/ask"
        content = json.dumps({"call_id": call_id, "arguments": dict(arguments)})
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(ASK_TIMEOUT_S, connect=10.0),
            transport=self._transport,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                url,
                content=content,
                headers={
                    "authorization": f"Bearer {self._token}",
                    "content-type": "application/json",
                },
            )
        try:
            answer = AskAnswer.model_validate_json(response.content)
        except ValueError:
            response.raise_for_status()
            raise AskError("the question server gave an answer that is not valid") from None
        if response.is_error:
            raise AskError(answer.error or f"the questions were not asked ({response.status_code})")
        return answer.answers
