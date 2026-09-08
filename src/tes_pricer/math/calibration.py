r"""Multi-start least-squares calibration of the Nelson-Siegel-Svensson curve.

What is fitted
--------------
The six NSS parameters against **observed yields to maturity**, one per
benchmark bond, minimising the unweighted sum of squared yield residuals

.. math:: \min_\theta \sum_i \big(z(\tau_i;\theta) - y_i\big)^2

with :func:`scipy.optimize.least_squares`, method ``trf`` (Trust Region
Reflective), which is the only one of the three that honours bounds.

Why yields and not prices - a stated v1 limitation
--------------------------------------------------
Fitting yields is the pedagogical standard (Nelson-Siegel 1987, Diebold-Li
2006): one closed-form curve evaluation per bond, no coupon schedule needed,
and a residual vector already in the unit a trader reads (basis points). It is
what this module does, and it is deliberately *not* what a professional desk
does.

The reason the difference matters is a real bias rather than a matter of taste.
The curve exists to **discount cash flows**, so the quantity that should be
small is the *price* error. Price error and yield error are linked by modified
duration, ``dP/P ~= -D_mod * dy``: one basis point of yield error is worth
about a cent of price on a one-year bond and about twenty cents on a
thirty-year bond. So the two objectives disagree, and each one left raw has the
opposite pathology:

* fitting **yields** with equal weights spends the model's flexibility on the
  short end, where a basis point is nearly free in price terms; while
* fitting **prices** with equal weights lets the long end dominate, because a
  high-duration bond's price moves far more for the same yield miss.

The standard correction is to fit price residuals **weighted by the inverse of
modified duration**, which rescales every price residual back into
yield-equivalent units and equalises the instruments' influence while keeping
the objective consistent with how the curve is consumed. (Note the direction:
the weight exists to stop *high*-duration bonds from dominating a price fit -
long bonds have the greater price sensitivity to a yield error, not the
shorter ones.)

.. todo::

   v2: calibrate on price error weighted by inverse modified duration.
   Concretely, replace :func:`yield_residuals` with a residual callable that,
   for each bond, builds the coupon schedule, prices it off the trial curve via
   ``bond_pricing.price_from_discount_factors(cashflows,
   nss_discount_factor(...))``, and returns
   ``(model_clean_price - observed_clean_price) / modified_duration_i``. The
   duration is computed once, from the *observed* yield, so the weights stay
   fixed across iterations and the problem remains a plain least-squares one
   rather than a moving target. Everything else here - the Latin-hypercube
   multi-start, the bounds, the best-RMSE selection, the degeneracy
   diagnostics - carries over unchanged; only the residual changes.

Why multi-start, and why Latin hypercube
----------------------------------------
Held fixed, ``lambda1``/``lambda2`` make the model **linear** in the four
betas. Free, they make the objective **non-convex and multi-modal**: distinct
decay pairs produce near-identical fits in-sample while implying very different
curves between and beyond the observed maturities. A single local solve
therefore reports whichever basin its starting point fell into, with no signal
that another basin fits better. Multi-start is not a robustness nicety here,
it is what makes the answer well defined.

The starting decay pairs come from a Latin hypercube
(:class:`scipy.stats.qmc.LatinHypercube`) rather than a regular grid or uniform
random draws: LHS stratifies every one-dimensional projection, so ``n`` points
cover ``n`` distinct bands of both ``lambda1`` and ``lambda2``, which neither a
coarse grid (which aliases with the hump locations) nor i.i.d. uniform sampling
(which clumps) does.

No network access, no I/O: ``observed_yields`` arrives already validated from
:mod:`tes_pricer.data.validators`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.optimize import least_squares
from scipy.stats import qmc

from tes_pricer.math.nss_model import N_PARAMS, NSSParams, nss_yield

__all__ = [
    "BETA_ABS_BOUND",
    "DEGENERACY_RELATIVE_TOLERANCE",
    "FLAT_CURVE_ABS_TOLERANCE",
    "LAMBDA_LOWER_BOUND",
    "LAMBDA_UPPER_BOUND",
    "MIN_BONDS",
    "NSSCalibrationError",
    "NSSCalibrationResult",
    "calibrate_nss",
    "initial_beta_guess",
    "lambda_multistart_points",
    "yield_residuals",
]

BETA_ABS_BOUND = 0.5
"""Soft bound on every beta: no realistic rate scenario needs |beta| > 50%.

Not an economic constraint - it is a numerical guard rail. Without it the
optimiser can wander into a region where a huge ``beta1`` is cancelled by a
huge ``beta2``, which fits in-sample, extrapolates absurdly, and stalls the
trust region on a nearly singular Jacobian.
"""

LAMBDA_LOWER_BOUND = 0.01
LAMBDA_UPPER_BOUND = 30.0
"""Hard bounds on the decay constants, in years.

Strictly positive because the loadings are undefined otherwise. Bounded above
because past roughly the longest observed maturity a decay is numerically
indistinguishable from a constant: ``lambda -> inf`` collapses the slope
loading onto the level one and the curvature loading onto zero, so the fit
stops identifying the parameter and the Jacobian column goes to zero.
"""

DEGENERACY_RELATIVE_TOLERANCE = 0.05
"""``|lambda1 - lambda2| / lambda1`` below which the Svensson term is redundant."""

FLAT_CURVE_ABS_TOLERANCE = 1e-4
"""1bp: below this, ``beta1``/``beta2``/``beta3`` are indistinguishable from zero."""

MIN_BONDS = 4
"""Fewest observations accepted.

Below six the six-parameter fit is formally under-determined. Four is still
accepted, and warned about, because a four-point fit is exactly the diagnostic
that shows *why* the benchmark universe needs eight: it drives the in-sample
RMSE to nearly zero while the implied curve swings wildly between multi-starts.
See ``tests/unit/test_calibration.py::test_four_bonds_overfit_and_are_unstable_where_ten_bonds_are_not``.
"""

MAX_NFEV = 2000
"""Function-evaluation budget per start. Generous; a healthy fit uses ~50."""

SOLVER_TOLERANCE = 1e-12
"""``ftol``/``xtol``/``gtol``. Tight, because a 1bp fit error is 1e-4 in these units."""

X_SCALE: tuple[float, ...] = (0.1, 0.1, 0.1, 0.1, 1.0, 1.0)
"""Characteristic magnitude of each parameter, for the trust-region metric.

The betas live near 0.1 and the decays near 1-10, two orders of magnitude
apart. Left unscaled, the trust region is effectively spherical in a space
where one direction is 100x coarser than another, and the solver crawls.
"""

DEFAULT_SEED = 0
"""Default Latin-hypercube seed.

Fixed rather than ``None`` on purpose: a calibration that returns a different
curve on every call is not reproducible, and reproducibility is the whole point
of the archived-snapshot design in :mod:`tes_pricer.data.socrata_client`. Vary
it explicitly to probe the stability of a fit.
"""


class NSSCalibrationError(RuntimeError):
    """Raised when no multi-start converged, with the diagnostics to act on.

    Deliberately an error rather than a partial result: a non-converged
    parameter vector prices bonds, discounts cash flows and feeds the FX leg
    just as happily as a converged one, and nothing downstream would notice.
    """


@dataclass(frozen=True, slots=True)
class NSSCalibrationResult:
    """A converged calibration and everything needed to judge whether to trust it."""

    beta0: float
    beta1: float
    beta2: float
    beta3: float
    lambda1: float
    lambda2: float
    rmse: float
    """Root mean squared yield error, as a decimal (``0.0012`` = 12bps)."""
    max_abs_error: float
    """Largest absolute yield error over all bonds, as a decimal."""
    n_bonds_used: int
    converged: bool
    n_starts_tried: int
    optimization_message: str
    per_bond_errors: dict[str, float]
    """``{identifier: signed model-minus-observed yield error in bps}``."""
    warnings: tuple[str, ...] = ()
    """Non-fatal diagnostics. Empty means nothing looked degenerate."""
    n_starts_converged: int = 0

    @property
    def rmse_bps(self) -> float:
        """:attr:`rmse` in basis points, which is how a fit is actually quoted."""
        return self.rmse * 1e4

    def to_params(self) -> NSSParams:
        """Return the fitted parameters as the curve module's value object."""
        return NSSParams(
            beta0=self.beta0,
            beta1=self.beta1,
            beta2=self.beta2,
            beta3=self.beta3,
            lambda1=self.lambda1,
            lambda2=self.lambda2,
        )


# --------------------------------------------------------------------------- #
# Objective
# --------------------------------------------------------------------------- #
def yield_residuals(
    param_vector: NDArray[np.float64],
    maturities: NDArray[np.float64],
    observed_yields: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Model-minus-observed yield residuals: the callable handed to the solver.

    Sign convention is ``model - observed``, so a positive residual means the
    fitted curve sits above the market. ``least_squares`` squares them, so the
    sign is irrelevant to the fit and relevant only to
    :attr:`NSSCalibrationResult.per_bond_errors`, where "we are 3bps rich here"
    is the reading a trader wants.

    Args:
        param_vector: ``(beta0, beta1, beta2, beta3, lambda1, lambda2)``.
        maturities: Maturities in years, aligned with ``observed_yields``.
        observed_yields: Observed YTMs as decimals.

    Returns:
        The residual vector, shaped like ``observed_yields``.
    """
    beta0, beta1, beta2, beta3, lambda1, lambda2 = (float(v) for v in param_vector)
    model = np.asarray(
        nss_yield(maturities, beta0, beta1, beta2, beta3, lambda1, lambda2),
        dtype=np.float64,
    )
    return model - observed_yields


def initial_beta_guess(
    observed_yields: NDArray[np.float64],
    maturities: NDArray[np.float64],
) -> tuple[float, float, float, float]:
    """Seed the betas from the observed curve shape, given any decay pair.

    Reads the two parameters the data actually pins down and stays neutral on
    the two it does not:

    * ``beta0`` is the asymptotic long rate, so the mean of the yields in the
      longest quartile (at least one bond) - a mean rather than the single
      longest yield because the long end is the illiquid end, and one stale
      quote should not set the level.
    * ``beta1`` is the short-end spread, ``z(0+) - beta0``, estimated as
      ``shortest observed yield - beta0``.
    * ``beta2 = beta3 = 0``: no hump assumed. Starting the curvatures at zero
      lets the first Gauss-Newton step decide their sign from the residuals,
      instead of the seed committing to a hump the data may not support.

    The guess is independent of ``lambda1``/``lambda2``, which is what lets one
    beta seed serve every multi-start point.
    """
    order = np.argsort(maturities)
    sorted_yields = observed_yields[order]
    n_long = max(1, sorted_yields.size // 4)
    beta0 = float(np.mean(sorted_yields[-n_long:]))
    beta1 = float(sorted_yields[0]) - beta0
    return beta0, beta1, 0.0, 0.0


def lambda_multistart_points(
    n_multistart: int,
    lambda_grid_bounds: tuple[float, float],
    seed: int | None = DEFAULT_SEED,
) -> NDArray[np.float64]:
    """Return an ``(n_multistart, 2)`` array of starting ``(lambda1, lambda2)`` pairs.

    Latin hypercube over the square ``lambda_grid_bounds ** 2``, then each pair
    is sorted so ``lambda1 <= lambda2``. The sort is the identifiability
    convention of :class:`~tes_pricer.math.nss_model.NSSParams`, and folding the
    square onto its upper triangle costs no coverage: the LHS stratification
    survives the fold, and the two orderings of a pair are not the same model
    anyway (``lambda1`` drives both the slope and the first hump, ``lambda2``
    only the second), so the fold merely fixes which of the two the seed means.

    Raises:
        ValueError: If ``n_multistart < 1`` or the bounds are not a strictly
            increasing pair inside :data:`LAMBDA_LOWER_BOUND` ..
            :data:`LAMBDA_UPPER_BOUND`.
    """
    if n_multistart < 1:
        raise ValueError(f"n_multistart must be at least 1; got {n_multistart!r}")
    low, high = (float(b) for b in lambda_grid_bounds)
    if not np.isfinite(low) or not np.isfinite(high) or low >= high:
        raise ValueError(
            f"lambda_grid_bounds must be a finite, strictly increasing pair; got {lambda_grid_bounds!r}"
        )
    if low < LAMBDA_LOWER_BOUND or high > LAMBDA_UPPER_BOUND:
        raise ValueError(
            f"lambda_grid_bounds {lambda_grid_bounds!r} must lie inside the solver bounds "
            f"[{LAMBDA_LOWER_BOUND}, {LAMBDA_UPPER_BOUND}]"
        )
    unit_sample = np.asarray(
        qmc.LatinHypercube(d=2, seed=seed).random(n=n_multistart), dtype=np.float64
    )
    scaled = low + unit_sample * (high - low)
    return np.sort(scaled, axis=1)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _validated_inputs(
    observed_yields: ArrayLike,
    maturities: ArrayLike,
    bond_isins: list[str],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Coerce and check the three aligned inputs, or raise ``ValueError``."""
    yields = np.asarray(observed_yields, dtype=np.float64).ravel()
    taus = np.asarray(maturities, dtype=np.float64).ravel()
    if not (yields.size == taus.size == len(bond_isins)):
        raise ValueError(
            "observed_yields, maturities and bond_isins must be aligned; got sizes "
            f"{yields.size}, {taus.size}, {len(bond_isins)}"
        )
    if yields.size < MIN_BONDS:
        raise ValueError(
            f"NSS calibration needs at least {MIN_BONDS} instruments; got {yields.size}"
        )
    if not np.all(np.isfinite(yields)) or not np.all(np.isfinite(taus)):
        raise ValueError("observed_yields and maturities must be finite; filter upstream")
    if np.any(taus <= 0.0):
        raise ValueError(f"maturities must be strictly positive years; got min {taus.min()!r}")
    duplicates = sorted({isin for isin in bond_isins if bond_isins.count(isin) > 1})
    if duplicates:
        raise ValueError(
            f"bond_isins must be unique, else per_bond_errors loses rows: {duplicates}"
        )
    return yields, taus


def _solver_bounds() -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """The ``(lower, upper)`` box handed to ``trf``."""
    lower = np.array([-BETA_ABS_BOUND] * 4 + [LAMBDA_LOWER_BOUND, LAMBDA_LOWER_BOUND])
    upper = np.array([BETA_ABS_BOUND] * 4 + [LAMBDA_UPPER_BOUND, LAMBDA_UPPER_BOUND])
    return lower, upper


def _clipped_into_box(
    x0: NDArray[np.float64],
    lower: NDArray[np.float64],
    upper: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Pull a starting point strictly inside the box.

    ``trf`` rejects an infeasible ``x0``, and a seed sitting exactly *on* a
    bound starts the trust region against a wall. The margin is relative to the
    edge width so it scales with the parameter.
    """
    margin = 1e-6 * (upper - lower)
    return np.clip(x0, lower + margin, upper - margin)


def _diagnostic_warnings(
    params: NSSParams,
    n_bonds: int,
) -> tuple[str, ...]:
    """Non-fatal findings about the converged fit."""
    messages: list[str] = []

    separation = abs(params.lambda1 - params.lambda2) / abs(params.lambda1)
    if separation < DEGENERACY_RELATIVE_TOLERANCE:
        messages.append(
            f"degenerate Svensson term: lambda1={params.lambda1:.4f} and "
            f"lambda2={params.lambda2:.4f} differ by {separation:.2%} "
            f"(< {DEGENERACY_RELATIVE_TOLERANCE:.0%}), so the two curvature loadings are "
            "collinear and the fit has effectively collapsed to a 4-parameter "
            "Nelson-Siegel; beta2 and beta3 are individually meaningless, only their "
            "sum is identified"
        )

    slope_and_humps = (params.beta1, params.beta2, params.beta3)
    if max(abs(b) for b in slope_and_humps) < FLAT_CURVE_ABS_TOLERANCE:
        messages.append(
            f"flat/degenerate curve: beta1, beta2 and beta3 are all below "
            f"{FLAT_CURVE_ABS_TOLERANCE * 1e4:.0f}bp, so the fit is the constant "
            f"z(tau) = {params.beta0:.4%}; lambda1 and lambda2 are unidentified here "
            "(their Jacobian columns vanish) and carry no information"
        )

    at_bound = [
        f"{name}={value:.4f}"
        for name, value in (("lambda1", params.lambda1), ("lambda2", params.lambda2))
        if value <= LAMBDA_LOWER_BOUND * 1.01 or value >= LAMBDA_UPPER_BOUND * 0.99
    ]
    if at_bound:
        messages.append(
            f"decay constant pinned at a solver bound ({', '.join(at_bound)}): the data does "
            f"not locate the hump inside [{LAMBDA_LOWER_BOUND}, {LAMBDA_UPPER_BOUND}] years, "
            "so the reported value is the bound, not an estimate"
        )

    pinned_betas = [
        f"{name}={value:+.4f}"
        for name, value in (
            ("beta0", params.beta0),
            ("beta1", params.beta1),
            ("beta2", params.beta2),
            ("beta3", params.beta3),
        )
        if abs(value) >= BETA_ABS_BOUND * 0.999
    ]
    if pinned_betas:
        messages.append(
            f"beta pinned at the soft bound ({', '.join(pinned_betas)}): the unconstrained "
            f"optimum lies outside +/-{BETA_ABS_BOUND:.0%}, which in practice means two loadings "
            "are cancelling each other; the in-sample fit may still be good but the "
            "extrapolated curve is not trustworthy"
        )

    if params.beta0 <= 0.0 or params.beta0 + params.beta1 <= 0.0:
        messages.append(
            f"non-economic fit: implied long rate beta0 = {params.beta0:.4%} and instantaneous "
            f"short rate beta0 + beta1 = {params.beta0 + params.beta1:.4%}; one of them is not "
            "positive. Selection is by lowest RMSE only, so an over-fitted curve can win "
            "in-sample while being unusable for discounting - reject this fit or re-run with a "
            "wider bond universe"
        )

    if n_bonds < N_PARAMS:
        messages.append(
            f"under-determined fit: {n_bonds} observations for {N_PARAMS} free parameters, so "
            "the residuals can be driven to zero by interpolating the market noise; the RMSE "
            "below is not evidence of a good curve"
        )

    return tuple(messages)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def calibrate_nss(
    observed_yields: ArrayLike,
    maturities: ArrayLike,
    bond_isins: list[str],
    n_multistart: int = 25,
    lambda_grid_bounds: tuple[float, float] = (0.1, 15.0),
    seed: int | None = DEFAULT_SEED,
) -> NSSCalibrationResult:
    """Fit the six NSS parameters to observed yields by multi-start least squares.

    The algorithm, in the order it runs:

    1. Draw ``n_multistart`` starting ``(lambda1, lambda2)`` pairs by Latin
       hypercube over ``lambda_grid_bounds`` (:func:`lambda_multistart_points`).
    2. Seed the betas once from the observed curve shape
       (:func:`initial_beta_guess`); they do not depend on the decay pair.
    3. Run :func:`scipy.optimize.least_squares` with ``method="trf"`` from each
       start, inside the box of :data:`BETA_ABS_BOUND` and
       :data:`LAMBDA_LOWER_BOUND` .. :data:`LAMBDA_UPPER_BOUND`.
    4. Score every *successful* run by its RMSE in yield.
    5. Keep the lowest-RMSE run. If none succeeded, raise
       :class:`NSSCalibrationError` with the collected solver messages rather
       than returning a partial fit.
    6. Attach diagnostics: Svensson degeneracy, a flat curve, a decay pinned at
       a bound, an under-determined fit.

    Args:
        observed_yields: YTMs as decimals (``0.1208`` = 12.08%), one per bond.
        maturities: Time to maturity in years, aligned with ``observed_yields``.
        bond_isins: Identifiers, aligned and unique, used as the keys of
            :attr:`NSSCalibrationResult.per_bond_errors`. In this repository
            ``tes_referencia.yaml`` carries ``isin: null`` for every TES - the
            ISIN is not published in open primary sources - so what is passed
            here is in practice the ``nemotecnico``. The parameter keeps its
            name because that is the field the rest of the pipeline calls it.
        n_multistart: Number of Latin-hypercube starts. 25 is comfortably past
            the point where extra starts stop finding new basins on a 10-bond
            COP curve; raise it if the fit is used on a wider universe.
        lambda_grid_bounds: Range the starting decays are drawn from. Narrower
            than the solver bounds on purpose - the solver may still walk
            outside it, this only seeds.
        seed: Latin-hypercube seed; see :data:`DEFAULT_SEED`.

    Returns:
        The best converged fit and its diagnostics.

    Raises:
        ValueError: If the inputs are misaligned, non-finite, too few, carry a
            non-positive maturity or a duplicate identifier, or if the
            multi-start configuration is invalid.
        NSSCalibrationError: If no start converged.
    """
    yields, taus = _validated_inputs(observed_yields, maturities, bond_isins)
    starts = lambda_multistart_points(n_multistart, lambda_grid_bounds, seed)
    lower, upper = _solver_bounds()
    beta_seed = initial_beta_guess(yields, taus)

    best_x: NDArray[np.float64] | None = None
    best_rmse = np.inf
    best_message = ""
    n_converged = 0
    failures: list[str] = []

    for lambda1, lambda2 in starts:
        x0 = _clipped_into_box(
            np.array([*beta_seed, lambda1, lambda2], dtype=np.float64), lower, upper
        )
        try:
            outcome = least_squares(
                yield_residuals,
                x0,
                args=(taus, yields),
                method="trf",
                bounds=(lower, upper),
                x_scale=np.array(X_SCALE, dtype=np.float64),
                ftol=SOLVER_TOLERANCE,
                xtol=SOLVER_TOLERANCE,
                gtol=SOLVER_TOLERANCE,
                max_nfev=MAX_NFEV,
            )
        except (ValueError, np.linalg.LinAlgError) as exc:  # pragma: no cover - defensive
            failures.append(
                f"start ({lambda1:.3f}, {lambda2:.3f}) raised {type(exc).__name__}: {exc}"
            )
            continue
        if not bool(outcome.success):
            failures.append(f"start ({lambda1:.3f}, {lambda2:.3f}): {outcome.message}")
            continue
        n_converged += 1
        residuals = np.asarray(outcome.fun, dtype=np.float64)
        rmse = float(np.sqrt(np.mean(residuals**2)))
        if rmse < best_rmse:
            best_rmse = rmse
            best_x = np.asarray(outcome.x, dtype=np.float64)
            best_message = str(outcome.message)

    if best_x is None:
        raise NSSCalibrationError(
            f"no Latin-hypercube start converged: {len(starts)} tried over "
            f"lambda bounds {lambda_grid_bounds!r} with seed {seed!r}, on {yields.size} bonds "
            f"spanning {taus.min():.2f}-{taus.max():.2f} years "
            f"(yields {yields.min():.4%}-{yields.max():.4%}). Solver messages: "
            + " | ".join(failures[:5])
        )

    params = NSSParams.from_array(best_x)
    final_residuals = yield_residuals(best_x, taus, yields)
    return NSSCalibrationResult(
        beta0=params.beta0,
        beta1=params.beta1,
        beta2=params.beta2,
        beta3=params.beta3,
        lambda1=params.lambda1,
        lambda2=params.lambda2,
        rmse=best_rmse,
        max_abs_error=float(np.max(np.abs(final_residuals))),
        n_bonds_used=int(yields.size),
        converged=True,
        n_starts_tried=int(len(starts)),
        optimization_message=best_message,
        per_bond_errors={
            isin: float(residual) * 1e4
            for isin, residual in zip(bond_isins, final_residuals, strict=True)
        },
        warnings=_diagnostic_warnings(params, int(yields.size)),
        n_starts_converged=n_converged,
    )
