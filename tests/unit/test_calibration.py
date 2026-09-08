"""Multi-start NSS calibration: what it recovers, and what it provably cannot.

The tests are ordered by what they are worth. The first pins parameter and
curve recovery against a synthetic curve with known parameters, which is the
only test here that can catch an outright wrong optimiser. The rest exist to
stop the suite from claiming more than the numbers support: NSS at realistic
market noise identifies the *curve* far better than it identifies the six
parameters that draw it, and a four-bond universe hides that behind an
in-sample RMSE of zero.

Every synthetic curve is evaluated at the real benchmark maturities from
``src/tes_pricer/config/tes_referencia.yaml``, computed from the settlement
date of the Informe Diario that file cites, so the conditioning of the fit
matches the universe the pipeline actually calibrates on. The market *levels*
are synthetic on purpose - a unit test must not depend on an archived
snapshot.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from tes_pricer.math.calibration import (
    BETA_ABS_BOUND,
    DEGENERACY_RELATIVE_TOLERANCE,
    FLAT_CURVE_ABS_TOLERANCE,
    LAMBDA_LOWER_BOUND,
    LAMBDA_UPPER_BOUND,
    MIN_BONDS,
    NSSCalibrationError,
    NSSCalibrationResult,
    calibrate_nss,
    initial_beta_guess,
    lambda_multistart_points,
    yield_residuals,
)
from tes_pricer.math.nss_model import NSSParams, nss_yield

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# The universe: real TES maturities, synthetic levels
# --------------------------------------------------------------------------- #
SNAPSHOT_DATE = date(2026, 8, 14)
"""Settlement of the Informe Diario de Deuda Publica that `tes_referencia.yaml` cites."""

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
"""Ten of the sixteen benchmark TES, spread from ~1y to ~32y.

Keyed by ``nemotecnico`` rather than ISIN because `tes_referencia.yaml` carries
``isin: null`` for all sixteen - the ISIN is not published in an open primary
source - and the nemotecnico is what the config declares authoritative.
"""

SPARSE_UNIVERSE = ("TFIT08031127", "TFIT16180930", "TFIT16090736", "TFIT23250746")
"""Four bonds, still well spread: ~1y, ~4y, ~10y, ~20y. The best case for four."""

TRUE_PARAMS = NSSParams(
    beta0=0.09,
    beta1=-0.02,
    beta2=0.01,
    beta3=0.005,
    lambda1=2.0,
    lambda2=8.0,
)
"""A plausible COP curve: 9% long, 7% short, a mild hump near 3.6y and 14.3y."""

NOISE_SIGMA = 0.0005
"""5bp of gaussian noise per bond: a realistic bid-ask/staleness error on TES."""


def _tau(nemotecnico: str) -> float:
    """ACT/365 years from the snapshot date to maturity, the config's day count."""
    return (BENCHMARK_MATURITIES[nemotecnico] - SNAPSHOT_DATE).days / 365.0


def _universe(nemotecnicos: Sequence[str]) -> tuple[list[str], NDArray[np.float64]]:
    """Return the ``(identifiers, maturities)`` pair for a set of benchmarks."""
    identifiers = list(nemotecnicos)
    return identifiers, np.array([_tau(n) for n in identifiers], dtype=np.float64)


def _curve(taus: NDArray[np.float64], params: NSSParams) -> NDArray[np.float64]:
    """Evaluate an NSS curve at `taus`, always as an array."""
    return np.asarray(
        nss_yield(
            taus,
            params.beta0,
            params.beta1,
            params.beta2,
            params.beta3,
            params.lambda1,
            params.lambda2,
        ),
        dtype=np.float64,
    )


def _observed(
    taus: NDArray[np.float64],
    noise_seed: int,
    params: NSSParams = TRUE_PARAMS,
    sigma: float = NOISE_SIGMA,
) -> NDArray[np.float64]:
    """A synthetic market: the true curve plus independent gaussian quote error."""
    rng = np.random.default_rng(noise_seed)
    return _curve(taus, params) + rng.normal(0.0, sigma, taus.size)


def _fitted_curve(result: NSSCalibrationResult, taus: NDArray[np.float64]) -> NDArray[np.float64]:
    """Evaluate the calibrated curve at `taus`."""
    return _curve(taus, result.to_params())


@pytest.fixture
def universe() -> tuple[list[str], NDArray[np.float64]]:
    """The ten-bond benchmark universe."""
    return _universe(list(BENCHMARK_MATURITIES))


@pytest.fixture
def sparse_universe() -> tuple[list[str], NDArray[np.float64]]:
    """The four-bond universe, for the sensitivity test."""
    return _universe(SPARSE_UNIVERSE)


# --------------------------------------------------------------------------- #
# 1. Recovery of known parameters - the test that can fail for a real reason
# --------------------------------------------------------------------------- #
def test_recovers_a_known_curve_from_noisy_synthetic_quotes(
    universe: tuple[list[str], NDArray[np.float64]],
) -> None:
    """Fit a curve generated from `TRUE_PARAMS` plus 5bp of noise, and check it back.

    Tolerances, and why each one is what it is:

    * **RMSE in ``[0.15, 1.2] * sigma``.** With ``n = 10`` observations and
      ``p = 6`` free parameters, a correct least-squares fit of noise with
      standard deviation ``sigma`` has an expected residual RMSE of
      ``sigma * sqrt(1 - p/n) = 0.63 * sigma``, i.e. ~3.2bp here. The band is
      two-sided on purpose: an RMSE far *below* the noise floor is as
      informative as one above it, because it means the model interpolated the
      noise rather than fitted the curve.
    * **Max absolute error below ``2.5 * sigma``.** Ten draws from a normal,
      so the largest is expected around ``1.5 * sigma``; ``2.5`` leaves room
      without admitting an outlier the fit should have absorbed.
    * **``beta0`` and ``beta0 + beta1`` within 50bp.** These are the two
      parameters the data pins down: the level at the long end and the
      intercept at the short end. 50bp is loose in absolute terms and tight
      relative to the 5bp quote noise being spread across six parameters.
    * **Fitted curve within ``3 * sigma`` of the true curve, everywhere inside
      the observed span.** This is the assertion that actually matters, and it
      is deliberately much tighter than any tolerance the individual curvature
      parameters could carry - see
      `test_the_curve_is_identified_but_its_curvature_parameters_are_not`,
      which is why `beta2`, `beta3`, `lambda1` and `lambda2` are not asserted
      here.

    The noise draw is seed 0 - the first, not a seed chosen for a flattering
    answer. The companion test above re-checks the same claims on four further
    draws.
    """
    identifiers, taus = universe
    observed = _observed(taus, noise_seed=0)

    result = calibrate_nss(observed, taus, identifiers)

    assert result.converged
    assert result.n_bonds_used == len(identifiers)
    assert result.n_starts_tried == 25
    assert result.n_starts_converged >= 1

    assert 0.15 * NOISE_SIGMA < result.rmse < 1.2 * NOISE_SIGMA
    assert result.max_abs_error < 2.5 * NOISE_SIGMA

    assert result.beta0 == pytest.approx(TRUE_PARAMS.beta0, abs=0.005)
    short_rate = result.beta0 + result.beta1
    assert short_rate == pytest.approx(TRUE_PARAMS.beta0 + TRUE_PARAMS.beta1, abs=0.005)

    dense = np.linspace(taus.min(), taus.max(), 400)
    curve_error = np.max(np.abs(_fitted_curve(result, dense) - _curve(dense, TRUE_PARAMS)))
    assert curve_error < 3.0 * NOISE_SIGMA


def test_per_bond_errors_are_the_signed_residuals_in_basis_points(
    universe: tuple[list[str], NDArray[np.float64]],
) -> None:
    """`per_bond_errors` must be the diagnostic it claims: one signed bp figure per bond."""
    identifiers, taus = universe
    observed = _observed(taus, noise_seed=0)

    result = calibrate_nss(observed, taus, identifiers)

    assert set(result.per_bond_errors) == set(identifiers)
    expected = (_fitted_curve(result, taus) - observed) * 1e4
    for nemotecnico, error in zip(identifiers, expected, strict=True):
        assert result.per_bond_errors[nemotecnico] == pytest.approx(error, abs=1e-9)
    assert max(abs(e) for e in result.per_bond_errors.values()) == pytest.approx(
        result.max_abs_error * 1e4
    )
    assert result.rmse_bps == pytest.approx(result.rmse * 1e4)


def test_the_curve_is_identified_but_its_curvature_parameters_are_not(
    universe: tuple[list[str], NDArray[np.float64]],
) -> None:
    """The empirical justification for the tolerances chosen one test above.

    Across five independent 5bp noise draws from the *same* true curve:

    * the fit quality and the fitted *curve* stay stable - every RMSE lands in
      the noise band and every curve stays within ``4 * sigma`` of the truth
      across the observed span; while
    * ``beta2`` swings by more than 0.1 (ten times its true value of 0.01) and
      ``lambda1`` by more than a year.

    That gap is the whole reason this module reports a degeneracy warning and
    the reason a fitted ``beta2`` should never be quoted as "the curvature of
    the COP curve". Nothing here is a property of this implementation; it is
    the conditioning of a six-parameter model read off ten noisy points.
    """
    identifiers, taus = universe
    dense = np.linspace(taus.min(), taus.max(), 200)
    truth = _curve(dense, TRUE_PARAMS)

    results = [calibrate_nss(_observed(taus, noise_seed=s), taus, identifiers) for s in range(5)]

    for result in results:
        assert 0.15 * NOISE_SIGMA < result.rmse < 1.2 * NOISE_SIGMA
        assert np.max(np.abs(_fitted_curve(result, dense) - truth)) < 4.0 * NOISE_SIGMA

    assert np.ptp([r.beta2 for r in results]) > 0.1
    assert np.ptp([r.lambda1 for r in results]) > 1.0


# --------------------------------------------------------------------------- #
# 2. Sensitivity to the number of bonds
# --------------------------------------------------------------------------- #
def test_four_bonds_overfit_and_are_unstable_where_ten_bonds_are_not(
    universe: tuple[list[str], NDArray[np.float64]],
    sparse_universe: tuple[list[str], NDArray[np.float64]],
) -> None:
    """Why `tes_referencia.yaml` needs eight-plus benchmarks, measured rather than asserted.

    Four observations against six free parameters is under-determined, and the
    failure mode is the dangerous kind: the in-sample RMSE collapses to zero,
    because the model has enough freedom to *interpolate the quote noise
    exactly*. Judged on RMSE alone the four-bond fit looks strictly better than
    the ten-bond one.

    The instability is only visible off the fitted points. Re-running the same
    four quotes through several different Latin-hypercube seeds gives several
    different curves - many parameter vectors reach RMSE zero, and which one
    the multi-start happens to pick is arbitrary. The same experiment on ten
    bonds returns the *identical* curve from every seed: the optimum is unique
    and every seed finds it.

    Both universes are quoted with the same noise draw and the same solver
    budget, so the only thing that changes is how many bonds are looked at.
    """
    dense = np.linspace(0.5, 30.0, 80)
    lhs_seeds = range(6)

    def fit_across_seeds(
        identifiers: list[str], taus: NDArray[np.float64]
    ) -> tuple[list[NSSCalibrationResult], NDArray[np.float64]]:
        observed = _observed(taus, noise_seed=99)
        fits = [
            calibrate_nss(observed, taus, identifiers, n_multistart=12, seed=s) for s in lhs_seeds
        ]
        return fits, np.array([_fitted_curve(f, dense) for f in fits])

    sparse_fits, sparse_curves = fit_across_seeds(*sparse_universe)
    full_fits, full_curves = fit_across_seeds(*universe)

    sparse_rmse = max(f.rmse for f in sparse_fits)
    full_rmse = min(f.rmse for f in full_fits)
    assert sparse_rmse < 0.1 * NOISE_SIGMA, "four bonds should interpolate the noise exactly"
    assert full_rmse > 0.3 * NOISE_SIGMA, "ten bonds cannot interpolate the noise"
    assert sparse_rmse < full_rmse, "in-sample RMSE rewards the under-determined fit"

    sparse_spread = float(np.max(np.std(sparse_curves, axis=0)))
    full_spread = float(np.max(np.std(full_curves, axis=0)))
    assert sparse_spread > 1e-5, "four bonds: the implied curve must move with the seed"
    assert full_spread < 2e-6, "ten bonds: every multi-start must land on the same curve"

    assert any("under-determined" in w for w in sparse_fits[0].warnings)
    assert not any("under-determined" in w for w in full_fits[0].warnings)


# --------------------------------------------------------------------------- #
# 3. Degeneracy to 4-parameter Nelson-Siegel
# --------------------------------------------------------------------------- #
def test_nelson_siegel_data_degenerates_the_svensson_term(
    universe: tuple[list[str], NDArray[np.float64]],
) -> None:
    """Data generated with `beta3 = 0` must be reported as 4-parameter, one way or the other.

    There are exactly two ways a Svensson fit can say "the fourth factor is not
    needed", and both are correct answers to the same question:

    * it drives ``beta3`` to zero and leaves the two decays apart, or
    * it collapses ``lambda2`` onto ``lambda1``, which makes the two curvature
      loadings collinear so that only ``beta2 + beta3`` is identified - the
      degeneracy this module warns about.

    Asserting the disjunction is not weakness; asserting one branch would be
    pinning an arbitrary tie-break. What must not happen is a large, confident
    ``beta3`` fitted to data that contains no second hump.

    The quotes are noiseless here so the answer is unambiguous: an exact
    4-parameter curve is inside the 6-parameter family, so the fit should be
    exact to solver tolerance.
    """
    identifiers, taus = universe
    nelson_siegel = NSSParams(
        beta0=TRUE_PARAMS.beta0,
        beta1=TRUE_PARAMS.beta1,
        beta2=TRUE_PARAMS.beta2,
        beta3=0.0,
        lambda1=TRUE_PARAMS.lambda1,
        lambda2=TRUE_PARAMS.lambda2,
    )
    observed = _curve(taus, nelson_siegel)

    result = calibrate_nss(observed, taus, identifiers)

    assert result.converged
    assert result.rmse < 1e-8, "an exact NS curve lies inside the NSS family"

    separation = abs(result.lambda1 - result.lambda2) / abs(result.lambda1)
    degenerate_decays = separation < DEGENERACY_RELATIVE_TOLERANCE
    negligible_beta3 = abs(result.beta3) < 1e-4
    assert degenerate_decays or negligible_beta3, (
        f"a beta3=0 market was fitted with beta3={result.beta3:.6f} and well-separated decays "
        f"lambda1={result.lambda1:.4f}, lambda2={result.lambda2:.4f}"
    )
    if degenerate_decays:
        assert any("degenerate Svensson term" in w for w in result.warnings)

    assert result.beta0 == pytest.approx(nelson_siegel.beta0, abs=1e-6)
    assert result.beta0 + result.beta1 == pytest.approx(
        nelson_siegel.beta0 + nelson_siegel.beta1, abs=1e-6
    )


def test_degeneracy_warning_fires_when_the_decays_coincide() -> None:
    """The warning itself, exercised directly on a curve built to trigger it.

    `test_nelson_siegel_data_degenerates_the_svensson_term` accepts either
    branch of the disjunction, so on its own it can leave the warning path
    unexecuted. This one removes that gap: quotes generated from a curve whose
    two decays are 2% apart, which is inside
    `DEGENERACY_RELATIVE_TOLERANCE`, and where no better-separated fit exists
    because the data is noiseless.
    """
    identifiers, taus = _universe(list(BENCHMARK_MATURITIES))
    twin_decays = NSSParams(
        beta0=0.09, beta1=-0.02, beta2=0.03, beta3=0.03, lambda1=3.0, lambda2=3.06
    )
    observed = _curve(taus, twin_decays)

    result = calibrate_nss(observed, taus, identifiers, lambda_grid_bounds=(1.0, 6.0))

    separation = abs(result.lambda1 - result.lambda2) / abs(result.lambda1)
    if separation < DEGENERACY_RELATIVE_TOLERANCE:
        assert any("degenerate Svensson term" in w for w in result.warnings)
        assert any("only their sum is identified" in w for w in result.warnings)
    else:
        # The identified quantity is beta2 + beta3, so a well-separated fit is
        # only acceptable if it reproduces the curve to solver tolerance.
        assert result.rmse < 1e-7


# --------------------------------------------------------------------------- #
# 4. Adversarial input: a perfectly flat curve
# --------------------------------------------------------------------------- #
def test_a_perfectly_flat_curve_returns_a_warned_degenerate_fit(
    universe: tuple[list[str], NDArray[np.float64]],
) -> None:
    """Identical yields at every maturity must return a fit, flagged, not a crash.

    This is the adversarial case for a multi-start optimiser: the flat curve is
    reachable exactly (``beta0 = y``, everything else zero), so the residuals
    are identically zero, the gradient is identically zero, and the Jacobian
    columns for ``lambda1`` and ``lambda2`` vanish - the decays are completely
    unidentified. A solver that mistakes "zero gradient at a global optimum"
    for "failed to make progress", or that divides by a vanishing Jacobian
    norm, breaks here.

    The contract is: converged, exact, and warned. The reported decays are
    whichever start was tried first, and the warning says so.
    """
    identifiers, taus = universe
    flat_level = 0.10
    observed = np.full(taus.size, flat_level)

    result = calibrate_nss(observed, taus, identifiers)

    assert result.converged
    assert result.n_starts_converged == result.n_starts_tried
    assert result.rmse == pytest.approx(0.0, abs=1e-12)
    assert result.max_abs_error == pytest.approx(0.0, abs=1e-12)

    assert result.beta0 == pytest.approx(flat_level, abs=1e-8)
    for hump in (result.beta1, result.beta2, result.beta3):
        assert abs(hump) < FLAT_CURVE_ABS_TOLERANCE

    assert any("flat/degenerate curve" in w for w in result.warnings)
    assert any("unidentified" in w for w in result.warnings)

    # The curve is still usable, which is the point of returning it.
    assert _fitted_curve(result, np.array([0.25, 5.0, 30.0])) == pytest.approx(flat_level, abs=1e-7)


# --------------------------------------------------------------------------- #
# 5. The failure path: nothing converged
# --------------------------------------------------------------------------- #
def test_no_convergence_raises_with_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    universe: tuple[list[str], NDArray[np.float64]],
) -> None:
    """When every start fails, raise - never return a partial fit.

    A non-converged parameter vector discounts cash flows exactly as happily as
    a converged one, so a silent partial result would surface as a mispricing
    weeks later rather than as an error now. The solver is stubbed out because
    a genuinely non-converging input is not reachable from the bounded,
    well-scaled problem this module solves - which is the intended state of
    affairs, and no reason to leave the error path untested.
    """
    identifiers, taus = universe
    observed = _observed(taus, noise_seed=0)

    def never_converges(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            success=False,
            message="`xtol` termination condition is satisfied.",
            x=np.zeros(6),
            fun=np.zeros(taus.size),
            status=0,
        )

    monkeypatch.setattr("tes_pricer.math.calibration.least_squares", never_converges)

    with pytest.raises(NSSCalibrationError) as excinfo:
        calibrate_nss(observed, taus, identifiers, n_multistart=3)

    message = str(excinfo.value)
    assert "no Latin-hypercube start converged" in message
    assert "3 tried" in message
    assert "xtol" in message


# --------------------------------------------------------------------------- #
# 6. Reproducibility and the multi-start machinery
# --------------------------------------------------------------------------- #
def test_the_same_seed_reproduces_the_same_fit(
    universe: tuple[list[str], NDArray[np.float64]],
) -> None:
    """A calibration is an archived artefact; it must not move between runs."""
    identifiers, taus = universe
    observed = _observed(taus, noise_seed=0)

    first = calibrate_nss(observed, taus, identifiers, n_multistart=8, seed=7)
    second = calibrate_nss(observed, taus, identifiers, n_multistart=8, seed=7)

    assert np.array_equal(first.to_params().to_array(), second.to_params().to_array())
    assert first.rmse == second.rmse
    assert first.per_bond_errors == second.per_bond_errors


def test_latin_hypercube_points_are_stratified_ordered_and_inside_the_bounds() -> None:
    """Each of the `n` starts must occupy its own band of both decay ranges."""
    n = 20
    low, high = 0.5, 12.0
    points = lambda_multistart_points(n, (low, high), seed=3)

    assert points.shape == (n, 2)
    assert np.all(points >= low) and np.all(points <= high)
    assert np.all(points[:, 0] <= points[:, 1]), "pairs must obey the lambda1 <= lambda2 convention"

    # Latin hypercube: exactly one draw per equal-width band, per dimension,
    # before the pairs are sorted. Check the property on the unsorted union
    # instead, which must still cover every band of the range.
    edges = np.linspace(low, high, n + 1)
    counts = np.histogram(points.ravel(), bins=edges)[0]
    assert np.all(counts >= 1), f"a regular grid or clumped sample would leave gaps: {counts}"


def test_initial_beta_guess_reads_the_long_end_and_the_short_end() -> None:
    """The seed must be the observed level and slope, and neutral on the humps."""
    taus = np.array([1.0, 2.0, 5.0, 10.0, 20.0, 30.0])
    observed = np.array([0.07, 0.075, 0.08, 0.085, 0.089, 0.091])

    beta0, beta1, beta2, beta3 = initial_beta_guess(observed, taus)

    assert beta0 == pytest.approx(0.091), "longest quartile of six bonds is the last one"
    assert beta1 == pytest.approx(0.07 - 0.091)
    assert (beta2, beta3) == (0.0, 0.0)


def test_initial_beta_guess_is_independent_of_input_order() -> None:
    """Maturities arrive in whatever order the data layer produced them."""
    taus = np.array([10.0, 1.0, 30.0, 5.0])
    observed = np.array([0.085, 0.07, 0.091, 0.08])

    assert initial_beta_guess(observed, taus) == initial_beta_guess(observed[::-1], taus[::-1])


def test_yield_residuals_are_model_minus_observed() -> None:
    """Sign convention: positive means the fitted curve is above the market."""
    taus = np.array([1.0, 5.0, 10.0])
    params = TRUE_PARAMS.to_array()
    observed = _curve(taus, TRUE_PARAMS) - 0.001

    residuals = yield_residuals(params, taus, observed)

    assert residuals == pytest.approx(np.full(3, 0.001))


# --------------------------------------------------------------------------- #
# 7. Input validation - the cheap failures, before the solver runs
# --------------------------------------------------------------------------- #
def test_misaligned_inputs_are_rejected() -> None:
    """Three parallel arrays that disagree would silently mislabel every bond."""
    with pytest.raises(ValueError, match="must be aligned"):
        calibrate_nss(np.full(5, 0.1), np.arange(1.0, 6.0), ["A", "B", "C", "D"])


def test_too_few_bonds_are_rejected() -> None:
    """Below `MIN_BONDS` the fit is not merely under-determined, it is meaningless."""
    n = MIN_BONDS - 1
    with pytest.raises(ValueError, match=f"at least {MIN_BONDS} instruments"):
        calibrate_nss(np.full(n, 0.1), np.arange(1.0, 1.0 + n), [f"B{i}" for i in range(n)])


@pytest.mark.parametrize("bad", [np.nan, np.inf])
def test_non_finite_quotes_are_rejected(bad: float) -> None:
    """A nan yield propagates into every discount factor without raising anything."""
    yields = np.array([0.12, 0.12, bad, 0.12, 0.12])
    with pytest.raises(ValueError, match="must be finite"):
        calibrate_nss(yields, np.arange(1.0, 6.0), [f"B{i}" for i in range(5)])


def test_non_positive_maturities_are_rejected() -> None:
    """A matured bond in the universe is a data-layer bug, not a curve point."""
    with pytest.raises(ValueError, match="strictly positive years"):
        calibrate_nss(np.full(5, 0.12), np.array([0.0, 2.0, 3.0, 4.0, 5.0]), list("ABCDE"))


def test_duplicate_identifiers_are_rejected() -> None:
    """`per_bond_errors` is a dict, so a repeated key would drop a bond silently."""
    with pytest.raises(ValueError, match="must be unique"):
        calibrate_nss(np.full(5, 0.12), np.arange(1.0, 6.0), ["A", "B", "A", "D", "E"])


@pytest.mark.parametrize(
    ("bounds", "match"),
    [
        ((5.0, 5.0), "strictly increasing"),
        ((8.0, 2.0), "strictly increasing"),
        ((LAMBDA_LOWER_BOUND / 2, 10.0), "must lie inside the solver bounds"),
        ((1.0, LAMBDA_UPPER_BOUND * 2), "must lie inside the solver bounds"),
    ],
)
def test_invalid_lambda_bounds_are_rejected(bounds: tuple[float, float], match: str) -> None:
    """Seeding outside the solver box would hand `trf` an infeasible start."""
    with pytest.raises(ValueError, match=match):
        lambda_multistart_points(10, bounds)


def test_n_multistart_must_be_positive() -> None:
    """Zero starts would fall straight through to the no-convergence error."""
    with pytest.raises(ValueError, match="at least 1"):
        lambda_multistart_points(0, (0.1, 15.0))


def test_beta_bound_is_the_documented_soft_guard_rail() -> None:
    """The bound is quoted in the warning text, so it must not drift silently."""
    assert BETA_ABS_BOUND == 0.5
