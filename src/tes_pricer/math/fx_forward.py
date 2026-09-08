r"""USD/COP forward pricing under covered interest parity, plus the FX option leg.

Quoting convention
------------------
**COP per USD** throughout, so USD is the *foreign* (base, asset) currency and
COP the *domestic* (quote, numeraire) currency. A forward above spot therefore
means more COP are needed per USD in the future than today.

Compounding convention - declared, not left implicit
----------------------------------------------------
**Effective annual (E.A.) on ACT/365**, the same basis as
:class:`~tes_pricer.math.ois_curve.ShortRateCurve`:

.. math::

    F(T) = S\,\frac{(1 + r_{COP})^{T}}{(1 + r_{USD})^{T}}
         = S\,\frac{DF_{USD}(T)}{DF_{COP}(T)},
    \qquad DF(T) = (1 + r)^{-T}

and **not** :math:`F = S\,e^{(z_{COP} - z_{USD})T}`.

The reason is that :func:`price_fx_forward` reads its rates off
:class:`~tes_pricer.math.ois_curve.ShortRateCurve` objects, and that class is
effective-annual ACT/365 by construction, because that is the unit Banco de la
Republica publishes IBR in ("Tasa efectiva, base 365"). Feeding an E.A. rate
into a continuous formula is not a rounding difference: at 10% E.A. over one
year it misprices the leg by about 46 bp of rate
(:math:`\ln 1.10 = 9.531\%` versus :math:`10\%`), which on a 4000 COP spot is
roughly 18 COP of forward - a level a trader would spot instantly and an
automated system would happily carry into a P&L.

:mod:`tes_pricer.math.nss_model` is the other convention in this project: its
zero curve is **continuously compounded**. The two never mix implicitly. Convert
explicitly at the boundary:

* continuous to effective annual: ``r = exp(z) - 1``
* effective annual to continuous: ``z = log(1 + r)``

The Garman-Kohlhagen block at the bottom of this module is the one place that
deliberately takes *continuous* rates, because Black-Scholes-style pricing is
written in that basis; convert before calling it.

Forward points
--------------
This module follows the project convention ``points = (F - S) * 10000``, exposed
as :data:`FORWARD_POINTS_SCALE`. Worth knowing when reconciling against a
broker screen: COP desks commonly quote USD/COP points in plain COP (scale 1)
or in thousandths, so a quote that looks four orders of magnitude off is a
scale mismatch rather than a pricing error. :func:`forward_points` keeps
``scale`` as an argument for exactly that reason.

Both curves arrive already bootstrapped: this module never fetches a rate.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

import numpy as np
from numpy.typing import NDArray

from tes_pricer.math.day_count import DayCount
from tes_pricer.math.ois_curve import ShortRateCurve

FORWARD_POINTS_SCALE: Final = 10_000.0
"""Market-convention multiplier applied to ``F - S`` to quote forward points."""

_DAY_COUNT_DENOMINATORS: Final[dict[DayCount, float]] = {
    DayCount.ACT_365: 365.0,
    DayCount.ACT_360: 360.0,
}
"""Fixed-denominator conventions this module can compute without a calendar.

Deliberately a local table rather than a call to
:func:`tes_pricer.math.day_count.year_fraction`: that function is a Phase 3
stub, and ``ACT/ACT-ISDA`` and ``30/360`` need date arithmetic that belongs
there, not here. They are rejected with a message saying so instead of being
silently approximated.
"""

_RATE_SIGN_TOLERANCE: Final = 1e-12
"""Below this, a rate differential counts as zero for the sign sanity check."""

_POINTS_SIGN_TOLERANCE: Final = 1e-6
"""Below this, forward points count as zero for the sign sanity check."""


class OptionType(StrEnum):
    """European option payoff direction, on the USD/COP (COP per USD) rate."""

    CALL = "CALL"
    PUT = "PUT"


class ForwardPointsSignWarning(UserWarning):
    """Forward points disagree in sign with the interest rate differential.

    Under CIP the two signs are the *same* quantity seen twice, so a
    disagreement is never a market fact: it is a sign error in the formula, a
    pair of curves passed the wrong way round, or an inverted quoting
    convention. Its own class so a caller can escalate it
    (``warnings.simplefilter("error", ForwardPointsSignWarning)``) in a batch
    job, where nobody is reading stderr.
    """


class CurveConsistencyWarning(UserWarning):
    """A curve handed to the pricer does not match the leg it was passed as.

    Covers the currency label and the curve's observation date. Neither is
    fatal - a synthetic curve in a test is legitimately unlabelled - but a USD
    curve passed as the COP leg produces a forward that is internally
    consistent and economically backwards, which the sign check cannot catch
    because both rates get swapped together.
    """


@dataclass(frozen=True, slots=True)
class FXForwardQuote:
    """A single outright USD/COP forward and the inputs that produced it.

    The sign sanity check runs in :meth:`__post_init__` rather than only inside
    :func:`price_fx_forward`, so every quote is checked no matter how it was
    built.

    Attributes:
        spot_rate: ``S``, COP per 1 USD.
        forward_rate: ``F``, COP per 1 USD.
        forward_points: ``(F - S) * 10000``, the market points convention.
        tenor_years: ``T``, the year fraction the rates were read at.
        r_cop: Domestic (COP) rate used, effective annual as a decimal.
        r_usd: Foreign (USD) rate used, effective annual as a decimal.
        valuation_date: Spot / curve observation date.
        maturity_date: Delivery date of the forward.

    Warns:
        ForwardPointsSignWarning: If ``sign(forward_points)`` disagrees with
            ``sign(r_cop - r_usd)``.
    """

    spot_rate: float
    forward_rate: float
    forward_points: float
    tenor_years: float
    r_cop: float
    r_usd: float
    valuation_date: date
    maturity_date: date

    def __post_init__(self) -> None:
        """Validate the quote and run the forward-points sign sanity check.

        Raises:
            ValueError: If any numeric field is non-finite, if ``spot_rate`` or
                ``forward_rate`` is non-positive, or if ``tenor_years`` is
                negative.
        """
        numeric = {
            "spot_rate": self.spot_rate,
            "forward_rate": self.forward_rate,
            "forward_points": self.forward_points,
            "tenor_years": self.tenor_years,
            "r_cop": self.r_cop,
            "r_usd": self.r_usd,
        }
        for name, value in numeric.items():
            if not np.isfinite(value):
                raise ValueError(f"{name} must be finite; got {value}.")
        if self.spot_rate <= 0.0:
            raise ValueError(f"spot_rate must be positive COP per USD; got {self.spot_rate}.")
        if self.forward_rate <= 0.0:
            raise ValueError(f"forward_rate must be positive COP per USD; got {self.forward_rate}.")
        if self.tenor_years < 0.0:
            raise ValueError(f"tenor_years must be non-negative; got {self.tenor_years}.")

        check_forward_points_sign(
            self.forward_points,
            self.r_cop,
            self.r_usd,
            tenor_years=self.tenor_years,
        )


def check_forward_points_sign(
    points: float,
    r_cop: float,
    r_usd: float,
    *,
    tenor_years: float,
    rate_tolerance: float = _RATE_SIGN_TOLERANCE,
    points_tolerance: float = _POINTS_SIGN_TOLERANCE,
) -> bool:
    """Check that forward points and the rate differential carry the same sign.

    Covered interest parity makes this an identity, not a market observation:
    for ``T > 0``, ``F - S`` and ``r_cop - r_usd`` are positive together, zero
    together and negative together. A violation therefore means the *code* is
    wrong - an inverted ratio, swapped curves, or a forward quoted USD per COP
    while the rates are labelled the other way round. Colombia's structural
    rate differential over the United States keeps the real-world case firmly
    in the ``r_cop > r_usd``, positive-points regime, which is why a negative
    print is worth shouting about.

    ``tenor_years == 0`` short-circuits: no time, no carry, no points, whatever
    the differential.

    Args:
        points: Forward points, ``(F - S) * FORWARD_POINTS_SCALE``.
        r_cop: Domestic rate used, effective annual.
        r_usd: Foreign rate used, effective annual.
        tenor_years: Year fraction the forward was priced at.
        rate_tolerance: Below this magnitude the differential counts as zero.
        points_tolerance: Below this magnitude the points count as zero.

    Returns:
        ``True`` if the signs agree (or the tenor is zero), ``False`` otherwise.

    Warns:
        ForwardPointsSignWarning: When the signs disagree.
    """
    if tenor_years <= 0.0:
        return True

    rate_differential = r_cop - r_usd
    expected_sign = (
        0 if abs(rate_differential) <= rate_tolerance else int(np.sign(rate_differential))
    )
    observed_sign = 0 if abs(points) <= points_tolerance else int(np.sign(points))
    if expected_sign == observed_sign:
        return True

    warnings.warn(
        f"FORWARD POINTS SIGN CHECK FAILED: rate differential r_cop - r_usd = "
        f"{rate_differential:+.6%} implies forward points of sign {expected_sign:+d}, "
        f"but the quote carries {points:+.6f} points (sign {observed_sign:+d}) over "
        f"{tenor_years:.6f} years. Under covered interest parity these two signs are "
        f"the same fact, so this is an implementation error - an inverted "
        f"(1 + r_cop) / (1 + r_usd) ratio, the COP and USD curves passed the wrong "
        f"way round, or a forward quoted in USD per COP while the rates are labelled "
        f"COP per USD - not a market condition. Do not trade or book this number.",
        ForwardPointsSignWarning,
        stacklevel=3,
    )
    return False


def implied_forward_points(spot: float, forward: float) -> float:
    """Forward points from an outright: ``(F - S) * 10000``.

    Positive points mean COP trades at a discount forward relative to spot -
    more COP are required per USD in the future than today - which is what a
    domestic rate above the foreign rate implies, and therefore the structural
    case for USD/COP given the Colombia-versus-United States differential.
    Use :func:`check_forward_points_sign` to assert that against the rates that
    produced the forward.

    Args:
        spot: ``S``, COP per USD.
        forward: ``F``, COP per USD.

    Returns:
        The forward points on the :data:`FORWARD_POINTS_SCALE` convention.

    Raises:
        ValueError: If either input is non-finite or non-positive.
    """
    if not np.isfinite(spot) or not np.isfinite(forward):
        raise ValueError(f"spot and forward must be finite; got {spot} and {forward}.")
    if spot <= 0.0 or forward <= 0.0:
        raise ValueError(
            f"spot and forward are COP per USD and must be positive; got {spot} and {forward}."
        )
    return (float(forward) - float(spot)) * FORWARD_POINTS_SCALE


def implied_forward_rate_from_points(spot: float, points: float) -> float:
    """Rebuild an outright forward from a points quote.

    Exact inverse of :func:`implied_forward_points`.

    Args:
        spot: ``S``, COP per USD.
        points: Forward points on the :data:`FORWARD_POINTS_SCALE` convention.

    Returns:
        The outright forward in COP per USD.

    Raises:
        ValueError: If the inputs are non-finite, ``spot`` is non-positive, or
            the points imply a non-positive forward.
    """
    if not np.isfinite(spot) or not np.isfinite(points):
        raise ValueError(f"spot and points must be finite; got {spot} and {points}.")
    if spot <= 0.0:
        raise ValueError(f"spot is COP per USD and must be positive; got {spot}.")
    forward = float(spot) + float(points) / FORWARD_POINTS_SCALE
    if forward <= 0.0:
        raise ValueError(
            f"{points} points against a spot of {spot} implies a non-positive forward "
            f"of {forward}; check the points scale (this module uses "
            f"{FORWARD_POINTS_SCALE:g})."
        )
    return forward


def _year_fraction(valuation_date: date, maturity_date: date, day_count: str) -> float:
    """Year fraction between two dates on a fixed-denominator convention.

    Args:
        valuation_date: Start date, inclusive.
        maturity_date: End date, exclusive.
        day_count: ``"ACT/365"`` or ``"ACT/360"``.

    Returns:
        ``(maturity_date - valuation_date).days / denominator``.

    Raises:
        ValueError: If ``day_count`` is unknown or needs calendar arithmetic
            this module does not own, or if ``maturity_date`` precedes
            ``valuation_date``.
    """
    try:
        convention = DayCount(day_count)
    except ValueError as exc:
        supported = ", ".join(sorted(c.value for c in _DAY_COUNT_DENOMINATORS))
        raise ValueError(f"Unknown day count {day_count!r}; supported here: {supported}.") from exc

    denominator = _DAY_COUNT_DENOMINATORS.get(convention)
    if denominator is None:
        supported = ", ".join(sorted(c.value for c in _DAY_COUNT_DENOMINATORS))
        raise ValueError(
            f"{convention.value} needs calendar-aware accrual, which belongs in "
            f"tes_pricer.math.day_count.year_fraction (a Phase 3 stub) rather than in "
            f"an approximation here. Supported in this module: {supported}."
        )

    days = (maturity_date - valuation_date).days
    if days < 0:
        raise ValueError(
            f"maturity_date {maturity_date.isoformat()} precedes valuation_date "
            f"{valuation_date.isoformat()}."
        )
    return days / denominator


def _warn_on_curve_mismatch(
    curve: ShortRateCurve,
    expected_currency: str,
    valuation_date: date,
) -> None:
    """Warn when a curve's currency label or observation date looks wrong."""
    if curve.currency != expected_currency:
        warnings.warn(
            f"The {expected_currency} leg was given a curve labelled "
            f"{curve.currency!r}. If the COP and USD curves were swapped, the forward "
            f"will still be internally consistent and economically inverted, and the "
            f"forward-points sign check cannot detect it because both rates move "
            f"together.",
            CurveConsistencyWarning,
            stacklevel=3,
        )
    if curve.curve_date != valuation_date:
        warnings.warn(
            f"The {curve.currency} curve is dated {curve.curve_date.isoformat()} but the "
            f"forward is being priced as of {valuation_date.isoformat()}; the rates are "
            f"stale relative to the spot.",
            CurveConsistencyWarning,
            stacklevel=3,
        )


def price_fx_forward(
    spot_rate: float,
    cop_curve: ShortRateCurve,
    usd_curve: ShortRateCurve,
    valuation_date: date,
    maturity_date: date,
    day_count: str = "ACT/365",
) -> FXForwardQuote:
    r"""Price a USD/COP outright forward by covered interest parity.

    **Compounding convention: effective annual (E.A.), ACT/365.** That is:

    .. math::

        F = S\,\frac{(1 + r_{COP})^{T}}{(1 + r_{USD})^{T}}

    and *not* the continuous form :math:`F = S\,e^{(z_{COP} - z_{USD})T}`.

    Why E.A. is the consistent choice here: the rates come from
    :class:`~tes_pricer.math.ois_curve.ShortRateCurve`, whose Phase 5
    interpolation is defined on effective annual ACT/365 rates because Banco de
    la Republica publishes IBR that way ("Tasa efectiva, base 365"), and whose
    :meth:`~tes_pricer.math.ois_curve.ShortRateCurve.discount_factor` is
    :math:`(1 + r)^{-\tau}`. The formula above is exactly the discount-factor
    ratio :math:`S \cdot DF_{USD}(T) / DF_{COP}(T)` on that basis, and this
    function computes it that way - through the curve's own discount factors -
    so the convention cannot drift apart from the curve it reads.

    :mod:`tes_pricer.math.nss_model` is continuously compounded, and its output
    must be converted (``r = exp(z) - 1``) before it is handed to a
    :class:`ShortRateCurve` and priced here. Mixing the two silently costs
    ~46 bp of rate at a 10% level, which is why the convention is stated in
    three places rather than one.

    ``T`` uses the same ``day_count`` for both legs. Keep it at ``"ACT/365"``:
    the curves interpolate on ACT/365 tenors, so an ACT/360 ``T`` would read
    both curves at the wrong point on their own axis.

    Args:
        spot_rate: ``S``, COP per 1 USD, positive.
        cop_curve: Domestic COP curve, effective annual.
        usd_curve: Foreign USD curve, effective annual.
        valuation_date: Spot date the curves are observed at.
        maturity_date: Delivery date, on or after ``valuation_date``.
        day_count: ``"ACT/365"`` (default) or ``"ACT/360"``.

    Returns:
        The :class:`FXForwardQuote`, with the rates actually read off each curve.

    Raises:
        ValueError: If ``spot_rate`` is not a positive finite number, if
            ``maturity_date`` precedes ``valuation_date``, or if ``day_count``
            is unsupported.

    Warns:
        CurveConsistencyWarning: If a curve's currency label or observation
            date does not match the leg it was passed as.
        ForwardPointsSignWarning: If the resulting points contradict the rate
            differential (raised from :class:`FXForwardQuote`).
    """
    if not np.isfinite(spot_rate):
        raise ValueError(f"spot_rate must be finite; got {spot_rate}.")
    if spot_rate <= 0.0:
        raise ValueError(f"spot_rate is COP per USD and must be positive; got {spot_rate}.")

    tau = _year_fraction(valuation_date, maturity_date, day_count)
    _warn_on_curve_mismatch(cop_curve, "COP", valuation_date)
    _warn_on_curve_mismatch(usd_curve, "USD", valuation_date)

    r_cop = cop_curve.interpolated_rate(tau)
    r_usd = usd_curve.interpolated_rate(tau)

    forward = float(
        forward_rate_cip(
            spot_rate,
            np.array([cop_curve.discount_factor(tau)], dtype=np.float64),
            np.array([usd_curve.discount_factor(tau)], dtype=np.float64),
        )[0]
    )

    return FXForwardQuote(
        spot_rate=float(spot_rate),
        forward_rate=forward,
        forward_points=implied_forward_points(spot_rate, forward),
        tenor_years=tau,
        r_cop=r_cop,
        r_usd=r_usd,
        valuation_date=valuation_date,
        maturity_date=maturity_date,
    )


def forward_rate_cip(
    spot: float,
    domestic_discount: NDArray[np.float64],
    foreign_discount: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Outright USD/COP forwards implied by two discount curves.

    The discount-factor form of covered interest parity,
    ``F = S * DF_foreign / DF_domestic``, which is compounding-agnostic: it
    holds whatever basis produced the discount factors, as long as *both* were
    produced by the same one. :func:`price_fx_forward` is the dated wrapper
    that reads those discount factors off effective annual curves.

    Args:
        spot: Spot rate in COP per USD.
        domestic_discount: COP discount factors at each tenor.
        foreign_discount: USD discount factors at the same tenors.

    Returns:
        The outright forwards, COP per USD, elementwise.

    Raises:
        ValueError: If ``spot`` is not positive and finite, if the two discount
            arrays differ in shape, or if any discount factor is not strictly
            positive and finite.
    """
    if not np.isfinite(spot) or spot <= 0.0:
        raise ValueError(f"spot is COP per USD and must be positive and finite; got {spot}.")

    domestic = np.asarray(domestic_discount, dtype=np.float64)
    foreign = np.asarray(foreign_discount, dtype=np.float64)
    if domestic.shape != foreign.shape:
        raise ValueError(
            f"domestic_discount and foreign_discount must have the same shape; "
            f"got {domestic.shape} and {foreign.shape}."
        )
    for name, values in (("domestic_discount", domestic), ("foreign_discount", foreign)):
        if not np.all(np.isfinite(values)) or np.any(values <= 0.0):
            raise ValueError(f"{name} must be strictly positive and finite; got {values.tolist()}.")

    return np.asarray(float(spot) * foreign / domestic, dtype=np.float64)


def forward_points(
    spot: float,
    forward: NDArray[np.float64],
    *,
    scale: float = FORWARD_POINTS_SCALE,
) -> NDArray[np.float64]:
    """Forward points ``(forward - spot) * scale``, vectorised.

    ``scale`` exists because desks quote USD/COP points in different multiples;
    :data:`FORWARD_POINTS_SCALE` is this project's convention, and ``1.0``
    gives raw COP.

    Args:
        spot: Spot rate in COP per USD.
        forward: Outright forwards in COP per USD.
        scale: Points multiplier.

    Returns:
        The forward points, elementwise.

    Raises:
        ValueError: If ``spot`` or ``scale`` is not finite, or ``spot`` is
            non-positive.
    """
    if not np.isfinite(spot) or spot <= 0.0:
        raise ValueError(f"spot is COP per USD and must be positive and finite; got {spot}.")
    if not np.isfinite(scale):
        raise ValueError(f"scale must be finite; got {scale}.")
    return np.asarray((np.asarray(forward, dtype=np.float64) - float(spot)) * float(scale))


def basis_spread(
    spot: float,
    market_forward: NDArray[np.float64],
    domestic_discount: NDArray[np.float64],
    foreign_discount: NDArray[np.float64],
    tau: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Cross-currency basis: the spread that reconciles CIP with the traded forward.

    A persistently non-zero basis is not a bug in the curves; it is the price of
    USD funding, and it is exactly the quantity this project is meant to measure.
    """
    raise NotImplementedError("Phase 8: cross-currency basis")


def garman_kohlhagen_price(
    spot: float,
    strike: float,
    tau: float,
    domestic_rate: float,
    foreign_rate: float,
    volatility: float,
    option_type: OptionType,
) -> float:
    """European FX option price in COP, per 1 USD of notional.

    Rates are continuously compounded - the one place in this module that is
    *not* effective annual, because Black-Scholes-style pricing is written in
    that basis. Convert an E.A. curve rate with ``z = log(1 + r)`` first.
    ``volatility`` is the annualised lognormal vol of the COP-per-USD rate.
    """
    raise NotImplementedError("Phase 8: Garman-Kohlhagen pricing")


def gk_d1_d2(
    spot: float,
    strike: float,
    tau: float,
    domestic_rate: float,
    foreign_rate: float,
    volatility: float,
) -> tuple[float, float]:
    """Return the Garman-Kohlhagen ``(d1, d2)`` terms shared by price and greeks."""
    raise NotImplementedError("Phase 8: Garman-Kohlhagen d1/d2")


def implied_volatility(
    price: float,
    spot: float,
    strike: float,
    tau: float,
    domestic_rate: float,
    foreign_rate: float,
    option_type: OptionType,
    *,
    tolerance: float = 1e-8,
    max_iterations: int = 100,
) -> float:
    """Invert :func:`garman_kohlhagen_price` for the lognormal volatility."""
    raise NotImplementedError("Phase 8: FX implied volatility")


def _standard_normal_cdf(x: NDArray[np.float64]) -> NDArray[np.float64]:
    """Vectorised standard normal CDF used by the pricing formulas."""
    raise NotImplementedError("Phase 8: normal CDF helper")
