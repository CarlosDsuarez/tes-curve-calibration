"""Risk sensitivities: bond DV01/duration/convexity and FX option greeks.

Two families live here on purpose, because they answer the same question in two
markets and share the finite-difference machinery:

* **Rates.** DV01 is reported per 100 of face for a one basis point parallel
  shift. Analytic duration is only valid for a flat-yield reparameterisation, so
  the curve-consistent variants bump the NSS parameters instead.
* **FX.** Garman-Kohlhagen greeks, with the usual market scalings noted per
  function (delta per 1 USD notional, vega per volatility point, theta per day).
* **FX forwards.** Outright USD/COP forward greeks, reported in COP for the
  whole position. These are computed twice - once in closed form and once by
  bump-and-reprice - and the two are reconciled before anything is returned;
  see :func:`compute_forward_greeks`.

All functions are pure. Curve and parameter objects are supplied by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Final

import numpy as np
from numpy.typing import NDArray

from tes_pricer.math.bond_pricing import BondTerms
from tes_pricer.math.fx_forward import OptionType
from tes_pricer.math.nss_model import NSSParams
from tes_pricer.math.ois_curve import ShortRateCurve

BASIS_POINT = 1e-4


@dataclass(frozen=True, slots=True)
class BondRisk:
    """Rate sensitivities of a single bond position."""

    dv01: float
    """Price change per 100 of face for a 1bp parallel shift, sign-flipped so a
    long position reports a positive number."""
    macaulay_duration: float
    modified_duration: float
    convexity: float


@dataclass(frozen=True, slots=True)
class FXOptionGreeks:
    """Garman-Kohlhagen sensitivities, per 1 USD of notional."""

    price: float
    delta: float
    gamma: float
    vega: float
    """Per 1.00 of volatility; divide by 100 for a per-vol-point figure."""
    theta: float
    """Per year; divide by 365 for a per-calendar-day figure."""
    rho_domestic: float
    rho_foreign: float


def dv01_from_ytm(
    terms: BondTerms,
    settlement_date: date,
    ytm: float,
    *,
    bump: float = BASIS_POINT,
) -> float:
    """Central-difference DV01 under a flat-yield bump."""
    raise NotImplementedError("Phase 8: DV01 from YTM")


def dv01_from_curve(
    terms: BondTerms,
    settlement_date: date,
    params: NSSParams,
    *,
    bump: float = BASIS_POINT,
) -> float:
    """DV01 under a parallel shift of the whole NSS curve (``beta0`` bump).

    This is the number a curve-based book actually hedges on; the flat-yield
    variant above is kept for reconciliation against street quotes.
    """
    raise NotImplementedError("Phase 8: curve DV01")


def key_rate_durations(
    terms: BondTerms,
    settlement_date: date,
    params: NSSParams,
    key_tenors: NDArray[np.float64],
    *,
    bump: float = BASIS_POINT,
) -> NDArray[np.float64]:
    """Partial DV01s against bumps localised at each of ``key_tenors``."""
    raise NotImplementedError("Phase 8: key rate durations")


def bond_risk(
    terms: BondTerms,
    settlement_date: date,
    ytm: float,
) -> BondRisk:
    """Analytic Macaulay/modified duration, convexity and DV01 at a flat ``ytm``."""
    raise NotImplementedError("Phase 8: analytic bond risk")


def fx_option_greeks(
    spot: float,
    strike: float,
    tau: float,
    domestic_rate: float,
    foreign_rate: float,
    volatility: float,
    option_type: OptionType,
) -> FXOptionGreeks:
    """Analytic Garman-Kohlhagen greeks for a European USD/COP option."""
    raise NotImplementedError("Phase 8: FX option greeks")


def forward_delta(
    notional_usd: float,
    domestic_discount: float,
) -> float:
    """Spot-equivalent delta of an outright forward, in USD.

    A forward is not delta-one against spot: its spot sensitivity is discounted
    by the domestic (COP) discount factor to the settlement date.
    """
    raise NotImplementedError("Phase 8: forward delta")


# --------------------------------------------------------------------------- #
# Outright USD/COP forward greeks
# --------------------------------------------------------------------------- #

SPOT_BUMP_RELATIVE: Final = 1e-4
"""Spot bump for the finite differences, as a fraction of spot (1bp of spot).

Relative rather than absolute on purpose. USD/COP trades near 4,000 while
EUR/USD trades near 1.1; a fixed absolute bump that is well conditioned for one
is either noise-dominated or curvature-dominated for the other. One basis point
of spot is the desk convention and keeps the truncation/rounding trade-off in
the same place whatever the quote scale.
"""

DELTA_RECONCILIATION_TOLERANCE: Final = 1e-3
"""Maximum relative gap allowed between analytic and bumped delta (0.1%)."""

DAYS_PER_YEAR_ACT_365: Final = 365.0
"""ACT/365 fixed, the basis :class:`~tes_pricer.math.ois_curve.ShortRateCurve` uses."""

THETA_STEP = timedelta(days=1)
"""Theta is quoted per **calendar** day, matching the ACT/365 discounting."""


class GreeksReconciliationError(RuntimeError):
    """Two independent estimates of the same greek disagree.

    Raised instead of quietly returning the analytic number, because these two
    methods only diverge when one of them is wrong: either the closed form was
    mis-derived, or the pricing function it is supposed to differentiate does
    not price what the formula assumes. In both cases the analytic value is
    untrustworthy, and a risk number that is silently wrong is worse than no
    risk number at all - it gets hedged on.
    """

    def __init__(
        self,
        *,
        greek: str,
        analytic: float,
        numeric: float,
        relative_error: float,
        tolerance: float,
        context: str = "",
    ) -> None:
        self.greek = greek
        self.analytic = analytic
        self.numeric = numeric
        self.relative_error = relative_error
        self.tolerance = tolerance
        self.context = context
        detail = f" [{context}]" if context else ""
        super().__init__(
            f"{greek} failed reconciliation{detail}: analytic={analytic!r}, "
            f"bump-and-reprice={numeric!r}, relative error {relative_error:.6e} "
            f"exceeds the {tolerance:.3e} tolerance. The closed form and the "
            f"pricing function disagree, so one of them is wrong."
        )


@dataclass(frozen=True, slots=True)
class ForwardGreeks:
    """Sensitivities of an outright USD/COP forward, for the whole position.

    Every number is in **COP**, not per unit of notional, so they can be summed
    straight into a book-level risk report.

    Attributes:
        delta_spot: ``dV/dS``, COP of P&L per 1.00 COP move in the spot rate.
            Numerically equal to a USD amount, because ``S`` is COP per USD.
        gamma_spot: ``d2V/dS2``. Exactly ``0.0`` for a forward; see
            :attr:`notes`.
        dv01_cop: Value change for +1bp on the whole COP curve, in COP.
        dv01_usd: Value change for +1bp on the whole USD curve, in COP.
        theta: Value change from letting one calendar day pass with everything
            else held fixed, in COP per day. Carry plus roll-down.
        notes: Plain-language explanations of the results, including the
            reconciliation residual and any convention caveat that applies to
            this particular call. Read them before treating a zero as a bug.
    """

    delta_spot: float
    gamma_spot: float
    dv01_cop: float
    dv01_usd: float
    theta: float
    notes: list[str]


def _year_fraction_act365(start: date, end: date) -> float:
    """Year fraction on ACT/365 fixed.

    :func:`tes_pricer.math.day_count.year_fraction` is the eventual home for
    this, but it is still a Phase 3 stub that raises, so the arithmetic is
    inlined rather than importing a function that cannot run. ACT/365 is not a
    free choice here: it is the basis
    :class:`~tes_pricer.math.ois_curve.ShortRateCurve` discounts on, and mixing
    a different day count into the tenor would misprice the discount factors.
    """
    return (end - start).days / DAYS_PER_YEAR_ACT_365


def _bumped_curve(curve: ShortRateCurve, bump: float) -> ShortRateCurve:
    """Return ``curve`` with every pillar rate shifted by ``bump``.

    Shifting the pillars rather than the interpolated output keeps the shift
    parallel everywhere, including in the flat-extrapolated regions outside the
    pillar range, so the bumped curve is still a valid curve rather than a
    curve plus a patch. Validation runs again through ``__post_init__``.
    """
    return replace(curve, rates=curve.rates + bump)


def _relative_error(analytic: float, numeric: float) -> float:
    """Relative gap between two estimates, scaled by the larger magnitude.

    ``max(|a|, |n|)`` rather than ``|a|`` in the denominator so the measure
    stays symmetric and finite when the analytic value happens to be the one
    that collapsed to zero. Two exact zeros agree perfectly, and report ``0.0``.
    """
    scale = max(abs(analytic), abs(numeric))
    if scale == 0.0:
        return 0.0
    return abs(analytic - numeric) / scale


def _forward_value_at_tau(
    notional_usd: float,
    contract_forward_rate: float,
    spot: float,
    cop_curve: ShortRateCurve,
    usd_curve: ShortRateCurve,
    tau: float,
) -> float:
    r"""Present value in COP of a long outright USD/COP forward.

    Under covered interest parity the fair forward is
    :math:`F = S\,DF_{USD}(\tau)/DF_{COP}(\tau)`, and the contract pays
    :math:`N (S_T - K)` COP at :math:`T`, so

    .. math::

        V = N\,(F - K)\,DF_{COP}(\tau)
          = N\,\bigl(S\,DF_{USD}(\tau) - K\,DF_{COP}(\tau)\bigr).

    The second form is the one evaluated: it is algebraically identical and
    avoids dividing by :math:`DF_{COP}` only to multiply it back, which is what
    makes :math:`\partial^2 V/\partial S^2 = 0` hold to machine precision
    rather than approximately.

    Args:
        notional_usd: USD notional. Negative is a short (sold USD forward).
        contract_forward_rate: The struck rate ``K``, COP per USD.
        spot: Current spot ``S``, COP per USD.
        cop_curve: COP discount curve.
        usd_curve: USD discount curve.
        tau: Year fraction to settlement, ACT/365, non-negative.

    Returns:
        Present value in COP.
    """
    return notional_usd * (
        spot * usd_curve.discount_factor(tau)
        - contract_forward_rate * cop_curve.discount_factor(tau)
    )


def forward_value(
    notional_usd: float,
    contract_forward_rate: float,
    spot: float,
    cop_curve: ShortRateCurve,
    usd_curve: ShortRateCurve,
    valuation_date: date,
    maturity_date: date,
) -> float:
    """Date-based wrapper around :func:`_forward_value_at_tau`.

    Public because bump-and-reprice is only a check if the thing being bumped
    is the same function the desk prices on. Callers verifying a greek should
    reprice through this, not re-implement the payoff.

    Args:
        notional_usd: USD notional; negative is a short position.
        contract_forward_rate: Struck rate ``K``, COP per USD.
        spot: Current spot, COP per USD.
        cop_curve: COP discount curve.
        usd_curve: USD discount curve.
        valuation_date: Date the value is as of.
        maturity_date: Settlement date; must not precede ``valuation_date``.

    Returns:
        Present value in COP.

    Raises:
        ValueError: If ``maturity_date`` precedes ``valuation_date``.
    """
    tau = _year_fraction_act365(valuation_date, maturity_date)
    if tau < 0.0:
        raise ValueError(
            f"maturity_date {maturity_date.isoformat()} precedes valuation_date "
            f"{valuation_date.isoformat()}; an expired forward has no value to compute."
        )
    return _forward_value_at_tau(
        notional_usd, contract_forward_rate, spot, cop_curve, usd_curve, tau
    )


def _validate_forward_inputs(
    notional_usd: float,
    contract_forward_rate: float,
    current_spot: float,
    cop_curve: ShortRateCurve,
    usd_curve: ShortRateCurve,
    valuation_date: date,
    maturity_date: date,
) -> None:
    """Reject inputs that would make the greeks meaningless rather than wrong-looking.

    Raises:
        ValueError: On non-finite inputs, a non-positive spot or strike, a
            maturity that does not strictly follow the valuation date, or two
            curves carrying the same currency code.
    """
    for name, value in (
        ("notional_usd", notional_usd),
        ("contract_forward_rate", contract_forward_rate),
        ("current_spot", current_spot),
    ):
        if not np.isfinite(value):
            raise ValueError(f"{name} must be finite; got {value!r}.")

    if current_spot <= 0.0:
        raise ValueError(
            f"current_spot is COP per USD and must be strictly positive; got {current_spot!r}. "
            f"It also sets the finite-difference step, which would be degenerate at zero."
        )
    if contract_forward_rate <= 0.0:
        raise ValueError(
            f"contract_forward_rate is COP per USD and must be strictly positive; "
            f"got {contract_forward_rate!r}."
        )
    if maturity_date <= valuation_date:
        raise ValueError(
            f"maturity_date {maturity_date.isoformat()} must strictly follow valuation_date "
            f"{valuation_date.isoformat()}. A forward on or past its settlement date has no "
            f"remaining sensitivity, and theta would have to step past expiry."
        )
    if cop_curve.currency == usd_curve.currency:
        raise ValueError(
            f"cop_curve and usd_curve both carry currency {cop_curve.currency!r}. "
            f"That is almost always the same curve passed twice or the two arguments "
            f"swapped, which silently reports the wrong sign on both DV01s."
        )


def compute_forward_greeks(
    notional_usd: float,
    contract_forward_rate: float,
    current_spot: float,
    cop_curve: ShortRateCurve,
    usd_curve: ShortRateCurve,
    valuation_date: date,
    maturity_date: date,
) -> ForwardGreeks:
    r"""Greeks of an outright USD/COP forward, cross-checked two ways.

    Pricing
    -------
    With :math:`\tau` the ACT/365 year fraction to settlement,

    .. math::

        V = N\,\bigl(S\,DF_{USD}(\tau) - K\,DF_{COP}(\tau)\bigr) \quad [\text{COP}]

    Spot delta - two methods, reconciled
    ------------------------------------
    *Analytic.* Differentiating the line above,
    :math:`\partial V/\partial S = N\,DF_{USD}(\tau)`. Written in the
    carry-times-domestic-discount form the desk quotes,

    .. math::

        \Delta_S = N \cdot \underbrace{\frac{(1 + r_{COP})^{\tau}}
                                             {(1 + r_{USD})^{\tau}}}_{F/S}
                     \cdot DF_{COP}(\tau),

    which is what this function actually evaluates. **The compounding matters.**
    The textbook version of that carry factor is
    :math:`e^{(r_{COP} - r_{USD})\tau}`, but
    :class:`~tes_pricer.math.ois_curve.ShortRateCurve` is effective annual on
    ACT/365 (:math:`DF = (1+r)^{-\tau}`, never :math:`e^{-r\tau}`), so the
    exponential form would be wrong by roughly
    :math:`\tfrac{1}{2}(r_{COP}^2 - r_{USD}^2)\tau` - 0.397%, about 40bp of
    delta, at one year on a 10.1%/4.1% rate pair. That is not a rounding
    difference; it is a mispriced hedge.

    *Numeric.* Central difference with :math:`h = S \times 10^{-4}`:
    :math:`[V(S+h) - V(S-h)] / 2h`.

    The two are compared, and a relative gap above
    :data:`DELTA_RECONCILIATION_TOLERANCE` raises
    :class:`GreeksReconciliationError` rather than returning the analytic value.
    Reporting an unverified closed form is the failure mode this whole function
    exists to prevent.

    Gamma
    -----
    Exactly ``0.0``. :math:`V` is affine in :math:`S`, so the second derivative
    vanishes identically - this is a property of the payoff, not an
    approximation, and not a missing implementation. The bumped second
    difference is computed anyway and its residual is reported in
    :attr:`ForwardGreeks.notes`; it should be float noise.

    DV01
    ----
    Bump-and-reprice only, deliberately: a +1bp parallel shift of every pillar
    on one curve, repriced, differenced. Closed forms exist
    (:math:`\partial V/\partial r_{COP} = +N K \tau (1+r_{COP})^{-\tau-1}`,
    :math:`\partial V/\partial r_{USD} = -N S \tau (1+r_{USD})^{-\tau-1}`) but
    they are per-pillar-convention and go stale the moment the interpolation
    changes, whereas the bump follows whatever the curve object actually does.

    Signs, for a **long** position (:math:`N > 0`, bought USD forward): a long
    forward is long a USD zero and short a COP zero, so ``dv01_cop`` is
    **positive** (the COP you owe discounts harder) and ``dv01_usd`` is
    **negative** (the USD you receive discounts harder, dragging :math:`F`
    down). Both flip for a short.

    Theta
    -----
    One calendar day of :attr:`valuation_date`, maturity and curves held fixed.
    Because the curve is re-read at the shorter :math:`\tau`, this is carry
    *plus* roll-down, which is the number that actually shows up in overnight
    P&L. It stays finite as :math:`\tau \to 0`, where
    :math:`V \to N(S - K)`.

    Args:
        notional_usd: USD notional. Negative is a short (sold USD forward).
        contract_forward_rate: The struck rate ``K``, COP per USD.
        current_spot: Current spot ``S``, COP per USD.
        cop_curve: COP discount curve. Read as a tenor-to-rate mapping anchored
            at ``valuation_date``, so its ``curve_date`` is not required to
            match; theta could not be computed at all if it were, since that
            step moves the valuation date while holding the curves fixed. A
            mismatch is recorded in :attr:`ForwardGreeks.notes`.
        usd_curve: USD discount curve, same treatment.
        valuation_date: Date the greeks are as of.
        maturity_date: Settlement date; must strictly follow ``valuation_date``.

    Returns:
        The :class:`ForwardGreeks`, all in COP for the full notional.

    Raises:
        ValueError: If the inputs are non-finite, the spot or strike is
            non-positive, the maturity does not strictly follow the valuation
            date, or both curves carry the same currency code.
        GreeksReconciliationError: If the analytic and bumped spot deltas
            disagree by more than :data:`DELTA_RECONCILIATION_TOLERANCE`.
    """
    _validate_forward_inputs(
        notional_usd,
        contract_forward_rate,
        current_spot,
        cop_curve,
        usd_curve,
        valuation_date,
        maturity_date,
    )

    tau = _year_fraction_act365(valuation_date, maturity_date)
    notes: list[str] = []

    def price(
        *,
        spot: float = current_spot,
        cop: ShortRateCurve = cop_curve,
        usd: ShortRateCurve = usd_curve,
        remaining: float = tau,
    ) -> float:
        return _forward_value_at_tau(
            notional_usd, contract_forward_rate, spot, cop, usd, remaining
        )

    base_value = price()

    # -- Method 1: closed form ---------------------------------------------- #
    rate_cop = cop_curve.rate(tau)
    rate_usd = usd_curve.rate(tau)
    carry = ((1.0 + rate_cop) / (1.0 + rate_usd)) ** tau
    delta_analytic = notional_usd * carry * cop_curve.discount_factor(tau)
    gamma_analytic = 0.0

    # -- Method 2: bump and reprice ----------------------------------------- #
    step = current_spot * SPOT_BUMP_RELATIVE
    value_up = price(spot=current_spot + step)
    value_down = price(spot=current_spot - step)
    delta_numeric = (value_up - value_down) / (2.0 * step)
    gamma_numeric = (value_up - 2.0 * base_value + value_down) / (step * step)

    # -- Reconciliation ------------------------------------------------------ #
    delta_gap = _relative_error(delta_analytic, delta_numeric)
    if delta_gap > DELTA_RECONCILIATION_TOLERANCE:
        raise GreeksReconciliationError(
            greek="delta_spot",
            analytic=delta_analytic,
            numeric=delta_numeric,
            relative_error=delta_gap,
            tolerance=DELTA_RECONCILIATION_TOLERANCE,
            context=(
                f"S={current_spot!r}, K={contract_forward_rate!r}, tau={tau!r}, "
                f"bump={step!r}"
            ),
        )

    # -- Rate and time sensitivities ----------------------------------------- #
    dv01_cop = price(cop=_bumped_curve(cop_curve, BASIS_POINT)) - base_value
    dv01_usd = price(usd=_bumped_curve(usd_curve, BASIS_POINT)) - base_value
    tau_tomorrow = _year_fraction_act365(valuation_date + THETA_STEP, maturity_date)
    theta = price(remaining=tau_tomorrow) - base_value

    notes.append(
        f"delta_spot reconciled: analytic {delta_analytic:.6f} vs bump-and-reprice "
        f"{delta_numeric:.6f} (relative gap {delta_gap:.3e}, tolerance "
        f"{DELTA_RECONCILIATION_TOLERANCE:.1e}) using h = {step:.6f} COP, "
        f"{SPOT_BUMP_RELATIVE:.0e} of spot."
    )
    notes.append(
        "gamma_spot=0 is expected, not a bug: V = N*(S*DF_usd - K*DF_cop) is affine "
        "in S, so the second derivative vanishes identically for any linear payoff. "
        f"The bumped second difference came out at {gamma_numeric:.3e}, which is "
        "floating-point cancellation noise, not curvature."
    )
    notes.append(
        "Compounding is effective annual on ACT/365 (DF = (1+r)^-tau), so the carry "
        f"factor is ((1+r_cop)/(1+r_usd))^tau = {carry:.8f}, not exp((r_cop-r_usd)*tau) "
        f"= {float(np.exp((rate_cop - rate_usd) * tau)):.8f}. Using the exponential form "
        "against this curve would misprice delta."
    )
    direction = "long" if notional_usd > 0 else "short"
    notes.append(
        f"DV01s are bump-and-reprice on a +1bp parallel shift of every pillar, in COP. "
        f"This {direction} position is {'short' if notional_usd > 0 else 'long'} the COP "
        f"zero and {'long' if notional_usd > 0 else 'short'} the USD zero, so dv01_cop "
        f"({dv01_cop:+.2f}) and dv01_usd ({dv01_usd:+.2f}) carry opposite signs."
    )
    notes.append(
        f"theta is one calendar day of decay with maturity and curves fixed: tau goes "
        f"{tau:.6f} -> {tau_tomorrow:.6f}. It is carry plus roll-down, because the curve "
        f"is re-read at the shorter tenor. It stays finite into expiry, where "
        f"V -> N*(S-K) = {notional_usd * (current_spot - contract_forward_rate):.2f} COP."
    )
    for label, curve in (("cop_curve", cop_curve), ("usd_curve", usd_curve)):
        if curve.curve_date != valuation_date:
            notes.append(
                f"{label}.curve_date is {curve.curve_date.isoformat()} but the valuation "
                f"date is {valuation_date.isoformat()}; the curve was used as a "
                f"tenor-to-rate mapping anchored at the valuation date. Intended during "
                f"the theta step, worth checking otherwise."
            )

    return ForwardGreeks(
        delta_spot=delta_analytic,
        gamma_spot=gamma_analytic,
        dv01_cop=dv01_cop,
        dv01_usd=dv01_usd,
        theta=theta,
        notes=notes,
    )
