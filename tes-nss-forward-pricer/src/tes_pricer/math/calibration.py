"""Least-squares calibration of the NSS curve to observed TES prices.

Design decisions the implementation must honour:

* **Fit prices, not yields.** Minimising yield error over-weights the short end,
  where a basis point is worth almost nothing in price terms. The default
  objective is the price residual weighted by ``1 / duration``, which is the
  standard fix for that bias.
* **Two-stage optimisation.** ``lambda1``/``lambda2`` enter non-linearly and the
  objective is multi-modal in them. Grid-search the decay pair, solve the betas
  by linear least squares on each grid node, then polish the full six-parameter
  vector with :func:`scipy.optimize.least_squares` (Trust Region Reflective,
  which accepts bounds).
* **No network access.** ``observed`` arrives already validated from
  :mod:`tes_pricer.data.validators`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from tes_pricer.math.bond_pricing import BondTerms
from tes_pricer.math.nss_model import NSSParams


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """Outcome of a single calibration run, including its diagnostics."""

    params: NSSParams
    settlement_date: date
    rmse_price: float
    """Root mean squared price error, per 100 of face."""
    rmse_yield_bps: float
    """Root mean squared yield error, in basis points."""
    max_abs_error_bps: float
    n_instruments: int
    n_iterations: int
    converged: bool
    residuals: NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class CalibrationSettings:
    """Solver configuration, kept separate so runs stay reproducible."""

    lambda1_grid: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0, 3.0)
    lambda2_grid: tuple[float, ...] = (4.0, 6.0, 8.0, 12.0)
    weight_by_inverse_duration: bool = True
    max_nfev: int = 2000
    ftol: float = 1e-12
    xtol: float = 1e-12
    loss: str = "soft_l1"
    """Robust loss keeps a single stale quote from dragging the whole curve."""


def price_residuals(
    param_vector: NDArray[np.float64],
    bonds: list[BondTerms],
    settlement_date: date,
    observed_clean_prices: NDArray[np.float64],
    weights: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Weighted price residuals: the callable handed to ``least_squares``."""
    raise NotImplementedError("Phase 6: NSS residual function")


def initial_guess(
    observed: pd.DataFrame,
    settings: CalibrationSettings,
) -> NSSParams:
    """Seed the optimiser from the observed curve shape.

    Uses the longest observed yield for ``beta0``, the short-minus-long spread
    for ``beta1`` and zero curvature, which is a stable starting point across
    regimes.
    """
    raise NotImplementedError("Phase 6: NSS initial guess")


def calibrate_nss(
    bonds: list[BondTerms],
    observed_clean_prices: NDArray[np.float64],
    settlement_date: date,
    settings: CalibrationSettings | None = None,
) -> CalibrationResult:
    """Calibrate NSS parameters to observed TES clean prices.

    Args:
        bonds: Benchmark bond terms, already resolved from the reference config.
        observed_clean_prices: Clean prices per 100 of face, aligned with ``bonds``.
        settlement_date: Valuation date the prices refer to.
        settings: Solver configuration; defaults to :class:`CalibrationSettings`.

    Raises:
        ValueError: If fewer than six instruments are supplied (the model has
            six free parameters) or the inputs are misaligned.
    """
    raise NotImplementedError("Phase 6: NSS calibration driver")
