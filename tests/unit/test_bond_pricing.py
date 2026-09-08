"""Clean/dirty pricing, accrued interest and the YTM solver.

The suite is built on three kinds of check, in decreasing order of how much they
prove:

1. **A published known answer** — the textbook example in
   :func:`test_reproduces_fabozzi_twenty_year_example`, cross-checked against
   the closed-form annuity formula, which is an independent code path.
2. **Round trips** — pricing and then inverting must return the input yield to
   near machine precision, for every supported convention and frequency.
3. **Structural invariants** — accrued interest vanishing on a coupon date,
   monotonicity in yield, and the behaviour of a bond inside its final period.
"""

from __future__ import annotations

from datetime import date

import pytest

from tes_pricer.math.bond_pricing import (
    BondTerms,
    YTMConvergenceError,
    accrued_interest,
    clean_price,
    clean_price_from_ytm,
    dirty_price_from_ytm,
    yield_to_maturity,
    ytm_from_dirty_price,
)

pytestmark = pytest.mark.unit

DAY_COUNTS = ["ACT/365", "ACT/360", "30/360"]
FREQUENCIES = [1, 2]


@pytest.fixture
def bond(sample_bond_terms: dict[str, object]) -> BondTerms:
    """The shared synthetic 10-year, 7% annual bond."""
    return BondTerms(**sample_bond_terms)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 1. Round trip: price(ytm) then ytm(price) must be the identity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("day_count", DAY_COUNTS)
@pytest.mark.parametrize("frequency", FREQUENCIES)
@pytest.mark.parametrize("target_ytm", [0.0025, 0.045, 0.0912, 0.1875, 0.42])
def test_ytm_round_trip(day_count: str, frequency: int, target_ytm: float) -> None:
    """Inverting the pricing function must recover the yield that produced the price.

    Run across both frequencies and all three conventions, and across a yield
    range that reaches the ends of the solver bracket, so a convention-specific
    off-by-one in the period weight cannot hide inside a benign 8-9% quote.
    """
    settlement = date(2026, 3, 16)
    maturity = date(2035, 6, 30)

    dirty = dirty_price_from_ytm(
        target_ytm,
        coupon_rate=0.0725,
        face_value=100.0,
        settlement_date=settlement,
        maturity_date=maturity,
        frequency=frequency,
        day_count=day_count,
    )
    recovered = ytm_from_dirty_price(
        dirty,
        coupon_rate=0.0725,
        face_value=100.0,
        settlement_date=settlement,
        maturity_date=maturity,
        frequency=frequency,
        day_count=day_count,
    )
    assert recovered == pytest.approx(target_ytm, abs=1e-8)


@pytest.mark.parametrize("day_count", DAY_COUNTS)
@pytest.mark.parametrize("frequency", FREQUENCIES)
def test_round_trip_on_a_coupon_date(day_count: str, frequency: int) -> None:
    """The round trip must also hold where the partial-period weight is exactly 1."""
    settlement = date(2026, 6, 30)
    maturity = date(2035, 6, 30)
    dirty = dirty_price_from_ytm(
        0.0912, 0.0725, 100.0, settlement, maturity, frequency, day_count
    )
    recovered = ytm_from_dirty_price(
        dirty, 0.0725, 100.0, settlement, maturity, frequency, day_count
    )
    assert recovered == pytest.approx(0.0912, abs=1e-8)


# --------------------------------------------------------------------------- #
# 2. Published known answer
# --------------------------------------------------------------------------- #
def test_reproduces_fabozzi_twenty_year_example() -> None:
    """Reproduce the standard worked example from Fabozzi's bond pricing chapter.

    Source: Frank J. Fabozzi, *Bond Markets, Analysis, and Strategies*, the
    worked example in the "Pricing a Bond" section of the bond pricing chapter:
    a 20-year, 10% coupon bond with a par value of $1,000 paying semiannual
    coupons, priced at a required yield of 11%, is worth **$919.77**.

    Discounting 40 semiannual coupons of $50 plus $1,000 of principal at 5.5%
    per period:

    ``50 * (1 - 1.055^-40) / 0.055 + 1000 * 1.055^-40 = 919.7693765...``

    The test pins both the published figure (to the cent it is printed at) and
    the closed-form annuity value (to 1e-11 relative). The closed form is the
    part that makes this a real known-answer test: it is computed here from the
    ordinary annuity identity, not by the code under test, so an error in the
    schedule roll or the period weight cannot cancel out of both sides.

    Settlement is placed on a coupon date exactly 20 years before maturity, so
    the partial-period weight is 1 and the discount exponents are the integers
    1..40 the textbook uses.
    """
    settlement = date(2025, 1, 15)
    maturity = date(2045, 1, 15)

    dirty = dirty_price_from_ytm(
        ytm=0.11,
        coupon_rate=0.10,
        face_value=1000.0,
        settlement_date=settlement,
        maturity_date=maturity,
        frequency=2,
        day_count="ACT/365",
    )

    periods, coupon, periodic_yield = 40, 50.0, 0.055
    discount = (1.0 + periodic_yield) ** -periods
    closed_form = coupon * (1.0 - discount) / periodic_yield + 1000.0 * discount

    assert dirty == pytest.approx(closed_form, rel=1e-11)
    assert dirty == pytest.approx(919.77, abs=0.005)

    # On a coupon date there is nothing accrued, so the published figure is both
    # the clean and the dirty price.
    accrued = accrued_interest(
        settlement_date=settlement,
        last_coupon_date=settlement,
        next_coupon_date=date(2025, 7, 15),
        coupon_rate=0.10,
        face_value=1000.0,
        frequency=2,
        day_count="ACT/365",
    )
    assert clean_price(dirty, accrued) == pytest.approx(919.77, abs=0.005)


def test_recovers_the_fabozzi_yield_from_the_published_price() -> None:
    """The solver run on the published $919.77 returns the 11% required yield.

    Tolerance is set by the rounding of the printed price, not by the solver: a
    half-cent of price on a 20-year bond is worth roughly 4e-7 of yield.
    """
    recovered = ytm_from_dirty_price(
        919.77,
        coupon_rate=0.10,
        face_value=1000.0,
        settlement_date=date(2025, 1, 15),
        maturity_date=date(2045, 1, 15),
        frequency=2,
        day_count="ACT/365",
    )
    assert recovered == pytest.approx(0.11, abs=1e-6)


# --------------------------------------------------------------------------- #
# 3. Edge case: inside the final coupon period
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("day_count", DAY_COUNTS)
@pytest.mark.parametrize("frequency", FREQUENCIES)
def test_final_coupon_period_is_well_defined(day_count: str, frequency: int) -> None:
    """A bond with less than one coupon period left must price without degeneracy.

    The failure mode being excluded is a zero denominator in the partial-period
    weight, or an empty cash flow list, when the only remaining flow is the
    redemption itself.
    """
    maturity = date(2026, 1, 15)
    settlement = date(2025, 11, 20)

    dirty = dirty_price_from_ytm(0.09, 0.0725, 100.0, settlement, maturity, frequency, day_count)

    # One coupon plus principal, discounted over a fraction of a period: the
    # price must sit between the undiscounted and the fully-discounted flow.
    final_flow = 100.0 + 0.0725 / frequency * 100.0
    assert 0.0 < dirty < final_flow
    assert dirty > final_flow / (1.0 + 0.09 / frequency)

    recovered = ytm_from_dirty_price(
        dirty, 0.0725, 100.0, settlement, maturity, frequency, day_count
    )
    assert recovered == pytest.approx(0.09, abs=1e-8)


@pytest.mark.parametrize("day_count", DAY_COUNTS)
def test_one_day_before_maturity_converges_to_the_final_flow(day_count: str) -> None:
    """With a single day left the price is the redemption flow, barely discounted."""
    maturity = date(2026, 1, 15)
    dirty = dirty_price_from_ytm(0.09, 0.0725, 100.0, date(2026, 1, 14), maturity, 1, day_count)
    final_flow = 107.25
    assert dirty == pytest.approx(final_flow, rel=5e-4)
    assert dirty < final_flow


def test_settlement_on_or_after_maturity_is_rejected() -> None:
    """No cash flows remain, so there is no price; this must not return 0.0 quietly."""
    maturity = date(2026, 1, 15)
    with pytest.raises(ValueError, match="no cash flows remain"):
        dirty_price_from_ytm(0.09, 0.0725, 100.0, maturity, maturity, 1, "ACT/365")
    with pytest.raises(ValueError, match="no cash flows remain"):
        dirty_price_from_ytm(0.09, 0.0725, 100.0, date(2026, 2, 1), maturity, 1, "ACT/365")


# --------------------------------------------------------------------------- #
# 4. Accrued interest on the coupon date itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("day_count", DAY_COUNTS)
@pytest.mark.parametrize("frequency", FREQUENCIES)
def test_accrued_interest_is_zero_on_the_coupon_date(day_count: str, frequency: int) -> None:
    """Settling on the coupon date accrues nothing.

    Asserted with an explicit absolute tolerance rather than ``== 0.0``: the
    contract is "zero to within floating point", and a future refactor that
    computes the accrued fraction differently should not fail this test for a
    1e-17 residue. (The current implementation does return an exact 0.0, because
    the numerator is an integer count of days.)
    """
    last_coupon = date(2025, 6, 30)
    next_coupon = date(2026, 6, 30) if frequency == 1 else date(2025, 12, 31)

    accrued = accrued_interest(
        settlement_date=last_coupon,
        last_coupon_date=last_coupon,
        next_coupon_date=next_coupon,
        coupon_rate=0.0725,
        face_value=100.0,
        frequency=frequency,
        day_count=day_count,
    )
    assert accrued == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize("day_count", DAY_COUNTS)
def test_accrued_interest_reaches_a_full_coupon_at_period_end(day_count: str) -> None:
    """The other end of the accrual: a full periodic coupon, not a fraction of one."""
    accrued = accrued_interest(
        settlement_date=date(2026, 6, 30),
        last_coupon_date=date(2025, 6, 30),
        next_coupon_date=date(2026, 6, 30),
        coupon_rate=0.0725,
        face_value=100.0,
        frequency=1,
        day_count=day_count,
    )
    assert accrued == pytest.approx(7.25, abs=1e-12)


def test_accrued_interest_is_linear_in_elapsed_days() -> None:
    """Half of an ACT-measured period accrues half a coupon."""
    accrued = accrued_interest(
        settlement_date=date(2025, 7, 1),
        last_coupon_date=date(2025, 1, 1),
        next_coupon_date=date(2026, 1, 1),
        coupon_rate=0.08,
        face_value=100.0,
        frequency=1,
        day_count="ACT/365",
    )
    assert accrued == pytest.approx(8.0 * 181 / 365, abs=1e-12)


def test_thirty_360_uses_the_isda_bond_basis_numerator() -> None:
    """30/360 must change the numerator, and must be the ISDA bond basis variant.

    15 January to 31 March is 75 actual days. Under 30/360 Bond Basis (ISDA 2006
    4.16(f)) it is 76: ``D1 = 15`` is not 31, so the truncation of ``D2 = 31`` is
    *not* triggered, and the stub month contributes 16 days. The European
    variant, 30E/360, truncates ``D2`` unconditionally and would return 75 —
    which is exactly what this assertion rules out.
    """
    kwargs = {
        "settlement_date": date(2025, 3, 31),
        "last_coupon_date": date(2025, 1, 15),
        "next_coupon_date": date(2026, 1, 15),
        "coupon_rate": 0.08,
        "face_value": 100.0,
        "frequency": 1,
    }
    actual = accrued_interest(day_count="ACT/365", **kwargs)  # type: ignore[arg-type]
    thirty = accrued_interest(day_count="30/360", **kwargs)  # type: ignore[arg-type]
    assert actual == pytest.approx(8.0 * 75 / 365, abs=1e-12)
    assert thirty == pytest.approx(8.0 * 76 / 360, abs=1e-12)
    assert thirty != pytest.approx(actual, abs=1e-6)


def test_thirty_360_bond_basis_has_no_end_of_february_rule() -> None:
    """31 January to 28 February is 28 days, not 30.

    The 30/360 US (NASD/SIA) variant carries an extra end-of-February rule that
    would round this period up to a full month. ISDA's Bond Basis does not, and
    the difference shows up in every February accrual on a month-end coupon, so
    it is pinned here rather than left to the next person to rediscover.
    """
    accrued = accrued_interest(
        settlement_date=date(2025, 2, 28),
        last_coupon_date=date(2025, 1, 31),
        next_coupon_date=date(2026, 1, 31),
        coupon_rate=0.08,
        face_value=100.0,
        frequency=1,
        day_count="30/360",
    )
    assert accrued == pytest.approx(8.0 * 28 / 360, abs=1e-12)


def test_settlement_outside_the_coupon_period_is_rejected() -> None:
    """A settlement date outside [last, next] is a caller bug, not a negative accrual."""
    with pytest.raises(ValueError, match="outside the coupon period"):
        accrued_interest(
            settlement_date=date(2024, 12, 1),
            last_coupon_date=date(2025, 1, 1),
            next_coupon_date=date(2026, 1, 1),
            coupon_rate=0.08,
            face_value=100.0,
            frequency=1,
            day_count="ACT/365",
        )


# --------------------------------------------------------------------------- #
# Solver diagnostics
# --------------------------------------------------------------------------- #
def test_unattainable_price_raises_with_a_diagnostic() -> None:
    """An out-of-bracket price must raise, never return the initial guess."""
    with pytest.raises(YTMConvergenceError) as excinfo:
        ytm_from_dirty_price(
            5.0,  # far below the price of any bond yielding under 50%
            coupon_rate=0.0725,
            face_value=100.0,
            settlement_date=date(2026, 3, 16),
            maturity_date=date(2035, 6, 30),
            frequency=1,
            day_count="ACT/365",
            initial_guess=0.08,
        )
    message = str(excinfo.value)
    assert "not attainable" in message
    assert "50.0000%" in message
    assert "initial_guess=0.08" in message


def test_rejects_unknown_day_count() -> None:
    """An unsupported convention fails loudly at the boundary."""
    with pytest.raises(ValueError, match="unknown day count"):
        dirty_price_from_ytm(
            0.09, 0.0725, 100.0, date(2026, 3, 16), date(2030, 3, 16), 1, "ACT/366"
        )
    with pytest.raises(ValueError, match="not supported here"):
        dirty_price_from_ytm(
            0.09, 0.0725, 100.0, date(2026, 3, 16), date(2030, 3, 16), 1, "ACT/ACT-ISDA"
        )


# --------------------------------------------------------------------------- #
# Terms-based adapters
# --------------------------------------------------------------------------- #
def test_par_bond_prices_at_par(bond: BondTerms, settlement_date: date) -> None:
    """Discounting at the coupon rate on a coupon date gives par."""
    price = clean_price_from_ytm(bond, settlement_date, bond.coupon_rate)
    assert price == pytest.approx(100.0, abs=1e-8)


def test_price_is_monotone_decreasing_in_yield(bond: BondTerms, settlement_date: date) -> None:
    """A higher yield must produce a lower price."""
    low = clean_price_from_ytm(bond, settlement_date, 0.05)
    high = clean_price_from_ytm(bond, settlement_date, 0.11)
    assert low > high


def test_ytm_inverts_the_pricing_function(bond: BondTerms, settlement_date: date) -> None:
    """The solver must recover the yield used to build the price."""
    target = 0.0912
    price = clean_price_from_ytm(bond, settlement_date, target)
    assert yield_to_maturity(bond, settlement_date, price) == pytest.approx(target, abs=1e-10)


def test_dirty_exceeds_clean_between_coupons(bond: BondTerms) -> None:
    """Between coupon dates the dirty price carries accrued interest."""
    mid_period = date(2026, 9, 16)
    dirty = dirty_price_from_ytm(
        0.09,
        bond.coupon_rate,
        bond.face_value,
        mid_period,
        bond.maturity_date,
        bond.coupon_frequency,
        bond.day_count,
    )
    clean = clean_price_from_ytm(bond, mid_period, 0.09)
    assert dirty > clean
