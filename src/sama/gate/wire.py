"""The wire form of the gate endpoint ``POST /gate`` of the sama tool server
(docs/architecture.md, Section 16.1). The runner in the sandbox sends ``GateRequest`` with the
run token. The control plane answers with ``GateAnswer``."""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

GATE_PATH: Final = "/gate"
MAX_GATE_BODY_BYTES: Final = 1_000_000


class GateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


class GateAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["allow", "ask", "deny"]
    reason: str = ""
    step: str = ""
