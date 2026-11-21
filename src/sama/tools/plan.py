"""``plan_propose``: the agent proposes a plan in coding mode (docs/architecture.md, Section
15.5).

The tool stores the plan as a new version and turns plan mode on. A person approves the plan,
also with an edited text, or rejects it, in the web app or with ``/plan`` in a chat. The tool
does not wait: the agent ends its answer, and the approval sends the plan back as a message.

The store is ``sama.coding_mode.tool.PlanTool``. This module has only the contract, so the tool
server does not import the coding package.
"""

from __future__ import annotations

from typing import Final, Protocol

from sama.tenancy.run_token import RunTokenClaims

PLAN_PROPOSE: Final = "plan_propose"
MAX_TITLE_CHARS: Final = 200


class PlanError(Exception):
    """The message is for the agent."""


class PlanProposer(Protocol):
    async def propose(self, claims: RunTokenClaims, plan: str, title: str | None) -> str:
        """Stores the plan and returns the tool result. Raises ``PlanError``."""
        ...
