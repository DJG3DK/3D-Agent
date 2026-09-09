"""The resume endpoint's top-up bound. The dashboard only shows the add-budget
field when a task is nearly out of money and sends 0 otherwise, so 0 must be
accepted -- rejecting it stranded every merge-failure and Stop resume on a 400
with nothing on screen to change (2026-09-09)."""
import pytest
from fastapi import HTTPException

from agent.server import _MAX_BUDGET_TOPUP_USD, _check_budget_topup


def test_zero_topup_is_allowed():
    _check_budget_topup(0)
    _check_budget_topup(0.0)


def test_positive_topups_up_to_the_ceiling_are_allowed():
    _check_budget_topup(2.0)
    _check_budget_topup(_MAX_BUDGET_TOPUP_USD)


@pytest.mark.parametrize("delta", [-0.01, -5, _MAX_BUDGET_TOPUP_USD + 0.01, 10_000])
def test_negative_or_oversized_topups_are_rejected(delta):
    with pytest.raises(HTTPException) as exc:
        _check_budget_topup(delta)
    assert exc.value.status_code == 400
    assert "between 0 and" in exc.value.detail
