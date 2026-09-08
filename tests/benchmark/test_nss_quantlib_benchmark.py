r"""Cross-check the NSS curve and its calibration against ``QuantLib.SvenssonFitting``.

QuantLib implements the Svensson curve natively, with the same mathematics as
:mod:`tes_pricer.math.nss_model` but its own calibration routine. That makes it
a referee for three things this repository could get quietly wrong: the algebra
of the loadings, the meaning of the decay parameters, and the compounding basis
of the rate the curve returns.

Deselected by default and skipped outright when QuantLib is absent (see
``tests/conftest.py``). Run with::

    pytest -m benchmark

QuantLib is the referee, not a dependency: nothing under ``src/`` imports it.

Two conventions had to be established empirically, and both are now pinned by
tests here rather than left as lore
-----------------------------------------------------------------------------
**The decay parameters are reciprocals of ours.** QuantLib's ``SvenssonFitting``
carries ``x[4]`` and ``x[5]`` as decay *rates* :math:`\kappa = 1/\lambda`,
where :class:`~tes_pricer.math.nss_model.NSSParams` carries decay *time
constants* :math:`\lambda` in years. Its discount function is

.. code-block:: text

    z(t) = x0 + (x1 + x2) * (1 - exp(-k t)) / (k t) - x2 * exp(-k t)
              + x3 * ((1 - exp(-k1 t)) / (k1 t) - exp(-k1 t))

which regroups to exactly our ``beta0 + beta1 L1 + beta2 (L1 - e^-x1) +
beta3 (L2 - e^-x2)`` once ``k = 1/lambda1`` and ``k1 = 1/lambda2``. Reading
``x[4]`` as a lambda instead of a kappa moves the 15-year point by 76bp, which
is the sort of error that survives a code review and dies in a benchmark;
:func:`test_quantlib_parameterises_the_decay_as_kappa_not_lambda` is the
regression guard, and it uses no optimiser at all.

**The rate is continuously compounded on both sides.** QuantLib forms
``discount = exp(-z t)``, which is the convention
:mod:`tes_pricer.math.nss_model` documents and returns. No conversion is
applied anywhere below, and none should be.

Why the fitted universe is zero-coupon
--------------------------------------
:func:`~tes_pricer.math.calibration.calibrate_nss` minimises **yield**
residuals, ``z(tau_i) - y_i``. ``QuantLib.FittedBondDiscountCurve`` minimises
**price** residuals. Those are different objectives, and on coupon-paying bonds
they have different minimisers: a coupon bond's yield to maturity is a
cash-flow-weighted blend of the whole curve up to its maturity, not the zero
rate at ``tau``. The one instrument on which the two objectives coincide is the
zero-coupon bond, whose single cash flow makes ``price = 100 exp(-z(tau) tau)``
and ``ytm = z(tau)`` simultaneously true.

So the strict comparison below runs on zero-coupon bonds - built as
``ql.FixedRateBond`` with a 0% coupon, so the instrument type is still the one
the pipeline will use - and it is a genuine test of the curve mathematics and
of both calibration routines. What it deliberately is *not* is a test of the
v1 yield-space objective, whose bias is a known limitation rather than a bug.
:func:`test_coupon_bonds_expose_the_yield_space_objective_gap` measures that
bias instead of hiding it, and ``docs/known_limitations.md`` records it.

Calendar
--------
QuantLib 1.43 ships **no Colombian calendar** (``ql.Colombia`` does not exist;
neither does a Bogota variant), so ``ql.NullCalendar()`` is used with
``ql.Unadjusted`` rolls. That is not a compromise here, it is the correct
choice: the maturities are compared through ``ACT/365`` year fractions taken
straight from the snapshot date, exactly as ``tests/unit/test_calibration.py``
computes them, and any business-day adjustment would shift a maturity off that
grid and break the very correspondence being tested. A Colombian calendar
matters for accrual and settlement on real TES - see
``docs/known_limitations.md`` - not for this comparison.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import numpy as np
import pytest
from numpy.typing import NDArray

from tes_pricer.math.bond_pricing import ytm_from_dirty_price
from tes_pricer.math.calibration import calibrate_nss, initial_beta_guess
from tes_pricer.math.nss_model import NSSParams, nss_discount_factor, nss_yield

ql = pytest.importorskip("QuantLib")

pytestmark = [pytest.mark.benchmark]


# --------------------------------------------------------------------------- #
# Tolerances
# --------------------------------------------------------------------------- #
SYNTHETIC_SPOT_TOLERANCE = 1e-4
"""1bp: the acceptance criterion on noiseless synthetic data.

Both systems are fitting a curve that reproduces the observations exactly, so
the only thing separating them is optimiser convergence. 1bp is loose enough to
absorb that and tight enough to catch any real disagreement in the maths: a
mis-read decay convention shows up as 76bp, a swapped compounding basis as
about 38bp at a 9% level.
"""

REAL_DATA_SPOT_TOLERANCE = 5e-4
"""5bp: the tolerance the same comparison gets once it runs on real quotes.

Not used yet - there is no real-data phase to point it at - and defined here so
the relaxation is a stated constant rather than a number someone edits into an
assertion later. The justification is
:func:`test_two_optimisers_find_different_parameters_but_the_same_curve`: the
objective is not convex, so two correct implementations can settle in different
basins. On exact data every basin that fits is the same curve; on noisy data
they are merely close, and 5bp is the width of "close" for a ten-bond COP
universe. Widening it beyond this is a finding to investigate, not a knob.
"""

PARAMETER_TOLERANCE = 1e-5
"""Absolute agreement required on the six parameters when both fits are exact.

Applies to the betas and to the decay constants alike. It is far tighter than
:data:`SYNTHETIC_SPOT_TOLERANCE` because on an exactly-reproducible universe
both optimisers drive their residuals to machine zero, so the parameters
themselves - not just the curve they draw - have to line up.
"""

QUANTLIB_COST_CEILING = 1e-16
"""Squared-price cost above which QuantLib's own fit is not trustworthy.

A precondition, not a result. Comparing against a QuantLib curve that failed to
converge measures nothing, so the tests assert this first and fail with
QuantLib's own diagnostic rather than reporting a spurious spot-rate gap.
"""


# --------------------------------------------------------------------------- #
# The universe: the maturities of the calibration recovery test
# --------------------------------------------------------------------------- #
SNAPSHOT_DATE = date(2026, 8, 14)
"""Kept identical to ``tests/unit/test_calibration.py``, so the taus match."""

BENCHMARK_MATURITIES: dict[str, date] = {
    "TFIT08031127": date(2027, 11, 3),
    "TFIT16280428": date(2028, 4, 28),
    "TFIT05220829": date(2029, 8, 22),
    "TFIT16180930": date(2030, 9, 18),
    "TFIT16300632": date(2032, 6, 30),
    "TFIT16181034": date(2034, 10, 18),
    "TFIT16090736": date(2036, 7, 9),
    "TFIT16281140": date(2040, 11, 28),
    "TFIT23250746": date(2046, 7, 25),
    "TFIT34130358": date(2058, 3, 13),
}
"""The ten benchmark TES of the recovery test, ~1y to ~32y."""

COUPON_RATES: dict[str, float] = {
    "TFIT08031127": 0.0550,
    "TFIT16280428": 0.0600,
    "TFIT05220829": 0.0600,
    "TFIT16180930": 0.0750,
    "TFIT16300632": 0.0700,
    "TFIT16181034": 0.0725,
    "TFIT16090736": 0.0625,
    "TFIT16281140": 0.0725,
    "TFIT23250746": 0.0700,
    "TFIT34130358": 0.0725,
}
"""Annual coupons, used only by the objective-gap test. TES tasa fija pay annually."""

TRUE_PARAMS = NSSParams(
    beta0=0.09,
    beta1=-0.02,
    beta2=0.01,
    beta3=0.005,
    lambda1=2.0,
    lambda2=8.0,
)
"""The same synthetic COP curve the recovery test fits, and noiseless here."""

TEST_MATURITIES: NDArray[np.float64] = np.linspace(0.5, 15.0, 10)
"""The ten probe maturities, 0.5y to 15y, at which the two curves are compared."""

FACE_VALUE = 100.0
SETTLEMENT_DAYS = 0
"""Zero, so the bond settlement date is the evaluation date and no calendar rolls."""


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _quantlib_evaluation_date() -> Iterator[None]:
    """Pin QuantLib's global evaluation date, and put it back afterwards.

    ``ql.Settings.instance()`` is a process-wide singleton, so a test that left
    the date moved would silently change the meaning of every later one.
    """
    settings = ql.Settings.instance()
    previous = settings.evaluationDate
    settings.evaluationDate = _ql_date(SNAPSHOT_DATE)
    try:
        yield
    finally:
        settings.evaluationDate = previous


def _ql_date(day: date) -> ql.Date:
    """Convert a :class:`datetime.date` to a QuantLib date."""
    return ql.Date(day.day, day.month, day.year)


def _tau(nemotecnico: str) -> float:
    """ACT/365 years from the snapshot to maturity, the config's day count.

    Deliberately the same arithmetic as ``tests/unit/test_calibration.py::_tau``
    and the same number QuantLib's ``Actual365Fixed`` produces from the curve
    reference date, which is what lets the two systems see one shared grid.
    """
    return (BENCHMARK_MATURITIES[nemotecnico] - SNAPSHOT_DATE).days / 365.0


def _true_curve(taus: NDArray[np.float64]) -> NDArray[np.float64]:
    """Evaluate :data:`TRUE_PARAMS` at ``taus``, always as an array."""
    return np.asarray(nss_yield(taus, *TRUE_PARAMS.to_array()), dtype=np.float64)


def _fixed_rate_bond(nemotecnico: str, coupon_rate: float) -> ql.FixedRateBond:
    """Build the QuantLib bond for one benchmark.

    Annual schedule rolled backwards from maturity, ``ACT/365`` accrual,
    ``NullCalendar`` with unadjusted rolls (see the module docstring), and no
    settlement lag, so the bond settles on the curve's reference date.
    """
    schedule = ql.Schedule(
        _ql_date(SNAPSHOT_DATE),
        _ql_date(BENCHMARK_MATURITIES[nemotecnico]),
        ql.Period(ql.Annual),
        ql.NullCalendar(),
        ql.Unadjusted,
        ql.Unadjusted,
        ql.DateGeneration.Backward,
        False,
    )
    return ql.FixedRateBond(
        SETTLEMENT_DAYS,
        FACE_VALUE,
        schedule,
        [coupon_rate],
        ql.Actual365Fixed(),
        ql.Unadjusted,
        FACE_VALUE,
    )


def _clean_price_on_true_curve(bond: ql.FixedRateBond) -> float:
    """Discount ``bond``'s remaining flows on :data:`TRUE_PARAMS` and strip accrued.

    The year fraction of every cash flow is taken with ``Actual365Fixed`` from
    the reference date, which is precisely how
    ``FittedBondDiscountCurve``'s cost function times its own discounting. Any
    other route to a price would leave a day-count difference in the residual
    and turn a curve test into a date-arithmetic test.
    """
    day_counter = ql.Actual365Fixed()
    reference = _ql_date(SNAPSHOT_DATE)
    dirty = sum(
        flow.amount()
        * float(nss_discount_factor(day_counter.yearFraction(reference, flow.date()), TRUE_PARAMS))
        for flow in bond.cashflows()
        if flow.date() > reference
    )
    return float(dirty) - bond.accruedAmount(reference)


def _svensson_guess(
    observed_yields: NDArray[np.float64],
    taus: NDArray[np.float64],
) -> ql.Array:
    """A neutral starting point for QuantLib, in QuantLib's own parameterisation.

    The betas come from :func:`~tes_pricer.math.calibration.initial_beta_guess`,
    the very seed our own multi-start uses, so neither system is handed a better
    starting point than the other; the decays are seeded at 3y and 10y, which
    are not the answer (2y and 8y) and are passed as ``1/lambda`` because that
    is what ``SvenssonFitting`` expects.

    A guess is required rather than optional. Left to default from an all-zero
    vector, QuantLib's Simplex stalls on this universe at a cost of ~10 with
    ``lambda1 == lambda2``, i.e. a collapsed, meaningless fit. That is an
    optimiser failure, not a modelling one, and seeding past it is what makes
    the comparison about the curve.
    """
    beta0, beta1, beta2, beta3 = initial_beta_guess(observed_yields, taus)
    return ql.Array([beta0, beta1, beta2, beta3, 1.0 / 3.0, 1.0 / 10.0])


def _fit_quantlib_svensson(
    helpers: list[ql.BondHelper],
    guess: ql.Array,
    optimization_method: ql.OptimizationMethod | None = None,
) -> ql.FittedBondDiscountCurve:
    """Calibrate ``ql.SvenssonFitting`` to ``helpers`` and return the fitted curve."""
    fitting = (
        ql.SvenssonFitting()
        if optimization_method is None
        else ql.SvenssonFitting(ql.Array(), optimization_method)
    )
    curve = ql.FittedBondDiscountCurve(
        _ql_date(SNAPSHOT_DATE),
        helpers,
        ql.Actual365Fixed(),
        fitting,
        1e-12,  # accuracy
        20_000,  # maxEvaluations
        guess,
        0.05,  # simplexLambda: the guess is good, so start the simplex small
    )
    curve.enableExtrapolation()
    return curve


def _quantlib_params(curve: ql.FittedBondDiscountCurve) -> NSSParams:
    """Read the fitted solution back as :class:`NSSParams`, undoing ``kappa = 1/lambda``.

    The inversion is the whole point of this helper: everything downstream then
    compares like with like, and the one place the convention is applied is
    covered by
    :func:`test_quantlib_parameterises_the_decay_as_kappa_not_lambda`.
    """
    solution = list(curve.fitResults().solution())
    beta0, beta1, beta2, beta3, kappa1, kappa2 = (float(value) for value in solution)
    return NSSParams(
        beta0=beta0,
        beta1=beta1,
        beta2=beta2,
        beta3=beta3,
        lambda1=1.0 / kappa1,
        lambda2=1.0 / kappa2,
    )


def _quantlib_spot_rates(curve: ql.FittedBondDiscountCurve) -> NDArray[np.float64]:
    """Continuously compounded zero rates from ``curve`` at :data:`TEST_MATURITIES`."""
    return np.array(
        [
            curve.zeroRate(float(tau), ql.Continuous, ql.NoFrequency).rate()
            for tau in TEST_MATURITIES
        ],
        dtype=np.float64,
    )


def _zero_coupon_universe() -> (
    tuple[list[str], NDArray[np.float64], NDArray[np.float64], list[ql.BondHelper]]
):
    """The strict-comparison universe: ten zero-coupon bonds priced off the true curve.

    Returns:
        ``(nemotecnicos, taus, observed_yields, helpers)``. ``observed_yields``
        are the continuously compounded zero rates, which for a zero-coupon bond
        *are* its yield to maturity, so they can be fed to
        :func:`~tes_pricer.math.calibration.calibrate_nss` unconverted.
    """
    nemotecnicos = list(BENCHMARK_MATURITIES)
    taus = np.array([_tau(n) for n in nemotecnicos], dtype=np.float64)
    observed_yields = _true_curve(taus)
    helpers = []
    for nemotecnico, tau, zero_rate in zip(nemotecnicos, taus, observed_yields, strict=True):
        bond = _fixed_rate_bond(nemotecnico, coupon_rate=0.0)
        price = FACE_VALUE * float(np.exp(-zero_rate * tau))
        helpers.append(ql.BondHelper(ql.QuoteHandle(ql.SimpleQuote(price)), bond, True))
    return nemotecnicos, taus, observed_yields, helpers


# --------------------------------------------------------------------------- #
# 1. The benchmark
# --------------------------------------------------------------------------- #
def test_nss_matches_quantlib_svensson_curve() -> None:
    """Our calibration and QuantLib's must agree on parameters and on spot rates.

    Procedure:

    1. Build the ten benchmark maturities as ``ql.FixedRateBond`` objects on
       ``Actual365Fixed`` and ``NullCalendar`` (no Colombian calendar exists;
       see the module docstring), priced exactly off :data:`TRUE_PARAMS`.
    2. Fit ``ql.FittedBondDiscountCurve`` with ``ql.SvenssonFitting``.
    3. Compare the fitted parameters, after undoing QuantLib's
       ``kappa = 1/lambda``, against
       :func:`~tes_pricer.math.calibration.calibrate_nss` on the same data.
    4. Compare the implied spot rates at ten maturities from 0.5y to 15y, which
       is the robust comparison: it survives any reparameterisation that leaves
       the curve unchanged.

    The data is noiseless and exactly representable by an NSS curve, so both
    fits should reach the true parameters, not merely each other. The assertions
    check both, which is strictly stronger than checking agreement: two systems
    can agree while both being wrong.
    """
    nemotecnicos, taus, observed_yields, helpers = _zero_coupon_universe()

    quantlib_curve = _fit_quantlib_svensson(helpers, _svensson_guess(observed_yields, taus))
    cost = quantlib_curve.fitResults().minimumCostValue()
    assert cost < QUANTLIB_COST_CEILING, (
        f"QuantLib's own fit did not converge (squared price cost {cost:.3e} >= "
        f"{QUANTLIB_COST_CEILING:.0e} after "
        f"{quantlib_curve.fitResults().numberOfIterations()} iterations); comparing against "
        "it would measure QuantLib's optimiser, not our curve"
    )

    ours = calibrate_nss(observed_yields, taus, nemotecnicos, n_multistart=25)
    assert ours.warnings == (), f"our fit reported diagnostics: {ours.warnings}"
    assert (
        ours.rmse < SYNTHETIC_SPOT_TOLERANCE
    ), f"our fit did not reproduce noiseless data: RMSE {ours.rmse_bps:.4f}bp"

    # 3. Parameters, on a shared parameterisation.
    quantlib_params = _quantlib_params(quantlib_curve)
    for name, theirs, mine, truth in zip(
        ("beta0", "beta1", "beta2", "beta3", "lambda1", "lambda2"),
        quantlib_params.to_array(),
        ours.to_params().to_array(),
        TRUE_PARAMS.to_array(),
        strict=True,
    ):
        assert theirs == pytest.approx(
            truth, abs=PARAMETER_TOLERANCE
        ), f"QuantLib's {name} = {theirs!r} is not the true {truth!r}"
        assert mine == pytest.approx(
            theirs, abs=PARAMETER_TOLERANCE
        ), f"{name}: ours {mine!r} vs QuantLib {theirs!r}"

    # 4. Spot rates - the comparison that survives a reparameterisation.
    our_spots = np.asarray(nss_yield(TEST_MATURITIES, *ours.to_params().to_array()), dtype=float)
    quantlib_spots = _quantlib_spot_rates(quantlib_curve)
    differences = np.abs(our_spots - quantlib_spots)
    worst = int(np.argmax(differences))
    assert differences.max() < SYNTHETIC_SPOT_TOLERANCE, (
        f"spot rates disagree by {differences.max() * 1e4:.4f}bp at "
        f"tau = {TEST_MATURITIES[worst]:.3f}y (ours {our_spots[worst]:.6%}, "
        f"QuantLib {quantlib_spots[worst]:.6%}); tolerance is "
        f"{SYNTHETIC_SPOT_TOLERANCE * 1e4:.0f}bp. Do not widen it - audit the decay "
        "convention, the compounding basis and the sign of each loading first"
    )
    assert np.max(np.abs(quantlib_spots - _true_curve(TEST_MATURITIES))) < SYNTHETIC_SPOT_TOLERANCE


# --------------------------------------------------------------------------- #
# 2. The conventions the benchmark depends on
# --------------------------------------------------------------------------- #
def test_quantlib_parameterises_the_decay_as_kappa_not_lambda() -> None:
    """``SvenssonFitting`` takes decay *rates*; ours takes decay *time constants*.

    Uses ``FittedBondDiscountCurve``'s parameters-given constructor, so no
    optimiser and no bond data are involved: this compares two closed-form
    curves and nothing else. Handing QuantLib our lambdas verbatim is off by
    tens of basis points, which is the failure mode this pins down.
    """
    maximum_date = _ql_date(date(2066, 8, 14))

    def spot_rates(decays: tuple[float, float]) -> NDArray[np.float64]:
        curve = ql.FittedBondDiscountCurve(
            _ql_date(SNAPSHOT_DATE),
            ql.SvenssonFitting(),
            ql.Array(
                [
                    TRUE_PARAMS.beta0,
                    TRUE_PARAMS.beta1,
                    TRUE_PARAMS.beta2,
                    TRUE_PARAMS.beta3,
                    *decays,
                ]
            ),
            maximum_date,
            ql.Actual365Fixed(),
        )
        curve.enableExtrapolation()
        return _quantlib_spot_rates(curve)

    ours = _true_curve(TEST_MATURITIES)
    reciprocal = np.abs(spot_rates((1.0 / TRUE_PARAMS.lambda1, 1.0 / TRUE_PARAMS.lambda2)) - ours)
    verbatim = np.abs(spot_rates((TRUE_PARAMS.lambda1, TRUE_PARAMS.lambda2)) - ours)

    assert reciprocal.max() < 1e-12, (
        f"with kappa = 1/lambda the two curves must be the same function; worst gap "
        f"{reciprocal.max() * 1e4:.6f}bp"
    )
    assert verbatim.max() > SYNTHETIC_SPOT_TOLERANCE, (
        "passing lambda where QuantLib expects kappa was expected to be visibly wrong, "
        f"but the worst gap is only {verbatim.max() * 1e4:.4f}bp - if this now passes, "
        "the conversion in _quantlib_params may have become a no-op"
    )


def test_two_optimisers_find_different_parameters_but_the_same_curve() -> None:
    """Non-convexity, measured: Levenberg-Marquardt lands elsewhere and still agrees.

    QuantLib's ``SvenssonFitting`` defaults to Simplex; Levenberg-Marquardt has
    to be passed in explicitly. On this universe the two settle on visibly
    different parameter vectors - the second decay constant differs by more than
    an order of magnitude - while drawing curves that agree to well inside 1bp.

    This is the evidence behind :data:`REAL_DATA_SPOT_TOLERANCE`. Comparing raw
    parameters is only meaningful where the fit is exactly identified; comparing
    spot rates is meaningful always, which is why the acceptance criterion is
    written on the spot rates.
    """
    _, taus, observed_yields, helpers = _zero_coupon_universe()
    guess = _svensson_guess(observed_yields, taus)

    simplex = _fit_quantlib_svensson(helpers, guess)
    marquardt = _fit_quantlib_svensson(helpers, guess, ql.LevenbergMarquardt(1e-12, 1e-12, 1e-12))

    simplex_params = _quantlib_params(simplex).to_array()
    marquardt_params = _quantlib_params(marquardt).to_array()
    assert np.max(np.abs(simplex_params - marquardt_params)) > PARAMETER_TOLERANCE, (
        "the two optimisers were expected to disagree on the raw parameters; if they no "
        "longer do, this test has stopped demonstrating anything"
    )

    differences = np.abs(_quantlib_spot_rates(simplex) - _quantlib_spot_rates(marquardt))
    assert differences.max() < SYNTHETIC_SPOT_TOLERANCE, (
        f"two fits of the same exact data drew curves {differences.max() * 1e4:.4f}bp apart, "
        "which is more than the non-convexity of an exactly-representable problem allows"
    )


# --------------------------------------------------------------------------- #
# 3. The limitation the benchmark must not paper over
# --------------------------------------------------------------------------- #
def test_coupon_bonds_expose_the_yield_space_objective_gap() -> None:
    """On coupon bonds the two systems disagree by far more than 1bp, and should.

    QuantLib fits prices and recovers :data:`TRUE_PARAMS` exactly.
    :func:`~tes_pricer.math.calibration.calibrate_nss` fits yield residuals
    ``z(tau_i) - y_i``, and a coupon bond's yield to maturity is not the zero
    rate at its maturity: it is a cash-flow-weighted blend of the curve up to
    that point, quoted on an annually compounded basis rather than a continuous
    one. Two distinct errors therefore stack, and the fitted curve is tens to
    hundreds of basis points off.

    This is asserted rather than skipped because it is the reason the strict
    benchmark above runs on zeros, and because a future price-space objective
    (the ``v2`` note in :mod:`tes_pricer.math.calibration`) should make this
    test fail loudly - at which point the fix is to promote the coupon universe
    into the strict benchmark, not to delete this.
    """
    nemotecnicos = list(BENCHMARK_MATURITIES)
    taus = np.array([_tau(n) for n in nemotecnicos], dtype=np.float64)

    helpers, observed_yields = [], []
    for nemotecnico in nemotecnicos:
        coupon_rate = COUPON_RATES[nemotecnico]
        bond = _fixed_rate_bond(nemotecnico, coupon_rate)
        clean = _clean_price_on_true_curve(bond)
        accrued = bond.accruedAmount(_ql_date(SNAPSHOT_DATE))
        observed_yields.append(
            ytm_from_dirty_price(
                clean + accrued,
                coupon_rate,
                FACE_VALUE,
                SNAPSHOT_DATE,
                BENCHMARK_MATURITIES[nemotecnico],
                1,
                "ACT/365",
            )
        )
        helpers.append(ql.BondHelper(ql.QuoteHandle(ql.SimpleQuote(clean)), bond, True))
    yields = np.array(observed_yields, dtype=np.float64)

    quantlib_curve = _fit_quantlib_svensson(helpers, _svensson_guess(yields, taus))
    assert quantlib_curve.fitResults().minimumCostValue() < QUANTLIB_COST_CEILING
    quantlib_spots = _quantlib_spot_rates(quantlib_curve)
    assert (
        np.max(np.abs(quantlib_spots - _true_curve(TEST_MATURITIES))) < SYNTHETIC_SPOT_TOLERANCE
    ), (
        "QuantLib's price-space fit should still recover the true curve from exact "
        "coupon-bond prices; if it does not, the synthetic prices are wrong and the rest "
        "of this test proves nothing"
    )

    ours = calibrate_nss(yields, taus, nemotecnicos, n_multistart=25)
    our_spots = np.asarray(nss_yield(TEST_MATURITIES, *ours.to_params().to_array()), dtype=float)
    gap = float(np.max(np.abs(our_spots - quantlib_spots)))
    assert gap > REAL_DATA_SPOT_TOLERANCE, (
        f"the yield-space objective was expected to be visibly biased on coupon bonds, but "
        f"the worst gap is only {gap * 1e4:.2f}bp. If calibration.py has moved to a "
        "price-space objective, promote the coupon universe into "
        "test_nss_matches_quantlib_svensson_curve and delete this test"
    )
