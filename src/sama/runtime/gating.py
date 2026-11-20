"""The tool gate seam of the runtime adapters (docs/architecture.md, Section 16.1).

Every adapter asks a ``ToolGate`` before a tool call runs. The module has no SDK imports, so
the runner, the remote gate, and each adapter can use it.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from sama.runtime.contract import RunScope

__all__ = ["AllowAllGate", "BoundedSet", "GateDecision", "ToolGate"]

MEMORY_SIZE: Final = 10_000
"""How many paused runs, used approvals, and cancelled runs an adapter remembers."""


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


class BoundedSet:
    """Remembers the latest keys, up to a size. Old keys drop out first. The adapters use it
    for the paused runs, the used approvals, and the cancelled runs."""

    def __init__(self, size: int = MEMORY_SIZE) -> None:
        self._size = size
        self._keys: OrderedDict[str, None] = OrderedDict()

    def add(self, key: str) -> None:
        self._keys[key] = None
        self._keys.move_to_end(key)
        while len(self._keys) > self._size:
            self._keys.popitem(last=False)

    def discard(self, key: str) -> None:
        self._keys.pop(key, None)

    def __contains__(self, key: object) -> bool:
        return key in self._keys
