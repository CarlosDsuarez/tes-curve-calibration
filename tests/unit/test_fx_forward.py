"""USD/COP forward pricing under covered interest parity.

Every rate here is **effective annual, ACT/365**, matching
:class:`~tes_pricer.math.ois_curve.ShortRateCurve` and
:func:`~tes_pricer.math.fx_forward.price_fx_forward`. The curves are built
directly rather than through the ``build_*_short_curve`` helpers so that a
single flat pillar does not raise ``FlatCurveApproximationWarning``: flatness is
the point here, since it makes ``r_cop`` and ``r_usd`` known constants at every
tenor and turns each forward into a number that can be checked by hand.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from tes_pricer.math.fx_forward import (
    CurveConsistencyWarning,
    ForwardPointsSignWarning,
    FXForwardQuote,
    OptionType,
    check_forward_points_sign,
    forward_rate_cip,
    garman_kohlhagen_price,
    implied_forward_points,
    implied_forward_rate_from_points,
    price_fx_forward,
)
from tes_pricer.math.ois_curve import ShortRateCurve

pytestmark = pytest.mark.unit

VALUATION_DATE = date(2026, 3, 16)
ONE_YEAR_LATER = date(2027, 3, 16)
"""Exactly 365 calendar days after ``VALUATION_DATE`` (2027 is not a leap year)."""

SPOT = 4000.0


def flat_curve(rate: float, currency: str, curve_date: date = VALUATION_DATE) -> ShortRateCurve:
    """A single-pillar curve: the same effective annual rate at every tenor."""
    return ShortRateCurve(
        tenors_years=np.array([1.0], dtype=np.float64),
        rates=np.array([rate], dtype=np.float64),
        currency=currency,
        curve_date=curve_date,
    )


def test_year_offset_is_exactly_365_days() -> None:
    """Guard the fixtures: the 'one year' used below really is T = 1.0 on ACT/365."""
    assert (ONE_YEAR_LATER - VALUATION_DATE).days == 365


def test_forward_equals_spot_when_rates_are_identical() -> None:
    """Identical COP and USD rates leave the forward exactly at spot.

    No rate differential, no carry, nothing to arbitrage: the discount factors
    are the same float, their ratio is exactly 1.0, and the equality is exact
    rather than approximate.
    """
    quote = price_fx_forward(
        SPOT,
        flat_curve(0.08, "COP"),
        flat_curve(0.08, "USD"),
        VALUATION_DATE,
        ONE_YEAR_LATER,
    )
    assert quote.forward_rate == SPOT
    assert quote.forward_points == 0.0
    assert quote.tenor_years == 1.0


def test_known_case_ten_versus_five_percent() -> None:
    """Hand-checked case: S = 4000, r_cop = 10%, r_usd = 5%, T = 1.0 exactly.

    F = S x (1 + r_cop)^T / (1 + r_usd)^T
      = 4000 x 1.10 / 1.05
      = 4400 / 1.05
      = 4190.476190476190...

    and the points are (4190.476190... - 4000) x 10000 = 1904761.9047...
    """
    quote = price_fx_forward(
        SPOT,
        flat_curve(0.10, "COP"),
        flat_curve(0.05, "USD"),
        VALUATION_DATE,
        ONE_YEAR_LATER,
    )
    assert quote.forward_rate == pytest.approx(4190.476190476190, rel=1e-12)
    assert quote.forward_points == pytest.approx(1904761.9047619048, rel=1e-12)
    assert quote.r_cop == 0.10
    assert quote.r_usd == 0.05


def test_known_case_matches_the_closed_form_ratio() -> None:
    """The discount-factor route agrees with the E.A. ratio written out directly."""
    quote = price_fx_forward(
        SPOT,
        flat_curve(0.10, "COP"),
        flat_curve(0.05, "USD"),
        VALUATION_DATE,
        ONE_YEAR_LATER,
    )
    expected = SPOT * (1.10**1.0) / (1.05**1.0)
    assert quote.forward_rate == pytest.approx(expected, rel=1e-14)


def test_continuous_compounding_would_give_a_different_number() -> None:
    """The E.A. convention is a real choice, not a formatting detail.

    Pricing the same leg as exp((r_cop - r_usd) T) - which would be correct only
    if the curves were continuously compounded - moves the forward by around
    9 COP on a 4000 spot. The test pins the gap so that a future switch to the
    continuous form cannot pass silently.
    """
    quote = price_fx_forward(
        SPOT,
        flat_curve(0.10, "COP"),
        flat_curve(0.05, "USD"),
        VALUATION_DATE,
        ONE_YEAR_LATER,
    )
    continuous = SPOT * np.exp((0.10 - 0.05) * 1.0)
    assert abs(quote.forward_rate - continuous) > 5.0


@pytest.mark.parametrize(
    ("r_cop", "r_usd"),
    [
        (0.1150, 0.0430),  # the structural USD/COP case: COP well above USD
        (0.0900, 0.0900),  # no differential
        (0.0500, 0.0430),  # a thin positive differential
        (0.0300, 0.0430),  # hypothetical inversion: COP below USD
        (0.0010, 0.0900),  # deeply inverted, far outside anything observed
    ],
)
def test_forward_points_sign_tracks_the_rate_differential(r_cop: float, r_usd: float) -> None:
    """sign(forward points) must equal sign(r_cop - r_usd) at every combination."""
    quote = price_fx_forward(
        SPOT,
        flat_curve(r_cop, "COP"),
        flat_curve(r_usd, "USD"),
        VALUATION_DATE,
        ONE_YEAR_LATER,
    )
    assert np.sign(quote.forward_points) == np.sign(r_cop - r_usd)
    assert check_forward_points_sign(
        quote.forward_points,
        quote.r_cop,
        quote.r_usd,
        tenor_years=quote.tenor_years,
    )


def test_forward_is_monotone_in_tenor_when_cop_pays_more() -> None:
    """With a constant positive differential, a longer tenor means a higher forward."""
    cop_curve = flat_curve(0.1150, "COP")
    usd_curve = flat_curve(0.0430, "USD")
    day_offsets = [1, 30, 91, 182, 365, 730, 1095, 1826]

    forwards = np.array(
        [
            price_fx_forward(
                SPOT,
                cop_curve,
                usd_curve,
                VALUATION_DATE,
                VALUATION_DATE + timedelta(days=offset),
            ).forward_rate
            for offset in day_offsets
        ]
    )

    assert np.all(np.diff(forwards) > 0.0)
    assert forwards[0] > SPOT


def test_forward_is_monotone_downwards_when_usd_pays_more() -> None:
    """The mirror image: a constant negative differential falls with tenor."""
    cop_curve = flat_curve(0.0300, "COP")
    usd_curve = flat_curve(0.0430, "USD")
    forwards = np.array(
        [
            price_fx_forward(
                SPOT,
                cop_curve,
                usd_curve,
                VALUATION_DATE,
                VALUATION_DATE + timedelta(days=offset),
            ).forward_rate
            for offset in [30, 91, 182, 365, 730]
        ]
    )
    assert np.all(np.diff(forwards) < 0.0)


def test_zero_tenor_returns_spot() -> None:
    """A forward delivering today is spot, and carries no points."""
    quote = price_fx_forward(
        SPOT,
        flat_curve(0.1150, "COP"),
        flat_curve(0.0430, "USD"),
        VALUATION_DATE,
        VALUATION_DATE,
    )
    assert quote.tenor_years == 0.0
    assert quote.forward_rate == SPOT
    assert quote.forward_points == 0.0


def test_rates_are_read_at_the_forward_tenor() -> None:
    """A term structure is sampled at T, not at the front of the curve."""
    cop_curve = ShortRateCurve(
        tenors_years=np.array([0.25, 1.0], dtype=np.float64),
        rates=np.array([0.09, 0.13], dtype=np.float64),
        currency="COP",
        curve_date=VALUATION_DATE,
    )
    quote = price_fx_forward(
        SPOT,
        cop_curve,
        flat_curve(0.0430, "USD"),
        VALUATION_DATE,
        VALUATION_DATE + timedelta(days=228),  # 0.6246... years, between the pillars
    )
    assert quote.r_cop == pytest.approx(cop_curve.interpolated_rate(228 / 365.0), rel=1e-15)
    assert 0.09 < quote.r_cop < 0.13


def test_act_360_shortens_nothing_but_lengthens_t() -> None:
    """ACT/360 is accepted and moves T, so it must move the forward."""
    args = (
        SPOT,
        flat_curve(0.1150, "COP"),
        flat_curve(0.0430, "USD"),
        VALUATION_DATE,
        ONE_YEAR_LATER,
    )
    act_365 = price_fx_forward(*args, day_count="ACT/365")
    act_360 = price_fx_forward(*args, day_count="ACT/360")
    assert act_360.tenor_years == pytest.approx(365 / 360.0)
    assert act_360.forward_rate > act_365.forward_rate


@pytest.mark.parametrize("convention", ["ACT/ACT-ISDA", "30/360", "ACT/365F", ""])
def test_unsupported_day_counts_are_rejected(convention: str) -> None:
    """Calendar-aware conventions belong in day_count.year_fraction, not here."""
    with pytest.raises(ValueError):
        price_fx_forward(
            SPOT,
            flat_curve(0.1150, "COP"),
            flat_curve(0.0430, "USD"),
            VALUATION_DATE,
            ONE_YEAR_LATER,
            day_count=convention,
        )


def test_maturity_before_valuation_is_rejected() -> None:
    """A forward cannot deliver in the past."""
    with pytest.raises(ValueError, match="precedes valuation_date"):
        price_fx_forward(
            SPOT,
            flat_curve(0.1150, "COP"),
            flat_curve(0.0430, "USD"),
            VALUATION_DATE,
            VALUATION_DATE - timedelta(days=1),
        )


@pytest.mark.parametrize("bad_spot", [0.0, -4000.0, float("nan"), float("inf")])
def test_non_positive_spot_is_rejected(bad_spot: float) -> None:
    """Spot is COP per USD: it is positive and finite or it is a data error."""
    with pytest.raises(ValueError):
        price_fx_forward(
            bad_spot,
            flat_curve(0.1150, "COP"),
            flat_curve(0.0430, "USD"),
            VALUATION_DATE,
            ONE_YEAR_LATER,
        )


def test_swapped_curves_warn_on_the_currency_label() -> None:
    """A USD curve passed as the COP leg is exactly what the label check is for."""
    with pytest.warns(CurveConsistencyWarning, match="COP leg"):
        price_fx_forward(
            SPOT,
            flat_curve(0.0430, "USD"),
            flat_curve(0.1150, "COP"),
            VALUATION_DATE,
            ONE_YEAR_LATER,
        )


def test_stale_curve_date_warns() -> None:
    """Pricing today off yesterday's curve is legal but must be visible."""
    with pytest.warns(CurveConsistencyWarning, match="stale"):
        price_fx_forward(
            SPOT,
            flat_curve(0.1150, "COP", curve_date=VALUATION_DATE - timedelta(days=1)),
            flat_curve(0.0430, "USD"),
            VALUATION_DATE,
            ONE_YEAR_LATER,
        )


def test_contradictory_quote_warns_loudly() -> None:
    """A quote whose points fight its rate differential must not pass silently."""
    with pytest.warns(ForwardPointsSignWarning, match="SIGN CHECK FAILED"):
        FXForwardQuote(
            spot_rate=SPOT,
            forward_rate=3900.0,
            forward_points=implied_forward_points(SPOT, 3900.0),
            tenor_years=1.0,
            r_cop=0.1150,
            r_usd=0.0430,
            valuation_date=VALUATION_DATE,
            maturity_date=ONE_YEAR_LATER,
        )


def test_sign_check_reports_false_without_raising() -> None:
    """The check warns and returns False; it never fails silently, never crashes."""
    with pytest.warns(ForwardPointsSignWarning):
        assert not check_forward_points_sign(-1000.0, 0.1150, 0.0430, tenor_years=1.0)


def test_sign_check_ignores_zero_tenor() -> None:
    """Zero points over zero time contradict nothing, whatever the differential."""
    assert check_forward_points_sign(0.0, 0.1150, 0.0430, tenor_years=0.0)


def test_forward_points_round_trip() -> None:
    """implied_forward_rate_from_points inverts implied_forward_points."""
    forward = 4321.5
    points = implied_forward_points(SPOT, forward)
    assert points == pytest.approx((forward - SPOT) * 10000.0, rel=1e-15)
    assert implied_forward_rate_from_points(SPOT, points) == pytest.approx(forward, rel=1e-15)


def test_forward_equals_spot_when_curves_match() -> None:
    """Identical discount curves leave the forward at spot."""
    discount = np.array([0.97, 0.94], dtype=np.float64)
    forward = forward_rate_cip(4000.0, discount, discount)
    assert forward == pytest.approx(np.array([4000.0, 4000.0]))


def test_higher_cop_rate_implies_forward_above_spot() -> None:
    """COP rates above USD rates put the forward above spot: positive points."""
    cop_discount = np.array([0.91], dtype=np.float64)
    usd_discount = np.array([0.96], dtype=np.float64)
    forward = forward_rate_cip(4000.0, cop_discount, usd_discount)
    assert forward[0] > 4000.0


def test_forward_rate_cip_rejects_mismatched_shapes() -> None:
    """Two curves sampled at different tenor counts cannot be paired."""
    with pytest.raises(ValueError, match="same shape"):
        forward_rate_cip(
            4000.0,
            np.array([0.97, 0.94], dtype=np.float64),
            np.array([0.99], dtype=np.float64),
        )


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 8", strict=True)
def test_put_call_parity() -> None:
    """Garman-Kohlhagen must satisfy C - P = S*exp(-rf*T) - K*exp(-rd*T)."""
    spot, strike, tau, r_cop, r_usd, vol = 4000.0, 4100.0, 1.0, 0.09, 0.04, 0.12
    call = garman_kohlhagen_price(spot, strike, tau, r_cop, r_usd, vol, OptionType.CALL)
    put = garman_kohlhagen_price(spot, strike, tau, r_cop, r_usd, vol, OptionType.PUT)
    expected = spot * np.exp(-r_usd * tau) - strike * np.exp(-r_cop * tau)
    assert call - put == pytest.approx(expected, abs=1e-8)
