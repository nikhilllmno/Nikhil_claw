"""The retention rule of each plan: the seam for the plans of M6.6."""

from __future__ import annotations

import pytest

from sama.datacontrols import retention
from sama.datacontrols.retention import (
    PlanRetention,
    RetentionError,
    check_days,
    effective_days,
    plan_retention,
)


@pytest.fixture
def plans(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        retention,
        "PLAN_RETENTION",
        {"free": PlanRetention(default_days=30, max_days=90), "pro": PlanRetention(max_days=365)},
    )


def test_a_plan_without_a_rule_keeps_everything() -> None:
    assert plan_retention("team") == PlanRetention()
    assert effective_days("team", None) is None
    assert effective_days("team", 10) == 10


@pytest.mark.usefixtures("plans")
def test_the_plan_default_and_maximum_apply() -> None:
    assert effective_days("free", None) == 30
    assert effective_days("free", 7) == 7
    assert effective_days("free", 400) == 90
    assert effective_days("pro", None) == 365


@pytest.mark.usefixtures("plans")
def test_check_days_refuses_a_value_above_the_plan_maximum() -> None:
    check_days("free", None)
    check_days("free", 90)
    with pytest.raises(RetentionError, match="at most 90"):
        check_days("free", 91)


@pytest.mark.parametrize("days", [0, -1, 3651])
def test_check_days_refuses_a_value_out_of_range(days: int) -> None:
    with pytest.raises(RetentionError):
        check_days("team", days)
