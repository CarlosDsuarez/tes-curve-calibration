"""Day count conventions.

Skeleton: the assertions below are the contract the Phase 3 implementation has
to satisfy. They are marked ``xfail(raises=NotImplementedError)`` so the suite is
green today and turns red the moment an implementation lands that does not meet
the contract.
"""

from __future__ import annotations

from datetime import date

import pytest

from tes_pricer.math.day_count import DayCount, year_fraction

pytestmark = pytest.mark.unit


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 3", strict=True)
def test_act_365_full_year() -> None:
    """A non-leap calendar year is exactly 1.0 under ACT/365."""
    assert year_fraction(date(2025, 1, 1), date(2026, 1, 1), DayCount.ACT_365) == 1.0


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 3", strict=True)
def test_act_360_full_year_exceeds_one() -> None:
    """365 actual days over a 360-day basis is greater than one."""
    assert year_fraction(date(2025, 1, 1), date(2026, 1, 1), DayCount.ACT_360) > 1.0


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 3", strict=True)
def test_zero_length_period() -> None:
    """A zero-length accrual is zero under every convention."""
    day = date(2026, 3, 16)
    assert year_fraction(day, day, DayCount.ACT_365) == 0.0


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 3", strict=True)
def test_reversed_period_is_negative() -> None:
    """Reversing the endpoints flips the sign; it must not raise."""
    assert year_fraction(date(2026, 1, 1), date(2025, 1, 1), DayCount.ACT_365) < 0.0
