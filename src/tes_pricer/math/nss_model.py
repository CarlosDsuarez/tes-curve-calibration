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

Compounding convention
----------------------
**The zero curve produced here is continuously compounded**, so

.. math:: DF(\tau) = e^{-z(\tau)\,\tau}

and never :math:`DF(\tau) = (1 + z(\tau))^{-\tau}`. This is a decision, stated
rather than left implicit, and the reasons are:

* forwards become differences of log discount factors, so they are additive in
  :math:`z\tau` and need no per-period root extraction (see
  :func:`nss_forward_rate`, reused by the Phase 5 OIS bootstrap and by the
  Phase 6 CIP forward pricer);
* the instantaneous forward is then an *analytic* derivative of the curve
  (:func:`nss_instantaneous_forward`), which is what makes the curve greeks in
  :mod:`tes_pricer.math.greeks` differentiable rather than bumped; and
* it is the convention the FX leg already assumes -
  :mod:`tes_pricer.math.fx_forward` prices Garman-Kohlhagen off continuously
  compounded rates, and mixing bases across the two legs of a CIP trade is a
  classic source of silent basis-point errors.

Relation to :mod:`tes_pricer.math.bond_pricing`, which quotes ``ytm`` as a
**nominal annual rate compounded ``frequency`` times a year**: the two
conventions are not in conflict because they never meet as *rates*. They meet
as *prices*. ``bond_pricing.dirty_price_from_ytm`` collapses a whole curve into
one discrete yield for quoting, while the calibration objective prices the same
cash flows off this curve through
``bond_pricing.price_from_discount_factors(cashflows, nss_discount_factor(...))``.
A price computed either way is the same number; only the rate that labels it
differs. When a curve point has to be shown on the Colombian street basis (an
effective annual rate), convert explicitly: ``y_effective = exp(z) - 1``, or
``frequency * (exp(z / frequency) - 1)`` for a nominal rate compounded
``frequency`` times a year.

Numerical note: the loading terms are removable singularities at
:math:`\tau = 0`. Implementations must take the limits
(:math:`1` and :math:`0` respectively) instead of dividing by zero.

This module has no optimiser and no I/O. Calibration lives in
:mod:`tes_pricer.math.calibration`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import TypeAlias

import numpy as np
from numpy.typing import ArrayLike, NDArray

__all__ = [
    "NSSParams",
    "nss_discount_factor",
    "nss_forward_rate",
    "nss_instantaneous_forward",
    "nss_loadings",
    "nss_yield",
    "nss_zero_rate",
]

FloatOrArray: TypeAlias = float | NDArray[np.float64]
"""What every curve function accepts and mirrors back: a scalar in, a scalar out."""

N_PARAMS = 6
"""Length of the packed parameter vector handed to the optimiser."""

LEVEL_LOADING_SERIES_CUTOFF = 1e-7
"""Below this ``|tau / lambda|`` the level loading uses its Taylor series.

The cutoff exists for one reason only: at ``tau == 0`` exactly the ratio is a
literal ``0 / 0``, which numpy evaluates to ``nan`` with a warning rather than
raising, and a ``nan`` on the short end propagates silently into every discount
factor and every price downstream. It is *not* there to hide cancellation:
:func:`numpy.expm1` already computes ``1 - exp(-x)`` to full relative precision
for small ``x``, so the two branches agree to about ``1e-23`` relative at the
cutoff and the loading stays smooth across it.
"""


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
        return np.array(
            [self.beta0, self.beta1, self.beta2, self.beta3, self.lambda1, self.lambda2],
            dtype=np.float64,
        )

    @classmethod
    def from_array(cls, values: ArrayLike) -> NSSParams:
        """Rebuild the parameters from the optimiser's ordered vector.

        Raises:
            ValueError: If ``values`` does not hold exactly ``N_PARAMS`` entries
                in the order produced by :meth:`to_array`.
        """
        vector = np.asarray(values, dtype=np.float64).ravel()
        if vector.size != N_PARAMS:
            raise ValueError(
                f"expected {N_PARAMS} parameters in the order "
                f"{', '.join(f.name for f in fields(cls))}; got {vector.size}"
            )
        return cls(*(float(value) for value in vector))

    def is_admissible(self) -> bool:
        """Return whether the parameters satisfy the economic sanity constraints.

        Checks the positivity of the decay constants, ``beta0 > 0`` (positive
        long rate) and ``beta0 + beta1 > 0`` (positive instantaneous short rate).
        """
        return (
            self.lambda1 > 0.0
            and self.lambda2 > 0.0
            and self.beta0 > 0.0
            and self.beta0 + self.beta1 > 0.0
        )


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _level_loading(x: NDArray[np.float64]) -> NDArray[np.float64]:
    """The level (Nelson-Siegel slope) loading ``(1 - exp(-x)) / x``, safe at ``x = 0``.

    The removable singularity is handled by branching on
    :data:`LEVEL_LOADING_SERIES_CUTOFF` and evaluating
    ``1 - x/2 + x**2/6`` there. Two details matter:

    * the divisor is masked *before* the division, not after, because
      ``np.where`` evaluates both branches: writing
      ``np.where(small, 1.0, (1 - np.exp(-x)) / x)`` still divides by zero and
      still raises the warning it was meant to avoid; and
    * the main branch is ``-expm1(-x) / x`` rather than ``(1 - exp(-x)) / x``,
      which removes the catastrophic cancellation that the naive form suffers
      just above the cutoff.
    """
    small = np.abs(x) < LEVEL_LOADING_SERIES_CUTOFF
    safe = np.where(small, 1.0, x)
    ratio = -np.expm1(-safe) / safe
    series = 1.0 - x / 2.0 + x * x / 6.0
    return np.asarray(np.where(small, series, ratio), dtype=np.float64)


def _decay_ratios(
    tau: NDArray[np.float64],
    lambda1: float,
    lambda2: float,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Return ``(tau / lambda1, tau / lambda2)`` after validating the decays."""
    for name, value in (("lambda1", lambda1), ("lambda2", lambda2)):
        if not np.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be finite and strictly positive; got {value!r}")
    return tau / lambda1, tau / lambda2


def _as_float_array(tau: ArrayLike) -> NDArray[np.float64]:
    """Coerce a maturity or maturity vector to a float64 array."""
    return np.asarray(tau, dtype=np.float64)


def _match_input_shape(values: NDArray[np.float64], tau: ArrayLike) -> FloatOrArray:
    """Return a Python float for scalar ``tau`` and the array otherwise."""
    if np.ndim(tau) == 0:
        return float(values)
    return values


def _as_params(params: NSSParams | Mapping[str, float]) -> NSSParams:
    """Accept either an :class:`NSSParams` or the equivalent mapping.

    The mapping form is what the Excel bridge, the YAML config and ad hoc
    notebook code hand over; normalising once here keeps the curve functions
    from each growing their own key handling.
    """
    if isinstance(params, NSSParams):
        return params
    names = [f.name for f in fields(NSSParams)]
    missing = [name for name in names if name not in params]
    if missing:
        raise ValueError(f"params is missing {', '.join(missing)}; expected {', '.join(names)}")
    return NSSParams(**{name: float(params[name]) for name in names})


# --------------------------------------------------------------------------- #
# Curve
# --------------------------------------------------------------------------- #
def nss_yield(
    tau: ArrayLike,
    beta0: float,
    beta1: float,
    beta2: float,
    beta3: float,
    lambda1: float,
    lambda2: float,
) -> FloatOrArray:
    """Continuously compounded spot rate at maturities ``tau`` (in years).

    The loose-scalar entry point: the same maths as :func:`nss_zero_rate`, with
    the six parameters spelled out rather than packed, which is what the
    optimiser's residual callable and the Excel UDFs need.

    Fully vectorised - ``tau`` may be a scalar or an array of any shape, and the
    result mirrors it. There is no Python loop, so a thousand-point curve for a
    plot or an OIS bootstrap costs one pass of numpy.

    Limits, both exact and both reached without dividing by zero:

    * ``tau -> 0``: the level loading tends to 1 and the two curvature loadings
      to 0, so ``z(0+) = beta0 + beta1``, the instantaneous short rate.
    * ``tau -> inf``: every loading decays to 0, so ``z(inf) = beta0``.

    Args:
        tau: Maturity, or maturities, in years. Negative values are defined
            mathematically but have no term-structure meaning.
        beta0: Level; the asymptotic long rate.
        beta1: Slope; ``beta0 + beta1`` is the instantaneous short rate.
        beta2: First curvature, humped around ``1.7933 * lambda1``.
        beta3: Second curvature, humped around ``1.7933 * lambda2``.
        lambda1: First decay constant in years; strictly positive.
        lambda2: Second decay constant in years; strictly positive.

    Returns:
        The zero rate as a decimal: a ``float`` for scalar ``tau``, otherwise an
        array shaped like ``tau``.

    Raises:
        ValueError: If either decay constant is not finite and strictly positive.
    """
    tau_array = _as_float_array(tau)
    x1, x2 = _decay_ratios(tau_array, lambda1, lambda2)
    level1 = _level_loading(x1)
    level2 = _level_loading(x2)
    zero = (
        beta0
        + beta1 * level1
        + beta2 * (level1 - np.exp(-x1))
        + beta3 * (level2 - np.exp(-x2))
    )
    return _match_input_shape(np.asarray(zero, dtype=np.float64), tau)


def nss_zero_rate(tau: ArrayLike, params: NSSParams | Mapping[str, float]) -> FloatOrArray:
    """Continuously compounded zero rate at maturities ``tau`` (in years).

    The packed-parameter face of :func:`nss_yield`; see it for the limits, the
    vectorisation guarantee and the raised errors.
    """
    p = _as_params(params)
    return nss_yield(tau, p.beta0, p.beta1, p.beta2, p.beta3, p.lambda1, p.lambda2)


def nss_instantaneous_forward(
    tau: ArrayLike,
    params: NSSParams | Mapping[str, float],
) -> FloatOrArray:
    """Instantaneous forward rate implied by ``params`` at maturities ``tau``.

    The analytic derivative ``f(tau) = d/dtau [z(tau) * tau]``, which for this
    parameterisation is the far simpler expression

    ``f(tau) = beta0 + beta1 e^-x1 + beta2 x1 e^-x1 + beta3 x2 e^-x2``

    with ``xi = tau / lambda_i``. It has no singularity at zero: ``f(0) =
    beta0 + beta1``, matching the ``tau -> 0`` limit of the zero rate, as it
    must.

    This is the *instantaneous* forward. The forward over a finite span is
    :func:`nss_forward_rate`.
    """
    p = _as_params(params)
    tau_array = _as_float_array(tau)
    x1, x2 = _decay_ratios(tau_array, p.lambda1, p.lambda2)
    decay1 = np.exp(-x1)
    decay2 = np.exp(-x2)
    forward = p.beta0 + p.beta1 * decay1 + p.beta2 * x1 * decay1 + p.beta3 * x2 * decay2
    return _match_input_shape(np.asarray(forward, dtype=np.float64), tau)


def nss_discount_factor(
    tau: ArrayLike,
    params: NSSParams | Mapping[str, float],
) -> FloatOrArray:
    """Discount factors ``exp(-z(tau) * tau)`` implied by ``params``.

    Continuous compounding, for the reasons set out in the module docstring; a
    discrete-basis quote is a presentation-layer conversion, not what this
    returns. ``DF(0) == 1.0`` exactly, since the ``tau -> 0`` branch of the
    level loading keeps ``z(0) * 0`` finite instead of ``nan * 0``.

    Args:
        tau: Maturity, or maturities, in years.
        params: An :class:`NSSParams` or a mapping with the same six keys.

    Returns:
        A ``float`` for scalar ``tau``, otherwise an array shaped like ``tau``.
    """
    tau_array = _as_float_array(tau)
    zero = np.asarray(nss_zero_rate(tau_array, params), dtype=np.float64)
    return _match_input_shape(np.exp(-zero * tau_array), tau)


def nss_forward_rate(
    tau1: float,
    tau2: float,
    params: NSSParams | Mapping[str, float],
) -> float:
    """Continuously compounded forward rate over the span ``[tau1, tau2]``.

    ``f(tau1, tau2) = [ln DF(tau1) - ln DF(tau2)] / (tau2 - tau1)``

    Because the curve is continuously compounded, ``ln DF(tau) = -z(tau) * tau``
    in closed form, so the identical quantity

    ``f(tau1, tau2) = [z(tau2) tau2 - z(tau1) tau1] / (tau2 - tau1)``

    is what is actually evaluated: same number, without the ``exp`` then ``log``
    round trip that would throw away digits at the long end.

    Reused by the Phase 5 OIS bootstrap (each new pillar is solved so its
    forward reprices the quoted swap) and by the Phase 6 USD/COP forwards under
    covered interest parity.

    Args:
        tau1: Start of the span in years; must be non-negative.
        tau2: End of the span in years; must be strictly greater than ``tau1``.
        params: An :class:`NSSParams` or a mapping with the same six keys.

    Returns:
        The forward rate as a decimal.

    Raises:
        ValueError: If the span is empty, inverted or starts before today.
    """
    if not np.isfinite(tau1) or not np.isfinite(tau2):
        raise ValueError(f"the span [{tau1!r}, {tau2!r}] must be finite")
    if tau1 < 0.0:
        raise ValueError(f"tau1 must be non-negative; got {tau1!r}")
    if tau2 <= tau1:
        raise ValueError(f"tau2 must be strictly greater than tau1; got [{tau1!r}, {tau2!r}]")
    p = _as_params(params)
    zero1 = float(nss_yield(tau1, p.beta0, p.beta1, p.beta2, p.beta3, p.lambda1, p.lambda2))
    zero2 = float(nss_yield(tau2, p.beta0, p.beta1, p.beta2, p.beta3, p.lambda1, p.lambda2))
    return (zero2 * tau2 - zero1 * tau1) / (tau2 - tau1)


def nss_loadings(
    tau: ArrayLike,
    lambda1: float,
    lambda2: float,
) -> NDArray[np.float64]:
    """Return the ``(len(tau), 4)`` design matrix of factor loadings.

    Column order matches ``(beta0, beta1, beta2, beta3)``. Holding
    ``lambda1``/``lambda2`` fixed makes the model linear in the betas, which is
    what allows the hybrid grid-plus-least-squares calibration to work.

    The curvature columns peak at ``tau = 1.7933 * lambda_i``, the root of
    ``u**2 + u + 1 = e**u``; that is where each hump sits, and it is the
    property ``tests/unit/test_nss_model.py`` pins down.
    """
    tau_array = np.atleast_1d(_as_float_array(tau))
    x1, x2 = _decay_ratios(tau_array, lambda1, lambda2)
    level1 = _level_loading(x1)
    level2 = _level_loading(x2)
    return np.column_stack(
        (
            np.ones_like(tau_array),
            level1,
            level1 - np.exp(-x1),
            level2 - np.exp(-x2),
        )
    )
