r"""Pure Nelson-Siegel-Svensson term structure.

The instantaneous-forward parameterisation, with :math:`\tau` in years:

.. math::

    z(\tau) = \beta_0
      + \beta_1 \frac{1 - e^{-\tau/\lambda_1}}{\tau/\lambda_1}
      + \beta_2 \left(\frac{1 - e^{-\tau/\lambda_1}}{\tau/\lambda_1}
                      - e^{-\tau/\lambda_1}\right)
      + \beta_3 \left(\frac{1 - e^{-\tau/\lambda_2}}{\tau/\lambda_2}
                      - e^{-\tau/\lambda_2}\right)

Interpretation: ``beta0`` is the asymptotic long rate, ``beta1`` the short-end
spread (:math:`\beta_0 + \beta_1` is the instantaneous short rate), ``beta2``
and ``beta3`` the two curvature humps located by ``lambda1`` and ``lambda2``.

Numerical note: the loading terms are removable singularities at
:math:`\tau = 0`. Implementations must take the limits
(:math:`1` and :math:`0` respectively) instead of dividing by zero.

This module has no optimiser and no I/O. Calibration lives in
:mod:`tes_pricer.math.calibration`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class NSSParams:
    """The six Nelson-Siegel-Svensson parameters.

    Rates are decimals (``0.095`` = 9.5%) and ``lambda1``/``lambda2`` are decay
    time constants in years, both strictly positive and, by convention,
    ``lambda1 < lambda2`` to keep the fit identifiable.
    """

    beta0: float
    beta1: float
    beta2: float
    beta3: float
    lambda1: float
    lambda2: float

    def to_array(self) -> NDArray[np.float64]:
        """Return the parameters as the ordered vector used by the optimiser."""
        raise NotImplementedError("Phase 5: NSS parameter packing")

    @classmethod
    def from_array(cls, values: NDArray[np.float64]) -> NSSParams:
        """Rebuild the parameters from the optimiser's ordered vector."""
        raise NotImplementedError("Phase 5: NSS parameter unpacking")

    def is_admissible(self) -> bool:
        """Return whether the parameters satisfy the economic sanity constraints.

        Checks the positivity of the decay constants, ``beta0 > 0`` (positive
        long rate) and ``beta0 + beta1 > 0`` (positive instantaneous short rate).
        """
        raise NotImplementedError("Phase 5: NSS admissibility checks")


def nss_zero_rate(
    tau: NDArray[np.float64],
    params: NSSParams,
) -> NDArray[np.float64]:
    """Continuously compounded zero rate at maturities ``tau`` (in years)."""
    raise NotImplementedError("Phase 5: NSS zero curve")


def nss_instantaneous_forward(
    tau: NDArray[np.float64],
    params: NSSParams,
) -> NDArray[np.float64]:
    """Instantaneous forward rate implied by ``params`` at maturities ``tau``."""
    raise NotImplementedError("Phase 5: NSS forward curve")


def nss_discount_factor(
    tau: NDArray[np.float64],
    params: NSSParams,
) -> NDArray[np.float64]:
    """Discount factors ``exp(-z(tau) * tau)`` implied by ``params``."""
    raise NotImplementedError("Phase 5: NSS discount factors")


def nss_loadings(
    tau: NDArray[np.float64],
    lambda1: float,
    lambda2: float,
) -> NDArray[np.float64]:
    """Return the ``(len(tau), 4)`` design matrix of factor loadings.

    Column order matches ``(beta0, beta1, beta2, beta3)``. Holding
    ``lambda1``/``lambda2`` fixed makes the model linear in the betas, which is
    what allows the hybrid grid-plus-least-squares calibration to work.
    """
    raise NotImplementedError("Phase 5: NSS factor loadings")
