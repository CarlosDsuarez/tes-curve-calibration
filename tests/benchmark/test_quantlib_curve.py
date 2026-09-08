"""Cross-check curve construction and FX pricing against QuantLib. See the sibling module."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.benchmark]

DISCOUNT_TOLERANCE = 1e-10


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 10", strict=True)
def test_ois_bootstrap_matches_quantlib_piecewise_curve() -> None:
    """Our OIS bootstrap must reproduce `PiecewiseLogLinearDiscount` on the pillars."""
    pytest.importorskip("QuantLib")
    raise NotImplementedError("Phase 10: OIS bootstrap benchmark")


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 10", strict=True)
def test_nss_curve_matches_quantlib_svensson_fitting() -> None:
    """Our NSS evaluation must match `QuantLib.SvenssonFitting` for shared parameters."""
    pytest.importorskip("QuantLib")
    raise NotImplementedError("Phase 10: NSS benchmark")


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 10", strict=True)
def test_garman_kohlhagen_matches_quantlib() -> None:
    """Our FX option price must match QuantLib's `AnalyticEuropeanEngine`."""
    pytest.importorskip("QuantLib")
    raise NotImplementedError("Phase 10: FX option benchmark")
