"""Short-end E.A. curve: rate-space interpolation, flat extrapolation, flat fallback.

Every expected number in this file is a literal, computed once by hand from the
stated formula and pasted in. Nothing here re-derives an expectation by calling
the same code path under test, because a test that recomputes
``(1 + interp(tau)) ** -tau`` proves only that the module is self-consistent -
it would pass just as happily if the module interpolated discount factors.
"""

from __future__ import annotations

import itertools
import warnings
from datetime import date

import numpy as np
import pytest

from tes_pricer.math.ois_curve import (
    OVERNIGHT_TENOR_YEARS,
    FlatCurveApproximationWarning,
    ShortRateCurve,
    build_cop_short_curve,
    build_usd_short_curve,
)

pytestmark = pytest.mark.unit

CURVE_DATE = date(2026, 9, 8)

# Three deliberately hand-friendly pillars. 0.25 and 0.50 bracket the midpoint
# 0.375, where the linear rate interpolation lands exactly on (0.10 + 0.12) / 2
# = 0.11, so the expected discount factor can be written down without an
# interpolator: DF = 1.11 ** -0.375.
THREE_POINT_TENORS = np.array([0.25, 0.50, 1.00], dtype=np.float64)
THREE_POINT_RATES = np.array([0.10, 0.12, 0.11], dtype=np.float64)


def three_point_curve() -> ShortRateCurve:
    """The shared 3-pillar fixture curve, in COP."""
    return ShortRateCurve(
        tenors_years=THREE_POINT_TENORS,
        rates=THREE_POINT_RATES,
        currency="COP",
        curve_date=CURVE_DATE,
    )


# --------------------------------------------------------------------------- #
# 1. Interpolation happens on rates, not on discount factors
# --------------------------------------------------------------------------- #


def test_midpoint_discount_factor_matches_hand_computed_rate_interpolation() -> None:
    """At tau = 0.375 the rate is the midpoint 0.11, so DF = 1.11 ** -0.375.

    Worked by hand:
        r(0.375) = 0.10 + (0.375 - 0.25) / (0.50 - 0.25) * (0.12 - 0.10)
                 = 0.10 + 0.5 * 0.02 = 0.11
        DF       = 1.11 ** -0.375 = 0.9616208760381489
    """
    curve = three_point_curve()

    assert curve.rate(0.375) == pytest.approx(0.11, abs=1e-15)
    assert curve.discount_factor(0.375) == pytest.approx(0.9616208760381489, abs=1e-12)


def test_midpoint_discount_factor_differs_from_interpolating_discount_factors() -> None:
    """The same midpoint under DF-space interpolation gives a different number.

    This is the test that actually pins the convention. Hand-computed:
        DF(0.25) = 1.10 ** -0.25 = 0.9764540896763105
        DF(0.50) = 1.12 ** -0.50 = 0.9449111825230680
        midpoint of those two    = 0.9606826360996893   <- the WRONG convention
        1.11 ** -0.375           = 0.9616208760381489   <- what we must return

    The gap is ~9.4e-4, about 9 basis points of price on a par notional: far
    outside any tolerance, so a silent switch to DF interpolation cannot pass.
    """
    curve = three_point_curve()
    df_space_midpoint = 0.9606826360996893

    assert curve.discount_factor(0.375) == pytest.approx(0.9616208760381489, abs=1e-12)
    assert curve.discount_factor(0.375) != pytest.approx(df_space_midpoint, abs=1e-6)
    assert curve.discount_factor(0.375) - df_space_midpoint == pytest.approx(
        9.382399384596e-04, abs=1e-12
    )


def test_curve_reproduces_its_own_pillars() -> None:
    """On a pillar, interpolation is the identity, so DF = (1 + r_i) ** -tau_i."""
    curve = three_point_curve()

    assert curve.discount_factor(0.25) == pytest.approx(0.9764540896763105, abs=1e-12)
    assert curve.discount_factor(0.50) == pytest.approx(0.9449111825230680, abs=1e-12)
    assert curve.discount_factor(1.00) == pytest.approx(0.9009009009009008, abs=1e-12)


def test_interpolation_on_the_inverted_segment() -> None:
    """The 0.50 -> 1.00 segment slopes down; the midpoint rate is 0.115.

    Worked by hand:
        r(0.75) = 0.12 + (0.75 - 0.50) / (1.00 - 0.50) * (0.11 - 0.12) = 0.115
        DF      = 1.115 ** -0.75 = 0.9216029356291786
    """
    curve = three_point_curve()

    assert curve.rate(0.75) == pytest.approx(0.115, abs=1e-15)
    assert curve.discount_factor(0.75) == pytest.approx(0.9216029356291786, abs=1e-12)


def test_discount_factor_at_zero_is_one() -> None:
    """(1 + r) ** 0 = 1 for any r, so today's cash flow is undiscounted."""
    assert three_point_curve().discount_factor(0.0) == 1.0


# --------------------------------------------------------------------------- #
# 2. Flat extrapolation at both ends
# --------------------------------------------------------------------------- #


def test_flat_extrapolation_below_the_shortest_pillar() -> None:
    """Below 0.25 the shortest rate 0.10 is held, not extrapolated down the slope.

    Hand-computed at tau = 0.01: DF = 1.10 ** -0.01 = 0.9990473522592097.

    A linear extrapolation of the first segment would give
    r(0.01) = 0.10 + (0.01 - 0.25) / 0.25 * 0.02 = 0.0808, a 192bp error at the
    very point where a short curve is used most.
    """
    curve = three_point_curve()

    assert curve.rate(0.01) == pytest.approx(0.10, abs=1e-15)
    assert curve.discount_factor(0.01) == pytest.approx(0.9990473522592097, abs=1e-12)


def test_flat_extrapolation_above_the_longest_pillar() -> None:
    """Above 1.00 the longest rate 0.11 is held.

    Hand-computed at tau = 5.0: DF = 1.11 ** -5 = 0.5934513280585586.

    The last segment slopes *down* by 2% per 0.5y, so linear extrapolation would
    reach r(5.0) = 0.11 - 8 * 0.01 = -0.05: a negative COP rate and a discount
    factor above 1. That is the economically absurd long end this rule exists to
    prevent.
    """
    curve = three_point_curve()

    assert curve.rate(5.0) == pytest.approx(0.11, abs=1e-15)
    assert curve.discount_factor(5.0) == pytest.approx(0.5934513280585586, abs=1e-12)


@pytest.mark.parametrize("tau", [1e-9, 1e-6, 0.001, 0.1, 0.2499])
def test_short_extrapolation_is_constant_in_rate(tau: float) -> None:
    """Every tau below the first pillar returns exactly the first rate."""
    assert three_point_curve().rate(tau) == pytest.approx(0.10, abs=1e-15)


@pytest.mark.parametrize("tau", [1.0001, 2.0, 10.0, 30.0, 100.0])
def test_long_extrapolation_is_constant_in_rate(tau: float) -> None:
    """Every tau beyond the last pillar returns exactly the last rate."""
    assert three_point_curve().rate(tau) == pytest.approx(0.11, abs=1e-15)


def test_extrapolated_discount_factors_stay_in_the_unit_interval() -> None:
    """Flat extrapolation keeps DF monotone and bounded, however far out we go."""
    curve = three_point_curve()
    taus = [0.0, 1e-6, 0.01, 0.25, 0.375, 0.5, 1.0, 5.0, 30.0, 100.0]
    factors = [curve.discount_factor(tau) for tau in taus]

    assert all(0.0 < df <= 1.0 for df in factors)
    assert all(later <= earlier for earlier, later in itertools.pairwise(factors))


def test_negative_tau_is_rejected() -> None:
    """Discounting backwards is a caller bug, not a curve extrapolation case."""
    curve = three_point_curve()

    with pytest.raises(ValueError, match="non-negative"):
        curve.discount_factor(-0.5)


# --------------------------------------------------------------------------- #
# 3. Degenerate case: a single rate, a loud warning, a flat curve
# --------------------------------------------------------------------------- #


def test_single_pillar_cop_curve_warns_and_is_flat() -> None:
    """One IBR fixing gives a flat curve, and the warning has to say so.

    Hand-computed against the single rate 0.0985:
        DF(0.001) = 1.0985 ** -0.001 = 0.9999060587999083
        DF(0.25)  = 1.0985 ** -0.25  = 0.9767872557491274
        DF(1.0)   = 1.0985 ** -1     = 0.9103322712790168
        DF(7.5)   = 1.0985 ** -7.5   = 0.4943101640742340
    """
    with pytest.warns(FlatCurveApproximationWarning) as record:
        curve = build_cop_short_curve(
            ibr_overnight=0.0985,
            ibr_1m=None,
            ibr_3m=None,
            curve_date=CURVE_DATE,
        )

    message = str(record[0].message)
    assert "FLAT-RATE APPROXIMATION" in message
    assert "no term structure" in message
    assert "COP" in message

    assert curve.currency == "COP"
    assert curve.curve_date == CURVE_DATE
    assert curve.tenors_years.tolist() == [OVERNIGHT_TENOR_YEARS]

    for tau in (1e-9, 0.001, 0.05, 0.25, 1.0, 7.5, 50.0):
        assert curve.rate(tau) == pytest.approx(0.0985, abs=1e-15)

    assert curve.discount_factor(0.001) == pytest.approx(0.9999060587999083, abs=1e-12)
    assert curve.discount_factor(0.25) == pytest.approx(0.9767872557491274, abs=1e-12)
    assert curve.discount_factor(1.0) == pytest.approx(0.9103322712790168, abs=1e-12)
    assert curve.discount_factor(7.5) == pytest.approx(0.4943101640742340, abs=1e-12)


def test_single_pillar_warning_can_be_escalated_to_an_error() -> None:
    """The dedicated warning class lets a caller make the flat fallback fatal."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", FlatCurveApproximationWarning)
        with pytest.raises(FlatCurveApproximationWarning):
            build_cop_short_curve(0.0985, None, None, CURVE_DATE)


def test_cop_warning_names_the_free_banrep_term_series() -> None:
    """The remedy must be actionable: IBR 1M/3M/6M/12M are free, not paywalled."""
    with pytest.warns(FlatCurveApproximationWarning) as record:
        build_cop_short_curve(0.0985, None, None, CURVE_DATE)

    message = str(record[0].message)
    assert "free" in message
    assert "15325" in message
    assert "16561" in message


def test_usd_single_pillar_curve_warns_and_is_flat() -> None:
    """SOFR overnight alone is the realistic USD case, and it still warns.

    Hand-computed at the single rate 0.0432: the curve must return 0.0432 at
    every tau, and DF(2.0) = 1.0432 ** -2 = 0.9188927885882044.
    """
    with pytest.warns(FlatCurveApproximationWarning) as record:
        curve = build_usd_short_curve(sofr_overnight=0.0432, curve_date=CURVE_DATE)

    message = str(record[0].message)
    assert "FLAT-RATE APPROXIMATION" in message
    assert "USD" in message
    assert "CME Term SOFR is licensed" in message

    assert curve.currency == "USD"
    assert curve.tenors_years.tolist() == [OVERNIGHT_TENOR_YEARS]
    for tau in (1e-6, 0.5, 2.0, 20.0):
        assert curve.rate(tau) == pytest.approx(0.0432, abs=1e-15)
    assert curve.discount_factor(2.0) == pytest.approx(0.9188927885882044, abs=1e-12)


# --------------------------------------------------------------------------- #
# Builders with a real term structure: no warning, pillars in the right places
# --------------------------------------------------------------------------- #


def test_full_cop_curve_does_not_warn() -> None:
    """With 1M and 3M present there is a term structure, so no warning fires."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", FlatCurveApproximationWarning)
        curve = build_cop_short_curve(
            ibr_overnight=0.0985,
            ibr_1m=0.1002,
            ibr_3m=0.1035,
            curve_date=CURVE_DATE,
        )

    assert curve.tenors_years == pytest.approx([1.0 / 365.0, 1.0 / 12.0, 0.25])
    assert curve.rates == pytest.approx([0.0985, 0.1002, 0.1035])


def test_additional_tenors_extend_the_cop_curve_and_stay_sorted() -> None:
    """6M and 12M arrive out of order and must be merged into ascending pillars."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", FlatCurveApproximationWarning)
        curve = build_cop_short_curve(
            ibr_overnight=0.0985,
            ibr_1m=0.1002,
            ibr_3m=0.1035,
            curve_date=CURVE_DATE,
            additional_tenors={1.0: 0.1071, 0.5: 0.1054},
        )

    assert curve.tenors_years == pytest.approx([1.0 / 365.0, 1.0 / 12.0, 0.25, 0.5, 1.0])
    assert curve.rates == pytest.approx([0.0985, 0.1002, 0.1035, 0.1054, 0.1071])


def test_partial_cop_curve_keeps_only_the_published_tenors() -> None:
    """A missing 1M drops that pillar without shifting the others."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", FlatCurveApproximationWarning)
        curve = build_cop_short_curve(0.0985, None, 0.1035, CURVE_DATE)

    assert curve.tenors_years == pytest.approx([1.0 / 365.0, 0.25])
    assert curve.rates == pytest.approx([0.0985, 0.1035])


def test_usd_curve_with_licensed_term_pillars_does_not_warn() -> None:
    """Supplying Term SOFR through additional_tenors removes the flat-curve caveat."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", FlatCurveApproximationWarning)
        curve = build_usd_short_curve(
            sofr_overnight=0.0432,
            curve_date=CURVE_DATE,
            additional_tenors={0.25: 0.0431, 0.5: 0.0425},
        )

    assert curve.tenors_years == pytest.approx([1.0 / 365.0, 0.25, 0.5])
    assert curve.rates == pytest.approx([0.0432, 0.0431, 0.0425])


def test_additional_tenors_may_not_duplicate_a_named_tenor() -> None:
    """A tenor given twice is ambiguous, so it is an error rather than a silent pick."""
    with pytest.raises(ValueError, match="duplicates the tenor"):
        build_cop_short_curve(0.0985, 0.1002, 0.1035, CURVE_DATE, additional_tenors={0.25: 0.11})


# --------------------------------------------------------------------------- #
# Construction guards
# --------------------------------------------------------------------------- #


def test_percentages_are_rejected_as_a_unit_mix_up() -> None:
    """SUAMECA returns 11.985 for 11.985%; feeding that straight in must fail loudly."""
    with pytest.raises(ValueError, match="decimals, not percentages"):
        build_cop_short_curve(11.985, 12.023, 12.156, CURVE_DATE)


def test_mismatched_pillar_lengths_are_rejected() -> None:
    """A rate without a tenor has no place to sit on the curve."""
    with pytest.raises(ValueError, match="same length"):
        ShortRateCurve(
            tenors_years=np.array([0.25, 0.5]),
            rates=np.array([0.10]),
            currency="COP",
            curve_date=CURVE_DATE,
        )


def test_unsorted_or_duplicated_tenors_are_rejected() -> None:
    """np.interp silently returns nonsense on unsorted xp, so reject it up front."""
    with pytest.raises(ValueError, match="strictly increasing"):
        ShortRateCurve(
            tenors_years=np.array([0.5, 0.25]),
            rates=np.array([0.12, 0.10]),
            currency="COP",
            curve_date=CURVE_DATE,
        )


def test_empty_curve_is_rejected() -> None:
    """A curve with no pillars cannot discount anything."""
    with pytest.raises(ValueError, match="at least one pillar"):
        ShortRateCurve(
            tenors_years=np.array([]),
            rates=np.array([]),
            currency="COP",
            curve_date=CURVE_DATE,
        )


def test_non_finite_inputs_are_rejected() -> None:
    """A NaN pillar would propagate into every discount factor unnoticed."""
    with pytest.raises(ValueError, match="finite"):
        ShortRateCurve(
            tenors_years=np.array([0.25, 0.5]),
            rates=np.array([0.10, np.nan]),
            currency="COP",
            curve_date=CURVE_DATE,
        )


def test_lists_are_accepted_and_coerced_to_float_arrays() -> None:
    """Plain Python sequences are a normal way to hand-build a curve in a notebook."""
    curve = ShortRateCurve(
        tenors_years=[0.25, 0.5],  # type: ignore[arg-type]
        rates=[0.10, 0.12],  # type: ignore[arg-type]
        currency="COP",
        curve_date=CURVE_DATE,
    )

    assert curve.tenors_years.dtype == np.float64
    assert curve.rates.dtype == np.float64
    assert curve.discount_factor(0.25) == pytest.approx(0.9764540896763105, abs=1e-12)


def test_interpolated_rate_is_the_same_value_as_rate() -> None:
    """The FX-facing alias must not drift away from the method it delegates to."""
    curve = three_point_curve()
    for tau in (0.0, 0.01, 0.125, 0.375, 0.5, 0.75, 5.0):
        assert curve.interpolated_rate(tau) == curve.rate(tau)


def test_discount_factor_is_derived_from_the_interpolated_rate() -> None:
    """DF(tau) = (1 + interpolated_rate(tau))^-tau, the E.A. basis in one assertion."""
    curve = three_point_curve()
    tau = 0.375
    expected = (1.0 + curve.interpolated_rate(tau)) ** (-tau)
    assert curve.discount_factor(tau) == pytest.approx(expected, rel=1e-15)
