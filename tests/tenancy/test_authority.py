"""Helpers of the live authority of a run (docs/architecture.md, Section 5.1)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from sama.tenancy.authority import _cost, _tokens, budget_problem, lower


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [(None, None, None), (5, None, 5), (None, 7, 7), (5, 7, 5), (9, 7, 7)],
)
def test_lower_takes_the_lower_limit(first: Any, second: Any, expected: Any) -> None:
    assert lower(first, second) == expected


@pytest.mark.parametrize(
    ("budget", "expected"),
    [({"max_tokens": 10}, 10), ({"max_tokens": 0}, None), ({"max_tokens": True}, None)],
)
def test_tokens_reads_a_positive_integer(budget: Any, expected: int | None) -> None:
    assert _tokens(budget) == expected


@pytest.mark.parametrize("budget", [None, [], {}, {"max_tokens": "10"}, {"max_tokens": -1}])
def test_tokens_ignores_other_values(budget: Any) -> None:
    assert _tokens(budget) is None


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        ({"max_cost_usd": "0.5"}, Decimal("0.5")),
        ({"max_cost_usd": 2}, Decimal(2)),
        ({"max_cost_usd": 0}, None),
        ({"max_cost_usd": "x"}, None),
        ({"max_cost_usd": "NaN"}, None),
        ({"max_cost_usd": "Infinity"}, None),
        ({"max_cost_usd": False}, None),
        ({}, None),
        (None, None),
    ],
)
def test_cost_reads_a_positive_finite_number(budget: Any, expected: Decimal | None) -> None:
    assert _cost(budget) == expected


def test_budget_problem() -> None:
    assert budget_problem(10, Decimal(1), None, None) is None
    assert budget_problem(9, Decimal(1), 10, Decimal(2)) is None
    assert budget_problem(10, Decimal(1), 10, None) == "the run reached max_tokens"
    assert budget_problem(1, Decimal(2), None, Decimal(2)) == "the run reached max_cost_usd"
