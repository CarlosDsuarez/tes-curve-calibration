"""Nelson-Siegel-Svensson curve shape.

Skeleton for Phase 5. The properties encoded here are the ones a wrong
implementation usually violates first: the limits at zero and infinity, and the
removable singularity in the loading terms.
"""

from __future__ import annotations

import numpy as np
import pytest

from tes_pricer.math.nss_model import NSSParams, nss_discount_factor, nss_zero_rate

pytestmark = pytest.mark.unit


@pytest.fixture
def params(sample_nss_params: dict[str, float]) -> NSSParams:
    """The shared plausible COP curve, as an `NSSParams`."""
    return NSSParams(**sample_nss_params)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 5", strict=True)
def test_long_end_converges_to_beta0(params: NSSParams) -> None:
    """As tau grows, the zero rate converges to beta0."""
    tau = np.array([100.0, 200.0], dtype=np.float64)
    rates = nss_zero_rate(tau, params)
    assert rates[-1] == pytest.approx(params.beta0, abs=1e-3)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 5", strict=True)
def test_short_end_limit_is_beta0_plus_beta1(params: NSSParams) -> None:
    """The tau -> 0 limit is the instantaneous short rate, with no division by zero."""
    tau = np.array([1e-8], dtype=np.float64)
    rates = nss_zero_rate(tau, params)
    assert rates[0] == pytest.approx(params.beta0 + params.beta1, abs=1e-4)


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 5", strict=True)
def test_discount_factors_are_monotone_and_bounded(params: NSSParams) -> None:
    """Discount factors start at one and decrease, given positive forwards."""
    tau = np.array([0.0, 0.5, 1.0, 5.0, 10.0, 30.0], dtype=np.float64)
    factors = nss_discount_factor(tau, params)
    assert factors[0] == pytest.approx(1.0)
    assert np.all(np.diff(factors) < 0.0)
    assert np.all((factors > 0.0) & (factors <= 1.0))


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 5", strict=True)
def test_roundtrip_through_parameter_vector(params: NSSParams) -> None:
    """`from_array(to_array(p))` must be the identity, in the optimiser's order."""
    assert NSSParams.from_array(params.to_array()) == params
