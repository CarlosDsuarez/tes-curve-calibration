"""Curve-consistent bond pricing and risk: cash flow schedule, DF pricing, DV01.

These are the Phase 4/8 stubs the Excel bridge needs: pricing a TES off the
calibrated NSS discount factors and bumping that curve for DV01, duration and
convexity. Nothing here trusts a closed form on its own - every sensitivity is
re-derived a second way, usually through the zero-coupon special case where the
answer is known exactly.

Compounding basis: :func:`~tes_pricer.math.nss_model.nss_discount_factor` is
continuously compounded, ``DF = exp(-z * t)``. Under that basis a parallel
shift of the zero curve gives ``dP/P = -D_mac * dz`` exactly, so **modified
duration coincides with Macaulay duration** and convexity is the cash-flow
weighted ``t^2``. One test pins each of those identities so the basis can never
be silently swapped for a discrete one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import numpy as np
import pytest

from tes_pricer.math.bond_pricing import (
    BondTerms,
    dirty_price_from_ytm,
    generate_cashflow_schedule,
    price_from_discount_factors,
)
from tes_pricer.math.greeks import (
    BASIS_POINT,
    BondRisk,
    bond_risk_from_curve,
    dv01_from_curve,
)
from tes_pricer.math.nss_model import NSSParams, nss_discount_factor

pytestmark = pytest.mark.unit

SCHEDULE_COLUMNS = ["payment_date", "year_fraction", "coupon", "principal", "cashflow"]


@pytest.fixture
def bond(sample_bond_terms: dict[str, object]) -> BondTerms:
    """The shared synthetic 10-year, 7% annual bond (settles on a coupon date)."""
    return BondTerms(**sample_bond_terms)  # type: ignore[arg-type]


@pytest.fixture
def params(sample_nss_params: dict[str, float]) -> NSSParams:
    """The shared plausible COP curve."""
    return NSSParams(**sample_nss_params)


def _curve_dirty_price(terms: BondTerms, settlement: date, params: NSSParams) -> float:
    """Reference price: the schedule discounted on the NSS curve, written out by hand."""
    schedule = generate_cashflow_schedule(terms, settlement)
    taus = schedule["year_fraction"].to_numpy(dtype=np.float64)
    flows = schedule["cashflow"].to_numpy(dtype=np.float64)
    dfs = np.asarray(nss_discount_factor(taus, params), dtype=np.float64)
    return float(np.sum(flows * dfs))


# --------------------------------------------------------------------------- #
# generate_cashflow_schedule
# --------------------------------------------------------------------------- #
def test_schedule_has_the_documented_columns(bond: BondTerms, settlement_date: date) -> None:
    schedule = generate_cashflow_schedule(bond, settlement_date)
    assert list(schedule.columns) == SCHEDULE_COLUMNS


def test_schedule_holds_only_flows_strictly_after_settlement(
    bond: BondTerms, settlement_date: date
) -> None:
    """Settlement is a coupon date: that coupon belongs to the seller, not the schedule."""
    schedule = generate_cashflow_schedule(bond, settlement_date)
    dates = list(schedule["payment_date"])
    assert dates == [date(year, 3, 16) for year in range(2027, 2031)]
    assert all(d > settlement_date for d in dates)


def test_schedule_flows_are_coupon_plus_principal_at_maturity(
    bond: BondTerms, settlement_date: date
) -> None:
    schedule = generate_cashflow_schedule(bond, settlement_date)
    assert np.allclose(schedule["coupon"], 7.0)
    assert list(schedule["principal"]) == [0.0, 0.0, 0.0, 100.0]
    assert np.allclose(schedule["cashflow"], schedule["coupon"] + schedule["principal"])


def test_schedule_year_fractions_follow_the_terms_day_count(
    bond: BondTerms, settlement_date: date
) -> None:
    """ACT/365 from settlement, so 2028's leap day shows up in the second flow."""
    schedule = generate_cashflow_schedule(bond, settlement_date)
    expected = [(d - settlement_date).days / 365.0 for d in schedule["payment_date"]]
    assert np.allclose(schedule["year_fraction"], expected)
    assert schedule["year_fraction"].iloc[0] == pytest.approx(1.0)
    assert schedule["year_fraction"].iloc[1] == pytest.approx(731 / 365)


def test_schedule_semiannual_bond_pays_half_coupons_twice_a_year(
    bond: BondTerms, settlement_date: date
) -> None:
    semi = replace(bond, coupon_frequency=2)
    schedule = generate_cashflow_schedule(semi, settlement_date)
    assert len(schedule) == 8
    assert np.allclose(schedule["coupon"], 3.5)
    assert schedule["payment_date"].iloc[0] == date(2026, 9, 16)


def test_schedule_rejects_settlement_on_or_after_maturity(bond: BondTerms) -> None:
    with pytest.raises(ValueError, match="maturity"):
        generate_cashflow_schedule(bond, bond.maturity_date)


# --------------------------------------------------------------------------- #
# price_from_discount_factors
# --------------------------------------------------------------------------- #
def test_unit_discount_factors_return_the_undiscounted_sum() -> None:
    flows = np.array([7.0, 7.0, 107.0])
    assert price_from_discount_factors(flows, np.ones(3)) == pytest.approx(121.0)


def test_price_from_discount_factors_rejects_mismatched_shapes() -> None:
    with pytest.raises(ValueError, match="shape"):
        price_from_discount_factors(np.array([1.0, 2.0]), np.array([1.0]))


def test_price_from_discount_factors_rejects_non_positive_factors() -> None:
    with pytest.raises(ValueError, match="positive"):
        price_from_discount_factors(np.array([1.0, 2.0]), np.array([1.0, 0.0]))


def test_flat_curve_pricing_reproduces_the_yield_formula_on_a_coupon_date(
    bond: BondTerms, settlement_date: date
) -> None:
    """Independent code path: the schedule priced at ``(1+y)^-k`` must equal
    :func:`dirty_price_from_ytm`, which rolls its own exponents."""
    ytm = 0.0912
    schedule = generate_cashflow_schedule(bond, settlement_date)
    periods = np.arange(1, len(schedule) + 1, dtype=np.float64)
    dfs = (1.0 + ytm) ** -periods
    curve_price = price_from_discount_factors(schedule["cashflow"].to_numpy(), dfs)
    expected = dirty_price_from_ytm(
        ytm, bond.coupon_rate, bond.face_value, settlement_date, bond.maturity_date, 1, "ACT/365"
    )
    assert curve_price == pytest.approx(expected, rel=1e-12)


# --------------------------------------------------------------------------- #
# dv01_from_curve
# --------------------------------------------------------------------------- #
def test_curve_dv01_is_the_price_drop_for_a_one_bp_parallel_shift(
    bond: BondTerms, settlement_date: date, params: NSSParams
) -> None:
    """Bump-and-reprice written out by hand: shift ``beta0`` by 1bp and difference."""
    base = _curve_dirty_price(bond, settlement_date, params)
    bumped = _curve_dirty_price(
        bond, settlement_date, replace(params, beta0=params.beta0 + BASIS_POINT)
    )
    assert dv01_from_curve(bond, settlement_date, params) == pytest.approx(base - bumped)


def test_curve_dv01_is_positive_for_a_long_position(
    bond: BondTerms, settlement_date: date, params: NSSParams
) -> None:
    assert dv01_from_curve(bond, settlement_date, params) > 0.0


def test_curve_dv01_scales_with_face_value(
    bond: BondTerms, settlement_date: date, params: NSSParams
) -> None:
    per_100 = dv01_from_curve(bond, settlement_date, params)
    per_million = dv01_from_curve(replace(bond, face_value=1e6), settlement_date, params)
    assert per_million == pytest.approx(per_100 * 1e4, rel=1e-12)


def test_curve_dv01_agrees_with_duration_times_price(
    bond: BondTerms, settlement_date: date, params: NSSParams
) -> None:
    """First-order check: ``DV01 ~= D_mac * P * 1bp`` to the size of the convexity term."""
    risk = bond_risk_from_curve(bond, settlement_date, params)
    price = _curve_dirty_price(bond, settlement_date, params)
    assert risk.dv01 == pytest.approx(risk.macaulay_duration * price * BASIS_POINT, rel=1e-3)


# --------------------------------------------------------------------------- #
# bond_risk_from_curve
# --------------------------------------------------------------------------- #
def test_bond_risk_from_curve_returns_the_shared_value_object(
    bond: BondTerms, settlement_date: date, params: NSSParams
) -> None:
    risk = bond_risk_from_curve(bond, settlement_date, params)
    assert isinstance(risk, BondRisk)
    assert risk.dv01 == pytest.approx(dv01_from_curve(bond, settlement_date, params))


def test_zero_coupon_macaulay_duration_is_its_time_to_maturity(
    bond: BondTerms, settlement_date: date, params: NSSParams
) -> None:
    zero = replace(bond, coupon_rate=0.0)
    risk = bond_risk_from_curve(zero, settlement_date, params)
    tau = (zero.maturity_date - settlement_date).days / 365.0
    assert risk.macaulay_duration == pytest.approx(tau, rel=1e-12)


def test_zero_coupon_convexity_is_maturity_squared(
    bond: BondTerms, settlement_date: date, params: NSSParams
) -> None:
    """Continuous compounding: ``(1/P) d2P/dz2 = t^2`` for a single flow."""
    zero = replace(bond, coupon_rate=0.0)
    risk = bond_risk_from_curve(zero, settlement_date, params)
    tau = (zero.maturity_date - settlement_date).days / 365.0
    assert risk.convexity == pytest.approx(tau**2, rel=1e-5)


def test_modified_duration_matches_macaulay_under_continuous_compounding(
    bond: BondTerms, settlement_date: date, params: NSSParams
) -> None:
    """The numeric ``-(1/P) dP/dz`` must reproduce the analytic cash-flow-weighted time.

    Pinned deliberately: if anyone swaps the discount factor to ``(1+z)^-t``
    the two diverge by a factor ``1/(1+z)`` and this fails.
    """
    risk = bond_risk_from_curve(bond, settlement_date, params)
    assert risk.modified_duration == pytest.approx(risk.macaulay_duration, rel=1e-6)


def test_coupon_bond_duration_is_shorter_than_its_maturity(
    bond: BondTerms, settlement_date: date, params: NSSParams
) -> None:
    risk = bond_risk_from_curve(bond, settlement_date, params)
    tau = (bond.maturity_date - settlement_date).days / 365.0
    assert 0.0 < risk.macaulay_duration < tau
    assert risk.convexity > 0.0


def test_bond_risk_from_curve_rejects_inadmissible_params(
    bond: BondTerms, settlement_date: date, params: NSSParams
) -> None:
    with pytest.raises(ValueError, match="lambda"):
        bond_risk_from_curve(bond, settlement_date, replace(params, lambda1=-1.0))
