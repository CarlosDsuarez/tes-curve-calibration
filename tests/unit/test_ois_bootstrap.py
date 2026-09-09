"""COP OIS discount curve bootstrapped from IBR overnight index swaps.

Two tiers, in the sense of :mod:`tests.unit.test_day_count`.

``OISQuote`` and ``DiscountCurve`` are real, constructible dataclasses today,
so they get ordinary assertions - the defaults in particular, because an IBR
OIS leg that silently picked up the ACT/365 street basis used for TES would
misprice every pillar and nothing else in the module would notice.

The five numerical entry points are Phase 7 stubs. Their tests are marked
``xfail(raises=NotImplementedError, strict=True)``: the suite is green today and
turns red the moment an implementation lands that does not meet the contract
written here. Every expectation below is an identity that follows from the
convention, not a number read back out of the implementation.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import numpy as np
import pytest

from tes_pricer.math.day_count import DayCount
from tes_pricer.math.ois_bootstrap import (
    DiscountCurve,
    OISQuote,
    bootstrap_ois_curve,
    compound_overnight,
)

pytestmark = pytest.mark.unit

REFERENCE_DATE = date(2026, 9, 8)

# Annual pillars, so a fixed leg paying once a year has its coupon dates on the
# curve's own pillars and the par identity below needs no interpolation.
# 2028 is a leap year, hence 366 days across the second period.
ANNUAL_MATURITIES = (date(2027, 9, 8), date(2028, 9, 8), date(2029, 9, 8))
ANNUAL_TENORS = ("1Y", "2Y", "3Y")
ANNUAL_PAR_RATES = (0.0950, 0.0975, 0.0990)

NO_HOLIDAYS: frozenset[date] = frozenset()

# A hand-built curve for the interpolation contracts. Times are ACT/360 year
# fractions and the discount factors are strictly decreasing, as any curve with
# positive forwards must be.
PILLAR_TIMES = np.array([1.0, 2.0, 3.0], dtype=np.float64)
PILLAR_DISCOUNTS = np.array([0.90, 0.81, 0.72], dtype=np.float64)


def annual_quotes() -> list[OISQuote]:
    """Three par IBR OIS quotes on annual pillars, sorted by maturity."""
    return [
        OISQuote(tenor=tenor, maturity_date=maturity, fixed_rate=rate)
        for tenor, maturity, rate in zip(
            ANNUAL_TENORS, ANNUAL_MATURITIES, ANNUAL_PAR_RATES, strict=True
        )
    ]


def hand_built_curve() -> DiscountCurve:
    """A ``DiscountCurve`` assembled directly, bypassing the bootstrap."""
    return DiscountCurve(
        reference_date=REFERENCE_DATE,
        pillar_times=PILLAR_TIMES,
        discount_factors=PILLAR_DISCOUNTS,
    )


def par_ois_rate(curve: DiscountCurve, coupon_times: np.ndarray) -> float:
    """Par fixed rate of an annual OIS on ``coupon_times``, off ``curve``.

    Single-curve COP: discounting and forecasting share the curve, so the
    compounded floating leg telescopes to ``DF(0) - DF(T) = 1 - DF(T)`` and the
    par rate is that over the fixed-leg annuity. This is the identity a
    bootstrap is *defined* by, which is why it is written out here rather than
    read off the module under test.
    """
    discounts = curve.discount(coupon_times)
    accruals = np.diff(np.concatenate(([0.0], coupon_times)))
    annuity = float(np.sum(accruals * discounts))
    return (1.0 - float(discounts[-1])) / annuity


# --------------------------------------------------------------------------- #
# 1. The dataclasses, which exist today
# --------------------------------------------------------------------------- #


def test_quote_defaults_to_an_annual_act_360_fixed_leg() -> None:
    """IBR accrues ACT/360; defaulting to the ACT/365 TES basis would misprice."""
    quote = OISQuote(tenor="1Y", maturity_date=ANNUAL_MATURITIES[0], fixed_rate=0.0950)
    assert quote.day_count == DayCount.ACT_360
    assert quote.fixed_frequency == 1


def test_quote_keeps_an_explicit_non_default_schedule() -> None:
    """A quarterly fixed leg must survive construction, not be normalised away."""
    quote = OISQuote(
        tenor="3M",
        maturity_date=date(2026, 12, 8),
        fixed_rate=0.0930,
        fixed_frequency=4,
        day_count=DayCount.ACT_365,
    )
    assert quote.fixed_frequency == 4
    assert quote.day_count == DayCount.ACT_365
    assert quote.fixed_rate == 0.0930


def test_quote_is_immutable() -> None:
    """A quote is the bootstrap's input record: mutating it would decouple the
    curve from the quotes that produced it, with no way to detect the drift."""
    quote = OISQuote(tenor="1Y", maturity_date=ANNUAL_MATURITIES[0], fixed_rate=0.0950)
    with pytest.raises(dataclasses.FrozenInstanceError):
        quote.fixed_rate = 0.1050  # type: ignore[misc]


def test_curve_defaults_to_act_360_and_stores_its_pillars_verbatim() -> None:
    """The curve carries the basis its times were measured on, and does not
    resample, sort or otherwise touch the pillars it was handed."""
    curve = hand_built_curve()
    assert curve.day_count == DayCount.ACT_360
    assert curve.reference_date == REFERENCE_DATE
    assert np.array_equal(curve.pillar_times, PILLAR_TIMES)
    assert np.array_equal(curve.discount_factors, PILLAR_DISCOUNTS)


def test_curve_is_immutable() -> None:
    """A bootstrapped curve gets passed around a pricer; rebinding a field on it
    would change every holder's answer at a distance."""
    curve = hand_built_curve()
    with pytest.raises(dataclasses.FrozenInstanceError):
        curve.reference_date = date(2026, 9, 9)  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 2. Interpolation off the curve - Phase 7
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_discount_reproduces_its_own_pillars() -> None:
    """Interpolation is exact at the pillars: a bootstrap that does not reprice
    its own nodes has not bootstrapped anything."""
    curve = hand_built_curve()
    assert curve.discount(PILLAR_TIMES) == pytest.approx(PILLAR_DISCOUNTS, rel=1e-15)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_discount_is_log_linear_not_linear_between_pillars() -> None:
    """Log-linear on DF means the midpoint is the *geometric* mean of the two
    bracketing factors, which is what makes the instantaneous forward constant
    across the interval. Plain linear interpolation would give the arithmetic
    mean, 0.855, and a forward curve with a kink at every pillar."""
    curve = hand_built_curve()
    midpoint = curve.discount(np.array([1.5], dtype=np.float64))
    geometric = np.sqrt(PILLAR_DISCOUNTS[0] * PILLAR_DISCOUNTS[1])
    assert midpoint[0] == pytest.approx(float(geometric), rel=1e-12)
    assert midpoint[0] != pytest.approx(0.855, rel=1e-6)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_discount_at_the_reference_date_is_one() -> None:
    """No time, no discounting."""
    curve = hand_built_curve()
    assert curve.discount(np.array([0.0], dtype=np.float64))[0] == pytest.approx(1.0, rel=1e-15)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_zero_rate_is_continuously_compounded() -> None:
    """``zero_rate`` is documented as continuously compounded, so it inverts
    ``DF = exp(-z * tau)`` and *not* the effective-annual ``DF = (1 + r) ** -tau``
    that ois_curve.ShortRateCurve uses. At tau = 1, DF = 0.90, the two differ by
    about 42 bp - well outside anything a tolerance would absorb."""
    curve = hand_built_curve()
    zero = curve.zero_rate(PILLAR_TIMES)
    expected = -np.log(PILLAR_DISCOUNTS) / PILLAR_TIMES
    assert zero == pytest.approx(expected, rel=1e-12)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_forward_rate_is_the_simple_rate_that_links_two_discount_factors() -> None:
    """Simple, not compounded: ``DF(s) / DF(e) = 1 + F * (e - s)``."""
    curve = hand_built_curve()
    start = np.array([1.0], dtype=np.float64)
    end = np.array([2.0], dtype=np.float64)
    forward = curve.forward_rate(start, end)
    expected = (PILLAR_DISCOUNTS[0] / PILLAR_DISCOUNTS[1] - 1.0) / (end[0] - start[0])
    assert forward[0] == pytest.approx(float(expected), rel=1e-12)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_forward_rate_over_a_zero_length_window_is_not_a_division_by_zero() -> None:
    """``start == end`` has no accrual and must fail loudly rather than return
    ``inf`` or ``nan`` into a pricer."""
    curve = hand_built_curve()
    degenerate = np.array([1.0], dtype=np.float64)
    with pytest.raises(ValueError):
        curve.forward_rate(degenerate, degenerate)


# --------------------------------------------------------------------------- #
# 3. The bootstrap itself - Phase 7
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_bootstrap_reprices_its_own_quotes_at_par() -> None:
    """The definitional property. Each swap, valued on the curve built from it,
    must have a par rate equal to its own quote; a sequential exact bootstrap
    has no residual to hide behind."""
    quotes = annual_quotes()
    curve = bootstrap_ois_curve(quotes, REFERENCE_DATE, NO_HOLIDAYS)
    for index, quote in enumerate(quotes):
        coupon_times = curve.pillar_times[: index + 1]
        assert par_ois_rate(curve, coupon_times) == pytest.approx(quote.fixed_rate, abs=1e-12)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_bootstrap_produces_one_strictly_increasing_pillar_per_quote() -> None:
    """One quote in, one pillar out, in maturity order."""
    quotes = annual_quotes()
    curve = bootstrap_ois_curve(quotes, REFERENCE_DATE, NO_HOLIDAYS)
    assert curve.reference_date == REFERENCE_DATE
    assert curve.pillar_times.shape == (len(quotes),)
    assert np.all(np.diff(curve.pillar_times) > 0.0)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_bootstrap_output_is_a_monotone_discount_curve() -> None:
    """Positive COP rates mean strictly decreasing factors, all inside (0, 1]."""
    curve = bootstrap_ois_curve(annual_quotes(), REFERENCE_DATE, NO_HOLIDAYS)
    factors = curve.discount_factors
    assert np.all(factors > 0.0)
    assert np.all(factors <= 1.0)
    assert np.all(np.diff(factors) < 0.0)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_bootstrap_rejects_unsorted_quotes() -> None:
    """The bootstrap is sequential, so it cannot reorder its way out of this:
    an unsorted list is a caller error and must be named as one, not solved."""
    quotes = annual_quotes()
    with pytest.raises(ValueError):
        bootstrap_ois_curve([quotes[2], quotes[0], quotes[1]], REFERENCE_DATE, NO_HOLIDAYS)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_bootstrap_rejects_duplicate_pillars() -> None:
    """Two quotes on one maturity over-determine a single pillar; picking either
    one silently would discard a market observation."""
    quotes = annual_quotes()
    duplicate = OISQuote(tenor="1Y", maturity_date=ANNUAL_MATURITIES[0], fixed_rate=0.1010)
    with pytest.raises(ValueError):
        bootstrap_ois_curve([quotes[0], duplicate, quotes[1]], REFERENCE_DATE, NO_HOLIDAYS)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_bootstrap_rejects_quotes_implying_a_non_monotone_curve() -> None:
    """A 2Y par rate this far below the 1Y implies DF(2Y) > DF(1Y): a negative
    forward, i.e. an arbitrage in the input set, not a curve to be smoothed."""
    quotes = annual_quotes()
    arbitraged = [
        quotes[0],
        OISQuote(tenor="2Y", maturity_date=ANNUAL_MATURITIES[1], fixed_rate=-0.50),
    ]
    with pytest.raises(ValueError):
        bootstrap_ois_curve(arbitraged, REFERENCE_DATE, NO_HOLIDAYS)


# --------------------------------------------------------------------------- #
# 4. Overnight compounding - Phase 7
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_compound_overnight_over_a_full_360_day_basis() -> None:
    """One accrual of 360 days at a 10% nominal ACT/360 rate is exactly 1.10.
    Hand-checkable precisely because the accrual and the basis cancel."""
    growth = compound_overnight(
        np.array([0.10], dtype=np.float64),
        np.array([360.0], dtype=np.float64),
    )
    assert growth == pytest.approx(1.10, rel=1e-15)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_compound_overnight_compounds_rather_than_sums() -> None:
    """Two 180-day accruals at 10% give (1.05)^2 = 1.1025, not 1.10. This is the
    whole reason an IBR OIS leg is not an ACT/365 effective-rate discount
    factor, and the 25 bp gap is what a simple-interest implementation loses."""
    growth = compound_overnight(
        np.array([0.10, 0.10], dtype=np.float64),
        np.array([180.0, 180.0], dtype=np.float64),
    )
    assert growth == pytest.approx(1.1025, rel=1e-15)
    assert growth > 1.10


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_compound_overnight_of_an_empty_path_is_one() -> None:
    """An empty product is 1.0: no fixings, no growth, and no special case at
    the call site of a swap that has not started accruing."""
    growth = compound_overnight(
        np.array([], dtype=np.float64),
        np.array([], dtype=np.float64),
    )
    assert growth == 1.0


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 7", strict=True)
def test_compound_overnight_rejects_mismatched_path_lengths() -> None:
    """A rate without its accrual, or the reverse, is a broken fixing path."""
    with pytest.raises(ValueError):
        compound_overnight(
            np.array([0.10, 0.10], dtype=np.float64),
            np.array([180.0], dtype=np.float64),
        )
