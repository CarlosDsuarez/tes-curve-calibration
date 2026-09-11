"""Clean/dirty pricing, accrued interest and yield to maturity for fixed-rate TES.

Every function is a pure transformation of an already validated cash flow
schedule. Nothing here fetches a price: the caller supplies the schedule and the
market quote.

Sign and quoting conventions used throughout:

* Prices are expressed per unit of the ``face_value`` handed in; pass
  ``face_value=100.0`` to work on the per-100 scale quoted on the SEN/MEC
  screens.
* ``ytm`` is a **nominal annual rate compounded ``frequency`` times a year**:
  the discount factor over one coupon period is ``1 / (1 + ytm / frequency)``.
  For ``frequency == 1`` this coincides with the effective annual rate of the
  Colombian street convention; for ``frequency == 2`` it is a bond-equivalent
  yield, not an effective annual one.
* ``dirty = clean + accrued``.

Two families of entry points live here:

* the **explicit** functions (:func:`accrued_interest`,
  :func:`dirty_price_from_ytm`, :func:`clean_price`,
  :func:`ytm_from_dirty_price`) take loose scalars and dates, which is what the
  Excel bridge and the test suite need; and
* the **terms-based** adapters (:func:`accrued_interest_from_terms`,
  :func:`clean_price_from_ytm`, :func:`yield_to_maturity`) which take a
  :class:`BondTerms` and delegate to the explicit ones.

Day counts are resolved locally rather than through
:mod:`tes_pricer.math.day_count`, whose ``year_fraction`` is still a Phase 3
stub. The two must be reconciled when Phase 3 lands; the conventions
implemented here are deliberately the same three strings the
:class:`~tes_pricer.math.day_count.DayCount` enum spells.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from scipy.optimize import brentq

from tes_pricer.math.day_count import DayCount

__all__ = [
    "BondTerms",
    "YTMConvergenceError",
    "accrued_interest",
    "accrued_interest_from_terms",
    "clean_price",
    "clean_price_from_ytm",
    "dirty_price_from_ytm",
    "generate_cashflow_schedule",
    "price_from_discount_factors",
    "yield_to_maturity",
    "ytm_from_dirty_price",
]

# Day count conventions this module knows how to accrue over. ACT/ACT-ISDA is
# deliberately absent: its accrual splits a period across leap and non-leap
# years, which the single-ratio formula below cannot express.
_SUPPORTED_DAY_COUNTS = frozenset({DayCount.ACT_365, DayCount.ACT_360, DayCount.THIRTY_360})

# Annual basis each convention divides its accrual days by when a year fraction
# is needed on its own, as opposed to the period ratios used for accrual.
_ANNUAL_BASIS_DAYS = {
    DayCount.ACT_365: 365.0,
    DayCount.ACT_360: 360.0,
    DayCount.THIRTY_360: 360.0,
}

# Coupon frequencies that divide the year into whole months.
_SUPPORTED_FREQUENCIES = frozenset({1, 2, 4, 12})

_MONTHS_IN_YEAR = 12

# Bracket for the YTM solver: 0.01% to 50% nominal annual. Wide enough to hold
# a Colombian rate-stress scenario and still exclude the (economically
# meaningless) pole at ytm = -frequency.
YTM_LOWER_BOUND = 0.0001
YTM_UPPER_BOUND = 0.50

# Guard against an unbounded backward roll if a caller passes an absurd
# settlement date. 12 000 periods is 3 000 years of quarterly coupons.
_MAX_COUPON_PERIODS = 12_000


class YTMConvergenceError(ValueError):
    """The yield solver could not bracket or converge to a root.

    Subclasses :class:`ValueError` so that callers written against the older
    ``Raises: ValueError`` contract keep working, while callers that care about
    the difference can catch this type specifically.
    """


@dataclass(frozen=True, slots=True)
class BondTerms:
    """Static description of a fixed-rate bullet bond."""

    isin: str
    issue_date: date
    maturity_date: date
    coupon_rate: float
    """Annual coupon rate as a decimal, e.g. ``0.0725`` for 7.25%."""
    face_value: float = 100.0
    coupon_frequency: int = 1
    """Coupons per year. TES tasa fija pay annually."""
    day_count: DayCount = DayCount.ACT_365


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _normalise_day_count(day_count: str | DayCount) -> DayCount:
    """Coerce a convention label to a :class:`DayCount`, rejecting the unsupported."""
    try:
        convention = DayCount(str(day_count).upper())
    except ValueError:
        supported = ", ".join(sorted(str(c) for c in _SUPPORTED_DAY_COUNTS))
        raise ValueError(f"unknown day count {day_count!r}; supported: {supported}") from None
    if convention not in _SUPPORTED_DAY_COUNTS:
        supported = ", ".join(sorted(str(c) for c in _SUPPORTED_DAY_COUNTS))
        raise ValueError(f"day count {convention} is not supported here; supported: {supported}")
    return convention


def _validate_frequency(frequency: int) -> int:
    """Return ``frequency`` if it divides the year into whole months."""
    if frequency not in _SUPPORTED_FREQUENCIES:
        supported = ", ".join(str(f) for f in sorted(_SUPPORTED_FREQUENCIES))
        raise ValueError(f"coupon frequency must be one of {supported}; got {frequency!r}")
    return frequency


def _thirty_360_days(start: date, end: date) -> int:
    """Accrued days under 30/360 Bond Basis (ISDA 2006 definitions, 4.16(f)).

    This is the US/SIA "bond basis" adjustment, **not** 30E/360 (European): the
    end-of-month truncation of ``D2`` is conditional on ``D1`` having already
    been truncated, which is what makes a 31st-to-31st period 30 days but a
    30th-to-31st period 31 days.
    """
    d1, d2 = start.day, end.day
    if d1 == 31:
        d1 = 30
    if d2 == 31 and d1 == 30:
        d2 = 30
    return 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)


def _accrual_days(start: date, end: date, convention: DayCount) -> int:
    """Numerator of the accrual factor between ``start`` and ``end``, in days.

    ACT/365 and ACT/360 share this numerator; they differ only in the annual
    basis, which cancels out of every ratio computed in this module (see
    :func:`accrued_interest`).
    """
    if convention is DayCount.THIRTY_360:
        return _thirty_360_days(start, end)
    return (end - start).days


def _add_months(anchor: date, months: int) -> date:
    """Shift ``anchor`` by whole ``months``, clamping to the end of the target month."""
    total = anchor.month - 1 + months
    year = anchor.year + total // _MONTHS_IN_YEAR
    month = total % _MONTHS_IN_YEAR + 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def _coupon_schedule(
    settlement_date: date,
    maturity_date: date,
    frequency: int,
) -> tuple[date, list[date]]:
    """Roll the coupon schedule backwards from maturity.

    Every date is generated as an offset from ``maturity_date`` rather than from
    the previously generated date, so a month-end maturity such as 31 August
    cannot drift to the 28th after passing through February.

    Returns:
        ``(previous_coupon_date, remaining_payment_dates)`` where
        ``previous_coupon_date <= settlement_date`` is the start of the accrual
        period that contains settlement, and ``remaining_payment_dates`` is in
        ascending order and strictly after ``settlement_date``.
    """
    step = _MONTHS_IN_YEAR // frequency
    descending: list[date] = []
    for k in range(_MAX_COUPON_PERIODS):
        current = _add_months(maturity_date, -step * k)
        if current <= settlement_date:
            return current, list(reversed(descending))
        descending.append(current)
    raise ValueError(
        f"settlement {settlement_date} is more than {_MAX_COUPON_PERIODS} coupon periods "
        f"before maturity {maturity_date}"
    )


def _period_weight(
    settlement_date: date,
    previous_coupon_date: date,
    next_coupon_date: date,
    convention: DayCount,
) -> float:
    """Fraction of the current coupon period still to run (``w`` in the price formula).

    ``w == 1`` on a coupon date and decays to ``0`` at the next one, so it is
    exactly the complement of the accrued fraction used by
    :func:`accrued_interest`.
    """
    period_days = _accrual_days(previous_coupon_date, next_coupon_date, convention)
    if period_days <= 0:
        raise ValueError(
            f"degenerate coupon period {previous_coupon_date} to {next_coupon_date} "
            f"under {convention}"
        )
    return _accrual_days(settlement_date, next_coupon_date, convention) / period_days


def _periodic_coupon(coupon_rate: float, face_value: float, frequency: int) -> float:
    """Cash paid by one coupon."""
    return coupon_rate / frequency * face_value


# --------------------------------------------------------------------------- #
# Explicit (scalar) API
# --------------------------------------------------------------------------- #
def accrued_interest(
    settlement_date: date,
    last_coupon_date: date,
    next_coupon_date: date,
    coupon_rate: float,
    face_value: float,
    frequency: int,
    day_count: str | DayCount,
) -> float:
    """Interest accrued between ``last_coupon_date`` and ``settlement_date``.

    ``AI = (coupon_rate / frequency) * face_value * accrued_days / period_days``

    The denominator is the **actual current coupon period**
    (``last_coupon_date`` to ``next_coupon_date``) rather than the nominal
    ``basis / frequency``. That is the more precise of the two conventions and
    the one the Colombian street uses: a 366-day annual period accrues over 366
    days, not over 365.

    A consequence worth stating, because it surprises people reading a vendor
    screen: with the real period in the denominator, **ACT/365 and ACT/360
    return the same accrued interest**. The annual basis appears in both the
    numerator's and the denominator's implicit scaling and cancels. The two
    conventions diverge only where a year fraction is used on its own, which is
    not the case here. 30/360 does differ, because it changes the numerator.

    Args:
        settlement_date: Valuation date, in ``[last_coupon_date, next_coupon_date]``.
        last_coupon_date: Start of the current accrual period (inclusive).
        next_coupon_date: End of the current accrual period (exclusive of accrual,
            but accepted as an input, where it returns exactly one full coupon).
        coupon_rate: Annual coupon rate as a decimal, e.g. ``0.07``.
        face_value: Notional the coupon is computed on.
        frequency: Coupons per year; 1 (annual) or 2 (semiannual) for TES.
        day_count: ``"ACT/365"``, ``"ACT/360"`` or ``"30/360"``.

    Returns:
        Accrued interest in the same units as ``face_value``. Exactly ``0.0`` on
        a coupon date, with no floating point residue: the numerator is an
        integer number of days.

    Raises:
        ValueError: On an unsupported convention or frequency, a non-positive
            coupon period, or a settlement date outside that period.
    """
    convention = _normalise_day_count(day_count)
    _validate_frequency(frequency)

    if next_coupon_date <= last_coupon_date:
        raise ValueError(
            f"next coupon {next_coupon_date} must fall after last coupon {last_coupon_date}"
        )
    if not (last_coupon_date <= settlement_date <= next_coupon_date):
        raise ValueError(
            f"settlement {settlement_date} is outside the coupon period "
            f"[{last_coupon_date}, {next_coupon_date}]"
        )

    period_days = _accrual_days(last_coupon_date, next_coupon_date, convention)
    if period_days <= 0:
        raise ValueError(
            f"degenerate coupon period {last_coupon_date} to {next_coupon_date} under {convention}"
        )
    accrued_days = _accrual_days(last_coupon_date, settlement_date, convention)
    return _periodic_coupon(coupon_rate, face_value, frequency) * accrued_days / period_days


def dirty_price_from_ytm(
    ytm: float,
    coupon_rate: float,
    face_value: float,
    settlement_date: date,
    maturity_date: date,
    frequency: int,
    day_count: str | DayCount,
) -> float:
    """Present value of the remaining cash flows at a single yield.

    ``P_dirty = sum_i C_i / (1 + y/f)^((i - 1) + w) + VN / (1 + y/f)^((N - 1) + w)``

    where ``f`` is ``frequency``, ``C_i`` is the periodic coupon and ``w`` is the
    fraction of the current coupon period still to run, measured under
    ``day_count``. In years, the time to flow ``i`` is ``t_i = ((i - 1) + w) / f``.

    The ``w`` term is the partial-period adjustment: on a coupon date ``w == 1``
    and the exponents collapse to the textbook ``1, 2, ..., N``; between coupon
    dates ``w`` shrinks by exactly the accrued fraction that
    :func:`accrued_interest` charges, which is what keeps
    ``clean = dirty - accrued`` continuous across the period.

    Args:
        ytm: Nominal annual yield compounded ``frequency`` times a year.
        coupon_rate: Annual coupon rate as a decimal.
        face_value: Notional redeemed at maturity.
        settlement_date: Valuation date; must be strictly before ``maturity_date``.
        maturity_date: Redemption date; anchors the backward coupon roll.
        frequency: Coupons per year.
        day_count: ``"ACT/365"``, ``"ACT/360"`` or ``"30/360"``.

    Returns:
        The dirty (full) price, in the same units as ``face_value``.

    Raises:
        ValueError: If settlement is on or after maturity, the convention or
            frequency is unsupported, or ``ytm`` sits at or below the pole
            ``-frequency`` where the discount factor is undefined.
    """
    convention = _normalise_day_count(day_count)
    _validate_frequency(frequency)

    if settlement_date >= maturity_date:
        raise ValueError(
            f"settlement {settlement_date} is on or after maturity {maturity_date}: "
            "no cash flows remain to discount"
        )

    growth = 1.0 + ytm / frequency
    if growth <= 0.0:
        raise ValueError(
            f"ytm {ytm!r} is at or below the pole -{frequency} where "
            "(1 + ytm/frequency) is not positive"
        )

    previous_coupon_date, payment_dates = _coupon_schedule(
        settlement_date, maturity_date, frequency
    )
    weight = _period_weight(settlement_date, previous_coupon_date, payment_dates[0], convention)

    coupon = _periodic_coupon(coupon_rate, face_value, frequency)
    last_index = len(payment_dates) - 1
    price = 0.0
    for index in range(len(payment_dates)):
        cashflow = coupon + (face_value if index == last_index else 0.0)
        price += cashflow / growth ** (index + weight)
    return price


def clean_price(dirty_price: float, accrued: float) -> float:
    """Return the quoted (clean) price: ``P_clean = P_dirty - AI``.

    Trivial by construction, and named anyway: every mis-priced bond one ends up
    debugging is a place where dirty and clean were mixed silently.
    """
    return dirty_price - accrued


def ytm_from_dirty_price(
    dirty_price: float,
    coupon_rate: float,
    face_value: float,
    settlement_date: date,
    maturity_date: date,
    frequency: int,
    day_count: str | DayCount,
    initial_guess: float = 0.08,
) -> float:
    """Invert :func:`dirty_price_from_ytm` for the yield implied by a price.

    Solves ``f(y) = dirty_price_from_ytm(y, ...) - dirty_price = 0`` with
    ``scipy.optimize.brentq`` on ``[YTM_LOWER_BOUND, YTM_UPPER_BOUND]``
    (0.01% to 50% nominal annual). Brent's method is used rather than Newton
    because the bracket makes convergence unconditional: ``f`` is continuous and
    strictly decreasing in ``y`` over the bracket, so a sign change at the
    endpoints is necessary and sufficient for a unique root, and no starting
    point can send the iteration off to a distant or negative yield under a
    stressed quote.

    Args:
        dirty_price: Observed full price, in the same units as ``face_value``.
        coupon_rate: Annual coupon rate as a decimal.
        face_value: Notional redeemed at maturity.
        settlement_date: Valuation date; must be strictly before ``maturity_date``.
        maturity_date: Redemption date.
        frequency: Coupons per year.
        day_count: ``"ACT/365"``, ``"ACT/360"`` or ``"30/360"``.
        initial_guess: Accepted for interface compatibility with Newton-style
            solvers and deliberately unused: ``brentq`` derives its iterates from
            the bracket, which is the property being bought here. It is still
            reported in :class:`YTMConvergenceError` so a caller who tuned it can
            see it had no effect.

    Returns:
        The yield, on the same nominal-annual-compounded-``frequency`` basis that
        :func:`dirty_price_from_ytm` consumes.

    Raises:
        YTMConvergenceError: If the price lies outside the range spanned by the
            bracket, or if Brent's method fails to converge inside it. Never
            returns ``initial_guess`` as a silent fallback.
        ValueError: On an unsupported convention or frequency, or a settlement
            date on or after maturity (propagated from the pricing function).
    """

    def objective(yield_: float) -> float:
        return (
            dirty_price_from_ytm(
                yield_,
                coupon_rate,
                face_value,
                settlement_date,
                maturity_date,
                frequency,
                day_count,
            )
            - dirty_price
        )

    # Evaluating the endpoints first also validates the inputs before the solver
    # is entered, so a bad convention raises ValueError rather than surfacing as
    # a convergence failure.
    price_at_lower = objective(YTM_LOWER_BOUND) + dirty_price
    price_at_upper = objective(YTM_UPPER_BOUND) + dirty_price

    if not price_at_upper <= dirty_price <= price_at_lower:
        raise YTMConvergenceError(
            f"dirty price {dirty_price!r} is not attainable on "
            f"[{YTM_LOWER_BOUND:.4%}, {YTM_UPPER_BOUND:.4%}]: that bracket spans prices "
            f"[{price_at_upper!r}, {price_at_lower!r}] for a {coupon_rate:.4%} coupon "
            f"maturing {maturity_date} settled {settlement_date} "
            f"(frequency={frequency}, day_count={day_count}, initial_guess={initial_guess!r})"
        )

    try:
        root = brentq(
            objective,
            YTM_LOWER_BOUND,
            YTM_UPPER_BOUND,
            xtol=1e-14,
            rtol=8.881784197001252e-16,  # 4 * eps, the smallest brentq accepts
            maxiter=200,
            full_output=False,
        )
    except (RuntimeError, ValueError) as exc:  # pragma: no cover - unreachable given the bracket
        raise YTMConvergenceError(
            f"brentq failed on [{YTM_LOWER_BOUND}, {YTM_UPPER_BOUND}] for dirty price "
            f"{dirty_price!r} (initial_guess={initial_guess!r}): {exc}"
        ) from exc
    return float(root)


# --------------------------------------------------------------------------- #
# Terms-based adapters
# --------------------------------------------------------------------------- #
def accrued_interest_from_terms(terms: BondTerms, settlement_date: date) -> float:
    """Accrued interest at ``settlement_date`` for a :class:`BondTerms`.

    Derives the surrounding coupon dates from the schedule implied by
    ``terms.maturity_date`` and ``terms.coupon_frequency``, then delegates to
    :func:`accrued_interest`.
    """
    _validate_frequency(terms.coupon_frequency)
    previous_coupon_date, payment_dates = _coupon_schedule(
        settlement_date, terms.maturity_date, terms.coupon_frequency
    )
    return accrued_interest(
        settlement_date,
        previous_coupon_date,
        payment_dates[0],
        terms.coupon_rate,
        terms.face_value,
        terms.coupon_frequency,
        terms.day_count,
    )


def clean_price_from_ytm(terms: BondTerms, settlement_date: date, ytm: float) -> float:
    """Return :func:`dirty_price_from_ytm` net of accrued interest."""
    dirty = dirty_price_from_ytm(
        ytm,
        terms.coupon_rate,
        terms.face_value,
        settlement_date,
        terms.maturity_date,
        terms.coupon_frequency,
        terms.day_count,
    )
    return clean_price(dirty, accrued_interest_from_terms(terms, settlement_date))


def yield_to_maturity(
    terms: BondTerms,
    settlement_date: date,
    clean_price_quote: float,
    *,
    guess: float = 0.08,
    tolerance: float = 1e-10,
    max_iterations: int = 100,
) -> float:
    """Invert :func:`clean_price_from_ytm` for the yield implied by a clean quote.

    Args:
        terms: The bond.
        settlement_date: Valuation date.
        clean_price_quote: Observed clean price, in the units of ``terms.face_value``.
        guess: Passed through to :func:`ytm_from_dirty_price`; see its note on why
            a bracketing solver ignores it.
        tolerance: Unused; the bracketed solver runs to machine precision, which
            is tighter than any value a caller would pass here.
        max_iterations: Unused, for the same reason.

    Raises:
        YTMConvergenceError: If the quote is not attainable by any yield inside
            the solver bracket.
    """
    del tolerance, max_iterations
    dirty = clean_price_quote + accrued_interest_from_terms(terms, settlement_date)
    return ytm_from_dirty_price(
        dirty,
        terms.coupon_rate,
        terms.face_value,
        settlement_date,
        terms.maturity_date,
        terms.coupon_frequency,
        terms.day_count,
        initial_guess=guess,
    )


# --------------------------------------------------------------------------- #
# Curve-consistent pricing
# --------------------------------------------------------------------------- #
def generate_cashflow_schedule(
    terms: BondTerms,
    settlement_date: date,
) -> pd.DataFrame:
    """Build the remaining cash flows of a bond as of ``settlement_date``.

    The schedule is rolled backwards from ``terms.maturity_date`` by
    :func:`_coupon_schedule`, so it is the same set of dates the yield-based
    pricers discount; only the time axis differs. ``year_fraction`` is measured
    from ``settlement_date`` under ``terms.day_count`` with that convention's
    annual basis (365 for ACT/365, 360 otherwise), which for a TES is the same
    ACT/365 axis the NSS curve was calibrated on.

    Args:
        terms: The bond.
        settlement_date: Valuation date; must be strictly before maturity.

    Returns:
        A DataFrame with columns ``payment_date``, ``year_fraction``,
        ``coupon``, ``principal`` and ``cashflow``, one row per remaining flow in
        ascending date order and restricted to flows strictly after
        ``settlement_date``. A coupon falling on ``settlement_date`` belongs to
        the seller and is excluded.

    Raises:
        ValueError: If settlement is on or after maturity, or the day count or
            frequency is unsupported.
    """
    convention = _normalise_day_count(terms.day_count)
    _validate_frequency(terms.coupon_frequency)
    if settlement_date >= terms.maturity_date:
        raise ValueError(
            f"settlement {settlement_date} is on or after maturity {terms.maturity_date}: "
            "no cash flows remain"
        )

    _, payment_dates = _coupon_schedule(
        settlement_date, terms.maturity_date, terms.coupon_frequency
    )
    basis = _ANNUAL_BASIS_DAYS[convention]
    coupon = _periodic_coupon(terms.coupon_rate, terms.face_value, terms.coupon_frequency)
    last_index = len(payment_dates) - 1

    rows = [
        {
            "payment_date": payment_date,
            "year_fraction": _accrual_days(settlement_date, payment_date, convention) / basis,
            "coupon": coupon,
            "principal": terms.face_value if index == last_index else 0.0,
        }
        for index, payment_date in enumerate(payment_dates)
    ]
    schedule = pd.DataFrame(rows, columns=["payment_date", "year_fraction", "coupon", "principal"])
    schedule["cashflow"] = schedule["coupon"] + schedule["principal"]
    return schedule


def price_from_discount_factors(
    cashflows: NDArray[np.float64],
    discount_factors: NDArray[np.float64],
) -> float:
    """Present value of ``cashflows`` under an arbitrary discount curve.

    ``P = sum_i CF_i * DF_i``. This is the curve-consistent pricing entry point:
    it never assumes a flat yield, and it is agnostic to the compounding basis
    that produced the discount factors - the caller owns that choice.

    Args:
        cashflows: Amounts, one per flow.
        discount_factors: Discount factor to each flow's payment date, aligned
            with ``cashflows``.

    Returns:
        The dirty (full) price, in the units of ``cashflows``.

    Raises:
        ValueError: If the two arrays differ in shape, or any discount factor is
            not strictly positive and finite.
    """
    flows = np.asarray(cashflows, dtype=np.float64)
    factors = np.asarray(discount_factors, dtype=np.float64)
    if flows.shape != factors.shape:
        raise ValueError(
            f"cashflows and discount_factors must have the same shape; "
            f"got {flows.shape} and {factors.shape}"
        )
    if not np.all(np.isfinite(factors)) or np.any(factors <= 0.0):
        raise ValueError(
            f"discount factors must be strictly positive and finite; got {factors.tolist()}"
        )
    return float(np.sum(flows * factors))
