"""A shared cost cap for all live tests in one pytest process.

Each live run gets ``max_cost_usd`` equal to the rest of the cap, and its cost is added to the
spend when it ends. The Claude CLI checks its budget after each model turn, so the total can
pass the cap by at most one turn. When the cap is used up, the next live test fails before it
calls the model.

Set ``SAMA_LIVE_MAX_COST_USD`` to change the cap (default USD 0.50).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

import pytest

from sama.runtime import Budget, RunEvent, Usage

DEFAULT_CAP_USD = Decimal("0.50")


@dataclass
class LiveSpend:
    cap_usd: Decimal
    spent_usd: Decimal = Decimal(0)

    def remaining(self) -> Decimal:
        return self.cap_usd - self.spent_usd

    def budget(
        self,
        *,
        already_spent: Usage | None = None,
        max_turns: int | None = None,
        max_wall_time_s: float | None = 120,
    ) -> Budget:
        """A run budget with the rest of the cap.

        ``already_spent`` is the usage at a pause. A resume budget is for the whole run, so the
        cost before the pause is added back.
        """
        remaining = self.remaining()
        if remaining <= 0:
            pytest.fail(f"the live cost cap of USD {self.cap_usd} is used up")
        before = already_spent.cost_usd if already_spent is not None else Decimal(0)
        return Budget(
            max_cost_usd=before + remaining, max_turns=max_turns, max_wall_time_s=max_wall_time_s
        )

    def record(self, events: list[RunEvent], *, already_spent: Usage | None = None) -> None:
        """Adds the cost of one run stream. Usage is for the whole run, so a resume adds only
        the cost after the pause."""
        if not events:
            return
        before = already_spent.cost_usd if already_spent is not None else Decimal(0)
        self.spent_usd += max(Decimal(0), events[-1].usage.cost_usd - before)


LIVE_SPEND = LiveSpend(Decimal(os.environ.get("SAMA_LIVE_MAX_COST_USD", DEFAULT_CAP_USD)))
