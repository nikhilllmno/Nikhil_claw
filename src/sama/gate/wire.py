"""The wire form of the gate endpoint ``POST /gate`` of the sama tool server
(docs/architecture.md, Section 16.1). The runner in the sandbox sends ``GateRequest`` with the
run token. The control plane answers with ``GateAnswer``."""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from sama.gate.decide import Action

GATE_PATH: Final = "/gate"
ASK_PATH: Final = "/gate/ask"
"""The questions of the native ``AskUserQuestion`` tool of the Claude CLI (M8.3, Section 16.7)."""
MAX_GATE_BODY_BYTES: Final = 1_000_000
STEER_HEADER: Final = "x-sama-steer"
"""A runner that sends this header with the value ``1`` can take the notes of ``/steer`` in
``GateAnswer.steer`` (Section 4.3). The control plane does not send ``steer`` to a runner
without it, because an old runner refuses an unknown key. A header, not a body key: an old
control plane refuses an unknown body key (422), but it ignores a header."""


class GateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)
    child: bool = False
    """True for a call of a sub-agent. It cannot ask a human, so ``ask`` becomes ``deny`` and
    the gate records no approval."""


class GateAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Action
    reason: str = ""
    step: str = ""
    steer: str = ""
    """The notes of ``/steer`` that the run did not read yet (Section 4.3). The runtime gives
    them to the model with the verdict. On the wire only when it is not empty."""

    def wire(self) -> dict[str, object]:
        """The JSON body. ``steer`` is in it only when it is not empty."""
        return self.model_dump(exclude=None if self.steer else {"steer"})


class AskRequest(BaseModel):
    """``POST /gate/ask``: the input of one ``AskUserQuestion`` call, as the CLI gives it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AskAnswer(BaseModel):
    """The answers by question text, or ``None`` when nobody answered in time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answers: dict[str, str] | None = None
    error: str = ""
