r"""Outright USD/COP forward greeks: analytic vs bump-and-reprice.

The point of this file is that nothing here trusts the closed form. Every
analytic number is re-derived a second way - by repricing a bumped input
through :func:`~tes_pricer.math.greeks.forward_value`, or by writing the
derivative out by hand from the pricing identity

.. math::

    V = N\,\bigl(S\,DF_{USD}(\tau) - K\,DF_{COP}(\tau)\bigr)

and comparing. A test that re-derived an expectation by calling the same
analytic branch would prove only that the module agrees with itself.

The curves below are effective annual on ACT/365 (``DF = (1+r)^-tau``), which
is what :class:`~tes_pricer.math.ois_curve.ShortRateCurve` promises. That is
deliberately *not* the continuously compounded convention, and one test pins
the difference so the two can never be quietly swapped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pytest

from tes_pricer.math import greeks as greeks_module
from tes_pricer.math.greeks import (
    BASIS_POINT,
    DELTA_RECONCILIATION_TOLERANCE,
    SPOT_BUMP_RELATIVE,
    ForwardGreeks,
    GreeksReconciliationError,
    compute_forward_greeks,
    forward_value,
)
from tes_pricer.math.ois_curve import ShortRateCurve

pytestmark = pytest.mark.unit

VALUATION_DATE = date(2026, 9, 8)

# A plausible COP short end (IBR O/N through 12M) and USD short end (SOFR plus
# term pillars). Upward sloping in COP, mildly inverted in USD, which is the
# 2026 regime and gives the forward positive points.
COP_TENORS = np.array([1.0 / 365.0, 1.0 / 12.0, 0.25, 0.50, 1.00], dtype=np.float64)
COP_RATES = np.array([0.0925, 0.0940, 0.0965, 0.0990, 0.1010], dtype=np.float64)
USD_TENORS = np.array([1.0 / 365.0, 0.25, 0.50, 1.00], dtype=np.float64)
USD_RATES = np.array([0.0435, 0.0428, 0.0420, 0.0410], dtype=np.float64)


def cop_curve() -> ShortRateCurve:
    """The shared COP discount curve."""
    return ShortRateCurve(
        tenors_years=COP_TENORS, rates=COP_RATES, currency="COP", curve_date=VALUATION_DATE
    )


def usd_curve() -> ShortRateCurve:
    """The shared USD discount curve."""
    return ShortRateCurve(
        tenors_years=USD_TENORS, rates=USD_RATES, currency="USD", curve_date=VALUATION_DATE
    )


@dataclass(frozen=True, slots=True)
class Scenario:
    """One priced forward, spanning a different corner of the input space."""

    name: str
    notional_usd: float
    contract_forward_rate: float
    spot: float
    days_to_maturity: int

    @property
    def maturity_date(self) -> date:
        return VALUATION_DATE + timedelta(days=self.days_to_maturity)

    @property
    def tau(self) -> float:
        return self.days_to_maturity / 365.0

    def greeks(self) -> ForwardGreeks:
        return compute_forward_greeks(
            self.notional_usd,
            self.contract_forward_rate,
            self.spot,
            cop_curve(),
            usd_curve(),
            VALUATION_DATE,
            self.maturity_date,
        )

    def value(self, *, spot: float | None = None) -> float:
        return forward_value(
            self.notional_usd,
            self.contract_forward_rate,
            self.spot if spot is None else spot,
            cop_curve(),
            usd_curve(),
            VALUATION_DATE,
            self.maturity_date,
        )


# Five scenarios that move spot, notional sign, strike-versus-forward and tenor
# independently, so a bug that only shows up at one point on the curve or only
# for a long position cannot hide behind an average.
SCENARIOS = (
    Scenario("1y deep out of the money long", 1_000_000.0, 4_250.0, 4_000.0, 365),
    Scenario("3m long, strike near the forward", 5_000_000.0, 3_900.0, 3_850.0, 90),
    Scenario("1m short, high spot", -2_500_000.0, 4_405.0, 4_400.0, 30),
    Scenario("6m long, struck at spot", 750_000.0, 4_100.0, 4_100.0, 181),
    Scenario("overnight, large notional", 10_000_000.0, 3_951.0, 3_950.0, 1),
)
SCENARIO_IDS = tuple(scenario.name for scenario in SCENARIOS)


# --------------------------------------------------------------------------- #
# 1. Analytic delta reconciles with bump-and-reprice
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_analytic_delta_matches_central_difference(scenario: Scenario) -> None:
    """The closed form and the bumped delta agree far inside the 0.1% tolerance.

    Both are computed here rather than read off the returned object, so this
    fails if either branch drifts - the module's own reconciliation would only
    fail if they drift *apart* by more than the tolerance.

    The 0.1% figure is the contract; the assertion also pins the gap at 1e-9,
    because ``V`` is affine in ``S`` and a central difference of an affine
    function is exact up to rounding. Anything in between - say 1e-4 - would
    mean a real derivation error small enough to slip past the contract, which
    is exactly the case a loose tolerance would let through.
    """
    step = scenario.spot * SPOT_BUMP_RELATIVE
    delta_numeric = (
        scenario.value(spot=scenario.spot + step) - scenario.value(spot=scenario.spot - step)
    ) / (2.0 * step)

    delta_analytic = scenario.greeks().delta_spot

    relative_gap = abs(delta_analytic - delta_numeric) / max(
        abs(delta_analytic), abs(delta_numeric)
    )
    assert relative_gap < DELTA_RECONCILIATION_TOLERANCE
    assert relative_gap < 1e-9


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_delta_equals_notional_times_usd_discount_factor(scenario: Scenario) -> None:
    r"""``dV/dS = N * DF_usd(tau)``, written out independently of the module.

    Differentiating :math:`V = N(S\,DF_{USD} - K\,DF_{COP})` in :math:`S` leaves
    :math:`N\,DF_{USD}`, with no dependence on the strike at all. The discount
    factor is rebuilt here from the pillar rates rather than taken from the
    curve object, so this also pins the E.A. convention.
    """
    rate_usd = float(
        np.interp(scenario.tau, USD_TENORS, USD_RATES, left=USD_RATES[0], right=USD_RATES[-1])
    )
    expected = scenario.notional_usd * (1.0 + rate_usd) ** (-scenario.tau)

    assert scenario.greeks().delta_spot == pytest.approx(expected, rel=1e-12)


def test_carry_factor_is_effective_annual_not_continuous() -> None:
    """The E.A. and exponential carry factors are not interchangeable here.

    ``exp((r_cop - r_usd) * tau)`` is the textbook carry factor, but it belongs
    to a continuously compounded curve. Against this E.A. curve it overstates
    delta, and by enough to matter: at one year on a 10.1% / 4.1% pair the gap
    is 0.397%, about 40bp of delta - roughly the
    :math:`\tfrac12 (r_{COP}^2 - r_{USD}^2)\tau` second-order term you would
    predict. Pinning it stops the two conventions being swapped on the grounds
    that they "look the same".
    """
    scenario = SCENARIOS[0]
    rate_cop, rate_usd = 0.1010, 0.0410  # the 1y pillars, read off the tables above

    correct = scenario.notional_usd * (1.0 + rate_usd) ** -1.0
    exponential = (
        scenario.notional_usd * np.exp((rate_cop - rate_usd) * 1.0) * (1.0 + rate_cop) ** -1.0
    )

    assert scenario.greeks().delta_spot == pytest.approx(correct, rel=1e-12)
    assert abs(exponential - correct) / correct == pytest.approx(3.97e-3, rel=0.01)


# --------------------------------------------------------------------------- #
# 2. Gamma is zero, empirically and not only by assertion
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_bumped_gamma_is_zero_to_floating_point_noise(scenario: Scenario) -> None:
    r"""The *bumped* second difference is zero, not just the reported constant.

    :attr:`ForwardGreeks.gamma_spot` is hard-coded to ``0.0``, so on its own it
    proves nothing. This recomputes
    :math:`[V(S+h) - 2V(S) + V(S-h)] / h^2` from the public pricing function and
    checks the residual is pure rounding.

    **On the tolerance.** An absolute bound like ``1e-6`` COP is not attainable
    at desk notionals, and asserting it would be asserting something false. The
    numerator is a cancelling sum of terms of size :math:`|N|\,S\,DF_{USD}`, so
    its rounding error is about :math:`\varepsilon |N| S\,DF_{USD}`, and
    dividing by :math:`h^2 = (10^{-4} S)^2` *amplifies* it by :math:`10^8/S`.
    At $5m notional that noise floor is around 1e-5 COP - larger than 1e-6, and
    still economically nothing: a 100 COP spot move would generate
    :math:`\tfrac12 \gamma (100)^2 \approx 0.08` COP of second-order P&L.

    So two bounds are asserted instead, both scale-free:

    * the residual sits under the ``8 * eps`` rounding bound for that sum, and
    * per USD of notional it is below ``1e-6``, by six orders of magnitude.
    """
    step = scenario.spot * SPOT_BUMP_RELATIVE
    gamma_numeric = (
        scenario.value(spot=scenario.spot + step)
        - 2.0 * scenario.value()
        + scenario.value(spot=scenario.spot - step)
    ) / (step * step)

    largest_term = abs(scenario.notional_usd) * scenario.spot
    rounding_floor = 8.0 * float(np.finfo(np.float64).eps) * largest_term / (step * step)

    assert scenario.greeks().gamma_spot == 0.0
    assert abs(gamma_numeric) < rounding_floor
    assert abs(gamma_numeric) / abs(scenario.notional_usd) < 1e-6


def test_gamma_note_explains_the_zero() -> None:
    """A zero greek has to arrive with its reason attached, or it reads as a stub."""
    notes = " ".join(SCENARIOS[0].greeks().notes).lower()

    assert "gamma_spot=0 is expected" in notes
    assert "not a bug" in notes
    assert "affine" in notes or "linear" in notes


# --------------------------------------------------------------------------- #
# 3. DV01 signs, derived rather than assumed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_dv01_signs_follow_from_the_pricing_formula(scenario: Scenario) -> None:
    r"""A long forward gains on a COP sell-off and loses on a USD sell-off.

    Derived, not assumed. From :math:`V = N(S\,DF_{USD} - K\,DF_{COP})` with
    :math:`DF = (1+r)^{-\tau}`:

    .. math::

        \frac{\partial V}{\partial r_{COP}}
            = +N K \tau (1 + r_{COP})^{-\tau-1} > 0, \qquad
        \frac{\partial V}{\partial r_{USD}}
            = -N S \tau (1 + r_{USD})^{-\tau-1} < 0

    for :math:`N > 0`. Economically: buying USD forward is being short a COP
    zero (you owe ``K`` COP at ``T``) and long a USD zero. Higher COP rates
    shrink the present value of what you owe, so the position gains; higher USD
    rates shrink what you receive and pull the fair forward down, so it loses.
    Both flip for a short, and neither can be zero for a live position.
    """
    result = scenario.greeks()
    long_position = scenario.notional_usd > 0.0

    if long_position:
        assert result.dv01_cop > 0.0
        assert result.dv01_usd < 0.0
    else:
        assert result.dv01_cop < 0.0
        assert result.dv01_usd > 0.0

    # The two legs are close in magnitude but must not be equal and opposite:
    # they scale with K and S respectively, and discount on different curves.
    assert result.dv01_cop != pytest.approx(-result.dv01_usd, rel=1e-6)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_dv01_magnitudes_match_the_hand_derived_derivatives(scenario: Scenario) -> None:
    """The bumped DV01s match the closed forms above to the expected O(bump^2).

    The bump shifts every pillar, and the curve extrapolates flat, so the
    interpolated rate at ``tau`` moves by exactly 1bp. That makes the analytic
    derivative times 1bp the right comparison, accurate to second order - hence
    the 1e-3 relative tolerance rather than an exact match.
    """
    rate_cop = float(
        np.interp(scenario.tau, COP_TENORS, COP_RATES, left=COP_RATES[0], right=COP_RATES[-1])
    )
    rate_usd = float(
        np.interp(scenario.tau, USD_TENORS, USD_RATES, left=USD_RATES[0], right=USD_RATES[-1])
    )
    expected_cop = (
        scenario.notional_usd
        * scenario.contract_forward_rate
        * scenario.tau
        * (1.0 + rate_cop) ** (-scenario.tau - 1.0)
        * BASIS_POINT
    )
    expected_usd = (
        -scenario.notional_usd
        * scenario.spot
        * scenario.tau
        * (1.0 + rate_usd) ** (-scenario.tau - 1.0)
        * BASIS_POINT
    )

    result = scenario.greeks()

    assert result.dv01_cop == pytest.approx(expected_cop, rel=1e-3)
    assert result.dv01_usd == pytest.approx(expected_usd, rel=1e-3)


# --------------------------------------------------------------------------- #
# 4. Theta stays finite into expiry
# --------------------------------------------------------------------------- #


def test_theta_one_day_from_expiry_is_the_exact_pull_to_payoff() -> None:
    r"""With one day left, theta is exactly ``N(S - K) - V(1d)``.

    At :math:`\tau = 0` both discount factors are ``1`` by construction, so
    :math:`V \to N(S - K)`; the day's theta is the distance the position still
    has to travel. Nothing here divides by :math:`\tau`, which is why the
    number stays finite where an option's theta would not.
    """
    notional, spot, strike = 10_000_000.0, 3_950.0, 3_951.0
    maturity = VALUATION_DATE + timedelta(days=1)

    value_today = forward_value(
        notional, strike, spot, cop_curve(), usd_curve(), VALUATION_DATE, maturity
    )
    value_at_expiry = notional * (spot - strike)

    theta = compute_forward_greeks(
        notional, strike, spot, cop_curve(), usd_curve(), VALUATION_DATE, maturity
    ).theta

    assert theta == pytest.approx(value_at_expiry - value_today, rel=1e-12)
    assert np.isfinite(theta)


def test_theta_converges_rather_than_diverging_as_expiry_approaches() -> None:
    """Theta tends to one day of carry, it does not blow up.

    The failure this guards against is a theta that divides by the remaining
    tenor somewhere: that would grow without bound as ``tau -> 0``. The real
    answer shrinks monotonically towards pure overnight carry, which for $10m
    long USD at a ~5.4% E.A. rate differential is roughly 5m COP a day.
    """
    notional, spot, strike = 10_000_000.0, 3_950.0, 3_951.0
    thetas = [
        compute_forward_greeks(
            notional,
            strike,
            spot,
            cop_curve(),
            usd_curve(),
            VALUATION_DATE,
            VALUATION_DATE + timedelta(days=days),
        ).theta
        for days in (90, 30, 10, 5, 2, 1)
    ]

    assert all(np.isfinite(theta) for theta in thetas)
    # Monotonically decreasing in magnitude as expiry nears - the opposite of
    # divergence - and never more than one day's carry on the notional value.
    magnitudes = [abs(theta) for theta in thetas]
    assert magnitudes == sorted(magnitudes, reverse=True)
    assert max(magnitudes) < abs(notional) * spot * 0.001
    # And it lands on the carry estimate N * S * (ln(1+r_cop) - ln(1+r_usd)) / 365.
    overnight_carry = notional * spot * (np.log(1.0925) - np.log(1.0435)) / 365.0
    assert magnitudes[-1] == pytest.approx(overnight_carry, rel=0.05)


# --------------------------------------------------------------------------- #
# 5. The reconciliation guard is live, not decorative
# --------------------------------------------------------------------------- #


def test_reconciliation_raises_when_the_pricer_disagrees_with_the_closed_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pricing bug must raise, not silently return the analytic delta.

    The injected bug is the realistic one: a pricing function that forgets to
    discount the USD leg. It is still perfectly linear in spot, so gamma stays
    zero and nothing about the shape of the result looks wrong - only the delta
    reconciliation catches it, and at 1y the gap is about 3.9%, forty times the
    tolerance.
    """

    def undiscounted_usd_leg(
        notional_usd: float,
        contract_forward_rate: float,
        spot: float,
        cop_curve_: ShortRateCurve,
        usd_curve_: ShortRateCurve,
        tau: float,
    ) -> float:
        del usd_curve_
        return notional_usd * (spot - contract_forward_rate * cop_curve_.discount_factor(tau))

    monkeypatch.setattr(greeks_module, "_forward_value_at_tau", undiscounted_usd_leg)

    with pytest.raises(GreeksReconciliationError) as excinfo:
        SCENARIOS[0].greeks()

    error = excinfo.value
    assert error.greek == "delta_spot"
    assert error.relative_error > DELTA_RECONCILIATION_TOLERANCE
    assert error.relative_error == pytest.approx(0.0394, abs=1e-3)
    assert "one of them is wrong" in str(error)


# --------------------------------------------------------------------------- #
# 6. Input validation
# --------------------------------------------------------------------------- #


def test_two_curves_in_the_same_currency_are_rejected() -> None:
    """Passing the same curve twice would flip a DV01 sign in silence."""
    with pytest.raises(ValueError, match="both carry currency"):
        compute_forward_greeks(
            1_000_000.0,
            4_250.0,
            4_000.0,
            cop_curve(),
            cop_curve(),
            VALUATION_DATE,
            VALUATION_DATE + timedelta(days=365),
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"current_spot": 0.0}, "strictly positive"),
        ({"current_spot": -4_000.0}, "strictly positive"),
        ({"contract_forward_rate": 0.0}, "strictly positive"),
        ({"notional_usd": float("nan")}, "must be finite"),
        ({"days": 0}, "must strictly follow"),
        ({"days": -5}, "must strictly follow"),
    ],
)
def test_unusable_inputs_are_rejected(kwargs: dict[str, float], message: str) -> None:
    """Degenerate inputs raise rather than returning a plausible-looking zero."""
    arguments = {
        "notional_usd": 1_000_000.0,
        "contract_forward_rate": 4_250.0,
        "current_spot": 4_000.0,
        "days": 365,
    }
    arguments.update(kwargs)

    with pytest.raises(ValueError, match=message):
        compute_forward_greeks(
            arguments["notional_usd"],
            arguments["contract_forward_rate"],
            arguments["current_spot"],
            cop_curve(),
            usd_curve(),
            VALUATION_DATE,
            VALUATION_DATE + timedelta(days=int(arguments["days"])),
        )


def test_forward_value_rejects_a_maturity_before_valuation() -> None:
    """The pricing helper guards its own tenor, since callers bump it directly."""
    with pytest.raises(ValueError, match="precedes valuation_date"):
        forward_value(
            1_000_000.0,
            4_250.0,
            4_000.0,
            cop_curve(),
            usd_curve(),
            VALUATION_DATE,
            VALUATION_DATE - timedelta(days=1),
        )
