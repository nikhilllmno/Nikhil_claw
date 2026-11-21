"""The limits of ``browser_task`` (docs/architecture.md, Section 14.1; roadmap M5.6).

A module without other sama imports, so the tool server can read the limits when it builds the
tool schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sama.browser.target import BrowserError

DEFAULT_MAX_STEPS: Final = 15
MAX_STEPS: Final = 40
DEFAULT_MAX_COST_USD: Final = Decimal("0.50")
MAX_COST_USD: Final = Decimal("2.00")
DEFAULT_TIMEOUT_S: Final = 300
MAX_TIMEOUT_S: Final = 600
MIN_TIMEOUT_S: Final = 30
MAX_GOAL_CHARS: Final = 4_000


@dataclass(frozen=True, slots=True)
class TaskBudget:
    max_steps: int = DEFAULT_MAX_STEPS
    max_cost_usd: Decimal = DEFAULT_MAX_COST_USD
    timeout_s: float = DEFAULT_TIMEOUT_S


def parse_budget(
    max_steps: int | None, max_cost_usd: float | Decimal | None, timeout_s: float | None
) -> TaskBudget:
    """A budget within the limits. ``None`` takes the default."""
    steps = DEFAULT_MAX_STEPS if max_steps is None else max_steps
    cost = DEFAULT_MAX_COST_USD if max_cost_usd is None else Decimal(str(max_cost_usd))
    timeout = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    if not 1 <= steps <= MAX_STEPS:
        raise BrowserError(f"max_steps must be 1 to {MAX_STEPS}")
    if not Decimal("0.01") <= cost <= MAX_COST_USD:
        raise BrowserError(f"max_cost_usd must be 0.01 to {MAX_COST_USD}")
    if not MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S:
        raise BrowserError(f"timeout_s must be {MIN_TIMEOUT_S} to {MAX_TIMEOUT_S}")
    return TaskBudget(max_steps=steps, max_cost_usd=cost, timeout_s=float(timeout))
