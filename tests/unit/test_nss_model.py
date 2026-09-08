"""Nelson-Siegel-Svensson curve shape.

The properties encoded here are the ones a wrong implementation usually
violates first: the limits at zero and infinity, the removable singularity in
the loading terms, and the location of the curvature hump.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.optimize import brentq, minimize_scalar

from tes_pricer.math.nss_model import (
    NSSParams,
    nss_discount_factor,
    nss_forward_rate,
    nss_instantaneous_forward,
    nss_yield,
    nss_zero_rate,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def params(sample_nss_params: dict[str, float]) -> NSSParams:
    """The shared plausible COP curve, as an `NSSParams`."""
    return NSSParams(**sample_nss_params)


def _yield_at(tau: float | np.ndarray, params: NSSParams) -> float | np.ndarray:
    """Call the loose-scalar entry point with a packed parameter set."""
    return nss_yield(
        tau,
        params.beta0,
        params.beta1,
        params.beta2,
        params.beta3,
        params.lambda1,
        params.lambda2,
    )


# --------------------------------------------------------------------------- #
# 1. The tau -> 0 limit
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tau", [1e-10, 0.0])
def test_short_end_limit_is_beta0_plus_beta1(params: NSSParams, tau: float) -> None:
    """`z(0+) = beta0 + beta1`, finite, with no nan leaking out of the 0/0.

    Both the vanishing and the exactly-zero maturity are checked: numpy does not
    raise on `0.0 / 0.0`, it returns `nan` with a warning, so a missing limit
    branch shows up as a silently poisoned short end rather than as an error.
    """
    rate = _yield_at(tau, params)
    assert np.isfinite(rate)
    assert rate == pytest.approx(params.beta0 + params.beta1, abs=1e-6)


def test_short_end_limit_holds_for_the_whole_curve_api(params: NSSParams) -> None:
    """The same limit through the packed API, the forward and the discount factor."""
    assert nss_zero_rate(0.0, params) == pytest.approx(params.beta0 + params.beta1, abs=1e-12)
    assert nss_instantaneous_forward(0.0, params) == pytest.approx(
        params.beta0 + params.beta1, abs=1e-12
    )
    assert nss_discount_factor(0.0, params) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 2. The tau -> infinity limit
# --------------------------------------------------------------------------- #
def test_long_end_converges_to_beta0(params: NSSParams) -> None:
    """As tau grows the zero rate converges to beta0, at the analytic 1/tau rate.

    The convergence is *algebraic*, not exponential: the exponential parts of
    every loading are dead well before 1000 years, but the level terms decay
    only as `lambda / tau`, leaving

        z(tau) - beta0 -> [(beta1 + beta2) lambda1 + beta3 lambda2] / tau.

    So a tolerance tighter than that tail is a statement about the parameters,
    not about the implementation: with this curve the residual at tau = 1000 is
    about 7e-5, and 1e-6 is only reached past tau ~ 7e4. The test therefore
    pins the tail itself, which is the sharper claim.
    """
    tail = (params.beta1 + params.beta2) * params.lambda1 + params.beta3 * params.lambda2

    assert _yield_at(1000.0, params) == pytest.approx(params.beta0, abs=1e-3)
    assert _yield_at(1000.0, params) - params.beta0 == pytest.approx(tail / 1000.0, rel=1e-6)
    # Ten times further out, ten times closer: the 1/tau signature.
    assert _yield_at(10_000.0, params) - params.beta0 == pytest.approx(tail / 10_000.0, rel=1e-6)
    # And the absolute 1e-6 convergence, at the maturity where the tail allows it.
    assert _yield_at(1e6, params) == pytest.approx(params.beta0, abs=1e-6)


# --------------------------------------------------------------------------- #
# 3. Vectorisation is an optimisation, not a different model
# --------------------------------------------------------------------------- #
def test_vectorised_call_matches_elementwise_calls(params: NSSParams) -> None:
    """One array call must equal 100 scalar calls to the last bit that matters."""
    taus = np.linspace(0.0, 30.0, 100, dtype=np.float64)

    vectorised = np.asarray(_yield_at(taus, params), dtype=np.float64)
    elementwise = np.array([_yield_at(float(tau), params) for tau in taus], dtype=np.float64)

    assert vectorised.shape == taus.shape
    assert np.max(np.abs(vectorised - elementwise)) < 1e-12


def test_scalar_input_returns_a_scalar(params: NSSParams) -> None:
    """A scalar tau yields a float, an array yields an array: shapes are mirrored."""
    assert isinstance(_yield_at(5.0, params), float)
    assert np.shape(_yield_at(np.zeros((2, 3)), params)) == (2, 3)


# --------------------------------------------------------------------------- #
# 4. The curvature hump sits where the algebra says it does
# --------------------------------------------------------------------------- #
def _hump_location_in_decay_units() -> float:
    """Derive the hump constant instead of hardcoding it.

    The beta2 loading is `L(u) = (1 - e^-u)/u - e^-u`, with `u = tau / lambda1`.
    Differentiating,

        L'(u) = [u e^-u - (1 - e^-u)] / u**2 + e^-u,

    and multiplying through by `u**2 > 0` turns `L'(u) = 0` into

        u e^-u - 1 + e^-u + u**2 e^-u = 0
        e^-u (u**2 + u + 1) = 1
        u**2 + u + 1 = e**u.

    That has the trivial root `u = 0` (where L itself vanishes) and one strictly
    positive root, which is the maximum. Bracket away from zero and solve.
    """
    return float(brentq(lambda u: np.exp(u) - (u * u + u + 1.0), 1.0, 5.0, xtol=1e-14))


def test_hump_peaks_at_the_derived_multiple_of_lambda1() -> None:
    """With beta1 = beta3 = 0 the curve is beta0 plus the beta2 hump alone."""
    constant = _hump_location_in_decay_units()
    assert constant == pytest.approx(1.7933, abs=1e-4)  # the textbook value, now derived

    humped = NSSParams(
        beta0=0.09, beta1=0.0, beta2=0.03, beta3=0.0, lambda1=1.5, lambda2=8.0
    )
    expected_tau = constant * humped.lambda1

    result = minimize_scalar(
        lambda tau: -float(_yield_at(float(tau), humped)),
        bounds=(1e-6, 50.0),
        method="bounded",
        options={"xatol": 1e-10},
    )

    assert result.success
    assert result.x == pytest.approx(expected_tau, rel=1e-6)
    assert -result.fun == pytest.approx(_yield_at(expected_tau, humped))
    # A maximum, not a boundary artefact.
    assert _yield_at(expected_tau, humped) > _yield_at(expected_tau * 0.5, humped)
    assert _yield_at(expected_tau, humped) > _yield_at(expected_tau * 2.0, humped)


# --------------------------------------------------------------------------- #
# Discount factors, forwards and parameter packing
# --------------------------------------------------------------------------- #
def test_discount_factors_are_monotone_and_bounded(params: NSSParams) -> None:
    """Discount factors start at one and decrease, given positive forwards."""
    tau = np.array([0.0, 0.5, 1.0, 5.0, 10.0, 30.0], dtype=np.float64)
    factors = nss_discount_factor(tau, params)
    assert factors[0] == pytest.approx(1.0)
    assert np.all(np.diff(factors) < 0.0)
    assert np.all((factors > 0.0) & (factors <= 1.0))


def test_discount_factor_accepts_a_plain_mapping(
    params: NSSParams, sample_nss_params: dict[str, float]
) -> None:
    """The Excel bridge and the YAML config hand over dicts, not dataclasses."""
    assert nss_discount_factor(7.0, sample_nss_params) == pytest.approx(
        nss_discount_factor(7.0, params)
    )


def test_forward_rate_matches_the_log_discount_factor_definition(params: NSSParams) -> None:
    """The closed form must equal `[ln DF(t1) - ln DF(t2)] / (t2 - t1)`."""
    tau1, tau2 = 2.0, 5.0
    from_factors = (
        np.log(float(nss_discount_factor(tau1, params)))
        - np.log(float(nss_discount_factor(tau2, params)))
    ) / (tau2 - tau1)
    assert nss_forward_rate(tau1, tau2, params) == pytest.approx(from_factors, rel=1e-12)


def test_forward_rate_collapses_to_the_instantaneous_forward(params: NSSParams) -> None:
    """Shrinking the span recovers the analytic instantaneous forward."""
    tau = 4.0
    span = nss_forward_rate(tau, tau + 1e-6, params)
    assert span == pytest.approx(float(nss_instantaneous_forward(tau, params)), abs=1e-6)


@pytest.mark.parametrize(("tau1", "tau2"), [(5.0, 5.0), (5.0, 2.0), (-1.0, 2.0)])
def test_forward_rate_rejects_a_degenerate_span(
    params: NSSParams, tau1: float, tau2: float
) -> None:
    """An empty, inverted or pre-today span is an error, not a signed rate."""
    with pytest.raises(ValueError):
        nss_forward_rate(tau1, tau2, params)


def test_roundtrip_through_parameter_vector(params: NSSParams) -> None:
    """`from_array(to_array(p))` must be the identity, in the optimiser's order."""
    assert NSSParams.from_array(params.to_array()) == params


def test_from_array_rejects_a_misshapen_vector() -> None:
    """Six parameters, no more and no fewer."""
    with pytest.raises(ValueError):
        NSSParams.from_array(np.arange(5, dtype=np.float64))


@pytest.mark.parametrize("bad_lambda", [0.0, -1.5, np.nan])
def test_non_positive_decay_constants_are_rejected(bad_lambda: float) -> None:
    """A decay constant of zero would put the singularity back in the loadings."""
    with pytest.raises(ValueError):
        nss_yield(1.0, 0.09, -0.01, 0.02, -0.01, bad_lambda, 8.0)


def test_admissibility_checks_the_economic_constraints() -> None:
    """Positive decays, positive long rate, positive instantaneous short rate."""
    assert NSSParams(0.09, -0.01, 0.02, -0.01, 1.5, 8.0).is_admissible()
    assert not NSSParams(0.09, -0.50, 0.02, -0.01, 1.5, 8.0).is_admissible()  # short rate < 0
    assert not NSSParams(-0.01, 0.05, 0.02, -0.01, 1.5, 8.0).is_admissible()  # long rate < 0
    assert not NSSParams(0.09, -0.01, 0.02, -0.01, 0.0, 8.0).is_admissible()  # lambda1 = 0
