"""Clean/dirty pricing and the YTM solver. Skeleton for Phase 4."""

from __future__ import annotations

from datetime import date

import pytest

from tes_pricer.math.bond_pricing import (
    BondTerms,
    clean_price_from_ytm,
    dirty_price_from_ytm,
    yield_to_maturity,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def bond(sample_bond_terms: dict[str, object]) -> BondTerms:
    """The shared synthetic 10-year, 7% annual bond."""
    return BondTerms(**sample_bond_terms)  # type: ignore[arg-type]


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 4", strict=True)
def test_par_bond_prices_at_par(bond: BondTerms, settlement_date: date) -> None:
    """Discounting at the coupon rate on a coupon date gives par."""
    price = clean_price_from_ytm(bond, settlement_date, bond.coupon_rate)
    assert price == pytest.approx(100.0, abs=1e-8)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 4", strict=True)
def test_price_is_monotone_decreasing_in_yield(bond: BondTerms, settlement_date: date) -> None:
    """A higher yield must produce a lower price."""
    low = clean_price_from_ytm(bond, settlement_date, 0.05)
    high = clean_price_from_ytm(bond, settlement_date, 0.11)
    assert low > high


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 4", strict=True)
def test_ytm_inverts_the_pricing_function(bond: BondTerms, settlement_date: date) -> None:
    """The solver must recover the yield used to build the price."""
    target = 0.0912
    price = clean_price_from_ytm(bond, settlement_date, target)
    assert yield_to_maturity(bond, settlement_date, price) == pytest.approx(target, abs=1e-10)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 4", strict=True)
def test_dirty_exceeds_clean_between_coupons(bond: BondTerms) -> None:
    """Between coupon dates the dirty price carries accrued interest."""
    mid_period = date(2026, 9, 16)
    dirty = dirty_price_from_ytm(bond, mid_period, 0.09)
    clean = clean_price_from_ytm(bond, mid_period, 0.09)
    assert dirty > clean
