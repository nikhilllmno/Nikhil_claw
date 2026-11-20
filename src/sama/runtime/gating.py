"""The tool gate seam of the runtime adapters (docs/architecture.md, Section 16.1).

Every adapter asks a ``ToolGate`` before a tool call runs. The module has no SDK imports, so
the runner, the remote gate, and each adapter can use it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sama.runtime.contract import RunScope

__all__ = ["AllowAllGate", "GateDecision", "ToolGate"]


@dataclass(frozen=True, slots=True)
class GateDecision:
    """The answer of the tool gate for one tool call."""

    action: Literal["allow", "deny", "ask"]
    reason: str = ""


class ToolGate(Protocol):
    """Decides each tool call (architecture Section 16.1). ``call_id`` is the tool use id. In
    the sandbox the runner uses ``RemoteGate``, which asks the ``GateService`` of the control
    plane.

    ``child`` is true for a call of a sub-agent. A sub-agent cannot pause the run, so the gate
    must not answer ``ask`` for it, and it must not record an approval for it: it gives ``deny``
    instead."""

    async def check(
        self,
        scope: RunScope,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        child: bool = False,
    ) -> GateDecision: ...


class AllowAllGate:
    """Allows every tool call. Only for tests and local development."""

    async def check(
        self,
        scope: RunScope,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        child: bool = False,
    ) -> GateDecision:
        return GateDecision("allow")
