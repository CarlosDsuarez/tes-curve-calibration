"""Day count conventions for TES (ACT/365 street convention) and IBR/OIS legs.

Colombian conventions worth spelling out, because they are the usual source of
a two-basis-point discrepancy against a vendor curve:

* **TES tasa fija** quote on an effective annual yield with ``ACT/365``.
* **IBR overnight** accrues ``ACT/360`` and compounds daily on the *nominal*
  rate, so an IBR OIS leg is not simply an ACT/365 discount factor.
* Coupon schedules follow the *modified following* business day rule against
  the Colombian holiday calendar, but the accrual itself is unadjusted.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum


class DayCount(StrEnum):
    """Supported accrual conventions."""

    ACT_365 = "ACT/365"
    ACT_360 = "ACT/360"
    ACT_ACT_ISDA = "ACT/ACT-ISDA"
    THIRTY_360 = "30/360"


class BusinessDayConvention(StrEnum):
    """Schedule roll rules applied to coupon and settlement dates."""

    UNADJUSTED = "UNADJUSTED"
    FOLLOWING = "FOLLOWING"
    MODIFIED_FOLLOWING = "MODIFIED_FOLLOWING"
    PRECEDING = "PRECEDING"


def year_fraction(start: date, end: date, convention: DayCount) -> float:
    """Return the accrual factor between ``start`` and ``end``.

    Args:
        start: Accrual start date, inclusive.
        end: Accrual end date, exclusive.
        convention: Day count convention to apply.

    Returns:
        The year fraction, negative when ``end`` precedes ``start``.
    """
    raise NotImplementedError("Phase 3: day count conventions")


def day_count_days(start: date, end: date, convention: DayCount) -> int:
    """Return the numerator of the accrual factor, in days."""
    raise NotImplementedError("Phase 3: day count conventions")


def is_business_day(day: date, holidays: frozenset[date]) -> bool:
    """Return whether ``day`` is a Colombian settlement business day."""
    raise NotImplementedError("Phase 3: Colombian holiday calendar")


def adjust(
    day: date,
    convention: BusinessDayConvention,
    holidays: frozenset[date],
) -> date:
    """Roll ``day`` onto a business day following ``convention``."""
    raise NotImplementedError("Phase 3: business day adjustment")


def add_business_days(day: date, offset: int, holidays: frozenset[date]) -> date:
    """Shift ``day`` by ``offset`` business days (T+n settlement)."""
    raise NotImplementedError("Phase 3: business day arithmetic")
