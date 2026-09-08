"""Cross-check bond pricing against QuantLib.

Deselected by default and skipped outright when QuantLib is absent (see
``tests/conftest.py``). Run with:

    pytest -m benchmark

QuantLib is the referee, not a dependency of the library: nothing under ``src/``
imports it. Tolerances are stated per test because "matches QuantLib" is
meaningless without one.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.benchmark]

PRICE_TOLERANCE = 1e-6
"""Per 100 of face."""

YIELD_TOLERANCE_BPS = 0.01


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 10", strict=True)
def test_clean_price_matches_quantlib() -> None:
    """Our clean price must match `QuantLib.FixedRateBond` within PRICE_TOLERANCE."""
    quantlib = pytest.importorskip("QuantLib")
    raise NotImplementedError(f"Phase 10: benchmark against QuantLib {quantlib.__version__}")


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 10", strict=True)
def test_ytm_matches_quantlib() -> None:
    """Our YTM solver must agree with QuantLib within YIELD_TOLERANCE_BPS."""
    pytest.importorskip("QuantLib")
    raise NotImplementedError("Phase 10: YTM benchmark")


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 10", strict=True)
def test_duration_and_convexity_match_quantlib() -> None:
    """Modified duration and convexity must agree with QuantLib's analytics."""
    pytest.importorskip("QuantLib")
    raise NotImplementedError("Phase 10: risk benchmark")
