"""USD/COP forward pricing under CIP. Skeleton for Phase 8."""

from __future__ import annotations

import numpy as np
import pytest

from tes_pricer.math.fx_forward import OptionType, forward_rate_cip, garman_kohlhagen_price

pytestmark = pytest.mark.unit


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 8", strict=True)
def test_forward_equals_spot_when_curves_match() -> None:
    """Identical discount curves leave the forward at spot."""
    discount = np.array([0.97, 0.94], dtype=np.float64)
    forward = forward_rate_cip(4000.0, discount, discount)
    assert forward == pytest.approx(np.array([4000.0, 4000.0]))


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 8", strict=True)
def test_higher_cop_rate_implies_forward_above_spot() -> None:
    """COP rates above USD rates put the forward above spot: positive points."""
    cop_discount = np.array([0.91], dtype=np.float64)
    usd_discount = np.array([0.96], dtype=np.float64)
    forward = forward_rate_cip(4000.0, cop_discount, usd_discount)
    assert forward[0] > 4000.0


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 8", strict=True)
def test_put_call_parity() -> None:
    """Garman-Kohlhagen must satisfy C - P = S*exp(-rf*T) - K*exp(-rd*T)."""
    spot, strike, tau, r_cop, r_usd, vol = 4000.0, 4100.0, 1.0, 0.09, 0.04, 0.12
    call = garman_kohlhagen_price(spot, strike, tau, r_cop, r_usd, vol, OptionType.CALL)
    put = garman_kohlhagen_price(spot, strike, tau, r_cop, r_usd, vol, OptionType.PUT)
    expected = spot * np.exp(-r_usd * tau) - strike * np.exp(-r_cop * tau)
    assert call - put == pytest.approx(expected, abs=1e-8)
